"""Read-only look at the identifying metadata a file carries (used by the TUI preview).

Every function returns a list of (label, value) pairs; an empty list means nothing was found.
Parsing is best effort and never writes anything.
"""
from __future__ import annotations

import json
import posixpath
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from . import dump, images, isobmff, office, video
from .common import WipeError, quiet_pypdf

_EXIF_NAMES = {
    270: "ImageDescription", 271: "Make", 272: "Model", 305: "Software", 306: "DateTime", 315: "Artist",
    316: "HostComputer", 33432: "Copyright", 269: "DocumentName", 36867: "DateTimeOriginal",
    36868: "DateTimeDigitized", 42032: "OwnerName", 42033: "BodySerialNumber", 42035: "LensMake",
    42036: "LensModel", 42037: "LensSerialNumber", 37510: "UserComment",
    0x9C9B: "XPTitle", 0x9C9C: "XPComment", 0x9C9D: "XPAuthor", 0x9C9E: "XPKeywords", 0x9C9F: "XPSubject",
}
_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
_XMP_ID = b"http://ns.adobe.com/xap/1.0/\x00"
_READ_CAP = 256 << 20  # largest file read into memory
_PART_CAP = 8 << 20  # largest zip member read
_SEGMENT_CAP = 1000  # real JPEGs have a few dozen segments outside their scans


def _tiff_fields(tiff: bytes) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if tiff[:2] not in (b"II", b"MM") or len(tiff) < 8:
        return out
    order = "little" if tiff[:2] == b"II" else "big"

    def u(off, n):
        return int.from_bytes(tiff[off:off + n], order)

    def entries(ifd):
        if ifd <= 0 or ifd + 2 > len(tiff):
            return
        for n in range(min(u(ifd, 2), 512)):
            e = ifd + 2 + 12 * n
            if e + 12 > len(tiff):
                return
            typ, cnt = u(e + 2, 2), u(e + 4, 4)
            size = _SIZES.get(typ, 1) * cnt
            off = e + 8 if size <= 4 else u(e + 8, 4)
            yield u(e, 2), typ, cnt, off, size

    def rationals(off, cnt):
        vals = []
        for k in range(cnt):
            num, den = u(off + 8 * k, 4), u(off + 8 * k + 4, 4)
            vals.append(num / den if den else 0.0)
        return vals

    seen = set()  # IFDs that point at each other must not be read over and over

    def walk(ifd, depth=0):
        if ifd in seen:
            return
        seen.add(ifd)
        gps = {}
        for tag, typ, cnt, off, size in entries(ifd):
            if tag in (34665, 40965) and depth < 3:
                walk(u(off, 4), depth + 1)
            elif tag == 34853 and depth < 3 and u(off, 4) not in seen:
                seen.add(u(off, 4))
                for t2, ty2, c2, o2, s2 in entries(u(off, 4)):
                    gps[t2] = (ty2, c2, o2, s2)
            elif tag == 37500:
                out.append(("MakerNote", f"{size} bytes"))
            elif tag in _EXIF_NAMES:
                raw = tiff[off:off + size]
                if typ == 2:
                    text = raw.split(b"\x00")[0].decode("utf-8", "replace").strip()
                elif tag >= 0x9C9B:
                    text = raw.decode("utf-16-le", "replace").rstrip("\x00")
                else:
                    text = f"{size} bytes"
                if text:
                    out.append((_EXIF_NAMES[tag], text[:80]))
            elif tag in (700, 33723, 34377):
                out.append(({700: "XMP", 33723: "IPTC", 34377: "Photoshop"}[tag], f"{size} bytes"))
        if 2 in gps and 4 in gps:
            try:
                lat = sum(v / 60 ** i for i, v in enumerate(rationals(gps[2][2], 3)))
                lon = sum(v / 60 ** i for i, v in enumerate(rationals(gps[4][2], 3)))
                ref_lat = tiff[gps[1][2]:gps[1][2] + 1].decode("ascii", "replace") if 1 in gps else "?"
                ref_lon = tiff[gps[3][2]:gps[3][2] + 1].decode("ascii", "replace") if 3 in gps else "?"
                out.append(("GPS", f"{lat:.5f}°{ref_lat} {lon:.5f}°{ref_lon}"))
            except (IndexError, ValueError):
                out.append(("GPS", "present"))
        elif gps:
            out.append(("GPS", "present"))

    walk(u(4, 4))
    return out


def _xmp_fields(xmp: bytes) -> list[tuple[str, str]]:
    found = [("XMP", f"{len(xmp)} bytes")]
    m = re.search(rb"<dc:creator>.*?<rdf:li[^>]*>([^<]+)", xmp, re.S)
    if m:
        found.append(("XMP creator", m.group(1).decode("utf-8", "replace")[:80]))
    return found


def _jpeg(data: bytes) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        for count, (marker, start, end) in enumerate(images.jpeg_segments(data)):
            if count == _SEGMENT_CAP:
                out.append(("Not fully inspected", f"more than {_SEGMENT_CAP} JPEG segments"))
                break
            payload = data[start + 4:end] if 0xE0 <= marker <= 0xEF or marker == 0xFE else b""
            if marker == 0xE1 and payload[:6] == b"Exif\x00\x00":
                out += _tiff_fields(payload[6:])
            elif marker == 0xE1 and payload.startswith(_XMP_ID):
                out += _xmp_fields(payload[len(_XMP_ID):])
            elif marker == 0xED:
                out.append(("Photoshop/IPTC", f"{len(payload)} bytes"))
            elif marker == 0xFE:
                out.append(("Comment", payload.decode("utf-8", "replace")[:80]))
            elif marker == 0xE2 and payload[:4] == b"MPF\x00":
                out.append(("MPF (extra images)", f"{len(payload)} bytes"))
            elif marker == 0xE0 and payload[:5] == b"JFIF\x00" and len(payload) > 14 and (payload[12] or payload[13]):
                out.append(("JFIF thumbnail", f"{payload[12]}x{payload[13]}"))
            elif marker == 0xD9 and end < len(data):
                out.append(("Data after end of image", f"{len(data) - end} bytes"))
    except WipeError:
        pass  # damaged JPEG: keep what was found before the damage
    return out


def _png(data: bytes) -> list[tuple[str, str]]:
    out, i = [], 8
    while i + 12 <= len(data):
        length = int.from_bytes(data[i:i + 4], "big")
        ctype, payload = data[i + 4:i + 8], data[i + 8:i + 8 + length]
        if ctype == b"eXIf":
            out += _tiff_fields(payload)
        elif ctype in (b"tEXt", b"zTXt", b"iTXt"):
            keyword = payload.split(b"\x00")[0].decode("latin-1")
            out.append((f"PNG text '{keyword}'", f"{length} bytes"))
        elif ctype == b"tIME":
            out.append(("Modification time", "present"))
        elif ctype == b"caBX":
            out.append(("C2PA content credentials", f"{length} bytes"))
        i += 12 + length
        if ctype == b"IEND":
            break
    return out


def _webp(data: bytes) -> list[tuple[str, str]]:
    out, i = [], 12
    while i + 8 <= len(data):
        fourcc, size = data[i:i + 4], int.from_bytes(data[i + 4:i + 8], "little")
        payload = data[i + 8:i + 8 + size]
        if fourcc == b"EXIF":
            out += _tiff_fields(payload[6:] if payload[:6] == b"Exif\x00\x00" else payload)
        elif fourcc == b"XMP ":
            out += _xmp_fields(payload)
        i += 8 + size + (size & 1)
    return out


def _heif(data: bytes) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    buf = bytearray(data)
    try:
        isobmff._refuse_stray_items(buf, 0, len(buf), top=True)  # the cleaner refuses these files too
        for btype, _, pp, be in isobmff._boxes(buf, 0, len(buf)):
            if btype != b"meta":
                continue
            children = list(isobmff._boxes(buf, pp + 4, be))
            for name in (b"iinf", b"iloc", b"idat"):  # the cleaner refuses these files too
                if sum(c[0] == name for c in children) > 1:
                    raise WipeError(f"more than one {name.decode()} box in meta")
            children = {c[0]: c for c in children}
            if b"iinf" not in children or b"iloc" not in children:
                continue
            _, _, ip, ie = children[b"iinf"]
            first = ip + 4 + (2 if buf[ip] == 0 else 4)
            wanted = {}
            for bt, _, p2, b2 in isobmff._boxes(buf, first, ie):
                info = isobmff._parse_infe(buf, p2, b2) if bt == b"infe" else None
                if not info:
                    continue
                if info[1] in (b"Exif", b"mime"):  # exiv2 shows every mime item as XMP
                    wanted[info[0]] = info[1]
            _, _, lp, le = children[b"iloc"]
            idat = children.get(b"idat")
            for item_id, extents in isobmff._iloc_extents(buf, lp, le, set(wanted)).items():
                for method, offset, length in extents:
                    start = offset if method == 0 else (idat[2] + offset if idat else -1)
                    if start < 0 or length == 0:
                        continue
                    chunk = bytes(buf[start:start + length])
                    if wanted[item_id] == b"Exif":
                        skip = 4 + int.from_bytes(chunk[:4], "big")
                        out += _tiff_fields(chunk[skip:])
                    else:
                        out += _xmp_fields(chunk)
    except Exception:  # noqa: BLE001 - preview must never crash
        out.append(("HEIF metadata", "could not be parsed"))
    return out


@quiet_pypdf()
def _pdf(path: Path) -> list[tuple[str, str]]:
    from pypdf import PdfReader

    out: list[tuple[str, str]] = []
    reader = PdfReader(path)
    if reader.is_encrypted:
        reader.decrypt("")
    meta = reader.metadata or {}
    for key in ("/Author", "/Creator", "/Producer", "/Title", "/Subject", "/Keywords", "/CreationDate", "/ModDate"):
        if meta.get(key):
            out.append((key[1:], str(meta[key])[:80]))
    if "/Metadata" in reader.trailer["/Root"]:
        out.append(("XMP", "present"))
    if reader.trailer.get("/ID") is not None:
        out.append(("File ID", "present (links the file to its source)"))
    authors = set()
    for page in reader.pages:
        for annot in page.get("/Annots", []) or []:
            a = annot.get_object()
            if a.get("/T"):
                authors.add(str(a["/T"]))
    if authors:
        out.append(("Annotation authors", ", ".join(sorted(authors))[:80]))
    return out


def _package(path: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        left = _READ_CAP  # for all members together, or many big parts take minutes

        def read(name, cap=_PART_CAP):
            nonlocal left
            with z.open(name) as f:
                data = f.read(min(cap, left))
            left -= len(data)
            return data

        def text(name):
            return read(name).decode("utf-8", "replace")

        if "meta.xml" in names:  # OpenDocument
            meta = text("meta.xml")
            for tag in ("initial-creator", "creator", "creation-date", "date", "generator", "user-defined"):
                for m in re.finditer(rf"<(?:meta|dc):{tag}[^>]*>([^<]+)<", meta):
                    out.append((tag, m.group(1)[:80]))
            for name in names:
                if name.startswith("Thumbnails/") and not name.endswith("/"):
                    out.append(("Thumbnail", name))
            if "settings.xml" in names:
                m = re.search(r'config:name="PrinterName"[^>]*>([^<]+)<', text("settings.xml"))
                if m:
                    out.append(("Printer name", m.group(1)[:80]))
            if "content.xml" in names:
                creators = set(re.findall(r"<dc:creator>([^<]+)<", text("content.xml"))) - {"Author"}
                if creators:
                    out.append(("Comment/revision authors", ", ".join(sorted(creators))[:80]))
        for name, fields in (("docProps/core.xml", ("creator", "lastModifiedBy", "created", "modified", "title", "subject", "keywords")),
                             ("docProps/app.xml", ("Company", "Manager", "Application", "TotalTime"))):
            if name in names:
                xml = text(name)
                for field in fields:
                    m = re.search(rf"<(?:\w+:)?{field}\b[^>]*>([^<]+)<", xml)
                    if m and m.group(1).strip():
                        out.append((field, m.group(1).strip()[:80]))
        if "docProps/custom.xml" in names:
            out.append(("Custom properties", "present"))
        if any(n.startswith("docProps/thumbnail") for n in names):
            out.append(("Thumbnail", "present"))
        authors = set()
        for name in names:
            if re.match(r"^word/(comments|document|footnotes|endnotes)\.xml$", name):
                authors.update(re.findall(r'w:author="([^"]+)"', text(name)))
            elif re.match(r"^xl/comments[^/]*\.xml$", name):
                authors.update(re.findall(r"<author>([^<]+)<", text(name)))
            elif name == "ppt/commentAuthors.xml":
                authors.update(re.findall(r'\bname="([^"]+)"', text(name)))
        authors.discard("Author")  # what a scrub leaves behind
        if authors:
            out.append(("Comment/revision authors", ", ".join(sorted(authors))[:80]))
        for name in names:
            if "/printerSettings/" in name:
                out.append(("Printer settings", name))
            elif name.startswith("customXml/") and name.endswith("item1.xml"):
                out.append(("customXml store", name))
            elif name == "word/people.xml":
                out.append(("People list", name))
            elif name.startswith("_xmlsignatures/") and name.endswith(".xml"):
                out.append(("Digital signature", name))
        for name in names:
            ext = posixpath.splitext(name)[1].lower()
            if any(m in "/" + name.lower() for m in office._MEDIA_DIRS) and ext in images.BY_EXT:
                found = _by_extension(read(name, 32 << 20), ext)
                if found:
                    out.append((f"Embedded picture {posixpath.basename(name)}", ", ".join(f"{k}" for k, _ in found)[:80]))
        if left <= 0:
            out.append(("Not fully inspected", f"parts over {_READ_CAP >> 20} MB unpacked"))
    return out


def _by_extension(data: bytes, ext: str) -> list[tuple[str, str]]:
    if ext in (".jpg", ".jpeg", ".jpe"):
        return _jpeg(data)
    if ext in (".png", ".apng"):
        return _png(data)
    if ext == ".webp":
        return _webp(data)
    return _tiff_fields(data)


def _video(path: Path) -> list[tuple[str, str]]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return [("ffprobe", "not installed - cannot inspect")]
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-protocol_whitelist", "file", "-f", "mov", "-print_format", "json", "-show_format",
         "-show_streams", "-i", video._file_url(path)], capture_output=True, text=True, timeout=60)
    try:
        info = json.loads(proc.stdout)
    except ValueError:
        return [("ffprobe", "could not read the file")]
    out: list[tuple[str, str]] = []
    boring = {"major_brand", "minor_version", "compatible_brands", "language", "handler_name", "vendor_id"}
    for k, v in (info.get("format", {}).get("tags") or {}).items():
        if k not in boring:
            out.append((k, str(v)[:80]))
    for s in info.get("streams", []):
        if s.get("disposition", {}).get("attached_pic"):
            out.append(("Cover art", s.get("codec_name", "")))
        elif s.get("codec_type") == "data":
            out.append(("Timed metadata track", s.get("codec_tag_string", "")))
        for k, v in (s.get("tags") or {}).items():
            if k not in boring:
                out.append((f"stream {s.get('index')} {k}", str(v)[:80]))
    return out


def _read(path: Path) -> bytes:
    with path.open("rb") as f:
        return f.read(_READ_CAP)


def inspect_file(path: Path) -> list[tuple[str, str]]:
    """Identifying metadata found in `path` (best effort)."""
    if not path.is_file():  # a FIFO would block, a device would never end
        return [("could not inspect", "not a regular file")]
    # values are raw file text that reaches the terminal: strip control characters
    return [(dump._clip(k), dump._clip(v)) for k, v in _inspect(path)]


def _inspect(path: Path) -> list[tuple[str, str]]:
    ext = path.suffix.lower()
    try:
        if ext in (".jpg", ".jpeg", ".jpe"):
            return _jpeg(_read(path))
        if ext in (".png", ".apng"):
            return _png(_read(path))
        if ext == ".webp":
            return _webp(_read(path))
        if ext in (".tif", ".tiff"):
            return _tiff_fields(_read(path))
        if ext in (".heic", ".heif", ".avif"):
            return _heif(_read(path))
        if ext == ".pdf":
            return _pdf(path)
        if ext in office.OOXML_EXT | office.ODF_EXT:
            return _package(path)
        if ext in video.VIDEO_EXT:
            return _video(path)
    except Exception as e:  # noqa: BLE001 - a broken file must not crash the preview
        return [("could not inspect", f"{type(e).__name__}: {e}")]
    return [("unsupported", "metawipe cannot clean this file type")]
