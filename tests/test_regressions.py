"""Regression tests for problems found in the adversarial review of v0.2 (one test per finding)."""
import io
import os
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
import zlib
from pathlib import Path

from metawipe import images
from tests.test_metawipe import Base, HAVE, SECRET, exif_image, run

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None
try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None

TOOLS = {t: shutil.which(t) is not None for t in ("tiffset", "tiffdump", "exiv2")}


def png_bytes(text_chunks=()):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))

    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    for t, d in text_chunks:
        out += chunk(t, d)
    return out + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b"")


class TiffRegression(Base):
    def test_crafted_subifd_bomb_terminates(self):
        p = self.dir / "hang.tif"
        p.write_bytes(bytes.fromhex(
            "49492a000800000001004a010100040000001a1a1a1a0000000004004a010100040000001a1a1a1a"
            "69870100040000001a1a1a1a25880100040000001a1a1a1a05a00100040000001a1a1a1a"))
        res = subprocess.run([sys.executable, "-m", "metawipe", "-o", str(self.dir / "o"), str(p)],
                             capture_output=True, text=True, timeout=30)
        self.assertIn(res.returncode, (0, 1))
        self.assertEqual([f.name for f in self.dir.iterdir() if f.name.startswith(".metawipe-")], [])

    @unittest.skipUnless(Image and TOOLS["tiffset"], "needs Pillow and tiffset")
    def test_stale_values_from_libtiff_edits_are_zeroed(self):
        p = self.dir / "a.tif"
        Image.new("RGB", (16, 16), "red").save(p)
        for tag, old in (("270", "SECRET-OLD-DESCRIPTION-AAAAAAAAAAAAAAAAAAAAAAAA"), ("305", "SECRET-OLD-SOFTWARE-BBBBBBBBBBBBBBBBBBBBBB")):
            subprocess.run(["tiffset", "-s", tag, old, str(p)], check=True)
            subprocess.run(["tiffset", "-s", tag, "x", str(p)], check=True)
        self.assertIn(b"SECRET-OLD", p.read_bytes())
        before = Image.open(p).tobytes()
        self.clean(p)
        self.assertNotIn(b"SECRET-OLD", p.read_bytes())
        self.assertEqual(Image.open(p).tobytes(), before)

    @unittest.skipUnless(Image and TOOLS["exiv2"], "needs Pillow and exiv2")
    def test_exif_style_jpeg_thumbnail_ifd_is_removed(self):
        p = self.dir / "a.tif"
        Image.new("RGB", (64, 48), "red").save(p, compression="tiff_lzw")
        thumb = self.dir / "a-thumb.jpg"
        Image.new("RGB", (16, 12), "black").save(thumb)
        subprocess.run(["exiv2", "-f", "-it", str(p)], check=True, capture_output=True)
        thumb_bytes = thumb.read_bytes()
        self.assertIn(thumb_bytes, p.read_bytes())
        before = Image.open(p).tobytes()
        self.clean(p)
        self.assertNotIn(thumb_bytes, p.read_bytes())
        out = Image.open(p)
        self.assertEqual(out.n_frames, 1)
        self.assertEqual(out.tobytes(), before)


class PngRegression(Base):
    def test_orientation_in_imagemagick_raw_profile_text_is_kept(self):
        from metawipe.common import minimal_tiff

        raw = b"Exif\x00\x00" + minimal_tiff(6)
        text = b"\nexif\n%8d\n%s\n" % (len(raw), raw.hex().encode())
        p = self.dir / "a.png"
        p.write_bytes(png_bytes([(b"zTXt", b"Raw profile type exif\x00\x00" + zlib.compress(text))]))
        self.clean(p)
        data = p.read_bytes()
        self.assertNotIn(b"zTXt", data)
        self.assertIn(b"eXIf", data)
        from metawipe.common import tiff_orientation

        i = data.index(b"eXIf")
        n = int.from_bytes(data[i - 4:i], "big")
        self.assertEqual(tiff_orientation(data[i + 4:i + 4 + n]), 6)


@unittest.skipUnless(HAVE["heif-enc"] and HAVE["heif-info"] and Image, "libheif tools or Pillow missing")
class HeifRegression(Base):
    def source(self):
        src = self.dir / "src.jpg"
        Image.effect_mandelbrot((64, 48), (-3, -2.5, 2, 2.5), 100).convert("RGB").save(src, exif=exif_image())
        return src

    def test_udes_description_is_blanked(self):
        heic = self.dir / "u.heic"
        subprocess.run(["heif-enc", "-q", "70", "--pitm-description", "SECRET-DESC-UDES", str(self.source()), "-o", str(heic)],
                       check=True, capture_output=True)
        self.assertIn(b"SECRET-DESC-UDES", heic.read_bytes())
        self.clean(heic)
        self.assertNotIn(b"SECRET-DESC-UDES", heic.read_bytes())
        self.assertEqual(subprocess.run(["heif-info", str(heic)], capture_output=True).returncode, 0)

    def test_mini_layout_is_refused_not_reported_clean(self):
        heic = self.dir / "m.heic"
        res = subprocess.run(["heif-enc", "-q", "70", "--mini", str(self.source()), "-o", str(heic)], capture_output=True)
        if res.returncode != 0:
            self.skipTest("heif-enc without --mini support")
        original = heic.read_bytes()
        rc, out, err = run("-i", heic)
        self.assertEqual(rc, 1)
        self.assertIn("mini", err)
        self.assertEqual(heic.read_bytes(), original)


@unittest.skipIf(pypdf is None, "pypdf missing")
class PdfRegression(Base):
    def base(self):
        w = pypdf.PdfWriter()
        w.add_blank_page(100, 100)
        return w

    def write(self, w, name="a.pdf"):
        p = self.dir / name
        with open(p, "wb") as fh:
            w.write(fh)
        return p

    def test_pdftex_infodict_and_filename_removed(self):
        from pypdf.generic import DictionaryObject, NameObject, TextStringObject

        w = self.base()
        page = w.pages[0]
        info = DictionaryObject({NameObject("/Author"): TextStringObject("Jozko Mrkvicka"),
                                 NameObject("/Producer"): TextStringObject("pdfTeX")})
        form = DictionaryObject({NameObject("/PTEX.FileName"): TextStringObject("/home/jozko/secret.pdf"),
                                 NameObject("/PTEX.InfoDict"): w._add_object(info)})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Foo"): w._add_object(form)})
        p = self.write(w)
        self.assertIn(b"Mrkvicka", p.read_bytes())
        self.clean(p)
        data = p.read_bytes()
        for needle in (b"Mrkvicka", b"jozko", b"PTEX"):
            self.assertNotIn(needle, data)

    def test_trailer_id_removed(self):
        from pypdf.generic import ArrayObject, ByteStringObject

        w = self.base()
        w._ID = ArrayObject([ByteStringObject(b"SECRETID1"), ByteStringObject(b"SECRETID2")])
        p = self.write(w)
        self.assertIn(b"SECRETID".hex().encode(), p.read_bytes().lower())
        self.clean(p)
        data = p.read_bytes().lower()
        self.assertNotIn(b"SECRETID".hex().encode(), data)
        self.assertNotIn(b"/id", data)

    def test_empty_trailer_id_does_not_crash(self):
        from pypdf.generic import ArrayObject

        w = self.base()
        w._ID = ArrayObject([])
        p = self.write(w)
        rc, out, err = run("-i", p)
        self.assertEqual(rc, 0, out + err)

    def test_password_flag_with_empty_user_password(self):
        w = self.base()
        w.encrypt("", owner_password="owner", algorithm="RC4-128")
        p = self.write(w)
        rc, out, err = run("-p", "wrong", p)
        self.assertEqual(rc, 0, out + err)

    def test_deleted_page_still_referenced_is_reported(self):
        from pypdf.generic import NameObject, NumberObject

        w = self.base()
        page2 = w.add_blank_page(100, 100)
        w.add_outline_item("to deleted page", 1)
        pages = w.root_object["/Pages"]
        pages["/Kids"].pop()
        pages[NameObject("/Count")] = NumberObject(1)
        p = self.write(w)
        rc, out, err = run("--full", "-i", p)
        self.assertIn("not in the page tree", err)
        self.assertEqual(rc, 2)

    def test_signature_identity_removed(self):
        from pypdf.generic import ArrayObject, ByteStringObject, DictionaryObject, NameObject, TextStringObject

        w = self.base()
        sig = DictionaryObject({NameObject("/Type"): NameObject("/Sig"), NameObject("/Name"): TextStringObject("Jozko Mrkvicka"),
                                NameObject("/Contents"): ByteStringObject(b"CERT-" + SECRET)})
        field = DictionaryObject({NameObject("/FT"): NameObject("/Sig"), NameObject("/T"): TextStringObject("Sig1"),
                                  NameObject("/V"): w._add_object(sig)})
        w.root_object[NameObject("/AcroForm")] = DictionaryObject({NameObject("/Fields"): ArrayObject([w._add_object(field)])})
        p = self.write(w)
        self.clean(p)
        data = p.read_bytes()
        self.assertNotIn(b"Mrkvicka", data)
        self.assertNotIn(SECRET.hex().encode(), data.lower())

    def test_page_thumbnail_removed_and_legit_metadata_named_resource_kept(self):
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        w = self.base()
        thumb = DecodedStreamObject()
        thumb.set_data(b"THUMBPIXELS")
        form = DecodedStreamObject()
        form.set_data(b"q Q")
        form.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"),
                     NameObject("/BBox"): pypdf.generic.ArrayObject([pypdf.generic.NumberObject(0)] * 4)})
        page = w.pages[0]
        page[NameObject("/Thumb")] = w._add_object(thumb)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Metadata"): w._add_object(form)})})
        p = self.write(w)
        self.clean(p)
        self.assertNotIn(b"THUMBPIXELS", p.read_bytes())
        r = pypdf.PdfReader(p)
        self.assertIn("/Metadata", r.pages[0]["/Resources"]["/XObject"])


class OfficeRegression(Base):
    CT = (b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          b'<Default Extension="xml" ContentType="x"/><Override PartName="/meta/core.xml" ContentType="c"/>'
          b'<Override PartName="/_xmlsignatures/sig1.xml" ContentType="s"/></Types>')

    def zip(self, path, entries):
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in entries:
                z.writestr(name, data)

    def test_metadata_parts_found_by_relationship_type_and_signatures_printers_dropped(self):
        root = (b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                b'<Relationship Id="a" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="meta/core.xml"/>'
                b'<Relationship Id="b" Type="http://schemas.openxmlformats.org/package/2006/relationships/digital-signature/origin" Target="_xmlsignatures/origin.sigs"/>'
                b'<Relationship Id="c" Type="t/doc" Target="word/document.xml"/></Relationships>')
        p = self.dir / "a.docx"
        self.zip(p, [("[Content_Types].xml", self.CT), ("_rels/.rels", root), ("word/document.xml", b"<d/>"),
                     ("meta/core.xml", b"<c>" + SECRET + b"</c>"), ("_xmlsignatures/origin.sigs", b""),
                     ("_xmlsignatures/sig1.xml", b"SIGNER " + SECRET), ("word/printerSettings/printerSettings1.bin", SECRET)])
        rc, out, err = run("-i", p)
        self.assertEqual(rc, 0, out + err)
        with zipfile.ZipFile(p) as z:
            self.assertEqual(sorted(z.namelist()), ["[Content_Types].xml", "_rels/.rels", "word/document.xml"])
            rels = z.read("_rels/.rels")
            self.assertNotIn(b"meta/core.xml", rels)
            self.assertNotIn(b"_xmlsignatures", rels)
            self.assertIn(b"word/document.xml", rels)
            self.assertNotIn(b"core.xml", z.read("[Content_Types].xml"))
        self.assertIn("signature", err)

    def test_encrypted_odf_is_refused_untouched(self):
        mime = b"application/vnd.oasis.opendocument.text"
        manifest = (b'<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
                    b'<manifest:file-entry manifest:full-path="content.xml"><manifest:encryption-data/></manifest:file-entry></manifest:manifest>')
        p = self.dir / "a.odt"
        self.zip(p, [("mimetype", mime), ("content.xml", b"x"), ("META-INF/manifest.xml", manifest)])
        original = p.read_bytes()
        rc, out, err = run("-i", p)
        self.assertEqual(rc, 1)
        self.assertIn("encrypted", err)
        self.assertEqual(p.read_bytes(), original)

    def test_person_lists_and_workbook_paths_cleaned_by_default(self):
        wb = (b'<workbook xmlns:x15ac="u"><mc:AlternateContent xmlns:mc="m"><mc:Choice>'
              b'<x15ac:absPath url="C:\\Users\\Jozko\\a.xlsx"></x15ac:absPath></mc:Choice></mc:AlternateContent>'
              b'<fileSharing userName="Jozko Mrkvicka" readOnlyRecommended="1"/></workbook>')
        p = self.dir / "a.xlsx"
        self.zip(p, [("[Content_Types].xml", b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'),
                     ("xl/workbook.xml", wb),
                     ("xl/persons/person.xml", b'<personList><person displayName="Jozko Mrkvicka" userId="j@x.sk" providerId="AD"/></personList>')])
        self.assertEqual(run("-i", p)[0], 0)
        with zipfile.ZipFile(p) as z:
            blob = z.read("xl/workbook.xml") + z.read("xl/persons/person.xml")
            for needle in (b"Jozko", b"Mrkvicka", b"j@x.sk"):
                self.assertNotIn(needle, blob)
            self.assertIn(b"readOnlyRecommended", z.read("xl/workbook.xml"))

    def test_legacy_cp437_entry_names_are_preserved(self):
        p = self.dir / "a.docx"
        self.zip(p, [("[Content_Types].xml", b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'),
                     ("word/caf_.xml", b"<x/>")])
        raw = p.read_bytes().replace(b"caf_.xml", b"caf\x82.xml")
        p.write_bytes(raw)
        self.assertEqual(run("-i", p)[0], 0)
        out = p.read_bytes()
        self.assertIn(b"caf\x82.xml", out)
        self.assertNotIn(b"caf\xc3\xa9", out)

    def test_nested_odf_in_embeddings_is_cleaned(self):
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as z:
            z.writestr("mimetype", b"application/vnd.oasis.opendocument.spreadsheet")
            z.writestr("meta.xml", b"<m>" + SECRET + b"</m>")
        p = self.dir / "a.docx"
        self.zip(p, [("[Content_Types].xml", b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'),
                     ("word/embeddings/o.ods", inner.getvalue())])
        self.assertEqual(run("-i", p)[0], 0)
        with zipfile.ZipFile(p) as z:
            nested = zipfile.ZipFile(io.BytesIO(z.read("word/embeddings/o.ods")))
            self.assertNotIn(SECRET, nested.read("meta.xml"))


class CliRegression(Base):
    def png(self, path):
        Path(path).write_bytes(png_bytes([(b"tEXt", b"k\x00" + SECRET)]))

    def test_empty_path_argument_is_an_error_not_cwd(self):
        self.png(self.dir / "a.png")
        cwd = os.getcwd()
        os.chdir(self.dir)
        try:
            rc, out, err = run("-i", "")
        finally:
            os.chdir(cwd)
        self.assertEqual(rc, 1)
        self.assertIn(SECRET, (self.dir / "a.png").read_bytes())

    def test_explicit_lock_like_and_clean_named_files_are_processed(self):
        for name in ("~$a.png", "b.clean.png"):
            self.png(self.dir / name)
        (self.dir / "b.png").write_bytes(b"x")
        rc, out, err = run("-i", self.dir / "~$a.png", self.dir / "b.clean.png")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.count("clean  "), 2)

    def test_scan_skips_sidecars_fifos_and_notes_subdirectories(self):
        self.png(self.dir / "a.png")
        self.png(self.dir / "._a.png")
        os.mkfifo(self.dir / "pipe.jpg")
        (self.dir / "sub").mkdir()
        rc, out, err = run("-v", self.dir)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("use -r", out)
        self.assertIn("not a regular file", out)
        self.assertIn("1 cleaned", out)

    def test_fifo_given_explicitly_fails_fast(self):
        os.mkfifo(self.dir / "pipe.jpg")
        rc, out, err = run(self.dir / "pipe.jpg")
        self.assertEqual(rc, 1)
        self.assertIn("not a regular file", err)

    def test_real_file_wins_over_symlink_to_it(self):
        real = self.dir / "real.png"
        self.png(real)
        link = self.dir / "a-link.png"
        link.symlink_to(real)
        rc, out, err = run("-i", link, real)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn(SECRET, real.read_bytes())
        self.assertTrue(link.is_symlink())

    @unittest.skipIf(os.geteuid() == 0, "root ignores permissions")
    def test_read_only_file_is_not_replaced_in_place(self):
        p = self.dir / "a.png"
        self.png(p)
        os.chmod(p, 0o400)
        rc, out, err = run("-i", p)
        self.assertEqual(rc, 1)
        self.assertIn("read-only", err)
        self.assertIn(SECRET, p.read_bytes())

    def test_setuid_bits_not_copied_and_failed_job_leaves_no_empty_output_dirs(self):
        p = self.dir / "a.png"
        self.png(p)
        os.chmod(p, 0o4755)
        self.assertEqual(run(p)[0], 0)
        self.assertEqual(stat.S_IMODE((self.dir / "a.clean.png").stat().st_mode), 0o755)
        (self.dir / "bad.png").write_bytes(b"not a png")
        rc, out, err = run("-o", self.dir / "o" / "deep", self.dir / "bad.png")
        self.assertEqual(rc, 1)
        self.assertFalse((self.dir / "o").exists())
        self.assertNotIn(".metawipe-", err)

    def test_sigterm_removes_temp_files(self):
        p = self.dir / "big.png"
        self.png(p)
        script = (
            "import sys, time\n"
            "import metawipe.cli as c\n"
            "def slow(src, dst, ctx):\n"
            "    dst.write_bytes(b'partial'); print('ready', flush=True); time.sleep(30)\n"
            "c.HANDLERS['.png'] = slow\n"
            "sys.exit(c.main(['-i', sys.argv[1]]))\n"
        )
        proc = subprocess.Popen([sys.executable, "-c", script, str(p)], stdout=subprocess.PIPE, text=True)
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)
        self.assertEqual(proc.returncode, 130)
        self.assertEqual([f.name for f in self.dir.iterdir()], ["big.png"])


@unittest.skipUnless(HAVE["ffmpeg"] and HAVE["ffprobe"], "ffmpeg missing")
class VideoRegression(Base):
    def make(self, path, *extra, audio=True):
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=d=1:s=64x64:r=10"]
        if audio:
            cmd += ["-f", "lavfi", "-i", "sine=d=1"]
        cmd += ["-c:v", "libx264", *( ["-c:a", "aac"] if audio else []), *extra, str(path)]
        subprocess.run(cmd, check=True)
        return path

    def probe(self, path, entries="stream=index,codec_name,codec_type:stream_tags=language"):
        return subprocess.run(["ffprobe", "-v", "error", "-show_entries", entries, "-of", "compact", str(path)],
                              capture_output=True, text=True).stdout

    def test_url_looking_file_names_are_read_as_files(self):
        self.make(self.dir / "async:clip.mov")
        cwd = os.getcwd()
        os.chdir(self.dir)
        try:
            rc, out, err = run("-i", "async:clip.mov")
        finally:
            os.chdir(cwd)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("h264", self.probe(self.dir / "async:clip.mov"))

    def test_text_file_named_mp4_is_rejected_even_if_it_is_an_ffconcat_script(self):
        real = self.make(self.dir / "real.mov")
        fake = self.dir / "fake.mp4"
        fake.write_text(f"ffconcat version 1.0\nfile {real}\n")
        rc, out, err = run("-i", fake)
        self.assertEqual(rc, 1)
        self.assertTrue(fake.read_text().startswith("ffconcat"))

    def test_cover_art_dropped_and_languages_kept(self):
        from PIL import Image  # noqa: F401  (cover generated with ffmpeg below)

        cover = self.dir / "cover.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=32x32", "-frames:v", "1", str(cover)], check=True)
        vid = self.make(self.dir / "v.mp4")
        two = self.dir / "two.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(vid), "-i", str(vid), "-i", str(cover), "-map", "0:v", "-map", "0:a",
                        "-map", "1:a", "-map", "2", "-c", "copy", "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=ces",
                        "-disposition:v:1", "attached_pic", str(two)], check=True)
        self.assertIn("mjpeg", self.probe(two))
        rc, out, err = run("-i", two)
        self.assertEqual(rc, 0, out + err)
        probed = self.probe(two)
        self.assertNotIn("mjpeg", probed)
        self.assertIn("language=eng", probed)
        self.assertIn("language=ces", probed)

    @unittest.skipUnless(HAVE["x265"], "libx265 missing")
    def test_m4v_with_hevc_is_accepted(self):
        mp4 = self.dir / "h.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=d=1:s=64x64:r=10", "-c:v", "libx265", "-tag:v", "hvc1", str(mp4)], check=True)
        m4v = self.dir / "h.m4v"
        shutil.copy(mp4, m4v)
        rc, out, err = run("-i", m4v)
        self.assertEqual(rc, 0, out + err)

    def test_truncated_video_fails_and_is_left_alone(self):
        vid = self.make(self.dir / "full.mp4", "-b:v", "2M", "-movflags", "+faststart")
        data = vid.read_bytes()
        trunc = self.dir / "trunc.mp4"
        trunc.write_bytes(data[: len(data) * 6 // 10])
        original = trunc.read_bytes()
        rc, out, err = run("-i", trunc)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(trunc.read_bytes(), original)
        self.assertEqual([f.name for f in self.dir.iterdir() if f.name.startswith(".metawipe-")], [])


if __name__ == "__main__":
    unittest.main()
