# SPDX-License-Identifier: GPL-3.0-or-later
import os

from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

MENU = "Drone AI Georef"


class DroneAIGeorefPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None

    def initGui(self):
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icon.svg"))
        self.action = QAction(icon, "Georreferenciar imágenes de dron (IA)", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToRasterMenu(MENU, self.action)

    def unload(self):
        if self.action:
            self.iface.removePluginRasterMenu(MENU, self.action)
            self.iface.removeToolBarIcon(self.action)
        if self.dialog:
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None

    def run(self):
        if self.dialog is None:
            from .dialog import GeorefDialog
            self.dialog = GeorefDialog(self.iface, self.iface.mainWindow())
        self.dialog.refresh_basemaps()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
