"""Video metadata removal (iPhone .mov/.mp4/.m4v and friends) via ffmpeg, without re-encoding."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .common import Context, WipeError

VIDEO_EXT = {".mov", ".mp4", ".m4v", ".3gp"}


def _probe(src: Path, entry: str) -> str:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return ""
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", f"stream={entry}", "-of", "csv=p=0", str(src)],
        capture_output=True, text=True,
    )
    return out.stdout.strip()


def _strip_hvcc_sei(path: Path) -> int:
    """Blank SEI arrays (x265 puts its version/options string there) inside hvcC boxes, in place.

    The shrunk hvcC is followed by a `free` box of the freed size, so no offset in the file moves.
    Returns the number of SEI arrays removed.
    """
    buf = bytearray(path.read_bytes())
    removed = 0
    pos = buf.find(b"hvcC")
    while pos != -1:
        start = pos - 4
        size = int.from_bytes(buf[start:pos], "big")
        end = start + size
        if start >= 0 and size > 30 and end <= len(buf):
            header, n_arrays, p = bytes(buf[pos + 4:pos + 26]), buf[pos + 26], pos + 27
            kept, dropped = [], 0
            try:
                for _ in range(n_arrays):
                    first, count = buf[p], int.from_bytes(buf[p + 1:p + 3], "big")
                    q = p + 3
                    for _ in range(count):
                        q += 2 + int.from_bytes(buf[q:q + 2], "big")
                    if (first & 0x3F) in (39, 40):
                        dropped += 1
                    else:
                        kept.append(bytes(buf[p:q]))
                    p = q
            except IndexError:
                raise WipeError("corrupt hvcC box")
            if dropped:
                payload = header + bytes([len(kept)]) + b"".join(kept)
                new_size = 8 + len(payload)
                spare = size - new_size
                if spare < 8:
                    raise WipeError("hvcC SEI too small to blank safely")
                buf[start:end] = (
                    new_size.to_bytes(4, "big") + b"hvcC" + payload + spare.to_bytes(4, "big") + b"free" + bytes(spare - 8)
                )
                removed += dropped
        pos = buf.find(b"hvcC", pos + 4)
    if removed:
        path.write_bytes(bytes(buf))
    return removed


def video(src: Path, dst: Path, ctx: Context) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise WipeError("ffmpeg is required for video files (macOS: brew install ffmpeg)")
    fmt = {".mov": "mov", ".mp4": "mp4", ".m4v": "ipod", ".3gp": "3gp"}[src.suffix.lower()]
    # Only video/audio/subtitle streams are mapped, so timed-metadata tracks (iPhone location,
    # Live Photo still-image-time, ...) are dropped along with global metadata and chapters.
    cmd = [
        ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(src),
        "-map", "0:v?", "-map", "0:a?", "-map", "0:s?",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-c", "copy", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
        "-movflags", "+faststart", "-f", fmt, str(dst),
    ]
    codec = ""
    if ctx.full:
        # x264/x265 write their version and settings as "user data" SEI messages. SEI units cannot be
        # filtered by payload, so all SEI is dropped - except for HDR10, whose mastering-display
        # metadata lives there as well.
        codec = _probe(src, "codec_name")
        hdr10 = _probe(src, "color_transfer") == "smpte2084"
        sei = {"h264": "6", "hevc": "39|40"}.get(codec)
        if sei and hdr10:
            ctx.leftover("HDR10 video: encoder SEI messages left in place (they also carry the HDR metadata)")
        elif sei:
            insert = cmd.index("-c") + 2
            cmd[insert:insert] = ["-bsf:v", f"filter_units=remove_types={sei}"]
        elif codec:
            ctx.leftover(f"{codec} video: encoder strings inside the bitstream cannot be removed")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise WipeError(f"ffmpeg failed: {proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else proc.returncode}")
    if codec == "hevc":
        _strip_hvcc_sei(dst)
