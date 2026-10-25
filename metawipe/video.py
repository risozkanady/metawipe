"""Video metadata removal (iPhone .mov/.mp4/.m4v and friends) via ffmpeg, without re-encoding."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .common import Context, WipeError

VIDEO_EXT = {".mov", ".mp4", ".m4v", ".3gp"}


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
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise WipeError(f"ffmpeg failed: {proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else proc.returncode}")
