#!/usr/bin/env python3
"""metawipe - strip metadata from images, PDFs and Office/OpenDocument files."""
import argparse
import re
import shutil
import sys
import zipfile
from pathlib import Path

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
ZIP_EXT = {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}
OFFICE_META = ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml")
ODF_META = "meta.xml"
ODF_EMPTY_META = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<office:document-meta xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'office:version="1.3"><office:meta/></office:document-meta>'
)


def clean_image(src: Path, dst: Path) -> None:
    from PIL import Image

    with Image.open(src) as im:
        fmt = im.format
        # info holds EXIF/XMP/ICC/text chunks; Pillow only writes them if passed or present here.
        clean = im.copy()
        clean.info = {}
        save_kwargs = {"quality": 95} if fmt == "JPEG" else {}
        clean.save(dst, format=fmt, **save_kwargs)


def clean_pdf(src: Path, dst: Path) -> None:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(src)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.add_metadata({})
    writer._info = None  # drop the /Info dictionary entirely
    with open(dst, "wb") as fh:
        writer.write(fh)


def clean_zip(src: Path, dst: Path) -> None:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename in OFFICE_META:
                continue
            if item.filename == ODF_META:
                data = ODF_EMPTY_META.encode()
            if item.filename == "[Content_Types].xml":
                data = re.sub(rb"<Override[^>]*docProps/[^>]*/>", b"", data)
            if item.filename == "_rels/.rels":
                data = re.sub(rb"<Relationship [^>]*docProps/[^>]*/>", b"", data)
            zout.writestr(item, data)


def handler_for(path: Path):
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        return clean_image
    if ext == ".pdf":
        return clean_pdf
    if ext in ZIP_EXT:
        return clean_zip
    return None


def process(path: Path, inplace: bool, outdir: Path | None) -> bool:
    handler = handler_for(path)
    if handler is None:
        print(f"skip   {path} (unsupported type)")
        return False
    if inplace:
        dst = path.with_name(path.name + ".metawipe.tmp")
    else:
        dst = (outdir or path.parent) / f"{path.stem}.clean{path.suffix}"
        dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        handler(path, dst)
    except ImportError as e:
        print(f"error  {path}: missing dependency ({e.name}); pip install -r requirements.txt", file=sys.stderr)
        dst.unlink(missing_ok=True)
        return False
    except Exception as e:  # noqa: BLE001 - report and continue with other files
        print(f"error  {path}: {e}", file=sys.stderr)
        dst.unlink(missing_ok=True)
        return False
    if inplace:
        shutil.move(dst, path)
        dst = path
    print(f"clean  {path} -> {dst}")
    return True


def collect(paths, recursive):
    for p in map(Path, paths):
        if p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            yield from (f for f in sorted(it) if f.is_file() and ".clean." not in f.name)
        else:
            yield p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="metawipe", description=__doc__)
    ap.add_argument("paths", nargs="+", help="files or directories")
    ap.add_argument("-r", "--recursive", action="store_true", help="recurse into directories")
    ap.add_argument("-i", "--in-place", action="store_true", help="overwrite originals (default: write NAME.clean.EXT)")
    ap.add_argument("-o", "--output-dir", type=Path, help="write cleaned copies here")
    args = ap.parse_args(argv)
    if args.in_place and args.output_dir:
        ap.error("--in-place and --output-dir are mutually exclusive")

    ok = fail = 0
    for f in collect(args.paths, args.recursive):
        if not f.exists():
            print(f"error  {f}: not found", file=sys.stderr)
            fail += 1
        elif process(f, args.in_place, args.output_dir):
            ok += 1
        elif handler_for(f) is not None:
            fail += 1
    print(f"done: {ok} cleaned, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
