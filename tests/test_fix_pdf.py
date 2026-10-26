"""Regression tests for the round-3 PDF findings (one test per finding).

Fixtures are written byte for byte here (pypdf's writer would add the /Type keys and normalise the
filters these cases are about); results are checked with poppler and exiv2 where they are installed.
"""
import base64
import binascii
import io
import re
import shutil
import subprocess
import unittest
import zlib
from pathlib import Path

from tests.test_metawipe import Base, exif_image, run

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None
try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None

TOOLS = {t: shutil.which(t) is not None for t in ("pdfimages", "pdftoppm", "pdftotext", "pdfinfo", "exiv2")}


def stream(body: bytes, data: bytes) -> bytes:
    return b"<< " + body + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def build(objs: dict, root: int = 1) -> bytes:
    """A classic-xref PDF from {object number: object body}."""
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for n in sorted(objs):
        offsets[n] = len(out)
        out += b"%d 0 obj\n" % n + objs[n] + b"\nendobj\n"
    size = max(objs) + 1
    xref = len(out)
    out += b"xref\n0 %d\n" % size
    for n in range(size):
        out += b"%010d 00000 n \n" % offsets[n] if n in offsets else b"0000000000 65535 f \n"
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, root, xref)
    return bytes(out)


def document(page=b"", catalog=b"", content=b"", extra=None) -> dict:
    """One 200x200 page (objects 1-4); extra objects start at 5."""
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R " + catalog + b" >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R " + page + b" >>",
            4: stream(b"", content)}
    objs.update(extra or {})
    return objs


def lzw(data: bytes) -> bytes:
    """LZWDecode data from literal codes only; a clear code every 200 bytes keeps every code 9 bits wide."""
    codes = []
    for i in range(0, len(data), 200):
        codes += [256] + list(data[i:i + 200])
    bits = "".join(format(c, "09b") for c in codes + [257])
    bits += "0" * (-len(bits) % 8)
    return int(bits, 2).to_bytes(len(bits) // 8, "big")


def runlength(data: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(data), 128):
        chunk = data[i:i + 128]
        out += bytes([len(chunk) - 1]) + chunk
    return bytes(out) + b"\x80"


def render(path: Path, out_prefix: Path) -> list:
    """Pages rendered by poppler, as raw pixels."""
    subprocess.run(["pdftoppm", "-r", "40", str(path), str(out_prefix)], check=True, capture_output=True)
    return [Image.open(p).tobytes() for p in sorted(out_prefix.parent.glob(out_prefix.name + "*.ppm"))]


@unittest.skipIf(pypdf is None, "pypdf missing")
class PdfFixRegression(Base):
    def write(self, objs: dict, name="a.pdf") -> Path:
        p = self.dir / name
        p.write_bytes(build(objs))
        return p

    def scrub(self, path: Path, *extra):
        """Clean into out/ (the input stays for comparisons); return rc, stdout+stderr, output path."""
        rc, out, err = run(*extra, "-o", self.dir / "out", path)
        return rc, out + err, self.dir / "out" / path.name

    def fields(self, path: Path) -> list:
        return sorted(pypdf.PdfReader(path).get_fields() or {})

    # -- finding 0: JPEGs behind a filter chain
    @unittest.skipIf(Image is None, "Pillow missing")
    def test_full_cleans_jpeg_behind_lossless_filter_chain(self):
        im = Image.effect_mandelbrot((48, 32), (-3, -2.5, 2, 2.5), 100).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", exif=exif_image(), comment=b"COMMENTMARK", xmp=b"<x:xmpmeta>XMPMARK</x:xmpmeta>", quality=90)
        jpg = buf.getvalue()
        pixels = Image.open(io.BytesIO(jpg)).tobytes()
        self.assertIn(b"iPhone", jpg)
        cases = [  # (/Filter, /DecodeParms, stream data)
            (b"/DCTDecode", b"", jpg),
            (b"[/ASCII85Decode /DCTDecode]", b"", base64.a85encode(jpg) + b"~>"),
            (b"[/ASCIIHexDecode /DCTDecode]", b"", binascii.hexlify(jpg) + b">"),
            (b"[/FlateDecode /DCTDecode]", b"/DecodeParms [null << /ColorTransform 1 >>]", zlib.compress(jpg)),
            (b"[/LZWDecode /DCTDecode]", b"", lzw(jpg)),
            (b"[/RunLengthDecode /DCTDecode]", b"", runlength(jpg)),
            (b"[/A85 /DCT]", b"", base64.a85encode(jpg) + b"~>"),  # inline-image abbreviations
            (b"30 0 R", b"", binascii.hexlify(jpg) + b">"),  # indirect /Filter
        ]
        extra, names, draw = {30: b"[/ASCIIHexDecode /DCTDecode]"}, b"", b""
        for i, (filt, parms, data) in enumerate(cases):
            extra[10 + i] = stream(b"/Type /XObject /Subtype /Image /Width 48 /Height 32 /ColorSpace /DeviceRGB "
                                   b"/BitsPerComponent 8 /Filter " + filt + b" " + parms, data)
            names += b"/Im%d %d 0 R " % (i, 10 + i)
            draw += b"q 48 0 0 32 %d %d cm /Im%d Do Q\n" % (i % 4 * 50, i // 4 * 40, i)
        p = self.write(document(page=b"/Resources << /XObject << " + names + b">> >>", content=draw, extra=extra))
        rc, out, o = self.scrub(p, "--full")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("INCOMPLETE", out)
        xobjects = pypdf.PdfReader(o).pages[0]["/Resources"]["/XObject"]
        for i in range(len(cases)):
            img = xobjects[f"/Im{i}"].get_object()
            self.assertEqual(img["/Filter"], "/DCTDecode", i)  # stored as a bare JPEG, wrappers gone
            if i == 3:
                self.assertEqual(img["/DecodeParms"], {"/ColorTransform": 1})
            else:
                self.assertNotIn("/DecodeParms", img)
            data = img._data
            for mark in (b"iPhone", b"Mrkvicka", b"XMPMARK", b"COMMENTMARK"):
                self.assertNotIn(mark, data, i)
            self.assertEqual(Image.open(io.BytesIO(data)).tobytes(), pixels, i)
        if TOOLS["pdfimages"] and TOOLS["exiv2"]:  # independent extraction: poppler + exiv2
            for name, src in (("in", p), ("out", o)):
                (self.dir / name).mkdir(exist_ok=True)
                subprocess.run(["pdfimages", "-all", str(src), str(self.dir / name / "im")], check=True, capture_output=True)
                extracted = sorted((self.dir / name).glob("im-*.jpg"))
                self.assertEqual(len(extracted), len(cases), name)
                for jf in extracted:
                    tags = subprocess.run(["exiv2", "-pa", str(jf)], capture_output=True, text=True).stdout
                    if name == "in":
                        self.assertIn("GPS", tags, jf)
                    else:
                        self.assertNotIn("GPS", tags, jf)
                        self.assertNotIn("Mrkvicka", tags, jf)
                        self.assertEqual(Image.open(jf).tobytes(), pixels, jf)
        if TOOLS["pdftoppm"]:
            self.assertEqual(render(o, self.dir / "r-out"), render(p, self.dir / "r-in"))

    @unittest.skipIf(Image is None, "Pillow missing")
    def test_full_reports_jpeg_it_cannot_unwrap(self):
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(buf, "JPEG", exif=exif_image())
        jpg = buf.getvalue()
        for filt, data, message in ((b"[/FooDecode /DCTDecode]", jpg, "JPEG images behind a filter chain"),
                                    (b"[/ASCIIHexDecode /DCTDecode]", b"zz not hex >", "embedded JPEG could not be cleaned")):
            img = stream(b"/Type /XObject /Subtype /Image /Width 8 /Height 8 /ColorSpace /DeviceRGB /BitsPerComponent 8 "
                         b"/Filter " + filt, data)
            p = self.write(document(page=b"/Resources << /XObject << /Im0 5 0 R >> >>", content=b"/Im0 Do", extra={5: img}))
            rc, out, _ = self.scrub(p, "--full", "-f")
            self.assertEqual(rc, 2, out)  # kept as it was, but reported instead of claimed clean
            self.assertIn(message, out)

    @unittest.skipIf(Image is None, "Pillow missing")
    def test_full_corrupt_and_odd_filter_chains_fail_closed(self):
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(buf, "JPEG", exif=exif_image())
        jpg = buf.getvalue()
        cases = [  # (/Filter, data, expected message); every one stays as it was and is reported
            (b"[/FlateDecode /DCTDecode]", b"not zlib data at all", "embedded JPEG could not be cleaned"),
            (b"[/LZWDecode /DCTDecode]", b"\xff\xff\xff\xff", "embedded JPEG could not be cleaned"),
            (b"[/ASCII85Decode /DCTDecode]", b"\x01\x02 not a85 ~>", "embedded JPEG could not be cleaned"),
            (b"[/DCTDecode /FlateDecode]", zlib.compress(jpg), "JPEG images behind a filter chain"),  # DCT not last
            (b"/DCTDecode", b"not a jpeg at all", "embedded JPEG could not be cleaned"),
        ]
        for filt, data, message in cases:
            img = stream(b"/Type /XObject /Subtype /Image /Width 8 /Height 8 /ColorSpace /DeviceRGB /BitsPerComponent 8 "
                         b"/Filter " + filt, data)
            p = self.write(document(page=b"/Resources << /XObject << /Im0 5 0 R >> >>", content=b"/Im0 Do", extra={5: img}))
            rc, out, o = self.scrub(p, "--full", "-f")
            self.assertEqual(rc, 2, filt.decode() + out)
            self.assertIn(message, out)
            self.assertEqual(pypdf.PdfReader(o).pages[0]["/Resources"]["/XObject"]["/Im0"].get_object()._data, data)

    @unittest.skipIf(Image is None, "Pillow missing")
    def test_full_reports_inline_jpeg_and_ignores_plain_inline_images(self):
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(buf, "JPEG", exif=exif_image())
        jpeg = b"q 8 0 0 8 0 0 cm BI /W 8 /H 8 /CS /RGB /BPC 8 /F /DCT ID " + buf.getvalue() + b"\nEI Q"
        plain = b"q 2 0 0 2 0 0 cm BI /W 2 /H 2 /CS /G /BPC 8 ID abcd\nEI Q"
        for content, flagged, packed in ((jpeg, True, False), (jpeg, True, True), (plain, False, False)):
            objs = document(content=content)
            if packed:  # the same content, Flate-compressed
                objs[4] = stream(b"/Filter /FlateDecode", zlib.compress(content))
            p = self.write(objs)
            rc, out, _ = self.scrub(p, "--full", "-f")
            self.assertEqual(rc, 2 if flagged else 0, out)
            self.assertEqual("inline JPEG images" in out, flagged, out)

    # -- finding 1: form field names survive --full
    def test_full_keeps_form_field_names_and_scrubs_comment_authors(self):
        rect = b"/Rect [10 10 90 30] /P 3 0 R"
        extra = {
            5: b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (fullname) /V (Jozef) " + rect + b" >>",
            6: b"<< /Type /Annot /Subtype /Widget /FT /Btn /T (agree) /V /Off /AS /Off " + rect + b" >>",
            7: b"<< /FT /Btn /Ff 49152 /T (choice) /V /a /Kids [8 0 R 9 0 R] >>",
            8: b"<< /Type /Annot /Subtype /Widget /Parent 7 0 R /AS /a /AP << /N << /a 16 0 R /Off 16 0 R >> >> " + rect + b" >>",
            9: b"<< /Type /Annot /Subtype /Widget /Parent 7 0 R /AS /Off /AP << /N << /b 16 0 R /Off 16 0 R >> >> " + rect + b" >>",
            10: b"<< /T (address) /Kids [11 0 R] >>",  # name-only parent; /FT sits on the terminal field
            11: b"<< /Type /Annot /Subtype /Widget /Parent 10 0 R /FT /Tx /T (street) " + rect + b" >>",
            12: b"<< /FT /Sig /T (Signature1) /Kids [13 0 R] >>",
            13: b"<< /Type /Annot /Subtype /Widget /Parent 12 0 R " + rect + b" >>",
            14: b"<< /Type /Annot /Subtype /Text /T (Jozko Mrkvicka) /M (D:20240101000000) /NM (UNIQUEID) /Contents (note) "
                b"/Popup 15 0 R " + rect + b" >>",
            15: b"<< /Type /Annot /Subtype /Popup /Parent 14 0 R /T (Jozko Mrkvicka) " + rect + b" >>",
            16: stream(b"/Type /XObject /Subtype /Form /BBox [0 0 80 20]", b""),
        }
        p = self.write(document(catalog=b"/AcroForm << /Fields [5 0 R 6 0 R 7 0 R 10 0 R 12 0 R] >>",
                                page=b"/Annots [5 0 R 6 0 R 8 0 R 9 0 R 11 0 R 13 0 R 14 0 R 15 0 R]", extra=extra))
        names = self.fields(p)
        self.assertEqual(names, ["Signature1", "address", "address.street", "agree", "choice", "fullname"])
        rc, out, o = self.scrub(p, "--full")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.fields(o), names)
        data = o.read_bytes()
        self.assertIn(b"(fullname)", data)
        self.assertNotIn(b"Mrkvicka", data)  # comment and popup authors still go
        self.assertNotIn(b"UNIQUEID", data)
        annots = [a.get_object() for a in pypdf.PdfReader(o).pages[0]["/Annots"]]
        text = next(a for a in annots if a["/Subtype"] == "/Text")
        self.assertEqual(text["/Contents"], "note")
        self.assertNotIn("/M", text)

    # -- finding 2: attachments reachable only through /AF or RichMedia
    def test_attachments_via_associated_files_and_richmedia_are_reported(self):
        payload = stream(b"/Subtype /text#2Fplain /Params << /CreationDate (D:20200101000000) >>", b"ATTACHEDPAYLOAD")
        spec = b"<< /Type /Filespec /F (a.txt) /UF (a.txt) /AFRelationship /Data /EF << /F 6 0 R >> >>"
        layouts = {
            "af.pdf": document(catalog=b"/AF [5 0 R]", extra={5: spec, 6: payload}),
            "rich.pdf": document(page=b"/Annots [7 0 R]", extra={5: spec, 6: payload, 7:
                                 b"<< /Type /Annot /Subtype /RichMedia /Rect [0 0 50 50] "
                                 b"/RichMediaContent << /Assets << /Names [(a.txt) 5 0 R] >> >> >>"}),
        }
        for name, objs in layouts.items():
            p = self.write(objs, name)
            rc, out, _ = self.scrub(p, "--full", "-f")
            self.assertEqual(rc, 2, name + out)
            self.assertIn("embedded file attachments", out)
            rc, out, _ = self.scrub(p, "-f")  # default mode: a warning only
            self.assertEqual(rc, 0, name + out)
            self.assertIn("embedded file attachments", out)

    # -- finding 3: /DSS certificates go with the signature
    def test_dss_certificates_and_ocsp_removed_with_signature(self):
        extra = {
            5: b"<< /Type /Annot /Subtype /Widget /FT /Sig /T (Signature1) /V 6 0 R /Rect [0 0 0 0] /P 3 0 R >>",
            6: b"<< /Type /Sig /Filter /Adobe.PPKLite /ByteRange [0 10 20 10] /Contents <3082> /Name (SIGNERNAME) >>",
            7: b"<< /Type /DSS /Certs [8 0 R] /OCSPs [9 0 R] /VRI << /ABCDEF << /Cert [8 0 R] >> >> >>",
            8: stream(b"", b"CERTSUBJECTJOZKO jozko@example.org"),
            9: stream(b"", b"OCSPRESPONSEMARK"),
        }
        p = self.write(document(catalog=b"/AcroForm << /Fields [5 0 R] /SigFlags 3 >> /DSS 7 0 R",
                                page=b"/Annots [5 0 R]", extra=extra))
        for mode in ((), ("--full",)):
            rc, out, o = self.scrub(p, "-f", *mode)
            self.assertEqual(rc, 0, out)
            data = o.read_bytes()
            for mark in (b"SIGNERNAME", b"CERTSUBJECTJOZKO", b"jozko", b"OCSPRESPONSEMARK", b"/DSS", b"/VRI"):
                self.assertNotIn(mark, data, mark)
            self.assertIn("digital signature was removed", out)
            self.assertIn("/DSS", out)
            acro = pypdf.PdfReader(o).trailer["/Root"]["/AcroForm"]
            self.assertEqual(acro["/SigFlags"], 1)  # AppendOnly no longer applies
            self.assertEqual(self.fields(o), ["Signature1"])

    # -- finding 4: signatures found by content and through /Parent; visible appearance dropped
    def test_signature_with_inherited_type_and_visible_appearance_removed(self):
        font = b"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >>"

        def appearance(name):
            return stream(b"/Type /XObject /Subtype /Form /BBox [0 0 150 30] " + font,
                          b"BT /F1 8 Tf 2 12 Td (Digitally signed by " + name + b") Tj ET")

        def sig(name, typed):
            kind = b"/Type /Sig /ByteRange [0 10 20 10] " if typed else b""
            return b"<< " + kind + b"/Filter /Adobe.PPKLite /Contents <3082> /Name (" + name + b") /Reason (REASONMARK) /M (D:20240101120000) >>"

        widget = b"/Type /Annot /Subtype /Widget /Rect [20 %d 170 %d] /P 3 0 R /F 4"
        extra = {
            # /FT on the parent, /V on the kid, the value not recognisable by itself
            5: b"<< /FT /Sig /T (parent) /Kids [6 0 R] >>",
            6: b"<< " + widget % (20, 50) + b" /Parent 5 0 R /T (child) /V 7 0 R /AP << /N 8 0 R >> >>",
            7: sig(b"SIGNERONE", False), 8: appearance(b"SIGNERONE"),
            # no /FT at all: recognised by the signature dictionary
            9: b"<< " + widget % (70, 100) + b" /T (bare) /V 10 0 R /AP << /N 11 0 R >> >>",
            10: sig(b"SIGNERTWO", True), 11: appearance(b"SIGNERTWO"),
            # value on the field, appearance on its widget kid
            12: b"<< /FT /Sig /T (outer) /V 13 0 R /Kids [14 0 R] >>",
            13: sig(b"SIGNERTHREE", True),
            14: b"<< " + widget % (120, 150) + b" /Parent 12 0 R /AP << /N 15 0 R >> >>", 15: appearance(b"SIGNERTHREE"),
        }
        p = self.write(document(catalog=b"/AcroForm << /Fields [5 0 R 9 0 R 12 0 R] /SigFlags 3 >>",
                                page=b"/Annots [6 0 R 9 0 R 14 0 R]", extra=extra))
        names = self.fields(p)
        if TOOLS["pdftotext"]:
            text = subprocess.run(["pdftotext", str(p), "-"], capture_output=True, text=True).stdout
            self.assertIn("Digitally signed by SIGNERTHREE", text)
        for mode in ((), ("--full",)):
            rc, out, o = self.scrub(p, "-f", *mode)
            self.assertEqual(rc, 0, out)
            data = o.read_bytes()
            for mark in (b"SIGNERONE", b"SIGNERTWO", b"SIGNERTHREE", b"REASONMARK", b"Digitally"):
                self.assertNotIn(mark, data, mark)
            self.assertIn("digital signature was removed", out)
            self.assertIn("visible appearance", out)
            self.assertEqual(self.fields(o), names)
            if TOOLS["pdftotext"]:
                text = subprocess.run(["pdftotext", str(o), "-"], capture_output=True, text=True).stdout
                self.assertNotIn("Digitally signed", text)

    # -- finding 5: a legacy /Dests dictionary is not an Info copy
    def test_legacy_dests_named_like_info_keys_kept_and_thread_info_cleared(self):
        extra = {
            5: b"<< /Title [3 0 R /Fit] /Author [3 0 R /Fit] /Creator [3 0 R /XYZ 0 0 null] >>",
            6: b"<< /Type /Outlines /First 7 0 R /Last 7 0 R /Count 1 >>",
            7: b"<< /Title (Go to author) /Parent 6 0 R /Dest /Author >>",
            8: b"<< /Type /Thread /I << /Author (THREADAUTHOR) /Title (t) >> /F 9 0 R >>",
            9: b"<< /Type /Bead /T 8 0 R /N 9 0 R /V 9 0 R /P 3 0 R /R [0 0 100 100] >>",
        }
        p = self.write(document(catalog=b"/Dests 5 0 R /Outlines 6 0 R /Threads [8 0 R]", page=b"/B [9 0 R]", extra=extra))
        rc, out, o = self.scrub(p)
        self.assertEqual(rc, 0, out)
        r = pypdf.PdfReader(o)
        self.assertEqual(sorted(r.trailer["/Root"]["/Dests"]), ["/Author", "/Creator", "/Title"])
        self.assertEqual(r.get_destination_page_number(r.outline[0]), 0)
        self.assertNotIn(b"THREADAUTHOR", o.read_bytes())  # a real Info copy is still emptied
        self.assertIn("/V", r.trailer["/Root"]["/Threads"][0]["/F"])  # a bead's /V is not a signature
        if TOOLS["pdfinfo"]:
            dests = subprocess.run(["pdfinfo", "-dests", str(o)], capture_output=True, text=True).stdout
            for name in ("Author", "Creator", "Title"):
                self.assertIn(f'"{name}"', dests)

    # -- finding 6: catalog and pages without /Type
    def test_untyped_catalog_and_pages_cleaned_and_untyped_orphan_reported(self):
        objs = {
            1: b"<< /Pages 2 0 R /Outlines 7 0 R /PieceInfo << /App << /Private (CATALOGPRIVATE) >> >> "
               b"/LastModified (D:20200101000000) /Perms << /DocMDP 9 0 R >> >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R "
               b"/PieceInfo << /App << /Private (PAGEPRIVATE) >> >> /LastModified (D:20200202000000) >>",
            4: stream(b"", b""),
            5: b"<< /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 6 0 R >>",  # deleted page, no /Type
            6: stream(b"", b"BT /F1 12 Tf 10 10 Td (DELETEDPAGESECRET) Tj ET"),
            7: b"<< /Type /Outlines /First 8 0 R /Last 8 0 R /Count 1 >>",
            8: b"<< /Title (old page) /Parent 7 0 R /Dest [5 0 R /Fit] >>",
            9: b"<< /Type /Sig /Name (PERMSSIGNER) /Contents <3082> >>",
        }
        p = self.write(objs)
        rc, out, o = self.scrub(p, "--full")
        self.assertEqual(rc, 2, out)
        self.assertIn("not in the page tree", out)
        data = o.read_bytes()
        for mark in (b"CATALOGPRIVATE", b"PAGEPRIVATE", b"PERMSSIGNER", b"20200101000000", b"20200202000000"):
            self.assertNotIn(mark, data, mark)
        self.assertEqual(len(pypdf.PdfReader(o).pages), 1)
        if TOOLS["pdfinfo"]:
            info = subprocess.run(["pdfinfo", str(o)], capture_output=True, text=True).stdout
            self.assertRegex(info, r"Pages:\s+1")

    # -- finding 7: optional-content configurations lose /Creator by position
    def test_optional_content_configuration_creator_removed(self):
        extra = {
            5: b"<< /Type /OCG /Name (Layer1) /Usage << /CreatorInfo << /Creator (USAGECREATOR) /Subtype /Artwork >> >> >>",
            6: b"<< /Name (Web) /Creator (CFGAS) /AS [<< /Event /View /Category [/Zoom] /OCGs [5 0 R] >>] >>",
        }
        oc = (b"/OCProperties << /OCGs [5 0 R] /D << /Order [5 0 R] /Creator (DCREATOR) >> "
              b"/Configs [<< /Name (Print) /Creator (CFGONLYNAME) /Intent /Design >> 6 0 R] >>")
        p = self.write(document(catalog=oc, page=b"/Resources << /Properties << /L1 5 0 R >> >>", extra=extra))
        rc, out, o = self.scrub(p)
        self.assertEqual(rc, 0, out)
        data = o.read_bytes()
        for mark in (b"DCREATOR", b"CFGONLYNAME", b"CFGAS", b"USAGECREATOR"):
            self.assertNotIn(mark, data, mark)
        props = pypdf.PdfReader(o).trailer["/Root"]["/OCProperties"]
        self.assertEqual([c.get_object()["/Name"] for c in props["/Configs"]], ["Print", "Web"])
        self.assertEqual(len(props["/D"]["/Order"]), 1)
        self.assertEqual(props["/OCGs"][0].get_object()["/Name"], "Layer1")


if __name__ == "__main__":
    unittest.main()
