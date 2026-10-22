# SPDX-License-Identifier: GPL-3.0-or-later
"""Reconocimiento de patrones entre la foto de dron y el mapa base.

Métodos:
  sift       - SIFT + RootSIFT + test de Lowe (clásico, invariante a rotación y escala)
  orb        - ORB (muy rápido)
  loftr      - LoFTR (red neuronal, kornia) - emparejamiento denso sin detector
  lightglue  - DISK + LightGlue (redes neuronales, kornia)
Todas las funciones de este módulo son seguras para ejecutarse en un hilo (no usan Qt).
"""
import importlib.util
import math

import numpy as np

try:
    import cv2
except ImportError:  # el diálogo avisa y ofrece instalarlo
    cv2 = None

METHODS = {
    "sift": "SIFT – clásico, robusto (solo OpenCV)",
    "orb": "ORB – muy rápido, menos preciso (solo OpenCV)",
    "lightglue": "IA: DISK + LightGlue (red neuronal, requiere torch+kornia)",
    "loftr": "IA: LoFTR outdoor (red neuronal, requiere torch+kornia)",
}
DEEP_METHODS = {"loftr", "lightglue"}

_MODELS = {}


def deep_available():
    return importlib.util.find_spec("torch") is not None and importlib.util.find_spec("kornia") is not None


# ----------------------------------------------------------------------------- imágenes

def _to_uint8(img):
    if img.dtype == np.uint8:
        return img
    a = img.astype(np.float32)
    lo, hi = np.percentile(a[np.isfinite(a)], (1, 99))
    if hi <= lo:
        hi = lo + 1
    return np.clip((a - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)


def load_image_rgb(path):
    """Carga una imagen como RGB uint8 (soporta rutas con acentos, 16 bits y TIFF multibanda)."""
    img, is_rgb = None, False
    try:
        img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    except Exception:
        img = None
    if img is None:
        from osgeo import gdal
        ds = gdal.Open(path)
        if ds is None:
            raise IOError(f"No se pudo leer la imagen: {path}")
        arr = ds.ReadAsArray()
        ds = None
        img = np.transpose(arr, (1, 2, 0)) if arr.ndim == 3 else arr
        is_rgb = True
    img = _to_uint8(img)
    if img.ndim == 2:
        img = np.dstack([img] * 3)
    elif img.shape[2] < 3:
        img = np.dstack([img[:, :, 0]] * 3)
    else:
        img = img[:, :, :3]
        if not is_rgb:
            img = img[:, :, ::-1]
    return np.ascontiguousarray(img)


def to_gray(rgb, clahe=True):
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    if clahe:
        g = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(g)
    return g


def valid_mask(rgb, erode=7):
    m = (rgb.max(axis=2) > 8).astype(np.uint8) * 255
    if erode:
        m = cv2.erode(m, np.ones((erode, erode), np.uint8))
    return m


def flat_fraction(rgb, block=16):
    """Fracción de bloques sin textura (teselas «Map data not yet available», nubes, vacíos)."""
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    h, w = (g.shape[0] // block) * block, (g.shape[1] // block) * block
    if h == 0 or w == 0:
        return 1.0
    b = g[:h, :w].reshape(h // block, block, w // block, block)
    return float((b.std(axis=(1, 3)) < 2.0).mean())


def resize(rgb, scale):
    """Devuelve imagen escalada y matriz 3x3 original->escalada (convención de centros de píxel)."""
    h, w = rgb.shape[:2]
    nw, nh = max(8, int(round(w * scale))), max(8, int(round(h * scale)))
    sx, sy = nw / w, nh / h
    S = np.array([[sx, 0, 0.5 * sx - 0.5], [0, sy, 0.5 * sy - 0.5], [0, 0, 1]], dtype=np.float64)
    if nw == w and nh == h:
        return rgb, np.eye(3)
    return cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_AREA), S


def rotate(rgb, angle_deg):
    """Rota la imagen (positivo = antihorario) ampliando el lienzo. Devuelve imagen, máscara y matriz 3x3."""
    h, w = rgb.shape[:2]
    a = angle_deg % 360.0
    if abs(a) < 1e-6 or abs(a - 360) < 1e-6:
        return rgb, np.full((h, w), 255, np.uint8), np.eye(3)
    c = ((w - 1) / 2.0, (h - 1) / 2.0)
    M = cv2.getRotationMatrix2D(c, a, 1.0)
    cos, sin = abs(M[0, 0]), abs(M[0, 1])
    nw, nh = int(round(h * sin + w * cos)), int(round(h * cos + w * sin))
    M[0, 2] += (nw - 1) / 2.0 - c[0]
    M[1, 2] += (nh - 1) / 2.0 - c[1]
    out = cv2.warpAffine(rgb, M, (nw, nh), flags=cv2.INTER_LINEAR, borderValue=0)
    mask = cv2.warpAffine(np.full((h, w), 255, np.uint8), M, (nw, nh), flags=cv2.INTER_NEAREST)
    mask = cv2.erode(mask, np.ones((7, 7), np.uint8))
    return out, mask, np.vstack([M, [0, 0, 1]])


# ----------------------------------------------------------------------------- emparejamiento

def _empty():
    return np.zeros((0, 2), np.float32), np.zeros((0, 2), np.float32)


def _ratio(knn, ratio):
    good = []
    for pair in knn:
        if len(pair) == 2 and pair[0].distance < ratio * pair[1].distance:
            good.append(pair[0])
    return good


def _match_sift(ga, gb, ma, mb, max_kp):
    sift = cv2.SIFT_create(nfeatures=max_kp, contrastThreshold=0.02)
    ka, da = sift.detectAndCompute(ga, ma)
    kb, db = sift.detectAndCompute(gb, mb)
    if da is None or db is None or len(ka) < 8 or len(kb) < 8:
        return _empty()

    def root(d):  # RootSIFT: mejor para cambios de iluminación (foto vs satélite)
        d = d / (np.abs(d).sum(axis=1, keepdims=True) + 1e-7)
        return np.sqrt(d).astype(np.float32)

    da, db = root(da), root(db)
    flann = cv2.FlannBasedMatcher(dict(algorithm=1, trees=5), dict(checks=64))
    good = _ratio(flann.knnMatch(da, db, k=2), 0.82)
    pa = np.float32([ka[m.queryIdx].pt for m in good]).reshape(-1, 2)
    pb = np.float32([kb[m.trainIdx].pt for m in good]).reshape(-1, 2)
    return pa, pb


def _match_orb(ga, gb, ma, mb, max_kp):
    orb = cv2.ORB_create(nfeatures=max_kp, scaleFactor=1.2, nlevels=10, fastThreshold=10)
    ka, da = orb.detectAndCompute(ga, ma)
    kb, db = orb.detectAndCompute(gb, mb)
    if da is None or db is None or len(ka) < 8 or len(kb) < 8:
        return _empty()
    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    good = _ratio(bf.knnMatch(da, db, k=2), 0.85)
    pa = np.float32([ka[m.queryIdx].pt for m in good]).reshape(-1, 2)
    pb = np.float32([kb[m.trainIdx].pt for m in good]).reshape(-1, 2)
    return pa, pb


def _get_model(name):
    if name in _MODELS:
        return _MODELS[name]
    import torch
    import kornia.feature as KF
    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if name == "loftr":
        m = KF.LoFTR(pretrained="outdoor")
    elif name == "disk":
        m = KF.DISK.from_pretrained("depth")
    else:
        m = KF.LightGlueMatcher("disk")
    m = m.eval().to(dev)
    _MODELS[name] = (m, dev)
    return _MODELS[name]


def _pad8(a):
    ph, pw = (-a.shape[0]) % 8, (-a.shape[1]) % 8
    pad = ((0, ph), (0, pw)) + ((0, 0),) * (a.ndim - 2)
    return np.pad(a, pad)


def _match_loftr(ga, gb):
    import torch
    m, dev = _get_model("loftr")
    ta = torch.from_numpy(_pad8(ga)).float()[None, None].to(dev) / 255.0
    tb = torch.from_numpy(_pad8(gb)).float()[None, None].to(dev) / 255.0
    with torch.inference_mode():
        out = m({"image0": ta, "image1": tb})
    k0 = out["keypoints0"].cpu().numpy()
    k1 = out["keypoints1"].cpu().numpy()
    keep = out["confidence"].cpu().numpy() > 0.2
    return k0[keep].astype(np.float32), k1[keep].astype(np.float32)


def _match_lightglue(ra, rb, max_kp):
    import torch
    import kornia.feature as KF
    disk, dev = _get_model("disk")
    lg, _ = _get_model("lightglue")

    def feats(rgb):
        t = torch.from_numpy(np.ascontiguousarray(rgb)).permute(2, 0, 1).float()[None].to(dev) / 255.0
        f = disk(t, n=max_kp, window_size=5, score_threshold=0.0, pad_if_not_divisible=True)[0]
        return f.keypoints, f.descriptors, torch.tensor(rgb.shape[:2], device=dev)

    with torch.inference_mode():
        k0, d0, hw0 = feats(ra)
        # el mapa base es el mismo en todos los intentos de rotación/escala: sus rasgos se reutilizan
        key = (rb.shape, max_kp, rb[::97, ::89].tobytes())
        cached = _MODELS.get("disk_base")
        if cached is None or cached[0] != key:
            cached = (key, feats(rb))
            _MODELS["disk_base"] = cached
        k1, d1, hw1 = cached[1]
        if len(k0) < 8 or len(k1) < 8:
            return _empty()
        l0 = KF.laf_from_center_scale_ori(k0[None], torch.ones(1, len(k0), 1, 1, device=dev))
        l1 = KF.laf_from_center_scale_ori(k1[None], torch.ones(1, len(k1), 1, 1, device=dev))
        _, idxs = lg(d0, d1, l0, l1, hw1=hw0, hw2=hw1)
    idxs = idxs.cpu().numpy()
    if len(idxs) == 0:
        return _empty()
    return (k0.cpu().numpy()[idxs[:, 0]].astype(np.float32),
            k1.cpu().numpy()[idxs[:, 1]].astype(np.float32))


def _inside(mask, pts):
    if mask is None or len(pts) == 0:
        return np.ones(len(pts), bool)
    x = np.clip(np.round(pts[:, 0]).astype(int), 0, mask.shape[1] - 1)
    y = np.clip(np.round(pts[:, 1]).astype(int), 0, mask.shape[0] - 1)
    return mask[y, x] > 0


def match(rgb_a, rgb_b, mask_a, mask_b, method, max_kp=8000, clahe=True):
    """Puntos homólogos (pa en imagen A, pb en imagen B) como arrays float32 Nx2."""
    if method in ("sift", "orb"):
        ga, gb = to_gray(rgb_a, clahe), to_gray(rgb_b, clahe)
        fn = _match_sift if method == "sift" else _match_orb
        return fn(ga, gb, mask_a, mask_b, max_kp)
    if method == "loftr":
        pa, pb = _match_loftr(to_gray(rgb_a, clahe), to_gray(rgb_b, clahe))
    elif method == "lightglue":
        pa, pb = _match_lightglue(rgb_a, rgb_b, max_kp)
    else:
        raise ValueError(f"Método desconocido: {method}")
    keep = _inside(mask_a, pa) & _inside(mask_b, pb)
    return pa[keep], pb[keep]


# ----------------------------------------------------------------------------- geometría

def estimate_homography(pa, pb, thresh=3.0):
    """Homografía robusta (MAGSAC++ si existe, si no RANSAC). Devuelve (H, máscara_inliers) o None."""
    if len(pa) < 8:
        return None
    method = getattr(cv2, "USAC_MAGSAC", cv2.RANSAC)
    H, mask = cv2.findHomography(pa, pb, method=method, ransacReprojThreshold=thresh,
                                 maxIters=10000, confidence=0.9995)
    if H is None or mask is None:
        return None
    inl = mask.ravel().astype(bool)
    if inl.sum() >= 8:  # reajuste por mínimos cuadrados con los inliers
        H2, _ = cv2.findHomography(pa[inl], pb[inl], 0)
        if H2 is not None:
            H = H2
    return H / H[2, 2], inl


def estimate_similarity(pa, pb, thresh=3.0):
    """Similitud (rotación+escala+traslación, 4 GL): robusta con pocas correspondencias en fotos nadir."""
    if len(pa) < 4:
        return None
    A, mask = cv2.estimateAffinePartial2D(pa, pb, method=cv2.RANSAC, ransacReprojThreshold=thresh,
                                          maxIters=20000, confidence=0.9995, refineIters=20)
    if A is None or mask is None:
        return None
    return np.vstack([A, [0, 0, 1]]), mask.ravel().astype(bool)


def estimate_transform(pa, pb, thresh=3.0):
    """Lista de modelos candidatos [(nombre, H 3x3, inliers)]: homografía y similitud."""
    out = []
    h = estimate_homography(pa, pb, thresh)
    if h is not None:
        out.append(("homografía", h[0], h[1]))
    s = estimate_similarity(pa, pb, thresh)
    if s is not None:
        out.append(("similitud", s[0], s[1]))
    return out


def apply_h(M, pts):
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    p = np.hstack([pts, np.ones((len(pts), 1))]) @ M.T
    return p[:, :2] / p[:, 2:3]


def _signed_area(p):
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))


def check_homography(H, corners_work, base_shape, expected_area=None):
    """Descarta homografías físicamente imposibles. Devuelve (ok, motivo)."""
    c = np.asarray(corners_work, dtype=np.float64)
    den = np.hstack([c, np.ones((4, 1))]) @ H[2]
    if not (np.all(den > 0) or np.all(den < 0)):
        return False, "la proyección cruza el horizonte"
    p = apply_h(H, c)
    if not np.all(np.isfinite(p)):
        return False, "proyección no finita"
    # convexidad y misma orientación (sin espejado)
    s0 = np.sign(_signed_area(c))
    for i in range(4):
        a, b, d = p[i], p[(i + 1) % 4], p[(i + 2) % 4]
        cross = (b[0] - a[0]) * (d[1] - b[1]) - (b[1] - a[1]) * (d[0] - b[0])
        if np.sign(cross) != s0:
            return False, "huella no convexa o espejada"
    area = abs(_signed_area(p))
    hb, wb = base_shape[:2]
    if expected_area:
        r = area / expected_area
        if r < 0.25 or r > 4.0:
            return False, f"escala incoherente con la altura (x{math.sqrt(r):.2f})"
    elif area < 0.0005 * hb * wb or area > 4.0 * hb * wb:
        return False, "tamaño de huella incoherente"
    sides = [np.linalg.norm(p[(i + 1) % 4] - p[i]) for i in range(4)]
    for i in (0, 1):
        lo, hi = sorted((sides[i], sides[i + 2]))
        if lo <= 0 or hi / lo > 3.0:
            return False, "deformación de perspectiva excesiva"
    return True, ""
