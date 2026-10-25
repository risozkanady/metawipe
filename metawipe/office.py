"""Metadata removal for OOXML (docx/xlsx/pptx and variants) and OpenDocument packages.

Both are zip files. They are rebuilt entry by entry: document properties, thumbnails and the
other known identity-bearing parts are dropped, relationships and content-type entries that
pointed at them are pruned with a real XML parser, nested Office packages are cleaned
recursively and the zip container itself is normalised (fixed timestamps, no extra fields).
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
_ODF_EMPTY_META = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<office:document-meta xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'office:version="1.3"><office:meta/></office:document-meta>'
).encode()
_ODF_EMPTY_SETTINGS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<office:document-settings xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'office:version="1.3"><office:settings/></office:document-settings>'
).encode()
_EPOCH = (1980, 1, 1, 0, 0, 0)
_MAX_DEPTH = 3
_FIXED_DATE = b"2000-01-01T00:00:00Z"
_MEDIA_DIRS = ("/media/", "Pictures/", "media/")

# --full: author names and timestamps on comments, tracked changes and people lists.
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
    (re.compile(r"^(xl/persons/.*|ppt/authors)\.xml$"), [
        (re.compile(rb'(\bdisplayName=|\bname=)"[^"]*"'), rb'\1"Author"'),
        (re.compile(rb'(\buserId=)"[^"]*"'), rb'\1"author"'),
        (re.compile(rb'(\bproviderId=)"[^"]*"'), rb'\1"None"'),
    ]),
    (re.compile(r"^ppt/commentAuthors\.xml$"), [
        (re.compile(rb'(\bname=)"[^"]*"'), rb'\1"Author"'),
        (re.compile(rb'(\binitials=)"[^"]*"'), rb'\1"A"'),
    ]),
    (re.compile(r"^ppt/comments/.*\.xml$"), [
        (re.compile(rb'(\b(?:dt|created)=)"[^"]*"'), rb'\1"2000-01-01T00:00:00.000"'),
    ]),
]
_ODF_ANON = [
    (re.compile(rb"(<dc:creator>)[^<]*(</dc:creator>)"), rb"\1Author\2"),
    (re.compile(rb"(<dc:date>)[^<]*(</dc:date>)"), rb"\1" + _FIXED_DATE + rb"\2"),
    (re.compile(rb"(<meta:date-string>)[^<]*(</meta:date-string>)"), rb"\1\2"),
    (re.compile(rb"(<text:(author-name|author-initials|initial-creator|creator)\b[^>]*>)[^<]*(</text:\2>)"), rb"\1\3"),
]


def _ooxml_dropped(name: str, full: bool) -> bool:
    if (
        name.startswith("docProps/")
        or name.startswith("package/services/metadata/")
        or name == "word/people.xml"
    ):
        return True
    # --full: SharePoint/bibliography stores and printer settings (printer and user names)
    return full and (name.startswith("customXml/") or "/printerSettings/" in name)


def _anonymize(name: str, data: bytes, is_odf: bool) -> bytes:
    if is_odf:
        if posixpath.basename(name) in ("content.xml", "styles.xml"):
            for pattern, repl in _ODF_ANON:
                data = pattern.sub(repl, data)
        return data
    for name_re, rules in _OOXML_ANON:
        if name_re.match(name):
            for pattern, repl in rules:
                data = pattern.sub(repl, data)
    return data


def _clean_media(name: str, data: bytes, ctx: Context) -> bytes:
    fn = images.BY_EXT.get(posixpath.splitext(name)[1].lower())
    if fn is None:
        return data
    try:
        return fn(data)
    except WipeError as e:
        ctx.leftover(f"embedded picture {name} could not be cleaned ({e})")
        return data


def _odf_dropped(name: str, full: bool) -> bool:
    return name.startswith("Thumbnails/") or (full and name.startswith("Configurations2/"))


def _resolve(base: str, target: str) -> str:
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join(base, target))


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


def _clean_manifest(data: bytes, ctx: Context, dropped: set[str]) -> bytes:
    text = data.decode("utf-8")
    for name in sorted(dropped):
        pattern = r"<(\w+):file-entry\b[^>]*\bfull-path=([\"'])" + re.escape(name) + r"\2[^>]*/>\s*"
        text = re.sub(pattern, "", text)
        if re.search(r"full-path=([\"'])" + re.escape(name) + r"\1", text):
            ctx.warn(f"manifest entry for {name} could not be removed")
    return text.encode("utf-8")


def _zinfo(item: zipfile.ZipInfo) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(item.filename, date_time=_EPOCH)
    zi.compress_type = item.compress_type
    zi.external_attr = 0o644 << 16 if not item.is_dir() else (0o755 << 16) | 0x10
    zi.create_system = 3
    return zi


def clean_package(zin: zipfile.ZipFile, zout: zipfile.ZipFile, ctx: Context, depth: int = 0) -> None:
    names = [i.filename for i in zin.infolist()]
    is_odf = "mimetype" in names and zin.read("mimetype").startswith(b"application/vnd.oasis.opendocument")
    if not is_odf and "[Content_Types].xml" not in names:
        raise WipeError("not an OOXML or OpenDocument package")

    if is_odf:
        dropped = {n for n in names if _odf_dropped(n, ctx.full)}
    else:
        dropped = {n for n in names if _ooxml_dropped(n, ctx.full)}
        if not ctx.full and any(n.startswith("customXml/") for n in names):
            ctx.warn("package contains customXml parts (SharePoint/bibliography data); use --full to drop them")
        if any(n.lower().endswith(".bin") and "embeddings/" in n for n in names):
            ctx.leftover("package contains binary OLE embeddings; their metadata cannot be cleaned")

    items = list(zin.infolist())
    if is_odf:  # `mimetype` must be the first, stored entry
        items.sort(key=lambda i: i.filename != "mimetype")

    for item in items:
        name = item.filename
        if name in dropped:
            continue
        zi = _zinfo(item)
        if item.is_dir():
            zout.writestr(zi, b"")
            continue
        if is_odf and name == "mimetype":
            zi.compress_type = zipfile.ZIP_STORED
        if is_odf and (name == "meta.xml" or name.endswith("/meta.xml")):
            zout.writestr(zi, _ODF_EMPTY_META)
        elif is_odf and name == "settings.xml":
            zout.writestr(zi, _ODF_EMPTY_SETTINGS)
        elif is_odf and name == "META-INF/manifest.xml":
            zout.writestr(zi, _clean_manifest(zin.read(name), ctx, dropped))
        elif not is_odf and (name == "[Content_Types].xml"):
            zout.writestr(zi, _clean_content_types(zin.read(name), dropped))
        elif not is_odf and name.endswith(".rels"):
            zout.writestr(zi, _clean_rels(name, zin.read(name), dropped))
        elif not is_odf and name == "xl/workbook.xml":
            data = re.sub(rb"<(\w+):absPath\b[^>]*/>", b"", zin.read(name))
            zout.writestr(zi, data)
        elif _is_nested_package(name, is_odf) and depth < _MAX_DEPTH:
            zout.writestr(zi, _clean_nested(zin.read(name), name, ctx, depth + 1))
        elif ctx.full and name.endswith(".xml") and _wants_anon(name, is_odf):
            zout.writestr(zi, _anonymize(name, zin.read(name), is_odf))
        elif ctx.full and any(m in "/" + name for m in _MEDIA_DIRS) and posixpath.splitext(name)[1].lower() in images.BY_EXT:
            zout.writestr(zi, _clean_media(name, zin.read(name), ctx))
        else:
            zi.file_size = item.file_size
            with zin.open(item) as src, zout.open(zi, "w", force_zip64=item.file_size >= 2**31) as dst:
                shutil.copyfileobj(src, dst)


def _wants_anon(name: str, is_odf: bool) -> bool:
    if is_odf:
        return posixpath.basename(name) in ("content.xml", "styles.xml")
    return any(rx.match(name) for rx, _ in _OOXML_ANON)


def _is_nested_package(name: str, is_odf: bool) -> bool:
    ext = posixpath.splitext(name)[1].lower()
    if is_odf:
        return False
    return "embeddings/" in name and ext in OOXML_EXT


def _clean_nested(data: bytes, name: str, ctx: Context, depth: int) -> bytes:
    try:
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            clean_package(zin, zout, ctx, depth)
        return buf.getvalue()
    except (WipeError, zipfile.BadZipFile, ET.ParseError) as e:
        ctx.warn(f"embedded package {name} left untouched ({e})")
        return data


def package(src: Path, dst: Path, ctx: Context) -> None:
    try:
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            clean_package(zin, zout, ctx)
    except zipfile.BadZipFile as e:
        raise WipeError(f"not a valid zip package: {e}") from e
    except ET.ParseError as e:
        raise WipeError(f"malformed XML in package: {e}") from e
