"""Interactive terminal UI (Textual): pick files, see what they carry, scrub them, see what is left."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, DataTable, DirectoryTree, Footer, Header, Input, Label, RichLog, Select, Static, Switch

from . import __version__, cli, peek
from .common import WipeError

MODES = [("Copy next to the original (NAME.clean.EXT)", "copy"), ("Copy into a folder", "folder"), ("Overwrite the originals", "inplace")]

# ink-blue base; coral = tells where or who, amber = tells which device or program, sea = clean
INK, SLATE, PANEL, PAPER = "#17202b", "#1e2937", "#263446", "#e6e9ee"
STEEL, CORAL, AMBER, SEA, MUTED = "#7aa2d6", "#e8766c", "#e3b23c", "#62b7a0", "#8696aa"
THEME = Theme(name="metawipe", primary=STEEL, secondary="#8c8fd6", warning=AMBER, error=CORAL, success=SEA, accent=AMBER,
              foreground=PAPER, background=INK, surface=SLATE, panel=PANEL, dark=True)

DEFAULT_THEME = "metawipe"


def config_path() -> Path:
    """$XDG_CONFIG_HOME/metawipe/config.json (default ~/.config/metawipe/config.json)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "metawipe" / "config.json"


def load_config() -> dict:
    """Saved settings; a missing or broken file means defaults."""
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(**changes) -> None:
    """Merge changes into the config file (atomic replace, keeps keys it does not know)."""
    path = config_path()
    data = {**load_config(), **changes}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


_WHERE_OR_WHO = ("gps", "location", "coordinates", "author", "creator", "artist", "owner", "company", "manager", "people", "printer", "signature")
_DEVICE = ("make", "model", "lens", "serial", "software", "host", "application", "makernote", "xmp", "iptc", "photoshop",
           "file id", "custom", "comment", "thumbnail", "cover", "timed metadata")


def severity(key: str) -> int:
    """0 = reveals where or who, 1 = reveals device or program, 2 = everything else (dates, sizes, ...)."""
    k = key.lower()
    if any(w in k for w in _WHERE_OR_WHO):
        return 0
    return 1 if any(w in k for w in _DEVICE) else 2


_SEVERITY_STYLE = (CORAL, AMBER, MUTED)
STATE_GLYPH = {"done": ("✓ ", SEA), "incomplete": ("◐ ", AMBER), "failed": ("✗ ", CORAL)}


class MarkTree(DirectoryTree):
    """Directory tree whose entries can be marked with space; after a run each file shows how it went."""

    ICON_NODE, ICON_NODE_EXPANDED, ICON_FILE = "▸ ", "▾ ", "  "
    BINDINGS = [Binding("space", "app.toggle_mark", "Mark", show=False)]  # replaces Tree's own space binding

    def __init__(self, path, marks: set, states: dict, **kw):
        super().__init__(path, **kw)
        self.marks = marks
        self.states = states

    def filter_paths(self, paths):
        return [p for p in paths if not p.name.startswith(".")]

    def render_label(self, node, base_style, style):
        label = super().render_label(node, base_style, style)
        entry = node.data
        if entry is None:
            return label
        path = entry.path
        if not (path.is_dir() or cli.HANDLERS.get(path.suffix.lower()) is not None):
            label.stylize("dim")
            return Text.assemble("  ", label)  # nothing to mark: metawipe does not handle this type
        if path in self.marks:
            mark = ("▣ ", f"bold {SEA}")
        else:
            mark = STATE_GLYPH.get(self.states.get(path), ("▢ ", "dim"))
        return Text.assemble(mark, label)


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


HELP = """[b]metawipe[/b] - removes identifying metadata (location, device, author, dates, ...) from files.

[b]Screen[/b]
  Left   file tree. [green]▣[/] marked, ▢ not marked; dimmed names are types metawipe cannot clean (they cannot be marked).
         After a run each file shows how it went: [green]✓[/] cleaned, [yellow]◐[/] cleaned but something had to stay, [red]✗[/] failed.
  Right  top: what the highlighted file reveals (Make/Model, GPS, authors, comment authors, thumbnails, embedded
         pictures, video location, ...). [red]●[/] tells where or who, [yellow]●[/] the device or program, [dim]●[/] the rest.
         A folder shows how many files it holds. Middle: options.  Bottom: [b]Results[/b] log.

[b]Keys[/b]
  up/down, Enter  move, open/close folders        m or space  mark / unmark file or folder (folders: recursive)
  a  mark all supported files in this folder      c  clear all marks
  r  clean everything marked (marks clear)        f  toggle full scrub
  v  preview: switch between "what it reveals" and [b]everything[/b] the file carries (scrolls; sections in blue)
  F5  reload the file tree (also happens after every run)
  F1 or ?  this help                              q  quit            Tab  jump between tree and options
  Ctrl+P  command palette: "Change theme" - the chosen theme is remembered in ~/.config/metawipe/config.json

[b]Options[/b]
  [b]Full scrub (--full)[/b]  on: also anonymise authors/dates of comments and tracked changes, clean pictures embedded in
      documents, drop customXml/printer settings, remove encoder strings (x264/x265) from videos, set file time to 1980.
      Files where something identifying had to stay are reported INCOMPLETE.
  [b]Output[/b]  copy next to the original (NAME.clean.EXT)  |  copy into an output folder (same folder structure)  |
      overwrite the originals (asks first - cannot be undone).
  [b]Overwrite existing (-f)[/b]  replace output files that already exist (otherwise they are left alone and reported).

[b]Results[/b]
  clean  file -> destination        error  why it failed        skip  not a file metawipe handles
  "still carries: ..."  what the cleaned copy still has (so you can see what a normal run leaves behind)
  warn   something worth knowing (e.g. signatures removed, OLE embeddings left)

[b]Supported[/b]  jpg png apng webp tiff | heic heif avif | mov mp4 m4v 3gp (needs ffmpeg) | pdf | docx xlsx pptx and
variants | odt ods odp and variants.   [b]Never touched[/b]  file names and the text you wrote in a document.
"""


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,f1,question_mark", "close", "Close")]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > Vertical { width: 112; max-width: 96%; height: 90%; border: thick $primary; background: $surface; padding: 1 2; }
    HelpScreen VerticalScroll { height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            with VerticalScroll():
                yield Static(HELP)
            yield Label("[dim]Esc / F1 / ? closes this help[/]")

    def action_close(self) -> None:
        self.dismiss(None)


class TuiReport(cli.Report):
    """cli.Report that reports into the log widget instead of stderr."""

    def __init__(self, emit, failed=lambda path: None):
        super().__init__(verbose=True)
        self.emit = emit
        self.mark_failed = failed

    def fail(self, path, msg: str) -> None:
        self.failed += 1
        self.mark_failed(Path(path))
        self.emit(Text.assemble(("error", "red"), f"  {path}: {msg}"))

    def skip(self, path, why: str) -> None:
        self.skipped += 1
        self.emit(Text(f"skip   {path} ({why})", style="dim"))


class MetawipeApp(App):
    TITLE = "metawipe"
    SUB_TITLE = f"metadata scrubber {__version__}"
    CSS = """
    #left { width: 42%; border: round $primary 35%; padding: 0 1; }
    #right { width: 58%; }
    #preview { height: 45%; border: heavy $primary; background: $surface; padding: 0 1; }
    #opts { height: auto; background: $panel; padding: 1 2 0 2; margin: 1 0; }
    .row { height: 3; align: left middle; }
    .row Label { width: 12; height: 3; content-align: left middle; }
    .row .after { width: auto; margin-right: 3; }
    .row Switch { margin-right: 1; }
    #folder-row { display: none; }
    #run { width: 1fr; }
    #log { height: 1fr; border: round $primary 35%; padding: 0 1; }
    Select { width: 1fr; }
    """
    BINDINGS = [
        Binding("m", "toggle_mark", "Mark"),
        Binding("a", "mark_folder", "Mark all in folder"),
        Binding("c", "clear_marks", "Clear marks"),
        Binding("r", "run", "Run"),
        Binding("f", "toggle_full", "Full scrub"),
        Binding("v", "toggle_view", "Everything"),
        Binding("f5", "reload_tree", "Reload"),
        Binding("f1,question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, start: Path | None = None):
        super().__init__()
        # resolved like the tree resolves the folders it opens (macOS: /tmp -> /private/tmp), so paths stay comparable
        self.start = Path(start or Path.cwd()).expanduser().resolve()
        self.marks: set[Path] = set()
        self.highlighted: Path | None = None
        self.view_all = False  # preview: what the file reveals (summary) or every metadata entry it carries
        self.states: dict[Path, str] = {}  # outcome of the last run per file: done / incomplete / failed

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            with Vertical(id="left"):
                yield MarkTree(self.start, self.marks, self.states, id="tree")
            with Vertical(id="right"):
                yield DataTable(id="preview", cursor_type="none", show_header=False)
                with Vertical(id="opts"):
                    with Horizontal(classes="row"):
                        yield Label("Output")
                        yield Select(MODES, value="copy", allow_blank=False, id="mode")
                    with Horizontal(classes="row", id="folder-row"):
                        yield Label("Folder")
                        yield Input(placeholder="where the cleaned copies go", id="outdir")
                    with Horizontal(classes="row"):
                        yield Switch(value=True, id="full")
                        yield Label("Full scrub", classes="after")
                        yield Switch(value=False, id="force")
                        yield Label("Replace existing", classes="after")
                    with Horizontal(classes="row"):
                        yield Button("Clean (r)", id="run", variant="success")
                yield RichLog(id="log", markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(THEME)
        self._saved_theme = load_config().get("theme", DEFAULT_THEME)
        known = isinstance(self._saved_theme, str) and self._saved_theme in self.available_themes
        self.theme = self._saved_theme if known else DEFAULT_THEME
        self.theme_changed_signal.subscribe(self, self._remember_theme)
        self.query_one("#left").border_title = Text(str(self.start))
        table = self.query_one("#preview", DataTable)
        table.add_column("found", width=26)
        table.add_column("value")
        table.border_title = "What a file reveals"
        self.query_one("#log").border_title = "Results"
        self.query_one("#tree").focus()
        self._sync_run_button()
        self.log_line("Mark files with [b]space[/] or [b]m[/] and press [b]r[/]. Folders are cleaned recursively. [b]F1[/] explains everything here.")

    # ------------------------------------------------------------------ helpers
    def log_line(self, text: str | Text) -> None:
        """str: our own words with markup; anything from the disk (paths, metadata, errors) goes in as Text, never parsed
        (rich.markup.escape is not enough: a Windows path like C:\\x\\[1] loses its backslash)."""
        self.query_one("#log", RichLog).write(text)

    def _short(self, path: Path) -> str:
        try:
            return str(Path(path).resolve().relative_to(self.start.resolve()))
        except ValueError:
            return str(path)

    def _refresh_tree(self) -> None:
        tree = self.query_one("#tree", MarkTree)
        invalidate = getattr(tree, "_invalidate", None)  # drop the cached lines so the check boxes repaint
        if invalidate is not None:
            invalidate()
        tree.refresh()

    def action_reload_tree(self) -> None:
        """Re-read the folders from disk: new copies, output folders and outside changes show up (open folders stay open)."""
        self.query_one("#tree", MarkTree).reload()

    def _remember_theme(self, theme: Theme) -> None:
        if theme.name == self._saved_theme:
            return
        try:
            save_config(theme=theme.name)
        except OSError as e:
            self.notify(f"Could not save the theme to {config_path()}: {e}", severity="warning", markup=False)
            return
        self._saved_theme = theme.name

    # ------------------------------------------------------------------ marking
    def _marks_changed(self) -> None:
        self._refresh_tree()
        self.sub_title = f"{len(self.marks)} marked"
        self._sync_run_button()

    def _sync_run_button(self) -> None:
        """The button says what will happen; overwriting originals turns it red."""
        inplace = self.query_one("#mode", Select).value == "inplace"
        button = self.query_one("#run", Button)
        n = len(self.marks)
        button.label = f"{'Overwrite' if inplace else 'Clean'}{f' {n} marked' if n else ''} (r)"
        button.variant = "error" if inplace else "success"

    @on(Select.Changed, "#mode")
    def mode_changed(self) -> None:
        self.query_one("#folder-row").display = self.query_one("#mode", Select).value == "folder"
        self._sync_run_button()

    def _current_path(self) -> Path | None:
        node = self.query_one("#tree", MarkTree).cursor_node
        return node.data.path if node is not None and node.data is not None else None

    def action_toggle_mark(self) -> None:
        path = self._current_path()
        if path is None:
            return
        if not path.is_dir() and cli.HANDLERS.get(path.suffix.lower()) is None:
            self.notify(f"metawipe cannot clean {path.suffix or 'this'} files.", severity="warning", markup=False)
            return
        self.marks.symmetric_difference_update({path})
        self._marks_changed()

    def action_mark_folder(self) -> None:
        path = self._current_path()
        folder = path if path is not None and path.is_dir() else (path.parent if path else self.start)
        found = [p for p in sorted(folder.iterdir()) if p.is_file() and cli.HANDLERS.get(p.suffix.lower())]
        self.marks.update(found)
        self._marks_changed()
        self.log_line(Text(f"marked {len(found)} supported file(s) in {folder}"))

    def action_clear_marks(self) -> None:
        self.marks.clear()
        self._marks_changed()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_toggle_view(self) -> None:
        self.view_all = not self.view_all
        if self.highlighted is not None:
            self.preview(self.highlighted)

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
        everything = self.view_all
        if path.is_dir():
            # direct children only: a recursive walk per highlight piles up on big trees (the run dialog counts them all)
            rows = [("Folder", f"{sum(1 for p in path.iterdir() if p.is_file() and cli.HANDLERS.get(p.suffix.lower()))} supported file(s) directly inside")]
            everything = False
        elif everything:
            from . import dump
            rows = dump.dump_file(path)
        else:
            rows = peek.inspect_file(path) or [("Nothing identifying found", "")]
        self.call_from_thread(self._show_preview, path, rows, everything)

    def _show_preview(self, path: Path, rows, everything: bool = False) -> None:
        if path != self.highlighted or (everything != self.view_all and not path.is_dir()):
            return  # a slow preview finished after the cursor moved on or v switched the view
        table = self.query_one("#preview", DataTable)
        table.clear()
        table.border_title = Text(f"{path.name} - everything it carries" if everything else path.name)
        hint = "v: summary" if everything else "v: everything"
        if path.is_dir():
            table.add_row(Text("▸ Folder", style="bold"), rows[0][1])
            table.border_subtitle = "space marks it with everything inside"
            return
        if everything:
            section = None
            for sec, name, value in rows:
                if sec != section:
                    section = sec
                    table.add_row(Text(sec, style=f"bold {STEEL}"), "")
                table.add_row(Text.assemble(("● ", _SEVERITY_STYLE[severity(name)]), name), Text(value))
            sensitive = sum(1 for _, name, _ in rows if severity(name) == 0)
            table.border_subtitle = f"{len(rows)} entries, {sensitive} sensitive   {hint}"
            table.scroll_home(animate=False)
            return
        if rows and rows[0][0] == "unsupported":
            table.add_row(Text("– Not supported", style=MUTED), "metawipe cannot clean this file type")
            table.border_subtitle = ""
            return
        if not rows or rows[0][0] == "Nothing identifying found":
            table.add_row(Text("✓ Nothing found", style=f"bold {SEA}"), "no identifying metadata in this file")
            table.border_subtitle = hint
            return
        ranked = sorted(rows, key=lambda r: severity(r[0]))
        for key, value in ranked:
            table.add_row(Text.assemble(("● ", _SEVERITY_STYLE[severity(key)]), key), Text(value))
        sensitive = sum(1 for k, _ in rows if severity(k) == 0)
        table.border_subtitle = (f"{sensitive} sensitive, {len(rows) - sensitive} other" if sensitive else f"{len(rows)} found") + f"   {hint}"

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
            output_dir=Path(os.path.expanduser(outdir)) if mode == "folder" else None,
            force=self.query_one("#force", Switch).value, password=None,
            full=self.query_one("#full", Switch).value, verbose=True,
        )

    def action_run(self) -> None:
        if any(w.group == "run" and not w.is_finished for w in self.workers):
            self.notify("Still cleaning: wait for the current run to finish.", severity="warning")
            return
        if not self.marks:
            self.notify("Nothing marked: press space on files or folders.", severity="warning")
            return
        args = self._args()
        if args is None:
            return
        if args.in_place:
            self.confirm_in_place(args)
        else:
            self.start_run(args)

    @work(thread=True, group="run")  # planning a big tree takes seconds; in group "run" so r cannot start a second one
    def confirm_in_place(self, args: argparse.Namespace) -> None:
        n = len(cli.plan(args, TuiReport(lambda text: None)))  # count what will really be overwritten, hidden files included
        self.call_from_thread(lambda: self.push_screen(Confirm(f"Overwrite {n} file(s) in place?\nThe originals cannot be recovered."),
                              lambda yes: self.start_run(args) if yes else self.log_line("[yellow]cancelled[/]")))

    @work(thread=True, exclusive=True, group="run")
    def start_run(self, args: argparse.Namespace) -> None:
        emit = lambda text: self.call_from_thread(self.log_line, text)  # noqa: E731
        report = TuiReport(emit, lambda path: self.call_from_thread(self.states.__setitem__, path, "failed"))
        emit(f"[b]cleaning[/] {len(args.paths)} item(s){' (full scrub)' if args.full else ''} ...")
        jobs = cli.plan(args, report)
        incomplete = 0
        for job in jobs:
            try:
                ctx = cli.execute(job, args, warn=lambda msg, j=job: emit(Text.assemble(("warn", "yellow"), f"   {j.src.name}: {msg}")))
            except WipeError as e:
                report.fail(job.src, str(e))
                continue
            except Exception as e:  # noqa: BLE001 - one bad file must not stop the batch
                report.fail(job.src, f"{type(e).__name__}: {e}")
                continue
            report.cleaned += 1
            left = peek.inspect_file(job.dst)
            note = ""
            self.call_from_thread(self.states.__setitem__, job.src, "done")
            if args.full and ctx.residue:
                incomplete += 1
                self.call_from_thread(self.states.__setitem__, job.src, "incomplete")
                note = f" (INCOMPLETE: {len(ctx.residue)} item(s) left)"
            emit(Text.assemble(("clean", "green"), f"  {self._short(job.src)} -> {'in place' if job.inplace else self._short(job.dst)}",
                               (note, "yellow")))
            if left:
                emit(Text("        still carries: " + ", ".join(k for k, _ in left[:6]) + (" ..." if len(left) > 6 else "")))
        emit(f"[b]done:[/] {report.cleaned} cleaned, {report.failed} failed, {report.skipped} skipped" + (f", {incomplete} incomplete" if args.full else ""))
        self.call_from_thread(self._run_finished, args)
        if self.highlighted is not None:
            self.call_from_thread(self.preview, self.highlighted)


    def _run_finished(self, args: argparse.Namespace) -> None:
        self.marks.difference_update(map(Path, args.paths))  # the outcome now shows in the tree; marks made during the run stay
        self._marks_changed()
        self.action_reload_tree()  # the run made new files/folders (NAME.clean.EXT, the output folder)


def run(start: str | os.PathLike | None = None) -> int:
    MetawipeApp(Path(start) if start else None).run()
    return 0
