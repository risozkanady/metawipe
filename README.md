# metawipe

Command-line tool that strips identifying metadata from files before you share them.

| Type | Extensions | How |
|------|-----------|-----|
| Photos | `jpg jpeg png apng webp tif tiff` | Container-level: pixels are copied byte for byte (no re-encoding), only structural parts are kept |
| iPhone/HEIF | `heic heif avif` | Exif/XMP payloads zeroed in place, file size and image data unchanged |
| Video | `mov mp4 m4v 3gp` | `ffmpeg -c copy`: no re-encode; location, device, creation time and metadata tracks dropped |
| PDF | `pdf` | pypdf: Info dictionary, XMP, page/object `/Metadata`, `/PieceInfo`; outlines, forms, layers survive |
| Office | `docx xlsx pptx` (+ macro/template variants) | properties, thumbnail, people list, template path, absolute path; nested packages cleaned too |
| OpenDocument | `odt ods odp odg` (+ templates) | `meta.xml` emptied, thumbnail and printer settings removed |

## Install

```
git clone https://github.com/wspnowpartnerGAUC/metawipe && cd metawipe
python3 -m venv .venv && source .venv/bin/activate
pip install .            # puts a `metawipe` command in the venv; or use  python -m metawipe
```

Videos need `ffmpeg` on the PATH (`brew install ffmpeg`, `pacman -S ffmpeg`, ...). Everything else is pure Python.

## Usage

```
metawipe photo.jpg report.pdf          # writes photo.clean.jpg and report.clean.pdf
metawipe -i -r ~/Pictures/trip         # overwrite originals, recursively
metawipe -r -o cleaned/ ~/Documents    # cleaned copies in cleaned/, same folder structure
metawipe -p secret locked.pdf          # password for encrypted PDFs
```

- Existing output files are never overwritten unless you pass `-f`.
- `-i` refuses symlinks and hard-linked files (the other names would keep the metadata).
- File permissions are preserved; modification time is "now".
- Exit status is 1 if any file failed or was an explicit argument of an unsupported type.
  Unsupported files found while scanning a directory are only counted (`-v` lists them).

## What is deliberately kept

- **Orientation** of photos (a one-tag Exif block): dropping it would turn portrait photos sideways.
- **ICC colour profiles**, PNG gamma/sRGB, DPI and PDF/Office content itself.
- Extra images of a JPEG (MPO/Ultra HDR gain maps) and data after the end of a file are **dropped**.

## Full scrub (`--full`)

```
metawipe --full -i report.docx video.mp4
```

On top of the normal cleaning, `--full`:

- anonymises author names, initials and dates on comments, tracked changes and people lists
  (docx, xlsx, pptx, OpenDocument) and on PDF annotations (comment *text* is kept);
- cleans pictures embedded in Office/ODF files (EXIF/GPS) and embedded JPEGs in PDFs;
- drops `customXml` stores, printer settings (printer/user names), ODF `Configurations2/`,
  PDF page thumbnails and Word `docVars`;
- removes H.264 encoder SEI messages (x264 version and settings) from videos;
- sets the file modification time to 1980-01-01.

If something identifying had to be left in a file (PDF attachments, XFA forms, JPEG 2000 images, OLE
embeddings, HEVC encoder SEI), the file is reported `INCOMPLETE`, warnings say what, and the exit
status is 2.

New files are created from scratch, so file system extras of the original (macOS extended attributes
such as "where from", NTFS streams, birth time) are not carried over.

## What is never touched

- Document text itself, comment text, and anything identifying that you typed into a document.
- File names (they are often the leak: `IMG_1234 - Jozko.jpg`).
- BigTIFF, GIF, RAW/DNG, MKV/WebM.

## Tests

```
pip install pillow python-docx      # optional, enables more tests
python -m unittest discover -s tests -t . -v
```

Tests that need `ffmpeg`, `heif-enc` or `exiv2` are skipped when those tools are missing.
Tested on Linux with Python 3.14.
