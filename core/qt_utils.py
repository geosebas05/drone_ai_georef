# SPDX-License-Identifier: GPL-3.0-or-later
"""Utilidades Qt: ejecutar trabajo pesado sin congelar QGIS y convertir QImage a numpy."""
import io
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from qgis.PyQt.QtCore import QEventLoop, QTimer
from qgis.PyQt.QtGui import QImage

_FMT_RGB888 = getattr(QImage, "Format_RGB888", None) or QImage.Format.Format_RGB888


def run_in_thread(fn, *args, **kwargs):
    """Ejecuta fn en un hilo y mantiene viva la interfaz (OpenCV/torch liberan el GIL).

    fn no debe tocar objetos Qt/QGIS de la interfaz. Mientras corre se silencian los warnings y se
    desvía stdout/stderr: en QGIS ambos terminan en widgets/log del hilo principal y, llamados desde
    el hilo de trabajo (torch/kornia emiten avisos y barras de progreso), pueden bloquear QGIS.
    """
    sink = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sys.stdout = sys.stderr = sink
        try:
            ex = ThreadPoolExecutor(max_workers=1)
            fut = ex.submit(fn, *args, **kwargs)
            loop = QEventLoop()
            timer = QTimer()
            timer.setInterval(40)
            timer.timeout.connect(lambda: loop.quit() if fut.done() else None)
            timer.start()
            if not fut.done():
                loop.exec()
            timer.stop()
            ex.shutdown(wait=False)
        finally:
            sys.stdout, sys.stderr = old_out, old_err
    return fut.result()


def qimage_to_rgb(img):
    img = img.convertToFormat(_FMT_RGB888)
    w, h, bpl = img.width(), img.height(), img.bytesPerLine()
    ptr = img.constBits()
    ptr.setsize(h * bpl)
    arr = np.frombuffer(ptr, np.uint8, count=h * bpl).reshape(h, bpl)
    return arr[:, : w * 3].reshape(h, w, 3).copy()
