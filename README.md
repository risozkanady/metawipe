# metawipe

Command-line tool that strips identifying metadata from files before you share them.

| Type | Extensions | How |
|------|-----------|-----|
| Photos | `jpg jpeg png apng webp tif tiff` | Container-level: pixels are copied byte for byte (no re-encoding), only structural parts are kept |
| iPhone/HEIF | `heic heif avif` | Exif payloads and every mime item (XMP, whatever its content type) zeroed in place, file size and image data unchanged |
| Video | `mov mp4 m4v 3gp` | `ffmpeg -c copy`: no re-encode; location, device, creation time, metadata tracks and cover art dropped; track languages kept |
| PDF | `pdf` | pypdf: Info dictionary, XMP, `/PieceInfo`, thumbnails, file `/ID`, pdfTeX paths, signature identities (incl. signer certificates in `/DSS`); outlines, forms (field names too), layers survive |
| Office | `docx xlsx pptx` (+ macro/template variants) | properties, thumbnail, people list, template path, absolute path, printer settings; nested packages cleaned wherever they sit in the package |
| OpenDocument | `odt ods odp odg` (+ templates) | `meta.xml` emptied, thumbnail and printer settings removed |

## Install

Ready-made executables (Python and the TUI included, nothing else to install):

- **macOS / Linux with Homebrew:** `brew install risozkanady/metawipe/metawipe`
- **Windows:** `metawipe-VERSION-windows-x86_64.zip` from [Releases](https://github.com/risozkanady/metawipe/releases);
  unzip and run `metawipe.exe` (double-click opens the TUI). The file is not signed, so SmartScreen asks once
  ("More info" → "Run anyway").
- **Linux:** `metawipe-VERSION-linux-x86_64.tar.gz` from [Releases](https://github.com/risozkanady/metawipe/releases);
  unpack and put `metawipe` on the PATH.

Building one yourself: `pip install '.[tui]' pyinstaller && packaging/build.sh` (result in `dist/`).

From source:

```
git clone https://github.com/wspnowpartnerGAUC/metawipe && cd metawipe
python3 -m venv .venv && source .venv/bin/activate
pip install .            # puts a `metawipe` command in the venv; or use  python -m metawipe
```

Videos need `ffmpeg` (with `ffprobe`) on the PATH (`brew install ffmpeg`, `pacman -S ffmpeg`, ...);
`--full` on a video refuses to run without `ffprobe`. Everything else is pure Python.
For the interactive TUI (`metawipe --tui`) install with `pip install '.[tui]'`.
In the TUI, `v` switches the preview between what the file reveals and everything it carries.
The file tree reloads after every run (and on `F5`), so new `NAME.clean.EXT` copies and output folders show up.
`s` opens the settings (theme, clock with date, hidden files, preview mode, remember the cleaning options, the Keep switches); every change
is saved at once in `~/.config/metawipe/config.json` (`$XDG_CONFIG_HOME` is respected).

## Usage

```
metawipe photo.jpg report.pdf          # writes photo.clean.jpg and report.clean.pdf
metawipe -i -r ~/Pictures/trip         # overwrite originals, recursively
metawipe -r -o cleaned/ ~/Documents    # cleaned copies in cleaned/, same folder structure
metawipe -p secret locked.pdf          # password for encrypted PDFs
metawipe --keep dates photo.jpg        # keep the dates, wipe everything else (see "Keeping some metadata")
metawipe --show photo.jpg              # list everything the file carries, writes nothing (-r for folders)
```

- Existing output files are never overwritten unless you pass `-f`.
- `-i` refuses symlinks and hard-linked files (the other names would keep the metadata).
- File permissions are preserved (setuid/setgid/sticky bits are not); modification time is "now" (the original's with `--keep dates`).
  When run as root, `-i` gives the file back to its original owner and group.
- Exit status is 1 if any file failed or was an explicit argument of an unsupported type.
  Unsupported files found while scanning a directory are only counted (`-v` lists them).

## What is deliberately kept

- **Orientation** of photos (a one-tag Exif block): dropping it would turn portrait photos sideways.
- **ICC colour profiles**, PNG gamma/sRGB, DPI and PDF/Office content itself.
- Extra images of a JPEG (MPO/Ultra HDR gain maps), Exif-style TIFF thumbnails (the second IFD, old-style JPEG without strips,
  even with ImageWidth/ImageLength, unless it is as large as the first page; later IFDs with dimensions are kept as pages) and data after the end of a file are **dropped**.
- PNG orientation, also in ImageMagick's private `orNT` chunk.
- HEIF/AVIF handler, compressor and item names are blanked in place (size and offsets unchanged).
- Digital signatures (PDF, Office, ODF) are removed, because cleaning invalidates them anyway.

## Keeping some metadata (`--keep`)

Everything is wiped by default. `--keep LIST` (comma-separated, or `all`) leaves some kinds in:
`dates`, `location`, `device`, `software`, `people`, `descriptions`, `thumbnails`.

```
metawipe --keep dates photo.jpg
metawipe --keep dates,descriptions report.pdf
```

- Kept values are copied one by one into a fresh minimal structure, never a whole Exif/XMP/Info block; each value is
  checked (dates must parse, text is length-limited and free of control characters) and dropped if it does not pass.
- `dates` also keeps the file's modification time (instead of "now", or 1980 with `--full`).
- `device` means make, model and lens, **including the body and lens serial numbers**, which identify one exact camera.
- Each cleaned file reports what really stayed: `clean photo.jpg photo.clean.jpg (kept: dates)`. A format that has a kept
  kind but cannot carry it in this version removes it and prints a warning (`dates not kept: ...`).
- With `--full`, a kept kind is also spared by the full-scrub step for it: comment/revision authors (`people`), their
  dates (`dates`), the x264/x265 SEI strings (`software`) and PDF page thumbnails (`thumbnails`) stay as they are.
- **Never kept:** the camera MakerNote, XMP and IPTC (in this version, so a kept kind that also sits in XMP/IPTC is still removed
  there) and the file's creation (birth) time.
- In the TUI the same switches are in the settings (`s`, group "Keep (not wiped)"), saved as `"keep": [...]` in the config file;
  the top bar shows `keeps: ...` whenever anything is kept, and the Results log shows `(kept: ...)` per file.

## Full scrub (`--full`)

```
metawipe --full -i report.docx video.mp4
```

On top of the normal cleaning, `--full`:

- anonymises author names, initials and dates on comments, tracked changes and people lists:
  Word comments/revisions (`w:author`, `initials`, `date`, `dateUtc`, `commentsExtensible`), Excel
  comments, threaded comments, shared-workbook revision logs and `persons`, PowerPoint
  `commentAuthors`/`authors`, ODF creator/date/author/sender fields - and PDF annotations of
  non-form objects (comment *text* and form field names are kept);
- cleans pictures embedded in Office/ODF files (EXIF/GPS) and embedded JPEGs in PDFs, also behind a
  filter chain (ASCII85/Flate/... + DCT);
- drops `customXml` stores, the Microsoft sensitivity label (`docMetadata/LabelInfo.xml`; default mode
  only warns), ODF `Configurations2/`, PDF page thumbnails and Word `docVars`
  (printer settings are already dropped in the default mode, with the references to them);
- removes encoder strings from inside the video bitstream of every H.264/HEVC track: the x264/x265
  version and settings (user-data SEI) are overwritten in place, including the copy in the `hvcC` box.
  All other SEI (recovery point, frame packing, HDR10 mastering display, CLL) is kept, so decoding,
  seeking and HDR metadata are unchanged;
- sets the file modification time to 1980-01-01.

If something identifying had to be left in a file (PDF attachments and inline JPEGs, XFA forms, JPEG 2000
images, OLE embeddings, embedded video/audio/GIF/SVG/PDF media in Office files, mail-merge data sources,
TIFFs with an old-style JPEG stream or unverifiable byte counts, non-H.264/HEVC video tracks such as AV1
or encrypted tracks), the file is reported `INCOMPLETE`, warnings say what, and the exit status is 2.
In the default mode attachments and similar leftovers are only a warning.

Outputs are always new files, so file system extras of the original (extended attributes such as
macOS "where from"/quarantine, NTFS streams, birth time) are not carried over - checked with
Linux xattrs; the macOS and NTFS cases follow from the same mechanism but are untested. The
change time (ctime) is set by the OS and cannot be altered.

## Refused or reported instead of cleaned

- Encrypted OpenDocument files and HEIF files in the compact `mini` layout (`mif3`) are refused.
- HEIF/AVIF files whose structure could hide an Exif/XMP item from the cleaner are refused and nothing is written: more than one `iinf`, `iloc` or `idat` in `meta`;
  `iinf`/`infe`/`iloc` boxes outside their place in the top-level `meta`; an `infe` above version 3, version 3 with a version 2 layout, or truncated;
  an `iloc` that overruns its box, has zero-size length fields or extents longer than the file; an `mvhd`/`tkhd`/`mdhd` too short for its version.
- Truncated or damaged videos (including truncated fragmented MP4) fail instead of producing a partial copy; damaged TIFFs (strips past the end
  of the file, overlapping structures) fail instead of being zero-padded.
- Dolby Vision signalling cannot be written into `.mov` by ffmpeg (a warning says so); `.mp4`/`.m4v` keep it.
- Warnings are printed for OLE embeddings, `customXml` (dropped by `--full`) and PDF pages that are no
  longer in the page tree but still referenced by bookmarks or links.

## Limits of the preview and `--show`

They only limit what is displayed; the cleaners read whole files. The preview and the "still carries" check read at most 256 MB of a file,
8 MB per XML part and 32 MB per embedded picture, and 256 MB in total per package (`--show` stops at 256 MB decompressed and says so).
A JPEG with more than 1000 segments is cut ("Not fully inspected"). A JPEG cut inside a segment still shows what that segment holds plus a
"truncated JPEG segment" row, but the cleaner refuses it. Control and bidi-override characters in file text are shown as spaces.
"Not fully inspected" means that no finding is not proof.

## What is never touched

- Document text itself, comment text, and anything identifying that you typed into a document.
- File names (they are often the leak: `IMG_1234 - Jozko.jpg`).
- BigTIFF, GIF, RAW/DNG, MKV/WebM.

## Tests

```
pip install pillow python-docx textual   # needed by part of the tests
python -m unittest discover -s tests -t . -v
```

Tests that need `ffmpeg`, `heif-enc` or `exiv2` are skipped when those tools are missing.
Tested on Linux with Python 3.14. macOS and Windows should work (pure Python) but are untested.
