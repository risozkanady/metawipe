"""Video metadata removal (iPhone .mov/.mp4/.m4v and friends) via ffmpeg, without re-encoding."""
from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
from pathlib import Path

from . import sei
from .common import Context, WipeError

VIDEO_EXT = {".mov", ".mp4", ".m4v", ".3gp"}
_OUTPUT_FORMAT = {".mov": "mov", ".mp4": "mp4", ".m4v": "mp4", ".3gp": "3gp"}


def _file_url(path: Path) -> str:
    # `file:` stops ffmpeg from reading names such as "async:clip.mov" as URLs of another protocol.
    return "file:" + str(path.resolve())


def _probe(src: Path) -> dict | None:
    """ffprobe's view of the streams; None when ffprobe is not installed."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    out = subprocess.run(
        [ffprobe, "-v", "error", "-protocol_whitelist", "file", "-f", "mov", "-print_format", "json",
         "-show_entries", "stream=codec_type:stream_tags=language"
         ":stream_disposition=attached_pic:stream_side_data=side_data_type", "-i", _file_url(src)],
        capture_output=True, text=True, errors="replace",
    )
    try:
        info = json.loads(out.stdout)
    except ValueError:
        info = None
    if out.returncode != 0 or not isinstance(info, dict):
        lines = out.stderr.strip().splitlines()
        raise WipeError(f"ffprobe cannot read the file: {lines[0] if lines else out.returncode}")
    return info


# ffmpeg fixes these itself (e.g. two packets with the same DTS in cut or screen-recorded files)
# and the copy is complete. Every other line on stderr ("partial file", "moov atom not found",
# "Invalid data found ...") means the input was damaged or not copied completely.
_BENIGN = re.compile(r"Non-monoton(ic|ous) DTS.*changing to|non monotonically increasing dts.*changing to", re.I)


# Boxes whose loss costs no media data: a cut inside them still leaves a complete video.
_DROPPABLE = {b"free", b"skip", b"wide", b"junk", b"mfra"}
_FIRST_BOXES = {b"ftyp", b"styp", b"moov", b"mdat", b"wide", b"pnot", b"uuid"} | _DROPPABLE
_MAX_MOOF = 256 << 20


def _runs(moof: bytes, moof_pos: int, defaults: dict):
    """Yield (data_start, data_end, explicit_base) of every trun in a moof; what the box cannot say is skipped."""
    pos, end = 16 if moof[:4] == b"\0\0\0\1" else 8, len(moof)
    while pos + 8 <= end:
        size, btype = struct.unpack_from(">I4s", moof, pos)
        if size < 8 or pos + size > end:
            return
        if btype == b"traf":
            yield from _traf_runs(moof, pos + 8, pos + size, moof_pos, defaults)
        pos += size


def _traf_runs(moof: bytes, pos: int, end: int, moof_pos: int, defaults: dict):
    base, explicit, default_size, cursor = moof_pos, False, 0, None
    while pos + 8 <= end:
        size, btype = struct.unpack_from(">I4s", moof, pos)
        if size < 8 or pos + size > end:
            return
        body, stop = pos + 8, pos + size
        if btype == b"tfhd" and stop - body >= 8:
            flags = int.from_bytes(moof[body:body + 4], "big")
            track = struct.unpack_from(">I", moof, body + 4)[0]
            p = body + 8
            default_size = defaults.get(track, 0)
            try:
                if flags & 0x1:
                    base, explicit = struct.unpack_from(">Q", moof, p)[0], True
                    p += 8
                if flags & 0x2:
                    p += 4
                if flags & 0x8:
                    p += 4
                if flags & 0x10:
                    default_size = struct.unpack_from(">I", moof, p)[0]
            except struct.error:
                return
            if stop < p:
                return
        elif btype == b"trun" and stop - body >= 8:
            flags = int.from_bytes(moof[body:body + 4], "big")
            count = struct.unpack_from(">I", moof, body + 4)[0]
            p = body + 8
            try:
                start = cursor if cursor is not None else base
                if flags & 0x1:
                    start = base + struct.unpack_from(">i", moof, p)[0]
                    p += 4
                if flags & 0x4:
                    p += 4
                if flags & 0x200:
                    row = 4 * (1 + bool(flags & 0x100) + bool(flags & 0x400) + bool(flags & 0x800))
                    if stop - p < count * row:
                        return
                    off = p + 4 * bool(flags & 0x100)
                    total = sum(struct.unpack_from(">I", moof, off + i * row)[0] for i in range(count))
                else:
                    total = count * default_size
            except struct.error:
                return
            cursor = start + total
            yield start, cursor, explicit
        pos += size


def check_complete(src: Path) -> None:
    """Fail when the file ends before its own box structure does (ffmpeg exits 0 on some truncated fragmented files).

    Top-level boxes must lie inside the file, and the samples addressed by every moof/trun must too (and inside
    the mdat that follows the moof when they are addressed relative to it). Anything that is not a box tree is
    left to ffprobe/ffmpeg, and a cut exactly between two boxes or fragments cannot be told from a complete file."""
    size = src.stat().st_size
    with open(src, "rb") as f:
        defaults: dict = {}
        pos = 0
        while pos + 8 <= size:
            f.seek(pos)
            head = f.read(16)
            length, btype, header = int.from_bytes(head[:4], "big"), head[4:8], 8
            if pos == 0 and btype not in _FIRST_BOXES or not all(32 <= c < 127 for c in btype):
                return  # not an ISO box tree
            if length == 1:
                length, header = int.from_bytes(head[8:16], "big"), 16
            elif length == 0:
                length = size - pos
            if length < header:
                return
            if pos + length > size:
                if btype in _DROPPABLE:
                    return
                raise WipeError(f"truncated video: the {btype.decode('latin-1')} box at byte {pos} runs {pos + length - size} bytes past the end of the file")
            if btype == b"moov" and length <= _MAX_MOOF:
                f.seek(pos)
                defaults = _trex_sizes(f.read(length))
            elif btype == b"moof" and length <= _MAX_MOOF:
                f.seek(pos)
                moof = f.read(length)
                f.seek(pos + length)
                nxt = f.read(16)
                mdat_end = size  # the mdat that follows holds the samples of a moof-relative fragment
                if len(nxt) >= 8 and nxt[4:8] == b"mdat":
                    n = int.from_bytes(nxt[:4], "big")
                    if n == 1 and len(nxt) == 16:
                        n = int.from_bytes(nxt[8:16], "big")
                    mdat_end = min(pos + length + n, size) if n else size
                for start, end, explicit in _runs(moof, pos, defaults):
                    if end > size or (not explicit and end > mdat_end):
                        raise WipeError(f"truncated video: the fragment at byte {pos} addresses samples past the end of the data")
            pos += length
        f.seek(pos)
        tail = f.read()
        if tail and (len(tail) < 4 or any(tail)):  # zero padding such as the QuickTime terminator is fine
            raise WipeError("truncated video: the file ends inside a box header")


def _trex_sizes(moov: bytes) -> dict:
    """track_id -> default_sample_size from moov/mvex/trex."""
    out: dict = {}

    def walk(pos: int, end: int, depth: int) -> None:
        while pos + 8 <= end:
            size, btype = struct.unpack_from(">I4s", moov, pos)
            if size < 8 or pos + size > end:
                return
            if btype == b"mvex" and depth == 0:
                walk(pos + 8, pos + size, 1)
            elif btype == b"trex" and size >= 32:
                out[struct.unpack_from(">I", moov, pos + 12)[0]] = struct.unpack_from(">I", moov, pos + 24)[0]
            pos += size

    walk(16 if moov[:4] == b"\0\0\0\1" else 8, len(moov), 0)
    return out


def video(src: Path, dst: Path, ctx: Context) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise WipeError("ffmpeg is required for video files (macOS: brew install ffmpeg)")
    suffix = src.suffix.lower()
    info = _probe(src)
    if info is None:
        if ctx.full:
            raise WipeError("ffprobe is required for --full on video (it comes with ffmpeg)")
        ctx.warn("ffprobe not found: track languages are not kept and Dolby Vision is not detected")
        info = {}
    streams = info.get("streams", [])

    # Only real video/audio/subtitle streams are mapped (`0:V` leaves out cover art), so timed-metadata
    # tracks (iPhone location, Live Photo still-image-time, ...) are dropped along with global
    # metadata and chapters. Track languages are not identifying and are written back.
    kept = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    kept += [s for s in streams if s.get("codec_type") == "audio"]
    kept += [s for s in streams if s.get("codec_type") == "subtitle"]
    languages = []
    for out_index, s in enumerate(kept):
        lang = s.get("tags", {}).get("language")
        if lang and lang != "und":
            languages += [f"-metadata:s:{out_index}", f"language={lang}"]
    if any(s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") for s in streams):
        ctx.warn("cover art was dropped")

    has_dovi = any(
        sd.get("side_data_type", "").startswith("DOVI") for s in streams for sd in s.get("side_data_list", [])
    )
    extra: list[str] = []
    if has_dovi:
        if suffix == ".mov":
            ctx.warn("Dolby Vision signalling cannot be written into .mov by ffmpeg and is lost (plays as HDR10/HLG base layer)")
        else:
            extra += ["-strict", "unofficial"]  # lets the mp4 muxer write the dvcC/dvvC box
    cmd = [
        ffmpeg, "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file", "-f", "mov",
        "-i", _file_url(src),
        "-map", "0:V?", "-map", "0:a?", "-map", "0:s?",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-c", "copy", *extra, *languages,
        "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
        "-movflags", "+faststart", "-f", _OUTPUT_FORMAT[suffix], _file_url(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    problems = [line for line in proc.stderr.splitlines() if line.strip() and not _BENIGN.search(line)]
    if proc.returncode != 0 or problems:
        raise WipeError(f"ffmpeg failed: {problems[0] if problems else proc.returncode}")
    check_complete(src)  # after ffmpeg's own diagnostics, which are more specific where it notices the damage
    if ctx.full:
        # x264/x265 write their version and settings as user-data SEI messages into the bitstream
        # (and x265 also into hvcC); they are overwritten in place, all other SEI is kept.
        sei.scrub(dst, ctx)
