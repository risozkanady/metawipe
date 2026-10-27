"""PDF metadata removal on top of pypdf.

The in-memory document is stripped first (Info-like dictionaries, XMP streams, /PieceInfo,
/LastModified, pdfTeX file names, thumbnails, signatures with their visible appearance and /DSS
validation data, layer creator info), then cloned into a new writer so nothing removed can survive
as an unreferenced object; outlines, forms (with their field names), layers and links are kept. The
result is re-read and scanned before it is accepted.
"""
from __future__ import annotations

import bisect
import re
from pathlib import Path

from . import images
from .common import Context, WipeError, quiet_pypdf, real_date, text_ok

_INFO_KEYS = {"/Title", "/Author", "/Subject", "/Keywords", "/Creator", "/Producer", "/CreationDate", "/ModDate", "/Trapped"}
_INFO_IDENTITY = _INFO_KEYS - {"/Title", "/Subject", "/Keywords", "/Trapped"}
# --keep: the Info keys each category may bring back (only these, as plain validated strings)
_KEEP_INFO = {"dates": ("/CreationDate", "/ModDate"), "people": ("/Author",), "software": ("/Creator", "/Producer"),
              "descriptions": ("/Title", "/Subject", "/Keywords")}
_PDF_DATE = re.compile(r"D:(\d{4})(\d\d)?(\d\d)?(\d\d)?(\d\d)?(\d\d)?(Z(?:00'(?:00'?)?)?|[+-]\d\d(?:'(?:\d\d'?)?)?)?", re.A)
_PTEX = ("/PTEX.FileName", "/PTEX.PageNumber", "/PTEX.InfoDict")
_PRIVATE = ("/PieceInfo", "/LastModified")  # application data and edit dates on the catalog, pages, forms, images
_ANNOT_IDENTITY = ("/T", "/M", "/CreationDate", "/Subj", "/NM")  # author, dates, subject, unique name
_FIELD_IDENTITY = _ANNOT_IDENTITY[1:]  # on form fields and widgets /T is the field name (structure), the rest is still identity
_SIG_TYPES = ("/Sig", "/DocTimeStamp")
# Inline-image abbreviations, which readers also accept on image streams.
_FILTER_NAMES = {"/AHx": "/ASCIIHexDecode", "/A85": "/ASCII85Decode", "/LZW": "/LZWDecode", "/Fl": "/FlateDecode",
                 "/RL": "/RunLengthDecode", "/CCF": "/CCITTFaxDecode", "/DCT": "/DCTDecode"}
# Lossless encodings a JPEG can be wrapped in; --full decodes them and stores the cleaned JPEG bare.
_WRAPPERS = {"/ASCIIHexDecode", "/ASCII85Decode", "/LZWDecode", "/FlateDecode", "/RunLengthDecode"}
_ORPHAN_NOTE = "pages that are not in the page tree (deleted pages still referenced by bookmarks/links)"
_ATTACH_NOTE = "embedded file attachments"
# An inline image in a content stream: BI <dictionary> ID <data> EI. Both markers are found in one
# pass each (a bounded dictionary per BI, matched to the nearest ID by bisection), so the scan stays linear.
_INLINE_BI = re.compile(rb"(?<![^\s\]>)])BI\s")
_INLINE_ID = re.compile(rb"(?=\sID\s)")  # zero-width, so overlapping ID tokens are all found
_INLINE_DICT_MAX = 4096


def _is_xmp(value) -> bool:
    try:
        obj = value.get_object()
        return hasattr(obj, "keys") and (obj.get("/Type") == "/Metadata" or obj.get("/Subtype") == "/XML")
    except Exception:  # noqa: BLE001 - broken reference: treat as not XMP
        return False


def _resolve(value):
    """Follow an indirect reference (DictionaryObject.get does not); None if missing or broken."""
    try:
        return None if value is None else value.get_object()
    except Exception:  # noqa: BLE001 - broken reference
        return None


def _pop(obj, keys) -> bool:
    """Remove keys from a dictionary; True if any was there."""
    found = False
    for k in keys:
        found |= obj.pop(k, None) is not None
    return found


def _info_shaped(obj) -> bool:
    """A copy of the Info dictionary: only Info keys, an identifying one among them, plain values.

    Plain means strings/names/booleans (also behind a reference), so a legacy /Dests dictionary whose
    destination names happen to be Title/Author/... (values are arrays or dictionaries) is not
    mistaken for one.
    """
    from pypdf.generic import BooleanObject, ByteStringObject, NameObject, TextStringObject

    keys = obj.keys()
    return (bool(keys & _INFO_IDENTITY) and keys <= _INFO_KEYS
            and all(isinstance(_resolve(v), (TextStringObject, ByteStringObject, NameObject, BooleanObject)) for v in obj.values()))


def _untyped_page(obj) -> bool:
    """A page dictionary without /Type (legacy or hand-patched files): /Parent plus content or a media box.

    /Subtype rules out annotations (popups and widgets also have /Parent), /Kids rules out page tree nodes.
    """
    return ("/Type" not in obj and "/Subtype" not in obj and "/Kids" not in obj and "/Parent" in obj
            and ("/Contents" in obj or "/MediaBox" in obj))


def _field_type(obj):
    """/FT of a form field, inherited from its /Parent fields (bounded: broken files can loop)."""
    for _ in range(64):
        if not hasattr(obj, "keys"):
            return None
        if "/FT" in obj:
            return _resolve(obj.get("/FT"))
        obj = _resolve(obj.get("/Parent"))
    return None


def _is_field(obj) -> bool:
    """A form field or its widget: /T is the field name there (structure), not a comment author.

    A popup's /Parent is a markup annotation, which has neither /FT nor /Kids.
    """
    if obj.get("/Subtype") == "/Widget" or "/FT" in obj or "/Kids" in obj:
        return True
    parent = _resolve(obj.get("/Parent"))
    return hasattr(parent, "keys") and ("/FT" in parent or "/Kids" in parent or _field_type(parent) is not None)


def _is_signed_field(obj) -> bool:
    """obj holds a signature value: /V looks like a signature dictionary, or /FT /Sig (also inherited)."""
    value = _resolve(obj.get("/V"))
    if hasattr(value, "keys") and ("/ByteRange" in value or value.get("/Type") in _SIG_TYPES):
        return True
    return _field_type(obj) == "/Sig"


def _visible(annot) -> bool:
    """The annotation covers some page area (invisible signatures have an empty /Rect); True if unsure."""
    try:
        x1, y1, x2, y2 = (float(_resolve(v)) for v in _resolve(annot.get("/Rect")))
        return x1 != x2 and y1 != y2
    except Exception:  # noqa: BLE001 - missing or malformed /Rect
        return True


def _drop_signature(obj) -> bool:
    """Remove the signature value of field obj and the appearance of its widget(s); True if one was drawn."""
    from pypdf.generic import ArrayObject

    del obj["/V"]  # the rewrite invalidates the signature; do not keep the signer's identity
    widgets = [obj]
    kids = _resolve(obj.get("/Kids"))
    if isinstance(kids, ArrayObject):
        widgets += [_resolve(k) for k in kids]
    drawn = False
    for w in widgets:
        # A visible signature draws "Digitally signed by NAME", the date and the reason as page text.
        if hasattr(w, "keys") and (w.get("/Subtype") == "/Widget" or "/Rect" in w):
            if _pop(w, ("/AP",)) and _visible(w):
                drawn = True
    return drawn


def _date_ok(v) -> bool:
    m = _PDF_DATE.fullmatch(v) if isinstance(v, str) else None
    if not m:
        return False
    y, mo, d, h, mi, sec = (int(g) if g else dflt for g, dflt in zip(m.groups()[:6], (0, 1, 1, 0, 0, 0)))
    z = m.group(7)
    return real_date(y, mo, d, h, mi, sec) and (not z or z[0] == "Z" or int(z[1:3]) < 24 and int(z[4:6] or 0) < 60)


def _kept_info(reader, ctx: Context) -> dict:
    """The Info entries --keep asks for, validated; taken before _strip, which clears Info-shaped copies."""
    from pypdf.generic import TextStringObject

    want = [(c, k) for c in ctx.keep for k in _KEEP_INFO.get(c, ())]
    if not want:
        return {}
    try:
        info = _resolve(reader.trailer.get("/Info"))
        out, cats = {}, set()
        for c, k in want:
            v = _resolve(info.get(k)) if hasattr(info, "get") else None
            if isinstance(v, TextStringObject) and (_date_ok(v) if c == "dates" else text_ok(v, 2000 if c == "descriptions" else 256)):
                out[k] = str(v)
                cats.add(c)
        ctx.kept |= cats
        return out
    except Exception:  # noqa: BLE001 - damaged Info: keep nothing
        ctx.warn("PDF: the document info could not be read, nothing of it was kept")
        return {}


def _strip(root, reader, ctx: Context) -> tuple[set[str], set[str]]:
    """Remove identifying entries from everything reachable from root (in memory).

    Returns (warnings, notes): what was removed beyond plain metadata, and what had to stay.
    """
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject, NumberObject

    warnings: set[str] = set()
    notes: set[str] = set()
    page_ids = set()
    for page in reader.pages:
        if page.indirect_reference is not None:
            page_ids.add(page.indirect_reference.idnum)
            real = _resolve(page.indirect_reference)  # reader.pages holds copies; strip the dictionary itself
            if isinstance(real, DictionaryObject):
                _pop(real, _PRIVATE)
    # The catalog is found by position, not by /Type, which legacy files may lack. /Perms and
    # /Legal belong to signatures; /DSS holds signer certificates and OCSP/CRL responses (LTV).
    _pop(root, _PRIVATE + ("/Perms", "/Legal"))
    dss = _pop(root, ("/DSS",))
    # Optional-content configurations: /Creator names the authoring application.
    oc = _resolve(root.get("/OCProperties"))
    if isinstance(oc, DictionaryObject):
        configs = _resolve(oc.get("/Configs"))
        for cfg in [oc.get("/D")] + (list(configs) if isinstance(configs, ArrayObject) else []):
            cfg = _resolve(cfg)
            if isinstance(cfg, DictionaryObject):
                _pop(cfg, ("/Creator", "/CreatorInfo"))

    signed = drawn = False
    seen: set[tuple[int, int]] = set()
    stack = [(root, None)]
    while stack:
        obj, ref_id = stack.pop()
        if isinstance(obj, IndirectObject):
            key = (obj.idnum, obj.generation)
            if key in seen:
                continue
            seen.add(key)
            ref_id = obj.idnum
            obj = obj.get_object()
        if isinstance(obj, DictionaryObject):
            kind, subtype = obj.get("/Type"), obj.get("/Subtype")
            if "/Metadata" in obj and _is_xmp(obj["/Metadata"]):
                del obj["/Metadata"]
            page = kind == "/Page" or _untyped_page(obj)
            if page or kind == "/Catalog" or subtype in ("/Form", "/Image"):
                _pop(obj, _PRIVATE)
            _pop(obj, _PTEX + ("/CreatorInfo",))
            if "thumbnails" not in ctx.keep:
                _pop(obj, ("/Thumb",))
            elif "/Thumb" in obj:
                th, sub, th_notes = _resolve(obj["/Thumb"]), Context(warn=lambda _m: None), set()
                if not ctx.full and hasattr(th, "get") and th.get("/Subtype") == "/Image":
                    _clean_image(th, sub, th_notes)  # its own Exif/IPTC were not asked for; --full reaches it by the walk
                if sub.residue or th_notes:
                    del obj["/Thumb"]
                    ctx.cannot_keep("thumbnails", "the thumbnail picture could not be cleaned")
                else:
                    ctx.kept.add("thumbnails")
            if kind is None and _info_shaped(obj):
                for k in list(obj.keys()):  # an Info-shaped dictionary (e.g. pdfTeX's /PTEX.InfoDict)
                    del obj[k]
            if "/V" in obj and _is_signed_field(obj):
                signed = True
                drawn |= _drop_signature(obj)
            if page and ref_id is not None and ref_id not in page_ids:
                notes.add(_ORPHAN_NOTE)
            # A file specification with /EF embeds a file, whether it is reached through the
            # /EmbeddedFiles name tree, an associated-files (/AF) array, an annotation or RichMedia.
            if ("/EF" in obj or "/EmbeddedFiles" in obj or kind == "/EmbeddedFile"
                    or subtype in ("/FileAttachment", "/RichMedia")):
                notes.add(_ATTACH_NOTE)
            if ctx.full:
                _full_checks(obj, ctx, notes, page)
            stack.extend((v, None) for v in obj.values())
        elif isinstance(obj, ArrayObject):
            stack.extend((v, None) for v in obj)

    if signed:
        acro = _resolve(root.get("/AcroForm"))
        flags = _resolve(acro.get("/SigFlags")) if isinstance(acro, DictionaryObject) else None
        if isinstance(flags, int) and flags & 2:  # AppendOnly no longer applies: no signature is left
            acro[NameObject("/SigFlags")] = NumberObject(flags & ~2)
        removed = [what for what, flag in (("its visible appearance", drawn), ("the signer certificates and OCSP/CRL data (/DSS)", dss)) if flag]
        warnings.add("a digital signature was removed (the rewrite would invalidate it)"
                     + ("; also removed: " + " and ".join(removed) if removed else ""))
    elif dss:
        warnings.add("signature validation data was removed (signer certificates and OCSP/CRL data in /DSS)")
    return warnings, notes


def _filter_chain(obj) -> tuple[list, list, list]:
    """Return the stream's filter names as written, with abbreviations expanded, and their /DecodeParms."""
    f = _resolve(obj.get("/Filter"))
    raw = [str(_resolve(x)) for x in f] if isinstance(f, list) else ([] if f is None else [str(f)])
    names = [_FILTER_NAMES.get(n, n) for n in raw]
    p = _resolve(obj.get("/DecodeParms"))
    parms = [_resolve(x) for x in p] if isinstance(p, list) else [p]  # a single dictionary goes with the first filter
    parms = (parms + [None] * len(names))[:len(names)]
    return raw, names, parms


def _unwrap(data: bytes, wrappers: list, parms: list) -> bytes:
    """Decode lossless wrapper filters with pypdf (its decoding limits apply)."""
    from pypdf.filters import decode_stream_data
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, StreamObject

    tmp = StreamObject()
    tmp._data = data
    tmp[NameObject("/Filter")] = ArrayObject(NameObject(n) for n in wrappers)
    tmp[NameObject("/DecodeParms")] = ArrayObject(p if isinstance(p, DictionaryObject) else DictionaryObject() for p in parms)
    out = decode_stream_data(tmp)
    return out.encode("latin-1") if isinstance(out, str) else out


def _clean_image(obj, ctx: Context, notes: set[str]) -> None:
    """--full: clean an embedded JPEG (also behind lossless wrapper filters); flag what cannot be cleaned."""
    from pypdf.generic import DictionaryObject, NameObject

    raw, names, parms = _filter_chain(obj)
    if "/JPXDecode" in names:
        notes.add("JPEG 2000 images (their metadata boxes are not cleaned)")
        return
    if "/DCTDecode" not in names or not hasattr(obj, "_data"):
        return
    wrappers = names[:-1]
    if names[-1] != "/DCTDecode" or not set(wrappers) <= _WRAPPERS:
        notes.add("JPEG images behind a filter chain")
        return
    try:
        cleaned = images.clean_jpeg(_unwrap(obj._data, wrappers, parms[:-1]) if wrappers else obj._data)
    except WipeError as e:
        ctx.leftover(f"embedded JPEG could not be cleaned ({e})")
        return
    except Exception as e:  # noqa: BLE001 - corrupt wrapper data or a pypdf decoding limit: keep it, report it
        ctx.leftover(f"embedded JPEG could not be cleaned (cannot decode {' '.join(wrappers)}: {e})")
        return
    obj._data = cleaned
    if hasattr(obj, "decoded_self"):
        obj.decoded_self = None  # get_data() may have cached the old bytes, and cloning copies that cache
    if wrappers or raw != names:
        # Store the JPEG bare: the wrappers and their parameters go, /ColorTransform of DCT stays.
        obj[NameObject("/Filter")] = NameObject("/DCTDecode")
        if isinstance(parms[-1], DictionaryObject) and parms[-1]:
            obj[NameObject("/DecodeParms")] = parms[-1]
        else:
            obj.pop("/DecodeParms", None)


def _content(streams) -> bytes:
    """Decoded content of the given streams, joined; undecodable ones count as empty."""
    from pypdf.filters import decode_stream_data

    out = []
    for s in streams:
        try:
            data = decode_stream_data(s) if "/Filter" in s else s._data  # decode_stream_data rejects unfiltered streams
            out.append(data.encode("latin-1") if isinstance(data, str) else data)
        except Exception:  # noqa: BLE001 - broken or over-limit stream: nothing to report from it
            pass
    return b"\n".join(out)


def _inline_jpeg(obj, page: bool) -> bool:
    """A page or form/pattern content stream draws an inline JPEG (BI ... /F /DCT ... ID), which is not cleaned."""
    from pypdf.generic import ArrayObject

    if page:
        contents = _resolve(obj.get("/Contents"))
        streams = [_resolve(c) for c in contents] if isinstance(contents, ArrayObject) else [contents]
    elif obj.get("/Subtype") == "/Form" or obj.get("/PatternType") == 1:
        streams = [obj]
    else:
        return False
    data = _content(s for s in streams if hasattr(s, "_data"))
    if b"/DCT" not in data:
        return False
    ids = [m.start() for m in _INLINE_ID.finditer(data)]
    pos = 0
    for m in _INLINE_BI.finditer(data):
        if m.start() < pos:  # inside the previous image's dictionary or data
            continue
        start = m.end()  # first byte of the image dictionary
        i = bisect.bisect_left(ids, start)  # the whitespace before ID cannot be the one after BI
        if i == len(ids) or ids[i] - start > _INLINE_DICT_MAX:
            continue
        if b"/DCT" in data[start:ids[i]]:
            return True
        pos = ids[i] + 4
    return False


def _pop_identity(obj, keys, ctx: Context) -> None:
    """Remove keys, except the dates (/M, /CreationDate) and author (/T) of annotations the user asked to keep."""
    for k in keys:
        c = "dates" if k in ("/M", "/CreationDate") else "people" if k == "/T" else None
        if c in ctx.keep:
            if k in obj:
                ctx.kept.add(c)
        else:
            obj.pop(k, None)


def _full_checks(obj, ctx: Context, notes: set[str], page: bool) -> None:
    """--full: annotation authors/dates, EXIF inside embedded JPEGs; flag what cannot be cleaned."""
    # Form fields and their widgets keep /T: it is the field name, which forms are addressed by.
    if obj.get("/Type") == "/Annot" or ("/Subtype" in obj and "/Rect" in obj):
        _pop_identity(obj, _FIELD_IDENTITY if _is_field(obj) else _ANNOT_IDENTITY, ctx)
    elif "/FT" in obj or ("/Kids" in obj and "/T" in obj):
        _pop_identity(obj, _FIELD_IDENTITY, ctx)  # a field (or an intermediate field node) that is not a widget itself
    if "/XFA" in obj:
        notes.add("XFA form data")
    if obj.get("/Subtype") == "/Image":
        _clean_image(obj, ctx, notes)
    elif _inline_jpeg(obj, page):
        notes.add("inline JPEG images in page content")


@quiet_pypdf()
def clean_pdf(src: Path, dst: Path, ctx: Context) -> None:
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.errors import DependencyError, PyPdfError
        from pypdf.generic import DictionaryObject
    except ImportError as e:
        raise WipeError("pypdf is required for PDF files: pip install -r requirements.txt") from e

    try:
        reader = PdfReader(src)
        if reader.is_encrypted:
            if not (ctx.password and reader.decrypt(ctx.password)) and not reader.decrypt(""):
                raise WipeError("PDF is password protected; pass --password")
            ctx.warn("encryption and permission restrictions are removed from the output")
        # Strip the in-memory copy *before* cloning: pypdf keeps every object it cloned, so
        # removing a key afterwards would leave the XMP stream behind as an unreferenced object.
        root = _resolve(reader.trailer.get("/Root"))
        if not isinstance(root, DictionaryObject) or hasattr(root, "_data"):  # stripping the catalog is the point: do not write a file without it
            raise WipeError("cannot process PDF: the document catalog (/Root) is missing or not a dictionary")
        kept = _kept_info(reader, ctx)
        warnings, notes = _strip(root, reader, ctx)
        reader.trailer.pop("/ID", None)  # the permanent file identifier links the output to the original
        writer = PdfWriter(clone_from=reader)
        writer.metadata = None
        writer._ID = None
        if kept:
            writer.add_metadata(kept)
        with open(dst, "wb") as fh:
            writer.write(fh)
        _verify(dst, kept.keys())
        for msg in sorted(warnings):
            ctx.warn(f"PDF: {msg}")
        for note in sorted(notes):
            ctx.leftover(f"PDF contains {note}; not cleaned")  # without --full only a warning
    except DependencyError as e:
        raise WipeError(f"{e}; install it with: pip install cryptography") from e
    except PyPdfError as e:
        raise WipeError(f"cannot process PDF: {e}") from e
    except RecursionError as e:
        raise WipeError("PDF structure is nested too deeply to process") from e
    except NotImplementedError as e:
        raise WipeError(f"unsupported PDF feature: {e}") from e
    except (LookupError, AttributeError, TypeError, ValueError) as e:  # noqa: BLE001 - pypdf on damaged structures (missing /Size, bad references)
        raise WipeError(f"cannot process PDF: damaged structure ({type(e).__name__}: {e})") from e


def _verify(path: Path, allowed=()) -> None:
    from pypdf import PdfReader
    from pypdf.generic import IndirectObject

    reader = PdfReader(path)
    info = _resolve(reader.trailer.get("/Info"))
    if (info is not None and not (hasattr(info, "keys") and info.keys() <= set(allowed))) or "/Metadata" in reader.trailer["/Root"]:
        raise WipeError("verification failed: document info/XMP still present")
    leftovers = 0
    for gen, ids in reader.xref.items():
        for idnum in ids:
            obj = IndirectObject(idnum, gen, reader).get_object()
            if hasattr(obj, "keys"):
                leftovers += obj.get("/Type") == "/Metadata"
                leftovers += any(k in obj for k in _PTEX)
    if leftovers:
        raise WipeError(f"verification failed: {leftovers} metadata objects survived")
