"""Regression tests for the round-3 office findings (test_fNN_* = finding index NN).

Packages are assembled byte for byte here; results are read back with unzip/ET/ffprobe instead of the
code that produced them wherever those are installed.
"""
import io
import re
import shutil
import subprocess
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile

from metawipe import office
from metawipe.common import Context, WipeError
from tests.test_metawipe import Base, SECRET, run

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
CT = '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/>%s</Types>'
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OD = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
HAVE_UNZIP = shutil.which("unzip") is not None
HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
ODF_NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
          'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0"')


def b(s):
    return s if isinstance(s, bytes) else s.encode()


def rels(*items):
    """items: (Id, Type suffix or full type, Target[, 'External'])"""
    out = '<Relationships xmlns="%s">' % REL_NS
    for it in items:
        typ = it[1] if it[1].startswith("http") else OD + "/" + it[1]
        mode = ' TargetMode="External"' if len(it) > 3 else ""
        out += '<Relationship Id="%s" Type="%s" Target="%s"%s/>' % (it[0], typ, it[2], mode)
    return out + "</Relationships>"


class Fix(Base):
    CONTENT = ('<?xml version="1.0" encoding="UTF-8"?><office:document-content %s office:version="1.3"><office:body><office:text>'
               '<office:annotation><dc:creator>Jozko Mrkvicka</dc:creator><dc:date>2024-04-04T10:00:00</dc:date>'
               '<meta:date-string>4.4.2024</meta:date-string><text:p>a comment 2024</text:p></office:annotation>'
               '<text:p>body 1</text:p><text:author-name>Jozko</text:author-name>'
               '<text:tracked-changes><text:changed-region><text:insertion><office:change-info><dc:creator>Jozko</dc:creator>'
               '<dc:date>2024-05-05T10:00:00</dc:date></office:change-info></text:insertion></text:changed-region></text:tracked-changes>'
               '</office:text></office:body></office:document-content>') % ODF_NS
    MANIFEST = ('<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
                '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>'
                '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/></manifest:manifest>')

    def odt(self, name="a.odt", content=None, extra=()):
        return self.zip(self.dir / name, [("mimetype", "application/vnd.oasis.opendocument.text"),
                                          ("content.xml", content or self.CONTENT), ("styles.xml", content or self.CONTENT),
                                          ("META-INF/manifest.xml", self.MANIFEST)] + list(extra), stored=("mimetype",))

    def zip(self, path, entries, stored=()):
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in entries:
                zi = zipfile.ZipInfo(name, date_time=(2021, 5, 4, 3, 2, 1))
                zi.compress_type = zipfile.ZIP_STORED if name in stored else zipfile.ZIP_DEFLATED
                z.writestr(zi, b(data))
        return path

    def read(self, path, name):
        with zipfile.ZipFile(path) as z:
            return z.read(name)

    def names(self, path):
        with zipfile.ZipFile(path) as z:
            return z.namelist()

    def docx(self, name, parts, ct_extra="", root_rels=None):
        root_rels = root_rels or rels(("r1", "officeDocument", "word/document.xml"))
        entries = [("[Content_Types].xml", CT % ct_extra), ("_rels/.rels", root_rels)] + list(parts)
        return self.zip(self.dir / name, entries)

    def assertZipIntact(self, path):
        """Container check with an independent tool (unzip -t)."""
        if HAVE_UNZIP:
            r = subprocess.run(["unzip", "-tq", str(path)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def assertWellFormed(self, path):
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.lower().endswith((".xml", ".rels")):
                    try:
                        ET.fromstring(z.read(n))
                    except ET.ParseError as e:
                        self.fail(f"{n} is not well-formed: {e}")

    def assertRelIdsResolve(self, path, part, rel_part):
        """Every r:* attribute value in `part` names a relationship of `rel_part` (checked with ET, not regexes)."""
        ids = {r.get("Id") for r in ET.fromstring(self.read(path, rel_part))}
        for el in ET.fromstring(self.read(path, part)).iter():
            for k, v in el.attrib.items():
                if k.startswith("{" + OD + "}"):
                    self.assertIn(v, ids, f"{part}: dangling {k}={v}")


class F00OdfDate(Fix):
    def test_f00_dc_date_keeps_content_xml_well_formed(self):
        p = self.odt(extra=[("Object 1/content.xml", self.CONTENT)])
        rc, out, err = run("--full", "-i", p)
        self.assertEqual(rc, 0, out + err)
        for part in ("content.xml", "styles.xml", "Object 1/content.xml"):
            got = self.read(p, part)
            root = ET.fromstring(got)  # the old rule produced 'P00-01-01...' and an unbalanced tag
            self.assertIn(b"<dc:date>2000-01-01T00:00:00Z</dc:date>", got)
            expected = (self.CONTENT.replace("Jozko Mrkvicka", "Author").replace("<dc:creator>Jozko<", "<dc:creator>Author<")
                        .replace("2024-04-04T10:00:00", "2000-01-01T00:00:00Z").replace("2024-05-05T10:00:00", "2000-01-01T00:00:00Z")
                        .replace("4.4.2024", "").replace("<text:author-name>Jozko<", "<text:author-name><"))
            self.assertEqual(got.decode(), expected, part)
            self.assertIn("a comment 2024", [e.text for e in root.iter() if e.tag.endswith("}p")])

    def test_f00_no_replacement_template_uses_bare_group_followed_by_digit(self):
        src = open(office.__file__).read()
        self.assertIsNone(re.search(r"""rb?['"]\\[1-9]['"]?\s*\+\s*_?[A-Z_]+""", src))
        self.assertIsNone(re.search(r"\\[1-9]\d", re.sub(r"\\[1-9]\)", "", src.split("_PEOPLE_RULES")[0])))


class F01DanglingRefs(Fix):
    def test_f01_docx_printer_settings_reference_removed_in_both_modes(self):
        doc = ('<w:document %s %s><w:body><w:p/><w:sectPr><w:pgSz w:w="1"/><w:printerSettings r:id="rId90"/></w:sectPr></w:body></w:document>' % (W, R))
        for mode in ((), ("--full",)):
            p = self.docx("w%d.docx" % len(mode), [
                ("word/document.xml", doc),
                ("word/_rels/document.xml.rels", rels(("rId90", "printerSettings", "printerSettings/printerSettings1.bin"),
                                                      ("rId1", "styles", "styles.xml"))),
                ("word/styles.xml", "<w:styles %s/>" % W),
                ("word/printerSettings/printerSettings1.bin", SECRET)])
            self.clean(p, *mode)
            self.assertNotIn("word/printerSettings/printerSettings1.bin", self.names(p))
            self.assertNotIn(b"printerSettings", self.read(p, "word/document.xml"))
            self.assertIn(b"w:pgSz", self.read(p, "word/document.xml"))
            self.assertRelIdsResolve(p, "word/document.xml", "word/_rels/document.xml.rels")
            self.assertWellFormed(p)

    def test_f01_xlsx_pagesetup_rid_and_zip_order(self):
        ws = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" %s><sheetData/>'
              "<pageSetup orientation='portrait' r:id = 'rId50'/><drawing r:id=\"rId2\"/></worksheet>" % R)
        # rels first, owner last (and the other way round) must both work
        for order in (0, 1):
            parts = [("xl/_rels/workbook.xml.rels", rels(("rId1", "worksheet", "worksheets/sheet1.xml"))),
                     ("xl/worksheets/_rels/sheet1.xml.rels", rels(("rId50", "printerSettings", "../printerSettings/printerSettings1.bin"),
                                                                    ("rId2", "drawing", "../drawings/drawing1.xml"))),
                     ("xl/worksheets/sheet1.xml", ws), ("xl/drawings/drawing1.xml", "<d/>"),
                     ("xl/printerSettings/printerSettings1.bin", SECRET)]
            if order:
                parts.reverse()
            p = self.docx("x%d.xlsx" % order, parts, root_rels=rels(("r1", "officeDocument", "xl/workbook.xml")))
            self.clean(p)
            sheet = self.read(p, "xl/worksheets/sheet1.xml")
            self.assertNotIn(b"rId50", sheet)
            self.assertIn(b"rId2", sheet)  # references to parts that stay are untouched
            self.assertIn(b"orientation='portrait'", sheet)
            self.assertRelIdsResolve(p, "xl/worksheets/sheet1.xml", "xl/worksheets/_rels/sheet1.xml.rels")
            self.assertWellFormed(p)

    def test_f01_malformed_owner_and_missing_target_do_not_crash(self):
        p = self.docx("m.docx", [
            ("word/document.xml", '<w:document %s %s><w:body><w:printerSettings r:id="rId9"/></w:body>' % (W, R)),  # not closed
            ("word/_rels/document.xml.rels", rels(("rId9", "printerSettings", "printerSettings/p.bin"),
                                                  ("rId8", "image", "media/missing.png"))),
            ("word/printerSettings/p.bin", SECRET)])
        rc, out, err = run("-i", p)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn(b"rId9", self.read(p, "word/document.xml"))
        self.assertIn(b"rId8", self.read(p, "word/_rels/document.xml.rels"))


class F02WordDateUtc(Fix):
    def test_f02_dateutc_removed_everywhere_in_word_parts(self):
        ns = W + ' xmlns:w16du="http://schemas.microsoft.com/office/word/2023/wordml/word16du" xmlns:w16cex="http://schemas.microsoft.com/office/word/2018/wordml/cex"'
        doc = '<w:document %s><w:body><w:ins w:id="5" w:author="Jozko" w:date="2023-05-01T08:00:00Z" w16du:dateUtc="2023-05-01T08:00:00Z"/></w:body></w:document>' % ns
        cmt = '<w:comments %s><w:comment w:id="0" w:author="Jozko" w:initials="J" w16du:dateUtc="2023-05-01T08:00:00Z"/></w:comments>' % ns
        cex = '<w16cex:commentsExtensible %s><w16cex:commentExtensible w16cex:durableId="1A2B3C4D" w16cex:dateUtc="2023-05-01T08:00:00Z"/></w16cex:commentsExtensible>' % ns
        p = self.docx("a.docx", [("word/document.xml", doc), ("word/comments.xml", cmt), ("word/commentsExtensible.xml", cex)])
        self.clean(p, "--full")
        for part in ("word/document.xml", "word/comments.xml", "word/commentsExtensible.xml"):
            data = self.read(p, part)
            self.assertNotIn(b"2023-05", data, part)
            self.assertNotIn(b"dateUtc=", data, part)
            self.assertNotIn(b"Jozko", data, part)
        self.assertIn(b'w16cex:durableId="1A2B3C4D"', self.read(p, "word/commentsExtensible.xml"))
        self.assertWellFormed(p)


class F03Label(Fix):
    def parts(self):
        label = '<clbl:labelList xmlns:clbl="http://schemas.microsoft.com/office/2020/mipLabelMetadata"><clbl:label id="{1}" siteId="{SECRETTENANT}"/></clbl:labelList>'
        root = rels(("r1", "officeDocument", "word/document.xml"),
                    ("r2", "http://schemas.microsoft.com/office/2020/02/relationships/classificationlabels", "docMetadata/LabelInfo.xml"))
        ct = '<Override PartName="/docMetadata/LabelInfo.xml" ContentType="application/vnd.ms-office.classificationlabels+xml"/>'
        return self.docx("l.docx", [("word/document.xml", "<w:document %s/>" % W), ("docMetadata/LabelInfo.xml", label)], ct, root)

    def test_f03_full_drops_label_with_relationship_and_override(self):
        p = self.parts()
        self.clean(p, "--full")
        self.assertNotIn("docMetadata/LabelInfo.xml", self.names(p))
        self.assertNotIn(b"LabelInfo", self.read(p, "_rels/.rels"))
        self.assertNotIn(b"LabelInfo", self.read(p, "[Content_Types].xml"))
        self.assertIn(b"word/document.xml", self.read(p, "_rels/.rels"))

    def test_f03_default_keeps_label_and_warns(self):
        p = self.parts()
        rc, out, err = run("-i", p)
        self.assertEqual(rc, 0)
        self.assertIn("docMetadata/LabelInfo.xml", self.names(p))
        self.assertIn("sensitivity label", err)


class F04Media(Fix):
    def pptx(self, media):
        parts = [("ppt/presentation.xml", "<p/>")] + [("ppt/media/" + n, d) for n, d in media]
        return self.docx("m.pptx", parts, root_rels=rels(("r1", "officeDocument", "ppt/presentation.xml")))

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg missing")
    def test_f04_embedded_mp4_with_gps_is_reported_incomplete_in_full_only(self):
        mp4 = self.dir / "v.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=1:s=64x64:r=10", "-c:v", "libx264",
                        "-metadata", "location=+48.8584+002.2945/", str(mp4)], check=True)
        p = self.pptx([("media1.mp4", mp4.read_bytes())])
        rc, out, err = run("-i", p)
        self.assertEqual(rc, 0, out + err)
        p = self.pptx([("media1.mp4", mp4.read_bytes())])
        rc, out, err = run("--full", "-i", p)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("media1.mp4", err)
        self.assertIn("INCOMPLETE", out + err)

    def test_f04_gif_svg_flagged_picture_cleaned_not_flagged(self):
        for ext in ("gif", "svg", "mov", "pdf"):
            p = self.pptx([("media1." + ext, b"data")])
            rc, out, err = run("--full", "-i", p)
            self.assertEqual(rc, 2, ext + out + err)
        # unknown extension and EMF are not flagged (they would make every chart document incomplete)
        p = self.pptx([("image1.emf", b"x"), ("image2.dat", b"x")])
        self.assertEqual(run("--full", "-i", p)[0], 0)


class F05NestedAnywhere(Fix):
    def inner(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("[Content_Types].xml", CT % '<Override PartName="/docProps/core.xml" ContentType="c"/>')
            z.writestr("_rels/.rels", rels(("r1", "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties", "docProps/core.xml")))
            z.writestr("docProps/core.xml", "<c>" + SECRET.decode() + "</c>")
            z.writestr("word/document.xml", "<w:document %s/>" % W)
        return buf.getvalue()

    def test_f05_altchunk_outside_embeddings_is_cleaned_in_both_modes(self):
        for mode in ((), ("--full",)):
            p = self.docx("a%d.docx" % len(mode), [("word/document.xml", "<w:document %s/>" % W), ("word/afchunk.docx", self.inner())])
            self.clean(p, *mode)
            inner = zipfile.ZipFile(io.BytesIO(self.read(p, "word/afchunk.docx")))
            self.assertNotIn("docProps/core.xml", inner.namelist())
            self.assertNotIn(SECRET, b"".join(inner.read(n) for n in inner.namelist()))
            self.assertZipIntact(p)

    def test_f05_non_zip_package_name_is_reported_not_crashing(self):
        p = self.docx("a.docx", [("word/document.xml", "<w:document %s/>" % W), ("word/afchunk.docx", b"not a zip")])
        rc, out, err = run("--full", "-i", p)
        self.assertEqual(rc, 2, out + err)
        self.assertEqual(self.read(p, "word/afchunk.docx"), b"not a zip")


class F06To10Anonymise(Fix):
    def test_f06_openpyxl_comment_layout(self):
        p = self.docx("c.xlsx", [("xl/workbook.xml", "<workbook/>"),
                                 ("xl/comments/comment1.xml", '<comments xmlns="x"><authors><author>SECRETXLCOMMENTER</author></authors><commentList/></comments>'),
                                 ("xl/threadedComments/threadedComment1.xml", '<x><threadedComment dT="2021-01-02T03:04:05.06" personId="{1}"/></x>')],
                      root_rels=rels(("r1", "officeDocument", "xl/workbook.xml")))
        self.clean(p, "--full")
        self.assertNotIn(b"SECRETXLCOMMENTER", self.read(p, "xl/comments/comment1.xml"))
        self.assertIn(b"<author>Author</author>", self.read(p, "xl/comments/comment1.xml"))
        self.assertNotIn(b"2021", self.read(p, "xl/threadedComments/threadedComment1.xml"))

    def test_f07_authors_initials(self):
        authors = ('<p188:authorLst xmlns:p188="http://schemas.microsoft.com/office/powerpoint/2018/8/main">'
                   '<p188:author id="{1}" name="SECRETMODERN Person" initials="SMP" userId="S::x@example.com::g" providerId="AD"/></p188:authorLst>')
        for mode in ((), ("--full",)):
            p = self.docx("p%d.pptx" % len(mode), [("ppt/presentation.xml", "<p/>"), ("ppt/authors.xml", authors)],
                          root_rels=rels(("r1", "officeDocument", "ppt/presentation.xml")))
            self.clean(p, *mode)
            data = self.read(p, "ppt/authors.xml")
            for needle in (b"SMP", b"SECRETMODERN", b"example.com"):
                self.assertNotIn(needle, data)
            self.assertIn(b'initials="A"', data)

    def test_f08_shared_workbook_revisions(self):
        hdr = ('<headers xmlns="x" guid="{1}"><header guid="{2}" dateTime="2021-03-04T10:00:00" maxSheetId="1" userName="SECRETREVUSER" minRId="1" maxRId="2"/></headers>')
        usr = '<users xmlns="x" count="1"><userInfo guid="{3}" name="SECRETREVUSER" id="0" dateTime="2021-03-04T10:00:00"/></users>'
        log = '<revisions xmlns="x"><rcc rId="1" sId="1"><nc r="A1"><v>SECRETREVUSER stays as cell content</v></nc></rcc><rdn rId="2" localSheetId="0" customView="0" name="MyDefinedName" id="1"/><rcmt rId="3" sId="1" cell="A1" guid="{4}" author="SECRETREVUSER"/></revisions>'
        p = self.docx("r.xlsx", [("xl/workbook.xml", "<workbook/>"), ("xl/revisions/revisionHeaders.xml", hdr),
                                 ("xl/revisions/userNames.xml", usr), ("xl/revisions/revisionLog1.xml", log)],
                      root_rels=rels(("r1", "officeDocument", "xl/workbook.xml")))
        self.clean(p, "--full")
        for part in ("xl/revisions/revisionHeaders.xml", "xl/revisions/userNames.xml"):
            data = self.read(p, part)
            self.assertNotIn(b"SECRETREVUSER", data)
            self.assertNotIn(b"2021", data)
        log_out = self.read(p, "xl/revisions/revisionLog1.xml")
        self.assertIn(b'name="MyDefinedName"', log_out)  # a defined name is content, not a person
        self.assertIn(b"stays as cell content", log_out)
        self.assertIn(b'author="Author"', log_out)
        self.assertWellFormed(p)

    def test_f09_mail_merge_source_is_reported(self):
        settings = "<w:settings %s %s><w:mailMerge><w:dataSource r:id=\"rId1\"/></w:mailMerge></w:settings>" % (W, R)
        p = self.docx("m.docx", [("word/document.xml", "<w:document %s/>" % W), ("word/settings.xml", settings),
                                 ("word/_rels/settings.xml.rels", rels(("rId1", "mailMergeSource", "file:///C:\\Users\\SECRETUSER\\list.xlsx", "External")))])
        rc, out, err = run("--full", "-i", p)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("mail-merge", err)
        p = self.docx("m2.docx", [("word/document.xml", "<w:document %s/>" % W), ("word/settings.xml", settings),
                                  ("word/_rels/settings.xml.rels", rels(("rId1", "mailMergeSource", "file:///C:\\Users\\SECRETUSER\\list.xlsx", "External")))])
        rc, out, err = run("-i", p)  # default mode: warns, exit 0 (only --full promises completeness)
        self.assertEqual(rc, 0)
        self.assertIn("mail-merge", err)

    def test_f09_connect_string_without_relationship_is_reported(self):
        settings = ('<w:settings %s><w:mailMerge><w:connectString w:val="Data Source=C:\\Users\\SECRETUSER\\a.xlsx;"/></w:mailMerge></w:settings>' % W)
        p = self.docx("c.docx", [("word/document.xml", "<w:document %s/>" % W), ("word/settings.xml", settings)])
        rc, out, err = run("--full", "-i", p)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("connection string", err)

    def test_f10_odf_sender_fields_and_initials(self):
        content = ('<office:document-content %s><office:body><office:text><text:p>'
                   '<text:sender-firstname>SECRETSENDERFN</text:sender-firstname><text:sender-lastname>SECRETSENDERLN</text:sender-lastname>'
                   '<text:sender-email>secret@example.com</text:sender-email><text:sender-phone-private>555</text:sender-phone-private>'
                   '<text:initial-creator>Jozko</text:initial-creator><text:creator>Jozko</text:creator>'
                   '<text:author-initials>JM</text:author-initials><meta:creator-initials>SOI</meta:creator-initials>'
                   '<text:sender-company foo="a>b">Corp</text:sender-company> kept </text:p></office:text></office:body></office:document-content>') % ODF_NS
        p = self.odt(content=content)
        self.clean(p, "--full")
        got = self.read(p, "content.xml")
        ET.fromstring(got)
        for needle in (b"SECRET", b"secret@", b"Jozko", b"JM", b"SOI", b"555", b"Corp"):
            self.assertNotIn(needle, got)
        self.assertIn(b"kept", got)


class F11Encrypted(Fix):
    def test_f11_file_named_encryption_data_is_not_encrypted(self):
        manifest = ('<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
                    '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>'
                    '<manifest:file-entry manifest:full-path="Pictures/encryption-data.png" manifest:media-type="image/png"/></manifest:manifest>')
        p = self.zip(self.dir / "a.odt", [("mimetype", "application/vnd.oasis.opendocument.text"), ("content.xml", "<c/>"),
                                          ("Pictures/encryption-data.png", b"png"), ("META-INF/manifest.xml", manifest)], stored=("mimetype",))
        self.clean(p)

    def test_f11_real_encryption_data_with_prefix_is_still_refused(self):
        manifest = ('<m:manifest xmlns:m="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"><m:file-entry m:full-path="content.xml">'
                    '<m:encryption-data m:checksum-type="x"/></m:file-entry></m:manifest>')
        p = self.zip(self.dir / "e.odt", [("mimetype", "application/vnd.oasis.opendocument.text"), ("content.xml", "x"),
                                          ("META-INF/manifest.xml", manifest)])
        before = p.read_bytes()
        rc, out, err = run("-i", p)
        self.assertEqual(rc, 1)
        self.assertEqual(p.read_bytes(), before)


class F12PartNames(Fix):
    def core(self, name, target, entry):
        root = rels(("r1", "officeDocument", "word/document.xml"),
                    ("r2", "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties", target))
        return self.zip(self.dir / name, [("[Content_Types].xml", CT % ""), ("_rels/.rels", root), ("word/document.xml", "<d/>"),
                                          (entry, "<c>" + SECRET.decode() + "</c>")])

    def test_f12_percent_encoded_and_differently_cased_targets(self):
        for name, target, entry in (("pct.docx", "meta%20data/my%20props.xml", "meta data/my props.xml"),
                                    ("case.docx", "/meta/my.xml", "Meta/My.xml")):
            p = self.core(name, target, entry)
            self.clean(p)
            self.assertEqual(self.names(p), ["[Content_Types].xml", "_rels/.rels", "word/document.xml"], name)
            self.assertZipIntact(p)

    def test_f12_backslash_entry_names(self):
        entries = [("[Content_Types].xml", CT % '<Override PartName="/docProps/core.xml" ContentType="c"/>'),
                   ("_rels\\.rels", rels(("r1", "officeDocument", "word/document.xml"),
                                          ("r2", "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties", "docProps/core.xml"))),
                   ("word\\document.xml", "<d/>"), ("docProps\\core.xml", "<c>SECRET</c>"), ("docProps\\app.xml", "<c>SECRET</c>"),
                   ("docProps\\thumbnail.jpeg", "SECRET")]
        p = self.zip(self.dir / "b.docx", entries)
        self.clean(p)
        names = self.names(p)
        self.assertFalse([n for n in names if "docProps" in n], names)
        self.assertNotIn(b"core.xml", self.read(p, "_rels\\.rels"))
        self.assertNotIn(b"core.xml", self.read(p, "[Content_Types].xml"))
        self.assertZipIntact(p)


class F13Utf16(Fix):
    def test_f13_utf16_parts_are_anonymised_and_stay_utf16(self):
        cmt = ('<?xml version="1.0" encoding="UTF-16"?><w:comments %s><w:comment w:id="0" w:author="SECRETCOMMENTER" w:date="2024-01-01T00:00:00Z" w:initials="SC"/></w:comments>' % W)
        for codec, bom in (("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")):
            p = self.docx("u.docx", [("word/document.xml", "<w:document %s/>" % W), ("word/comments.xml", bom + cmt.encode(codec))])
            self.clean(p, "--full")
            data = self.read(p, "word/comments.xml")
            self.assertTrue(data.startswith(bom))
            text = data[2:].decode(codec)
            self.assertNotIn("SECRET", text)
            self.assertIn('w:author="Author"', text)
            self.assertNotIn("w:date=", text)
            ET.fromstring(data)

    def test_f13_utf16_part_without_changes_is_byte_identical_and_bomless_works(self):
        plain = ('<?xml version="1.0" encoding="UTF-16"?><w:comments %s/>' % W).encode("utf-16")
        bomless = ('<w:comments %s><w:comment w:author="SECRET"/></w:comments>' % W).encode("utf-16-le")
        p = self.docx("u.docx", [("word/document.xml", "<w:document %s/>" % W), ("word/comments.xml", plain), ("word/footnotes.xml", bomless)])
        self.clean(p, "--full")
        self.assertEqual(self.read(p, "word/comments.xml"), plain)
        self.assertNotIn("SECRET", self.read(p, "word/footnotes.xml").decode("utf-16-le"))

    def test_f13_utf8_bom_and_latin1_declaration(self):
        cmt = '<?xml version="1.0" encoding="ISO-8859-1"?><w:comments %s><w:comment w:author="J\u00e1\u0161o"/></w:comments>' % W
        bom = b"\xef\xbb\xbf" + ('<w:comments %s><w:comment w:author="SECRET"/></w:comments>' % W).encode()
        p = self.docx("u.docx", [("word/document.xml", "<w:document %s/>" % W),
                                 ("word/comments.xml", cmt.replace("\u0161", "").encode("latin-1")), ("word/footnotes.xml", bom)])
        self.clean(p, "--full")
        self.assertNotIn(b"SECRET", self.read(p, "word/footnotes.xml"))
        self.assertTrue(self.read(p, "word/footnotes.xml").startswith(b"\xef\xbb\xbf"))
        self.assertIn(b'w:author="Author"', self.read(p, "word/comments.xml"))

    def test_f13_undecodable_part_is_reported_not_silently_skipped(self):
        raw = b'<?xml version="1.0" encoding="UTF-16"?> <w:comments %s><w:comment w:author="SECRET"/></w:comments>' % W.encode()
        p = self.docx("u.docx", [("word/document.xml", "<w:document %s/>" % W), ("word/comments.xml", raw)])
        rc, out, err = run("--full", "-i", p)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("word/comments.xml", err)

    def test_f13_invalid_utf8_part_still_anonymised(self):
        raw = ('<w:comments %s><w:comment w:author="SECRET"/><!--' % W).encode() + b"\xff\xfe" + b"--></w:comments>"
        p = self.docx("u.docx", [("word/document.xml", "<w:document %s/>" % W), ("word/comments.xml", raw)])
        self.clean(p, "--full")
        got = self.read(p, "word/comments.xml")
        self.assertNotIn(b"SECRET", got)
        self.assertIn(b"\xff\xfe", got)


class F14Timing(Fix):
    LIMIT = 3.0

    def timed(self, p, *mode):
        t = time.time()
        rc, out, err = run(*mode, "-i", p)
        self.assertEqual(rc, 0, out + err)
        self.assertLess(time.time() - t, self.LIMIT)

    def test_f14_unclosed_abspath_openers_default_mode(self):
        wb = '<workbook xmlns:x15ac="u"><!--' + '<x15ac:absPath u="1">' * 50000 + "--></workbook>"
        self.assertGreater(len(wb), 1_000_000)
        p = self.docx("w.xlsx", [("xl/workbook.xml", wb)], root_rels=rels(("r1", "officeDocument", "xl/workbook.xml")))
        self.timed(p)

    def test_f14_unclosed_docvars_and_other_hostile_shapes_full(self):
        hostile = {
            "docVars": "<w:docVars>" * 90000,
            "tags": '<w:p w:author="a" ' * 60000,
            "quotes": "<w:ins w:author=\"" * 60000,
            "authors": "<author>" * 120000,
            "lt": "<" * 500000,
            "spaces": "<w:p w:author='a'" + " " * 600000 + "x" + " " * 600000 + "y=",
            "pairs": "<w:p w:author='a' " + "a " * 300000 + ">",
        }
        for name, body in hostile.items():
            doc = '<w:document %s><!--%s--></w:document>' % (W, body.replace("--", "- -"))
            self.assertGreater(len(doc), 500_000, name)
            p = self.docx("h_%s.docx" % name, [("word/document.xml", doc), ("word/comments.xml", doc),
                                              ("xl/comments1.xml", doc), ("content.xml", doc)])
            self.timed(p, "--full")
        odf = self.odt(content="<office:document-content %s><!--%s--></office:document-content>" % (ODF_NS, "<text:author-name>" * 60000))
        self.timed(odf, "--full")


class F17Variants(Fix):
    def test_f17_single_quotes_spaces_and_foreign_prefix(self):
        variants = {
            "single": "<w:comments %s><w:comment w:author='SECRETCOMMENTER' w:date='2024-01-01T00:00:00Z' w:initials='SC'/></w:comments>" % W,
            "spaces": '<w:comments %s><w:comment w:author = "SECRETCOMMENTER" w:date\n=\n"2024-01-01T00:00:00Z" w:initials = "SC"/></w:comments>' % W,
            "ns0": '<ns0:comments xmlns:ns0="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><ns0:comment ns0:author="SECRETCOMMENTER" ns0:date="2024-01-01T00:00:00Z" ns0:initials="SC"/></ns0:comments>',
            "gt": '<w:comments %s><w:comment w:author="SECRET>COMMENTER" w:date="2024-01-01T00:00:00Z" w:initials="SC"/></w:comments>' % W,
        }
        for name, xml in variants.items():
            p = self.docx(name + ".docx", [("word/document.xml", "<w:document %s/>" % W), ("word/comments.xml", xml)])
            self.clean(p, "--full")
            got = self.read(p, "word/comments.xml")
            self.assertNotIn(b"SECRET", got, name)
            self.assertNotIn(b"2024", got, name)
            self.assertNotIn(b"SC", got.replace(b"SECRET", b""), name)
            self.assertIn(b"Author", got, name)
            ET.fromstring(got)

    def test_f17_other_namespace_attributes_named_date_are_kept(self):
        xml = '<w:document %s xmlns:x="urn:x"><w:sdt><w:sdtPr><w:date w:fullDate="2024-01-01T00:00:00Z"/></w:sdtPr></w:sdt><x:e x:date="keep" date="keep2"/></w:document>' % W
        p = self.docx("k.docx", [("word/document.xml", xml)])
        self.clean(p, "--full")
        got = self.read(p, "word/document.xml")
        self.assertIn(b'w:fullDate="2024-01-01T00:00:00Z"', got)
        self.assertIn(b'x:date="keep"', got)
        self.assertIn(b'date="keep2"', got)


class WellFormedGuard(Fix):
    def test_edit_that_would_break_xml_fails_closed(self):
        ctx = Context(warn=lambda m: None)
        with self.assertRaises(WipeError):
            office._rewrite_xml("x.xml", b"<a/>", ctx, lambda t: "<a>")
        # input that was malformed to begin with is not blamed on the edit
        self.assertEqual(office._rewrite_xml("x.xml", b"<a>", ctx, lambda t: "<a><b>"), b"<a><b>")

    def test_clean_packages_stay_well_formed_in_full_mode(self):
        p = self.odt()
        self.clean(p, "--full")
        self.assertWellFormed(p)


if __name__ == "__main__":
    unittest.main()
