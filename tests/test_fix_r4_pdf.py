"""Regression tests for the round-4 PDF findings (one test per finding)."""
import io
import logging
import random
import re
import time
import unittest
import zlib
from pathlib import Path

from metawipe.common import Context, WipeError
from metawipe.pdf import clean_pdf
from tests.test_fix_pdf import build, document, pypdf, stream
from tests.test_metawipe import Base, run


@unittest.skipIf(pypdf is None, "pypdf missing")
class PdfRound4(Base):
    def write(self, objs: dict, name="a.pdf") -> Path:
        p = self.dir / name
        p.write_bytes(build(objs))
        return p

    def clean_to_out(self, path: Path, full: bool) -> Path:
        out = self.dir / "o.pdf"
        clean_pdf(path, out, Context(warn=lambda m: None, full=full))
        return out

    # -- finding 1: --full dropped the identity keys of everything except form widgets
    def test_full_scrubs_dates_and_ids_on_widgets_but_keeps_field_names(self):
        rect = b"/Rect [10 10 90 30] /P 3 0 R"
        identity = b"/M (D:20240101000000) /NM (UNIQUE-NM) /Subj (SECRETSUBJ) /CreationDate (D:20230202000000) "
        extra = {
            5: b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (fullname) /TU (Your name) /V (Jozef) /Contents (hint) " + identity + rect + b" >>",
            6: b"<< /FT /Btn /T (choice) /V /a /Kids [7 0 R 8 0 R] " + identity + b">>",
            7: b"<< /Type /Annot /Subtype /Widget /Parent 6 0 R /AS /a " + identity + rect + b" >>",
            8: b"<< /Type /Annot /Subtype /Widget /Parent 6 0 R /AS /Off " + identity + rect + b" >>",
            9: b"<< /T (group) /Kids [10 0 R] " + identity + b">>",  # name-only intermediate node
            10: b"<< /Type /Annot /Subtype /Widget /Parent 9 0 R /FT /Tx /T (leaf) " + identity + rect + b" >>",
        }
        p = self.write(document(catalog=b"/AcroForm << /Fields [5 0 R 6 0 R 9 0 R] >>",
                                page=b"/Annots [5 0 R 7 0 R 8 0 R 10 0 R]", extra=extra))
        names = sorted(pypdf.PdfReader(p).get_fields())
        self.assertEqual(names, ["choice", "fullname", "group", "group.leaf"])
        o = self.clean_to_out(p, full=True)
        data = o.read_bytes()
        for secret in (b"UNIQUE-NM", b"SECRETSUBJ", b"20240101000000", b"20230202000000"):
            self.assertNotIn(secret, data)
        for key in (b"/M ", b"/NM", b"/Subj", b"/CreationDate"):
            self.assertNotIn(key, data)
        self.assertEqual(sorted(pypdf.PdfReader(o).get_fields()), names)
        widget = pypdf.PdfReader(o).pages[0]["/Annots"][0].get_object()
        self.assertEqual((widget["/T"], widget["/TU"], widget["/V"], widget["/Contents"]),
                         ("fullname", "Your name", "Jozef", "hint"))

    def test_default_mode_leaves_widget_dates_alone(self):
        extra = {5: b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (a) /M (D:20240101000000) /Rect [1 1 5 5] /P 3 0 R >>"}
        p = self.write(document(catalog=b"/AcroForm << /Fields [5 0 R] >>", page=b"/Annots [5 0 R]", extra=extra))
        self.assertIn(b"20240101000000", self.clean_to_out(p, full=False).read_bytes())

    # -- finding 2: an unusable /Root must be a WipeError, never another exception
    def test_unresolvable_root_is_a_clean_error(self):
        good = build(document())
        for name, root in (("missing object", b"/Root 9 0 R"), ("number", b"/Root 42"), ("array", b"/Root [1 0 R]"),
                           ("stream", b"/Root 4 0 R"), ("absent", b"")):
            (self.dir / "a.pdf").write_bytes(good.replace(b"/Root 1 0 R", root))
            for full in (False, True):
                with self.subTest(root=name, full=full), self.assertRaises(WipeError) as cm:
                    self.clean_to_out(self.dir / "a.pdf", full)
                self.assertIn("cannot process PDF", str(cm.exception))
        rc, out, err = run("-o", self.dir / "out", self.dir / "a.pdf")
        self.assertNotEqual(rc, 0)
        self.assertFalse((self.dir / "out" / "a.pdf").exists())

    def test_fuzzed_pdfs_only_ever_raise_wipeerror(self):
        rect = b"/Rect [10 10 90 30] /P 3 0 R"
        extra = {
            5: b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (name) /V (x) /M (D:2024) " + rect + b" >>",
            6: b"<< /FT /Btn /T (c) /Kids [7 0 R] >>",
            7: b"<< /Type /Annot /Subtype /Widget /Parent 6 0 R /NM (n) " + rect + b" >>",
            8: b"<< /Title (Chapter) /Parent 9 0 R /Dest [3 0 R /Fit] >>",
            9: b"<< /Type /Outlines /First 8 0 R /Last 8 0 R /Count 1 >>",
            10: stream(b"/Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8", b"\x00"),
        }
        content = b"q BI /W 1 /H 1 /F /DCT ID abc\nEI Q"
        objs = document(catalog=b"/AcroForm << /Fields [5 0 R 6 0 R] >> /Outlines 9 0 R",
                        page=b"/Annots [5 0 R 7 0 R] /Resources << /XObject << /I 10 0 R >> >>", extra=extra, content=content)
        objs[4] = stream(b"/Filter /FlateDecode", zlib.compress(content))
        base = build(objs)
        rnd = random.Random(4)
        src = self.dir / "f.pdf"
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        start = time.time()
        for i in range(250):
            data = bytearray(base)
            if i % 5 == 0:
                data = data[:rnd.randrange(len(data))]
            elif i % 5 == 1:  # damage the numbers of the structural keys
                for m in rnd.sample(list(re.finditer(rb"/Root|/Pages|/Kids|/Parent|/Size|/Annots", bytes(base))), 2):
                    data[m.end() + rnd.randrange(1, 4)] = rnd.choice(b"0123456789 /[<")
            else:
                for _ in range(rnd.choice((1, 2, 6, 20))):
                    data[rnd.randrange(len(data))] = rnd.randrange(256)
            src.write_bytes(bytes(data))
            for full in (False, True):
                try:
                    self.clean_to_out(src, full)
                except WipeError:
                    pass
        self.assertLess(time.time() - start, 60)

    # -- finding 3: the inline-image detector must stay linear
    def test_inline_jpeg_scan_is_linear_on_bi_heavy_streams(self):
        content = b"BI " * 480000 + b"/DCT"  # about 1.4 MB, nearly 500k BI words and no ID
        p = self.write(document(content=content))
        start = time.time()
        o = self.clean_to_out(p, full=True)
        self.assertLess(time.time() - start, 5)  # the quadratic scan needed minutes here
        self.assertTrue(o.exists())
        # the same size with a real inline JPEG at the end is still found
        jpeg = content + b"\nq BI /W 8 /H 8 /F /DCT ID abc\nEI Q"
        ctx = Context(warn=lambda m: None, full=True)
        clean_pdf(self.write(document(content=jpeg), "b.pdf"), self.dir / "o2.pdf", ctx)
        self.assertTrue(any("inline JPEG" in m for m in ctx.residue), ctx.residue)

    def test_inline_jpeg_detection_boundaries(self):
        from metawipe import pdf
        for content, flagged in (
                (b"q BI /F /DCT ID x EI Q", True),
                (b"q BI /F [/A85 /DCTDecode] ID x EI Q", True),
                (b"q BI /F /Fl ID /DCT EI Q", False),  # /DCT only in the data
                (b"q BI /F /DCT\nID\nx EI Q", True),
                (b"BI ID /DCT x", False),  # the whitespace after BI cannot double as the one before ID
                (b"q BI /F /DCT " + b"x" * 5000 + b" ID x", False),  # dictionary longer than any real one
                (b"qBI /F /DCT ID x", False),  # BI must be a token of its own
                (b"BI /A BI /F /DCT ID x", True),
                (b"BI /W 1 ID abc EI BI /F /DCT ID x EI", True)):
            data = zlib.compress(content)
            obj = pypdf.generic.StreamObject()
            obj._data = data
            obj[pypdf.generic.NameObject("/Filter")] = pypdf.generic.NameObject("/FlateDecode")
            obj[pypdf.generic.NameObject("/Subtype")] = pypdf.generic.NameObject("/Form")
            with self.subTest(content=content[:40]):
                self.assertEqual(pdf._inline_jpeg(obj, False), flagged)


if __name__ == "__main__":
    unittest.main()
