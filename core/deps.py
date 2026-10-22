# SPDX-License-Identifier: GPL-3.0-or-later
"""Comprobación e instalación de dependencias opcionales (OpenCV, torch, kornia)."""
import importlib
import importlib.util
import os
import shutil
import site
import sys


def cv2_version():
    try:
        import cv2
        return cv2.__version__
    except Exception:
        return None


def deep_status():
    if importlib.util.find_spec("torch") is None or importlib.util.find_spec("kornia") is None:
        return None
    try:
        import torch
        import kornia
        dev = "GPU CUDA" if torch.cuda.is_available() else "CPU"
        return f"torch {torch.__version__} / kornia {kornia.__version__} ({dev})"
    except Exception as e:
        return f"error: {e}"


def find_python():
    cands = []
    exe = sys.executable or ""
    if os.path.basename(exe).lower().startswith("python"):
        cands.append(exe)
    for d in (sys.exec_prefix, sys.prefix):
        for n in ("python.exe", "python3.exe", os.path.join("bin", "python3"), os.path.join("bin", "python")):
            cands.append(os.path.join(d, n))
    for n in ("python3", "python"):
        w = shutil.which(n)
        if w:
            cands.append(w)
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def pip_args(packages):
    """Argumentos de pip fijando la versión de numpy de QGIS para no romperlo."""
    import numpy
    args = ["-m", "pip", "install", "--user", "--upgrade-strategy", "only-if-needed"]
    if "torch" in packages:  # rueda solo-CPU (~200 MB en vez de ~2.5 GB con CUDA)
        args += ["--index-url", "https://download.pytorch.org/whl/cpu", "--extra-index-url", "https://pypi.org/simple"]
    return args + [*packages, f"numpy=={numpy.__version__}"]


def refresh_paths():
    try:
        user_site = site.getusersitepackages()
        if user_site not in sys.path:
            site.addsitedir(user_site)
    except Exception:
        pass
    importlib.invalidate_caches()


CV2_PACKAGES = ["opencv-python-headless"]
DEEP_PACKAGES = ["torch", "kornia"]
