"""Interactive terminal UI (Textual): pick files, see what they carry, scrub them, see what is left."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, DirectoryTree, Footer, Header, Input, Label, RichLog, Select, Static, Switch

from . import __version__, cli, peek
from .common import WipeError

MODES = [("Copy next to the original (NAME.clean.EXT)", "copy"), ("Copy into an output folder", "folder"), ("Overwrite the originals", "inplace")]


class MarkTree(DirectoryTree):
    """Directory tree whose entries can be marked with space; marked ones get a check box."""

    BINDINGS = [Binding("space", "app.toggle_mark", "Mark", show=False)]  # replaces Tree's own space binding

    def __init__(self, path, marks: set, **kw):
        super().__init__(path, **kw)
        self.marks = marks

    def filter_paths(self, paths):
        return [p for p in paths if not p.name.startswith(".")]

    def render_label(self, node, base_style, style):
        label = super().render_label(node, base_style, style)
        entry = node.data
        if entry is None:
            return label
        box = "[x] " if entry.path in self.marks else "[ ] "
        supported = entry.path.is_dir() or cli.HANDLERS.get(entry.path.suffix.lower()) is not None
        out = Text.assemble((box, "bold green" if entry.path in self.marks else "dim"), label)
        if not supported:
            out.stylize("dim")
        return out


class Confirm(ModalScreen[bool]):
    BINDINGS = [Binding("y", "answer(True)", "Yes"), Binding("n,escape", "answer(False)", "No")]
    DEFAULT_CSS = """
    Confirm { align: center middle; }
    Confirm > Vertical { width: 64; height: auto; border: thick $error; background: $surface; padding: 1 2; }
    Confirm Horizontal { height: auto; margin-top: 1; }
    Confirm Button { margin-right: 2; }
    """

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.text)
            with Horizontal():
                yield Button("Yes (y)", id="yes", variant="error")
                yield Button("No (n)", id="no", variant="primary")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


class TuiReport(cli.Report):
    """cli.Report that reports into the log widget instead of stderr."""

    def __init__(self, emit):
        super().__init__(verbose=True)
        self.emit = emit

    def fail(self, path, msg: str) -> None:
        self.failed += 1
        self.emit(f"[red]error[/]  {path}: {msg}")

    def skip(self, path, why: str) -> None:
        self.skipped += 1
        self.emit(f"[dim]skip   {path} ({why})[/]")


class MetawipeApp(App):
    TITLE = "metawipe"
    SUB_TITLE = f"metadata scrubber {__version__}"
    CSS = """
    #left { width: 45%; border: round $primary; }
    #right { width: 55%; }
    #preview { height: 40%; border: round $primary; }
    #opts { height: auto; border: round $primary; padding: 0 1; }
    .row { height: 3; align: left middle; }
    .row Label { width: 22; }
    #log { height: 1fr; border: round $primary; }
    Select { width: 1fr; }
    """
    BINDINGS = [
        Binding("m", "toggle_mark", "Mark"),
        Binding("a", "mark_folder", "Mark all in folder"),
        Binding("c", "clear_marks", "Clear marks"),
        Binding("r", "run", "Run"),
        Binding("f", "toggle_full", "Full scrub"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, start: Path | None = None):
        super().__init__()
        self.start = Path(start or Path.cwd())
        self.marks: set[Path] = set()
        self.highlighted: Path | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="left"):
                yield MarkTree(self.start, self.marks, id="tree")
            with Vertical(id="right"):
                yield DataTable(id="preview", cursor_type="none", zebra_stripes=True)
                with Vertical(id="opts"):
                    with Horizontal(classes="row"):
                        yield Label("Full scrub (--full)")
                        yield Switch(value=True, id="full")
                    with Horizontal(classes="row"):
                        yield Label("Output")
                        yield Select(MODES, value="copy", allow_blank=False, id="mode")
                    with Horizontal(classes="row"):
                        yield Label("Output folder")
                        yield Input(placeholder="used for 'output folder' mode", id="outdir")
                    with Horizontal(classes="row"):
                        yield Label("Overwrite existing (-f)")
                        yield Switch(value=False, id="force")
                        yield Button("Run (r)", id="run", variant="success")
                yield RichLog(id="log", markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#left").border_title = str(self.start)
        self.query_one("#preview", DataTable).add_columns("Found in file", "Value")
        self.query_one("#preview").border_title = "Metadata preview"
        self.query_one("#log").border_title = "Results"
        self.query_one("#tree").focus()
        self.log_line("Mark files with [b]space[/] or [b]m[/], run with [b]r[/]. Marked folders are scanned recursively.")

    # ------------------------------------------------------------------ helpers
    def log_line(self, text: str) -> None:
        self.query_one("#log", RichLog).write(text)

    def _short(self, path: Path) -> str:
        try:
            return str(Path(path).resolve().relative_to(self.start.resolve()))
        except ValueError:
            return str(path)

    def _refresh_tree(self) -> None:
        self.query_one("#tree", MarkTree).refresh()

    # ------------------------------------------------------------------ marking
    def _current_path(self) -> Path | None:
        node = self.query_one("#tree", MarkTree).cursor_node
        return node.data.path if node is not None and node.data is not None else None

    def action_toggle_mark(self) -> None:
        path = self._current_path()
        if path is None:
            return
        self.marks.symmetric_difference_update({path})
        self._refresh_tree()
        self.sub_title = f"{len(self.marks)} marked"

    def action_mark_folder(self) -> None:
        path = self._current_path()
        folder = path if path is not None and path.is_dir() else (path.parent if path else self.start)
        found = [p for p in sorted(folder.iterdir()) if p.is_file() and cli.HANDLERS.get(p.suffix.lower())]
        self.marks.update(found)
        self._refresh_tree()
        self.sub_title = f"{len(self.marks)} marked"
        self.log_line(f"marked {len(found)} supported file(s) in {folder}")

    def action_clear_marks(self) -> None:
        self.marks.clear()
        self._refresh_tree()
        self.sub_title = "0 marked"

    def action_toggle_full(self) -> None:
        sw = self.query_one("#full", Switch)
        sw.value = not sw.value

    # ------------------------------------------------------------------ preview
    @on(DirectoryTree.NodeHighlighted)
    def highlighted_changed(self, event: DirectoryTree.NodeHighlighted) -> None:
        entry = event.node.data
        if entry is None:
            return
        self.highlighted = entry.path
        self.preview(entry.path)

    @work(thread=True, exclusive=True, group="preview")
    def preview(self, path: Path) -> None:
        if path.is_dir():
            rows = [("Folder", f"{sum(1 for p in path.iterdir() if p.is_file() and cli.HANDLERS.get(p.suffix.lower()))} supported file(s)")]
        else:
            rows = peek.inspect_file(path) or [("Nothing identifying found", "")]
        self.call_from_thread(self._show_preview, path, rows)

    def _show_preview(self, path: Path, rows) -> None:
        table = self.query_one("#preview", DataTable)
        table.clear()
        for key, value in rows:
            table.add_row(key, value)
        table.border_title = f"Metadata preview - {path.name}"

    # ------------------------------------------------------------------ run
    @on(Button.Pressed, "#run")
    def run_pressed(self) -> None:
        self.action_run()

    def _args(self) -> argparse.Namespace | None:
        mode = self.query_one("#mode", Select).value
        outdir = self.query_one("#outdir", Input).value.strip()
        if mode == "folder" and not outdir:
            self.notify("Enter an output folder first.", severity="error")
            return None
        return argparse.Namespace(
            paths=[str(p) for p in sorted(self.marks)], recursive=True, in_place=mode == "inplace",
            output_dir=Path(outdir).expanduser() if mode == "folder" else None,
            force=self.query_one("#force", Switch).value, password=None,
            full=self.query_one("#full", Switch).value, verbose=True,
        )

    def action_run(self) -> None:
        if not self.marks:
            self.notify("Nothing marked: press space on files or folders.", severity="warning")
            return
        args = self._args()
        if args is None:
            return
        if args.in_place:
            self.push_screen(Confirm(f"Overwrite {len(self.marks)} marked item(s) in place?\nThe originals cannot be recovered."),
                             lambda yes: self.start_run(args) if yes else self.log_line("[yellow]cancelled[/]"))
        else:
            self.start_run(args)

    @work(thread=True, exclusive=True, group="run")
    def start_run(self, args: argparse.Namespace) -> None:
        emit = lambda text: self.call_from_thread(self.log_line, text)  # noqa: E731
        report = TuiReport(emit)
        emit(f"[b]cleaning[/] {len(args.paths)} item(s){' (full scrub)' if args.full else ''} ...")
        jobs = cli.plan(args, report)
        incomplete = 0
        for job in jobs:
            try:
                ctx = cli.execute(job, args, warn=lambda msg, j=job: emit(f"[yellow]warn[/]   {j.src.name}: {msg}"))
            except WipeError as e:
                report.fail(job.src, str(e))
                continue
            except Exception as e:  # noqa: BLE001 - one bad file must not stop the batch
                report.fail(job.src, f"{type(e).__name__}: {e}")
                continue
            report.cleaned += 1
            left = peek.inspect_file(job.dst)
            note = ""
            if args.full and ctx.residue:
                incomplete += 1
                note = f" [yellow](INCOMPLETE: {len(ctx.residue)} item(s) left)[/]"
            emit(f"[green]clean[/]  {self._short(job.src)} -> {'in place' if job.inplace else self._short(job.dst)}{note}")
            if left:
                emit("        still carries: " + ", ".join(f"{k}" for k, _ in left[:6]) + (" ..." if len(left) > 6 else ""))
        emit(f"[b]done:[/] {report.cleaned} cleaned, {report.failed} failed, {report.skipped} skipped" + (f", {incomplete} incomplete" if args.full else ""))
        self.call_from_thread(self._refresh_tree)
        if self.highlighted is not None:
            self.call_from_thread(self.preview, self.highlighted)


def run(start: str | os.PathLike | None = None) -> int:
    MetawipeApp(Path(start) if start else None).run()
    return 0
