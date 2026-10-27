"""Read-only dump of EVERYTHING a file carries as metadata (used by the TUI "all metadata" mode).

`dump_file(path)` returns rows (section, name, value). Unlike `peek.py`, which shows a curated list of
identifying fields, nothing is filtered here: every Exif tag, XMP property, IPTC dataset, PNG chunk, PDF
key, OOXML/ODF property, video tag and so on is listed so the user can see what a file really contains.

The function never raises and never writes anything. Every section is parsed on its own (a failure
adds a `could not read` row), all loops and reads are bounded (hostile files cannot hang it or eat
memory), at most 3000 rows of at most 300 characters are returned.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import stat as statmod
import struct
import subprocess
import time
import zipfile
import zlib
import xml.etree.ElementTree as ET
from pathlib import Path

from . import office, video
from .common import WipeError
from .images import jpeg_segments

MAX_ROWS = 3000
MAX_VALUE = 300
_FFPROBE_TIMEOUT = 60
_XML_CAP = 4 << 20  # largest XMP packet / XML part that is parsed
_ZIP_PART_CAP = 8 << 20
_ZIP_TOTAL = 256 << 20  # decompressed bytes read from one package
_JPEG_EXT = {".jpg", ".jpeg", ".jpe"}
_PNG_EXT = {".png", ".apng"}
_TIFF_EXT = {".tif", ".tiff"}
_HEIF_EXT = {".heic", ".heif", ".avif"}


class _Stop(BaseException):
    """Row cap reached (BaseException so that per-section `except Exception` cannot swallow it)."""


class _ZipCut(BaseException):
    """Decompression budget of a package spent (BaseException for the same reason as _Stop)."""


_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f  \u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")  # bidi controls reorder the line


def _clip(value, limit=MAX_VALUE) -> str:
    text = value if isinstance(value, str) else str(value)
    if _CTRL.search(text):
        text = _CTRL.sub(lambda m: "\\n" if m.group() == "\n" else ("\\r" if m.group() == "\r" else " "), text)
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    return text


class _Out:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []
        self.remap: str | None = None  # embedded pictures: every row goes to one section

    def add(self, section, name, value="") -> None:
        if len(self.rows) >= MAX_ROWS - 1:
            self.rows.append(("File", "note", "output truncated"))
            raise _Stop
        self.rows.append((_clip(self.remap or section, 100), _clip(name, 150), _clip(value)))

    def fail(self, section, exc) -> None:
        self.add(section, "could not read", f"{type(exc).__name__}: {exc}"[:200])


def _guard(out: _Out, section: str, fn, *args) -> None:
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001 - a broken section must not stop the dump
        out.fail(section, exc)


# ---------------------------------------------------------------- readers (bounded random access)

class _Mem:
    def __init__(self, data):
        self.data = data
        self.size = len(data)

    def read(self, off: int, n: int) -> bytes:
        if off < 0 or n <= 0 or off >= self.size:
            return b""
        return bytes(self.data[off:off + n])


class _FileRd:
    def __init__(self, fh, size: int):
        self.fh = fh
        self.size = size

    def read(self, off: int, n: int) -> bytes:
        if off < 0 or n <= 0 or off >= self.size:
            return b""
        self.fh.seek(off)
        return self.fh.read(min(n, self.size - off, 64 << 20))


# ---------------------------------------------------------------- small helpers

def _num(x) -> str:
    if isinstance(x, float):
        if x != x or x in (float("inf"), float("-inf")):
            return str(x)
        if x == int(x) and abs(x) < 1e15:
            return str(int(x))
        return f"{x:.10g}"
    return str(x)


def _hex(b: bytes, n: int = 16) -> str:
    return b[:n].hex(" ")


def _blob(b: bytes, total: int | None = None) -> str:
    total = len(b) if total is None else total
    return f"<{total} bytes>" + (" " + _hex(b) if b else "")


def _text(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("latin-1")


def _strings(data: bytes, minlen: int = 4, limit: int = 40) -> list[str]:
    found = re.findall(rb"[\x20-\x7e]{%d,}" % minlen, data[:1 << 20])
    return [s.decode("ascii") for s in found[:limit]]


def _size_text(n: int) -> str:
    return f"{n} bytes"


def _fmt_time(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except (OverflowError, OSError, ValueError):
        return str(ts)


# ---------------------------------------------------------------- Exif / TIFF tables

_TAGS = {
    0x000B: "ProcessingSoftware", 0x00FE: "NewSubfileType", 0x00FF: "SubfileType", 0x0100: "ImageWidth",
    0x0101: "ImageLength", 0x0102: "BitsPerSample", 0x0103: "Compression", 0x0106: "PhotometricInterpretation",
    0x0107: "Thresholding", 0x0108: "CellWidth", 0x0109: "CellLength", 0x010A: "FillOrder", 0x010D: "DocumentName",
    0x010E: "ImageDescription", 0x010F: "Make", 0x0110: "Model", 0x0111: "StripOffsets", 0x0112: "Orientation",
    0x0115: "SamplesPerPixel", 0x0116: "RowsPerStrip", 0x0117: "StripByteCounts", 0x0118: "MinSampleValue",
    0x0119: "MaxSampleValue", 0x011A: "XResolution", 0x011B: "YResolution", 0x011C: "PlanarConfiguration",
    0x011D: "PageName", 0x011E: "XPosition", 0x011F: "YPosition", 0x0120: "FreeOffsets", 0x0121: "FreeByteCounts",
    0x0122: "GrayResponseUnit", 0x0123: "GrayResponseCurve", 0x0124: "T4Options", 0x0125: "T6Options",
    0x0128: "ResolutionUnit", 0x0129: "PageNumber", 0x012D: "TransferFunction", 0x0131: "Software", 0x0132: "DateTime",
    0x013B: "Artist", 0x013C: "HostComputer", 0x013D: "Predictor", 0x013E: "WhitePoint",
    0x013F: "PrimaryChromaticities", 0x0140: "ColorMap", 0x0141: "HalftoneHints", 0x0142: "TileWidth",
    0x0143: "TileLength", 0x0144: "TileOffsets", 0x0145: "TileByteCounts", 0x014A: "SubIFDs", 0x014C: "InkSet",
    0x014D: "InkNames", 0x014E: "NumberOfInks", 0x0150: "DotRange", 0x0151: "TargetPrinter", 0x0152: "ExtraSamples",
    0x0153: "SampleFormat", 0x0154: "SMinSampleValue", 0x0155: "SMaxSampleValue", 0x0156: "TransferRange",
    0x0157: "ClipPath", 0x0158: "XClipPathUnits", 0x0159: "YClipPathUnits", 0x015A: "Indexed",
    0x015B: "JPEGTables", 0x015F: "OPIProxy", 0x0190: "GlobalParametersIFD", 0x0191: "ProfileType",
    0x0192: "FaxProfile", 0x0193: "CodingMethods", 0x0194: "VersionYear", 0x0195: "ModeNumber", 0x01B1: "Decode",
    0x01B2: "DefaultImageColor", 0x01B3: "T82Options", 0x0200: "JPEGProc", 0x0201: "JPEGInterchangeFormat",
    0x0202: "JPEGInterchangeFormatLength", 0x0203: "JPEGRestartInterval", 0x0205: "JPEGLosslessPredictors",
    0x0206: "JPEGPointTransforms", 0x0207: "JPEGQTables", 0x0208: "JPEGDCTables", 0x0209: "JPEGACTables",
    0x0211: "YCbCrCoefficients", 0x0212: "YCbCrSubSampling", 0x0213: "YCbCrPositioning",
    0x0214: "ReferenceBlackWhite", 0x02BC: "XMLPacket", 0x4746: "Rating", 0x4749: "RatingPercent",
    0x800D: "ImageID", 0x80A3: "WangTag1", 0x828D: "CFARepeatPatternDim", 0x828E: "CFAPattern",
    0x828F: "BatteryLevel", 0x830E: "ModelPixelScaleTag", 0x8298: "Copyright", 0x829A: "ExposureTime",
    0x829D: "FNumber", 0x83BB: "IPTCNAA", 0x8480: "IntergraphMatrixTag", 0x8482: "ModelTiepointTag",
    0x85D8: "ModelTransformationTag", 0x8649: "ImageResources", 0x8769: "ExifTag", 0x8773: "InterColorProfile",
    0x87AF: "GeoKeyDirectoryTag", 0x87B0: "GeoDoubleParamsTag", 0x87B1: "GeoAsciiParamsTag",
    0x8822: "ExposureProgram", 0x8824: "SpectralSensitivity", 0x8825: "GPSTag", 0x8827: "ISOSpeedRatings",
    0x8828: "OECF", 0x8829: "Interlace", 0x882A: "TimeZoneOffset", 0x882B: "SelfTimerMode",
    0x8830: "SensitivityType", 0x8831: "StandardOutputSensitivity", 0x8832: "RecommendedExposureIndex",
    0x8833: "ISOSpeed", 0x8834: "ISOSpeedLatitudeyyy", 0x8835: "ISOSpeedLatitudezzz", 0x9000: "ExifVersion",
    0x9003: "DateTimeOriginal", 0x9004: "DateTimeDigitized", 0x9010: "OffsetTime", 0x9011: "OffsetTimeOriginal",
    0x9012: "OffsetTimeDigitized", 0x9101: "ComponentsConfiguration", 0x9102: "CompressedBitsPerPixel",
    0x9201: "ShutterSpeedValue", 0x9202: "ApertureValue", 0x9203: "BrightnessValue", 0x9204: "ExposureBiasValue",
    0x9205: "MaxApertureValue", 0x9206: "SubjectDistance", 0x9207: "MeteringMode", 0x9208: "LightSource",
    0x9209: "Flash", 0x920A: "FocalLength", 0x920B: "FlashEnergy", 0x920C: "SpatialFrequencyResponse",
    0x920D: "Noise", 0x920E: "FocalPlaneXResolution", 0x920F: "FocalPlaneYResolution",
    0x9210: "FocalPlaneResolutionUnit", 0x9211: "ImageNumber", 0x9212: "SecurityClassification",
    0x9213: "ImageHistory", 0x9214: "SubjectLocation", 0x9215: "ExposureIndex", 0x9216: "TIFFEPStandardID",
    0x9217: "SensingMethod", 0x927C: "MakerNote", 0x9286: "UserComment", 0x9290: "SubSecTime",
    0x9291: "SubSecTimeOriginal", 0x9292: "SubSecTimeDigitized", 0x935C: "ImageSourceData",
    0x9400: "AmbientTemperature", 0x9401: "Humidity", 0x9402: "Pressure", 0x9403: "WaterDepth",
    0x9404: "Acceleration", 0x9405: "CameraElevationAngle", 0x9C9B: "XPTitle", 0x9C9C: "XPComment",
    0x9C9D: "XPAuthor", 0x9C9E: "XPKeywords", 0x9C9F: "XPSubject", 0xA000: "FlashpixVersion", 0xA001: "ColorSpace",
    0xA002: "PixelXDimension", 0xA003: "PixelYDimension", 0xA004: "RelatedSoundFile", 0xA005: "InteroperabilityTag",
    0xA20B: "FlashEnergy", 0xA20C: "SpatialFrequencyResponse", 0xA20E: "FocalPlaneXResolution",
    0xA20F: "FocalPlaneYResolution", 0xA210: "FocalPlaneResolutionUnit", 0xA214: "SubjectLocation",
    0xA215: "ExposureIndex", 0xA217: "SensingMethod", 0xA300: "FileSource", 0xA301: "SceneType",
    0xA302: "CFAPattern", 0xA401: "CustomRendered", 0xA402: "ExposureMode", 0xA403: "WhiteBalance",
    0xA404: "DigitalZoomRatio", 0xA405: "FocalLengthIn35mmFilm", 0xA406: "SceneCaptureType", 0xA407: "GainControl",
    0xA408: "Contrast", 0xA409: "Saturation", 0xA40A: "Sharpness", 0xA40B: "DeviceSettingDescription",
    0xA40C: "SubjectDistanceRange", 0xA420: "ImageUniqueID", 0xA430: "CameraOwnerName",
    0xA431: "BodySerialNumber", 0xA432: "LensSpecification", 0xA433: "LensMake", 0xA434: "LensModel",
    0xA435: "LensSerialNumber", 0xA460: "CompositeImage", 0xA461: "SourceImageNumberOfCompositeImage",
    0xA462: "SourceExposureTimesOfCompositeImage", 0xA480: "GDALMetadata", 0xA481: "GDALNoData", 0xA500: "Gamma",
    0xC4A5: "PrintImageMatching", 0xC612: "DNGVersion", 0xC613: "DNGBackwardVersion", 0xC614: "UniqueCameraModel",
    0xC615: "LocalizedCameraModel", 0xC62F: "CameraSerialNumber", 0xC630: "LensInfo", 0xC68B: "OriginalRawFileName",
    0xC6F3: "CameraCalibrationSignature", 0xC6F4: "ProfileCalibrationSignature", 0xC6F8: "ProfileName",
    0xC764: "TimeCodes", 0xC7A1: "CameraLabel", 0xEA1C: "Padding", 0xEA1D: "OffsetSchema",
}
_GPS_TAGS = {
    0: "GPSVersionID", 1: "GPSLatitudeRef", 2: "GPSLatitude", 3: "GPSLongitudeRef", 4: "GPSLongitude",
    5: "GPSAltitudeRef", 6: "GPSAltitude", 7: "GPSTimeStamp", 8: "GPSSatellites", 9: "GPSStatus",
    10: "GPSMeasureMode", 11: "GPSDOP", 12: "GPSSpeedRef", 13: "GPSSpeed", 14: "GPSTrackRef", 15: "GPSTrack",
    16: "GPSImgDirectionRef", 17: "GPSImgDirection", 18: "GPSMapDatum", 19: "GPSDestLatitudeRef",
    20: "GPSDestLatitude", 21: "GPSDestLongitudeRef", 22: "GPSDestLongitude", 23: "GPSDestBearingRef",
    24: "GPSDestBearing", 25: "GPSDestDistanceRef", 26: "GPSDestDistance", 27: "GPSProcessingMethod",
    28: "GPSAreaInformation", 29: "GPSDateStamp", 30: "GPSDifferential", 31: "GPSHPositioningError",
}
_IOP_TAGS = {1: "InteroperabilityIndex", 2: "InteroperabilityVersion", 0x1000: "RelatedImageFileFormat",
             0x1001: "RelatedImageWidth", 0x1002: "RelatedImageLength"}
_MPF_TAGS = {0xB000: "MPFVersion", 0xB001: "NumberOfImages", 0xB002: "MPEntry", 0xB003: "ImageUIDList",
             0xB004: "TotalFrames", 0xB101: "MPIndividualNum", 0xB201: "PanOrientation", 0xB202: "PanOverlapH",
             0xB203: "PanOverlapV", 0xB204: "BaseViewpointNum", 0xB205: "ConvergenceAngle",
             0xB206: "BaselineLength", 0xB207: "VerticalDivergence", 0xB208: "AxisDistanceX",
             0xB209: "AxisDistanceY", 0xB20A: "AxisDistanceZ", 0xB20B: "YawAngle", 0xB20C: "PitchAngle",
             0xB20D: "RollAngle"}

_ENUMS = {
    0x0112: {1: "Normal", 2: "Mirrored horizontal", 3: "Rotate 180", 4: "Mirrored vertical",
             5: "Mirrored horizontal, rotate 270 CW", 6: "Rotate 90 CW", 7: "Mirrored horizontal, rotate 90 CW",
             8: "Rotate 270 CW"},
    0x0128: {1: "none", 2: "inches", 3: "centimeters"},
    0x9210: {1: "none", 2: "inches", 3: "centimeters", 4: "millimeters", 5: "micrometers"},
    0xA210: {1: "none", 2: "inches", 3: "centimeters", 4: "millimeters", 5: "micrometers"},
    0x0103: {1: "Uncompressed", 2: "CCITT 1D", 3: "T4/Group 3 Fax", 4: "T6/Group 4 Fax", 5: "LZW",
             6: "JPEG (old-style)", 7: "JPEG", 8: "Deflate (Adobe)", 9: "JBIG B&W", 10: "JBIG color",
             32773: "PackBits", 32946: "Deflate", 34712: "JPEG 2000", 34892: "Lossy JPEG", 50001: "WebP"},
    0x0106: {0: "WhiteIsZero", 1: "BlackIsZero", 2: "RGB", 3: "Palette", 4: "Transparency mask", 5: "CMYK",
             6: "YCbCr", 8: "CIELab", 9: "ICCLab", 10: "ITULab", 32803: "Color Filter Array", 34892: "LinearRaw"},
    0x010A: {1: "Normal", 2: "Reversed"},
    0x011C: {1: "Chunky", 2: "Planar"},
    0x013D: {1: "None", 2: "Horizontal differencing", 3: "Floating point"},
    0x0152: {0: "Unspecified", 1: "Associated alpha", 2: "Unassociated alpha"},
    0x0153: {1: "Unsigned integer", 2: "Signed integer", 3: "Floating point", 4: "Undefined"},
    0x0213: {1: "Centered", 2: "Co-sited"},
    0x8822: {0: "Not defined", 1: "Manual", 2: "Program AE", 3: "Aperture priority", 4: "Shutter priority",
             5: "Creative (slow)", 6: "Action (high speed)", 7: "Portrait", 8: "Landscape"},
    0x8830: {0: "Unknown", 1: "SOS", 2: "REI", 3: "ISO speed", 4: "SOS and REI", 5: "SOS and ISO speed",
             6: "REI and ISO speed", 7: "SOS, REI and ISO speed"},
    0x9207: {0: "Unknown", 1: "Average", 2: "Center-weighted average", 3: "Spot", 4: "Multi-spot",
             5: "Multi-segment", 6: "Partial", 255: "Other"},
    0x9208: {0: "Unknown", 1: "Daylight", 2: "Fluorescent", 3: "Tungsten", 4: "Flash", 9: "Fine weather",
             10: "Cloudy", 11: "Shade", 12: "Daylight fluorescent", 13: "Day white fluorescent",
             14: "Cool white fluorescent", 15: "White fluorescent", 16: "Warm white fluorescent",
             17: "Standard light A", 18: "Standard light B", 19: "Standard light C", 20: "D55", 21: "D65",
             22: "D75", 23: "D50", 24: "ISO studio tungsten", 255: "Other"},
    0xA001: {1: "sRGB", 2: "Adobe RGB", 0xFFFD: "Wide Gamut RGB", 0xFFFE: "ICC profile", 0xFFFF: "Uncalibrated"},
    0x9217: {1: "Not defined", 2: "One-chip color area", 3: "Two-chip color area", 4: "Three-chip color area",
             5: "Color sequential area", 7: "Trilinear", 8: "Color sequential linear"},
    0xA217: {1: "Not defined", 2: "One-chip color area", 3: "Two-chip color area", 4: "Three-chip color area",
             5: "Color sequential area", 7: "Trilinear", 8: "Color sequential linear"},
    0xA300: {1: "Film scanner", 2: "Reflection print scanner", 3: "Digital camera"},
    0xA301: {1: "Directly photographed"},
    0xA401: {0: "Normal", 1: "Custom", 2: "HDR (no original saved)", 3: "HDR (original saved)",
             4: "Original (for HDR)", 6: "Panorama", 7: "Portrait HDR", 8: "Portrait"},
    0xA402: {0: "Auto", 1: "Manual", 2: "Auto bracket"},
    0xA403: {0: "Auto", 1: "Manual"},
    0xA406: {0: "Standard", 1: "Landscape", 2: "Portrait", 3: "Night"},
    0xA407: {0: "None", 1: "Low gain up", 2: "High gain up", 3: "Low gain down", 4: "High gain down"},
    0xA408: {0: "Normal", 1: "Soft", 2: "Hard"},
    0xA409: {0: "Normal", 1: "Low", 2: "High"},
    0xA40A: {0: "Normal", 1: "Soft", 2: "Hard"},
    0xA40C: {0: "Unknown", 1: "Macro", 2: "Close", 3: "Distant"},
    0xA460: {0: "Unknown", 1: "Not a composite image", 2: "General composite image",
             3: "Composite image captured while shooting"},
}
_GPS_ENUMS = {
    5: {0: "Above sea level", 1: "Below sea level"},
    9: {"A": "Measurement in progress", "V": "Measurement interrupted"},
    10: {"2": "2-dimensional", "3": "3-dimensional"},
    12: {"K": "km/h", "M": "mph", "N": "knots"},
    14: {"T": "True north", "M": "Magnetic north"}, 16: {"T": "True north", "M": "Magnetic north"},
    23: {"T": "True north", "M": "Magnetic north"}, 25: {"K": "km", "M": "miles", "N": "nautical miles"},
    1: {"N": "North", "S": "South"}, 3: {"E": "East", "W": "West"}, 19: {"N": "North", "S": "South"},
    21: {"E": "East", "W": "West"}, 30: {0: "No correction", 1: "Differential correction applied"},
}
_COMPONENTS = {0: "-", 1: "Y", 2: "Cb", 3: "Cr", 4: "R", 5: "G", 6: "B"}
_TYPE_INFO = {1: (1, "B"), 2: (1, "c"), 3: (2, "H"), 4: (4, "I"), 5: (8, None), 6: (1, "b"), 7: (1, "B"),
              8: (2, "h"), 9: (4, "i"), 10: (8, None), 11: (4, "f"), 12: (8, "d"), 13: (4, "I"), 16: (8, "Q"),
              17: (8, "q"), 18: (8, "Q")}
_MAX_VALUES = 64  # array elements shown per tag
_MAX_TAG_READ = 4096  # bytes of a tag value that are looked at (XMP, IPTC, ICC, ... get more)
_BIG_TAG_READ = 4 << 20
_BIG_TAG_TOTAL = 32 << 20  # bytes of big tag values read per TIFF/Exif block
_BIG_TAGS = (0x02BC, 0x83BB, 0x8649, 0x8773, 0x927C, 0x935C)
_MAX_IFDS = 300


def _flash_text(v: int) -> str:
    parts = ["Flash fired" if v & 1 else "Flash did not fire"]
    ret = (v >> 1) & 3
    if ret == 2:
        parts.append("strobe return light not detected")
    elif ret == 3:
        parts.append("strobe return light detected")
    mode = (v >> 3) & 3
    parts.append({0: "", 1: "compulsory flash firing", 2: "compulsory flash suppression", 3: "auto mode"}[mode])
    if v & 0x20:
        parts.append("no flash function")
    if v & 0x40:
        parts.append("red-eye reduction")
    return ", ".join(p for p in parts if p)


def _rat(n: int, d: int):
    return n / d if d else None


def _dms(vals) -> str:
    d, m, s = vals[0], vals[1], vals[2]
    return f"{_num(d)}° {_num(m)}' {_num(round(s, 6))}\""


def _utf16(raw: bytes) -> str:
    return raw.decode("utf-16-le", "replace").rstrip("\x00")


def _user_comment(raw: bytes, order: str) -> str:
    code, body = raw[:8], raw[8:]
    if code == b"ASCII\x00\x00\x00":
        return _text(body).rstrip("\x00 ")
    if code == b"UNICODE\x00":  # UTF-16 in the byte order of the TIFF block, as exiv2 reads it
        enc = "utf-16" if body[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-16-le" if order == "<" else "utf-16-be"
        return "(UNICODE) " + body.decode(enc, "replace").rstrip("\x00 ")
    if code == b"JIS\x00\x00\x00\x00\x00":
        return "(JIS) " + body.decode("shift_jis", "replace").rstrip("\x00 ")
    return _text(raw).rstrip("\x00 ")


def _unpack(order: str, typ: int, cnt: int, raw: bytes):
    """Element list of a tag value: ints/floats, (n, d) tuples for rationals, bytes for ASCII/bytes."""
    size, code = _TYPE_INFO.get(typ, (1, "B"))
    cnt = min(cnt, len(raw) // size, 1 << 20)
    if code is None:
        fmt = order + ("II" if typ == 5 else "ii") * cnt
        vals = struct.unpack(fmt, raw[:8 * cnt])
        return [(vals[2 * i], vals[2 * i + 1]) for i in range(cnt)]
    if code == "c":
        return raw[:cnt]
    return list(struct.unpack(order + code * cnt, raw[:size * cnt]))


def _list_text(vals) -> str:
    shown = " ".join(_num(v) for v in vals[:_MAX_VALUES])
    return shown + (f" ... ({len(vals)} values)" if len(vals) > _MAX_VALUES else "")


def _tag_text(cls: str, tag: int, typ: int, cnt: int, raw: bytes, order: str, total: int, ctx: dict) -> str:
    """Human readable value of one TIFF/Exif tag. cls is main/gps/interop/mpf."""
    if typ not in _TYPE_INFO:
        return _blob(raw, total)
    if cls == "main" and tag in (0x9C9B, 0x9C9C, 0x9C9D, 0x9C9E, 0x9C9F):
        return _utf16(raw[:total])
    if typ == 2:
        parts = [p for p in raw[:cnt].split(b"\x00") if p]
        text = " | ".join(_text(p).strip() for p in parts)
        if cls == "gps":
            label = _GPS_ENUMS.get(tag, {}).get(text)
            if label:
                return f"{text} ({label})"
        return text
    if typ in (1, 7):
        data = raw[:cnt]
        if cls == "main" and tag in (0x9000, 0xA000) and len(data) == 4:
            return f"{_text(data)} ({int(data[:2] or b'0')}.{_text(data[2:])})" if data[:2].isdigit() else _blob(data)
        if cls == "interop" and tag == 2 and len(data) == 4:
            return _text(data)
        if cls == "main" and tag == 0x9101:
            return " ".join(str(b) for b in data) + " (" + " ".join(_COMPONENTS.get(b, "?") for b in data) + ")"
        if (cls == "main" and tag == 0x9286) or (cls == "gps" and tag in (27, 28)):
            return _user_comment(data, order)
        if cls == "gps" and tag == 0:
            return ".".join(str(b) for b in data)
        enum = _ENUMS.get(tag) if cls == "main" else _GPS_ENUMS.get(tag) if cls == "gps" else None
        if enum and cnt == 1:
            return f"{data[0]} ({enum[data[0]]})" if data and data[0] in enum else f"{data[0] if data else ''}"
        if cls == "gps" and tag == 5 and cnt == 1:
            return f"{data[0]}"
        if typ == 7 and len(data) >= 4 and re.fullmatch(rb"[\x20-\x7e\x00]+", data) and data.strip(b"\x00"):
            return _text(data).replace("\x00", " ").strip()
        if len(data) <= 16 and cnt == len(data):
            return " ".join(str(b) for b in data) if typ == 1 or cls != "main" or len(data) <= 8 else _blob(data, total)
        return _blob(data, total)
    vals = _unpack(order, typ, cnt, raw)
    if not vals:
        return ""
    if typ in (5, 10):
        fl = [_rat(n, d) for n, d in vals]
        if cls == "gps" and tag in (2, 4, 20, 22) and len(vals) == 3 and None not in fl:
            dec = fl[0] + fl[1] / 60 + fl[2] / 3600
            ref = ctx["gps_ref"].get(tag - 1, "")
            return f"{dec:.6f} {ref}".rstrip() + f" ({_dms(fl)})"
        if cls == "gps" and tag == 7 and len(vals) == 3 and None not in fl:
            return f"{int(fl[0]):02d}:{int(fl[1]):02d}:{_num(round(fl[2], 4))}"
        if cls == "gps" and tag in (6, 26, 31):
            suffix = ctx["gps_alt_ref"] if tag == 6 else ""
            return " ".join(_num(f) if f is not None else f"{n}/{d}" for f, (n, d) in zip(fl, vals)) + " m" + suffix
        if cls == "gps" and tag == 13 and fl[0] is not None:
            unit = {"K": "km/h", "M": "mph", "N": "knots"}.get(ctx["gps_ref"].get(12, ""), "")
            return (_num(fl[0]) + (" " + unit if unit else "")).strip()
        if cls == "main" and len(vals) == 1:
            n, d = vals[0]
            f = fl[0]
            if f is None:
                return f"{n}/{d} (undefined)"
            if tag == 0x829A:
                return f"{_num(f)} s ({n}/{d})" if 0 < f < 1 and d else f"{_num(f)} s"
            if tag == 0x920A:
                return f"{_num(f)} mm"
            if tag == 0x9204:
                return f"{_num(f)} EV"
            if tag == 0x9201:
                return f"{_num(f)} (1/{_num(2 ** f)} s)" if -50 < f < 50 else _num(f)
            if tag in (0x9202, 0x9205):
                return f"{_num(f)} (f/{_num(2 ** (f / 2))})" if -50 < f < 50 else _num(f)
            if tag == 0x9206:
                return f"{_num(f)} m"
        if cls == "main" and tag == 0xA432 and len(vals) == 4 and None not in fl:
            return " ".join(_num(f) for f in fl) + f" ({_num(fl[0])}-{_num(fl[1])} mm, f/{_num(fl[2])}-{_num(fl[3])})"
        return _list_text([_num(f) if f is not None else f"{n}/{d}" for f, (n, d) in zip(fl, vals)])
    if len(vals) == 1:
        v = vals[0]
        if cls == "main" and tag == 0x9209 and typ == 3:
            return f"{v} ({_flash_text(v)})"
        enum = _ENUMS.get(tag) if cls == "main" else None
        if enum is not None:
            return f"{v} ({enum[v]})" if v in enum else str(v)
        if typ in (11, 12):
            return _num(float(v))
        return str(v)
    if typ in (11, 12):
        vals = [float(v) for v in vals]
    return _list_text(vals)


def _is_sof(m: int) -> bool:
    return 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC)


# ---------------------------------------------------------------- Exif / TIFF structure walker

def _makernote(out: _Out, raw: bytes, total: int, make: str) -> None:
    prefixes = ((b"Apple iOS", "Apple"), (b"Nikon", "Nikon"), (b"OLYMP", "Olympus"), (b"SAMSUNG", "Samsung"),
                (b"SONY", "Sony"), (b"FUJIFILM", "Fujifilm"), (b"Panasonic", "Panasonic"), (b"LEICA", "Leica"),
                (b"PENTAX", "Pentax"), (b"AOC", "Pentax"), (b"RICOH", "Ricoh"), (b"Ricoh", "Ricoh"),
                (b"SIGMA", "Sigma"), (b"FOVEON", "Sigma"), (b"Canon", "Canon"), (b"Minolta", "Minolta"),
                (b"KDK", "Kodak"), (b"HUAWEI", "Huawei"), (b"Google", "Google"))
    maker = next((name for prefix, name in prefixes if raw.startswith(prefix)), None)
    if maker is None:
        maker = make.strip() + " (from Make)" if make.strip() else "unknown"
    out.add("MakerNote", "size", _size_text(total))
    out.add("MakerNote", "maker", maker)
    for s in _strings(raw):
        out.add("MakerNote", "string", s)


def _thumbnail(out: _Out, rd, offset: int, length: int) -> None:
    out.add("Thumbnail", "thumbnail image", f"JPEG, {length} bytes at offset {offset}")
    head = rd.read(offset, min(length, 262144))
    if head[:2] != b"\xff\xd8":
        out.add("Thumbnail", "thumbnail format", "not a JPEG stream: " + _hex(head, 8))
        return
    names = []
    try:
        for m, pos, end in jpeg_segments(head):
            if _is_sof(m) and end - pos >= 10:
                h, w = struct.unpack(">HH", head[pos + 5:pos + 9])
                out.add("Thumbnail", "thumbnail dimensions", f"{w}x{h}")
            elif 0xE0 <= m <= 0xEF or m == 0xFE:
                ident = head[pos + 4:pos + 20].split(b"\x00")[0]
                names.append(f"APP{m - 0xE0}" + (f" {ident.decode('latin-1')[:12]}" if ident.isalnum() else "") if m != 0xFE else "COM")
    except WipeError:
        pass  # a thumbnail cut at the read limit or damaged: list what came before
    if names:
        out.add("Thumbnail", "thumbnail segments", ", ".join(names))


def _mpf_entries(out: _Out, raw: bytes, order: str) -> None:
    for i in range(min(len(raw) // 16, 64)):
        attr, size, off = struct.unpack(order + "III", raw[16 * i:16 * i + 12])
        out.add("MPF", f"MPImage {i + 1}", f"{size} bytes at offset {off} (attributes 0x{attr:08X})")


def _tiff_block(out: _Out, rd, standalone: bool, depth: int = 0, mpf: bool = False) -> None:
    """Every IFD of a TIFF structure. `standalone`: a TIFF file (sections TIFF...), else an Exif block."""
    size = rd.size
    head = rd.read(0, 8)
    sec0 = "MPF" if mpf else "TIFF" if standalone else "Exif"
    if len(head) < 8 or head[:2] not in (b"II", b"MM"):
        out.add(sec0, "could not read", "not a TIFF/Exif byte order mark")
        return
    order = "<" if head[:2] == b"II" else ">"
    magic = struct.unpack(order + "H", head[2:4])[0]
    if magic == 43:
        out.add(sec0, "could not read", "BigTIFF is not parsed")
        return
    ctx = {"gps_ref": {}, "gps_alt_ref": "", "make": "", "big": set(), "big_bytes": 0}
    first = struct.unpack(order + "I", head[4:8])[0]
    queue = [(first, "mpf" if mpf else "ifd0", 1)]
    seen: set = set()
    n_ifds = 0
    sub_no = 0
    while queue and n_ifds < _MAX_IFDS:
        off, kind, page = queue.pop(0)
        if off <= 0 or off + 2 > size or (off, kind) in seen:
            continue
        seen.add((off, kind))
        n_ifds += 1
        sec = {"ifd0": "TIFF" if standalone else "Exif", "exif": "Exif", "gps": "Exif GPS", "interop": "Exif Interop",
               "ifd1": f"TIFF page {page}" if standalone else "Thumbnail", "sub": f"TIFF SubIFD {page}",
               "mpf": "MPF"}[kind]
        cls = {"gps": "gps", "interop": "interop", "mpf": "mpf"}.get(kind, "main")
        table = {"gps": _GPS_TAGS, "interop": _IOP_TAGS, "mpf": _MPF_TAGS}.get(cls, _TAGS)
        try:
            count = struct.unpack(order + "H", rd.read(off, 2))[0]
            body = rd.read(off + 2, 12 * min(count, 4096) + 4)
            n = min(count, 4096, len(body) // 12)
            records = []
            for i in range(n):
                tag, typ, cnt = struct.unpack(order + "HHI", body[12 * i:12 * i + 8])
                tsize = _TYPE_INFO.get(typ, (1, "B"))[0] * cnt
                if tsize <= 4:
                    voff, raw = off + 2 + 12 * i + 8, body[12 * i + 8:12 * i + 8 + tsize]
                else:
                    voff = struct.unpack(order + "I", body[12 * i + 8:12 * i + 12])[0]
                    raw = rd.read(voff, min(tsize, _MAX_TAG_READ))
                records.append((tag, typ, cnt, tsize, voff, raw))
            nxt = struct.unpack(order + "I", body[12 * n:12 * n + 4])[0] if len(body) >= 12 * n + 4 and n == count else 0
        except (struct.error, ValueError) as exc:
            out.add(sec, "could not read", f"IFD at offset {off}: {exc}")
            continue
        if cls == "gps":
            ctx["gps_ref"], ctx["gps_alt_ref"] = {}, ""
            for tag, typ, cnt, tsize, voff, raw in records:
                if typ == 2:
                    ctx["gps_ref"][tag] = _text(raw[:cnt]).strip("\x00 ")
                elif tag == 5 and raw:
                    ctx["gps_alt_ref"] = " (below sea level)" if raw[0] == 1 else " (above sea level)"
        position: dict = {}
        for tag, typ, cnt, tsize, voff, raw in records:
            name = table.get(tag) or f"Tag 0x{tag:04X}"
            # big values are read here, one at a time, once per (offset, size), within a per-block budget
            parse = depth < 4 and (voff, tsize) not in ctx["big"]
            if cls == "main" and tag in _BIG_TAGS and parse:
                ctx["big"].add((voff, tsize))
                if tsize > len(raw):
                    parse = ctx["big_bytes"] < _BIG_TAG_TOTAL
                    if parse:
                        raw = rd.read(voff, min(tsize, _BIG_TAG_READ))
                        ctx["big_bytes"] += len(raw)
            want = min(tsize, _BIG_TAG_READ if len(raw) > _MAX_TAG_READ else _MAX_TAG_READ)
            try:
                if len(raw) < want or (tsize > 4 and voff + tsize > size):
                    text = f"<value outside file: {tsize} bytes at offset {voff}>" if voff + min(tsize, 1) > size else \
                        _tag_text(cls, tag, typ, cnt, raw, order, tsize, ctx)
                else:
                    text = _tag_text(cls, tag, typ, cnt, raw, order, tsize, ctx)
            except (struct.error, ValueError, IndexError, KeyError, TypeError) as exc:
                text = f"<unreadable: {exc}>"
            if cls == "main" and tag in (0x927C, 0x02BC, 0x83BB, 0x8649, 0x8773, 0x935C, 0xC4A5) and tsize > 16:
                text = _blob(raw, tsize)
            out.add(sec, name, text)
            if cls == "gps" and tag in (2, 4) and typ == 5 and len(raw) >= 24:
                try:
                    v = struct.unpack(order + "6I", raw[:24])
                    if v[1] and v[3] and v[5]:
                        dec = v[0] / v[1] + v[2] / v[3] / 60 + v[4] / v[5] / 3600
                        ref = ctx["gps_ref"].get(tag - 1, "")
                        position[tag] = -dec if ref in ("S", "W") else dec
                except struct.error:
                    pass
            if cls != "main" and cls != "mpf":
                continue
            ptr = struct.unpack(order + "I", raw[:4])[0] if len(raw) >= 4 and typ in (4, 13) else 0
            if tag == 0x8769 and kind in ("ifd0", "ifd1", "sub"):
                queue.append((ptr, "exif", 1))
            elif tag == 0x8825 and kind in ("ifd0", "ifd1", "sub"):
                queue.append((ptr, "gps", 1))
            elif tag == 0xA005 and kind == "exif":
                queue.append((ptr, "interop", 1))
            elif tag == 0x014A and kind != "mpf":
                try:
                    code = "Q" if typ in (16, 18) else "I"
                    step = 8 if code == "Q" else 4
                    for k in range(min(len(raw) // step, 32)):
                        sub_no += 1
                        queue.append((struct.unpack(order + code, raw[step * k:step * k + step])[0], "sub", sub_no))
                except struct.error:
                    pass
            elif kind in ("ifd0", "ifd1", "exif", "sub") and tag == 0x010F and typ == 2:
                ctx["make"] = _text(raw[:cnt]).strip("\x00 ")
            elif tag == 0x927C and kind == "exif" and parse:
                _guard(out, "MakerNote", _makernote, out, raw, tsize, ctx["make"])
            elif tag == 0x02BC and parse:
                _guard(out, "XMP", _xmp, out, raw)
            elif tag == 0x83BB and parse:
                _guard(out, "IPTC", _iptc, out, raw)
            elif tag == 0x8649 and parse:
                _guard(out, "Photoshop", _irb, out, raw, depth + 1)
            elif tag == 0x8773 and parse:
                _guard(out, "ICC profile", _icc, out, raw)
            elif tag == 0xB002 and kind == "mpf":
                _guard(out, "MPF", _mpf_entries, out, raw, order)
        if len(position) == 2:
            out.add("Exif GPS", "GPSPosition", f"{position[2]:.6f}, {position[4]:.6f} (latitude, longitude)")
        if kind == "ifd1" and not standalone:
            by_tag = {r[0]: r for r in records}
            if 0x201 in by_tag and 0x202 in by_tag:
                try:
                    t_off = struct.unpack(order + "I", by_tag[0x201][5][:4])[0]
                    t_len = struct.unpack(order + "I", by_tag[0x202][5][:4])[0]
                    _guard(out, "Thumbnail", _thumbnail, out, rd, t_off, t_len)
                except struct.error:
                    pass
            else:
                out.add("Thumbnail", "thumbnail image", "IFD1 present without a JPEG stream")
        if kind == "ifd0" and nxt:
            queue.append((nxt, "ifd1", 2))
        elif kind == "ifd1" and nxt and standalone and page < 5000:
            queue.append((nxt, "ifd1", page + 1))
    if queue and n_ifds >= _MAX_IFDS:
        out.add(sec0, "note", f"more than {_MAX_IFDS} IFDs, the rest is not listed")


def _exif_block(out: _Out, data: bytes, depth: int = 0) -> None:
    _tiff_block(out, _Mem(data), False, depth)


# ---------------------------------------------------------------- XMP

_RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_XML_NS = "http://www.w3.org/XML/1998/namespace"
_XMP_PREFIXES = {
    "http://purl.org/dc/elements/1.1/": "dc", "http://ns.adobe.com/xap/1.0/": "xmp",
    "http://ns.adobe.com/xap/1.0/mm/": "xmpMM", "http://ns.adobe.com/xap/1.0/rights/": "xmpRights",
    "http://ns.adobe.com/xap/1.0/bj/": "xmpBJ", "http://ns.adobe.com/xap/1.0/t/pg/": "xmpTPg",
    "http://ns.adobe.com/xap/1.0/sType/ResourceRef#": "stRef", "http://ns.adobe.com/xap/1.0/sType/ResourceEvent#": "stEvt",
    "http://ns.adobe.com/photoshop/1.0/": "photoshop", "http://ns.adobe.com/exif/1.0/": "exif",
    "http://ns.adobe.com/exif/1.0/aux/": "aux", "http://ns.adobe.com/tiff/1.0/": "tiff",
    "http://ns.adobe.com/camera-raw-settings/1.0/": "crs", "http://ns.adobe.com/pdf/1.3/": "pdf",
    "http://ns.adobe.com/pdfx/1.3/": "pdfx", "http://ns.adobe.com/xap/1.0/g/img/": "xmpGImg",
    "http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/": "Iptc4xmpCore", "http://iptc.org/std/Iptc4xmpExt/2008-02-29/": "Iptc4xmpExt",
    "http://ns.google.com/photos/1.0/panorama/": "GPano", "http://www.metadataworkinggroup.com/schemas/regions/": "mwg-rs",
    "http://ns.apple.com/fix/1.0/": "apple-fi", "http://ns.microsoft.com/photo/1.0/": "MicrosoftPhoto",
    "http://ns.adobe.com/lightroom/1.0/": "lr", "http://ns.adobe.com/xmp/1.0/DynamicMedia/": "xmpDM",
    "http://ns.useplus.org/ldf/xmp/1.0/": "plus", "http://ns.adobe.com/illustrator/1.0/": "illustrator",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#": "rdf", "http://ns.adobe.com/xmp/note/": "xmpNote",
    "http://purl.org/dc/terms/": "dcterms", "http://prismstandard.org/namespaces/basic/2.0/": "prism",
    "http://ns.adobe.com/xap/1.0/sType/Dimensions#": "stDim", "http://ns.adobe.com/xap/1.0/sType/Font#": "stFnt",
}


def _decode_xml(data: bytes) -> str:
    if data[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):
        return data.decode("utf-32", "replace")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", "replace")
    if data[:2] == b"\x00<":
        return data.decode("utf-16-be", "replace")
    if data[:2] == b"<\x00":
        return data.decode("utf-16-le", "replace")
    return data.decode("utf-8", "replace")


def _xml_root(data: bytes):
    """(root, prefixes) of an XML part, or (None, reason). DTDs/entities are never processed."""
    text = _decode_xml(data[:_XML_CAP])
    low = text.lower()
    if "<!doctype" in low or "<!entity" in low:
        return None, "contains DOCTYPE/ENTITY declaration"
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text.lstrip("﻿ \t\r\n"), count=1)
    prefixes: dict = {}
    for prefix, uri in re.findall(r'xmlns:([\w.-]+)\s*=\s*["\']([^"\']*)["\']', text):
        prefixes.setdefault(uri, prefix)
    try:
        return ET.fromstring(text.encode("utf-8")), prefixes
    except (ET.ParseError, ValueError) as exc:
        return None, f"not well-formed XML ({exc})"


def _qname(tag: str, prefixes: dict) -> str:
    if not isinstance(tag, str):
        return "?"
    if tag.startswith("{"):
        ns, _, local = tag[1:].partition("}")
        prefix = prefixes.get(ns) or _XMP_PREFIXES.get(ns) or ns.rstrip("/#").rsplit("/", 1)[-1]
        return f"{prefix}:{local}"
    return tag


def _is_rdf(tag, local: str) -> bool:
    return tag == "{" + _RDF_NS + "}" + local


def _attr_items(el, prefixes):
    for k, v in el.attrib.items():
        if k.startswith("{" + _RDF_NS + "}") and k[len(_RDF_NS) + 2:] in ("about", "parseType", "nodeID", "ID", "datatype"):
            continue
        if k.startswith("{" + _XML_NS + "}"):
            continue
        yield _qname(k, prefixes), v


def _el_str(el, prefixes, depth: int = 0) -> str:
    if depth > 8:
        return "..."
    pieces = [f"{k}={v}" for k, v in _attr_items(el, prefixes)]
    for child in list(el)[:200]:
        if child.tag in ("{%s}Seq" % _RDF_NS, "{%s}Bag" % _RDF_NS, "{%s}Alt" % _RDF_NS):
            pieces.append(f"{_qname(child.tag, prefixes)}=[" + ", ".join(_li_str(li, prefixes, depth + 1) for li in list(child)[:200]) + "]")
        elif _is_rdf(child.tag, "Description"):
            pieces.append(_el_str(child, prefixes, depth + 1))
        else:
            pieces.append(f"{_qname(child.tag, prefixes)}={_el_str(child, prefixes, depth + 1)}")
    text = (el.text or "").strip()
    if not pieces:
        return text
    return ("{" + "; ".join(pieces) + "}") if not text else text + " {" + "; ".join(pieces) + "}"


def _li_str(li, prefixes, depth: int = 0) -> str:
    s = _el_str(li, prefixes, depth)
    lang = li.attrib.get("{%s}lang" % _XML_NS)
    return f"{s} [{lang}]" if lang and lang != "x-default" else s


def _xmp_prop(out: _Out, section: str, el, prefixes: dict, path: str, depth: int) -> None:
    name = path + _qname(el.tag, prefixes)
    kids = list(el)
    container = next((c for c in kids if c.tag in ("{%s}Seq" % _RDF_NS, "{%s}Bag" % _RDF_NS, "{%s}Alt" % _RDF_NS)), None)
    if container is not None:
        items = [_li_str(li, prefixes) for li in list(container)[:500]]
        out.add(section, "XMP " + name, ", ".join(items))
        return
    desc = next((c for c in kids if _is_rdf(c.tag, "Description")), None)
    attrs = list(_attr_items(el, prefixes))
    if (desc is not None or attrs or el.attrib.get("{%s}parseType" % _RDF_NS) == "Resource") and depth < 6 and (kids or attrs):
        holder = desc if desc is not None else el
        for k, v in list(_attr_items(holder, prefixes)) if desc is not None else attrs:
            out.add(section, "XMP " + name + "/" + k, v)
        for child in list(holder)[:500]:
            _xmp_prop(out, section, child, prefixes, name + "/", depth + 1)
        return
    out.add(section, "XMP " + name, (el.text or "").strip())


def _xmp(out: _Out, data: bytes, section: str = "XMP") -> None:
    data = data.rstrip(b"\x00 \r\n\t")
    root, info = _xml_root(data)
    if root is None:
        out.add(section, "XMP packet", f"{len(data)} bytes (not parsed: {info})")
        return
    out.add(section, "XMP packet", _size_text(len(data)))
    prefixes = info
    rdfs = [root] if _is_rdf(root.tag, "RDF") else [e for e in root.iter() if _is_rdf(e.tag, "RDF")][:4]
    if not rdfs:
        out.add(section, "XMP note", "no rdf:RDF element")
    for rdf in rdfs:
        for desc in list(rdf)[:500]:
            for k, v in _attr_items(desc, prefixes):
                out.add(section, "XMP " + k, v)
            for child in list(desc)[:2000]:
                _xmp_prop(out, section, child, prefixes, "", 0)


# ---------------------------------------------------------------- IPTC-IIM, Photoshop resources, ICC profile

_IPTC_1 = {0: "EnvelopeRecordVersion", 5: "Destination", 20: "FileFormat", 22: "FileVersion", 30: "ServiceIdentifier",
           40: "EnvelopeNumber", 50: "ProductID", 60: "EnvelopePriority", 70: "DateSent", 80: "TimeSent",
           90: "CodedCharacterSet", 100: "UniqueObjectName", 120: "ARMIdentifier", 122: "ARMVersion"}
_IPTC_2 = {0: "RecordVersion", 3: "ObjectTypeReference", 4: "ObjectAttributeReference", 5: "ObjectName",
           7: "EditStatus", 8: "EditorialUpdate", 10: "Urgency", 12: "SubjectReference", 15: "Category",
           20: "SupplementalCategories", 22: "FixtureIdentifier", 25: "Keywords", 26: "ContentLocationCode",
           27: "ContentLocationName", 30: "ReleaseDate", 35: "ReleaseTime", 37: "ExpirationDate",
           38: "ExpirationTime", 40: "SpecialInstructions", 42: "ActionAdvised", 45: "ReferenceService",
           47: "ReferenceDate", 50: "ReferenceNumber", 55: "DateCreated", 60: "TimeCreated",
           62: "DigitalCreationDate", 63: "DigitalCreationTime", 65: "OriginatingProgram", 70: "ProgramVersion",
           75: "ObjectCycle", 80: "By-line", 85: "By-lineTitle", 90: "City", 92: "Sub-location",
           95: "Province-State", 100: "Country-PrimaryLocationCode", 101: "Country-PrimaryLocationName",
           103: "OriginalTransmissionReference", 105: "Headline", 110: "Credit", 115: "Source",
           116: "CopyrightNotice", 118: "Contact", 120: "Caption-Abstract", 122: "Writer-Editor",
           125: "RasterizedCaption", 130: "ImageType", 131: "ImageOrientation", 135: "LanguageIdentifier",
           150: "AudioType", 151: "AudioSamplingRate", 152: "AudioSamplingResolution", 153: "AudioDuration",
           154: "AudioOutcue", 200: "ObjectDataPreviewFileFormat", 201: "ObjectDataPreviewFileFormatVersion",
           202: "ObjectDataPreviewData"}
_IPTC_NUMERIC = {"EnvelopeRecordVersion", "RecordVersion", "FileFormat", "FileVersion"}


def _iptc(out: _Out, data: bytes) -> None:
    pos, count, utf8 = 0, 0, False
    out.add("IPTC", "IPTC block", _size_text(len(data)))
    while pos + 5 <= len(data) and count < 2000:
        if data[pos] != 0x1C:
            break
        rec, ds = data[pos + 1], data[pos + 2]
        length = (data[pos + 3] << 8) | data[pos + 4]
        pos += 5
        if length & 0x8000:
            nbytes = length & 0x7FFF
            if nbytes > 4:
                break
            length = int.from_bytes(data[pos:pos + nbytes], "big")
            pos += nbytes
        val = data[pos:pos + length]
        pos += length
        count += 1
        table = _IPTC_1 if rec == 1 else _IPTC_2 if rec == 2 else {}
        name = table.get(ds) or f"IPTC {rec}:{ds}"
        if rec == 1 and ds == 90:
            utf8 = val.startswith(b"\x1b%G")
            out.add("IPTC", name, "UTF-8" if utf8 else _hex(val))
        elif name in _IPTC_NUMERIC and len(val) <= 4:
            out.add("IPTC", name, int.from_bytes(val, "big"))
        elif name == "ObjectDataPreviewData" or (rec not in (1, 2)) and not re.fullmatch(rb"[\x20-\x7e]*", val):
            out.add("IPTC", name, _blob(val))
        else:
            out.add("IPTC", name, val.decode("utf-8", "replace") if utf8 else _text(val))


_IRB_NAMES = {
    0x03E8: "Channels, rows, columns, depth, mode", 0x03ED: "ResolutionInfo", 0x03EE: "Alpha channel names",
    0x03EF: "DisplayInfo", 0x03F0: "Caption (Pascal string)", 0x03F1: "Border information", 0x03F2: "Background color",
    0x03F3: "Print flags", 0x03F4: "Grayscale/multichannel halftoning", 0x03F5: "Color halftoning",
    0x03F6: "Duotone halftoning", 0x03F7: "Grayscale/multichannel transfer function",
    0x03F8: "Color transfer functions", 0x03F9: "Duotone transfer functions", 0x03FA: "Duotone image information",
    0x03FB: "Effective black and white values", 0x03FD: "EPS options", 0x03FE: "Quick Mask information",
    0x0400: "Layer state information", 0x0401: "Working path", 0x0402: "Layers group information",
    0x0404: "IPTC-NAA record", 0x0405: "Image mode for raw format files", 0x0406: "JPEG quality",
    0x0408: "Grid and guides information", 0x0409: "Thumbnail resource (old)", 0x040A: "Copyright flag",
    0x040B: "URL", 0x040C: "Thumbnail resource", 0x040D: "Global angle", 0x040E: "Color samplers resource (old)",
    0x040F: "ICC profile", 0x0410: "Watermark", 0x0411: "ICC untagged profile", 0x0412: "Effects visible",
    0x0413: "Spot halftone", 0x0414: "Document-specific IDs seed number", 0x0415: "Unicode alpha names",
    0x0416: "Indexed color table count", 0x0417: "Transparency index", 0x0419: "Global altitude",
    0x041A: "Slices", 0x041B: "Workflow URL", 0x041C: "Jump to XPEP", 0x041D: "Alpha identifiers",
    0x041E: "URL list", 0x0421: "Version info", 0x0422: "Exif data 1", 0x0423: "Exif data 3",
    0x0424: "XMP metadata", 0x0425: "Caption digest", 0x0426: "Print scale", 0x0428: "Pixel aspect ratio",
    0x0429: "Layer comps", 0x042A: "Alternate duotone colors", 0x042B: "Alternate spot colors",
    0x042D: "Layer selection IDs", 0x042E: "HDR toning information", 0x042F: "Print info",
    0x0430: "Layer groups enabled ID", 0x0431: "Color samplers resource", 0x0432: "Measurement scale",
    0x0433: "Timeline information", 0x0434: "Sheet disclosure", 0x0435: "Display info (Photoshop CS3)",
    0x0436: "Onion skins", 0x0438: "Count information", 0x043A: "Print information", 0x043B: "Print style",
    0x043C: "Macintosh NSPrintInfo", 0x043D: "Windows DEVMODE", 0x043E: "Auto save file path",
    0x043F: "Auto save format", 0x0440: "Path selection state", 0x0BB7: "Name of clipping path",
    0x0BB8: "Origin path info", 0x1B58: "Image Ready variables", 0x1B59: "Image Ready data sets",
    0x1F40: "Lightroom workflow", 0x2710: "Print flags information",
}


def _irb(out: _Out, data: bytes, depth: int = 0) -> None:
    pos = 14 if data.startswith(b"Photoshop 3.0\x00") else 0
    out.add("Photoshop", "Photoshop resources", _size_text(len(data)))
    count = 0
    while pos + 12 <= len(data) and count < 500:
        if data[pos:pos + 4] not in (b"8BIM", b"8B64", b"MeSa", b"PHUT", b"AgHg", b"DCSR"):
            break
        rid = struct.unpack(">H", data[pos + 4:pos + 6])[0]
        nlen = data[pos + 6]
        nn = 1 + nlen + ((1 + nlen) & 1)
        rname = data[pos + 7:pos + 7 + nlen].decode("latin-1")
        if pos + 6 + nn + 4 > len(data):
            break
        size = struct.unpack(">I", data[pos + 6 + nn:pos + 10 + nn])[0]
        start = pos + 10 + nn
        payload = data[start:start + size]
        pos = start + size + (size & 1)
        count += 1
        label = _IRB_NAMES.get(rid, f"Resource 0x{rid:04X}")
        out.add("Photoshop", label, _size_text(size) + (f" (name '{rname}')" if rname else "") + f" id {rid}")
        if depth >= 3:
            continue
        if rid == 0x0404:
            _guard(out, "IPTC", _iptc, out, payload)
        elif rid == 0x0424:
            _guard(out, "XMP", _xmp, out, payload)
        elif rid == 0x040F:
            _guard(out, "ICC profile", _icc, out, payload)
        elif rid in (0x0422, 0x0423):
            _guard(out, "Exif", _exif_block, out, payload[:_BIG_TAG_READ], depth + 1)
        elif rid == 0x03ED and len(payload) >= 16:
            h, v = struct.unpack(">I", payload[:4])[0], struct.unpack(">I", payload[8:12])[0]
            unit = "inch" if payload[4:6] == b"\x00\x01" else "cm"
            out.add("Photoshop", "ResolutionInfo (decoded)", f"{h / 65536:.2f} x {v / 65536:.2f} pixels per {unit}")
        elif rid in (0x0409, 0x040C) and len(payload) >= 28:
            _fmt, w, h = struct.unpack(">III", payload[:12])
            out.add("Photoshop", "Thumbnail (decoded)", f"{w}x{h}")
        elif rid == 0x040B:
            out.add("Photoshop", "URL (decoded)", _text(payload))
        elif rid == 0x03F0 and payload:
            out.add("Photoshop", "Caption (decoded)", _text(payload[1:1 + payload[0]]))
        elif rid == 0x040A and payload:
            out.add("Photoshop", "Copyright flag (decoded)", "copyrighted" if payload[0] else "not copyrighted")
        elif rid == 0x0425:
            out.add("Photoshop", "Caption digest (decoded)", payload.hex())


_ICC_TEXT_TAGS = (("desc", "Description"), ("cprt", "Copyright"), ("dmnd", "Device manufacturer"),
                  ("dmdd", "Device model"), ("vued", "Viewing conditions"))


def _icc_text(blob: bytes) -> str:
    kind = blob[:4]
    if kind == b"desc" and len(blob) >= 12:
        n = struct.unpack(">I", blob[8:12])[0]
        return _text(blob[12:12 + max(n - 1, 0)])
    if kind == b"text":
        return _text(blob[8:]).strip("\x00 \r\n")
    if kind == b"mluc" and len(blob) >= 16:
        count, rec = struct.unpack(">II", blob[8:16])
        texts = []
        for i in range(min(count, 8)):
            if rec < 12 or 16 + rec * i + 12 > len(blob):
                break
            _lang, length, off = struct.unpack(">4sII", blob[16 + rec * i:16 + rec * i + 12])
            texts.append(blob[off:off + min(length, 600)].decode("utf-16-be", "replace"))
        return " | ".join(t for t in texts if t)
    return ""


def _icc(out: _Out, data: bytes) -> None:
    if len(data) < 132 or data[36:40] != b"acsp":
        out.add("ICC profile", "profile", _blob(data))
        return
    sec = "ICC profile"
    size, cmm, ver = struct.unpack(">I4s4s", data[:12])
    cls, space, pcs = data[12:16], data[16:20], data[20:24]
    y, mo, d, h, mi, s = struct.unpack(">6H", data[24:36])
    out.add(sec, "profile size", _size_text(size))
    out.add(sec, "preferred CMM", _text(cmm).strip("\x00 "))
    out.add(sec, "version", f"{ver[0]}.{ver[1] >> 4}.{ver[1] & 15}")
    out.add(sec, "class", _text(cls))
    out.add(sec, "color space", _text(space))
    out.add(sec, "connection space", _text(pcs))
    out.add(sec, "created", f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}")
    out.add(sec, "platform", _text(data[40:44]).strip("\x00 "))
    out.add(sec, "device manufacturer", _text(data[48:52]).strip("\x00 "))
    out.add(sec, "device model", _text(data[52:56]).strip("\x00 "))
    out.add(sec, "creator", _text(data[80:84]).strip("\x00 "))
    out.add(sec, "profile ID", data[84:100].hex() if any(data[84:100]) else "none")
    ntags = min(struct.unpack(">I", data[128:132])[0], 200)
    tags = {}
    for i in range(ntags):
        base = 132 + 12 * i
        if base + 12 > len(data):
            break
        sig, off, ln = struct.unpack(">4sII", data[base:base + 12])
        tags[_text(sig)] = (off, ln)
    out.add(sec, "tags", ", ".join(tags)[:290])
    for sig, label in _ICC_TEXT_TAGS:
        if sig in tags:
            off, ln = tags[sig]
            text = _icc_text(data[off:off + min(ln, 4096)])
            if text:
                out.add(sec, label, text)


# ---------------------------------------------------------------- images embedded in Office/ODF/PDF and the formats themselves

_SOF_NAMES = {0xC0: "baseline DCT", 0xC1: "extended sequential DCT", 0xC2: "progressive DCT", 0xC3: "lossless",
              0xC5: "differential sequential DCT", 0xC6: "differential progressive DCT", 0xC7: "differential lossless",
              0xC9: "arithmetic sequential DCT", 0xCA: "arithmetic progressive DCT", 0xCB: "arithmetic lossless",
              0xCD: "differential arithmetic sequential", 0xCE: "differential arithmetic progressive",
              0xCF: "differential arithmetic lossless"}
_APP_IDENTS = {b"http://ns.adobe.com/xap/1.0/": "XMP", b"http://ns.adobe.com/xmp/extension/": "XMP extension",
               b"Exif": "Exif", b"ICC_PROFILE": "ICC_PROFILE", b"MPF": "MPF", b"JFIF": "JFIF", b"JFXX": "JFXX",
               b"Photoshop 3.0": "Photoshop 3.0", b"Adobe": "Adobe", b"Ducky": "Ducky", b"FPXR": "FlashPix"}


def _jpeg(out: _Out, rd, embedded: bool = False) -> None:
    """rd is a _Mem over bytes (the shared walker needs random access to the whole stream)."""
    size = rd.size
    if rd.read(0, 2) != b"\xff\xd8":
        out.add("JPEG", "could not read", "missing JPEG start marker (FFD8)")
        return
    icc_chunks: dict = {}
    m = end = 0
    try:
        for m, pos, end in jpeg_segments(rd.data):
            plen = end - pos - 4
            if _is_sof(m):
                body = rd.read(pos + 4, 6)
                if len(body) >= 6:
                    prec, h, w, nc = body[0], struct.unpack(">H", body[1:3])[0], struct.unpack(">H", body[3:5])[0], body[5]
                    out.add("JPEG", "image", f"{w}x{h}, {prec}-bit, {nc} component(s), {_SOF_NAMES.get(m, 'SOF%d' % (m - 0xC0))}")
                continue
            if not (0xE0 <= m <= 0xEF or m == 0xFE):
                continue
            payload = rd.read(pos + 4, plen)
            if m == 0xFE:
                out.add("JPEG", "COM comment", f"{_text(payload)} ({plen} bytes)" if len(payload) > 0 else "0 bytes")
                continue
            n = m - 0xE0
            raw_ident = payload.split(b"\x00", 1)[0][:40]
            ident = next((label for key, label in _APP_IDENTS.items() if raw_ident == key or raw_ident.startswith(key)), None)
            if ident is None and raw_ident and re.fullmatch(rb"[\x20-\x7e]{2,40}", raw_ident):
                ident = raw_ident.decode("ascii")
            out.add("JPEG", f"APP{n} {ident}" if ident else f"APP{n}", _size_text(plen))
            if ident == "JFIF" and len(payload) >= 14:
                units = {0: "no units (aspect ratio)", 1: "dots per inch", 2: "dots per cm"}.get(payload[7], str(payload[7]))
                out.add("JFIF", "version", f"{payload[5]}.{payload[6]:02d}")
                out.add("JFIF", "density units", f"{payload[7]} ({units})")
                out.add("JFIF", "X density", struct.unpack(">H", payload[8:10])[0])
                out.add("JFIF", "Y density", struct.unpack(">H", payload[10:12])[0])
                out.add("JFIF", "thumbnail", f"{payload[12]}x{payload[13]}" if payload[12] or payload[13] else "none")
            elif ident == "JFXX":
                out.add("JFIF", "extension thumbnail", f"{plen} bytes, format {payload[5] if len(payload) > 5 else '?'}")
            elif ident == "Exif":
                _guard(out, "Exif", _exif_block, out, payload[6:])
            elif ident == "XMP":
                _guard(out, "XMP", _xmp, out, payload[len(b"http://ns.adobe.com/xap/1.0/") + 1:])
            elif ident == "ICC_PROFILE" and len(payload) > 14:
                icc_chunks[payload[12]] = payload[14:]
            elif ident == "MPF":
                _guard(out, "MPF", _tiff_block, out, _Mem(payload[4:]), False, 0, True)
            elif ident == "Photoshop 3.0":
                _guard(out, "Photoshop", _irb, out, payload, 0)
            elif ident == "Adobe" and len(payload) >= 12:
                transform = {0: "unknown (RGB or CMYK)", 1: "YCbCr", 2: "YCCK"}.get(payload[11], str(payload[11]))
                out.add("JPEG", "Adobe marker", f"DCT version {struct.unpack('>H', payload[5:7])[0]}, transform {payload[11]} ({transform})")
            elif ident not in ("JFIF", "JFXX", "Exif", "XMP", "XMP extension", "ICC_PROFILE", "MPF", "Photoshop 3.0", "Adobe"):
                found = _strings(payload[len(raw_ident):])
                if found:
                    out.add("JPEG", f"APP{n} strings", " | ".join(found[:12]))
    except WipeError as exc:
        out.add("JPEG", "could not read", str(exc))
        m = None  # damaged: no end-of-image verdict
    if icc_chunks:
        _guard(out, "ICC profile", _icc, out, b"".join(icc_chunks[k] for k in sorted(icc_chunks))[:2 << 20])
    if embedded or m is None:
        return
    if m != 0xD9:
        out.add("JPEG", "end of image", "no EOI marker found (file truncated?)")
    elif end < size:
        tail = rd.read(end, 4096)
        out.add("JPEG", "data after EOI", f"{size - end} bytes at offset {end}: {_hex(tail)}")
        for s in _strings(tail, 6, 8):
            out.add("JPEG", "trailing string", s)


# ---------------------------------------------------------------- PNG

_PNG_COLOR = {0: "grayscale", 2: "RGB", 3: "palette", 4: "grayscale+alpha", 6: "RGBA"}
_PNG_TEXT_CAP = 2 << 20


def _inflate(data: bytes, cap: int = _PNG_TEXT_CAP) -> bytes:
    d = zlib.decompressobj()
    return d.decompress(data, cap)


def _png_text(out: _Out, kind: bytes, payload: bytes) -> None:
    key, _, rest = payload.partition(b"\x00")
    keyword = key.decode("latin-1")[:80]
    label = f"{kind.decode('ascii')} {keyword}"
    if kind == b"tEXt":
        out.add("PNG", label, rest.decode("latin-1"))
    elif kind == b"zTXt":
        try:
            out.add("PNG", label, _inflate(rest[1:]).decode("latin-1"))
        except zlib.error as exc:
            out.add("PNG", label, f"<compressed, cannot decompress: {exc}>")
    else:  # iTXt
        if len(rest) < 2:
            out.add("PNG", label, "<truncated>")
            return
        flag, method = rest[0], rest[1]
        lang, _, rest = rest[2:].partition(b"\x00")
        translated, _, text = rest.partition(b"\x00")
        if flag:
            try:
                text = _inflate(text)
            except zlib.error as exc:
                out.add("PNG", label, f"<compressed, cannot decompress: {exc}>")
                return
        extra = (f" [lang {lang.decode('ascii', 'replace')}]" if lang else "") + (
            f" [translated keyword {translated.decode('utf-8', 'replace')}]" if translated else "")
        if keyword == "XML:com.adobe.xmp":
            out.add("PNG", label, f"{len(text)} bytes of XMP" + extra)
            _guard(out, "XMP", _xmp, out, text)
        else:
            out.add("PNG", label, text.decode("utf-8", "replace") + extra)


def _png(out: _Out, rd) -> None:
    size = rd.size
    if rd.read(0, 8) != b"\x89PNG\r\n\x1a\n":
        out.add("PNG", "could not read", "missing PNG signature")
        return
    pos, count, run = 8, 0, None
    seen_end = False

    def flush():
        nonlocal run
        if run:
            out.add("PNG", f"chunk {run[0]}", f"{run[1]} chunk(s), {run[2]} bytes in total")
            run = None

    while pos + 12 <= size and count < 20000:
        hdr = rd.read(pos, 8)
        if len(hdr) < 8:
            break
        length, ctype = struct.unpack(">I", hdr[:4])[0], hdr[4:8]
        name = ctype.decode("latin-1")
        count += 1
        if ctype in (b"IDAT", b"fdAT"):
            if run and run[0] == name:
                run[1] += 1
                run[2] += length
            else:
                flush()
                run = [name, 1, length]
            pos += 12 + length
            continue
        flush()
        is_text = ctype in (b"tEXt", b"zTXt", b"iTXt")
        cap = _PNG_TEXT_CAP if ctype in (b"tEXt", b"zTXt", b"iTXt", b"iCCP", b"eXIf", b"caBX") else 4096
        payload = rd.read(pos + 8, min(length, cap))
        out.add("PNG", f"chunk {name}", _size_text(length) + (" (truncated)" if pos + 12 + length > size else ""))
        try:
            if ctype == b"IHDR" and len(payload) >= 13:
                w, h, depth, color, _comp, _flt, inter = struct.unpack(">IIBBBBB", payload[:13])
                out.add("PNG", "image", f"{w}x{h}, {depth}-bit {_PNG_COLOR.get(color, color)}, {'Adam7 interlaced' if inter else 'not interlaced'}")
            elif is_text:
                _png_text(out, ctype, payload)
            elif ctype == b"tIME" and len(payload) >= 7:
                y, mo, d, h, mi, s = struct.unpack(">HBBBBB", payload[:7])
                out.add("PNG", "tIME", f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}")
            elif ctype == b"pHYs" and len(payload) >= 9:
                x, y, unit = struct.unpack(">IIB", payload[:9])
                dpi = f" ({x * 0.0254:.1f} x {y * 0.0254:.1f} dpi)" if unit == 1 else ""
                out.add("PNG", "pHYs", f"{x} x {y} {'pixels per metre' if unit == 1 else 'unit unknown'}{dpi}")
            elif ctype == b"gAMA" and len(payload) >= 4:
                out.add("PNG", "gAMA", _num(struct.unpack(">I", payload[:4])[0] / 100000))
            elif ctype == b"cHRM" and len(payload) >= 32:
                v = [x / 100000 for x in struct.unpack(">8I", payload[:32])]
                out.add("PNG", "cHRM", "white " + _num(v[0]) + "," + _num(v[1]) + " red " + _num(v[2]) + "," + _num(v[3]) + " green " + _num(v[4]) + "," + _num(v[5]) + " blue " + _num(v[6]) + "," + _num(v[7]))
            elif ctype == b"sRGB" and payload:
                names = {0: "perceptual", 1: "relative colorimetric", 2: "saturation", 3: "absolute colorimetric"}
                out.add("PNG", "sRGB", f"{payload[0]} ({names.get(payload[0], '?')})")
            elif ctype == b"iCCP":
                pname, _, rest = payload.partition(b"\x00")
                out.add("PNG", "iCCP profile name", pname.decode("latin-1"))
                _guard(out, "ICC profile", lambda: _icc(out, _inflate(rest[1:])))
            elif ctype == b"eXIf":
                _guard(out, "Exif", _exif_block, out, payload)
            elif ctype == b"orNT" and payload:
                out.add("PNG", "orNT", f"{payload[0]} (orientation)")
            elif ctype == b"PLTE":
                out.add("PNG", "PLTE", f"{length // 3} palette entries")
            elif ctype == b"acTL" and len(payload) >= 8:
                frames, plays = struct.unpack(">II", payload[:8])
                out.add("PNG", "acTL", f"{frames} frames, {plays or 'infinite'} plays")
            elif ctype == b"cICP" and len(payload) >= 4:
                out.add("PNG", "cICP", f"primaries {payload[0]}, transfer {payload[1]}, matrix {payload[2]}, full range {payload[3]}")
            elif ctype == b"oFFs" and len(payload) >= 9:
                x, y, unit = struct.unpack(">iiB", payload[:9])
                out.add("PNG", "oFFs", f"{x}, {y} {'micrometers' if unit else 'pixels'}")
            elif ctype == b"sBIT":
                out.add("PNG", "sBIT", " ".join(str(b) for b in payload[:4]))
            elif ctype == b"bKGD":
                out.add("PNG", "bKGD", _hex(payload, 8))
            elif ctype == b"caBX":
                out.add("PNG", "C2PA content credentials", _size_text(length))
            elif ctype[0:1].islower() and ctype not in (b"tRNS", b"hIST", b"sPLT", b"mDCV", b"cLLI") and payload:
                found = _strings(payload)
                if found:
                    out.add("PNG", f"{name} strings", " | ".join(found[:8]))
        except (struct.error, ValueError, zlib.error) as exc:
            out.add("PNG", f"{name} (decoded)", f"<unreadable: {exc}>")
        pos += 12 + length
        if ctype == b"IEND":
            seen_end = True
            break
    flush()
    if seen_end and pos < size:
        out.add("PNG", "data after IEND", f"{size - pos} bytes: {_hex(rd.read(pos, 16))}")
    elif not seen_end:
        out.add("PNG", "end of image", "no IEND chunk (file truncated?)")


# ---------------------------------------------------------------- WebP

def _webp(out: _Out, rd) -> None:
    size = rd.size
    head = rd.read(0, 12)
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WEBP":
        out.add("WebP", "could not read", "missing RIFF/WEBP header")
        return
    declared = struct.unpack("<I", head[4:8])[0] + 8
    out.add("WebP", "RIFF size", f"{declared} bytes" + ("" if declared == size else f" (file is {size} bytes)"))
    pos, count = 12, 0
    while pos + 8 <= min(size, declared + 8) and count < 5000:
        hdr = rd.read(pos, 8)
        fourcc, length = hdr[:4], struct.unpack("<I", hdr[4:8])[0]
        name = fourcc.decode("latin-1")
        count += 1
        cap = _XML_CAP if fourcc in (b"EXIF", b"XMP ", b"ICCP") else 64
        payload = rd.read(pos + 8, min(length, cap))
        out.add("WebP", f"chunk {name}", _size_text(length))
        try:
            if fourcc == b"VP8X" and len(payload) >= 10:
                flags = payload[0]
                names = [n for bit, n in ((0x20, "ICC profile"), (0x10, "alpha"), (0x08, "Exif"), (0x04, "XMP"), (0x02, "animation")) if flags & bit]
                w = 1 + int.from_bytes(payload[4:7], "little")
                h = 1 + int.from_bytes(payload[7:10], "little")
                out.add("WebP", "canvas", f"{w}x{h}, flags: {', '.join(names) or 'none'}")
            elif fourcc == b"VP8 " and len(payload) >= 10 and payload[3:6] == b"\x9d\x01\x2a":
                out.add("WebP", "image", f"{struct.unpack('<H', payload[6:8])[0] & 0x3FFF}x{struct.unpack('<H', payload[8:10])[0] & 0x3FFF}, lossy")
            elif fourcc == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
                bits = int.from_bytes(payload[1:5], "little")
                out.add("WebP", "image", f"{(bits & 0x3FFF) + 1}x{((bits >> 14) & 0x3FFF) + 1}, lossless")
            elif fourcc == b"ANIM" and len(payload) >= 6:
                out.add("WebP", "animation", f"background BGRA {_hex(payload[:4])}, loop count {struct.unpack('<H', payload[4:6])[0] or 'infinite'}")
            elif fourcc == b"EXIF":
                _guard(out, "Exif", _exif_block, out, payload[6:] if payload[:6] == b"Exif\x00\x00" else payload)
            elif fourcc == b"XMP ":
                _guard(out, "XMP", _xmp, out, payload)
            elif fourcc == b"ICCP":
                _guard(out, "ICC profile", _icc, out, payload)
        except (struct.error, ValueError) as exc:
            out.add("WebP", f"{name} (decoded)", f"<unreadable: {exc}>")
        pos += 8 + length + (length & 1)
    if declared + 8 < size and pos >= declared:
        out.add("WebP", "data after RIFF", f"{size - declared} bytes")


def _tiff_file(out: _Out, rd) -> None:
    if rd.size < 8:
        out.add("TIFF", "could not read", "file shorter than a TIFF header")
        return
    out.add("TIFF", "byte order", "little endian (II)" if rd.read(0, 2) == b"II" else "big endian (MM)" if rd.read(0, 2) == b"MM" else "unknown")
    _tiff_block(out, rd, True)



# ---------------------------------------------------------------- ISO base media (HEIC/HEIF/AVIF, also the box level of MP4/MOV)

_ISO_EPOCH = 2082844800  # seconds between 1904-01-01 and 1970-01-01
_ISO_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"udta", b"mvex", b"moof", b"traf"}
_XMP_UUID = bytes.fromhex("be7acfcb97a942e89c71999491e3afac")
_BOX_CAP = 3000
_UDTA_TEXT = {b"name", b"loci", b"manu", b"modl", b"cprt", b"titl", b"auth", b"perf", b"yrrc", b"kywd", b"albm", b"dscp"}


def _boxes_rd(rd, start: int, end: int, limit: int = _BOX_CAP):
    """Yield (type, box_start, payload_start, box_end, truncated); stops quietly at the first bad box."""
    pos, n = start, 0
    while pos + 8 <= end and n < limit:
        hdr = rd.read(pos, 16)
        if len(hdr) < 8:
            return
        size, typ = struct.unpack(">I", hdr[:4])[0], hdr[4:8]
        header = 8
        if size == 1:
            if len(hdr) < 16:
                return
            size, header = struct.unpack(">Q", hdr[8:16])[0], 16
        elif size == 0:
            size = end - pos
        if size < header:
            return
        n += 1
        if pos + size > end:
            yield typ, pos, pos + header, end, True
            return
        yield typ, pos, pos + header, pos + size, False
        pos += size


def _iso_time(v: int) -> str:
    if v == 0:
        return "0 (not set)"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(v - _ISO_EPOCH))
    except (OverflowError, OSError, ValueError):
        return f"{v} (out of range)"


def _cstring(b: bytes) -> str:
    return _text(b.split(b"\x00", 1)[0])


def _iso_decode(out: _Out, section: str, typ: bytes, payload: bytes) -> None:
    name = typ.decode("latin-1")
    if typ == b"mvhd" and len(payload) >= 20:
        ver = payload[0]
        if ver == 1 and len(payload) >= 32:
            c, m, ts, dur = struct.unpack(">QQIQ", payload[4:32])
        else:
            c, m, ts, dur = struct.unpack(">IIII", payload[4:20])
        out.add(section, "mvhd creation time", _iso_time(c))
        out.add(section, "mvhd modification time", _iso_time(m))
        out.add(section, "mvhd timescale/duration", f"{ts} / {dur}")
    elif typ == b"tkhd" and len(payload) >= 24:
        ver = payload[0]
        if ver == 1 and len(payload) >= 36:
            c, m, tid = struct.unpack(">QQI", payload[4:24])
            wh = payload[-8:]
        else:
            c, m, tid = struct.unpack(">III", payload[4:16])
            wh = payload[-8:]
        out.add(section, f"tkhd track {tid} creation time", _iso_time(c))
        out.add(section, f"tkhd track {tid} modification time", _iso_time(m))
        if len(wh) == 8 and (wh[:2] != b"\x00\x00" or wh[4:6] != b"\x00\x00"):
            out.add(section, f"tkhd track {tid} size", f"{struct.unpack('>H', wh[:2])[0]}x{struct.unpack('>H', wh[4:6])[0]}")
    elif typ == b"mdhd" and len(payload) >= 24:
        if payload[0] == 1 and len(payload) >= 36:
            c, m = struct.unpack(">QQ", payload[4:20])
            lang = struct.unpack(">H", payload[32:34])[0]
        else:
            c, m = struct.unpack(">II", payload[4:12])
            lang = struct.unpack(">H", payload[20:22])[0]
        code = "".join(chr(((lang >> s) & 31) + 0x60) for s in (10, 5, 0)) if lang else ""
        out.add(section, "mdhd creation time", _iso_time(c))
        out.add(section, "mdhd modification time", _iso_time(m))
        if code:
            out.add(section, "mdhd language", code)
    elif typ == b"hdlr" and len(payload) >= 24:
        htype = payload[8:12].decode("latin-1")
        hname = _cstring(payload[24:])
        out.add(section, "hdlr", f"{htype}" + (f" name '{hname}'" if hname else ""))
    elif typ == b"stsd" and len(payload) >= 16:
        entry = payload[12:16].decode("latin-1")
        comp = payload[58:90] if len(payload) >= 90 else b""
        info = entry
        if comp and comp[0] < 32:
            cname = comp[1:1 + comp[0]]
            if cname and re.fullmatch(rb"[\x20-\x7e]+", cname):
                info += f", compressor '{_text(cname)}'"
        out.add(section, "stsd sample entry", info)
    elif typ == b"uuid":
        out.add(section, "uuid box", f"{payload[:16].hex()} {_size_text(len(payload) - 16)}")
    else:
        out.add(section, f"box {name}", _size_text(len(payload)))


def _iso_text_box(payload: bytes) -> str:
    if len(payload) >= 4:
        n = struct.unpack(">H", payload[:2])[0]
        if 0 < n <= len(payload) - 4:
            return _text(payload[4:4 + n])
    return _text(payload).strip("\x00")


def _iso_meta_items(out: _Out, section: str, rd, start: int, end: int) -> None:
    keys: dict = {}
    for typ, bs, ps, be, trunc in _boxes_rd(rd, start, end, 500):
        if typ == b"keys":
            blob = rd.read(ps, min(be - ps, 1 << 20))
            cnt = struct.unpack(">I", blob[4:8])[0] if len(blob) >= 8 else 0
            p = 8
            for i in range(1, min(cnt, 500) + 1):
                if p + 8 > len(blob):
                    break
                ksz = struct.unpack(">I", blob[p:p + 4])[0]
                if ksz < 8:
                    break
                keys[i] = _text(blob[p + 8:p + ksz])
                p += ksz
        elif typ == b"ilst":
            for t2, b2, p2, e2, tr2 in _boxes_rd(rd, ps, be, 500):
                idx = struct.unpack(">I", t2)[0]
                label = keys.get(idx) or t2.decode("latin-1")
                for t3, b3, p3, e3, tr3 in _boxes_rd(rd, p2, e2, 20):
                    if t3 == b"data":
                        blob = rd.read(p3, min(e3 - p3, 4096))
                        flags = struct.unpack(">I", blob[:4])[0] & 0xFFFFFF if len(blob) >= 8 else 0
                        value = blob[8:]
                        out.add(section, f"ilst {label}", _text(value) if flags in (1, 0) and value else (_hex(value) if value else ""))


def _iso_moov(out: _Out, section: str, rd, start: int, end: int, depth: int, state: dict) -> None:
    if depth > 8 or state["boxes"] > 4000:
        return
    for typ, bs, ps, be, trunc in _boxes_rd(rd, start, end):
        state["boxes"] += 1
        name = typ.decode("latin-1")
        if typ in _ISO_CONTAINERS:
            _iso_moov(out, section, rd, ps, be, depth + 1, state)
        elif typ == b"meta":
            first = rd.read(ps, 12)
            body = ps if first[4:8] == b"hdlr" else ps + 4
            _iso_moov(out, section, rd, body, be, depth + 1, state)
            _iso_meta_items(out, section, rd, body, be)
        elif typ in (b"mvhd", b"tkhd", b"mdhd", b"hdlr", b"stsd", b"uuid"):
            _iso_decode(out, section, typ, rd.read(ps, min(be - ps, 4096)))
        elif depth >= 1 and (typ[:1] == b"\xa9" or typ in _UDTA_TEXT):
            out.add(section, f"udta {name}", _iso_text_box(rd.read(ps, min(be - ps, 2048))))
        elif typ in (b"free", b"skip", b"wide") and be - ps > 0:
            blob = rd.read(ps, min(be - ps, 4096))
            if any(blob):
                out.add(section, f"box {name}", f"{be - ps} bytes, not empty: " + (" | ".join(_strings(blob, 4, 6)) or _hex(blob)))


def _iso_top(out: _Out, section: str, rd, size: int, brands: bool = True) -> dict:
    """List the top-level boxes of a file; returns offsets of notable boxes."""
    found = {"moov": [], "meta": [], "mdat": []}
    for typ, bs, ps, be, trunc in _boxes_rd(rd, 0, size):
        name = typ.decode("latin-1")
        if typ == b"ftyp":
            out.add(section, "box ftyp", _size_text(be - bs))
            blob = rd.read(ps, min(be - ps, 4096))
            if len(blob) >= 8 and brands:
                names = [blob[i:i + 4].decode("latin-1") for i in range(8, len(blob) - 3, 4)][:20]
                out.add(section, "major brand", blob[:4].decode("latin-1"))
                out.add(section, "minor version", struct.unpack(">I", blob[4:8])[0])
                out.add(section, "compatible brands", ", ".join(names))
        elif typ == b"uuid":
            blob = rd.read(ps, min(be - ps, _XML_CAP))
            out.add(section, "box uuid", f"{blob[:16].hex()} {_size_text(be - ps - 16)}" + (" (truncated)" if trunc else ""))
            if blob[:16] == _XMP_UUID:
                _guard(out, "XMP", _xmp, out, blob[16:])
            else:
                strs = _strings(blob[16:], 4, 6)
                if strs:
                    out.add(section, "box uuid strings", " | ".join(strs))
        else:
            out.add(section, f"box {name}", _size_text(be - bs) + (" (truncated)" if trunc else ""))
            if typ in (b"free", b"skip", b"wide") and be - ps > 0:
                blob = rd.read(ps, min(be - ps, 4096))
                if any(blob):
                    out.add(section, f"box {name} content", " | ".join(_strings(blob, 4, 6)) or _hex(blob))
        if name in found:
            found[name].append((bs, ps, be))
    return found


def _uint_be(b: bytes) -> int:
    return int.from_bytes(b, "big") if b else 0


def _heif_item_data(rd, extents, method: int, idat_start: int, cap: int) -> bytes:
    data = b""
    for off, length in extents:
        start = off if method == 0 else idat_start + off if idat_start >= 0 else -1
        if start < 0:
            break
        data += rd.read(start, min(length, cap - len(data)))
        if len(data) >= cap:
            break
    return data


def _heif(out: _Out, path: Path, size: int) -> None:
    with open(path, "rb") as fh:
        rd = _FileRd(fh, size)
        found = _iso_top(out, "HEIF", rd, size)
        for bs, ps, be in found["meta"][:2]:
            _guard(out, "HEIF", _heif_meta, out, rd, ps, be)
        for bs, ps, be in found["moov"][:1]:
            _guard(out, "HEIF", _iso_moov, out, "HEIF", rd, ps, be, 0, {"boxes": 0})


def _heif_meta(out: _Out, rd, ps: int, be: int) -> None:
    first = rd.read(ps, 12)
    body = ps if first[4:8] == b"hdlr" else ps + 4
    items: dict = {}
    locs: dict = {}
    idat = -1
    children = {}
    for typ, bs, p2, e2, trunc in _boxes_rd(rd, body, be, 200):
        children.setdefault(typ, []).append((bs, p2, e2))
    for bs, p2, e2 in children.get(b"hdlr", [])[:1]:
        _iso_decode(out, "HEIF", b"hdlr", rd.read(p2, min(e2 - p2, 4096)))
    for bs, p2, e2 in children.get(b"pitm", [])[:1]:
        blob = rd.read(p2, 8)
        if len(blob) >= 6:
            out.add("HEIF", "primary item", _uint_be(blob[4:6]) if blob[0] == 0 else _uint_be(blob[4:8]))
    for bs, p2, e2 in children.get(b"idat", [])[:1]:
        idat = p2
        out.add("HEIF", "idat", _size_text(e2 - p2))
    for bs, p2, e2 in children.get(b"iinf", [])[:1]:
        blob = rd.read(p2, min(e2 - p2, 1 << 20))
        first_child = 6 if blob[:1] == b"\x00" else 8
        for t3, b3, p3, e3, tr3 in _boxes_rd(_Mem(blob), first_child, len(blob), 2000):
            if t3 != b"infe":
                continue
            ib = blob[p3:e3]
            ver = ib[0] if ib else 0
            try:
                if ver >= 2:
                    p = 4
                    iid = _uint_be(ib[p:p + (2 if ver == 2 else 4)])
                    p += 2 if ver == 2 else 4
                    p += 2
                    itype = ib[p:p + 4].decode("latin-1")
                    rest = ib[p + 4:]
                    parts = rest.split(b"\x00")
                    name = _text(parts[0])
                    ctype = _text(parts[1]) if itype == "mime" and len(parts) > 1 else ""
                else:
                    iid = _uint_be(ib[4:6])
                    parts = ib[8:].split(b"\x00")
                    itype, name, ctype = "", _text(parts[0]), _text(parts[1]) if len(parts) > 1 else ""
            except (IndexError, ValueError):
                continue
            items[iid] = (itype, name, ctype)
    for bs, p2, e2 in children.get(b"iloc", [])[:1]:
        blob = rd.read(p2, min(e2 - p2, 4 << 20))
        try:
            ver = blob[0]
            osz, lsz, bsz, isz = blob[4] >> 4, blob[4] & 15, blob[5] >> 4, (blob[5] & 15 if ver in (1, 2) else 0)
            p, count = 6, 0
            if ver < 2:
                count, p = _uint_be(blob[6:8]), 8
            else:
                count, p = _uint_be(blob[6:10]), 10
            for _ in range(min(count, 5000)):
                iid = _uint_be(blob[p:p + (2 if ver < 2 else 4)])
                p += 2 if ver < 2 else 4
                method = 0
                if ver in (1, 2):
                    method = _uint_be(blob[p:p + 2]) & 15
                    p += 2
                p += 2
                base = _uint_be(blob[p:p + bsz])
                p += bsz
                ecount = _uint_be(blob[p:p + 2])
                p += 2
                ext = []
                for _e in range(min(ecount, 64)):
                    p += isz
                    off = _uint_be(blob[p:p + osz])
                    p += osz
                    ln = _uint_be(blob[p:p + lsz])
                    p += lsz
                    ext.append((base + off, ln))
                locs[iid] = (method, ext)
        except IndexError:
            pass
    for iid, (itype, name, ctype) in list(items.items())[:500]:
        method, ext = locs.get(iid, (0, []))
        total = sum(l for _o, l in ext)
        desc = f"{itype or 'item'}" + (f" '{name}'" if name else "") + (f" {ctype}" if ctype else "") + f", {total} bytes"
        out.add("HEIF", f"item {iid}", desc)
        data_needed = itype in ("Exif", "mime")  # exiv2 reads any mime item as XMP, whatever its content type
        if data_needed and ext:
            data = _heif_item_data(rd, ext, method, idat, _XML_CAP)
            if itype == "Exif" and len(data) > 4:
                skip = 4 + _uint_be(data[:4])
                tiff = data[skip:]
                if tiff[:2] not in (b"II", b"MM"):
                    k = data.find(b"Exif\x00\x00")
                    tiff = data[k + 6:] if k >= 0 else tiff
                _guard(out, "Exif", _exif_block, out, tiff)
            elif itype == "mime":
                _guard(out, "XMP", _xmp, out, data)
    for bs, p2, e2 in children.get(b"iref", [])[:1]:
        blob = rd.read(p2, min(e2 - p2, 1 << 20))
        refs = [t[0].decode("latin-1") for t in _boxes_rd(_Mem(blob), 4, len(blob), 500)]
        out.add("HEIF", "item references", f"{len(refs)}: " + ", ".join(sorted(set(refs))))
    for bs, p2, e2 in children.get(b"iprp", [])[:1]:
        for t3, b3, p3, e3, tr3 in _boxes_rd(rd, p2, e2, 20):
            if t3 != b"ipco":
                continue
            for k, (t4, b4, p4, e4, tr4) in enumerate(_boxes_rd(rd, p3, e3, 500), 1):
                blob = rd.read(p4, min(e4 - p4, 1 << 16))
                label = t4.decode("latin-1")
                if t4 == b"ispe" and len(blob) >= 12:
                    desc = f"{_uint_be(blob[4:8])}x{_uint_be(blob[8:12])}"
                elif t4 == b"colr" and len(blob) >= 4:
                    kind = blob[:4].decode("latin-1")
                    desc = kind
                    if kind == "nclx" and len(blob) >= 11:
                        desc += f" primaries {_uint_be(blob[4:6])} transfer {_uint_be(blob[6:8])} matrix {_uint_be(blob[8:10])}"
                    elif kind in ("rICC", "prof"):
                        desc += f" ICC profile, {len(blob) - 4} bytes"
                        _guard(out, "ICC profile", _icc, out, blob[4:])
                elif t4 == b"irot" and blob:
                    desc = f"{(blob[0] & 3) * 90} degrees"
                elif t4 == b"pixi" and len(blob) >= 5:
                    desc = f"{blob[4]} channels, bits " + " ".join(str(b) for b in blob[5:5 + blob[4]])
                elif t4 == b"auxC":
                    desc = _cstring(blob[4:])
                else:
                    desc = _size_text(e4 - p4)
                out.add("HEIF", f"property {k} {label}", desc)
    for typ in (b"xml ", b"bxml"):
        for bs, p2, e2 in children.get(typ, [])[:2]:
            blob = rd.read(p2 + 4, min(e2 - p2 - 4, _XML_CAP))
            out.add("HEIF", "meta xml box", _size_text(len(blob)))
            _guard(out, "XMP", _xmp, out, blob)
    for typ in (b"grpl", b"dinf", b"free", b"uuid"):
        for bs, p2, e2 in children.get(typ, [])[:3]:
            out.add("HEIF", f"meta box {typ.decode('latin-1')}", _size_text(e2 - bs))



# ---------------------------------------------------------------- embedded pictures (Office/ODF/PDF)

def _picture(out: _Out, label: str, data: bytes, ext: str, limit: int = 80) -> None:
    """Rows of an embedded picture, all under one section `label`."""
    sub = _Out()
    sub.remap = label
    mem = _Mem(data)
    try:
        if ext in _JPEG_EXT:
            _jpeg(sub, mem, True)
        elif ext in _PNG_EXT:
            _png(sub, mem)
        elif ext == ".webp":
            _webp(sub, mem)
        elif ext in _TIFF_EXT:
            _tiff_block(sub, mem, True)
    except _Stop:
        pass
    except Exception as exc:  # noqa: BLE001
        sub.rows.append((label, "could not read", _clip(f"{type(exc).__name__}: {exc}")))
    for row in sub.rows[:limit]:
        out.add(*row)
    if len(sub.rows) > limit:
        out.add(label, "note", f"{len(sub.rows) - limit} more rows not shown")


# ---------------------------------------------------------------- PDF

_PDF_MAX_FILE = 256 << 20
_PDF_MAX_PAGES = 300
_PDF_TIME = 25.0


def _pv(x, depth: int = 0) -> str:
    """Readable value of a pypdf object."""
    try:
        if hasattr(x, "get_object"):
            x = x.get_object()
        if isinstance(x, (bytes, bytearray)):
            return _hex(bytes(x), 32)
        if isinstance(x, (list, tuple)):
            return "[" + ", ".join(_pv(i, depth + 1) for i in list(x)[:20]) + "]" if depth < 3 else "[...]"
        if hasattr(x, "items") and hasattr(x, "keys"):
            if depth >= 3:
                return "{...}"
            return "{" + ", ".join(f"{k}: {_pv(v, depth + 1)}" for k, v in list(x.items())[:20]) + "}"
        return str(x)
    except Exception as exc:  # noqa: BLE001
        return f"<unreadable: {type(exc).__name__}>"


def _pdf_get(obj, key):
    try:
        v = obj.get(key)
        return v.get_object() if hasattr(v, "get_object") else v
    except Exception:  # noqa: BLE001
        return None


def _pdf_stream_bytes(obj, cap: int = _XML_CAP) -> bytes:
    data = obj.get_data()
    return bytes(data[:cap])


def _pdf_filespec(out: _Out, label: str, spec) -> None:
    spec = spec.get_object() if hasattr(spec, "get_object") else spec
    if not hasattr(spec, "get"):
        out.add("PDF", label, _pv(spec))
        return
    parts = []
    for key, title in (("/UF", "name"), ("/F", "file"), ("/Desc", "description"), ("/Subtype", "type")):
        v = _pdf_get(spec, key)
        if v is not None and str(v):
            parts.append(f"{title}: {v}")
    ef = _pdf_get(spec, "/EF")
    stream = _pdf_get(ef, "/F") if ef is not None and hasattr(ef, "get") else None
    if stream is not None and hasattr(stream, "get"):
        params = _pdf_get(stream, "/Params")
        sub = _pdf_get(stream, "/Subtype")
        if sub:
            parts.append(f"mime: {sub}")
        if params is not None and hasattr(params, "get"):
            for key, title in (("/Size", "size"), ("/CreationDate", "created"), ("/ModDate", "modified"), ("/CheckSum", "checksum")):
                v = _pdf_get(params, key)
                if v is not None:
                    parts.append(f"{title}: {_pv(v)}")
    out.add("PDF", label, "; ".join(parts) or "attachment")


def _name_tree(node, limit: int = 500):
    """(name, value) pairs of a PDF name tree, bounded and cycle safe."""
    stack, seen, n = [node], set(), 0
    while stack and n < limit:
        cur = stack.pop()
        cur = cur.get_object() if hasattr(cur, "get_object") else cur
        if id(cur) in seen or not hasattr(cur, "get"):
            continue
        seen.add(id(cur))
        names = _pdf_get(cur, "/Names")
        if isinstance(names, list):
            for i in range(0, len(names) - 1, 2):
                yield str(names[i].get_object() if hasattr(names[i], "get_object") else names[i]), names[i + 1]
                n += 1
        kids = _pdf_get(cur, "/Kids")
        if isinstance(kids, list):
            stack.extend(reversed(kids[:200]))


def _pdf_signature(out: _Out, name: str, sig) -> None:
    sig = sig.get_object() if hasattr(sig, "get_object") else sig
    if not hasattr(sig, "get"):
        return
    parts = []
    for key, title in (("/Name", "signer"), ("/M", "date"), ("/Reason", "reason"), ("/Location", "location"),
                       ("/ContactInfo", "contact"), ("/Filter", "filter"), ("/SubFilter", "subfilter")):
        v = _pdf_get(sig, key)
        if v is not None and str(v):
            parts.append(f"{title}: {_pv(v)}")
    contents = _pdf_get(sig, "/Contents")
    if isinstance(contents, (bytes, bytearray)):
        parts.append(f"signature data: {len(contents)} bytes")
    if _pdf_get(sig, "/Cert") is not None:
        parts.append("certificate present")
    out.add("PDF signature", name or "signature", "; ".join(parts) or "present")


def _pdf_fields(out: _Out, root, deadline: float) -> None:
    form = _pdf_get(root, "/AcroForm")
    if form is None or not hasattr(form, "get"):
        return
    out.add("PDF form", "AcroForm", "present" + (" (XFA)" if _pdf_get(form, "/XFA") is not None else "")
            + (f", SigFlags {_pv(_pdf_get(form, '/SigFlags'))}" if _pdf_get(form, "/SigFlags") is not None else ""))
    fields = _pdf_get(form, "/Fields")
    stack = [(f, "") for f in reversed(fields[:500])] if isinstance(fields, list) else []
    seen, count = set(), 0
    while stack and count < 1500 and time.monotonic() < deadline:
        ref, prefix = stack.pop()
        node = ref.get_object() if hasattr(ref, "get_object") else ref
        if not hasattr(node, "get") or id(node) in seen:
            continue
        seen.add(id(node))
        t = _pdf_get(node, "/T")
        name = prefix + ("." if prefix and t is not None else "") + (str(t) if t is not None else "")
        kids = _pdf_get(node, "/Kids")
        ft = _pdf_get(node, "/FT")
        if isinstance(kids, list) and kids and not _pdf_get(node, "/V") and ft is None:
            stack.extend((k, name) for k in reversed(kids[:300]))
            continue
        if isinstance(kids, list):
            stack.extend((k, name) for k in reversed(kids[:300]) if hasattr(k, "get_object") and _pdf_get(k, "/T") is not None)
        count += 1
        value = _pdf_get(node, "/V")
        if ft == "/Sig":
            _pdf_signature(out, name, value if value is not None else {})
        else:
            out.add("PDF form", name or "(unnamed field)", f"{str(ft or '')[1:]}: {_pv(value)}" if value is not None else f"{str(ft or '')[1:]} (empty)")


def _pdf_xobjects(out: _Out, resources, state: dict, depth: int, where: str) -> None:
    if depth > 5 or not hasattr(resources, "get"):
        return
    xo = _pdf_get(resources, "/XObject")
    if xo is None or not hasattr(xo, "items"):
        return
    for name, ref in list(xo.items())[:200]:
        if state["images"] > 300 or time.monotonic() > state["deadline"]:
            return
        key = getattr(ref, "idnum", None)
        obj = ref.get_object() if hasattr(ref, "get_object") else ref
        key = ("i", key) if key is not None else ("o", id(obj))
        if key in state["seen"] or not hasattr(obj, "get"):
            continue
        state["seen"].add(key)
        sub = _pdf_get(obj, "/Subtype")
        label = f"{where}{str(name)[1:]}"
        for k in list(obj.keys())[:60]:
            if str(k).startswith("/PTEX"):
                out.add("PDF", f"{label} {str(k)[1:]}", _pv(obj.get(k)))
        if _pdf_get(obj, "/PieceInfo") is not None:
            out.add("PDF", f"{label} PieceInfo", ", ".join(str(k)[1:] for k in _pdf_get(obj, "/PieceInfo").keys()))
        meta = _pdf_get(obj, "/Metadata")
        if sub == "/Image":
            state["images"] += 1
            filt = _pdf_get(obj, "/Filter")
            filters = [str(f.get_object() if hasattr(f, "get_object") else f) for f in (filt if isinstance(filt, list) else [filt] if filt else [])]
            out.add("PDF", f"image {label}", f"{_pv(_pdf_get(obj, '/Width'))}x{_pv(_pdf_get(obj, '/Height'))}"
                    + (f", filter {' '.join(filters)}" if filters else "") + (", has /Metadata" if meta is not None else ""))
            if meta is not None and hasattr(meta, "get_data"):
                _guard(out, "XMP", lambda: _xmp(out, _pdf_stream_bytes(meta), f"PDF image {label}"))
            if "/DCTDecode" in filters and hasattr(obj, "_data"):
                _guard(out, f"PDF image {label}", _pdf_jpeg, out, f"PDF image {label}", obj)
        elif sub == "/Form":
            if meta is not None and hasattr(meta, "get_data"):
                _guard(out, "XMP", lambda: _xmp(out, _pdf_stream_bytes(meta), f"PDF form {label}"))
            _pdf_xobjects(out, _pdf_get(obj, "/Resources"), state, depth + 1, label + "/")


def _pdf_jpeg(out: _Out, label: str, obj) -> None:
    """Metadata of a JPEG image XObject, also behind lossless wrapper filters (ASCII85, Flate, ...)."""
    from .pdf import _WRAPPERS, _filter_chain, _unwrap

    _raw, names, parms = _filter_chain(obj)
    if names[-1:] != ["/DCTDecode"] or not set(names[:-1]) <= _WRAPPERS:
        return
    data = bytes(obj._data[:16 << 20])
    if len(names) > 1:
        data = _unwrap(data, names[:-1], parms[:-1])
    _picture(out, label, data[:16 << 20], ".jpg")


def _pdf_annots(out: _Out, page, pno: int) -> None:
    annots = _pdf_get(page, "/Annots")
    if not isinstance(annots, list):
        return
    for ref in annots[:500]:
        a = ref.get_object() if hasattr(ref, "get_object") else ref
        if not hasattr(a, "get"):
            continue
        subtype = str(_pdf_get(a, "/Subtype") or "")[1:]
        parts = []
        for key, title in (("/T", "author"), ("/M", "modified"), ("/CreationDate", "created"), ("/Subj", "subject"),
                           ("/NM", "id"), ("/Contents", "contents")):
            v = _pdf_get(a, key)
            if v is not None and str(v):
                parts.append(f"{title}: {_pv(v)}")
        action = _pdf_get(a, "/A")
        if action is not None and hasattr(action, "get") and _pdf_get(action, "/URI") is not None:
            parts.append(f"URI: {_pdf_get(action, '/URI')}")
        if subtype == "FileAttachment" and _pdf_get(a, "/FS") is not None:
            _pdf_filespec(out, f"page {pno} attachment", a.get("/FS"))
        if parts:
            out.add("PDF annotations", f"page {pno} {subtype}", "; ".join(parts))
        elif subtype not in ("Link", "Widget"):
            out.add("PDF annotations", f"page {pno} {subtype}", "no author/date/contents")


def _pdf(out: _Out, path: Path) -> None:
    import logging
    import warnings

    size = os.stat(path).st_size
    if size > _PDF_MAX_FILE:
        out.add("PDF", "note", f"file is {size} bytes, too large to inspect")
        return
    logger = logging.getLogger("pypdf")
    old_level = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            _pdf_inner(out, path)
    finally:
        logger.setLevel(old_level)


def _pdf_inner(out: _Out, path: Path) -> None:
    from pypdf import PdfReader

    deadline = time.monotonic() + _PDF_TIME
    reader = PdfReader(str(path), strict=False)
    out.add("PDF", "version", (reader.pdf_header or "").lstrip("%"))
    trailer = reader.trailer
    if reader.is_encrypted:
        enc = _pdf_get(trailer, "/Encrypt")
        if enc is not None and hasattr(enc, "get"):
            out.add("PDF", "encryption", "; ".join(f"{k[1:]}: {_pv(enc.get(k))}" for k in ("/Filter", "/V", "/R", "/Length", "/P")
                                                    if enc.get(k) is not None))
        try:
            ok = bool(reader.decrypt(""))
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            out.add("PDF", "encrypted", "yes - a password is needed, contents not readable")
            _pdf_id(out, trailer)
            return
        out.add("PDF", "encrypted", "yes (opens with an empty password)")
    else:
        out.add("PDF", "encrypted", "no")
    _pdf_id(out, trailer)
    try:
        out.add("PDF", "pages", len(reader.pages))
    except Exception as exc:  # noqa: BLE001
        out.fail("PDF", exc)
    info = _pdf_get(trailer, "/Info")
    if info is not None and hasattr(info, "items"):
        for k, v in list(info.items())[:200]:
            out.add("PDF", str(k).lstrip("/"), _pv(v))
    else:
        out.add("PDF", "Info dictionary", "none")
    root = _pdf_get(trailer, "/Root")
    if root is None or not hasattr(root, "get"):
        out.add("PDF", "catalog", "missing")
        return
    out.add("PDF", "catalog entries", ", ".join(str(k).lstrip("/") for k in list(root.keys())[:60]))
    for key in ("/Version", "/Lang", "/PageLayout", "/PageMode", "/URI"):
        v = _pdf_get(root, key)
        if v is not None:
            out.add("PDF", key[1:], _pv(v))
    meta = _pdf_get(root, "/Metadata")
    if meta is not None and hasattr(meta, "get_data"):
        _guard(out, "XMP", lambda: _xmp(out, _pdf_stream_bytes(meta)))
    piece = _pdf_get(root, "/PieceInfo")
    if piece is not None and hasattr(piece, "items"):
        for app, data in list(piece.items())[:30]:
            lm = _pdf_get(data, "/LastModified") if hasattr(data.get_object() if hasattr(data, "get_object") else data, "get") else None
            out.add("PDF", f"PieceInfo {str(app)[1:]}", f"LastModified {lm}" if lm else "present")
    names = _pdf_get(root, "/Names")
    if names is not None and hasattr(names, "get"):
        out.add("PDF", "name trees", ", ".join(str(k)[1:] for k in names.keys()))
        emb = _pdf_get(names, "/EmbeddedFiles")
        if emb is not None:
            for name, spec in _name_tree(emb):
                _guard(out, "PDF", _pdf_filespec, out, f"embedded file {name}", spec)
        js = _pdf_get(names, "/JavaScript")
        if js is not None:
            out.add("PDF", "JavaScript", "document-level scripts present")
    _guard(out, "PDF form", _pdf_fields, out, root, deadline)
    state = {"seen": set(), "images": 0, "deadline": deadline}
    try:
        npages = min(len(reader.pages), _PDF_MAX_PAGES)
    except Exception:  # noqa: BLE001
        npages = 0
    for i in range(npages):
        if time.monotonic() > deadline:
            out.add("PDF", "note", "stopped after the time limit; later pages are not listed")
            break
        try:
            page = reader.pages[i]
        except Exception as exc:  # noqa: BLE001
            out.fail("PDF", exc)
            break
        _guard(out, "PDF annotations", _pdf_annots, out, page, i + 1)
        for key, title in (("/LastModified", "LastModified"), ("/Thumb", "thumbnail"), ("/PieceInfo", "PieceInfo"), ("/Metadata", "XMP stream")):
            v = _pdf_get(page, key)
            if v is not None:
                out.add("PDF", f"page {i + 1} {title}", _pv(v) if key == "/LastModified" else "present")
        for key in list(page.keys())[:60]:
            if str(key).startswith("/PTEX"):
                out.add("PDF", f"page {i + 1} {str(key)[1:]}", _pv(page.get(key)))
        _guard(out, "PDF", _pdf_xobjects, out, _pdf_get(page, "/Resources"), state, 0, f"p{i + 1} ")
    if len(reader.pages) > npages:
        out.add("PDF", "note", f"{len(reader.pages) - npages} more pages are not scanned for annotations/images")


def _pdf_id(out: _Out, trailer) -> None:
    pid = _pdf_get(trailer, "/ID")
    if isinstance(pid, list):
        parts = []
        for p in pid[:2]:
            p = p.get_object() if hasattr(p, "get_object") else p
            raw = getattr(p, "original_bytes", None)
            if raw is None:
                raw = bytes(p) if isinstance(p, (bytes, bytearray)) else str(p).encode("latin-1", "replace")
            parts.append(raw.hex())
        out.add("PDF", "file ID", " / ".join(parts))



# ---------------------------------------------------------------- Office Open XML and OpenDocument packages

_ZIP_MAX_ENTRIES = 20000
_IMAGE_EXT = _JPEG_EXT | _PNG_EXT | _TIFF_EXT | {".webp"}
_AUTH_ATTRS = {"author", "username", "initials", "date", "datetime", "dt", "displayname", "userid", "providerid",
               "authorid", "ua", "created", "personid", "lastmodifiedby"}
_PEOPLE_PARTS = re.compile(r"(people|commentauthors|authors|person|usernames)[^/]*\.xml$", re.I)
_COMMENT_ELEMENTS = {"comment", "cm", "threadedcomment"}
_ODF_FIELDS = {"author-name", "author-initials", "initial-creator", "sender-firstname", "sender-lastname",
               "sender-initials", "sender-company", "sender-email", "sender-phone-private", "sender-phone-work",
               "sender-fax", "sender-street", "sender-city", "sender-postal-code", "sender-country",
               "sender-position", "sender-state-or-province", "sender-title", "printed-by"}


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else "?"


def _flat_text(el, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()[:limit]


def _zread(z: zipfile.ZipFile, info: zipfile.ZipInfo, cap: int = _ZIP_PART_CAP) -> bytes:
    done = getattr(z, "mw_read", 0)  # decompressed so far, kept on this dump's ZipFile
    if done >= _ZIP_TOTAL:
        raise _ZipCut
    with z.open(info) as fh:
        data = fh.read(min(cap, _ZIP_TOTAL - done))
    z.mw_read = done + len(data)
    return data


def _zxml(out: _Out, z, info, section: str = "Document"):
    """(root, prefixes) of an XML part; adds a row and returns None when it cannot be parsed."""
    if info.file_size > _ZIP_PART_CAP:
        out.add(section, info.filename, f"{info.file_size} bytes, too large to read")
        return None
    root, extra = _xml_root(_zread(z, info))
    if root is None:
        out.add(section, info.filename, f"{info.file_size} bytes (not parsed: {extra})")
        return None
    return root, extra


def _attrs_text(el, prefixes: dict, only=None) -> str:
    parts = []
    for k, v in el.attrib.items():
        if only is not None and _local(k).lower() not in only:
            continue
        parts.append(f"{_qname(k, prefixes)}={v}")
    return "; ".join(parts)


def _props_rows(out: _Out, root, prefixes: dict, qualified: bool) -> None:
    for el in list(root)[:500]:
        name = _qname(el.tag, prefixes) if qualified else _local(el.tag)
        text = ", ".join(t.strip() for t in el.itertext() if t.strip())
        attrs = _attrs_text(el, prefixes)
        out.add("Document", name, text + (f" ({attrs})" if attrs else ""))


def _custom_rows(out: _Out, root, prefixes: dict) -> None:
    for el in list(root)[:1000]:
        name = el.attrib.get("name", _local(el.tag))
        out.add("Document", f"custom {name}", ", ".join(t.strip() for t in el.itertext() if t.strip()))


def _authorship(out: _Out, part: str, root, prefixes: dict, state: dict) -> None:
    base = posixpath.basename(part)
    people = bool(_PEOPLE_PARTS.search(part))
    allowed = _AUTH_ATTRS | ({"name", "id"} if people else set())
    seen: dict = {}
    for el in root.iter():
        if state["rows"] >= 400:
            out.add("Document comments", "note", "more authorship records are not listed")
            return
        tag = _local(el.tag)
        attrs = _attrs_text(el, prefixes, allowed)
        if not attrs:
            continue
        text = _flat_text(el, 150) if tag.lower() in _COMMENT_ELEMENTS else ""
        if tag.lower() in ("ins", "del", "moveto", "movefrom"):
            text = _flat_text(el, 80)
        value = attrs + (f"; text: {text}" if text else "")
        seen[(tag, value)] = seen.get((tag, value), 0) + 1
        if seen[(tag, value)] == 1:
            state["rows"] += 1
            out.add("Document comments", f"{tag} [{base}]", value)


def _rels_external(out: _Out, z, infos: list) -> None:
    count = 0
    for info in infos:
        if not info.filename.endswith(".rels") or count >= 200:
            continue
        try:
            data = _zread(z, info, 2 << 20)
        except Exception:  # noqa: BLE001
            continue
        root, _p = _xml_root(data)
        if root is None:
            continue
        for rel in root:
            if rel.attrib.get("TargetMode") == "External":
                count += 1
                kind = rel.attrib.get("Type", "").rsplit("/", 1)[-1]
                out.add("Document", f"external reference [{posixpath.basename(info.filename)}]", f"{kind}: {rel.attrib.get('Target', '')}")
                if count >= 200:
                    break


def _ooxml_extras(out: _Out, z, infos: list, by_name: dict, state: dict) -> None:
    wb = by_name.get("xl/workbook.xml")
    if wb is not None:
        parsed = _zxml(out, z, wb)
        if parsed:
            root, prefixes = parsed
            names = []
            for el in root.iter():
                t = _local(el.tag)
                if t == "sheet":
                    names.append(el.attrib.get("name", "") + (" (hidden)" if el.attrib.get("state") in ("hidden", "veryHidden") else ""))
                elif t in ("fileVersion", "workbookPr", "absPath", "fileSharing", "workbookProtection"):
                    out.add("Document", f"workbook {t}", _attrs_text(el, prefixes) or _flat_text(el))
                elif t == "definedName" and len(names) < 5000 and state.setdefault("defined", 0) < 60:
                    state["defined"] += 1
                    out.add("Document", f"defined name {el.attrib.get('name', '')}", _flat_text(el, 120))
            if names:
                out.add("Document", "sheets", ", ".join(names))
    settings = by_name.get("word/settings.xml")
    if settings is not None:
        parsed = _zxml(out, z, settings)
        if parsed:
            root, prefixes = parsed
            for el in root.iter():
                t = _local(el.tag)
                if t in ("attachedTemplate", "docVar", "mailMerge", "dataSource", "documentProtection"):
                    out.add("Document", f"word settings {t}", _attrs_text(el, prefixes) or _flat_text(el))
    label = by_name.get("docMetadata/LabelInfo.xml")
    if label is not None:
        parsed = _zxml(out, z, label)
        if parsed:
            for el in parsed[0].iter():
                if _local(el.tag) == "label":
                    out.add("Document", "sensitivity label", _attrs_text(el, parsed[1]))
    for info in infos:
        n = info.filename
        low = n.lower()
        if low.startswith("docprops/thumbnail"):
            out.add("Document", "thumbnail", f"{n}, {info.file_size} bytes")
        elif "/printersettings/" in low or low.startswith("printersettings/"):
            out.add("Document", "printer settings", f"{n}, {info.file_size} bytes")
        elif low.startswith("customxml/") and not low.endswith(".rels") and not info.is_dir():
            line = f"{info.file_size} bytes"
            if info.file_size <= 65536:
                root, _p = _xml_root(_zread(z, info, 65536))
                if root is not None:
                    line += f", root <{_local(root.tag)}>" + (f": {_flat_text(root, 120)}" if _flat_text(root, 120) else "")
            out.add("Document", f"customXml {n}", line)
        elif "/embeddings/" in low or low.startswith("embeddings/"):
            out.add("Document", "embedded object", f"{n}, {info.file_size} bytes")
        elif low.endswith("vbaproject.bin"):
            out.add("Document", "macro project", f"{n}, {info.file_size} bytes")
        elif low.startswith("_xmlsignatures/") and low.endswith(".xml"):
            out.add("Document", "digital signature", f"{n}, {info.file_size} bytes")
        elif low.startswith("ppt/tags/") and not info.is_dir():
            out.add("Document", "presentation tag", f"{n}, {info.file_size} bytes")
        elif low in ("word/people.xml", "word/commentsids.xml", "word/commentsextended.xml") or low.startswith("xl/persons/"):
            out.add("Document", "part", f"{n}, {info.file_size} bytes")


def _package_pictures(out: _Out, z, infos: list) -> None:
    n = 0
    for info in infos:
        ext = posixpath.splitext(info.filename)[1].lower()
        if ext not in _IMAGE_EXT or info.is_dir():
            continue
        n += 1
        if n > 60:
            out.add("Document", "note", "more pictures are not examined")
            break
        label = f"Embedded picture {posixpath.basename(info.filename)}"
        if info.file_size > 32 << 20:
            out.add(label, "note", f"{info.file_size} bytes, too large to examine")
            continue
        _guard(out, label, lambda: _picture(out, label, _zread(z, info, 32 << 20), ext))


def _package(out: _Out, path: Path, ext: str) -> None:
    with zipfile.ZipFile(path) as z:
        every = z.infolist()
        infos = every[:_ZIP_MAX_ENTRIES]
        by_name = {i.filename: i for i in infos}
        out.add("Document", "package parts", f"{len(every)}")
        if z.comment:
            out.add("Document", "zip comment", _text(z.comment))
        stamps = sorted(i.date_time for i in infos if i.date_time)
        if stamps:
            fmt = lambda t: "%04d-%02d-%02d %02d:%02d:%02d" % t  # noqa: E731
            out.add("Document", "zip entry timestamps", f"{fmt(stamps[0])} .. {fmt(stamps[-1])}")
        try:
            if ext in office.ODF_EXT:
                _guard(out, "Document", _odf, out, z, infos, by_name)
            else:
                _guard(out, "Document", _ooxml, out, z, infos, by_name)
            _guard(out, "Document", _rels_external, out, z, infos)
            _package_pictures(out, z, infos)
        except _ZipCut:
            out.add("Document", "note", f"more than {_ZIP_TOTAL >> 20} MB decompressed, the rest of the package is not listed")


def _ooxml(out: _Out, z, infos: list, by_name: dict) -> None:
    for part, mode in (("docProps/core.xml", "core"), ("docProps/app.xml", "app"), ("docProps/custom.xml", "custom")):
        info = by_name.get(part)
        if info is None:
            continue
        parsed = _zxml(out, z, info)
        if parsed is None:
            continue
        root, prefixes = parsed
        if mode == "custom":
            _custom_rows(out, root, prefixes)
        else:
            _props_rows(out, root, prefixes, mode == "core")
    state = {"rows": 0}
    for info in infos:
        n = info.filename
        if not n.endswith(".xml") or info.is_dir():
            continue
        low = n.lower()
        if re.match(r"^(word/(comments[^/]*|document|footnotes|endnotes|header\d*|footer\d*|people)|xl/(comments[^/]*|threadedcomments/[^/]+|persons/[^/]+|revisions/[^/]+)|ppt/(commentauthors|authors|comments/[^/]+))\.xml$", low):
            parsed = _zxml(out, z, info, "Document comments")
            if parsed:
                _guard(out, "Document comments", _authorship, out, n, parsed[0], parsed[1], state)
    _ooxml_extras(out, z, infos, by_name, state)


def _odf(out: _Out, z, infos: list, by_name: dict) -> None:
    if "mimetype" in by_name:
        out.add("Document", "mimetype", _text(_zread(z, by_name["mimetype"], 256)).strip())
    meta = by_name.get("meta.xml")
    if meta is not None:
        parsed = _zxml(out, z, meta)
        if parsed:
            root, prefixes = parsed
            holder = next((e for e in root.iter() if _local(e.tag) == "meta"), root)
            for el in list(holder)[:500]:
                name = _qname(el.tag, prefixes)
                text = _flat_text(el, 280)
                if _local(el.tag) == "user-defined":
                    label = next((v for k, v in el.attrib.items() if _local(k) == "name"), "")
                    out.add("Document", f"{name} {label}", text)
                elif _local(el.tag) == "document-statistic" or (not text and el.attrib):
                    for k, v in el.attrib.items():
                        out.add("Document", f"{name} {_qname(k, prefixes)}", v)
                else:
                    attrs = _attrs_text(el, prefixes)
                    out.add("Document", name, text + (f" ({attrs})" if attrs else ""))
    manifest = by_name.get("META-INF/manifest.xml")
    if manifest is not None and b"encryption-data" in _zread(z, manifest, 1 << 20):
        out.add("Document", "encrypted", "yes (manifest lists encryption data)")
    for info in infos:
        n = info.filename
        if n.startswith("Thumbnails/") and not info.is_dir():
            out.add("Document", "thumbnail", f"{n}, {info.file_size} bytes")
        elif n.startswith("Configurations2/") and not info.is_dir():
            out.add("Document", "configuration part", f"{n}, {info.file_size} bytes")
        elif n.startswith("Object ") and n.endswith("/") is False and n.count("/") == 1 and not n.endswith(".xml"):
            out.add("Document", "embedded object", f"{n}, {info.file_size} bytes")
    settings = by_name.get("settings.xml")
    if settings is not None:
        parsed = _zxml(out, z, settings)
        if parsed:
            for el in parsed[0].iter():
                nm = next((v for k, v in el.attrib.items() if _local(k) == "name"), "")
                if _local(el.tag) == "config-item" and "printer" in nm.lower():
                    out.add("Document", f"settings {nm}", _flat_text(el, 280) if "Setup" not in nm else f"{len(_flat_text(el, 1 << 20))} characters")
    content = by_name.get("content.xml")
    if content is not None:
        parsed = _zxml(out, z, content, "Document comments")
        if parsed:
            root, prefixes = parsed
            rows = 0
            for el in root.iter():
                t = _local(el.tag)
                if rows >= 300:
                    out.add("Document comments", "note", "more entries are not listed")
                    break
                if t == "annotation":
                    creator = next((_flat_text(c) for c in el if _local(c.tag) == "creator"), "")
                    date = next((_flat_text(c) for c in el if _local(c.tag) == "date"), "")
                    initials = next((_flat_text(c) for c in el if _local(c.tag) == "creator-initials"), "")
                    body = " ".join(_flat_text(c, 120) for c in el if _local(c.tag) == "p")[:200]
                    out.add("Document comments", "annotation", f"author: {creator}; initials: {initials}; date: {date}; text: {body}")
                    rows += 1
                elif t == "change-info":
                    creator = next((_flat_text(c) for c in el if _local(c.tag) == "creator"), "")
                    date = next((_flat_text(c) for c in el if _local(c.tag) == "date"), "")
                    out.add("Document comments", "tracked change", f"author: {creator}; date: {date}")
                    rows += 1
                elif t in _ODF_FIELDS:
                    text = _flat_text(el, 200)
                    if text:
                        out.add("Document comments", f"field {_qname(el.tag, prefixes)}", text)
                        rows += 1



# ---------------------------------------------------------------- video (ffprobe + container boxes + encoder strings)

_ISO6709 = re.compile(r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)?(?:CRS\S*)?/?$")
_ENCODER_RE = re.compile(rb"(x264 - core [ -~]{0,500}|x265 \(build [ -~]{0,500}|Lavf[0-9][0-9.]{0,20}|Lavc[0-9][ -~]{0,40})")
_SCAN_BYTES = 4 << 20


def _iso6709_text(value: str) -> str | None:
    m = _ISO6709.match(value.strip())
    if not m:
        return None
    lat, lon, alt = m.group(1), m.group(2), m.group(3)
    return f"latitude {float(lat)}, longitude {float(lon)}" + (f", altitude {float(alt)} m" if alt else "")


def _ffprobe_rows(out: _Out, path: Path, ffprobe: str) -> None:
    cmd = [ffprobe, "-v", "error", "-protocol_whitelist", "file", "-f", "mov", "-print_format", "json", "-show_format",
           "-show_streams", "-show_chapters", "-i", video._file_url(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=_FFPROBE_TIMEOUT,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        out.add("Video", "could not read", f"ffprobe did not finish within {_FFPROBE_TIMEOUT} s")
        return
    except OSError as exc:
        out.add("Video", "could not read", f"ffprobe: {exc}")
        return
    try:
        info = json.loads(proc.stdout[:32 << 20])
    except ValueError:
        info = None
    if not isinstance(info, dict) or not (info.get("format") or info.get("streams")):
        lines = proc.stderr.strip().splitlines()
        out.add("Video", "could not read", "ffprobe: " + (lines[0] if lines else f"exit status {proc.returncode}"))
        return

    def emit_tags(section: str, tags) -> None:
        if not isinstance(tags, dict):
            return
        for k, v in list(tags.items())[:500]:
            out.add(section, str(k), v)
            if str(k).lower() in ("location", "location-eng", "com.apple.quicktime.location.iso6709"):
                decoded = _iso6709_text(str(v))
                if decoded:
                    out.add(section, f"{k} (decoded)", decoded)

    fmt = info.get("format") or {}
    for k, v in fmt.items():
        if k not in ("tags", "filename") and not isinstance(v, (dict, list)):
            out.add("Video", str(k), v)
    emit_tags("Video", fmt.get("tags"))
    for st in (info.get("streams") or [])[:64]:
        if not isinstance(st, dict):
            continue
        ctype = st.get("codec_type", "unknown")
        section = f"Stream {st.get('index', '?')} ({ctype})"
        for k, v in st.items():
            if k in ("tags", "disposition", "side_data_list", "index", "codec_type") or isinstance(v, (dict, list)):
                continue
            out.add(section, str(k), v)
        disp = st.get("disposition") or {}
        if isinstance(disp, dict):
            on = [k for k, v in disp.items() if v]
            if on:
                out.add(section, "disposition", ", ".join(on))
            if disp.get("attached_pic"):
                out.add(section, "cover art", f"attached picture ({st.get('codec_name', '?')})")
        if ctype == "data":
            out.add(section, "timed metadata track", st.get("codec_tag_string", ""))
        emit_tags(section, st.get("tags"))
        for sd in (st.get("side_data_list") or [])[:20]:
            if isinstance(sd, dict):
                out.add(section, "side data", ", ".join(f"{k}={v}" for k, v in sd.items() if not isinstance(v, (dict, list)))[:290])
    for ch in (info.get("chapters") or [])[:200]:
        if isinstance(ch, dict):
            tags = ch.get("tags") or {}
            out.add("Video", f"chapter {ch.get('id', '?')}", f"{tags.get('title', '')} at {ch.get('start_time', '?')} s".strip())


def _encoder_scan(out: _Out, rd, found: dict) -> None:
    chunks = []
    for key in ("mdat", "moov"):
        for bs, ps, be in found[key][:3]:
            chunks.append(rd.read(ps, min(be - ps, _SCAN_BYTES)))
    if not any(chunks):
        chunks.append(rd.read(0, _SCAN_BYTES))
    seen = []
    for chunk in chunks:
        for m in _ENCODER_RE.finditer(chunk):
            text = m.group(1).decode("ascii", "replace").strip()
            if text not in seen:
                seen.append(text)
                out.add("Video", "encoder string in bitstream", text)
            if len(seen) >= 12:
                return


def _video(out: _Out, path: Path, size: int) -> None:
    ffprobe = shutil.which("ffprobe")
    with open(path, "rb") as fh:
        rd = _FileRd(fh, size)
        found = _iso_top(out, "Video", rd, size, brands=ffprobe is None)
        if ffprobe is None:
            out.add("Video", "ffprobe", "not installed - only the container boxes are listed")
            for bs, ps, be in found["moov"][:1]:
                _guard(out, "Video", _iso_moov, out, "Video", rd, ps, be, 0, {"boxes": 0})
        else:
            _guard(out, "Video", _ffprobe_rows, out, path, ffprobe)
        _guard(out, "Video", _encoder_scan, out, rd, found)


# ---------------------------------------------------------------- entry point

_TYPE_NAMES = {}
for _e in _JPEG_EXT:
    _TYPE_NAMES[_e] = "JPEG image"
for _e in _PNG_EXT:
    _TYPE_NAMES[_e] = "PNG image"
_TYPE_NAMES.update({".webp": "WebP image", ".tif": "TIFF image", ".tiff": "TIFF image", ".heic": "HEIC image (HEIF)",
                    ".heif": "HEIF image", ".avif": "AVIF image (HEIF)", ".pdf": "PDF document"})


def _type_name(ext: str) -> str | None:
    if ext in _TYPE_NAMES:
        return _TYPE_NAMES[ext]
    if ext in office.OOXML_EXT:
        return "Office Open XML document"
    if ext in office.ODF_EXT:
        return "OpenDocument document"
    if ext in video.VIDEO_EXT:
        return "video (ISO base media / QuickTime)"
    return None


def _dump(out: _Out, path: Path) -> None:
    ext = path.suffix.lower()
    kind = _type_name(ext)
    try:
        st = os.stat(path)
    except OSError as exc:
        out.add("File", "type", kind or "not supported by metawipe")
        out.add("File", "size", "unknown")
        out.add("File", "could not read", f"{type(exc).__name__}: {exc.strerror or exc}")
        return
    regular = statmod.S_ISREG(st.st_mode)
    out.add("File", "type", ("not supported by metawipe" if kind is None else kind) if regular else "not a regular file")
    out.add("File", "size", _size_text(st.st_size) if regular else "unknown")
    out.add("File", "name", path.name)
    out.add("File", "modified", _fmt_time(st.st_mtime))
    out.add("File", "permissions", f"{statmod.S_IMODE(st.st_mode):04o}")
    if not regular or kind is None:
        return
    size = st.st_size
    if ext in _JPEG_EXT | _PNG_EXT | _TIFF_EXT | {".webp"}:
        with open(path, "rb") as fh:
            rd = _FileRd(fh, size)
            if ext in _JPEG_EXT:  # read, not mmap: a file cut by another process meanwhile would kill us with SIGBUS
                # shortcut: a JPEG over 256 MB is shown as cut there; raise the cap if such files turn up
                _guard(out, "JPEG", lambda: _jpeg(out, _Mem(fh.read(256 << 20))))
            elif ext in _PNG_EXT:
                _guard(out, "PNG", _png, out, rd)
            elif ext == ".webp":
                _guard(out, "WebP", _webp, out, rd)
            else:
                _guard(out, "TIFF", _tiff_file, out, rd)
    elif ext in _HEIF_EXT:
        _guard(out, "HEIF", _heif, out, path, size)
    elif ext == ".pdf":
        _guard(out, "PDF", _pdf, out, path)
    elif ext in office.OOXML_EXT | office.ODF_EXT:
        _guard(out, "Document", _package, out, path, ext)
    elif ext in video.VIDEO_EXT:
        _guard(out, "Video", _video, out, path, size)


def dump_file(path: Path) -> list[tuple[str, str, str]]:
    """Every piece of metadata in `path` as (section, name, value) rows. Never raises, never writes."""
    out = _Out()
    try:
        _dump(out, Path(path))
    except _Stop:
        pass
    except Exception as exc:  # noqa: BLE001 - this function must never raise
        try:
            out.fail("File", exc)
        except _Stop:
            pass
    if not out.rows:
        out.rows.append(("File", "type", "unknown"))
    return out.rows
