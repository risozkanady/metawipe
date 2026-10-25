"""Metadata removal for OOXML (docx/xlsx/pptx and variants) and OpenDocument packages.

Both are zip files. They are rebuilt entry by entry: document properties, thumbnails, signatures,
printer settings and the other known identity-bearing parts are dropped, relationships and
content-type entries that pointed at them are pruned with a real XML parser, nested Office packages
are cleaned recursively and the zip container itself is normalised (fixed timestamps, no extra
fields). With --full, comment/revision authors and dates are anonymised, pictures inside the package
are cleaned and customXml stores are dropped as well.
"""
from __future__ import annotations

import io
import posixpath
import re
import shutil
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from . import images
from .common import Context, WipeError

OOXML_EXT = {
    ".docx", ".docm", ".dotx", ".dotm", ".xlsx", ".xlsm", ".xltx", ".xltm",
    ".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm",
}
ODF_EXT = {".odt", ".ods", ".odp", ".odg", ".odf", ".ott", ".ots", ".otp", ".otg"}

_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_MANIFEST_NS = "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"
# Relationship types (from the package root) that always point at metadata parts, wherever they live.
_METADATA_REL_SUFFIXES = (
    "/metadata/core-properties", "/extended-properties", "/custom-properties", "/metadata/thumbnail",
    "/digital-signature/origin",
)
_ODF_EMPTY_META = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<office:document-meta xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'office:version="1.3"><office:meta/></office:document-meta>'
).encode()
_EPOCH = (1980, 1, 1, 0, 0, 0)
_MAX_DEPTH = 3
_FIXED_DATE = b"2000-01-01T00:00:00Z"
_MEDIA_DIRS = ("/media/", "Pictures/", "media/")

# Person lists name people (and their accounts); they identify rather than hold content.
_OOXML_PEOPLE = [
    (re.compile(r"^(xl/persons/.*|ppt/authors)\.xml$"), [
        (re.compile(rb'(\bdisplayName=|\bname=)"[^"]*"'), rb'\1"Author"'),
        (re.compile(rb'(\buserId=)"[^"]*"'), rb'\1"author"'),
        (re.compile(rb'(\bproviderId=)"[^"]*"'), rb'\1"None"'),
    ]),
    (re.compile(r"^ppt/commentAuthors\.xml$"), [
        (re.compile(rb'(\bname=)"[^"]*"'), rb'\1"Author"'),
        (re.compile(rb'(\binitials=)"[^"]*"'), rb'\1"A"'),
    ]),
    (re.compile(r"^xl/workbook\.xml$"), [
        (re.compile(rb"<(?:\w+:)?absPath\b[^>]*/>"), b""),
        (re.compile(rb"<(\w+:)?absPath\b[^>]*>.*?</(?:\w+:)?absPath>", re.S), b""),
        (re.compile(rb"(<(?:\w+:)?fileSharing\b[^>]*?)\s+userName=\"[^\"]*\""), rb"\1"),
    ]),
]
# --full: author names and timestamps on comments and tracked changes.
_OOXML_ANON = [
    (re.compile(r"^word/.*\.xml$"), [
        (re.compile(rb'(\bw:author=)"[^"]*"'), rb'\1"Author"'),
        (re.compile(rb'(\bw:initials=)"[^"]*"'), rb'\1"A"'),
        (re.compile(rb'\sw:date="[^"]*"'), b""),
        (re.compile(rb"<w:docVars>.*?</w:docVars>", re.S), b""),
    ]),
    (re.compile(r"^xl/comments[^/]*\.xml$"), [
        (re.compile(rb"(<(?:\w+:)?author>)[^<]*(</(?:\w+:)?author>)"), rb"\1Author\2"),
    ]),
    (re.compile(r"^xl/threadedComments/.*\.xml$"), [
        (re.compile(rb'(\bdT=)"[^"]*"'), rb'\1"2000-01-01T00:00:00.00"'),
    ]),
    (re.compile(r"^ppt/comments/.*\.xml$"), [
        (re.compile(rb'(\b(?:dt|created)=)"[^"]*"'), rb'\1"2000-01-01T00:00:00.000"'),
    ]),
]
_ODF_SETTINGS = [
    (re.compile(rb'(<config:config-item\b[^>]*config:name="(?:PrinterName|PrinterSetup)"[^>]*>)[^<]*(</config:config-item>)'), rb"\1\2"),
]
_ODF_ANON = [
    (re.compile(rb"(<dc:creator>)[^<]*(</dc:creator>)"), rb"\1Author\2"),
    (re.compile(rb"(<dc:date>)[^<]*(</dc:date>)"), rb"\1" + _FIXED_DATE + rb"\2"),
    (re.compile(rb"(<meta:date-string>)[^<]*(</meta:date-string>)"), rb"\1\2"),
    (re.compile(rb"(<text:(author-name|author-initials|initial-creator|creator)\b[^>]*>)[^<]*(</text:\2>)"), rb"\1\3"),
]


class _RawZipInfo(zipfile.ZipInfo):
    """ZipInfo that writes the entry name exactly as it was stored (no UTF-8 re-encoding)."""

    def __init__(self, filename, raw_name: bytes, flag_bits: int, **kw):
        super().__init__(filename, **kw)
        self._raw_name = raw_name
        self._raw_flags = flag_bits

    def _encodeFilenameFlags(self):  # noqa: N802 - overrides the zipfile hook
        return self._raw_name, self._raw_flags


def _zinfo(item: zipfile.ZipInfo) -> zipfile.ZipInfo:
    if not item.flag_bits & 0x800 and not item.filename.isascii():
        try:  # legacy (cp437) name: keep the stored bytes so references still match
            zi = _RawZipInfo(item.filename, item.filename.encode("cp437"), item.flag_bits & 0x800, date_time=_EPOCH)
        except UnicodeEncodeError:
            zi = zipfile.ZipInfo(item.filename, date_time=_EPOCH)
    else:
        zi = zipfile.ZipInfo(item.filename, date_time=_EPOCH)
    zi.compress_type = item.compress_type
    zi.external_attr = 0o644 << 16 if not item.is_dir() else (0o755 << 16) | 0x10
    zi.create_system = 3
    return zi


def _resolve(base: str, target: str) -> str:
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join(base, target))


def _metadata_targets(zin: zipfile.ZipFile) -> set[str]:
    """Parts the package root marks as properties/thumbnail/signature origin, wherever they are stored."""
    try:
        root = ET.fromstring(zin.read("_rels/.rels"))
    except KeyError:
        return set()
    return {
        _resolve("", rel.get("Target", ""))
        for rel in root
        if rel.get("TargetMode") != "External" and rel.get("Type", "").endswith(_METADATA_REL_SUFFIXES)
    }


def _ooxml_dropped(names: list[str], zin: zipfile.ZipFile, full: bool) -> set[str]:
    dropped = _metadata_targets(zin)
    for name in names:
        if (
            name.startswith(("docProps/", "package/services/metadata/", "_xmlsignatures/"))
            or name == "word/people.xml"
            or "/printerSettings/" in name
            or (full and name.startswith("customXml/"))
        ):
            dropped.add(name)
    return dropped


def _odf_dropped(names: list[str], full: bool) -> set[str]:
    return {
        n for n in names
        if n.startswith("Thumbnails/")
        or n in ("META-INF/documentsignatures.xml", "META-INF/macrosignatures.xml")
        or (full and n.startswith("Configurations2/"))
    }


def _apply(rules, data: bytes) -> bytes:
    for pattern, repl in rules:
        data = pattern.sub(repl, data)
    return data


def _ooxml_rules(name: str, full: bool) -> list:
    rules = []
    for name_re, rs in _OOXML_PEOPLE + (_OOXML_ANON if full else []):
        if name_re.match(name):
            rules.extend(rs)
    return rules


def _clean_rels(name: str, data: bytes, dropped: set[str]) -> bytes:
    base = posixpath.dirname(posixpath.dirname(name))
    ET.register_namespace("", _REL_NS)
    root = ET.fromstring(data)
    changed = False
    for rel in list(root):
        target = rel.get("Target", "")
        if rel.get("TargetMode") == "External":
            if rel.get("Type", "").endswith("/attachedTemplate"):
                rel.set("Target", "Normal.dotm")  # keeps the r:id valid, drops the original path
                changed = True
        elif _resolve(base, target) in dropped:
            root.remove(rel)
            changed = True
    if not changed:
        return data
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def _clean_content_types(data: bytes, dropped: set[str]) -> bytes:
    ET.register_namespace("", _CT_NS)
    root = ET.fromstring(data)
    changed = False
    for el in list(root):
        if el.tag == f"{{{_CT_NS}}}Override" and el.get("PartName", "").lstrip("/") in dropped:
            root.remove(el)
            changed = True
    if not changed:
        return data
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def _clean_manifest(data: bytes, dropped: set[str]) -> bytes:
    ET.register_namespace("manifest", _MANIFEST_NS)
    root = ET.fromstring(data)
    entry = f"{{{_MANIFEST_NS}}}file-entry"
    path_attr = f"{{{_MANIFEST_NS}}}full-path"
    changed = False
    for el in list(root):
        if el.tag == entry and el.get(path_attr, "") in dropped:
            root.remove(el)
            changed = True
    if not changed:
        return data
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def _check_odf_not_encrypted(zin: zipfile.ZipFile) -> None:
    try:
        manifest = zin.read("META-INF/manifest.xml")
    except KeyError:
        return
    if b"encryption-data" in manifest:
        raise WipeError("encrypted OpenDocument files are not supported")


def clean_package(zin: zipfile.ZipFile, zout: zipfile.ZipFile, ctx: Context, depth: int = 0) -> None:
    names = [i.filename for i in zin.infolist()]
    is_odf = "mimetype" in names and zin.read("mimetype").startswith(b"application/vnd.oasis.opendocument")
    if not is_odf and "[Content_Types].xml" not in names:
        raise WipeError("not an OOXML or OpenDocument package")

    if is_odf:
        _check_odf_not_encrypted(zin)
        dropped = _odf_dropped(names, ctx.full)
    else:
        dropped = _ooxml_dropped(names, zin, ctx.full)
        if not ctx.full and any(n.startswith("customXml/") for n in names):
            ctx.warn("package contains customXml parts (SharePoint/bibliography data); use --full to drop them")
    if any(n.startswith(("_xmlsignatures/", "META-INF/documentsignatures")) for n in names):
        ctx.warn("digital signatures were removed (cleaning invalidates them)")
    if any(posixpath.splitext(n)[1].lower() in (".bin", ".doc", ".xls", ".ppt") and "embeddings/" in n for n in names):
        ctx.leftover("package contains OLE embeddings; their metadata cannot be cleaned")

    items = list(zin.infolist())
    if is_odf:  # `mimetype` must be the first, stored entry
        items.sort(key=lambda i: i.filename != "mimetype")

    for item in items:
        name = item.filename
        if name in dropped:
            continue
        zi = _zinfo(item)
        base = posixpath.basename(name)
        if item.is_dir():
            zout.writestr(zi, b"")
        elif is_odf and name == "mimetype":
            zi.compress_type = zipfile.ZIP_STORED
            zout.writestr(zi, zin.read(name))
        elif is_odf and base == "meta.xml":
            zout.writestr(zi, _ODF_EMPTY_META)
        elif is_odf and base == "settings.xml":
            zout.writestr(zi, _apply(_ODF_SETTINGS, zin.read(name)))
        elif is_odf and name == "META-INF/manifest.xml":
            zout.writestr(zi, _clean_manifest(zin.read(name), dropped))
        elif is_odf and ctx.full and base in ("content.xml", "styles.xml"):
            zout.writestr(zi, _apply(_ODF_ANON, zin.read(name)))
        elif not is_odf and name == "[Content_Types].xml":
            zout.writestr(zi, _clean_content_types(zin.read(name), dropped))
        elif not is_odf and name.endswith(".rels"):
            zout.writestr(zi, _clean_rels(name, zin.read(name), dropped))
        elif not is_odf and name.endswith(".xml") and _ooxml_rules(name, ctx.full):
            zout.writestr(zi, _apply(_ooxml_rules(name, ctx.full), zin.read(name)))
        elif _is_nested_package(name, is_odf):
            if depth >= _MAX_DEPTH:
                ctx.leftover(f"embedded package {name} is nested too deeply to clean")
                _copy(zin, zout, item, zi)
            else:
                zout.writestr(zi, _clean_nested(zin.read(name), name, ctx, depth + 1))
        elif ctx.full and any(m in "/" + name for m in _MEDIA_DIRS) and posixpath.splitext(name)[1].lower() in images.BY_EXT:
            zout.writestr(zi, _clean_media(name, zin.read(name), ctx))
        else:
            _copy(zin, zout, item, zi)


def _copy(zin, zout, item, zi) -> None:
    zi.file_size = item.file_size
    with zin.open(item) as src, zout.open(zi, "w", force_zip64=item.file_size >= 2**31) as dst:
        shutil.copyfileobj(src, dst)


def _clean_media(name: str, data: bytes, ctx: Context) -> bytes:
    fn = images.BY_EXT.get(posixpath.splitext(name)[1].lower())
    if fn is None:
        return data
    try:
        return fn(data)
    except WipeError as e:
        ctx.leftover(f"embedded picture {name} could not be cleaned ({e})")
        return data


def _is_nested_package(name: str, is_odf: bool) -> bool:
    ext = posixpath.splitext(name)[1].lower()
    return not is_odf and "embeddings/" in name and ext in OOXML_EXT | ODF_EXT


def _clean_nested(data: bytes, name: str, ctx: Context, depth: int) -> bytes:
    try:
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            clean_package(zin, zout, ctx, depth)
        return buf.getvalue()
    except (WipeError, zipfile.BadZipFile, ET.ParseError) as e:
        ctx.leftover(f"embedded package {name} left untouched ({e})")
        return data


def package(src: Path, dst: Path, ctx: Context) -> None:
    try:
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            clean_package(zin, zout, ctx)
    except zipfile.BadZipFile as e:
        raise WipeError(f"not a valid zip package: {e}") from e
    except ET.ParseError as e:
        raise WipeError(f"malformed XML in package: {e}") from e
