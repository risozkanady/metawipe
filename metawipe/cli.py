"""Command line interface: collect files, plan outputs, run the per-type handlers."""
from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import stat
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
    """Lock files, our own temp files and macOS AppleDouble sidecars; only skipped while scanning."""
    return name.startswith(("~$", ".metawipe-", "._"))


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
        if not explicit and _is_transient(src.name):
            report.skip(src, "lock/temp/sidecar file")
            return
        try:
            regular = stat.S_ISREG(os.stat(src).st_mode)
        except OSError as e:
            report.fail(src, e.strerror or str(e))
            return
        if not regular:
            if explicit:
                report.fail(src, "not a regular file")
            else:
                report.skip(src, "not a regular file")
            return
        if _handler(src) is None:
            if explicit:
                report.fail(src, "unsupported file type (nothing was cleaned)")
            else:
                report.skip(src, "unsupported type")
            return
        if not explicit and not (args.in_place or args.output_dir):
            m = _OUTPUT_NAME.match(src.name)
            if m and (src.parent / (m["stem"] + m["ext"])).exists():
                report.skip(src, "looks like the output of an earlier run")
                return
        jobs.append(Job(src, _dst_for(src, root, args), args.in_place))

    def scan_error(err: OSError) -> None:
        report.fail(err.filename, f"cannot read directory: {err.strerror}")

    for raw in args.paths:
        if not raw:
            report.fail("''", "empty path argument")
            continue
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
                            dirnames.remove(d)  # earlier outputs are not inputs; say so for what is in there
                            for sub, subdirs, files in os.walk(full):
                                subdirs.sort()
                                for f in sorted(files):
                                    report.skip(Path(sub, f), "inside the output directory")
                    for f in sorted(filenames):
                        add(Path(dirpath, f), p, False)
            else:
                try:
                    entries = sorted(os.scandir(p), key=lambda e: e.name)
                except OSError as e:
                    report.fail(p, f"cannot read directory: {e.strerror}")
                    continue
                for e in entries:
                    if e.is_dir(follow_symlinks=False):
                        report.skip(e.path, "directory; use -r to descend")
                    else:
                        add(Path(e.path), p, False)
        else:
            add(p, None, True)
    return jobs


def _validate(jobs: list[Job], args, report: Report) -> list[Job]:
    ok: list[Job] = []
    index: dict[str, int] = {}
    for job in jobs:
        key = os.path.realpath(job.src)
        if key not in index:
            index[key] = len(ok)
            ok.append(job)
        elif ok[index[key]].src.is_symlink() and not job.src.is_symlink():
            ok[index[key]] = job  # the real file wins over a symlink to it
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
            if not os.access(job.src, os.W_OK):
                report.fail(job.src, "is read-only; make it writable or use -o")
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


def _missing_dirs(path: Path) -> list[Path]:
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    return missing


def _hide_temp(text: str, tmp: Path, dst: Path) -> str:
    """Messages name the destination, never our temp file (in any of the spellings tools use)."""
    for name in sorted({str(tmp.resolve()), os.path.abspath(tmp), str(tmp)}, key=len, reverse=True):
        text = text.replace(name, str(dst))
    return text.replace(tmp.name, dst.name)


def _os_error_for(e: OSError, tmp: Path, dst: Path) -> OSError:
    """The same error with our temp file replaced by the destination in its file names. The message is rebuilt
    (OSError prints "-> None" when filename2 is merely set to None) and names the destination only once."""
    names = [n for n in (e.filename, e.filename2) if n is not None]
    if not names or e.errno is None or not any(_hide_temp(str(n), tmp, dst) != str(n) for n in names):
        return e
    first, second = (_hide_temp(str(n), tmp, dst) if n is not None else None for n in (e.filename, e.filename2))
    if second == first:
        second = None
    new = type(e)(e.errno, e.strerror, first, None, second)
    new.__cause__ = e
    return new


def _finish(tmp: Path, st: os.stat_result, inplace: bool, full: bool) -> None:
    """Permissions, owner and mtime of the finished temp file, applied through an open descriptor so a path swapped
    for a symlink or a hard link (the directory may belong to somebody else when running as root) cannot redirect them."""
    mode = stat.S_IMODE(st.st_mode) & 0o777  # no setuid/setgid/sticky on outputs
    if os.name == "nt":
        os.chmod(tmp, mode)
        if full:
            os.utime(tmp, (_FULL_MTIME, _FULL_MTIME))
        return
    fd = os.open(tmp, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        mine = os.fstat(fd)
        if not stat.S_ISREG(mine.st_mode) or mine.st_nlink != 1 or mine.st_uid not in (os.geteuid(), os.getuid()):
            raise WipeError("the output file was replaced while it was being written")
        os.fchmod(fd, mode)
        if inplace:
            try:
                # root (sudo): give the file back to its owner; others keep the group where they are allowed to
                os.fchown(fd, st.st_uid if os.geteuid() == 0 else -1, st.st_gid)
            except OSError:
                pass
        if full:
            os.utime(fd, (_FULL_MTIME, _FULL_MTIME))
    finally:
        os.close(fd)


def _run(job: Job, args, warn=None) -> Context:
    ctx = Context(password=args.password, full=args.full,
                  warn=warn or (lambda msg: print(f"warn   {job.src}: {msg}", file=sys.stderr)))
    created = _missing_dirs(job.dst.parent)
    job.dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    ok = False
    try:
        try:
            fd, tmp_name = tempfile.mkstemp(dir=job.dst.parent, prefix=".metawipe-", suffix=job.src.suffix)
        except OSError as e:
            raise WipeError(f"cannot create a file in {job.dst.parent}: {e.strerror or e}") from None
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            _handler(job.src)(job.src, tmp, ctx)
            _finish(tmp, job.src.stat(), job.inplace, args.full)
            os.replace(tmp, job.dst)
        except WipeError as e:
            raise WipeError(_hide_temp(str(e), tmp, job.dst)) from e
        except Exception as e:
            # e.g. full disk, unwritable directory, destination is a directory
            err = _os_error_for(e, tmp, job.dst) if isinstance(e, OSError) else e
            text = str(err)
            if _hide_temp(text, tmp, job.dst) != text:
                raise WipeError(f"{type(err).__name__}: {_hide_temp(text, tmp, job.dst)}") from e
            if err is not e:
                raise err from e
            raise
        ok = True
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        if not ok:
            for d in created:  # directories this job created and left empty
                try:
                    d.rmdir()
                except OSError:
                    break
    return ctx


def plan(args, report: Report) -> list[Job]:
    """Collect and validate the files to clean (also used by the TUI)."""
    return _validate(_collect(args, report), args, report)


def execute(job: Job, args, warn=None) -> Context:
    """Clean one planned file; raises WipeError/OSError on failure (also used by the TUI)."""
    return _run(job, args, warn)


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
    ap.add_argument("paths", nargs="*", help="files or directories (none: open the interactive UI)")
    ap.add_argument("--tui", action="store_true", help="open the interactive terminal UI")
    ap.add_argument("-r", "--recursive", action="store_true", help="recurse into directories")
    ap.add_argument("-i", "--in-place", action="store_true", help="overwrite originals (default: write NAME.clean.EXT)")
    ap.add_argument("-o", "--output-dir", type=Path, help="write cleaned copies here, keeping the folder structure")
    ap.add_argument("-f", "--force", action="store_true", help="overwrite existing output files")
    ap.add_argument("-p", "--password", help="password for encrypted PDFs")
    ap.add_argument("--full", action="store_true", help=(
        "full scrub: also anonymise comment/revision authors and dates, clean pictures embedded in "
        "documents, drop customXml/printer settings, remove H.264/HEVC encoder strings (x264/x265 SEI), set mtime to 1980; "
        "exit status 2 if something identifying had to be left"))
    ap.add_argument("-v", "--verbose", action="store_true", help="also list skipped files")
    ap.add_argument("--version", action="version", version=f"metawipe {__version__}")
    args = ap.parse_args(argv)
    if args.in_place and args.output_dir:
        ap.error("--in-place and --output-dir are mutually exclusive")
    if args.tui or (not args.paths and sys.stdin.isatty() and sys.stdout.isatty()):
        try:
            from . import tui
        except ImportError:
            ap.error("the interactive UI needs Textual: pip install 'metawipe[tui]'")
        return tui.run(args.paths[0] if args.paths else None)
    if not args.paths:
        ap.error("no files given (run without arguments in a terminal for the interactive UI)")

    def _terminate(signum, frame):
        raise KeyboardInterrupt

    for sig in ("SIGTERM", "SIGHUP"):  # run the cleanup in `finally` blocks instead of leaving temp files
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), _terminate)

    report = Report(args.verbose)
    try:
        return _main(args, report)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def _main(args, report: Report) -> int:
    jobs = plan(args, report)
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
