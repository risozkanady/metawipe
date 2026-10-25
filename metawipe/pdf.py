"""PDF metadata removal on top of pypdf.

The document is cloned (outlines, forms, layers, tagging and links survive) and then the
/Info dictionary, the catalog XMP stream and every /Metadata, /PieceInfo and /LastModified entry
reachable from the document are removed. The result is re-read and scanned before it is accepted.
"""
from __future__ import annotations

from pathlib import Path

from .common import Context, WipeError

_FORBIDDEN = ("/Metadata", "/PieceInfo", "/LastModified")


def _walk(root) -> int:
    """Delete forbidden keys from every object reachable from root; return how many were found."""
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

    seen: set[tuple[int, int]] = set()
    found = 0
    stack = [root]
    while stack:
        obj = stack.pop()
        if isinstance(obj, IndirectObject):
            key = (obj.idnum, obj.generation)
            if key in seen:
                continue
            seen.add(key)
            obj = obj.get_object()
        if isinstance(obj, DictionaryObject):
            for key in _FORBIDDEN:
                if key in obj:
                    del obj[key]
                    found += 1
            stack.extend(obj.values())
        elif isinstance(obj, ArrayObject):
            stack.extend(obj)
    return found


def clean_pdf(src: Path, dst: Path, ctx: Context) -> None:
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.errors import DependencyError, PdfReadError
    except ImportError as e:
        raise WipeError("pypdf is required for PDF files: pip install -r requirements.txt") from e

    try:
        reader = PdfReader(src)
        if reader.is_encrypted:
            if not reader.decrypt(ctx.password or ""):
                raise WipeError("PDF is password protected; pass --password")
            ctx.warn("encryption and permission restrictions are removed from the output")
        # Strip the in-memory copy *before* cloning: pypdf keeps every object it cloned, so
        # removing a key afterwards would leave the XMP stream behind as an unreferenced object.
        _walk(reader.trailer["/Root"])
        writer = PdfWriter(clone_from=reader)
        writer.metadata = None
        with open(dst, "wb") as fh:
            writer.write(fh)
        _verify(dst)
    except DependencyError as e:
        raise WipeError(f"{e}; install it with: pip install cryptography") from e
    except PdfReadError as e:
        raise WipeError(f"cannot read PDF: {e}") from e


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
                leftovers += sum(1 for k in _FORBIDDEN if k in obj)
                leftovers += obj.get("/Type") == "/Metadata"
    if leftovers:
        raise WipeError(f"verification failed: {leftovers} metadata entries survived")
