# SPDX-License-Identifier: GPL-3.0-or-later
"""Lectura de metadatos de fotos de dron (EXIF + XMP de DJI/Parrot/etc.) usando solo GDAL."""
import math
import re
from dataclasses import dataclass
from typing import Optional

from osgeo import gdal

_NUM = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


@dataclass
class DroneMetadata:
    path: str
    width: int = 0
    height: int = 0
    lat: Optional[float] = None
    lon: Optional[float] = None
    abs_alt: Optional[float] = None
    rel_alt: Optional[float] = None
    focal_mm: Optional[float] = None
    focal35: Optional[float] = None
    yaw: Optional[float] = None          # rumbo de la parte superior de la imagen, grados horarios desde el norte
    pitch: Optional[float] = None        # -90 = nadir
    camera: str = ""

    def has_gps(self):
        return self.lat is not None and self.lon is not None and not (self.lat == 0 and self.lon == 0)

    def horizontal_fov(self):
        """Campo de visión horizontal (rad) a partir de la focal equivalente 35 mm."""
        if self.focal35 and self.focal35 > 0:
            return 2.0 * math.atan(36.0 / (2.0 * self.focal35))
        return None

    def estimated_gsd(self, altitude):
        """Tamaño de píxel en el terreno (m) para una altura sobre el suelo dada (cámara en nadir)."""
        fov = self.horizontal_fov()
        if not fov or not altitude or altitude <= 0 or not self.width:
            return None
        ground_w = 2.0 * altitude * math.tan(fov / 2.0)
        return ground_w / float(max(self.width, self.height))

    def summary(self):
        parts = []
        if self.has_gps():
            parts.append(f"GPS {self.lat:.6f}, {self.lon:.6f}")
        else:
            parts.append("sin GPS")
        if self.rel_alt is not None:
            parts.append(f"altura relativa {self.rel_alt:.1f} m")
        if self.focal35:
            parts.append(f"focal 35mm {self.focal35:.0f}")
        if self.yaw is not None:
            parts.append(f"rumbo {self.yaw:.1f}°")
        if self.pitch is not None:
            parts.append(f"inclinación {self.pitch:.1f}°")
        if self.camera:
            parts.append(self.camera)
        return ", ".join(parts)


def _nums(text):
    return [float(x) for x in _NUM.findall(text or "")]


def _dms(text, ref):
    v = _nums(text)
    if not v:
        return None
    val = v[0] + (v[1] / 60.0 if len(v) > 1 else 0) + (v[2] / 3600.0 if len(v) > 2 else 0)
    if ref and ref.strip().upper()[:1] in ("S", "W"):
        val = -val
    return val


def _first(text):
    v = _nums(text)
    return v[0] if v else None


def _xmp_value(xmp, key):
    # atributo: drone-dji:GimbalYawDegree="+12.3"  |  elemento: <drone-dji:GimbalYawDegree>12.3</...>
    m = re.search(r"[:\s]" + key + r'\s*=\s*"\s*([-+]?[0-9.]+)', xmp)
    if not m:
        m = re.search(r"<[\w-]*:?" + key + r">\s*([-+]?[0-9.]+)\s*<", xmp)
    return float(m.group(1)) if m else None


def read_metadata(path):
    md = DroneMetadata(path=path)
    try:
        ds = gdal.Open(path)
    except Exception:
        ds = None
    if ds is None:
        return md
    md.width, md.height = ds.RasterXSize, ds.RasterYSize
    meta = ds.GetMetadata() or {}

    lat = _dms(meta.get("EXIF_GPSLatitude"), meta.get("EXIF_GPSLatitudeRef"))
    lon = _dms(meta.get("EXIF_GPSLongitude"), meta.get("EXIF_GPSLongitudeRef"))
    md.lat, md.lon = lat, lon
    md.abs_alt = _first(meta.get("EXIF_GPSAltitude"))
    md.focal_mm = _first(meta.get("EXIF_FocalLength"))
    md.focal35 = _first(meta.get("EXIF_FocalLengthIn35mmFilm"))
    md.camera = " ".join(x for x in (meta.get("EXIF_Make", ""), meta.get("EXIF_Model", "")) if x).strip()

    xmp_list = ds.GetMetadata("xml:XMP") or []
    xmp = xmp_list[0] if xmp_list else ""
    if xmp:
        md.rel_alt = _xmp_value(xmp, "RelativeAltitude")
        yaw = _xmp_value(xmp, "GimbalYawDegree")
        if yaw is None:
            yaw = _xmp_value(xmp, "FlightYawDegree")
        if yaw is None:
            yaw = _xmp_value(xmp, "Yaw")
        md.yaw = yaw
        md.pitch = _xmp_value(xmp, "GimbalPitchDegree")
        if not md.has_gps():
            la = _xmp_value(xmp, "GpsLatitude") or _xmp_value(xmp, "Latitude")
            lo = (_xmp_value(xmp, "GpsLongitude") or _xmp_value(xmp, "GpsLongtitude")
                  or _xmp_value(xmp, "Longitude"))
            if la is not None and lo is not None:
                md.lat, md.lon = la, lo
    ds = None
    return md
