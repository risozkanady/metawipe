"""--keep for JPEG, PNG, WebP and TIFF: fresh Exif/text rebuilds, validation, default behaviour unchanged."""
from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from metawipe import images
from metawipe.common import Context, minimal_tiff

ASCII, SHORT, LONG, RATIONAL, UNDEF, BYTE = 2, 3, 4, 5, 7, 1
SECRET = b"SECRET-VALUE"


def asc(s):
    s = s if isinstance(s, bytes) else s.encode()
    return ASCII, len(s) + 1, s + b"\0"


def rat(*pairs):
    return RATIONAL, len(pairs), b"".join(struct.pack("<II", n, d) for n, d in pairs)


def build(ifd0, exif=(), gps=(), thumb=None, ifd1=()):
    """Little-endian TIFF: entries are (tag, type, count, bytes); sub-IFDs and a JPEG thumbnail are linked in."""
    groups = [["0", list(ifd0)], ["e", list(exif)], ["g", list(gps)], ["1", list(ifd1)]]
    groups[0][1] += [(34665, LONG, 1, b"E")] * bool(exif) + [(34853, LONG, 1, b"G")] * bool(gps)
    if thumb is not None:
        groups[3][1] += [(513, LONG, 1, b"T"), (514, LONG, 1, struct.pack("<I", len(thumb)))]
    groups = [(k, sorted(v)) for k, v in groups if v or k == "0"]
    sizes = {k: 6 + 12 * len(v) + sum(len(d) + len(d) % 2 for _, _, _, d in v if len(d) > 4) for k, v in groups}
    offs, pos = {}, 8
    for k, _ in groups:
        offs[k], pos = pos, pos + sizes[k]
    body = b""
    for k, v in groups:
        table, vals = b"", b""
        for tag, typ, cnt, d in v:
            if d in (b"E", b"G", b"T"):
                d = struct.pack("<I", {b"E": offs.get("e"), b"G": offs.get("g"), b"T": pos}[d])
            if len(d) > 4:
                slot = struct.pack("<I", offs[k] + 6 + 12 * len(v) + len(vals))
                vals += d + b"\0" * (len(d) % 2)
            else:
                slot = d.ljust(4, b"\0")
            table += struct.pack("<HHI", tag, typ, cnt) + slot
        nxt = offs.get("1", 0) if k == "0" else 0
        body += struct.pack("<H", len(v)) + table + struct.pack("<I", nxt) + vals
    return b"II*\0" + struct.pack("<I", 8) + body + (thumb or b"")


def parse(tiff):
    """{ifd name: {tag: (type, count, value bytes)}} read independently of the code under test."""
    o = "<" if tiff[:2] == b"II" else ">"
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1}

    def ifd(off):
        out = {}
        count = struct.unpack_from(o + "H", tiff, off)[0]
        for n in range(count):
            tag, typ, cnt = struct.unpack_from(o + "HHI", tiff, off + 2 + 12 * n)
            size = sizes[typ] * cnt
            at = off + 10 + 12 * n if size <= 4 else struct.unpack_from(o + "I", tiff, off + 10 + 12 * n)[0]
            out[tag] = (typ, cnt, tiff[at:at + size])
        return out, struct.unpack_from(o + "I", tiff, off + 2 + 12 * count)[0]

    first = struct.unpack_from(o + "I", tiff, 4)[0]
    res = {}
    res["0"], nxt = ifd(first)
    for name, tag in (("e", 34665), ("g", 34853)):
        if tag in res["0"]:
            res[name], _ = ifd(struct.unpack(o + "I", res["0"][tag][2])[0])
    if nxt:
        res["1"], _ = ifd(nxt)
    return res


def num(v):
    return struct.unpack(">H", v[2])[0]  # rebuilt blocks are big-endian


def jpeg(tiff=None, extra=b""):
    app1 = b""
    if tiff is not None:
        block = b"Exif\0\0" + tiff
        app1 = b"\xff\xe1" + struct.pack(">H", len(block) + 2) + block
    return b"\xff\xd8" + app1 + extra + b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00" + b"\x12\x34" + b"\xff\xd9"


def exif_of(data):
    return data[data.index(b"Exif\0\0") + 6:data.index(b"\xff\xda")]


def sample(**kw):
    big_thumb = kw.pop("thumb", None)
    return build(
        [(270, *asc("A caption " + SECRET.decode())), (271, *asc("ACME")), (272, *asc("Model-9")), (274, SHORT, 1, struct.pack("<H", 6)),
         (305, *asc("EditorPro 1.0")), (306, *asc("2020:01:02 03:04:05")), (315, *asc("Jane Doe")), (33432, *asc("(c) Jane")),
         (316, *asc("host-name")), (40093, BYTE, 16, "Jane Xp".encode("utf-16-le") + b"\0\0")],
        exif=[(36867, *asc("2020:01:02 03:04:06")), (36868, *asc("2020:01:02 03:04:07")), (37520, *asc("123")),
              (36880, *asc("+01:00")), (37500, UNDEF, 12, b"MAKERNOTE-" + SECRET[:2]), (42033, *asc("SERIAL42")),
              (42036, *asc("Lens 50")), (37510, UNDEF, 13, b"ASCII\0\0\0hello"), (33434, RATIONAL, 1, struct.pack("<II", 1, 100)),
              (42034, *rat((24, 1), (70, 1), (28, 10), (28, 10)))],
        gps=[(1, *asc("N")), (2, *rat((48, 1), (30, 1), (0, 1))), (3, *asc("E")), (4, *rat((17, 1), (6, 1), (0, 1))),
             (7, *rat((3, 1), (4, 1), (5, 1))), (29, *asc("2020:01:02")), (18, *asc("WGS-84"))],
        thumb=big_thumb, ifd1=[(259, SHORT, 1, struct.pack("<H", 6))] if big_thumb else ())


class KeepTiffBlock(unittest.TestCase):
    """exif_keep builds a fresh TIFF from whitelisted, validated fields."""

    def keep(self, *cats, tiff=None, **kw):
        return images.exif_keep(tiff or sample(), frozenset(cats), **kw)

    def test_empty_keep_is_orientation_only_block(self):
        block, cats = self.keep()
        self.assertEqual((block, cats), (minimal_tiff(6), set()))
        self.assertEqual(images.exif_keep(build([(274, SHORT, 1, struct.pack("<H", 1))]), frozenset({"dates"})), (None, set()))

    def test_dates(self):
        block, cats = self.keep("dates")
        t = parse(block)
        self.assertEqual(cats, {"dates"})
        self.assertEqual(t["0"][306][2], b"2020:01:02 03:04:05\0")
        self.assertEqual(set(t["e"]), {36867, 36868, 37520, 36880})
        self.assertEqual(set(t["g"]) - {0}, {7, 29})
        self.assertEqual(num(t["0"][274]), 6)  # orientation always
        for gone in (b"ACME", b"Jane", b"MAKERNOTE", b"SERIAL", b"Lens", SECRET, b"hello", b"WGS"):
            self.assertNotIn(gone, block)
        self.assertEqual(set(t["0"]) - {34665, 34853}, {274, 306})

    def test_location_has_no_gps_date(self):
        block, cats = self.keep("location")
        t = parse(block)
        self.assertEqual(cats, {"location"})
        self.assertEqual(set(t["g"]) - {0}, {1, 2, 3, 4})  # not 7/29, not the free-text MapDatum 18
        self.assertEqual(set(t["0"]) - {34853}, {274})
        self.assertNotIn("e", t)
        self.assertNotIn(b"2020", block)

    def test_device_never_copies_makernote_or_dates(self):
        block, cats = self.keep("device")
        t = parse(block)
        self.assertEqual(cats, {"device"})
        self.assertEqual({k for k in t["0"]} - {34665}, {271, 272, 274, 316})
        self.assertEqual(set(t["e"]), {42033, 42034, 42036})
        self.assertNotIn(b"MAKERNOTE", block)
        self.assertNotIn(b"2020", block)

    def test_software_people_descriptions(self):
        t = parse(self.keep("software")[0])
        self.assertEqual(set(t["0"]), {274, 305})
        block, cats = self.keep("people")
        t = parse(block)
        self.assertEqual(cats, {"people"})
        self.assertEqual(set(t["0"]), {274, 315, 33432, 40093})
        self.assertEqual(t["0"][40093][2].decode("utf-16-le"), "Jane Xp\0")
        block, cats = self.keep("descriptions")
        t = parse(block)
        self.assertEqual(set(t["0"]) - {34665}, {270, 274})
        self.assertEqual(t["e"][37510][2], b"ASCII\0\0\0hello")

    def test_unicode_user_comment_is_rewritten_big_endian(self):
        d = build([], exif=[(37510, UNDEF, 8 + 8, b"UNICODE\0" + "häh".encode("utf-16-le") + b"\0\0")])
        block, cats = self.keep("descriptions", tiff=d)
        self.assertEqual(parse(block)["e"][37510][2], b"UNICODE\0" + "häh".encode("utf-16-be") + b"\0\0"[:0])
        self.assertEqual(block[:2], b"MM")

    def test_invalid_values_are_dropped(self):
        long_text = "x" * 257
        d = build([(271, *asc(long_text)), (272, *asc("bad\x1b[31m")), (306, *asc("see you at the pub 123")),
                   (305, *asc("ok")), (315, ASCII, 3, b"a\0b"), (270, *asc("d" * 2001)), (316, SHORT, 1, b"\1\0\0\0")],
                  exif=[(36867, *asc("2020:13:45 99:99:99")), (36868, *asc("2020:02:30 00:00:00")), (36880, *asc("+99:99")),
                        (37520, *asc("12ab")), (42034, *rat((1, 0), (0, 1), (1, 1), (1, 1)))],
                  gps=[(2, RATIONAL, 3, struct.pack("<6I", 1, 0, 0, 1, 0, 1)), (1, *asc("1")), (29, *asc("junk-date")),
                       (4, SHORT, 3, b"\1\0\2\0\3\0")])
        block, cats = self.keep(*images.KEEP, tiff=d)
        self.assertEqual(cats, {"software"})
        t = parse(block)
        self.assertEqual(set(t["0"]), {305})
        self.assertNotIn("e", t)
        self.assertNotIn("g", t)

    def test_caps(self):
        d = build([(270, *asc("d" * 2000)), (271, *asc("m" * 256))])
        t = parse(self.keep("descriptions", "device", tiff=d)[0])
        self.assertEqual(set(t["0"]), {270, 271})

    def test_hostile_input_falls_back_and_warns_once(self):
        msgs = []
        bad = b"II*\0" + struct.pack("<I", 8) + struct.pack("<H", 60000) + b"\xff" * 40
        for tiff in (bad, build([(274, SHORT, 1, struct.pack("<H", 6))])[:-3]):
            msgs.clear()
            block, cats = images.exif_keep(tiff, frozenset(images.KEEP), msgs.append)
            self.assertEqual(cats, set())
            self.assertIn(block, (None, minimal_tiff(6)))
            self.assertLessEqual(len(msgs), 1)
        # GPS pointer pointing at IFD0 itself and a loop through the thumbnail link do not hang or grow
        d = bytearray(build([(34853, LONG, 1, struct.pack("<I", 8)), (271, *asc("ACME"))]))
        block, cats = images.exif_keep(bytes(d), frozenset({"device", "location", "dates", "thumbnails"}))
        self.assertEqual(cats, {"device"})

    def test_thumbnail_is_cleaned_and_limit_drops_it(self):
        inner = jpeg(build([(271, *asc("INNERMAKE"))]), b"\xff\xfe\x00\x08" + SECRET[:6])
        d = sample(thumb=inner)
        msgs = []
        block, cats = images.exif_keep(d, frozenset({"thumbnails"}), msgs.append)
        self.assertEqual(cats, {"thumbnails"})
        t = parse(block)
        start, size = struct.unpack(">I", t["1"][513][2])[0], struct.unpack(">I", t["1"][514][2])[0]
        thumb = block[start:start + size]
        self.assertEqual(thumb[:2], b"\xff\xd8")
        self.assertNotIn(b"INNERMAKE", thumb)
        self.assertNotIn(SECRET[:6], thumb)
        self.assertEqual(thumb, images.clean_jpeg(inner))
        room = len(images.exif_keep(d, frozenset({"dates"}))[0]) + 20
        block, cats = images.exif_keep(d, frozenset({"thumbnails", "dates"}), msgs.append, limit=room)
        self.assertEqual(cats, {"dates"})
        self.assertNotIn("1", parse(block))
        self.assertTrue(any("thumbnails not kept" in m for m in msgs))
        bad = sample(thumb=b"\xff\xd8not a jpeg at all")
        self.assertEqual(images.exif_keep(bad, frozenset({"thumbnails"}), msgs.append), (minimal_tiff(6), set()))


class KeepJpeg(unittest.TestCase):
    def test_default_unchanged(self):
        out = images.clean_jpeg(jpeg(sample()), keep=frozenset())
        self.assertEqual(out, images.clean_jpeg(jpeg(minimal_tiff(6))))
        self.assertEqual(exif_of(out), minimal_tiff(6))

    def test_keep_dates_through_handler(self):
        with tempfile.TemporaryDirectory() as d:
            src, dst = Path(d, "a.jpg"), Path(d, "b.jpg")
            src.write_bytes(jpeg(sample(), b"\xff\xfe\x00\x08" + SECRET[:6]))
            ctx = Context(keep=frozenset({"dates"}), warn=lambda m: None)
            images.jpeg(src, dst, ctx)
            out = dst.read_bytes()
        self.assertEqual(ctx.kept, {"dates"})
        self.assertIn(b"2020:01:02 03:04:05", out)
        for gone in (b"ACME", b"MAKERNOTE", b"SERIAL42", b"Jane", SECRET[:6], b"hello"):
            self.assertNotIn(gone, out)
        self.assertEqual(out[:2] + out[-2:], b"\xff\xd8\xff\xd9")

    def test_segment_length_fits(self):
        out = images.clean_jpeg(jpeg(sample()), keep=frozenset(images.KEEP))
        i = out.index(b"\xff\xe1")
        self.assertEqual(struct.unpack(">H", out[i + 2:i + 4])[0] + i + 2, out.index(b"\xff\xda"))

    def test_kept_set_collects(self):
        kept = set()
        images.clean_jpeg(jpeg(sample()), frozenset({"people", "location"}), kept)
        self.assertEqual(kept, {"people", "location"})


def chunks(png):
    i, out = 8, []
    while i < len(png):
        n = int.from_bytes(png[i:i + 4], "big")
        body = png[i + 4:i + 8 + n]
        assert zlib.crc32(body) == int.from_bytes(png[i + 8 + n:i + 12 + n], "big"), "bad CRC"
        out.append((png[i + 4:i + 8], png[i + 8:i + 8 + n]))
        i += 12 + n
    return out


def png(*extra):
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (images._PNG_SIG + images._png_chunk(b"IHDR", ihdr) + b"".join(extra)
            + images._png_chunk(b"IDAT", zlib.compress(b"\0\0\0\0")) + images._png_chunk(b"IEND", b""))


def text(key, val, kind=b"tEXt"):
    if kind == b"zTXt":
        return images._png_chunk(kind, key + b"\0\0" + zlib.compress(val))
    if kind == b"iTXt":
        return images._png_chunk(kind, key + b"\0\0\0\0\0" + val)
    return images._png_chunk(kind, key + b"\0" + val)


class KeepPng(unittest.TestCase):
    def clean(self, data, *cats):
        kept, msgs = set(), []
        return images.clean_png(data, frozenset(cats), kept, msgs.append), kept

    def test_only_known_date_keywords(self):
        d = png(text(b"date:JANE DOE LIVES HERE", b"2020-01-02T03:04:05Z"), text(b"date:\x01hidden", b"2020-01-02"),
                text(b"date:create", b"2020-01-02T03:04:05Z"))
        out, kept = self.clean(d, "dates")
        self.assertEqual([k for c, k in chunks(out) if c == b"tEXt"], [b"date:create\x002020-01-02T03:04:05Z"])
        self.assertEqual(kept, {"dates"})

    def test_default_unchanged(self):
        d = png(text(b"Title", b"x"), images._png_chunk(b"tIME", struct.pack(">HBBBBB", 2020, 1, 2, 3, 4, 5)),
                images._png_chunk(b"eXIf", sample()))
        out, kept = self.clean(d)
        self.assertEqual([c for c, _ in chunks(out)], [b"IHDR", b"eXIf", b"IDAT", b"IEND"])
        self.assertEqual(dict(chunks(out))[b"eXIf"], minimal_tiff(6))
        self.assertEqual(kept, set())

    def test_text_time_and_exif(self):
        d = png(text(b"Creation Time", b"2020-01-02T03:04:05Z"), text(b"Author", b"Jane Doe", b"iTXt"),
                text(b"Software", "Gimp ü".encode("latin-1"), b"tEXt"), text(b"Comment", b"a comment", b"zTXt"),
                text(b"Source", b"my phone " + SECRET), text(b"parameters", SECRET), text(b"Disclaimer", SECRET),
                images._png_chunk(b"tIME", struct.pack(">HBBBBB", 2020, 1, 2, 3, 4, 5)), images._png_chunk(b"eXIf", sample()))
        out, kept = self.clean(d, "dates", "people", "software")
        self.assertEqual(kept, {"dates", "people", "software"})
        cs = chunks(out)
        names = [c for c, _ in cs]
        self.assertEqual(names[-1], b"IEND")
        self.assertEqual(names.index(b"IDAT") < names.index(b"tIME"), True)
        got = {p.split(b"\0")[0] for c, p in cs if c in (b"tEXt", b"iTXt")}
        self.assertEqual(got, {b"Creation Time", b"Author", b"Software"})
        self.assertNotIn(SECRET, out)
        self.assertNotIn(b"a comment", out)
        self.assertEqual(set(parse(dict(cs)[b"eXIf"])["0"]) - {34665, 34853}, {274, 305, 306, 315, 33432, 40093})
        self.assertIn(b"\x07\xe4\x01\x02\x03\x04\x05", dict(cs)[b"tIME"])

    def test_invalid_text_and_time_dropped(self):
        d = png(text(b"Creation Time", b"yesterday evening at Bob's"), text(b"date:create", b"2020-13-40T00:00:00"),
                text(b"date:modify", b"2020-01-02T03:04:05+00:00"), text(b"Title", b"a\x1bb"), text(b"Author", b"x" * 257),
                text(b"Description", b"d" * 2000), images._png_chunk(b"tIME", struct.pack(">HBBBBB", 2020, 14, 2, 3, 4, 5)))
        out, kept = self.clean(d, "dates", "people", "descriptions")
        got = {p.split(b"\0")[0] for c, p in chunks(out) if c == b"tEXt"}
        self.assertEqual(got, {b"date:modify", b"Description"})
        self.assertNotIn(b"tIME", out)
        self.assertEqual(kept, {"dates", "descriptions"})

    def test_utf8_value_uses_itxt_and_rfc_dates_ok(self):
        d = png(text(b"Title", "Žlutý kůň".encode(), b"iTXt"), text(b"Creation Time", b"Tue, 27 Oct 2026 10:00:00 +0000"))
        out, kept = self.clean(d, "dates", "descriptions")
        cs = chunks(out)
        self.assertIn(b"iTXt", [c for c, _ in cs])
        self.assertIn("Žlutý kůň".encode(), out)
        self.assertIn(b"Tue, 27 Oct 2026", out)

    def test_imagemagick_raw_exif_profile(self):
        hexdata = (b"Exif\0\0" + sample()).hex().encode()
        raw = b"\nexif\n%8d\n" % (len(hexdata) // 2) + hexdata + b"\n"
        out, kept = self.clean(png(text(b"Raw profile type exif", raw)), "device")
        self.assertEqual(kept, {"device"})
        self.assertEqual(set(parse(dict(chunks(out))[b"eXIf"])["0"]), {271, 272, 274, 316, 34665})


def webp(*extra, vp8x=True):
    vp8 = b"VP8 " + struct.pack("<I", 10) + bytes(10)
    head = b"VP8X" + struct.pack("<I", 10) + bytes(10) if vp8x else b""
    body = b"WEBP" + head + vp8 + b"".join(extra)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def exif_chunk(tiff):
    return b"EXIF" + struct.pack("<I", len(tiff)) + tiff + b"\0" * (len(tiff) & 1)


class KeepGpsTime(unittest.TestCase):
    def test_time_of_day_only(self):
        for hms, ok in (((3, 4, 5), True), ((91, 48, 30), False), ((23, 60, 0), False), ((1, 2, 60), False)):
            t = build([], gps=[(7, *rat(*[(v, 1) for v in hms]))])
            block, cats = images.exif_keep(t, frozenset({"dates"}))
            self.assertEqual(cats, {"dates"} if ok else set(), hms)


class KeepWebp(unittest.TestCase):
    def test_default_unchanged(self):
        out = images.clean_webp(webp(exif_chunk(sample())), keep=frozenset())
        self.assertIn(exif_chunk(minimal_tiff(6)), out)
        self.assertEqual(out[20] & 0x08, 0x08)

    def test_keep_people(self):
        kept = set()
        out = images.clean_webp(webp(exif_chunk(sample())), frozenset({"people"}), kept)
        self.assertEqual(kept, {"people"})
        i = out.index(b"EXIF")
        n = struct.unpack("<I", out[i + 4:i + 8])[0]
        self.assertEqual(set(parse(out[i + 8:i + 8 + n])["0"]), {274, 315, 33432, 40093})
        self.assertEqual(struct.unpack("<I", out[4:8])[0], len(out) - 8)
        self.assertNotIn(b"ACME", out)
        self.assertTrue(out[20] & 0x08)

    def test_no_vp8x_warns(self):
        msgs, kept = [], set()
        out = images.clean_webp(webp(exif_chunk(sample()), vp8x=False), frozenset({"people"}), kept, msgs.append)
        self.assertNotIn(b"EXIF", out)
        self.assertEqual(kept, set())
        self.assertEqual(len(msgs), 1)
        self.assertIn("people not kept", msgs[0])


class KeepTiffFile(unittest.TestCase):
    def tif(self, keep, data=None, **kw):
        msgs, kept = [], set()
        out = images.clean_tiff(data or self.data(**kw), lambda m: msgs.append("leftover " + m), frozenset(keep), kept, msgs.append)
        return out, kept, msgs

    def data(self, **kw):
        strip = b"PIXELDATA"
        ifd0 = [(256, LONG, 1, struct.pack("<I", 3)), (257, LONG, 1, struct.pack("<I", 3)), (258, SHORT, 1, struct.pack("<H", 8)),
                (259, SHORT, 1, struct.pack("<H", 1)), (262, SHORT, 1, struct.pack("<H", 1)),
                (273, LONG, 1, b"S\0\0\0"), (277, SHORT, 1, struct.pack("<H", 1)), (278, LONG, 1, struct.pack("<I", 3)),
                (279, LONG, 1, struct.pack("<I", 9)),
                (271, *asc("ACME-CAM")), (305, *asc("Soft 2")), (306, *asc("2020:01:02 03:04:05")), (315, *asc("Jane Doe")),
                (270, *asc("caption " + SECRET.decode()))]
        t = bytearray(build(ifd0, exif=[(36867, *asc("2020:01:02 03:04:06")), (42033, *asc("SERIALNO"))], gps=[(1, *asc("N"))]))
        t += strip
        # patch the strip offset (entry for tag 273 holds b"S\0\0\0" == 83): point it at the appended pixel data
        i = t.index(struct.pack("<HHI", 273, LONG, 1)) + 8
        t[i:i + 4] = struct.pack("<I", len(t) - len(strip))
        return bytes(t)

    def test_default_unchanged(self):
        d = self.data()
        out, kept, msgs = self.tif((), d)
        self.assertEqual(out, images.clean_tiff(d))
        for gone in (b"ACME-CAM", b"Jane", b"2020:01", b"SERIALNO", SECRET):
            self.assertNotIn(gone, out)
        self.assertIn(b"PIXELDATA", out)
        self.assertEqual((kept, msgs), (set(), []))

    def test_keeps_ifd0_fields_of_chosen_categories(self):
        out, kept, msgs = self.tif({"dates", "people", "device"})
        self.assertEqual(kept, {"dates", "people", "device"})
        for want in (b"2020:01:02 03:04:05", b"Jane Doe", b"ACME-CAM", b"PIXELDATA"):
            self.assertIn(want, out)
        for gone in (b"Soft 2", SECRET, b"SERIALNO", b"2020:01:02 03:04:06"):
            self.assertNotIn(gone, out)
        self.assertTrue(any(m.startswith("dates not kept") for m in msgs))
        self.assertTrue(any(m.startswith("device not kept") for m in msgs))
        self.assertFalse(any(m.startswith("people") or m.startswith("leftover") for m in msgs))
        self.assertEqual(set(parse(out)["0"]) & {271, 305, 306, 315, 270}, {271, 306, 315})

    def test_invalid_value_dropped_and_location_warns(self):
        d = self.data().replace(b"2020:01:02 03:04:05", b"SECRET-DATE-TEXT-12")
        out, kept, msgs = self.tif({"dates", "location"}, d)
        self.assertNotIn(b"SECRET-DATE", out)
        self.assertEqual(kept, set())
        self.assertTrue(any(m.startswith("location not kept") for m in msgs))


if __name__ == "__main__":
    unittest.main()
