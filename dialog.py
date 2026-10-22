# SPDX-License-Identifier: GPL-3.0-or-later
import os
import sys
import traceback

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFillSymbol,
    QgsGeometry,
    QgsMessageLog,
    QgsProject,
    QgsRasterLayer,
    QgsSettings,
    QgsVectorLayer,
)
from qgis.gui import QgsProjectionSelectionWidget
from qgis.PyQt.QtCore import QProcess, QProcessEnvironment
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from .core import deps
from .core.basemap import PRESETS, BaseMapSource
from .core.matcher import DEEP_METHODS, METHODS

IMG_EXT = (".jpg", ".jpeg", ".tif", ".tiff", ".png", ".dng")
SETTINGS = "drone_ai_georef/"
LOG_TAG = "Drone AI Georef"
FOOTPRINT_NAME = "Huellas dron (IA)"
GROUP_NAME = "Dron georreferenciado (IA)"


class GeorefDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self._cancel = False
        self._running = False
        self._presets = {}
        self._proc = None
        deps.refresh_paths()
        self.setWindowTitle("Georreferenciación automática de imágenes de dron (IA)")
        self.resize(760, 820)
        self.setAcceptDrops(True)
        self._build()
        self._load_settings()
        self.update_deps_label()

    # ------------------------------------------------------------------ interfaz
    def _build(self):
        lay = QVBoxLayout(self)

        g1 = QGroupBox("1. Imágenes de dron (arrastre aquí archivos o carpetas)")
        v1 = QVBoxLayout(g1)
        self.list_images = QListWidget()
        self.list_images.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        v1.addWidget(self.list_images)
        h = QHBoxLayout()
        for text, slot in (("Agregar imágenes…", self.add_files), ("Agregar carpeta…", self.add_folder),
                           ("Quitar", self.remove_selected), ("Limpiar", self.list_images.clear)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            h.addWidget(b)
        v1.addLayout(h)
        lay.addWidget(g1)

        g2 = QGroupBox("2. Referencia y reconocimiento de patrones")
        f2 = QFormLayout(g2)
        self.cmb_basemap = QComboBox()
        f2.addRow("Mapa base de referencia:", self.cmb_basemap)
        self.cmb_method = QComboBox()
        for k, label in METHODS.items():
            self.cmb_method.addItem(label, k)
        f2.addRow("Método de emparejamiento:", self.cmb_method)
        self.cmb_search = QComboBox()
        self.cmb_search.addItem("Automática: GPS de la foto (si no tiene, extensión del mapa)", "auto")
        self.cmb_search.addItem("Extensión actual del mapa en pantalla", "canvas")
        f2.addRow("Zona de búsqueda:", self.cmb_search)
        self.spin_alt = QDoubleSpinBox()
        self.spin_alt.setRange(0, 5000)
        self.spin_alt.setSuffix(" m")
        self.spin_alt.setSpecialValueText("automática (XMP)")
        f2.addRow("Altura de vuelo sobre el terreno:", self.spin_alt)
        self.spin_gsd = QDoubleSpinBox()
        self.spin_gsd.setRange(0, 10)
        self.spin_gsd.setDecimals(1)
        self.spin_gsd.setSingleStep(0.5)
        self.spin_gsd.setSuffix(" cm/px")
        self.spin_gsd.setSpecialValueText("automático (altura + focal)")
        f2.addRow("Tamaño de píxel de la foto (GSD):", self.spin_gsd)
        self.spin_margin = QDoubleSpinBox()
        self.spin_margin.setRange(0, 5000)
        self.spin_margin.setValue(30)
        self.spin_margin.setSuffix(" m")
        f2.addRow("Margen de error del GPS:", self.spin_margin)
        lay.addWidget(g2)

        g3 = QGroupBox("3. Salida")
        f3 = QFormLayout(g3)
        self.crs_widget = QgsProjectionSelectionWidget()
        self.crs_widget.setCrs(QgsProject.instance().crs())
        f3.addRow("SRC de salida (geográfico → UTM auto):", self.crs_widget)
        from .core.processor import TRANSFORMS
        self.cmb_transform = QComboBox()
        for k, label in TRANSFORMS.items():
            self.cmb_transform.addItem(label, k)
        f3.addRow("Transformación:", self.cmb_transform)
        self.spin_res = QDoubleSpinBox()
        self.spin_res.setRange(0, 100)
        self.spin_res.setDecimals(3)
        self.spin_res.setSingleStep(0.01)
        self.spin_res.setSuffix(" m")
        self.spin_res.setSpecialValueText("nativa de la foto")
        f3.addRow("Resolución de salida:", self.spin_res)
        h3 = QHBoxLayout()
        self.txt_out = QLineEdit()
        self.txt_out.setPlaceholderText("(vacío = misma carpeta de cada foto)")
        b = QPushButton("…")
        b.clicked.connect(self.pick_out_dir)
        h3.addWidget(self.txt_out)
        h3.addWidget(b)
        f3.addRow("Carpeta de salida:", h3)
        self.chk_refine = QCheckBox("Segunda pasada de refinamiento (más precisa)")
        self.chk_points = QCheckBox("Guardar puntos de control .points (para revisar en el Georreferenciador)")
        self.chk_footprint = QCheckBox("Crear capa de huellas con indicadores de calidad")
        self.chk_add = QCheckBox("Agregar resultados al proyecto")
        for c in (self.chk_refine, self.chk_points, self.chk_footprint, self.chk_add):
            c.setChecked(True)
            f3.addRow(c)
        lay.addWidget(g3)

        g4 = QGroupBox("Avanzado")
        g4.setCheckable(True)
        g4.setChecked(False)
        f4 = QFormLayout(g4)
        self.spin_inliers = QSpinBox()
        self.spin_inliers.setRange(6, 1000)
        self.spin_inliers.setValue(15)
        f4.addRow("Mínimo de correspondencias válidas:", self.spin_inliers)
        self.spin_maxpx = QSpinBox()
        self.spin_maxpx.setRange(0, 8000)
        self.spin_maxpx.setSpecialValueText("según método")
        f4.addRow("Tamaño máx. del recorte de mapa base (px):", self.spin_maxpx)
        self.chk_clahe = QCheckBox("Ecualizar contraste (CLAHE) antes de emparejar")
        self.chk_clahe.setChecked(True)
        f4.addRow(self.chk_clahe)
        self.adv_box = g4
        lay.addWidget(g4)

        h5 = QHBoxLayout()
        self.lbl_deps = QLabel()
        self.lbl_deps.setWordWrap(True)
        h5.addWidget(self.lbl_deps, 1)
        self.btn_cv2 = QPushButton("Instalar OpenCV")
        self.btn_cv2.clicked.connect(lambda: self.install(deps.CV2_PACKAGES))
        self.btn_deep = QPushButton("Instalar IA (torch+kornia)")
        self.btn_deep.clicked.connect(lambda: self.install(deps.DEEP_PACKAGES))
        h5.addWidget(self.btn_cv2)
        h5.addWidget(self.btn_deep)
        lay.addLayout(h5)

        self.progress = QProgressBar()
        lay.addWidget(self.progress)
        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumBlockCount(5000)
        lay.addWidget(self.txt_log, 1)

        h6 = QHBoxLayout()
        h6.addStretch(1)
        self.btn_run = QPushButton("Georreferenciar")
        self.btn_run.setDefault(True)
        self.btn_run.clicked.connect(self.run)
        self.btn_cancel = QPushButton("Cancelar")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel)
        b_close = QPushButton("Cerrar")
        b_close.clicked.connect(self.close)
        for b in (self.btn_run, self.btn_cancel, b_close):
            h6.addWidget(b)
        lay.addLayout(h6)

    def refresh_basemaps(self):
        current = self.cmb_basemap.currentData()
        self.cmb_basemap.clear()
        for name in PRESETS:
            self.cmb_basemap.addItem(name, "preset:" + name)
        for lyr in QgsProject.instance().mapLayers().values():
            if isinstance(lyr, QgsRasterLayer) and not lyr.name().endswith("_georef"):
                self.cmb_basemap.addItem(f"Capa del proyecto: {lyr.name()}", "layer:" + lyr.id())
        i = self.cmb_basemap.findData(current or QgsSettings().value(SETTINGS + "basemap", ""))
        if i >= 0:
            self.cmb_basemap.setCurrentIndex(i)

    def update_deps_label(self):
        cv = deps.cv2_version()
        deep = deps.deep_status()
        txt = [f"OpenCV {cv} ✔" if cv else "<b style='color:#c0392b'>OpenCV no instalado</b> (requerido)",
               f"IA: {deep} ✔" if deep else "IA (LoFTR/LightGlue): no instalada (opcional)"]
        self.lbl_deps.setText(" &nbsp;|&nbsp; ".join(txt))
        self.btn_cv2.setVisible(cv is None)
        self.btn_deep.setVisible(deep is None)

    # ------------------------------------------------------------------ imágenes
    def _add_paths(self, paths):
        existing = {self.list_images.item(i).text() for i in range(self.list_images.count())}
        for p in paths:
            if os.path.isdir(p):
                self._add_paths(sorted(os.path.join(p, f) for f in os.listdir(p)))
            elif p.lower().endswith(IMG_EXT) and p not in existing:
                self.list_images.addItem(p)
                existing.add(p)

    def add_files(self):
        start = QgsSettings().value(SETTINGS + "last_dir", "")
        files, _ = QFileDialog.getOpenFileNames(self, "Imágenes de dron", start,
                                                "Imágenes (*.jpg *.jpeg *.JPG *.tif *.tiff *.png *.dng)")
        if files:
            QgsSettings().setValue(SETTINGS + "last_dir", os.path.dirname(files[0]))
            self._add_paths(files)

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Carpeta de imágenes",
                                             QgsSettings().value(SETTINGS + "last_dir", ""))
        if d:
            QgsSettings().setValue(SETTINGS + "last_dir", d)
            self._add_paths([d])

    def remove_selected(self):
        for it in self.list_images.selectedItems():
            self.list_images.takeItem(self.list_images.row(it))

    def pick_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Carpeta de salida", self.txt_out.text())
        if d:
            self.txt_out.setText(d)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self._add_paths([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])

    # ------------------------------------------------------------------ ajustes
    def _load_settings(self):
        s = QgsSettings()
        for cmb, key in ((self.cmb_method, "method"), (self.cmb_transform, "transform"),
                         (self.cmb_search, "search")):
            i = cmb.findData(s.value(SETTINGS + key, ""))
            if i >= 0:
                cmb.setCurrentIndex(i)
        self.txt_out.setText(s.value(SETTINGS + "out_dir", ""))
        self.spin_margin.setValue(float(s.value(SETTINGS + "margin", 30)))

    def _save_settings(self):
        s = QgsSettings()
        s.setValue(SETTINGS + "method", self.cmb_method.currentData())
        s.setValue(SETTINGS + "transform", self.cmb_transform.currentData())
        s.setValue(SETTINGS + "search", self.cmb_search.currentData())
        s.setValue(SETTINGS + "basemap", self.cmb_basemap.currentData())
        s.setValue(SETTINGS + "out_dir", self.txt_out.text())
        s.setValue(SETTINGS + "margin", self.spin_margin.value())

    # ------------------------------------------------------------------ dependencias
    def install(self, packages):
        py = deps.find_python()
        if not py:
            QMessageBox.warning(self, LOG_TAG, "No se encontró el ejecutable de Python de QGIS.\n"
                                "Instale manualmente desde la consola OSGeo4W:\n"
                                f"python -m pip install {' '.join(packages)}")
            return
        args = deps.pip_args(packages)
        extra = "\n\nTorch ocupa ~250 MB y puede tardar varios minutos." if "torch" in packages else ""
        msg = f"Se ejecutará:\n\n{py} {' '.join(args)}{extra}\n\n¿Continuar?"
        yes = QMessageBox.StandardButton.Yes
        if QMessageBox.question(self, LOG_TAG, msg, yes | QMessageBox.StandardButton.No) != yes:
            return
        self.log(f"Instalando {' '.join(packages)} …")
        self._proc = QProcess(self)
        self._proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        if not env.contains("PYTHONHOME"):  # python3.exe de QGIS no arranca sin él
            env.insert("PYTHONHOME", sys.exec_prefix)
        self._proc.setProcessEnvironment(env)
        self._proc.readyReadStandardOutput.connect(
            lambda: self.log(bytes(self._proc.readAllStandardOutput()).decode(errors="replace").rstrip()))
        self._proc.finished.connect(self._install_finished)
        self.btn_cv2.setEnabled(False)
        self.btn_deep.setEnabled(False)
        self._proc.start(py, args)

    def _install_finished(self, code, _status=None):
        self.btn_cv2.setEnabled(True)
        self.btn_deep.setEnabled(True)
        deps.refresh_paths()
        if code == 0:
            self.log("Instalación terminada. Si el plugin no detecta el paquete, reinicie QGIS.")
            from .core import matcher
            if matcher.cv2 is None:
                try:
                    import cv2
                    matcher.cv2 = cv2
                    from .core import georef
                    georef.cv2 = cv2
                except ImportError:
                    pass
        else:
            self.log(f"pip terminó con código {code}. Revise el registro de arriba.")
        self.update_deps_label()

    # ------------------------------------------------------------------ ejecución
    def log(self, text):
        self.txt_log.appendPlainText(text)
        QgsMessageLog.logMessage(text, LOG_TAG, Qgis.MessageLevel.Info)

    def cancel(self):
        self._cancel = True
        self.log("Cancelando…")

    def closeEvent(self, e):
        if self._running:
            self._cancel = True
        super().closeEvent(e)

    def _basemap_source(self):
        key = self.cmb_basemap.currentData() or ""
        if key.startswith("preset:"):
            name = key[7:]
            if name not in self._presets:
                self._presets[name] = BaseMapSource.from_preset(name)
            return self._presets[name]
        lyr = QgsProject.instance().mapLayer(key[6:])
        return BaseMapSource.from_layer(lyr) if lyr else None

    def _footprint_layer(self, crs):
        for lyr in QgsProject.instance().mapLayersByName(FOOTPRINT_NAME):
            if isinstance(lyr, QgsVectorLayer):
                return lyr
        fields = ("field=imagen:string(254)&field=metodo:string(40)&field=inliers:integer"
                  "&field=rmse_m:double&field=pixel_cm:double&field=calidad:string(20)&field=salida:string(254)")
        lyr = QgsVectorLayer("Polygon?" + fields, FOOTPRINT_NAME, "memory")
        lyr.setCrs(crs)
        lyr.renderer().setSymbol(QgsFillSymbol.createSimple(
            {"color": "0,0,0,0", "outline_color": "230,60,40", "outline_width": "0.6"}))
        QgsProject.instance().addMapLayer(lyr)
        return lyr

    def _group(self):
        root = QgsProject.instance().layerTreeRoot()
        g = root.findGroup(GROUP_NAME)
        return g or root.insertGroup(0, GROUP_NAME)

    def run(self):
        if self._running:
            return
        if deps.cv2_version() is None:
            QMessageBox.warning(self, LOG_TAG, "Se necesita OpenCV. Pulse «Instalar OpenCV» y vuelva a intentar.")
            return
        method = self.cmb_method.currentData()
        if method in DEEP_METHODS and deps.deep_status() is None:
            QMessageBox.warning(self, LOG_TAG, "Este método de IA necesita torch y kornia. Pulse "
                                "«Instalar IA» o elija SIFT.")
            return
        paths = [self.list_images.item(i).text() for i in range(self.list_images.count())]
        if not paths:
            QMessageBox.information(self, LOG_TAG, "Agregue al menos una imagen de dron.")
            return
        source = self._basemap_source()
        if source is None or not source.is_valid():
            QMessageBox.warning(self, LOG_TAG, "El mapa base seleccionado no es válido.")
            return

        from .core import processor
        canvas = self.iface.mapCanvas()
        cfg = processor.Config(
            basemap=source, method=method, work_crs=self.crs_widget.crs(),
            search_mode=self.cmb_search.currentData(),
            canvas_extent=canvas.extent(), canvas_crs=canvas.mapSettings().destinationCrs(),
            altitude=self.spin_alt.value(), gsd=self.spin_gsd.value() / 100.0, gps_margin=self.spin_margin.value(),
            transform=self.cmb_transform.currentData(), refine=self.chk_refine.isChecked(),
            out_dir=self.txt_out.text().strip(), save_points=self.chk_points.isChecked(),
            out_res=self.spin_res.value(),
            min_inliers=self.spin_inliers.value() if self.adv_box.isChecked() else 15,
            max_base_px=self.spin_maxpx.value() if self.adv_box.isChecked() else 0,
            clahe=self.chk_clahe.isChecked() if self.adv_box.isChecked() else True,
        )
        self._save_settings()
        self._running, self._cancel = True, False
        self.btn_run.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.progress.setRange(0, len(paths))
        self.progress.setValue(0)
        self.log(f"=== {len(paths)} imagen(es) · {self.cmb_method.currentText()} · {source.label} ===")

        ok = 0
        try:
            for i, path in enumerate(paths):
                if self._cancel:
                    break
                self.log(f"[{i + 1}/{len(paths)}] {os.path.basename(path)}")
                try:
                    r = processor.process_image(path, cfg, self.log, lambda: self._cancel)
                except processor.Cancelled:
                    break
                except Exception as e:
                    self.log(f"  ERROR: {e}")
                    QgsMessageLog.logMessage(traceback.format_exc(), LOG_TAG, Qgis.MessageLevel.Critical)
                    self.progress.setValue(i + 1)
                    continue
                self.log(("  ✔ " if r.ok else "  ✖ ") + r.message + f" ({r.seconds:.1f} s)")
                if r.ok:
                    ok += 1
                    self._publish(r)
                self.progress.setValue(i + 1)
        finally:
            self._running = False
            self.btn_run.setEnabled(True)
            self.btn_cancel.setEnabled(False)
        self.log(f"=== Terminado: {ok} de {len(paths)} georreferenciadas"
                 + (" (cancelado)" if self._cancel else "") + " ===")
        if ok:
            self.iface.messageBar().pushSuccess(LOG_TAG, f"{ok} imagen(es) georreferenciada(s)")

    def _publish(self, r):
        self.log(f"  → {r.out_path}")
        if r.points_path:
            self.log(f"  → {r.points_path}")
        if self.chk_add.isChecked():
            lyr = QgsRasterLayer(r.out_path, os.path.splitext(os.path.basename(r.out_path))[0])
            if lyr.isValid():
                QgsProject.instance().addMapLayer(lyr, False)
                self._group().insertLayer(0, lyr)
        if self.chk_footprint.isChecked() and r.footprint:
            fl = self._footprint_layer(r.crs)
            geom = QgsGeometry.fromPolygonXY([r.footprint + [r.footprint[0]]])
            if fl.crs() != r.crs:
                geom.transform(QgsCoordinateTransform(r.crs, fl.crs(), QgsProject.instance()))
            f = QgsFeature(fl.fields())
            f.setGeometry(geom)
            f.setAttributes([os.path.basename(r.path), r.method, r.inliers, round(r.rmse_m, 3),
                             round(r.gsd_m * 100, 2), r.quality, r.out_path])
            fl.dataProvider().addFeatures([f])
            fl.updateExtents()
            fl.triggerRepaint()
