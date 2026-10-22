# SPDX-License-Identifier: GPL-3.0-or-later
"""Generación de productos georreferenciados (GeoTIFF, .points) a partir de la transformación encontrada.

G (3x3) mapea píxel de la imagen original (centro de píxel = coordenada entera) -> coordenada del mapa.
"""
import math
import os

import numpy as np
from osgeo import gdal

from .matcher import apply_h, cv2

GTIFF_OPTS = ["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=2", "BIGTIFF=IF_SAFER"]
MAX_OUT_SIDE = 30000


def corners(w, h):
    return np.array([[-0.5, -0.5], [w - 0.5, -0.5], [w - 0.5, h - 0.5], [-0.5, h - 0.5]], dtype=np.float64)


def footprint(G, w, h):
    return apply_h(G, corners(w, h))


def polygon_area(p):
    x, y = p[:, 0], p[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y)))


def native_gsd(G, w, h):
    return math.sqrt(polygon_area(footprint(G, w, h)) / float(w * h))


def rotation_angle(G, w, h):
    """Ángulo (grados, antihorario) que hay que rotar la imagen para dejarla con el norte arriba."""
    c = np.array([[w / 2.0, h / 2.0], [w / 2.0 + 10.0, h / 2.0]])
    p = apply_h(G, c)
    return math.degrees(math.atan2(p[1, 1] - p[0, 1], p[1, 0] - p[0, 0]))


def unique_path(path):
    if not os.path.exists(path):
        return path
    try:
        os.remove(path)  # falla si QGIS lo tiene abierto -> se usa otro nombre
        for ext in (".aux.xml", ".ovr"):
            if os.path.exists(path + ext):
                os.remove(path + ext)
        if not os.path.exists(path):
            return path
    except Exception:
        pass
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base}_{i}{ext}"):
        i += 1
    return f"{base}_{i}{ext}"


def _output_grid(fp, res):
    xmin, ymin = fp.min(axis=0)
    xmax, ymax = fp.max(axis=0)
    xmin = math.floor(xmin / res) * res
    ymax = math.ceil(ymax / res) * res
    ow = int(math.ceil((xmax - xmin) / res))
    oh = int(math.ceil((ymax - ymin) / res))
    if max(ow, oh) > MAX_OUT_SIDE:
        f = max(ow, oh) / MAX_OUT_SIDE
        res *= f
        ow, oh = int(math.ceil((xmax - xmin) / res)), int(math.ceil((ymax - ymin) / res))
    return xmin, ymax, ow, oh, res


def warp_projective(rgb, G, out_path, wkt, res):
    """Ortorrectifica la foto con la homografía (escena plana) y escribe un GeoTIFF RGBA."""
    h, w = rgb.shape[:2]
    fp = footprint(G, w, h)
    ox, oy, ow, oh, res = _output_grid(fp, res)
    O = np.array([[res, 0, ox + 0.5 * res], [0, -res, oy - 0.5 * res], [0, 0, 1]])
    M = np.linalg.inv(O) @ G
    rgba = np.dstack([rgb, np.full((h, w), 255, np.uint8)])
    out = cv2.warpPerspective(rgba, M, (ow, oh), flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
    out_path = unique_path(out_path)
    ds = gdal.GetDriverByName("GTiff").Create(out_path, ow, oh, 4, gdal.GDT_Byte, options=GTIFF_OPTS)
    ds.SetGeoTransform((ox, res, 0.0, oy, 0.0, -res))
    ds.SetProjection(wkt)
    interp = (gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand, gdal.GCI_AlphaBand)
    for i in range(4):
        b = ds.GetRasterBand(i + 1)
        b.WriteArray(out[:, :, i])
        b.SetColorInterpretation(interp[i])
    ds.BuildOverviews("AVERAGE", [2, 4, 8, 16])
    ds.FlushCache()
    ds = None
    return out_path, res


def warp_gdal(src_path, px, mp, out_path, wkt, res, mode):
    """Georreferencia con GDAL usando puntos de control (mode: 'tps' o 'poly1'/'poly2')."""
    gcps = [gdal.GCP(float(X), float(Y), 0.0, float(c) + 0.5, float(r) + 0.5) for (c, r), (X, Y) in zip(px, mp)]
    vrt = "/vsimem/drone_ai_georef_tmp.vrt"
    tmp = gdal.Translate(vrt, src_path, format="VRT", GCPs=gcps, outputSRS=wkt)
    kw = dict(format="GTiff", dstSRS=wkt, xRes=res, yRes=res, dstAlpha=True, resampleAlg="bilinear",
              creationOptions=GTIFF_OPTS, multithread=True)
    if mode == "tps":
        kw["tps"] = True
    else:
        kw["polynomialOrder"] = int(mode[-1])
    out_path = unique_path(out_path)
    ds = gdal.Warp(out_path, tmp, **kw)
    if ds is None:
        raise RuntimeError("GDAL Warp falló")
    ds.BuildOverviews("AVERAGE", [2, 4, 8, 16])
    ds = None
    tmp = None
    gdal.Unlink(vrt)
    return out_path, res


def select_distributed(px, err, w, h, cells=8, per_cell=1):
    """Elige puntos repartidos por toda la imagen (los de menor error en cada celda)."""
    if len(px) == 0:
        return np.zeros(0, int)
    cx = np.clip((px[:, 0] / w * cells).astype(int), 0, cells - 1)
    cy = np.clip((px[:, 1] / h * cells).astype(int), 0, cells - 1)
    key = cy * cells + cx
    order = np.lexsort((err, key))
    sel, last, cnt = [], -1, 0
    for i in order:
        if key[i] != last:
            last, cnt = key[i], 0
        if cnt < per_cell:
            sel.append(i)
            cnt += 1
    return np.array(sel, int)


def write_points(path, px, mp, residuals, crs_wkt):
    """Archivo .points compatible con el Georreferenciador de QGIS (y = -fila)."""
    path = unique_path(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"#CRS: {crs_wkt}\n")
        f.write("mapX,mapY,sourceX,sourceY,enable,dX,dY,residual\n")
        for (c, r), (X, Y), e in zip(px, mp, residuals):
            f.write(f"{X:.4f},{Y:.4f},{c + 0.5:.2f},{-(r + 0.5):.2f},1,0,0,{e:.4f}\n")
    return path
