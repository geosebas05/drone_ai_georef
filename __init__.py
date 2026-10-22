# SPDX-License-Identifier: GPL-3.0-or-later
"""Drone AI Georef - georreferenciación automática de imágenes de dron."""


def classFactory(iface):
    from .core.deps import refresh_paths
    refresh_paths()  # paquetes instalados con pip --user después de abrir QGIS
    from .plugin import DroneAIGeorefPlugin
    return DroneAIGeorefPlugin(iface)
