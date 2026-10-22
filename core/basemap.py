# SPDX-License-Identifier: GPL-3.0-or-later
"""Fuentes de mapa base y renderizado de un recorte como imagen numpy con georreferencia conocida."""
import math
from urllib.parse import parse_qs

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDataSourceUri,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsProject,
    QgsRasterLayer,
    QgsUnitTypes,
)
from qgis.PyQt.QtCore import QEventLoop, QSize
from qgis.PyQt.QtGui import QColor

from .qt_utils import qimage_to_rgb

# Solo servicios cuyo uso desde software de terceros está permitido. Cualquier otra fuente (WMS
# nacional, ortofoto propia, XYZ que el usuario tenga derecho a usar) se elige como capa del proyecto.
PRESETS = {
    "Esri World Imagery (satélite)": (
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", 19),
    "OpenStreetMap": ("https://tile.openstreetmap.org/{z}/{x}/{y}.png", 19),
}

_WEB_MERC_RES0 = 156543.03392804097  # m/píxel en zoom 0 en el ecuador


def _meters_factor(crs):
    try:
        from qgis.core import Qgis
        meters = Qgis.DistanceUnit.Meters
    except AttributeError:
        meters = QgsUnitTypes.DistanceMeters
    return QgsUnitTypes.fromUnitToUnitFactor(crs.mapUnits(), meters)


class BaseMapSource:
    def __init__(self, layer, label, zmax=None):
        self.layer = layer
        self.label = label
        self.zmax = zmax

    @classmethod
    def from_preset(cls, name):
        url, zmax = PRESETS[name]
        uri = QgsDataSourceUri()
        uri.setParam("type", "xyz")
        uri.setParam("url", url)
        uri.setParam("zmax", str(zmax))
        uri.setParam("zmin", "0")
        layer = QgsRasterLayer(bytes(uri.encodedUri()).decode(), name, "wms")
        return cls(layer, name, zmax)

    @classmethod
    def from_layer(cls, layer):
        zmax = None
        if layer.providerType() == "wms":
            q = parse_qs(layer.source())
            if q.get("type", [""])[0] == "xyz" and q.get("zmax"):
                try:
                    zmax = int(q["zmax"][0])
                except ValueError:
                    zmax = 19
        return cls(layer, layer.name(), zmax)

    def is_valid(self):
        return self.layer is not None and self.layer.isValid()

    def native_resolution(self, center, crs):
        """Resolución nativa aproximada (m/píxel) del mapa base en el punto `center` (en `crs`)."""
        wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        lat = QgsCoordinateTransform(crs, wgs, QgsProject.instance()).transform(center).y()
        coslat = max(0.05, math.cos(math.radians(lat)))
        if self.zmax is not None:
            return _WEB_MERC_RES0 * coslat / (2 ** self.zmax)
        if self.layer.providerType() == "gdal":
            lcrs = self.layer.crs()
            r = self.layer.rasterUnitsPerPixelX()
            if lcrs.isGeographic():
                return r * 111320.0 * coslat
            return r * _meters_factor(lcrs)
        return 0.1  # WMS sin teselas: se pide la resolución que haga falta

    def render(self, extent, crs, width, height):
        """Renderiza el mapa base para `extent` (en `crs`). Debe llamarse desde el hilo principal."""
        ms = QgsMapSettings()
        ms.setLayers([self.layer])
        ms.setDestinationCrs(crs)
        ms.setOutputSize(QSize(int(width), int(height)))
        ms.setExtent(extent)
        ms.setBackgroundColor(QColor(0, 0, 0))
        ms.setTransformContext(QgsProject.instance().transformContext())
        job = QgsMapRendererParallelJob(ms)
        loop = QEventLoop()
        job.finished.connect(loop.quit)
        job.start()
        if job.isActive():
            loop.exec()
        img = job.renderedImage()
        return qimage_to_rgb(img), ms.visibleExtent()
