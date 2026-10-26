"""Video metadata removal (iPhone .mov/.mp4/.m4v and friends) via ffmpeg, without re-encoding."""
from __future__ import annotations

import json
import re
import shutil
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
    if ctx.full:
        # x264/x265 write their version and settings as user-data SEI messages into the bitstream
        # (and x265 also into hvcC); they are overwritten in place, all other SEI is kept.
        sei.scrub(dst, ctx)
