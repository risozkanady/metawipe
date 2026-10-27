"""--keep on video (ffmpeg stream copy): exact whitelisted tags only, validated values, read back with ffprobe."""
import json
import unittest

from metawipe import video
from metawipe.common import Context
from tests.test_fix_cli import X264, X264_STRING, ff, probe
from tests.test_metawipe import Base, run

TAGS = {
    "creation_time": "2020-01-02T03:04:05Z", "location": "+50.0875+014.4213+235.500/",
    "com.apple.quicktime.location.ISO6709": "+50.0875+014.4213+235.500/",
    "com.apple.quicktime.make": "Apple", "com.apple.quicktime.model": "iPhone 12", "com.apple.quicktime.software": "16.0",
    "artist": "Bob", "author": "Al", "copyright": "CC", "title": "T", "comment": "C", "description": "D", "keywords": "K",
    # never wanted by any category
    "com.apple.quicktime.creationdate": "2020-01-02T04:04:05+0100", "com.apple.quicktime.location.name": "Home", "album": "Secret album",
}
EXPECT = {  # category -> container tags that must survive
    "dates": {"creation_time"}, "location": {"location", "com.apple.quicktime.location.ISO6709"},  # + location-eng: loci twin
    "device": {"com.apple.quicktime.make", "com.apple.quicktime.model"}, "software": {"com.apple.quicktime.software"},
    "people": {"artist", "author", "copyright"}, "descriptions": {"title", "comment", "description", "keywords"},
}
BORING = {"major_brand", "minor_version", "compatible_brands"}


def container_tags(path):
    return {k: v for k, v in json.loads(probe(path, "-show_entries", "format_tags", "-of", "json")).get("format", {}).get("tags", {}).items()
            if k not in BORING}


@unittest.skipUnless(X264, "ffmpeg/ffprobe with libx264 missing")
class KeepVideoTests(Base):
    def make(self, name, tags=TAGS, *extra):
        path = self.dir / name
        meta = [a for k, v in tags.items() for a in ("-metadata", f"{k}={v}")]
        ff("-f", "lavfi", "-i", "testsrc=d=1:s=64x64:r=5", "-c:v", "libx264", "-metadata:s:v:0", "creation_time=2020-01-02T03:04:05Z",
           "-movflags", "+use_metadata_tags", *meta, *extra, path)
        return path

    def keep(self, src, keep, *extra):
        out = self.dir / "out" / src.name
        out.parent.mkdir(exist_ok=True)
        out.unlink(missing_ok=True)
        rc, stdout, err = run("--keep", keep, *extra, "-o", out.parent, src)
        self.assertEqual(rc, 0, stdout + err)
        return out, stdout, err

    def test_each_category_alone_keeps_only_its_tags(self):
        for ext in ("mov", "mp4", "m4v"):
            src = self.make(f"a.{ext}")
            for cat, keys in EXPECT.items():
                with self.subTest(ext=ext, category=cat):
                    out, stdout, _ = self.keep(src, cat)
                    got = container_tags(out)
                    self.assertEqual(set(got) - {"location-eng"}, keys)
                    self.assertIn(f"(kept: {cat})", stdout)
                    for k in keys - {"creation_time"}:
                        if k != "location":  # the mp4 muxer rewrites the coordinates to 4 decimals
                            self.assertEqual(got[k], TAGS[k])

    def test_all_keeps_everything_but_the_decoys(self):
        out, stdout, _ = self.keep(self.make("a.mov"), "all")
        got = container_tags(out)
        for k in ("com.apple.quicktime.creationdate", "com.apple.quicktime.location.name", "album"):
            self.assertNotIn(k, got)
        for keys in EXPECT.values():
            self.assertTrue(keys <= set(got), (keys, got))
        self.assertIn("(kept: dates, location, device, software, people, descriptions)", stdout)

    def test_dates_are_in_container_and_streams(self):
        out, _, _ = self.keep(self.make("a.mp4"), "dates")
        self.assertIn("2020-01-02T03:04:05", probe(out, "-show_entries", "stream_tags=creation_time", "-of", "csv=p=0"))
        self.assertTrue(container_tags(out)["creation_time"].startswith("2020-01-02T03:04:05"))

    def test_dates_without_any_date_in_source_keeps_no_date_tag(self):
        src = self.make("a.mp4", {"title": "x"})
        run("-o", self.dir / "o", src)  # a clean file carries zero times
        out, _, _ = self.keep(self.dir / "o" / "a.mp4", "dates")
        self.assertNotIn("creation_time", container_tags(out))

    def test_3gp_keeps_dates_and_location_only(self):
        src = self.make("a.3gp", {k: TAGS[k] for k in ("creation_time", "location", "title")})
        out, stdout, _ = self.keep(src, "dates,location,descriptions")
        got = container_tags(out)
        self.assertTrue(got["creation_time"].startswith("2020-01-02T03:04:05"))
        self.assertTrue(got["location"].startswith("+50.0875+014.4213"))
        self.assertNotIn("title", got)
        self.assertIn("(kept: dates, location)", stdout)

    def test_roundtrip_of_a_kept_file(self):
        out, _, _ = self.keep(self.make("a.mov"), "all")  # its creation_time reads back as "x;y"
        kept = out.rename(self.dir / "kept.mov")
        again, _, _ = self.keep(kept, "dates,people")
        got = container_tags(again)
        self.assertTrue(got["creation_time"].startswith("2020-01-02T03:04:05"))
        self.assertEqual(got["artist"], "Bob")
        self.assertNotIn("title", got)

    def test_cover_art_is_reported_not_kept(self):
        png = self.dir / "c.png"
        ff("-f", "lavfi", "-i", "color=red:s=16x16", "-frames:v", "1", png)
        src = self.dir / "cover.mp4"
        ff("-f", "lavfi", "-i", "testsrc=d=1:s=64x64:r=5", "-i", png, "-map", "0", "-map", "1", "-c:v", "libx264", "-c:v:1", "png",
           "-disposition:v:1", "attached_pic", src)
        out, _, err = self.keep(src, "thumbnails")
        self.assertIn("thumbnails not kept", err)
        self.assertNotIn("attached_pic=1", probe(out, "-show_entries", "stream_disposition", "-of", "default=nw=1"))

    def test_software_keeps_x264_strings_with_full(self):
        src = self.make("a.mp4")
        self.assertIn(X264_STRING, src.read_bytes())
        out, _, _ = self.keep(src, "software", "--full")
        self.assertIn(X264_STRING, out.read_bytes())
        out, _, _ = self.keep(src, "dates", "--full")
        self.assertNotIn(X264_STRING, out.read_bytes())

    def test_dates_win_over_full_mtime(self):
        src = self.make("a.mp4")
        out, _, _ = self.keep(src, "dates", "--full")
        self.assertEqual(out.stat().st_mtime_ns, src.stat().st_mtime_ns)


class ValidationTests(unittest.TestCase):
    def test_hostile_values_are_dropped(self):
        bad = {"creation_time": "2040-01-01T00:00:00.000000Z", "location": "+50.0+014.0/ -map 0", "title": "a\nb",
               "comment": "x" * 2001, "artist": "x" * 257, "make": "ok", "com.apple.quicktime.make": "tab\there", "copyright": "‮",
               "com.apple.quicktime.model": 5}
        for cat, key in (("dates", "creation_time"), ("location", "location"), ("descriptions", "title"), ("descriptions", "comment"),
                         ("people", "artist"), ("device", "com.apple.quicktime.make"), ("people", "copyright"), ("device", "com.apple.quicktime.model")):
            self.assertIsNone(video._valid(cat, bad[key]), key)
        self.assertEqual(video._valid("descriptions", "x" * 2000), "x" * 2000)
        self.assertIsNone(video._valid("dates", "2020-13-01T00:00:00Z"))
        self.assertIsNone(video._valid("dates", "1969-12-31T23:59:59Z"))
        self.assertEqual(video._valid("dates", "2020-01-02T03:04:05.000000Z;2020-01-02T03:04:05Z"), "2020-01-02T03:04:05.000000Z")

    def test_only_whitelisted_keys_are_taken(self):
        info = {"format": {"tags": {"title": "t", "album": "no", "com.apple.quicktime.location.name": "no", "encoder": "Lavf"}}}
        ctx = Context(keep=frozenset({"descriptions", "location", "software"}), warn=lambda m: None)
        self.assertEqual(video._wanted(info, [], ctx, ".mov"), {"descriptions": [(None, "title", "t")]})

    def test_3gp_text_tags_are_reported(self):
        warned = []
        ctx = Context(keep=frozenset({"people", "location"}), warn=warned.append)
        info = {"format": {"tags": {"artist": "a", "location": "+50.0+014.0/"}}}
        self.assertEqual(video._wanted(info, [], ctx, ".3gp"), {"location": [(None, "location", "+50.0+014.0/")]})
        self.assertEqual(warned, ["people not kept: 3GP text tags are not written by ffmpeg"])


if __name__ == "__main__":
    unittest.main()
