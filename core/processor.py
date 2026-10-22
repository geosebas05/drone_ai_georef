# SPDX-License-Identifier: GPL-3.0-or-later
"""Flujo completo para una imagen: metadatos -> mapa base -> patrones -> transformación -> productos."""
import math
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from osgeo import osr
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
)

from . import georef, matcher
from .exif import read_metadata
from .qt_utils import run_in_thread

# máximo tamaño del recorte de mapa base, puntos clave y umbral RANSAC (px) por método
METHOD_DEFAULTS = {
    "sift": dict(max_base_px=2600, max_kp=12000, thresh=3.0),
    "orb": dict(max_base_px=2600, max_kp=12000, thresh=3.0),
    "lightglue": dict(max_base_px=1280, max_kp=4096, thresh=3.0),
    "loftr": dict(max_base_px=1024, max_kp=0, thresh=3.0),
}
TRANSFORMS = {
    "projective": "Proyectiva / homografía (terreno plano, recomendado)",
    "poly1": "Polinomial 1 – afín (GDAL)",
    "poly2": "Polinomial 2 (GDAL)",
    "tps": "Thin Plate Spline (terreno irregular, GDAL)",
}


class Cancelled(Exception):
    pass


@dataclass
class Config:
    basemap: object
    method: str = "sift"
    work_crs: Optional[QgsCoordinateReferenceSystem] = None
    search_mode: str = "auto"          # auto (GPS, si no hay -> mapa) | canvas
    canvas_extent: Optional[QgsRectangle] = None
    canvas_crs: Optional[QgsCoordinateReferenceSystem] = None
    altitude: float = 0.0              # 0 = usar la de XMP
    gsd: float = 0.0                   # m/px de la foto; 0 = estimar con altura y focal
    gps_margin: float = 30.0
    default_radius: float = 250.0
    transform: str = "projective"
    refine: bool = True
    out_dir: str = ""
    save_points: bool = True
    out_res: float = 0.0               # 0 = resolución nativa de la foto
    min_inliers: int = 15
    max_base_px: int = 0
    clahe: bool = True


@dataclass
class ImageResult:
    path: str
    ok: bool = False
    message: str = ""
    out_path: str = ""
    points_path: str = ""
    crs: Optional[QgsCoordinateReferenceSystem] = None
    footprint: list = field(default_factory=list)
    inliers: int = 0
    matches: int = 0
    rmse_m: float = float("nan")
    gsd_m: float = float("nan")
    method: str = ""
    seconds: float = 0.0
    transform: Optional[np.ndarray] = None   # 3x3: píxel de la foto -> coordenada de mapa

    @property
    def quality(self):
        if not self.ok:
            return "fallida"
        if self.inliers >= 80 and self.rmse_m <= 1.0:
            return "alta"
        if self.inliers >= 30:
            return "media"
        return "baja"


@dataclass
class _Match:
    H: np.ndarray
    T: np.ndarray
    pa: np.ndarray
    pb: np.ndarray
    n: int
    total: int
    scale: float
    angle: float
    model: str = "homografía"


def _utm_crs(lon, lat):
    zone = int((lon + 180.0) / 6.0) + 1
    return QgsCoordinateReferenceSystem(f"EPSG:{(32600 if lat >= 0 else 32700) + zone}")


def _wkt(crs):
    srs = osr.SpatialReference()
    if crs.authid() and srs.SetFromUserInput(crs.authid()) == 0:
        return srs.ExportToWkt()
    return crs.toWkt()


def _render(source, extent, crs, res, max_px, log=None):
    """Renderiza el mapa base; si a esa resolución no hay imágenes (teselas vacías) baja de zoom."""
    for _ in range(4):
        rgb, B, r = _render_once(source, extent, crs, res, max_px)
        if matcher.flat_fraction(rgb) < 0.4 or source.zmax is None:
            return rgb, B, r
        if log:
            log(f"  Sin imágenes del mapa base a {r:.2f} m/px; se prueba a {2 * r:.2f} m/px")
        res = 2 * r
    return rgb, B, r


def _render_once(source, extent, crs, res, max_px):
    w = max(64, int(math.ceil(extent.width() / res)))
    h = max(64, int(math.ceil(extent.height() / res)))
    if max(w, h) > max_px:
        res *= max(w, h) / max_px
        w, h = int(math.ceil(extent.width() / res)), int(math.ceil(extent.height() / res))
    ext = QgsRectangle(extent.xMinimum(), extent.yMaximum() - h * res,
                       extent.xMinimum() + w * res, extent.yMaximum())
    rgb, vis = source.render(ext, crs, w, h)
    rx, ry = vis.width() / rgb.shape[1], vis.height() / rgb.shape[0]
    B = np.array([[rx, 0, vis.xMinimum() + 0.5 * rx], [0, -ry, vis.yMaximum() - 0.5 * ry], [0, 0, 1]])
    return rgb, B, rx


def _search(rgb, base_rgb, scales, angles, method, d, clahe, expected_known, is_cancelled):
    """Prueba escalas/rotaciones y devuelve el mejor emparejamiento válido + bitácora (corre en hilo)."""
    notes = []
    base_mask = matcher.valid_mask(base_rgb)
    if (base_mask > 0).mean() < 0.05:
        return None, ["El mapa base está vacío o sin cobertura en esta zona."]
    h0, w0 = rgb.shape[:2]
    best = None
    for s in scales:
        small, S = matcher.resize(rgb, s)
        if min(small.shape[:2]) < 48:
            notes.append(f"escala {s:.3f}: imagen demasiado pequeña frente al mapa base, se omite")
            continue
        for a in angles:
            if is_cancelled():
                raise Cancelled()
            work, mask, R = matcher.rotate(small, a)
            mask = np.minimum(mask, matcher.valid_mask(work, erode=5))
            T = R @ S
            pa, pb = matcher.match(work, base_rgb, mask, base_mask, method, d["max_kp"], clahe)
            tag = f"escala {s:.3f}, rotación {a % 360:.0f}°: {len(pa)} correspondencias"
            cw = matcher.apply_h(T, georef.corners(w0, h0))
            exp_area = (s * s * w0 * h0) if expected_known else None
            chosen, desc = None, []
            for model, H, inl in matcher.estimate_transform(pa, pb, d["thresh"]):
                ok, why = matcher.check_homography(H, cw, base_rgb.shape, exp_area)
                n = int(inl.sum())
                desc.append(f"{model} {n}" + ("" if ok else f" (rechazada: {why})"))
                if not ok:
                    continue
                # la homografía (8 GL) solo se prefiere con suficientes puntos; si no, similitud (4 GL)
                if model == "homografía" and n < 25:
                    continue
                if chosen is None or n > chosen.n + (0 if model == "homografía" else -10):
                    chosen = _Match(H, T, pa[inl], pb[inl], n, len(pa), s, a, model)
            notes.append(tag + (", " + ", ".join(desc) if desc else ", sin modelo"))
            if chosen and (best is None or chosen.n > best.n):
                best = chosen
            if best is not None and best.n >= 60 and best.n >= 0.2 * best.total:
                return best, notes
    return best, notes


def process_image(path, cfg, log, is_cancelled):
    t0 = time.time()
    res = ImageResult(path=path, method=cfg.method)
    project = QgsProject.instance()
    wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    d = dict(METHOD_DEFAULTS[cfg.method])
    if cfg.max_base_px:
        d["max_base_px"] = cfg.max_base_px

    md = read_metadata(path)
    log(f"  Metadatos: {md.summary()}")
    if md.pitch is not None and md.pitch > -60:
        log(f"  Aviso: foto oblicua (inclinación {md.pitch:.0f}°); la precisión será menor.")
    rgb = run_in_thread(matcher.load_image_rgb, path)
    H0, W0 = rgb.shape[:2]
    md.width, md.height = W0, H0

    # ---- zona de búsqueda
    use_gps = cfg.search_mode == "auto" and md.has_gps()
    if use_gps:
        lon, lat = md.lon, md.lat
    else:
        if cfg.canvas_extent is None:
            raise RuntimeError("La foto no tiene GPS y no hay extensión de mapa para buscar.")
        c = QgsCoordinateTransform(cfg.canvas_crs, wgs, project).transform(cfg.canvas_extent.center())
        lon, lat = c.x(), c.y()
        if cfg.search_mode == "auto":
            log("  Sin GPS: se busca en la extensión actual del mapa.")

    work = cfg.work_crs
    if work is None or not work.isValid() or work.isGeographic():
        work = _utm_crs(lon, lat)
        log(f"  SRC de trabajo: {work.authid()} (UTM automático)")
    res.crs = work

    alt = cfg.altitude if cfg.altitude > 0 else md.rel_alt
    gsd = md.estimated_gsd(alt) if alt else None
    if cfg.gsd > 0:
        gsd = cfg.gsd
        log(f"  GSD indicado por el usuario: {gsd * 100:.1f} cm/px")
    elif gsd:
        log(f"  GSD estimado de la foto: {gsd * 100:.1f} cm/px (altura {alt:.0f} m)")

    if use_gps:
        center = QgsCoordinateTransform(wgs, work, project).transform(QgsPointXY(lon, lat))
        radius = (0.5 * math.hypot(W0 * gsd, H0 * gsd) * 1.2 if gsd else cfg.default_radius) + cfg.gps_margin
        extent = QgsRectangle(center.x() - radius, center.y() - radius, center.x() + radius, center.y() + radius)
    else:
        extent = QgsCoordinateTransform(cfg.canvas_crs, work, project).transformBoundingBox(cfg.canvas_extent)
        center = extent.center()

    native = cfg.basemap.native_resolution(center, work)
    res1 = max(native, max(extent.width(), extent.height()) / d["max_base_px"])
    log(f"  Mapa base: {extent.width():.0f} x {extent.height():.0f} m a {res1:.2f} m/px")
    asked = res1
    base1, B1, res1 = _render(cfg.basemap, extent, work, res1, d["max_base_px"], log)
    if res1 > 1.5 * asked:  # la zona no tiene el zoom máximo: no pedirlo de nuevo al refinar
        native = res1
    if is_cancelled():
        raise Cancelled()

    if gsd:
        scales = [min(1.0, gsd / res1)]
    else:
        lb, ld = max(base1.shape[:2]), max(W0, H0)
        scales = []
        for f in (0.35, 0.6, 0.2, 0.9, 0.12):
            s = min(1.0, f * lb / ld)
            if all(abs(s - x) > 0.05 * x for x in scales):
                scales.append(s)
        log("  Sin altura/focal: se prueban varias escalas (indique el GSD o la altura para más fiabilidad)")
    # las redes neuronales no son invariantes a la rotación: sin rumbo se prueban giros cada 45°
    a0 = -md.yaw if md.yaw is not None else 0.0
    if cfg.method in matcher.DEEP_METHODS:
        angles = [a0 + k for k in (0, 180, 45, 315, 90, 270, 135, 225)]
        if md.yaw is None:
            log("  Sin rumbo en los metadatos: se prueban 8 orientaciones (más lento)")
    else:
        angles = [a0]

    best, notes = run_in_thread(_search, rgb, base1, scales, angles, cfg.method, d, cfg.clahe,
                                gsd is not None, is_cancelled)
    for n in notes:
        log("    " + n)
    if best is None or best.n < cfg.min_inliers:
        res.message = "No se encontraron suficientes patrones comunes con el mapa base."
        res.seconds = time.time() - t0
        return res
    B, base_res = B1, res1
    G = B @ best.H @ best.T

    # ---- refinamiento: recorte ajustado a la huella, foto rotada al norte y escala conocida
    if cfg.refine:
        fp = georef.footprint(G, W0, H0)
        (xmin, ymin), (xmax, ymax) = fp.min(axis=0), fp.max(axis=0)
        m = 0.15 * max(xmax - xmin, ymax - ymin) + 3 * res1
        ext2 = QgsRectangle(xmin - m, ymin - m, xmax + m, ymax + m)
        res2 = max(native, max(ext2.width(), ext2.height()) / d["max_base_px"])
        base2, B2, res2 = _render(cfg.basemap, ext2, work, res2, d["max_base_px"], log)
        est_gsd = georef.native_gsd(G, W0, H0)
        ang = georef.rotation_angle(G, W0, H0)
        best2, notes2 = run_in_thread(_search, rgb, base2, [min(1.0, est_gsd / res2)], [ang], cfg.method,
                                      d, cfg.clahe, True, is_cancelled)
        if best2 is not None and best2.n >= cfg.min_inliers:
            G2 = B2 @ best2.H @ best2.T
            shift = np.linalg.norm(georef.footprint(G2, W0, H0).mean(axis=0) - fp.mean(axis=0))
            if shift < 0.5 * max(xmax - xmin, ymax - ymin):
                log(f"  Refinamiento a {res2:.2f} m/px: {best2.n} inliers")
                best, B, base_res, G = best2, B2, res2, G2
            else:
                log("  Refinamiento descartado (incoherente con la primera pasada)")
        else:
            log("  Refinamiento sin resultado; se conserva la primera pasada")

    # ---- puntos de control y calidad
    err = np.linalg.norm(matcher.apply_h(best.H, best.pa) - best.pb, axis=1)
    px = matcher.apply_h(np.linalg.inv(best.T), best.pa)      # píxel de la foto original
    mp = matcher.apply_h(B, best.pb)                           # coordenada de mapa medida
    res.inliers, res.matches = best.n, best.total
    res.rmse_m = float(np.sqrt(np.mean(err ** 2)) * base_res)
    out_res = cfg.out_res if cfg.out_res > 0 else georef.native_gsd(G, W0, H0)

    stem = os.path.splitext(os.path.basename(path))[0]
    out_dir = cfg.out_dir or os.path.dirname(path)
    os.makedirs(out_dir, exist_ok=True)
    wkt = _wkt(work)
    out_path = os.path.join(out_dir, f"{stem}_georef.tif")

    mode = cfg.transform
    sel = georef.select_distributed(px, err, W0, H0, cells=8, per_cell=2 if mode == "tps" else 1)
    need = {"poly1": 3, "poly2": 6, "tps": 10}.get(mode, 0)
    if mode != "projective" and len(sel) < need:
        log(f"  Pocos puntos repartidos ({len(sel)}) para {mode}; se usa la transformación proyectiva.")
        mode = "projective"
    if mode == "projective":
        res.out_path, res.gsd_m = run_in_thread(georef.warp_projective, rgb, G, out_path, wkt, out_res)
    else:
        res.out_path, res.gsd_m = run_in_thread(georef.warp_gdal, path, px[sel], mp[sel], out_path, wkt,
                                                out_res, mode)
    if cfg.save_points:
        res.points_path = georef.write_points(os.path.join(out_dir, os.path.basename(path) + ".points"),
                                              px[sel], mp[sel], err[sel] * base_res, work.toWkt())
    res.transform = G
    res.footprint = [QgsPointXY(x, y) for x, y in georef.footprint(G, W0, H0)]
    res.ok = True
    res.seconds = time.time() - t0
    res.message = (f"{best.model}: {res.inliers} inliers de {res.matches}, RMSE {res.rmse_m:.2f} m, "
                   f"píxel {res.gsd_m * 100:.1f} cm, calidad {res.quality}")
    return res
