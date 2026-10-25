"""Command line interface: collect files, plan outputs, run the per-type handlers."""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import __version__, images, isobmff, office, pdf, video
from .common import Context, WipeError

HANDLERS = {
    ".jpg": images.jpeg, ".jpeg": images.jpeg, ".jpe": images.jpeg,
    ".png": images.png, ".apng": images.png,
    ".webp": images.webp,
    ".tif": images.tiff, ".tiff": images.tiff,
    ".heic": isobmff.heif, ".heif": isobmff.heif, ".avif": isobmff.heif,
    ".pdf": pdf.clean_pdf,
    **{ext: office.package for ext in office.OOXML_EXT | office.ODF_EXT},
    **{ext: video.video for ext in video.VIDEO_EXT},
}
_OUTPUT_NAME = re.compile(r"^(?P<stem>.+)\.clean(?P<ext>\.[^.]+)$")


@dataclass
class Job:
    src: Path
    dst: Path
    inplace: bool


_FULL_MTIME = 315532800  # 1980-01-01T00:00:00Z


class Report:
    def __init__(self, verbose: bool):
        self.verbose = verbose
        self.cleaned = self.failed = self.skipped = self.incomplete = 0

    def fail(self, path, msg: str) -> None:
        self.failed += 1
        print(f"error  {path}: {msg}", file=sys.stderr)

    def skip(self, path, why: str) -> None:
        self.skipped += 1
        if self.verbose:
            print(f"skip   {path} ({why})")


def _handler(path: Path):
    return HANDLERS.get(path.suffix.lower())


def _is_transient(name: str) -> bool:
    return name.startswith("~$") or name.startswith(".metawipe-")


def _dst_for(src: Path, root: Path | None, args) -> Path:
    if args.in_place:
        return src
    if args.output_dir:
        return args.output_dir / (src.relative_to(root) if root else src.name)
    return src.with_name(f"{src.stem}.clean{src.suffix}")


def _collect(args, report: Report) -> list[Job]:
    jobs: list[Job] = []
    out_real = os.path.realpath(args.output_dir) if args.output_dir else None

    def add(src: Path, root: Path | None, explicit: bool) -> None:
        if _is_transient(src.name):
            return
        if _handler(src) is None:
            if explicit:
                report.fail(src, "unsupported file type (nothing was cleaned)")
            else:
                report.skip(src, "unsupported type")
            return
        if not (args.in_place or args.output_dir):
            m = _OUTPUT_NAME.match(src.name)
            if m and (src.parent / (m["stem"] + m["ext"])).exists():
                report.skip(src, "looks like the output of an earlier run")
                return
        jobs.append(Job(src, _dst_for(src, root, args), args.in_place))

    def scan_error(err: OSError) -> None:
        report.fail(err.filename, f"cannot read directory: {err.strerror}")

    for raw in args.paths:
        p = Path(raw)
        if not os.path.lexists(p):
            report.fail(p, "not found")
        elif p.is_dir():
            if args.recursive:
                for dirpath, dirnames, filenames in os.walk(p, onerror=scan_error):
                    for d in list(dirnames):
                        full = os.path.join(dirpath, d)
                        if os.path.islink(full):
                            report.skip(full, "symlinked directory")
                            dirnames.remove(d)
                        elif out_real and os.path.realpath(full) == out_real:
                            dirnames.remove(d)
                    for f in sorted(filenames):
                        add(Path(dirpath, f), p, False)
            else:
                try:
                    names = sorted(e.name for e in os.scandir(p) if e.is_file())
                except OSError as e:
                    report.fail(p, f"cannot read directory: {e.strerror}")
                    continue
                for f in names:
                    add(p / f, p, False)
        else:
            add(p, None, True)
    return jobs


def _validate(jobs: list[Job], args, report: Report) -> list[Job]:
    ok: list[Job] = []
    seen_src: set[str] = set()
    for job in jobs:
        key = os.path.realpath(job.src)
        if key in seen_src:
            continue
        seen_src.add(key)
        ok.append(job)
    srcs = {os.path.realpath(j.src) for j in ok}
    claimed: dict[str, Path] = {}
    final: list[Job] = []
    for job in ok:
        if job.inplace:
            if job.src.is_symlink():
                report.fail(job.src, "is a symlink; refusing to replace it in place (use the real path)")
                continue
            if job.src.stat().st_nlink > 1:
                report.fail(job.src, "has other hard links; in-place cleaning would leave them uncleaned")
                continue
        else:
            dst_key = os.path.abspath(job.dst)
            if os.path.realpath(job.dst) in srcs:
                report.fail(job.src, f"output {job.dst} is also an input")
                continue
            if dst_key in claimed:
                report.fail(job.src, f"output {job.dst} would overwrite the output for {claimed[dst_key]}")
                continue
            if os.path.lexists(job.dst) and not args.force:
                report.fail(job.src, f"{job.dst} already exists (use --force to overwrite)")
                continue
            claimed[dst_key] = job.src
        final.append(job)
    return final


def _run(job: Job, args) -> Context:
    ctx = Context(password=args.password, full=args.full,
                  warn=lambda msg: print(f"warn   {job.src}: {msg}", file=sys.stderr))
    job.dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=job.dst.parent, prefix=".metawipe-", suffix=job.src.suffix)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        _handler(job.src)(job.src, tmp, ctx)
        shutil.copymode(job.src, tmp)
        if args.full:
            os.utime(tmp, (_FULL_MTIME, _FULL_MTIME))
        os.replace(tmp, job.dst)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return ctx


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except AttributeError:
            pass
    ap = argparse.ArgumentParser(
        prog="metawipe",
        description="Strip metadata from images, HEIC, videos, PDFs and Office/OpenDocument files.",
    )
    ap.add_argument("paths", nargs="+", help="files or directories")
    ap.add_argument("-r", "--recursive", action="store_true", help="recurse into directories")
    ap.add_argument("-i", "--in-place", action="store_true", help="overwrite originals (default: write NAME.clean.EXT)")
    ap.add_argument("-o", "--output-dir", type=Path, help="write cleaned copies here, keeping the folder structure")
    ap.add_argument("-f", "--force", action="store_true", help="overwrite existing output files")
    ap.add_argument("-p", "--password", help="password for encrypted PDFs")
    ap.add_argument("--full", action="store_true", help=(
        "full scrub: also anonymise comment/revision authors and dates, clean pictures embedded in "
        "documents, drop customXml/printer settings, strip encoder SEI from H.264, set mtime to 1980; "
        "exit status 2 if something identifying had to be left"))
    ap.add_argument("-v", "--verbose", action="store_true", help="also list skipped files")
    ap.add_argument("--version", action="version", version=f"metawipe {__version__}")
    args = ap.parse_args(argv)
    if args.in_place and args.output_dir:
        ap.error("--in-place and --output-dir are mutually exclusive")

    report = Report(args.verbose)
    jobs = _validate(_collect(args, report), args, report)
    for job in jobs:
        try:
            ctx = _run(job, args)
        except WipeError as e:
            report.fail(job.src, str(e))
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the batch
            report.fail(job.src, f"{type(e).__name__}: {e}")
        else:
            report.cleaned += 1
            target = "in place" if job.inplace else f"-> {job.dst}"
            partial = ""
            if args.full and ctx.residue:
                report.incomplete += 1
                partial = f" (INCOMPLETE: {len(ctx.residue)} item(s) left, see warnings)"
            print(f"clean  {job.src} {target}{partial}")
    summary = f"done: {report.cleaned} cleaned, {report.failed} failed, {report.skipped} skipped"
    if args.full:
        summary += f", {report.incomplete} incomplete"
    print(summary)
    return 1 if report.failed else (2 if report.incomplete else 0)
