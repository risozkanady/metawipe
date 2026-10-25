"""Video metadata removal (iPhone .mov/.mp4/.m4v and friends) via ffmpeg, without re-encoding."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .common import Context, WipeError

VIDEO_EXT = {".mov", ".mp4", ".m4v", ".3gp"}
_OUTPUT_FORMAT = {".mov": "mov", ".mp4": "mp4", ".m4v": "mp4", ".3gp": "3gp"}


def _file_url(path: Path) -> str:
    # `file:` stops ffmpeg from reading names such as "async:clip.mov" as URLs of another protocol.
    return "file:" + str(path.resolve())


def _probe(src: Path) -> dict:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return {}
    out = subprocess.run(
        [ffprobe, "-v", "error", "-protocol_whitelist", "file", "-f", "mov", "-print_format", "json",
         "-show_entries", "stream=codec_type,codec_name,color_transfer:stream_tags=language"
         ":stream_disposition=attached_pic:stream_side_data=side_data_type", "-i", _file_url(src)],
        capture_output=True, text=True,
    )
    try:
        return json.loads(out.stdout)
    except ValueError:
        return {}


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
    suffix = src.suffix.lower()
    info = _probe(src)
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

    codec = next((s.get("codec_name") for s in streams if s in kept and s.get("codec_type") == "video"), "")
    has_dovi = any(
        sd.get("side_data_type", "").startswith("DOVI") for s in streams for sd in s.get("side_data_list", [])
    )
    extra: list[str] = []
    if has_dovi:
        if suffix == ".mov":
            ctx.warn("Dolby Vision signalling cannot be written into .mov by ffmpeg and is lost (plays as HDR10/HLG base layer)")
        else:
            extra += ["-strict", "unofficial"]  # lets the mp4 muxer write the dvcC/dvvC box
    if ctx.full:
        # x264/x265 write their version and settings as "user data" SEI messages. SEI units cannot be
        # filtered by payload, so all SEI is dropped - except for HDR10, whose mastering-display
        # metadata lives there as well.
        hdr10 = any(s.get("color_transfer") == "smpte2084" for s in kept)
        sei = {"h264": "6", "hevc": "39|40"}.get(codec)
        if sei and hdr10:
            ctx.leftover("HDR10 video: encoder SEI messages left in place (they also carry the HDR metadata)")
        elif sei:
            extra += ["-bsf:v", f"filter_units=remove_types={sei}"]
        elif codec:
            ctx.leftover(f"{codec} video: encoder strings inside the bitstream cannot be removed")

    cmd = [
        ffmpeg, "-nostdin", "-v", "error", "-xerror", "-y", "-protocol_whitelist", "file", "-f", "mov",
        "-i", _file_url(src),
        "-map", "0:V?", "-map", "0:a?", "-map", "0:s?",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-c", "copy", *extra, *languages,
        "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
        "-movflags", "+faststart", "-f", _OUTPUT_FORMAT[suffix], _file_url(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    complaint = proc.stderr.strip().splitlines()[0] if proc.stderr.strip() else ""
    if proc.returncode != 0 or complaint:
        raise WipeError(f"ffmpeg failed: {complaint or proc.returncode}")
    if codec == "hevc" and ctx.full:
        _strip_hvcc_sei(dst)
