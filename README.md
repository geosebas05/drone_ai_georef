# Drone AI Georef – plugin de QGIS

**Español** · [English](#english)

Georreferenciación **automática** de fotos de dron: el plugin reconoce patrones comunes (techos, calles,
bordes de parcelas, árboles…) entre cada foto y un mapa base de referencia, y calcula la transformación
sin que usted tenga que marcar puntos de control a mano.

## Instalación

**Desde QGIS (recomendado):** *Complementos ▸ Administrar e instalar complementos ▸ Todos*, busque
**Drone AI Georef** e instale. Mientras el plugin esté marcado como experimental, active antes
*Configuración ▸ Mostrar también los complementos experimentales*.

**Desde ZIP:** descargue `drone_ai_georef-<versión>.zip` de la página de *Releases* y use
*Complementos ▸ Instalar a partir de ZIP*.

**Dependencias:** al abrir el plugin, pulse **Instalar OpenCV** si se lo pide. Para los métodos de IA
pulse **Instalar IA (torch+kornia)** (~250 MB, versión CPU). El instalador usa `pip install --user` y fija
la versión de numpy de QGIS para no romperlo. Reinicie QGIS al terminar.

Instalación manual (consola *OSGeo4W Shell*):

```bat
python -m pip install --user opencv-python-headless
python -m pip install --user torch kornia --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple
```

## Uso

Abra el plugin con el botón de la barra o desde *Ráster ▸ Drone AI Georef*.

1. **Imágenes:** agregue fotos o carpetas (o arrástrelas). Use las fotos originales de la tarjeta del
   dron, porque traen GPS, altura y rumbo. Las copias enviadas por chat o WhatsApp los pierden.
2. **Mapa base:**
   - Lo más preciso es una ortofoto o un mosaico propio cargado en el proyecto («Capa del proyecto»).
   - Esri World Imagery sirve en cualquier parte, con precisión de 0,3 a 1 m.
   - Puede usar cualquier capa WMS o XYZ que tenga en el proyecto, por ejemplo los servicios del SNIT
     en Costa Rica o una capa XYZ que agregue usted desde el panel Navegador.
3. **Método:**
   - **SIFT** contra un mosaico u ortofoto propia: rápido, unos 20 s por foto.
   - **IA: DISK + LightGlue** o **LoFTR** contra imagen satelital. En CPU tarda de 1 a 3 min por foto
     si la foto trae el rumbo del dron. Sin rumbo prueba 8 orientaciones y puede tardar de 10 a 15 min.
     Con GPU NVIDIA tarda segundos.
4. **Zona de búsqueda:** *Automática* usa el GPS de la foto. Sin GPS, haga zoom a la zona y elija
   *Extensión actual del mapa*. Si la foto no trae altura ni focal, indique la altura o el GSD (cm/píxel).
5. **Salida:** elija el SRC (por ejemplo CRTM05, EPSG:5367) y la transformación:
   - **Proyectiva** para terreno plano (recomendada).
   - **Thin Plate Spline** para relieve marcado.
6. Pulse **Georreferenciar**.

**Productos:**
- `<foto>_georef.tif`: GeoTIFF RGBA.
- `<foto>.points`: puntos de control para revisar o corregir en *Ráster ▸ Georreferenciador*.
- Capa **«Huellas dron (IA)»**: inliers, RMSE, tamaño de píxel y calidad de cada foto.

## Cómo funciona

1. Lee los metadatos EXIF/XMP: GPS, altura relativa, focal equivalente y rumbo del gimbal. Con ellos
   estima la huella de la foto y el GSD.
2. Renderiza el mapa base alrededor de la foto. Si a esa resolución no hay imágenes, baja de zoom.
3. Empareja la foto con el mapa base (SIFT/RootSIFT, ORB, DISK + LightGlue o LoFTR).
4. Estima una homografía y una similitud con MAGSAC++/RANSAC. Descarta las soluciones imposibles:
   espejadas, con escala incoherente con la altura o con perspectiva excesiva.
5. Hace una segunda pasada con un recorte ajustado a la huella, la foto girada al norte y la escala ya
   conocida.

## Límites

- La precisión nunca es mejor que la del mapa base.
- Las fotos que cubren menos de unos 60 m tienen poco detalle en la imagen satelital: use IA o una
  referencia propia.
- Las fotos oblicuas y los edificios altos aumentan el error de la transformación proyectiva.

## Licencia y atribución

GPL-3.0-or-later (ver `LICENSE`). Si usa los mapas base integrados, respete sus condiciones:
- Esri World Imagery: *Esri, Maxar, Earthstar Geographics y la comunidad de usuarios GIS*.
- OpenStreetMap: © colaboradores de OpenStreetMap ([política de uso de teselas](https://operations.osmfoundation.org/policies/tiles/)).

El plugin no incluye Google; si tiene derecho a usar otra fuente, agréguela como capa del proyecto.

---

## English

QGIS plugin that **automatically georeferences drone photos**. It matches each photo against a
reference basemap with classic (SIFT/ORB) or deep-learning (DISK + LightGlue, LoFTR) feature matchers,
so you don't have to click ground control points by hand.

**Install:**
- From QGIS, go to *Plugins ▸ Manage and Install Plugins*, search **Drone AI Georef** and install it.
  Enable experimental plugins first while the plugin is flagged experimental.
- From a ZIP, use *Plugins ▸ Install from ZIP*.

Then click **Instalar OpenCV** in the dialog. Optionally click **Instalar IA** to add torch and kornia.

**Use:**
1. Add photos.
2. Pick a basemap: your own orthophoto (most accurate), Esri World Imagery, or any project layer.
3. Pick a method: SIFT against your own orthophoto, LightGlue against satellite imagery.
4. Click **Georreferenciar**.

**Outputs:**
- A GeoTIFF for each photo.
- A `.points` file you can open in the QGIS Georeferencer.
- A footprint layer with match count, RMSE and quality.

The user interface is in Spanish.

License: GPL-3.0-or-later.
