"""PDF metadata removal on top of pypdf.

The in-memory document is stripped first (Info-like dictionaries, XMP streams, /PieceInfo,
/LastModified, pdfTeX file names, thumbnails, signature identities, layer creator info), then cloned
into a new writer so nothing removed can survive as an unreferenced object; outlines, forms, layers
and links are kept. The result is re-read and scanned before it is accepted.
"""
from __future__ import annotations

from pathlib import Path

from . import images
from .common import Context, WipeError

_INFO_KEYS = {"/Title", "/Author", "/Subject", "/Keywords", "/Creator", "/Producer", "/CreationDate", "/ModDate", "/Trapped"}
_INFO_IDENTITY = _INFO_KEYS - {"/Title", "/Subject", "/Keywords", "/Trapped"}
_PTEX = ("/PTEX.FileName", "/PTEX.PageNumber", "/PTEX.InfoDict")
_ANNOT_IDENTITY = ("/T", "/M", "/CreationDate", "/Subj", "/NM")  # author, dates, subject, unique name
_OC_CONFIG_KEYS = {"/BaseState", "/ON", "/OFF", "/Order", "/OCGs"}


def _is_xmp(value) -> bool:
    try:
        obj = value.get_object()
        return hasattr(obj, "keys") and (obj.get("/Type") == "/Metadata" or obj.get("/Subtype") == "/XML")
    except Exception:  # noqa: BLE001 - broken reference: treat as not XMP
        return False


def _strip(root, reader, ctx: Context) -> set[str]:
    """Remove identifying entries from everything reachable from root (in memory); return notes."""
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

    page_ids = {p.indirect_reference.idnum for p in reader.pages if p.indirect_reference is not None}
    seen: set[tuple[int, int]] = set()
    notes: set[str] = set()
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
            if kind in ("/Page", "/Catalog") or subtype in ("/Form", "/Image"):
                for k in ("/PieceInfo", "/LastModified"):
                    obj.pop(k, None)
            for k in _PTEX:
                obj.pop(k, None)
            obj.pop("/Thumb", None)
            obj.pop("/CreatorInfo", None)
            if "/Creator" in obj and obj.keys() & _OC_CONFIG_KEYS:
                del obj["/Creator"]
            if kind is None and obj.keys() <= _INFO_KEYS and obj.keys() & _INFO_IDENTITY:
                for k in list(obj.keys()):  # an Info-shaped dictionary (e.g. pdfTeX's /PTEX.InfoDict)
                    del obj[k]
            if obj.get("/FT") == "/Sig" and "/V" in obj:
                del obj["/V"]  # the rewrite invalidates the signature; do not keep the signer's identity
                notes.add("a digital signature was removed (the rewrite would invalidate it)")
            if kind == "/Catalog":
                obj.pop("/Perms", None)
            if kind == "/Page" and ref_id is not None and ref_id not in page_ids:
                notes.add("pages that are not in the page tree (deleted pages still referenced by bookmarks/links)")
            if ctx.full:
                _full_checks(obj, ctx, notes)
            stack.extend((v, None) for v in obj.values())
        elif isinstance(obj, ArrayObject):
            stack.extend((v, None) for v in obj)
    return notes


def _filters(obj) -> list:
    f = obj.get("/Filter")
    if f is None:
        return []
    return [str(x) for x in f] if isinstance(f, list) else [str(f)]


def _full_checks(obj, ctx: Context, notes: set[str]) -> None:
    """--full: annotation authors/dates, EXIF inside embedded JPEGs; flag what cannot be cleaned."""
    if obj.get("/Type") == "/Annot" or ("/Subtype" in obj and "/Rect" in obj):
        for k in _ANNOT_IDENTITY:
            obj.pop(k, None)
        if obj.get("/Subtype") == "/FileAttachment":
            notes.add("embedded file attachments")
    if "/EmbeddedFiles" in obj or obj.get("/Type") == "/EmbeddedFile":
        notes.add("embedded file attachments")
    if "/XFA" in obj:
        notes.add("XFA form data")
    if obj.get("/Subtype") == "/Image":
        filters = _filters(obj)
        if filters == ["/DCTDecode"] and hasattr(obj, "_data"):
            try:
                obj._data = images.clean_jpeg(obj._data)
            except WipeError as e:
                ctx.leftover(f"embedded JPEG could not be cleaned ({e})")
        elif "/JPXDecode" in filters:
            notes.add("JPEG 2000 images (their metadata boxes are not cleaned)")
        elif filters and filters[0] == "/DCTDecode":
            notes.add("JPEG images behind a filter chain")


def clean_pdf(src: Path, dst: Path, ctx: Context) -> None:
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.errors import DependencyError, PyPdfError
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
        notes = _strip(reader.trailer["/Root"], reader, ctx)
        reader.trailer.pop("/ID", None)  # the permanent file identifier links the output to the original
        writer = PdfWriter(clone_from=reader)
        writer.metadata = None
        writer._ID = None
        with open(dst, "wb") as fh:
            writer.write(fh)
        _verify(dst)
        for note in sorted(notes):
            if note.startswith("a digital"):
                ctx.warn(f"PDF: {note}")
            else:
                ctx.leftover(f"PDF contains {note}; not cleaned")
    except DependencyError as e:
        raise WipeError(f"{e}; install it with: pip install cryptography") from e
    except PyPdfError as e:
        raise WipeError(f"cannot process PDF: {e}") from e
    except RecursionError as e:
        raise WipeError("PDF structure is nested too deeply to process") from e
    except NotImplementedError as e:
        raise WipeError(f"unsupported PDF feature: {e}") from e


def _verify(path: Path) -> None:
    from pypdf import PdfReader
    from pypdf.generic import IndirectObject

    reader = PdfReader(path)
    if reader.trailer.get("/Info") is not None or "/Metadata" in reader.trailer["/Root"]:
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
