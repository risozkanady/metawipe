"""--keep for PDF: only the whitelisted Info keys (validated) come back; the rest stays removed."""
import contextlib
import io
import unittest

from metawipe import pdf
from metawipe.common import Context, WipeError
from tests.test_metawipe import Base, run

try:
    import pypdf
    from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject,
                               NumberObject, TextStringObject)
except ImportError:  # pragma: no cover
    pypdf = None

INFO = {"/Title": "Report", "/Author": "Jane Doe", "/Subject": "Quarterly", "/Keywords": "a, b", "/Creator": "Writer",
        "/Producer": "LibreOffice", "/CreationDate": "D:20200102030405+01'00'", "/ModDate": "D:20210102030405Z",
        "/Trapped": "/False", "/Evil": "secret"}


@unittest.skipIf(pypdf is None, "pypdf missing")
class KeepPdf(Base):
    def make(self, info=None, thumb=False, annot=False, password=None, name="a.pdf", xmp=True):
        w = pypdf.PdfWriter()
        page = w.add_blank_page(100, 100)
        w.add_metadata(INFO if info is None else info)
        if xmp:
            x = DecodedStreamObject()
            x.set_data(b"<x:xmpmeta>secret</x:xmpmeta>")
            x.update({NameObject("/Type"): NameObject("/Metadata"), NameObject("/Subtype"): NameObject("/XML")})
            w._root_object[NameObject("/Metadata")] = w._add_object(x)
        if thumb:
            t = DecodedStreamObject()
            t.set_data(b"\x80" * 4)
            t.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"), NameObject("/Width"): NumberObject(2),
                      NameObject("/Height"): NumberObject(2), NameObject("/ColorSpace"): NameObject("/DeviceGray"), NameObject("/BitsPerComponent"): NumberObject(8)})
            page[NameObject("/Thumb")] = w._add_object(t)
        if annot:
            a = DictionaryObject({NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Text"),
                                  NameObject("/Rect"): ArrayObject([FloatObject(0), FloatObject(0), FloatObject(10), FloatObject(10)]),
                                  NameObject("/T"): TextStringObject("Jane"), NameObject("/M"): TextStringObject("D:20200102030405"),
                                  NameObject("/CreationDate"): TextStringObject("D:20200102030405"), NameObject("/NM"): TextStringObject("uid"),
                                  NameObject("/Subj"): TextStringObject("subj"), NameObject("/Contents"): TextStringObject("hi")})
            page[NameObject("/Annots")] = ArrayObject([w._add_object(a)])
        if password:
            w.encrypt(password)
        p = self.dir / name
        with open(p, "wb") as fh:
            w.write(fh)
        return p

    def info(self, path):
        r = pypdf.PdfReader(path)
        if r.is_encrypted:
            r.decrypt("")
        return {k: str(v) for k, v in (r.trailer.get("/Info") or {}).items()}

    def annot(self, path):
        a = pypdf.PdfReader(path).pages[0]["/Annots"][0].get_object()
        return set(a.keys())

    def test_each_category_brings_back_only_its_keys(self):
        for cat, keys in pdf._KEEP_INFO.items():
            with self.subTest(cat):
                p = self.make()
                self.clean(p, "--keep", cat)
                self.assertEqual(self.info(p), {k: INFO[k] for k in keys})
                self.assertNotIn("/Metadata", pypdf.PdfReader(p).trailer["/Root"])

    def test_all_keeps_the_eight_keys_and_nothing_else(self):
        p = self.make()
        rc, out, err = run("--keep", "all", "-i", p)
        self.assertEqual(rc, 0, err)
        self.assertEqual(set(self.info(p)), {k for ks in pdf._KEEP_INFO.values() for k in ks})
        self.assertIn("(kept: dates, software, people, descriptions)", out)
        self.assertNotIn(b"secret", p.read_bytes())

    def test_default_is_still_bare(self):
        p = self.make()
        self.clean(p)
        self.assertEqual(self.info(p), {})

    def test_bad_values_are_dropped(self):
        bad = {"/CreationDate": "D:20201341000000", "/ModDate": "yesterday", "/Author": "x\x00y", "/Title": "t" * 2001,
               "/Subject": "ok", "/Creator": "c" * 257, "/Producer": "line\nbreak"}
        p = self.make(info=bad)
        self.clean(p, "--keep", "all")
        self.assertEqual(self.info(p), {"/Subject": "ok"})
        p = self.make(info={"/Title": "t" * 2000, "/Creator": "c" * 256, "/CreationDate": "D:2020"}, name="b.pdf")
        self.clean(p, "--keep", "all")
        self.assertEqual(set(self.info(p)), {"/Title", "/Creator", "/CreationDate", "/Producer"})  # Producer: the fixture's own pypdf default

    def test_thumbnail_only_when_kept(self):
        p = self.make(thumb=True)
        self.clean(p)
        self.assertNotIn("/Thumb", pypdf.PdfReader(p).pages[0])
        p = self.make(thumb=True, name="b.pdf")
        rc, out, err = run("--keep", "thumbnails", "-i", p)
        self.assertIn("/Thumb", pypdf.PdfReader(p).pages[0])
        self.assertEqual(self.info(p), {})
        self.assertIn("(kept: thumbnails)", out)

    def test_dct_thumbnail_is_cleaned_without_full(self):
        from tests.test_keep_images import jpeg, sample
        p = self.make(thumb=True)
        w = pypdf.PdfWriter(clone_from=p)
        t = w.pages[0]["/Thumb"].get_object()
        t.set_data(jpeg(sample(), b"\xff\xfe\x00\x0bCOMMENT-X"))
        t[NameObject("/Filter")] = NameObject("/DCTDecode")
        with open(p, "wb") as fh:
            w.write(fh)
        self.assertIn(b"ACME", p.read_bytes())
        rc, out, err = run("--keep", "thumbnails", "-i", p)
        data = p.read_bytes()
        self.assertIn(b"/Thumb", data)
        for leak in (b"ACME", b"COMMENT-X", b"Jane", b"SECRET"):
            self.assertNotIn(leak, data)

    def test_uncleanable_thumbnail_is_dropped(self):
        p = self.make(thumb=True)
        w = pypdf.PdfWriter(clone_from=p)
        t = w.pages[0]["/Thumb"].get_object()
        t.set_data(b"\xff\xd8\xff\xe1\x00\x0aExif--ACME garbage")
        t[NameObject("/Filter")] = NameObject("/DCTDecode")
        with open(p, "wb") as fh:
            w.write(fh)
        rc, out, err = run("--keep", "thumbnails", "-i", p)
        self.assertNotIn(b"ACME", p.read_bytes())
        self.assertIn("thumbnails not kept", err)
        self.assertNotIn("(kept: thumbnails)", out)

    def test_blank_and_bidi_text_is_dropped(self):
        p = self.make(info={"/Title": " ", "/Author": "a\u202ebob", "/Subject": "ok"})
        self.clean(p, "--keep", "descriptions,people")
        self.assertEqual(self.info(p), {"/Subject": "ok"})

    def test_non_ascii_digits_are_not_a_date(self):
        self.assertFalse(pdf._date_ok("D:\u0662\u0660\u0662\u0660"))
        self.assertTrue(pdf._date_ok("D:2020"))

    def test_full_keeps_annotation_dates_and_author_only_when_asked(self):
        for keep, left in (("dates", {"/M", "/CreationDate"}), ("people", {"/T"}), ("none", set())):
            with self.subTest(keep):
                p = self.make(annot=True)
                self.clean(p, "--full", "--keep", keep) if keep != "none" else self.clean(p, "--full")
                self.assertEqual(self.annot(p) & {"/T", "/M", "/CreationDate", "/NM", "/Subj"}, left)

    def test_encrypted_pdf(self):
        p = self.make(password="pw")
        rc, out, err = run("--password", "pw", "--keep", "people", "-i", p)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.info(p), {"/Author": "Jane Doe"})

    def test_verify_accepts_only_allowed_info_keys(self):
        p = self.make(info={"/Title": "t", "/Author": "a"}, xmp=False)
        pdf._verify(p, {"/Title", "/Author", "/Producer"})
        with self.assertRaises(WipeError):
            pdf._verify(p, {"/Title", "/Producer"})
        with self.assertRaises(WipeError):
            pdf._verify(p)


if __name__ == "__main__":
    unittest.main()
