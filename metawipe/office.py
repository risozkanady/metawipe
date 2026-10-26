"""Metadata removal for OOXML (docx/xlsx/pptx and variants) and OpenDocument packages.

Both are zip files. They are rebuilt entry by entry: document properties, thumbnails, signatures,
printer settings and the other known identity-bearing parts are dropped, relationships, content-type
entries and r:id references that pointed at them are pruned (relationship files with a real XML parser,
the owner parts with a tag-level rewrite), nested Office packages are cleaned recursively and the zip
container itself is normalised (fixed timestamps, no extra fields). With --full, comment/revision
authors and dates are anonymised, pictures inside the package are cleaned, customXml stores and the
sensitivity label are dropped as well and media that cannot be cleaned are reported as left over.

Part names are compared the OPC way (percent-decoded, case-insensitive, backslashes as slashes).
XML parts are edited as text (decoded by BOM/declaration and re-encoded the same way) so namespace
prefixes and declarations stay exactly as the producer wrote them; every edited part is checked to
still be well-formed.
"""
from __future__ import annotations

import codecs
import io
import posixpath
import re
import shutil
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote
from xml.parsers import expat

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
_LABEL_REL_SUFFIX = "/classificationlabels"  # sensitivity label (tenant GUID); dropped by --full only
# External relationships that point at a data source on the author's disk (profile name in the path).
_MAIL_MERGE_SUFFIXES = ("/mailMergeSource", "/mailMergeHeaderSource", "/recipientData")
_ODF_EMPTY_META = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<office:document-meta xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'office:version="1.3"><office:meta/></office:document-meta>'
).encode()
_EPOCH = (1980, 1, 1, 0, 0, 0)
_MAX_DEPTH = 3
_FIXED_DATE = "2000-01-01T00:00:00Z"
# Directories (as path components of the normalised name) that hold embedded media.
_MEDIA_DIRS = ("/media/", "/pictures/", "/embeddings/")
# Media that carries its own metadata (GPS, artist, comments) and that --full cannot clean here.
_UNCLEANABLE_MEDIA = {
    ".mp4", ".mov", ".m4v", ".m4a", ".3gp", ".avi", ".wmv", ".wma", ".asf", ".mpg", ".mpeg", ".webm", ".mkv",
    ".mp3", ".wav", ".aac", ".flac", ".ogg", ".oga", ".ogv", ".opus", ".gif", ".svg", ".svgz", ".pdf",
    ".heic", ".heif", ".avif", ".jp2", ".j2k", ".jxl",
}

# --- namespaces ---------------------------------------------------------------------------------
_NS = {
    "w": ("http://schemas.openxmlformats.org/wordprocessingml/2006/main", "http://purl.oclc.org/ooxml/wordprocessingml/main"),
    "r": ("http://schemas.openxmlformats.org/officeDocument/2006/relationships",
          "http://purl.oclc.org/ooxml/officeDocument/relationships"),
    "dc": ("http://purl.org/dc/elements/1.1/",),
    "text": ("urn:oasis:names:tc:opendocument:xmlns:text:1.0",),
    "meta": ("urn:oasis:names:tc:opendocument:xmlns:meta:1.0",),
}
_XMLNS_DECL = re.compile(r"""\bxmlns:([\w.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
# A start tag: attribute values are skipped as units so a '>' inside quotes does not end it, and nothing
# can run past the next '<', which keeps every scan linear on hostile input.
_TAG_BODY = r"""(?:[^<>"']|"[^"<]*"|'[^'<]*')*"""
_TAG = re.compile(r"<([\w.-]+(?::[\w.-]+)?)(" + _TAG_BODY + r")>")
_TAG_END = re.compile(_TAG_BODY + ">")
_ATTR = re.compile(r"""(?<!\s)(\s+)([\w.-]+(?::[\w.-]+)?)(\s*=\s*)("[^"]*"|'[^']*')""")


class _Doc:
    """An XML part as text, with the namespace prefixes it declares (found lazily)."""

    def __init__(self, text: str):
        self.text = text
        self._decl = None
        self._cache: dict = {}

    def prefixes(self, key: str) -> set:
        """Prefixes bound to the namespace group `key` (declared ones plus the conventional one)."""
        if key not in self._cache:
            if self._decl is None:
                self._decl = [(m.group(1), m.group(2) if m.group(2) is not None else m.group(3))
                              for m in _XMLNS_DECL.finditer(self.text)]
            self._cache[key] = {p for p, uri in self._decl if uri in _NS[key]} | {key}
        return self._cache[key]

    def matches(self, key: str, prefix: str) -> bool:
        """Does an element/attribute prefix belong to `key`? '*' is anything, '' only unprefixed names."""
        if key == "*":
            return True
        if key == "":
            return prefix == ""
        return prefix != "" and prefix in self.prefixes(key)


class _Rules:
    """What to rewrite in one XML part.

    attrs: (elements | None, namespace key, local name, replacement | None to delete the attribute)
    texts: (namespace key, local-name regex, hint substring, replacement) - text-only elements
    drops: (namespace key, local name) - elements removed with their content
    """

    def __init__(self, attrs=(), texts=(), drops=()):
        self.attrs, self.texts, self.drops = list(attrs), list(texts), list(drops)

    def __bool__(self):
        return bool(self.attrs or self.texts or self.drops)

    def extend(self, other: "_Rules") -> None:
        self.attrs += other.attrs
        self.texts += other.texts
        self.drops += other.drops


def _attr(local, repl, ns="", elements=None):
    return (frozenset(elements) if elements else None, ns, local, repl)


# Person lists name people (and their accounts); they identify rather than hold content.
_PEOPLE_RULES = [
    (re.compile(r"^(xl/persons/.*|ppt/authors)\.xml$", re.I), _Rules(attrs=[
        _attr("displayName", "Author"), _attr("name", "Author"), _attr("initials", "A"),
        _attr("userId", "author"), _attr("providerId", "None"),
    ])),
    (re.compile(r"^ppt/commentAuthors\.xml$", re.I), _Rules(attrs=[_attr("name", "Author"), _attr("initials", "A")])),
    (re.compile(r"^xl/workbook\.xml$", re.I), _Rules(
        attrs=[_attr("userName", None, elements=["fileSharing"])], drops=[("*", "absPath")])),
]
# --full: author names and timestamps on comments and tracked changes.
_OOXML_ANON = [
    (re.compile(r"^word/.*\.xml$", re.I), _Rules(
        attrs=[_attr("author", "Author", "w"), _attr("initials", "A", "w"), _attr("date", None, "w"),
               _attr("dateUtc", None, "*")],  # w16du:dateUtc on runs/comments, w16cex:dateUtc in commentsExtensible
        drops=[("w", "docVars")])),
    (re.compile(r"^xl/comments.*\.xml$", re.I), _Rules(texts=[("*", "author", "<author", "Author")])),
    (re.compile(r"^xl/threadedComments/.*\.xml$", re.I), _Rules(attrs=[_attr("dT", "2000-01-01T00:00:00.00")])),
    (re.compile(r"^ppt/comments/.*\.xml$", re.I), _Rules(attrs=[
        _attr("dt", "2000-01-01T00:00:00.000"), _attr("created", "2000-01-01T00:00:00.000")])),
    # shared-workbook revision logs: who changed what (the changed values are content and stay)
    (re.compile(r"^xl/revisions/.*\.xml$", re.I), _Rules(attrs=[
        _attr("userName", "Author", elements=["header"]), _attr("author", "Author", elements=["rcmt"]),
        _attr("name", "Author", elements=["userInfo"]),
        _attr("dateTime", "2000-01-01T00:00:00", elements=["header", "userInfo"]),
    ])),
]
_ODF_ANON_RULES = _Rules(texts=[
    ("dc", "creator", "creator", "Author"),
    ("dc", "date", "date", _FIXED_DATE),
    ("meta", "date-string", "date-string", ""),
    ("*", r"author-name|author-initials|initial-creator|creator-initials|initial-creator-initials|sender-[\w-]+",
     "-", ""),
    ("text", "creator", "creator", ""),
])
_ODF_PRINTER_ITEM = re.compile(
    r"(<(?P<p>(?:[\w.-]+:)?)config-item(?![\w.-])(?P<a>" + _TAG_BODY + r")>)[^<]*(</(?P=p)config-item\s*>)")
_ODF_PRINTER_NAME = re.compile(r"""\bname\s*=\s*["'](?:PrinterName|PrinterSetup)["']""")


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




def _norm(name: str) -> str:
    """OPC part-name key: percent-decoded, backslashes as slashes, case-folded, no leading slash or dot segments."""
    n = unquote(name.replace("\\", "/")).replace("\\", "/")
    key = posixpath.normpath("/" + n).lstrip("/").casefold()
    return key + "/" if n.endswith("/") and key else key


def _resolve(base: str, target: str) -> str:
    target = target.replace("\\", "/")
    if target.startswith("/"):
        return _norm(target)
    return _norm(posixpath.join(base, target))


def _rels_owner(nn: str):
    """Normalised name of the part a .rels file belongs to ('' for the package root), None if not a rels path."""
    folder, base = posixpath.split(nn)
    if not base.endswith(".rels"):
        return None
    if posixpath.basename(folder) != "_rels":
        return None
    return posixpath.join(posixpath.dirname(folder), base[:-5]) if base != ".rels" else ""


def _rels_base(nn: str) -> str:
    return posixpath.dirname(posixpath.dirname(nn))


def _metadata_targets(zin: zipfile.ZipFile, by_norm: dict, full: bool) -> set[str]:
    """Parts the package root marks as properties/thumbnail/signature origin, wherever they are stored."""
    root_name = by_norm.get("_rels/.rels")
    if root_name is None:
        return set()
    suffixes = _METADATA_REL_SUFFIXES + ((_LABEL_REL_SUFFIX,) if full else ())
    root = ET.fromstring(zin.read(root_name))
    return {
        _resolve("", rel.get("Target", ""))
        for rel in root
        if rel.get("TargetMode") != "External" and rel.get("Type", "").endswith(suffixes)
    }


def _ooxml_dropped(nns: list[str], by_norm: dict, zin: zipfile.ZipFile, full: bool) -> set[str]:
    dropped = _metadata_targets(zin, by_norm, full)
    for nn in nns:
        if (
            nn.startswith(("docprops/", "package/services/metadata/", "_xmlsignatures/"))
            or nn == "word/people.xml"
            or "/printersettings/" in nn
            or (full and nn.startswith(("customxml/", "docmetadata/")))
        ):
            dropped.add(nn)
    return dropped


def _odf_is_dropped(nn: str, full: bool) -> bool:
    return (
        nn.startswith("thumbnails/")
        or nn in ("meta-inf/documentsignatures.xml", "meta-inf/macrosignatures.xml")
        or (full and nn.startswith("configurations2/"))
    )


# --- XML text editing ---------------------------------------------------------------------------

def _decode(data: bytes):
    """(text, encode function) for an XML part, honouring BOMs, BOM-less UTF-16/32 and the declaration."""
    for bom, codec in ((codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"), (codecs.BOM_UTF8, "utf-8"),
                       (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")):
        if data.startswith(bom):
            return data[len(bom):].decode(codec), lambda t, b=bom, c=codec: b + t.encode(c)
    for start, codec in ((b"<\x00\x00\x00", "utf-32-le"), (b"\x00\x00\x00<", "utf-32-be"),
                         (b"<\x00", "utf-16-le"), (b"\x00<", "utf-16-be")):
        if data.startswith(start):
            return data.decode(codec), lambda t, c=codec: t.encode(c)
    declared = re.match(rb"""\s*<\?xml[^>]*?encoding\s*=\s*["']([\w.:-]+)["']""", data[:200])
    codec = declared.group(1).decode("ascii").lower() if declared else "utf-8"
    if codec in ("utf-8", "utf8", "us-ascii", "ascii"):
        codec = "utf-8"
    codecs.lookup(codec)  # unknown encoding: LookupError, the caller reports the part as left over
    if any(w in codec for w in ("utf-16", "utf16", "utf-32", "utf32", "ucs")):
        raise LookupError(f"{codec} part without a byte order mark")
    try:
        return data.decode(codec), lambda t, c=codec: t.encode(c)
    except UnicodeDecodeError:
        pass
    return data.decode("latin-1"), lambda t: t.encode("latin-1")  # structure is ASCII; bytes round-trip


def _well_formed(data: bytes) -> bool:
    try:
        expat.ParserCreate().Parse(data, True)  # namespace-unaware: only checks the markup structure
    except expat.ExpatError:
        return False
    return True


def _drop_elements(doc: _Doc, text: str, key: str, local: str) -> str:
    """Remove every <local> element (with content). One forward scan: an opener without a closer ends the search."""
    opener = re.compile(r"<(?P<p>(?:[\w.-]+:)?)" + re.escape(local) + r"(?![\w.-])")
    out, pos = [], 0
    dead = set()  # prefixes for which no balanced closing tag exists after this point
    for m in opener.finditer(text):
        if m.start() < pos or not doc.matches(key, m.group("p")[:-1]):
            continue
        end = _TAG_END.match(text, m.end())
        if end is None:
            continue
        stop = end.end()
        if text[stop - 2:stop] != "/>":
            p = m.group("p")
            if p in dead:
                continue
            stop = _closing_end(text, stop, p + local)
            if stop is None:
                dead.add(p)
                continue
        out.append(text[pos:m.start()])
        pos = stop
    out.append(text[pos:])
    return "".join(out)


def _closing_end(text: str, pos: int, qname: str):
    """Index just after the end tag that balances an open <qname> whose start tag ended at `pos`, or None."""
    depth = 1
    for t in re.compile(r"<(/?)" + re.escape(qname) + r"(?![\w.-])").finditer(text, pos):
        end = _TAG_END.match(text, t.end())
        if end is None:
            continue
        if t.group(1):
            depth -= 1
            if depth == 0:
                return end.end()
        elif text[end.end() - 2:end.end()] != "/>":
            depth += 1
    return None


def _sub_text_elements(doc: _Doc, text: str, rule) -> str:
    key, locals_re, hint, repl = rule
    pattern = re.compile(
        r"(<(?P<p>(?:[\w.-]+:)?)(?P<l>" + locals_re + r")(?![\w.-])(?:[^<>\"'/]|\"[^\"<]*\"|'[^'<]*')*>)[^<]*(</(?P=p)(?P=l)\s*>)")

    def sub(m):
        if not doc.matches(key, m.group("p")[:-1]):
            return m.group(0)
        return m.group(1) + repl + m.group(4)

    return pattern.sub(sub, text) if hint in text else text


def _edit_tags(doc: _Doc, text: str, attrs: list, rel_ids: set) -> str:
    """Attribute rewrites and removal of r:id references to dropped relationships, one pass over the start tags."""
    hints = {a[2] for a in attrs} | set(rel_ids)
    if not any(h in text for h in hints):
        return text
    by_local: dict = {}
    for rule in attrs:
        by_local.setdefault(rule[2], []).append(rule)

    def tag(m):
        body = m.group(2)
        if not any(h in body for h in hints):
            return m.group(0)
        element = m.group(1).rpartition(":")[2]
        pieces, pos, changed, kill = [], 0, False, False
        for a in _ATTR.finditer(body):
            space, qname, eq, value = a.groups()
            prefix, _, local = qname.rpartition(":")
            action = "keep"
            if rel_ids and prefix and value[1:-1] in rel_ids and doc.matches("r", prefix):
                action = "drop"
                kill = element == "printerSettings"
            else:
                for elements, key, _, repl in by_local.get(local, ()):
                    if (elements is None or element in elements) and doc.matches(key, prefix):
                        action = repl
                        break
            if action == "keep":
                continue
            pieces.append(body[pos:a.start()])
            if action not in ("drop", None):
                pieces.append(space + qname + eq + value[0] + action + value[0])
            pos, changed = a.end(), True
        if not changed:
            return m.group(0)
        pieces.append(body[pos:])
        new_body = "".join(pieces)
        if kill and new_body.rstrip().endswith("/"):
            return ""  # <w:printerSettings r:id=.../> is meaningless (and invalid) without its part
        return "<" + m.group(1) + new_body + ">"

    if attrs or not rel_ids:
        return _TAG.sub(tag, text)
    # only dangling r:id references to remove: visit just the tags that carry one (big worksheets stay fast)
    pieces, pos = [], 0
    for hit in re.compile("|".join(re.escape(i) for i in sorted(rel_ids))).finditer(text):
        start = text.rfind("<", 0, hit.start())
        m = _TAG.match(text, start) if start >= pos else None
        if m is None or m.end() <= hit.start():
            continue
        pieces += [text[pos:start], tag(m)]
        pos = m.end()
    pieces.append(text[pos:])
    return "".join(pieces)


def _apply_rules(text: str, rules: _Rules, rel_ids: set) -> str:
    doc = _Doc(text)
    for key, local in rules.drops:
        if local in text:
            text = doc.text = _drop_elements(doc, text, key, local)
    for rule in rules.texts:
        text = doc.text = _sub_text_elements(doc, text, rule)
    return _edit_tags(doc, text, rules.attrs, rel_ids)


def _odf_settings(text: str) -> str:
    def sub(m):
        return m.group(0) if not _ODF_PRINTER_NAME.search(m.group("a")) else m.group(1) + m.group(4)

    return _ODF_PRINTER_ITEM.sub(sub, text) if "config-item" in text else text


def _rewrite_xml(name: str, data: bytes, ctx: Context, fn) -> bytes:
    """Apply a text-level edit to an XML part; untouched bytes are returned as they were."""
    try:
        text, encode = _decode(data)
    except (UnicodeDecodeError, LookupError):
        ctx.leftover(f"{name} could not be decoded; its identifying data was not removed")
        return data
    new = fn(text)
    if new == text:
        return data
    out = encode(new)
    if not _well_formed(out) and _well_formed(data):
        raise WipeError(f"internal error: rewriting {name} would break the XML; the file was left untouched")
    return out


def _ooxml_rules(nn: str, full: bool) -> _Rules:
    rules = _Rules()
    for name_re, rs in _PEOPLE_RULES + (_OOXML_ANON if full else []):
        if name_re.match(nn):
            rules.extend(rs)
    return rules


# --- relationships, content types, manifest -----------------------------------------------------

def _dropped_rel_ids(nn: str, data: bytes, dropped: set[str]) -> set[str]:
    base = _rels_base(nn)
    root = ET.fromstring(data)
    return {
        rel.get("Id", "") for rel in root
        if rel.get("TargetMode") != "External" and _resolve(base, rel.get("Target", "")) in dropped
    }


def _clean_rels(name: str, nn: str, data: bytes, dropped: set[str], ctx: Context) -> bytes:
    base = _rels_base(nn)
    ET.register_namespace("", _REL_NS)
    root = ET.fromstring(data)
    changed = False
    for rel in list(root):
        target = rel.get("Target", "")
        if rel.get("TargetMode") == "External":
            kind = rel.get("Type", "")
            if kind.endswith("/attachedTemplate"):
                rel.set("Target", "Normal.dotm")  # keeps the r:id valid, drops the original path
                changed = True
            elif kind.endswith(_MAIL_MERGE_SUFFIXES):
                ctx.leftover(f"{name}: mail-merge data source path ({kind.rsplit('/', 1)[-1]}) is kept; it may contain a user profile name")
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
        if el.tag == f"{{{_CT_NS}}}Override" and _norm(el.get("PartName", "")) in dropped:
            root.remove(el)
            changed = True
    if not changed:
        return data
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def _clean_manifest(data: bytes, full: bool) -> bytes:
    ET.register_namespace("manifest", _MANIFEST_NS)
    root = ET.fromstring(data)
    entry = f"{{{_MANIFEST_NS}}}file-entry"
    path_attr = f"{{{_MANIFEST_NS}}}full-path"
    changed = False
    for el in list(root):
        if el.tag == entry and _odf_is_dropped(_norm(el.get(path_attr, "")), full):
            root.remove(el)
            changed = True
    if not changed:
        return data
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def _check_odf_not_encrypted(zin: zipfile.ZipFile, by_norm: dict) -> None:
    name = by_norm.get("meta-inf/manifest.xml")
    if name is None:
        return
    root = ET.fromstring(zin.read(name))
    if any(el.tag.rpartition("}")[2] == "encryption-data" for el in root.iter()):
        raise WipeError("encrypted OpenDocument files are not supported")


# --- the package --------------------------------------------------------------------------------

def clean_package(zin: zipfile.ZipFile, zout: zipfile.ZipFile, ctx: Context, depth: int = 0) -> None:
    items = list(zin.infolist())
    entries = [(item, _norm(item.filename)) for item in items]
    nns = [nn for _, nn in entries]
    by_norm: dict = {}
    for item, nn in entries:
        by_norm.setdefault(nn, item.filename)
    is_odf = "mimetype" in by_norm and zin.read(by_norm["mimetype"]).startswith(b"application/vnd.oasis.opendocument")
    if not is_odf and "[content_types].xml" not in by_norm:
        raise WipeError("not an OOXML or OpenDocument package")

    rel_ids: dict = {}  # owner part -> relationship ids that point at dropped parts
    if is_odf:
        _check_odf_not_encrypted(zin, by_norm)
        dropped = {nn for nn in nns if _odf_is_dropped(nn, ctx.full)}
    else:
        dropped = _ooxml_dropped(nns, by_norm, zin, ctx.full)
        for item, nn in entries:
            owner = _rels_owner(nn)
            if owner is not None and owner not in dropped:
                ids = _dropped_rel_ids(nn, zin.read(item), dropped)
                if ids:
                    rel_ids[owner] = ids
        if not ctx.full and any(nn.startswith("customxml/") for nn in nns):
            ctx.warn("package contains customXml parts (SharePoint/bibliography data); use --full to drop them")
        if not ctx.full and any(nn.startswith("docmetadata/") for nn in nns):
            ctx.warn("package carries a sensitivity label (docMetadata/LabelInfo.xml, organisation id); use --full to drop it")
    if not is_odf and "word/settings.xml" in by_norm and re.search(
            rb"<(?:[\w.-]+:)?connectString(?![\w.-])", zin.read(by_norm["word/settings.xml"])):
        ctx.leftover("word/settings.xml: mail-merge connection string is kept; it may contain a user profile name")
    if any(nn.startswith(("_xmlsignatures/", "meta-inf/documentsignatures")) for nn in nns):
        ctx.warn("digital signatures were removed (cleaning invalidates them)")
    if any(posixpath.splitext(nn)[1] in (".bin", ".doc", ".xls", ".ppt") and "embeddings/" in nn for nn in nns):
        ctx.leftover("package contains OLE embeddings; their metadata cannot be cleaned")

    if is_odf:  # `mimetype` must be the first, stored entry
        entries.sort(key=lambda e: e[1] != "mimetype")

    for item, nn in entries:
        name = item.filename
        if nn in dropped or (not is_odf and _rels_owner(nn) in dropped):  # a dropped part's own rels go too
            continue
        zi = _zinfo(item)
        base = posixpath.basename(nn)
        ext = posixpath.splitext(nn)[1]
        if item.is_dir():
            zout.writestr(zi, b"")
        elif is_odf and nn == "mimetype":
            zi.compress_type = zipfile.ZIP_STORED
            zout.writestr(zi, zin.read(name))
        elif is_odf and base == "meta.xml":
            zout.writestr(zi, _ODF_EMPTY_META)
        elif is_odf and base == "settings.xml":
            zout.writestr(zi, _rewrite_xml(name, zin.read(name), ctx, _odf_settings))
        elif is_odf and nn == "meta-inf/manifest.xml":
            zout.writestr(zi, _clean_manifest(zin.read(name), ctx.full))
        elif is_odf and ctx.full and base in ("content.xml", "styles.xml"):
            zout.writestr(zi, _rewrite_xml(name, zin.read(name), ctx, lambda t: _apply_rules(t, _ODF_ANON_RULES, set())))
        elif not is_odf and nn == "[content_types].xml":
            zout.writestr(zi, _clean_content_types(zin.read(name), dropped))
        elif not is_odf and nn.endswith(".rels"):
            zout.writestr(zi, _clean_rels(name, nn, zin.read(name), dropped, ctx))
        elif not is_odf and nn.endswith(".xml") and (_ooxml_rules(nn, ctx.full) or nn in rel_ids):
            rules, ids = _ooxml_rules(nn, ctx.full), rel_ids.get(nn, set())
            zout.writestr(zi, _rewrite_xml(name, zin.read(name), ctx, lambda t: _apply_rules(t, rules, ids)))
        elif ext in OOXML_EXT | ODF_EXT:
            if depth >= _MAX_DEPTH:
                ctx.leftover(f"embedded package {name} is nested too deeply to clean")
                _copy(zin, zout, item, zi)
            else:
                zout.writestr(zi, _clean_nested(zin.read(name), name, ctx, depth + 1))
        elif ctx.full and any(m in "/" + nn for m in _MEDIA_DIRS) and ext in images.BY_EXT:
            zout.writestr(zi, _clean_media(name, zin.read(name), ctx))
        else:
            if ctx.full and ext in _UNCLEANABLE_MEDIA and any(m in "/" + nn for m in _MEDIA_DIRS):
                ctx.leftover(f"embedded media {name} may carry metadata (GPS, author, comments) that cannot be cleaned")
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
        if fn is images.clean_tiff:
            return fn(data, lambda m: ctx.leftover(f"embedded picture {name}: {m}"))
        return fn(data)
    except WipeError as e:
        ctx.leftover(f"embedded picture {name} could not be cleaned ({e})")
        return data


def _clean_nested(data: bytes, name: str, ctx: Context, depth: int) -> bytes:
    try:
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            clean_package(zin, zout, ctx, depth)
        return buf.getvalue()
    except (WipeError, zipfile.BadZipFile, ET.ParseError, KeyError, RuntimeError, NotImplementedError) as e:
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
