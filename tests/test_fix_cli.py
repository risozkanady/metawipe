"""Regression tests for the round-3 video/CLI findings: user-data SEI neutralised in place (open-GOP,
frame packing, HDR10, several video tracks), ffprobe handling, ffmpeg diagnostics, ownership under
root, files inside the output directory and temp names in messages. Fixtures are built here."""
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from metawipe import cli, sei, video
from metawipe.common import Context
from tests.test_metawipe import HAVE, SECRET, Base, run
from tests.test_regressions import png_bytes

ENCODERS = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout if HAVE["ffmpeg"] else ""
X264 = HAVE["ffmpeg"] and HAVE["ffprobe"] and " libx264 " in ENCODERS
X265 = X264 and " libx265 " in ENCODERS
AV1 = next((e for e in ("libsvtav1", "libaom-av1", "librav1e") if f" {e} " in ENCODERS), None)
X264_STRING, X265_STRING = b"x264 - core", b"x265 (build"


def ff(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *map(str, args)], check=True)


def probe(path, *args):
    return subprocess.run(["ffprobe", "-v", "error", *args, str(path)], capture_output=True, text=True).stdout


def packets(path):
    return probe(path, "-show_entries", "packet=stream_index,pts,dts,duration,flags,size,pos", "-of", "csv=p=0")


def framemd5(path, spec):
    return subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", f"0:{spec}", "-f", "framemd5", "-"],
                          capture_output=True, text=True).stdout


def frame_count(path, spec="v:0"):
    return probe(path, "-count_frames", "-select_streams", spec, "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0").strip()


def side_data(path):
    return probe(path, "-select_streams", "v:0", "-read_intervals", "%+#2", "-show_frames", "-show_entries",
                 "frame=side_data_list", "-of", "default=nw=1")


def moov_range(data: bytes):
    pos = 0
    while pos + 8 <= len(data):
        size, btype = struct.unpack_from(">I4s", data, pos)
        if size == 1:
            size = struct.unpack_from(">Q", data, pos + 8)[0]
        if btype == b"moov":
            return pos, pos + size
        pos += size
    raise AssertionError("no moov")


def annexb_sei(messages, hevc=False) -> bytes:
    """One escaped SEI NAL unit (without start code) holding the given (type, payload) messages."""
    rbsp = b""
    for ptype, payload in messages:
        for value in (ptype, len(payload)):
            rbsp += b"\xff" * (value // 255) + bytes([value % 255])
        rbsp += payload
    rbsp += b"\x80"
    out, zeros = bytearray(), 0
    for b in rbsp:
        if zeros >= 2 and b <= 3:
            out.append(3)
            zeros = 0
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return (b"\x4e\x01" if hevc else b"\x06") + bytes(out)


def parsed(nal: bytes, header: int):
    rbsp, _ = sei._unescape(bytearray(nal), header)
    return [(ptype, bytes(rbsp[a:b])) for _, ptype, a, b in sei._messages(rbsp)]


class SeiUnitTests(unittest.TestCase):
    UUID = bytes.fromhex("dc45e9bde6d948b7962cd820d923eeef")

    def test_filler_has_exact_length_and_parses(self):
        for n in range(3, 1300):
            f = sei._filler(n)
            self.assertEqual(len(f), n)
            self.assertNotIn(b"\x00\x00", f)
            self.assertNotEqual(f[-1], 0)
            msgs = [(t, b - a) for _, t, a, b in sei._messages(bytearray(f + b"\x80"))]
            self.assertTrue(msgs and all(t == 0xF0 for t, _ in msgs), n)

    def test_only_user_data_is_replaced_escapes_before_inside_and_after(self):
        before = (1, b"\x00\x00\x01\x00\x00\x02")  # needs emulation prevention before the user data
        user = (5, self.UUID + b"\x00\x00\x00\x01x264 - core " + SECRET + b"\x00\x00")  # escapes inside and after
        after = (3, b"\xff\xff")  # starts with 0x03: preceded by an escape byte in the original
        for hevc in (False, True):
            nal = annexb_sei([before, user, after, (6, b"\x84")], hevc)
            self.assertGreater(nal.count(b"\x00\x00\x03"), 3)
            out = bytearray(nal)
            self.assertEqual(sei.scrub_nal(out, 2 if hevc else 1), 1)
            self.assertEqual(len(out), len(nal))
            self.assertNotIn(SECRET, out)
            msgs = parsed(out, 2 if hevc else 1)
            self.assertEqual(msgs[0], before)
            self.assertEqual(msgs[-2:], [after, (6, b"\x84")])
            self.assertTrue(all(t == 0xF0 for t, _ in msgs[1:-2]))

    def test_size_with_no_single_message_encoding(self):
        # 256k + 1 raw bytes cannot be one message: two reserved messages are written instead
        payload = self.UUID + b"\x00\x00\x01" + b"a" * (254 - 16 - 3)  # 254 bytes + 1 escape byte
        nal = annexb_sei([(5, payload)])
        self.assertEqual(len(nal), 1 + 257 + 1)
        out = bytearray(nal)
        sei.scrub_nal(out, 1)
        self.assertEqual(len(out), len(nal))
        self.assertEqual([t for t, _ in parsed(out, 1)], [0xF0, 0xF0])

    def test_malformed_sei_is_reported_not_rewritten(self):
        out = bytearray(b"\x06\x05\x40abc\x80")  # size 64 but only 3 bytes
        with self.assertRaises(sei._Malformed):
            sei.scrub_nal(out, 1)
        self.assertEqual(out, b"\x06\x05\x40abc\x80")

    @unittest.skipUnless(X264, "ffmpeg with libx264 missing")
    def test_rewritten_sei_parses_in_ffmpeg_and_decoding_is_unchanged(self):
        """Independent check with ffmpeg's own SEI parser (trace_headers) on an Annex-B stream."""
        d = Path(self._tmp())
        base = d / "a.h264"
        ff("-f", "lavfi", "-i", "testsrc2=d=0.4:s=64x64:r=10", "-c:v", "libx264", "-x264-params", "log-level=error", base)
        data = base.read_bytes()
        first_slice = data.index(b"\x00\x00\x01\x65")  # crafted SEI goes in front of the first IDR slice
        user = (5, self.UUID + b"\x00\x00\x00\x01" + SECRET + b"\x00\x00")
        nal = annexb_sei([user, (3, b"\xff\xff")])
        scrubbed = bytearray(nal)
        sei.scrub_nal(scrubbed, 1)
        for name, unit in (("in.h264", nal), ("out.h264", bytes(scrubbed))):
            (d / name).write_bytes(data[:first_slice] + b"\x00\x00\x00\x01" + unit + data[first_slice:])
        traces = {}
        for name in ("in.h264", "out.h264"):
            res = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(d / name), "-c", "copy", "-bsf:v", "trace_headers",
                                  "-f", "null", "-"], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr[-2000:])
            traces[name] = [int(m) for m in re.findall(r"last_payload_type_byte\s+\d+ = (\d+)", res.stderr)]
        # the stream's own x264 SEI (also type 5) is left alone here: only the crafted unit is scrubbed
        self.assertEqual(traces["in.h264"].count(5), 2)
        self.assertEqual(traces["out.h264"].count(5), 1)
        self.assertIn(240, traces["out.h264"])
        self.assertEqual(traces["in.h264"].count(3), traces["out.h264"].count(3))
        self.assertNotIn(SECRET, (d / "out.h264").read_bytes())
        self.assertEqual(framemd5(d / "in.h264", "v"), framemd5(d / "out.h264", "v"))

    def _tmp(self):
        import tempfile
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        return t.name


@unittest.skipUnless(X264, "ffmpeg/ffprobe with libx264 missing")
class FullVideoTests(Base):
    """--full output must equal the default output except inside video samples / moov, and decode identically."""

    def both(self, src, expect_rc=0):
        """Clean copies of src in default and --full mode; returns (default_output, full_output, full_messages)."""
        plain, full = self.dir / ("plain" + src.suffix), self.dir / ("full" + src.suffix)
        shutil.copy(src, plain)
        shutil.copy(src, full)
        rc, out, err = run("-i", plain)
        self.assertEqual(rc, 0, out + err)
        rc, out, err = run("--full", "-i", full)
        self.assertEqual(rc, expect_rc, out + err)
        return plain, full, out + err

    def assertSameVideo(self, plain, full, n_video=1):
        a, b = plain.read_bytes(), full.read_bytes()
        self.assertEqual(len(a), len(b), "nothing may move")
        self.assertEqual(packets(plain), packets(full))
        for k in range(n_video):
            self.assertEqual(framemd5(plain, f"v:{k}"), framemd5(full, f"v:{k}"))
            self.assertEqual(frame_count(plain, f"v:{k}"), frame_count(full, f"v:{k}"))
        # every changed byte lies inside a video packet or inside moov (hvcC)
        allowed = [moov_range(b)]
        streams = probe(full, "-show_entries", "stream=index,codec_type", "-of", "csv=p=0").split()
        video_streams = {s.split(",")[0] for s in streams if s.endswith(",video")}
        for line in packets(full).splitlines():
            idx, _, _, _, size, pos, *_ = line.split(",")
            if idx in video_streams:
                allowed.append((int(pos), int(pos) + int(size)))
        allowed.sort()
        changed = [i for i in range(len(a)) if a[i] != b[i]] if len(a) < 5_000_000 else []
        for i in changed:
            self.assertTrue(any(s <= i < e for s, e in allowed), f"byte {i} changed outside video samples/moov")
        return changed

    def test_open_gop_cut_file_keeps_decoding(self):
        og = self.dir / "og.mp4"
        ff("-f", "lavfi", "-i", "testsrc2=d=6:s=320x240:r=25", "-c:v", "libx264", "-x264-params",
           "open-gop=1:keyint=25:min-keyint=10:bframes=3:scenecut=0:log-level=error", "-bf", "3", og)
        cut = self.dir / "cut.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "quiet", "-y", "-ss", "1.1", "-i", str(og), "-c", "copy", str(cut)], check=True)
        expected = frame_count(cut)
        self.assertGreater(int(expected), 100)
        plain, full, _ = self.both(cut)
        self.assertSameVideo(plain, full)
        self.assertEqual(frame_count(full), expected)  # was 0: the recovery-point SEI was dropped
        self.assertEqual(plain.read_bytes(), full.read_bytes())  # no user data in the cut: nothing to change

        # uncut: keyframe flags survive, encoder string goes
        self.assertIn(X264_STRING, og.read_bytes())
        plain, full, _ = self.both(og)
        changed = self.assertSameVideo(plain, full)
        self.assertTrue(changed)
        self.assertNotIn(X264_STRING, full.read_bytes())
        self.assertEqual(packets(full).count(",K"), packets(og).count(",K"))
        self.assertGreater(packets(full).count(",K"), 1)

    def test_frame_packing_and_timing_sei_kept(self):
        sbs = self.dir / "sbs.mp4"
        ff("-f", "lavfi", "-i", "testsrc2=d=1:s=320x180:r=25", "-c:v", "libx264", "-x264-params",
           "frame-packing=3:nal-hrd=vbr:pic-struct=1:bitrate=500:vbv-maxrate=500:vbv-bufsize=1000:log-level=error", sbs)
        self.assertIn("Stereo 3D", side_data(sbs))
        plain, full, _ = self.both(sbs)
        self.assertSameVideo(plain, full)
        self.assertIn("Stereo 3D", side_data(full))
        self.assertNotIn(X264_STRING, full.read_bytes())

    @unittest.skipUnless(X265, "libx265 missing")
    def test_every_video_track_h264_and_hevc_both_orders(self):
        for order in (("libx264", "libx265"), ("libx265", "libx264")):
            src = self.dir / f"two-{order[0]}.mp4"
            args = []
            for k, enc in enumerate(order):
                args += [f"-c:v:{k}", enc] + ([f"-tag:v:{k}", "hvc1"] if enc == "libx265" else [])
            ff("-f", "lavfi", "-i", "testsrc2=d=2:s=320x240:r=25", "-f", "lavfi", "-i", "testsrc2=d=2:s=160x120:r=25",
               "-f", "lavfi", "-i", "sine=d=2", "-map", "0:v", "-map", "1:v", "-map", "2:a", *args,
               "-x265-params", "keyint=10:open-gop=1:bframes=4:scenecut=0:log-level=error", "-c:a", "aac", src)
            data = src.read_bytes()
            self.assertIn(X264_STRING, data)
            self.assertIn(X265_STRING, data)
            plain, full, _ = self.both(src)
            self.assertSameVideo(plain, full, n_video=2)
            self.assertNotIn(X264_STRING, full.read_bytes())
            self.assertNotIn(X265_STRING, full.read_bytes())
            for p in (plain, full):
                p.unlink()

    @unittest.skipUnless(AV1, "no AV1 encoder")
    def test_av1_track_untouched_and_reported(self):
        src = self.dir / "av1.mp4"
        ff("-f", "lavfi", "-i", "testsrc2=d=2:s=320x240:r=25", "-f", "lavfi", "-i", "testsrc2=d=2:s=160x120:r=25",
           "-map", "0:v", "-map", "1:v", "-c:v:0", "libx264", "-c:v:1", AV1, src)
        plain, full, messages = self.both(src, expect_rc=2)
        self.assertSameVideo(plain, full, n_video=2)
        self.assertIn("video track 2 (av01)", messages)
        self.assertNotIn(X264_STRING, full.read_bytes())

    def test_other_codec_track_in_mov_is_incomplete_not_failed(self):
        src = self.dir / "mixed.mov"
        ff("-f", "lavfi", "-i", "testsrc2=d=1:s=160x120:r=10", "-f", "lavfi", "-i", "testsrc2=d=1:s=160x120:r=10",
           "-map", "0:v", "-map", "1:v", "-c:v:0", "libx264", "-c:v:1", "mpeg4", src)
        plain, full, messages = self.both(src, expect_rc=2)
        self.assertSameVideo(plain, full, n_video=2)
        self.assertIn("video track 2 (mp4v)", messages)

    @unittest.skipUnless(X265, "libx265 missing")
    def test_hdr10_metadata_kept_also_when_only_in_hvcc(self):
        hdr = self.dir / "hdr.mp4"
        ff("-f", "lavfi", "-i", "testsrc2=d=1:s=320x240:r=30", "-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1",
           "-x265-params", "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:master-display=G(13250,34500)B(7500,3000)"
           "R(34000,16000)WP(15635,16450)L(10000000,1):max-cll=1000,400:hdr10=1:repeat-headers=1:log-level=error", hdr)
        only = self.dir / "hvcc_only.mp4"
        ff("-i", hdr, "-map", "0", "-c", "copy", "-bsf:v", "filter_units=remove_types=39", only)
        for src in (hdr, only):
            before = side_data(src)
            self.assertIn("Mastering display metadata", before)
            self.assertIn("Content light level metadata", before)
            plain, full, messages = self.both(src, expect_rc=0)  # no longer "incomplete"
            self.assertSameVideo(plain, full)
            after = side_data(full)
            self.assertIn("Mastering display metadata", after)
            self.assertIn("Content light level metadata", after)
            self.assertNotIn(X265_STRING, full.read_bytes())
            self.assertNotIn("HDR10", messages)
            plain.unlink()
            full.unlink()

    @unittest.skipUnless(X265, "libx265 missing")
    def test_hvcc_lookalike_inside_audio_is_not_touched(self):
        src = self.dir / "h.mov"  # PCM audio: the planted bytes do not disturb ffmpeg's probing
        ff("-f", "lavfi", "-i", "testsrc2=d=2:s=320x240:r=25", "-f", "lavfi", "-i", "sine=d=2", "-c:v", "libx265",
           "-tag:v", "hvc1", "-x265-params", "log-level=error", "-c:a", "pcm_s16be", src)
        data = bytearray(src.read_bytes())
        audio = [line.split(",") for line in probe(src, "-select_streams", "a", "-show_entries", "packet=pos,size",
                                                   "-of", "csv=p=0").split()]
        pos, size = (int(audio[3][0]), int(audio[3][1]))
        blob = struct.pack(">I", 60) + b"hvcC" + bytes(22) + b"\x01\x27" + struct.pack(">HH", 1, 3) + b"abc"
        self.assertGreater(size, len(blob) + 8)
        data[pos + 4:pos + 4 + len(blob)] = blob
        src.write_bytes(bytes(data))
        plain, full, _ = self.both(src)
        self.assertSameVideo(plain, full)
        self.assertEqual(framemd5(plain, "a"), framemd5(full, "a"))
        self.assertNotIn(X265_STRING, full.read_bytes())

    def test_sei_scrub_writes_in_place_without_reading_the_whole_file(self):
        src = self.dir / "a.mp4"
        ff("-f", "lavfi", "-i", "testsrc2=d=1:s=160x120:r=10", "-c:v", "libx264", "-x264-params", "log-level=error", src)
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read")):
            ctx = Context()
            self.assertEqual(sei.scrub(src, ctx), 1)
        self.assertEqual(ctx.residue, [])
        self.assertNotIn(X264_STRING, src.read_bytes())


@unittest.skipUnless(X264, "ffmpeg/ffprobe with libx264 missing")
class FfmpegDiagnosticsTests(Base):
    def make(self, path, *extra):
        ff("-f", "lavfi", "-i", "testsrc=d=1:s=64x64:r=10", "-f", "lavfi", "-i", "sine=d=1", "-c:v", "libx264", "-c:a", "aac",
           *extra, path)
        return path

    def zero_delta(self, src, dst, handler):
        """Copy of src (moov last) whose `handler` track has one sample of duration 0 (two equal DTS)."""
        data = src.read_bytes()
        start, end = moov_range(data)
        self.assertEqual(end, len(data))
        containers = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}
        state = {}

        def rebuild(s, e):
            out = b""
            pos = s
            while pos + 8 <= e:
                size, btype = struct.unpack_from(">I4s", data, pos)
                body = data[pos + 8:pos + size]
                if btype in containers:
                    body = rebuild(pos + 8, pos + size)
                elif btype == b"hdlr":
                    state["handler"] = body[8:12]
                elif btype == b"stts" and state.get("handler") == handler:
                    deltas = []
                    for k in range(struct.unpack_from(">I", body, 4)[0]):
                        count, delta = struct.unpack_from(">II", body, 8 + 8 * k)
                        deltas += [delta] * count
                    deltas[4] += deltas[3]
                    deltas[3] = 0
                    body = body[:4] + struct.pack(">I", len(deltas)) + b"".join(struct.pack(">II", 1, d) for d in deltas)
                out += struct.pack(">I4s", 8 + len(body), btype) + body
                pos += size
            return out

        dst.write_bytes(data[:start] + rebuild(start, end))
        return dst

    def test_duplicate_dts_that_ffmpeg_fixes_is_accepted(self):
        src = self.make(self.dir / "src.mp4")
        for handler in (b"vide", b"soun"):
            for mode in ((), ("--full",)):
                p = self.zero_delta(src, self.dir / "zd.mp4", handler)
                dts = [line.split(",") for line in packets(p).splitlines()]
                seen = [(i, d) for i, _, d, *_ in dts]
                self.assertNotEqual(len(seen), len(set(seen)), "fixture must have a duplicate DTS")
                rc, out, err = run(*mode, "-i", p)
                self.assertEqual(rc, 0, out + err)  # was 1 with -xerror: "Non-monotonic DTS"
                self.assertEqual(len(packets(p).splitlines()), len(dts))

    def test_only_auto_corrected_dts_messages_are_benign(self):
        src = self.make(self.dir / "src.mp4")
        real_run = subprocess.run
        benign = ("[vost#0:0/copy @ 0x1] Non-monotonic DTS; previous: 1536, current: 1536; changing to 1537. "
                  "This may result in incorrect timestamps in the output file.\n")

        def with_stderr(extra):
            def fake(cmd, *a, **k):
                res = real_run(cmd, *a, **k)
                if str(cmd[0]).endswith("ffmpeg"):
                    res.stderr += extra
                return res
            return fake

        for extra, rc_expected in ((benign, 0), (benign + "[mov,mp4 @ 0x2] partial file\n", 1),
                                   ("[vost#0:0/copy @ 0x1] Non-monotonic DTS; previous: 5, current: 5; "
                                    "Error submitting a packet to the muxer: Invalid argument\n", 1)):
            with mock.patch.object(video.subprocess, "run", with_stderr(extra)):
                rc, out, err = run(src)
            self.assertEqual(rc, rc_expected, extra + out + err)
            (self.dir / "src.clean.mp4").unlink(missing_ok=True)

    def test_truncated_video_still_fails_in_both_modes(self):
        vid = self.make(self.dir / "full.mp4", "-b:v", "2M", "-movflags", "+faststart")
        data = vid.read_bytes()
        trunc = self.dir / "trunc.mp4"
        trunc.write_bytes(data[: len(data) * 6 // 10])
        for mode in ((), ("--full",)):
            rc, out, err = run(*mode, "-i", trunc)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("partial file", err)
            self.assertEqual(trunc.read_bytes(), data[: len(data) * 6 // 10])
        self.assertEqual(sorted(f.name for f in self.dir.iterdir()), ["full.mp4", "trunc.mp4"])
        nomoov = self.dir / "nomoov.mp4"
        nomoov.write_bytes(self.make(self.dir / "end.mp4").read_bytes()[:-2000])  # moov at the end, cut off
        rc, out, err = run("-i", nomoov)
        self.assertEqual(rc, 1, out + err)

    def test_missing_ffprobe(self):
        real_which = shutil.which
        src = self.make(self.dir / "a.mp4", "-metadata:s:a:0", "language=ces")

        def which(name, *a, **k):
            return None if name == "ffprobe" else real_which(name, *a, **k)

        with mock.patch.object(video.shutil, "which", which):
            full = self.dir / "full.mp4"
            shutil.copy(src, full)
            rc, out, err = run("--full", "-i", full)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("ffprobe is required for --full", err)
            self.assertEqual(full.read_bytes(), src.read_bytes())
            rc, out, err = run(src)
            self.assertEqual(rc, 0, out + err)
            self.assertIn("ffprobe not found", err)
        self.assertTrue((self.dir / "a.clean.mp4").exists())

    def test_ffprobe_failure_is_an_error(self):
        fake = self.dir / "fake.mov"
        fake.write_text("not a movie\n")
        for mode in ((), ("--full",)):
            rc, out, err = run(*mode, "-i", fake)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("cannot read", err)


class CliFixTests(Base):
    def png(self, path):
        Path(path).write_bytes(png_bytes([(b"tEXt", b"k\x00" + SECRET)]))
        return Path(path)

    def test_root_gives_in_place_file_back_to_its_owner(self):
        p = self.png(self.dir / "a.png")
        st = p.stat()
        calls = []
        with mock.patch.object(cli.os, "geteuid", return_value=0), \
                mock.patch.object(cli.os, "fchown", side_effect=lambda fd, uid, gid: calls.append((uid, gid))):
            rc, out, err = run("-i", p)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(calls, [(st.st_uid, st.st_gid)])
        calls.clear()
        with mock.patch.object(cli.os, "geteuid", return_value=1000), \
                mock.patch.object(cli.os, "fchown", side_effect=lambda fd, uid, gid: calls.append((uid, gid))):
            self.assertEqual(run("-i", self.png(p))[0], 0)
        self.assertEqual(calls, [(-1, st.st_gid)])

    @unittest.skipIf(os.name == "nt", "POSIX symlinks")
    def test_output_swapped_for_symlink_cannot_redirect_chmod_chown_utime(self):
        """The directory may be controlled by someone else while root cleans: the finished temp file is
        handled through an O_NOFOLLOW descriptor, so a path swapped for a symlink or hard link is refused."""
        victim = self.dir / "victim.txt"
        victim.write_text("keep")
        victim.chmod(0o640)
        before = victim.stat()
        real_handler = cli._handler

        def swapping(src):
            inner = real_handler(src)

            def run_handler(s, dst, ctx):
                inner(s, dst, ctx)
                dst.unlink()
                dst.symlink_to(victim)
            return run_handler

        for extra in ((), ("--full",)):
            p = self.png(self.dir / "a.png")
            original = p.read_bytes()
            with mock.patch.object(cli, "_handler", swapping):
                rc, out, err = run("-i", *extra, p)
            self.assertEqual(rc, 1, out + err)
            self.assertNotIn(".metawipe-", out + err)
            after = victim.stat()
            self.assertEqual((after.st_mode, after.st_mtime_ns, after.st_uid), (before.st_mode, before.st_mtime_ns, before.st_uid))
            self.assertEqual(p.read_bytes(), original)
            self.assertEqual(victim.read_text(), "keep")

        hard = self.dir / "hard.txt"
        os.link(victim, hard)

        def hardlinking(src):
            inner = real_handler(src)

            def run_handler(s, dst, ctx):
                inner(s, dst, ctx)
                dst.unlink()
                os.link(victim, dst)
            return run_handler

        p = self.png(self.dir / "b.png")
        with mock.patch.object(cli, "_handler", hardlinking):
            rc, out, err = run("-i", p)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(victim.stat().st_mode, before.st_mode)

    @unittest.skipUnless(shutil.which("unshare"), "unshare missing")
    def test_root_in_user_namespace_keeps_owner(self):
        p = self.png(self.dir / "private.png")
        script = (f"chown 5:6 '{p}' && chmod 600 '{p}' && "
                  f"PYTHONPATH='{Path(__file__).resolve().parents[1]}' '{sys.executable}' -m metawipe -i '{p}' >/dev/null && "
                  f"stat -c '%u:%g %a' '{p}' && ! grep -q '{SECRET.decode()}' '{p}'")
        res = subprocess.run(["unshare", "--user", "--map-root-user", "--map-auto", "--", "sh", "-c", script],
                             capture_output=True, text=True)
        if res.returncode != 0 and ("map" in res.stderr or "Operation not permitted" in res.stderr or "chown" in res.stderr):
            self.skipTest(f"user namespaces with subordinate ids unavailable: {res.stderr.strip()[:200]}")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "5:6 600")  # was 0:6: the owner was locked out of a 0600 file

    def test_files_inside_output_directory_are_reported_as_skipped(self):
        src = self.dir / "in"
        (src / "sub" / "deeper").mkdir(parents=True)
        self.png(src / "a.png")
        self.png(src / "sub" / "b.png")
        self.png(src / "sub" / "deeper" / "c.png")
        rc, out, err = run("-v", "-r", "-o", src / "sub", src)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"{src / 'sub' / 'b.png'} (inside the output directory)", out)
        self.assertIn(f"{src / 'sub' / 'deeper' / 'c.png'} (inside the output directory)", out)
        self.assertIn("1 cleaned, 0 failed, 2 skipped", out)
        rc, out, err = run("-r", "-o", self.dir / "o2", "-f", src)
        self.assertIn("0 skipped", out)

    @unittest.skipIf(os.geteuid() == 0, "root ignores permissions")
    def test_no_temp_names_when_directory_is_not_writable(self):
        ro = self.dir / "ro"
        ro.mkdir()
        p = self.png(ro / "x.png")
        os.chmod(ro, 0o555)
        cwd = os.getcwd()
        os.chdir(self.dir)  # relative names: mkstemp returns relative temp paths on old Pythons
        try:
            for args in (("-i", "ro/x.png"), ("ro/x.png",)):
                rc, out, err = run(*args)
                self.assertEqual(rc, 1, out + err)
                self.assertNotIn(".metawipe-", err)
                self.assertIn("cannot create a file in ro", err)
        finally:
            os.chdir(cwd)
            os.chmod(ro, 0o755)  # not an addCleanup: tearDown removes the directory first
        self.assertIn(SECRET, p.read_bytes())

    def test_no_temp_names_in_os_errors_and_other_exceptions(self):
        p = self.png(self.dir / "x.png")
        (self.dir / "x.clean.png").mkdir()  # destination is a directory: os.replace fails
        rc, out, err = run("-f", p)
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn(".metawipe-", err)
        self.assertIn("x.clean.png", err)

        def broken(src, dst, ctx):
            raise ValueError(f"cannot parse {Path(dst).resolve()} (also known as {dst})")

        (self.dir / "x.clean.png").rmdir()
        with mock.patch.dict(cli.HANDLERS, {".png": broken}):
            rc, out, err = run(p)
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn(".metawipe-", err)
        self.assertIn(f"ValueError: cannot parse {self.dir / 'x.clean.png'}", err)

        def oserror(src, dst, ctx):
            raise PermissionError(13, "Permission denied", str(dst))

        with mock.patch.dict(cli.HANDLERS, {".png": oserror}):
            with self.assertRaises(PermissionError) as caught:
                cli.execute(cli.Job(p, self.dir / "y.png", False), mock.Mock(password=None, full=False))
        self.assertNotIn(".metawipe-", str(caught.exception))
        self.assertIn("y.png", str(caught.exception))

    def test_help_mentions_hevc(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            cli.main(["--help"])
        self.assertIn("H.264/HEVC", re.sub(r"\s+", " ", buf.getvalue()))


if __name__ == "__main__":
    unittest.main()
