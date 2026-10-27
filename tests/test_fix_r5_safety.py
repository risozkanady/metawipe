"""Regression tests for round-5 F13: safety checks that no test caught when they were mutated away."""
import os
import unittest
from unittest import mock

from metawipe import pdf
from tests.test_fix_office import Fix, rels
from tests.test_fix_pdf import build, document, pypdf, stream
from tests import test_metawipe as tm  # not CliTests by name: unittest would run its tests here again
from tests.test_metawipe import SECRET, Base, run


class SafetyChecks(Base):
    @unittest.skipIf(not hasattr(os, "link"), "hard links")
    def test_in_place_refuses_a_file_with_other_hard_links(self):
        real, other = self.dir / "real.png", self.dir / "other.png"
        tm.CliTests.png(self, real)
        os.link(real, other)
        original = real.read_bytes()
        rc, _, err = run("-i", real)
        self.assertEqual(rc, 1, err)
        self.assertIn("hard links", err)
        self.assertEqual(real.read_bytes(), original)
        self.assertEqual(other.read_bytes(), original)

    def test_output_that_is_also_an_input_is_not_overwritten(self):
        a, user = self.dir / "a.png", self.dir / "a.clean.png"
        tm.CliTests.png(self, a)
        tm.CliTests.png(self, user)
        original = user.read_bytes()
        rc, _, err = run("-f", a, user)
        self.assertEqual(rc, 1, err)
        self.assertIn("is also an input", err)
        self.assertEqual(user.read_bytes(), original)


class OleEmbeddingIsLeftover(Fix):
    def test_full_reports_ole_embedding_as_incomplete(self):
        doc = '<w:document %s><w:body><w:p/></w:body></w:document>' % 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        p = self.docx("a.docx", [("word/document.xml", doc), ("word/_rels/document.xml.rels", rels()),
                                 ("word/embeddings/oleObject1.bin", b"\xd0\xcf\x11\xe0OLE")])
        rc, out, err = run("--full", p)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("INCOMPLETE", out)


@unittest.skipIf(pypdf is None, "pypdf missing")
class PdfVerification(Base):
    def test_metadata_left_by_strip_fails_verification(self):
        xmp = stream(b"/Type /Metadata /Subtype /XML", b"<x:xmpmeta>" + SECRET + b"</x:xmpmeta>")
        p = self.dir / "a.pdf"
        p.write_bytes(build(document(catalog=b"/Metadata 5 0 R", extra={5: xmp})))
        with mock.patch.object(pdf, "_strip", return_value=(set(), set())):
            rc, out, err = run(p)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("verification failed", err)
        self.assertFalse((self.dir / "a.clean.pdf").exists())


if __name__ == "__main__":
    unittest.main()
