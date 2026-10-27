"""Rerunning over a folder is not an error; damaged PDFs do not leak pypdf's log lines to the terminal."""
import importlib.util
import unittest

from tests.test_metawipe import Base, run
from tests.test_regressions import png_bytes


class RerunTests(Base):
    def test_second_run_over_a_folder_skips_finished_files(self):
        (self.dir / "a.png").write_bytes(png_bytes([(b"tEXt", b"k\x00x")]))
        rc, out, err = run(self.dir)
        self.assertEqual((rc, err), (0, ""), out)
        rc, out, err = run("-v", self.dir)
        self.assertEqual((rc, err), (0, ""), out)
        self.assertIn("already exists", out)
        self.assertIn("0 cleaned, 0 failed, 2 skipped", out)
        self.assertEqual(run("--force", self.dir)[0], 0)

    def test_naming_the_file_still_fails_when_the_output_exists(self):
        a = self.dir / "a.png"
        a.write_bytes(png_bytes([(b"tEXt", b"k\x00x")]))
        self.assertEqual(run(a)[0], 0)
        rc, out, err = run(a)
        self.assertEqual(rc, 1)
        self.assertIn("already exists", err)


@unittest.skipUnless(importlib.util.find_spec("pypdf"), "pypdf missing")
class PdfLogTests(Base):
    def test_damaged_pdf_prints_only_our_error(self):
        bad = self.dir / "bad.pdf"
        bad.write_bytes(b"%PDF-1.4\nbroken junk\n")
        rc, out, err = run(bad)
        self.assertEqual(rc, 1)
        self.assertEqual(err.count("\n"), 1, err)
        self.assertTrue(err.startswith("error  "), err)


if __name__ == "__main__":
    unittest.main()
