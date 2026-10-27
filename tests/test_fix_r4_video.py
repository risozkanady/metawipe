"""Regression tests for the round-4 video/CLI findings: truncated fragmented MP4 files were cleaned with exit 0
and lost most of their frames (ffmpeg says nothing), and the error for a destination that is a directory ended
in "-> None". Fixtures are built here; tests that need ffmpeg are skipped without it."""
import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from metawipe import cli, video
from metawipe.common import WipeError
from tests.test_fix_cli import X264, ff, frame_count
from tests.test_metawipe import SECRET, Base, run
from tests.test_regressions import png_bytes


def box(btype: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), btype) + payload


def synthetic_fragmented(sizes, mdat=b"", trex_size=0, with_size_field=True, mdat_len=None, tail=b""):
    """ftyp + moov(mvex/trex) + one moof/trun + mdat. The mdat holds sum(sizes) bytes unless `mdat` says otherwise."""
    trex = box(b"trex", struct.pack(">IIIIII", 0, 1, 1, 0, trex_size, 0))
    moov = box(b"moov", box(b"mvex", trex))
    flags = 0x201 if with_size_field else 0x001
    entries = b"".join(struct.pack(">I", n) for n in sizes) if with_size_field else b""

    def moof(offset):
        tfhd = box(b"tfhd", struct.pack(">II", 0x20000, 1))
        trun = box(b"trun", struct.pack(">IIi", flags, len(sizes), offset) + entries)
        return box(b"moof", box(b"mfhd", bytes(8)) + box(b"traf", tfhd + trun))

    head = moof(0)
    head = moof(len(head) + 8)
    body = mdat or bytes(sum(sizes) if with_size_field else len(sizes) * trex_size)
    return box(b"ftyp", b"isom" + bytes(4) + b"isom") + moov + head + struct.pack(">I4s", mdat_len if mdat_len is not None else 8 + len(body), b"mdat") + body + tail


def shift_data_offsets(moof: bytes, delta: int) -> bytes:
    """Add `delta` to the data_offset of every trun of a moof (after its mdat header grew)."""
    out = bytearray(moof)

    def walk(pos, end):
        while pos + 8 <= end:
            size, btype = struct.unpack_from(">I4s", out, pos)
            if btype == b"traf":
                walk(pos + 8, pos + size)
            elif btype == b"trun" and struct.unpack_from(">I", out, pos + 8)[0] & 1:
                struct.pack_into(">i", out, pos + 16, struct.unpack_from(">i", out, pos + 16)[0] + delta)
            pos += size

    walk(8, len(out))
    return bytes(out)


class StructuralCheck(Base):
    def check(self, data):
        p = self.dir / "s.mp4"
        p.write_bytes(data)
        video.check_complete(p)

    def test_complete_synthetic_fragmented_file_passes(self):
        self.check(synthetic_fragmented([10, 20, 30]))
        self.check(synthetic_fragmented([10, 20, 30], tail=box(b"free", bytes(100)) + box(b"skip", b"")))
        self.check(synthetic_fragmented([10, 20, 30], trex_size=10, with_size_field=False))  # sizes from trex
        self.check(synthetic_fragmented([10, 20, 30], mdat_len=0))  # size 0: the mdat runs to the end of the file
        self.check(synthetic_fragmented([10, 20, 30], tail=bytes(4)))  # QuickTime terminator
        self.check(b"")
        self.check(b"not an mp4 at all, but ffprobe has to say so")  # not ours to judge
        self.check(b"\0\0\x10\0RIFF" + bytes(100))

    def test_truncated_synthetic_files_fail(self):
        whole = synthetic_fragmented([100, 200, 300])
        for cut in (1, 3, 40, 100, 300):
            with self.assertRaises(WipeError, msg=cut) as cm:
                self.check(whole[:-cut])
            self.assertIn("truncated video", str(cm.exception))
        with self.assertRaises(WipeError):  # a moof cut in the middle
            self.check(whole[:whole.index(b"moof") + 30])
        with self.assertRaises(WipeError):  # a box header cut in the middle (the first two size bytes are zero)
            self.check(whole + b"\0\0")
        with self.assertRaises(WipeError):
            self.check(whole + b"\0\0\1")

    def test_samples_past_the_end_of_the_mdat_fail(self):
        # the mdat is complete and ends at EOF, but the trun asks for more than it holds
        with self.assertRaises(WipeError) as cm:
            self.check(synthetic_fragmented([100, 200, 300], mdat=bytes(150)))
        self.assertIn("truncated video", str(cm.exception))
        with self.assertRaises(WipeError):
            self.check(synthetic_fragmented([10, 20, 30], trex_size=10, with_size_field=False, mdat=bytes(25)))
        # the mdat is followed by another box: the trun may not reach into it
        with self.assertRaises(WipeError):
            self.check(synthetic_fragmented([100, 200, 300], mdat=bytes(150), tail=box(b"free", bytes(500))))

    def test_trailing_droppable_box_cut_off_is_not_data_loss(self):
        self.check(synthetic_fragmented([10]) + box(b"mfra", bytes(100))[:-30])

    def test_largesize_boxes(self):
        data = synthetic_fragmented([10, 20, 30])
        i = data.index(b"mdat") - 4
        mdat = data[i:]
        large = struct.pack(">I4sQ", 1, b"mdat", 16 + len(mdat) - 8) + mdat[8:]
        # the trun's data_offset (moof size + 8) grows by 8
        moof_start = data.index(b"moof") - 4
        whole = data[:moof_start] + shift_data_offsets(data[moof_start:i], 8) + large
        self.check(whole)
        with self.assertRaises(WipeError):
            self.check(whole[:-1])
        free = struct.pack(">I4sQ", 1, b"free", 16 + 5) + bytes(5)
        self.check(whole + free)
        self.check(whole + free[:-2])  # a cut-off free box loses nothing


@unittest.skipUnless(X264, "ffmpeg/ffprobe with libx264 missing")
class TruncatedVideos(Base):
    def make(self, name, *extra, gop=10, vcodec="libx264", acodec="aac"):
        path = self.dir / name
        ff("-f", "lavfi", "-i", "testsrc=d=3:s=160x120:r=25", "-f", "lavfi", "-i", "sine=d=3", "-c:v", vcodec, "-g", gop,
           "-c:a", acodec, *extra, path)
        return path

    def clean_ok(self, path, *mode):
        rc, out, err = run(*mode, "-i", path)
        self.assertEqual(rc, 0, out + err)
        return path

    def assert_rejected(self, path, *mode):
        before = path.read_bytes()
        rc, out, err = run(*mode, "-i", path)
        self.assertEqual(rc, 1, out + err)
        self.assertTrue(err.startswith(f"error  {path}: "), err)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sorted(f.name for f in self.dir.iterdir() if f.name.startswith(".metawipe-")), [])
        return err

    def test_truncated_fragmented_mp4_fails_in_both_modes(self):
        full = self.make("full.mp4", "-movflags", "frag_keyframe+empty_moov").read_bytes()
        for cut in (len(full) * 6 // 10, len(full) // 2, len(full) // 3):
            for mode in ((), ("--full",)):
                p = self.dir / "t.mp4"
                p.write_bytes(full[:cut])
                self.assert_rejected(p, *mode)
                p.unlink()

    def test_truncated_fragmented_mp4_that_ffmpeg_copies_silently(self):
        # ffmpeg exits 0 without a word and the copy has lost most frames and the whole audio track
        path = self.dir / "full.mp4"
        ff("-f", "lavfi", "-i", "testsrc=d=4:s=320x240:r=25", "-f", "lavfi", "-i", "sine=d=4", "-c:v", "libx264", "-b:v", "300k",
           "-c:a", "aac", "-movflags", "frag_keyframe+empty_moov", path)
        full = path.read_bytes()
        self.assertGreater(len(full), 60000)
        p = self.dir / "t.mp4"
        for cut in (60000, len(full) * 6 // 10, len(full) * 3 // 10, len(full) // 4, 28077, len(full) - 300):
            p.write_bytes(full[:cut])
            for mode in ((), ("--full",)):
                self.assert_rejected(p, *mode)

    def test_fragmented_cut_anywhere_inside_a_box_is_never_exit_0(self):
        full = self.make("full.mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof").read_bytes()
        boundaries, pos = {0}, 0
        while pos < len(full):
            pos += struct.unpack_from(">I", full, pos)[0]
            boundaries.add(pos)
        mfra = full.rindex(b"mfra") - 4 if b"mfra" in full else len(full)
        p = self.dir / "t.mp4"
        for cut in range(2000, mfra, 997):
            if cut in boundaries:
                continue
            p.write_bytes(full[:cut])
            rc, out, err = run("-i", p)
            self.assertEqual(rc, 1, f"cut at {cut} of {len(full)}: {out}{err}")

    def test_non_fragmented_cut_by_one_byte_fails(self):
        for flags in ("+faststart", "+write_colr"):
            full = self.make("n.mp4", "-movflags", flags).read_bytes()
            p = self.dir / "t.mp4"
            p.write_bytes(full[:-1])
            self.assert_rejected(p)
            self.assert_rejected(p, "--full")

    def test_valid_files_are_still_cleaned(self):
        variants = {
            "faststart.mp4": ("-movflags", "+faststart"),
            "plain.mp4": (),
            "frag.mp4": ("-movflags", "frag_keyframe+empty_moov"),
            "dashish.mp4": ("-movflags", "frag_keyframe+empty_moov+default_base_moof"),
            "every.mp4": ("-movflags", "frag_every_frame+empty_moov+default_base_moof"),
            "sep.mp4": ("-movflags", "frag_keyframe+empty_moov+separate_moof+omit_tfhd_offset"),
            "a.mov": (),
            "a.m4v": (),
            "a.3gp": (),
        }
        for name, extra in variants.items():
            with self.subTest(name):
                src = self.make(name, *extra)
                video.check_complete(src)
                rc, o, e = run("-o", self.dir / "out", src)
                self.assertEqual(rc, 0, o + e)
                self.assertEqual(frame_count(self.dir / "out" / name), "75")

    def test_valid_file_with_trailing_boxes_size_zero_and_largesize(self):
        src = self.make("faststart.mp4", "-movflags", "+faststart").read_bytes()
        i = src.rindex(b"mdat") - 4
        # mdat with size 0 (to the end of the file), then the same file with trailing free/skip boxes
        zero = self.dir / "zero.mp4"
        zero.write_bytes(src[:i] + bytes(4) + src[i + 4:])
        free = self.dir / "free.mp4"
        free.write_bytes(src + box(b"free", bytes(50)) + box(b"skip", bytes(5)))
        for p in (zero, free):
            with self.subTest(p.name):
                self.assertEqual(run("-i", p)[0], 0)
        frag = self.make("f.mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof", gop=250).read_bytes()
        j = frag.rindex(b"mdat") - 4
        moof_start = frag.rindex(b"moof") - 4
        size = struct.unpack_from(">I", frag, j)[0]
        large = (frag[:moof_start] + shift_data_offsets(frag[moof_start:j], 8) + struct.pack(">I4sQ", 1, b"mdat", size + 8)
                 + frag[j + 8:])
        p = self.dir / "large.mp4"
        p.write_bytes(large)
        rc, out, err = run("-o", self.dir / "o2", p)
        self.assertEqual(rc, 0, out + err)
        p.write_bytes(large[:len(large) // 2])
        self.assertEqual(run("-i", p)[0], 1)


class DestinationErrors(Base):
    def png(self, path):
        path.write_bytes(png_bytes())
        return path

    def test_destination_is_a_directory(self):
        p = self.png(self.dir / "x.png")
        dst = self.dir / "x.clean.png"
        dst.mkdir()
        rc, out, err = run("-f", p)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(err, f"error  {p}: IsADirectoryError: [Errno 21] Is a directory: '{dst}'\n")
        # the same through -o with a relative path
        cwd = os.getcwd()
        os.chdir(self.dir)
        try:
            (self.dir / "o").mkdir()
            (self.dir / "o" / "x.png").mkdir()
            rc, out, err = run("-f", "-o", "o", "x.png")
        finally:
            os.chdir(cwd)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(err, f"error  x.png: IsADirectoryError: [Errno 21] Is a directory: '{Path('o') / 'x.png'}'\n")
        self.assertEqual([f.name for f in self.dir.iterdir() if f.name.startswith(".metawipe-")], [])

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0, "root (and Windows) ignore these permission bits")
    def test_unwritable_directory(self):
        ro = self.dir / "ro"
        ro.mkdir()
        p = self.png(ro / "x.png")
        os.chmod(ro, 0o555)
        try:
            rc, out, err = run(p)
        finally:
            os.chmod(ro, 0o755)
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(err, f"error  {p}: cannot create a file in {ro}: Permission denied\n")

    def test_handler_errors_with_a_second_file_name(self):
        p = self.png(self.dir / "x.png")
        other = self.dir / "elsewhere" / "y.png"
        calls = []

        def raising(make):
            def handler(src, dst, ctx):
                raise make(dst)
            return handler

        cases = [
            (lambda dst: OSError(18, "Invalid cross-device link", str(dst), None, str(other)),
             f"OSError: [Errno 18] Invalid cross-device link: '{self.dir / 'x.clean.png'}' -> '{other}'"),
            (lambda dst: OSError(18, "Invalid cross-device link", str(p), None, str(dst)),
             f"OSError: [Errno 18] Invalid cross-device link: '{p}' -> '{self.dir / 'x.clean.png'}'"),
            (lambda dst: PermissionError(13, "Permission denied", str(dst), None, str(dst)),
             f"PermissionError: [Errno 13] Permission denied: '{self.dir / 'x.clean.png'}'"),
            (lambda dst: PermissionError(13, "Permission denied", os.path.realpath(dst)),
             f"PermissionError: [Errno 13] Permission denied: '{self.dir / 'x.clean.png'}'"),
        ]
        for make, expected in cases:
            with self.subTest(expected), mock.patch.dict(cli.HANDLERS, {".png": raising(make)}):
                rc, out, err = run(p)
                self.assertEqual(rc, 1, out + err)
                self.assertEqual(err, f"error  {p}: {expected}\n")
                self.assertNotIn(".metawipe-", err)
                self.assertNotIn("None", err)


if __name__ == "__main__":
    unittest.main()
