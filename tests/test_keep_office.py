"""--keep for OOXML and ODF: fresh minimal core.xml/app.xml/meta.xml with validated fields, cleaned thumbnails."""
import io
import struct
import unittest
import xml.etree.ElementTree as ET
import zipfile
import zlib

from metawipe import office
from metawipe.common import Context
from tests.test_fix_office import Fix, ODF_NS, W, rels
from tests.test_metawipe import run

CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC = "http://purl.org/dc/elements/1.1/"
DCT = "http://purl.org/dc/terms/"
EXT = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
MT = "urn:oasis:names:tc:opendocument:xmlns:meta:1.0"
OF = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
CORE = ('<?xml version="1.0"?><cp:coreProperties xmlns:cp="%s" xmlns:dc="%s" xmlns:dcterms="%s" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:title>Plan</dc:title><dc:subject>Subj</dc:subject>'
        '<dc:creator>Jane Doe</dc:creator><cp:keywords>k1 k2</cp:keywords><dc:description>About</dc:description>'
        '<cp:lastModifiedBy>John Roe</cp:lastModifiedBy><cp:category>Cat</cp:category><cp:revision>7</cp:revision>'
        '<dcterms:created xsi:type="dcterms:W3CDTF">2020-01-02T03:04:05Z</dcterms:created>'
        '<dcterms:modified xsi:type="dcterms:W3CDTF">2021-02-03T04:05:06Z</dcterms:modified>'
        '<cp:lastPrinted>2021-03-04T05:06:07Z</cp:lastPrinted><evil:x xmlns:evil="urn:e">leak</evil:x></cp:coreProperties>') % (CP, DC, DCT)
APP = ('<Properties xmlns="%s"><Application>Microsoft Word</Application><AppVersion>16.0000</AppVersion>'
       '<Company>ACME Corp</Company><Manager>Boss</Manager><TotalTime>999</TotalTime><Template>Normal.dotm</Template></Properties>') % EXT
META = ('<?xml version="1.0"?><office:document-meta %s office:version="1.3"><office:meta>'
        '<meta:generator>LibreOffice/7</meta:generator><meta:initial-creator>Jane</meta:initial-creator>'
        '<dc:creator>John</dc:creator><meta:creation-date>2020-01-02T03:04:05</meta:creation-date>'
        '<dc:date>2021-02-03T04:05:06.5</dc:date><meta:print-date>2021-03-04T05:06:07</meta:print-date>'
        '<dc:title>Plan</dc:title><dc:subject>Subj</dc:subject><dc:description>About</dc:description>'
        '<meta:keyword>one</meta:keyword><meta:keyword>two</meta:keyword><meta:printed-by>Printer Pete</meta:printed-by>'
        '<meta:editing-duration>PT5H</meta:editing-duration><meta:user-defined meta:name="k">secret</meta:user-defined>'
        '</office:meta></office:document-meta>') % ODF_NS


def png(text=b"tEXt", payload=b"Comment\x00leaked"):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
            + chunk(text, payload) + chunk(b"IDAT", zlib.compress(b"\x00\x80")) + chunk(b"IEND", b""))


def tags(data):
    return {(el.tag): (el.text, dict(el.attrib)) for el in ET.fromstring(data)}


class KeepOffice(Fix):
    def word(self, name="a.docx", core=CORE, app=APP, thumb=True, nested=False):
        parts = [("word/document.xml", '<w:document %s><w:body><w:ins w:id="1" w:author="Jane Doe" w:date="2021-01-01T00:00:00Z"/></w:body></w:document>' % W),
                 ("docProps/core.xml", core), ("docProps/app.xml", app), ("docProps/custom.xml", "<Properties>custom-secret</Properties>")]
        ct = ('<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
              '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
              '<Default Extension="png" ContentType="image/png"/>')
        rr = [("r1", "officeDocument", "word/document.xml"),
              ("r2", "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties", "docProps/core.xml"),
              ("r3", "extended-properties", "docProps/app.xml")]
        if thumb:
            parts.append(("docProps/thumbnail.png", png()))
            rr.append(("r4", "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail", "docProps/thumbnail.png"))
        if nested:
            parts.append(("word/embeddings/inner.docx", self.docx("inner.docx", [("docProps/core.xml", core)],
                          root_rels=rels(("r1", "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties",
                                          "docProps/core.xml"))).read_bytes()))
        return self.docx(name, parts, ct, rels(*rr))

    def out(self, src, *keep, full=False):
        p = self.clean(src, *(["--keep", ",".join(keep)] if keep else []), *(["--full"] if full else []))
        self.assertZipIntact(p)
        self.assertWellFormed(p)
        return p

    def test_dates_only_core(self):
        p = self.out(self.word(), "dates")
        core = tags(self.read(p, "docProps/core.xml"))
        self.assertEqual(sorted(k.rpartition("}")[2] for k in core), ["created", "lastPrinted", "modified"])
        self.assertEqual(core["{%s}created" % DCT][1], {"{http://www.w3.org/2001/XMLSchema-instance}type": "dcterms:W3CDTF"})
        self.assertNotIn("docProps/app.xml", self.names(p))
        self.assertNotIn("docProps/thumbnail.png", self.names(p))
        self.assertNotIn(b"custom-secret", b"".join(zipfile.ZipFile(p).read(n) for n in self.names(p)))
        self.assertNotIn(b"app.xml", self.read(p, "_rels/.rels"))
        self.assertIn(b"core.xml", self.read(p, "[Content_Types].xml"))
        self.assertIn(b"core-properties", self.read(p, "_rels/.rels"))

    def test_all_text_categories_and_app(self):
        p = self.out(self.word(), "people", "descriptions", "software")
        core = {k.rpartition("}")[2] for k in tags(self.read(p, "docProps/core.xml"))}
        self.assertEqual(core, {"creator", "lastModifiedBy", "title", "subject", "keywords", "description", "category"})
        app = {k.rpartition("}")[2]: v[0] for k, v in tags(self.read(p, "docProps/app.xml")).items()}
        self.assertEqual(app, {"Application": "Microsoft Word", "AppVersion": "16.0000", "Company": "ACME Corp", "Manager": "Boss"})
        blob = self.read(p, "docProps/core.xml") + self.read(p, "docProps/app.xml")
        for leak in (b"leak", b"2020-01-02", b"999", b"Normal.dotm", b"revision"):
            self.assertNotIn(leak, blob)

    def test_software_only_has_no_core(self):
        p = self.out(self.word(), "software")
        self.assertEqual(sorted(self.names(p))[:2], ["[Content_Types].xml", "_rels/.rels"])
        self.assertNotIn("docProps/core.xml", self.names(p))
        self.assertNotIn(b"core-properties", self.read(p, "_rels/.rels"))
        self.assertNotIn(b"core.xml", self.read(p, "[Content_Types].xml"))
        self.assertIn(b"Microsoft Word", self.read(p, "docProps/app.xml"))
        self.assertNotIn(b"ACME", self.read(p, "docProps/app.xml"))

    def test_thumbnail_kept_and_cleaned(self):
        p = self.out(self.word(), "thumbnails")
        data = self.read(p, "docProps/thumbnail.png")
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertNotIn(b"leaked", data)
        self.assertIn(b"metadata/thumbnail", self.read(p, "_rels/.rels"))
        self.assertNotIn("docProps/core.xml", self.names(p))

    def test_thumbnail_unsupported_format_warns_and_drops(self):
        warns = []
        p = self.dir / "o.docx"
        src = self.word(thumb=False)
        with zipfile.ZipFile(src) as z:
            ents = [(n, z.read(n)) for n in z.namelist()]
        ents = [(n, d.replace(b"</Relationships>", b'<Relationship Id="r9" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail" Target="docProps/thumbnail.wmf"/></Relationships>') if n == "_rels/.rels" else d) for n, d in ents]
        self.zip(src, ents + [("docProps/thumbnail.wmf", b"\xd7\xcd\xc6\x9aWMF")])
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(p, "w") as zout:
            ctx = Context(keep=frozenset({"thumbnails"}), warn=warns.append)
            office.clean_package(zin, zout, ctx)
        self.assertTrue(any("thumbnails not kept" in w for w in warns), warns)
        self.assertNotIn("thumbnails", ctx.kept)
        self.assertNotIn("docProps/thumbnail.wmf", self.names(p))
        self.assertNotIn(b"thumbnail", zipfile.ZipFile(p).read("_rels/.rels"))

    def test_invalid_values_dropped(self):
        core = CORE.replace("2020-01-02T03:04:05Z", "2020-13-45T03:04:05Z").replace("2021-02-03T04:05:06Z", "Jane Doe was here") \
            .replace("Jane Doe", "x" * 257).replace("John Roe", "a\nb").replace("<dc:title>Plan", "<dc:title>" + "t" * 2001 + "Plan")
        p = self.out(self.word(core=core), "dates", "people", "descriptions")
        got = {k.rpartition("}")[2] for k in tags(self.read(p, "docProps/core.xml"))}
        self.assertEqual(got, {"lastPrinted", "subject", "keywords", "description", "category"})

    def test_malformed_or_hostile_core_falls_back_to_drop(self):
        for bad in ("<cp:coreProperties", '<!DOCTYPE x [<!ENTITY a "AAAA">]><cp:coreProperties xmlns:cp="%s">&a;</cp:coreProperties>' % CP,
                    "<other/>"):
            warns = []
            src = self.word(core=bad)
            with zipfile.ZipFile(src) as zin, zipfile.ZipFile(self.dir / "o.docx", "w") as zout:
                office.clean_package(zin, zout, Context(keep=frozenset({"dates"}), warn=warns.append))
            with zipfile.ZipFile(self.dir / "o.docx") as z:
                self.assertNotIn("docProps/core.xml", z.namelist(), bad)
            self.assertEqual(len(warns), 1, (bad, warns))

    def test_escaping(self):
        p = self.out(self.word(core=CORE.replace("Jane Doe", "A &amp; B &lt;x&gt;")), "people")
        self.assertEqual(tags(self.read(p, "docProps/core.xml"))["{%s}creator" % DC][0], "A & B <x>")

    def test_full_keeps_revision_dates_and_authors_only_when_asked(self):
        d = self.word()
        base = self.out(d, full=True)
        self.assertNotIn(b"Jane Doe", self.read(base, "word/document.xml"))
        self.assertNotIn(b"2021-01-01", self.read(base, "word/document.xml"))
        p = self.out(self.word("b.docx"), "dates", full=True)
        doc = self.read(p, "word/document.xml")
        self.assertIn(b"2021-01-01", doc)
        self.assertNotIn(b"Jane Doe", doc)
        p = self.out(self.word("c.docx"), "people", full=True)
        doc = self.read(p, "word/document.xml")
        self.assertIn(b"Jane Doe", doc)
        self.assertNotIn(b"2021-01-01", doc)

    def test_nested_package_and_report(self):
        p = self.out(self.word(nested=True), "dates", "people")
        with zipfile.ZipFile(p) as z, zipfile.ZipFile(io.BytesIO(z.read("word/embeddings/inner.docx"))) as inner:
            self.assertFalse([n for n in inner.namelist() if n.startswith("docProps")], inner.namelist())
        rc, out, _ = run("--keep", "dates,people", "-i", self.word("n.docx"))
        self.assertIn("(kept: dates, people)", out)

    # --- ODF ---

    def odf(self, name="a.odt", meta=META, thumb=True, extra=()):
        manifest = self.MANIFEST.replace("</manifest:manifest>", '<manifest:file-entry manifest:full-path="meta.xml" manifest:media-type="text/xml"/>'
                                         '<manifest:file-entry manifest:full-path="Thumbnails/thumbnail.png" manifest:media-type="image/png"/></manifest:manifest>')
        ents = [("meta.xml", meta), ("Object 1/meta.xml", meta)] + ([("Thumbnails/thumbnail.png", png())] if thumb else [])
        p = self.odt(name, extra=ents + list(extra))
        with zipfile.ZipFile(p) as z:
            items = [(n, manifest if n == "META-INF/manifest.xml" else z.read(n)) for n in z.namelist()]
        return self.zip(p, items, stored=("mimetype",))

    def meta(self, p, name="meta.xml"):
        root = ET.fromstring(self.read(p, name))
        return [(el.tag, el.text) for el in root.find("{%s}meta" % OF)]

    def test_odf_dates_people_software(self):
        p = self.out(self.odf(), "dates", "people", "software")
        got = {t.rpartition("}")[2] for t, _ in self.meta(p)}
        self.assertEqual(got, {"generator", "initial-creator", "creator", "creation-date", "date", "print-date"})
        self.assertEqual(dict(self.meta(p))["{%s}date" % DC], "2021-02-03T04:05:06.5")
        blob = self.read(p, "meta.xml")
        for leak in (b"Printer Pete", b"PT5H", b"secret", b"Plan", b"one"):
            self.assertNotIn(leak, blob)
        self.assertEqual(self.meta(p, "Object 1/meta.xml"), [], "sub-object meta stays empty")
        self.assertEqual(self.read(p, "mimetype"), b"application/vnd.oasis.opendocument.text")
        self.assertEqual(self.names(p)[0], "mimetype")

    def test_odf_descriptions_keywords_and_validation(self):
        bad = META.replace("2020-01-02T03:04:05", "yesterday").replace("<dc:title>Plan", "<dc:title>" + "t" * 2001)
        p = self.out(self.odf(meta=bad), "dates", "descriptions")
        got = [(t.rpartition("}")[2], v) for t, v in self.meta(p)]
        self.assertEqual(sorted(g[0] for g in got), ["date", "description", "keyword", "keyword", "print-date", "subject"])

    def test_odf_thumbnail(self):
        p = self.out(self.odf(), "thumbnails")
        data = self.read(p, "Thumbnails/thumbnail.png")
        self.assertNotIn(b"leaked", data)
        self.assertIn(b"Thumbnails/thumbnail.png", self.read(p, "META-INF/manifest.xml"))
        self.assertEqual(self.meta(p), [])

    def test_odf_full_keeps_content_authors_when_asked(self):
        p = self.out(self.odf(), "people", full=True)
        c = self.read(p, "content.xml")
        self.assertIn(b"Jozko", c)
        self.assertNotIn(b"2024-04-04", c)
        p = self.out(self.odf("b.odt"), "dates", full=True)
        c = self.read(p, "content.xml")
        self.assertIn(b"2024-04-04", c)
        self.assertNotIn(b"Jozko", c)


class KeepValues(unittest.TestCase):
    def test_shared_validators(self):
        self.assertIsNone(office._iso("\u0662\u0660\u0662\u0660-01-02"))
        self.assertIsNone(office._iso("2020-01-02T03:04:60Z"))
        self.assertIsNone(office._text(256)("a\u202ebob"))
        self.assertEqual(office._text(256)(" ok "), "ok")


if __name__ == "__main__":
    unittest.main()
