from __future__ import annotations

import asyncio
import tomllib

import shlex
import subprocess
from pathlib import Path
import functools
from typing import Any, Callable, Optional
from rich.text import Text

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import CommandPalette
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    OptionList,
    ProgressBar,
    Static,
)
from textual.widgets.option_list import Option

from ow import __version__
from ow.utils import index, paths
from ow.utils.config import (
    Config,
    WorkspaceConfig,
    load_global_config,
    load_workspace_config,
    write_global_config,
    write_workspace_config,
)
from ow.utils.display import display_path
from ow.utils.legacy import check_legacy_layout
from ow.utils.status import WorkspaceStatus, gather_workspace_status

from ow.tui.runner import TuiSink
from ow.tui.widgets import ConfirmDialog, OperationLog, WorkspaceDetail

MARKER = Path(".ow") / "config.toml"


# ---------------------------------------------------------------------------
# Workspace list entry
# ---------------------------------------------------------------------------


class WorkspaceEntry:
    """One row of the workspace list: path + whether it is archived + cached config."""

    __slots__ = ("path", "archived", "ws", "error")

    def __init__(
        self,
        path: Path,
        archived: bool,
        ws: WorkspaceConfig | None,
        error: str | None,
    ) -> None:
        self.path = path
        self.archived = archived
        self.ws = ws
        self.error = error

    @property
    def name(self) -> str:
        return self.path.name


def _load_ws_config(ws_dir: Path) -> tuple[WorkspaceConfig | None, str | None]:
    try:
        return load_workspace_config(ws_dir / MARKER), None
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        return None, str(exc)


# ---------------------------------------------------------------------------
# Help screen
# ---------------------------------------------------------------------------


class HelpScreen(ModalScreen[None]):
    """Compact key-binding reference."""

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    HelpScreen > Vertical {
        width: 60;
        height: auto;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    HelpScreen > Vertical > Horizontal {
        align: center middle;
        height: auto;
        margin-top: 1;
    }
    HelpScreen Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss(None)", "", show=False),
    ]

    def compose(self) -> ComposeResult:
        from textual.widgets import Button

        bindings_text = Text.from_markup(
            "[bold]Navigation[/]\n"
            "  j/k or up/down   Move in list\n"
            "  tab              Cycle focus\n"
            "  enter            Focus detail\n"
            "\n"
            "[bold]Operations[/]\n"
            "  s            Status (local)\n"
            "  f            Fetch + status\n"
            "  a            Apply\n"
            "  R            Rebase\n"
            "  P            Pull\n"
            "  S            Switch\n"
            "  r            Reset\n"
            "  p            Prune\n"
            "\n"
            "[bold]Workspace management[/]\n"
            "  n            New workspace\n"
            "  e            Edit workspace config\n"
            "  E            Edit global config\n"
            "  o            Open in editor\n"
            "  m            Move\n"
            "  A            Archive / unarchive\n"
            "  x            Remove\n"
            "\n"
            "[bold]Other[/]\n"
            "  ctrl+r       Reload list\n"
            "  ctrl+l       Clear log\n"
            "  ctrl+c       Cancel operation (or quit if idle)\n"
            "  ?            This help\n"
            "  q            Quit\n"
            "\n"
            "[dim]Esc — Close[/]"
        )
        with Vertical():
            yield Static(bindings_text)
            yield Horizontal(
                Button("Close", id="btn_close", variant="primary"),
            )

    def on_button_pressed(self, event) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key in ("escape", "question_mark"):
            self.dismiss(None)


# ---------------------------------------------------------------------------
# Main screen
# ---------------------------------------------------------------------------


class MainScreen(Screen):
    """Two-pane dashboard with a persistent log at the bottom."""

    BINDINGS = [
        Binding("tab", "focus_next_pane", "Next pane", show=False),
        Binding("shift+tab", "focus_previous_pane", "Prev pane", show=False),
        Binding("enter", "focus_detail", "Detail", show=False),
        Binding("ctrl+r", "reload_workspaces", "Reload", show=False),
        Binding("ctrl+l", "clear_log", "Clear log", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("q", "request_quit", "Quit", show=False),
        Binding("s", "status_local", "Status", show=True),
        Binding("f", "fetch_status", "Fetch+status", show=True),
        Binding("a", "apply", "Apply", show=True),
        Binding("R", "rebase", "Rebase", show=True),
        Binding("P", "pull", "Pull", show=True),
        Binding("S", "switch", "Switch", show=True),
        Binding("r", "reset", "Reset", show=True),
        Binding("n", "new_workspace", "New", show=True),
        Binding("e", "edit_config", "Edit", show=True),
        Binding("E", "edit_global_config", "Global", show=True),
        Binding("o", "open_editor", "Open", show=True),
        Binding("m", "move", "Move", show=True),
        Binding("A", "archive_toggle", "Archive", show=True),
        Binding("x", "remove", "Remove", show=True),
        Binding("p", "prune", "Prune", show=True),
        Binding("question_mark", "help", "Help", show=True),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._config = config
        self._entries: list[WorkspaceEntry] = []
        self._status_cache: dict[Path, WorkspaceStatus] = {}
        self._busy = False
        self._debounce_timer: Any = None
        self.run_operation_worker: Any = None

    # ---- layout ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static("", id="size_warning")
        yield Header(show_clock=False)
        with Horizontal(id="panes"):
            yield OptionList(id="ws_list")
            yield WorkspaceDetail(id="detail")
            yield Static(
                "[dim]No workspaces found[/]\n\n"
                "Press [bold]n[/] to create one, [bold]Ctrl+R[/] to reload, or [bold]?[/] for help.",
                id="empty_state",
            )
        with Vertical(id="bottom"):
            with Horizontal(id="progress"):
                yield Static("", id="task_label")
                yield ProgressBar(id="task_bar")
            yield OperationLog(id="log")
        yield Footer()

    CSS = """
    /* --- Layout --- */
    #panes {
        height: 1fr;
    }
    #ws_list {
        width: 34;
        border: solid $surface;
    }
    #ws_list:focus-within {
        border: solid $accent;
    }
    #detail {
        width: 1fr;
        border: solid $surface;
        padding: 1 2;
    }
    #detail:focus-within {
        border: solid $accent;
    }
    #bottom {
        height: 12;
        dock: bottom;
    }
    #progress {
        height: 1;
        display: none;
    }
    #progress.-active {
        display: block;
    }
    #task_label {
        width: auto;
        max-width: 20;
        color: $text-muted;
    }
    #task_bar {
        width: 1fr;
    }
    #log {
        height: 1fr;
        border: solid $surface;
        padding: 0 1;
    }
    #log:focus-within {
        border: solid $accent;
    }

    /* --- Empty state --- */
    #empty_state {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        text-align: center;
        color: $text-muted;
        display: none;
    }
    #empty_state.-visible {
        display: block;
    }

    /* --- Minimum size warning --- */
    #size_warning {
        dock: top;
        width: 100%;
        height: auto;
        max-height: 3;
        background: $warning;
        color: $text;
        text-align: center;
        display: none;
    }
    #size_warning.-visible {
        display: block;
    }
    """

    # ---- startup --------------------------------------------------------

    def on_mount(self) -> None:
        self.reload_workspaces(initial=True, version=__version__)
        self.run_operation(
            "legacy-layout check",
            lambda: check_legacy_layout(fatal=False),
            quiet=True,
        )

    def _show_progress_row(self) -> None:
        try:
            self.query_one("#progress").add_class("-active")
        except Exception as e:
            import sys
            print(f"Warning: _show_progress_row failed: {e}", file=sys.stderr)

    def _hide_progress_row(self) -> None:
        try:
            self.query_one("#progress").remove_class("-active")
        except Exception as e:
            import sys
            print(f"Warning: _hide_progress_row failed: {e}", file=sys.stderr)

    def on_resize(self, event: Any) -> None:
        min_w, min_h = 80, 24
        warning = self.query_one("#size_warning")
        if event.size.width < min_w or event.size.height < min_h:
            warning.update(
                f"Terminal too small ({event.size.width}x{event.size.height}). "
                f"Minimum: {min_w}x{min_h}"
            )
            warning.add_class("-visible")
        else:
            warning.remove_class("-visible")

    # ---- workspace list ------------------------------------------------

    def reload_workspaces(
        self, initial: bool = False, version: str = "dev"
    ) -> None:
        """Re-read the index + archives and repopulate the list."""
        from ow.commands.archive import archived_workspaces

        active = sorted(index.known_workspaces(), key=lambda p: (p.name, str(p)))
        archived = archived_workspaces()

        entries: list[WorkspaceEntry] = []
        for p in active:
            ws, err = _load_ws_config(p)
            entries.append(WorkspaceEntry(p, archived=False, ws=ws, error=err))
        if archived:
            for p in archived:
                ws, err = _load_ws_config(p)
                entries.append(
                    WorkspaceEntry(p, archived=True, ws=ws, error=err)
                )
        self._entries = entries

        option_list = self.query_one("#ws_list", OptionList)
        prev_idx = option_list.highlighted
        option_list.clear_options()
        for i, entry in enumerate(entries):
            text = display_path(entry.path)
            if entry.error:
                prompt = (
                    f"[bold]{entry.name}[/]  [dim]{text}[/]  [red](error)[/]"
                )
            elif entry.archived:
                prompt = f"[dim]{entry.name}  {text}[/]"
            else:
                prompt = f"[bold]{entry.name}[/]  [dim]{text}[/]"
            option_list.add_option(Option(prompt, id=f"ws_{i}"))
            if i == len(active) - 1 and archived:
                option_list.add_option(None)
        # Restore previous selection or default to first item
        if entries:
            option_list.highlighted = prev_idx if prev_idx is not None and prev_idx < len(entries) else 0

        self.app.sub_title = (
            f"{len(active)} workspace(s), {len(archived)} archived"
        )
        if initial:
            log = self.query_one("#log", OperationLog)
            log.write(Text.from_markup(f"[bold]ow[/] {version} — {len(active)} workspace(s)"))

        empty = self.query_one("#empty_state")
        if not entries:
            empty.add_class("-visible")
            detail = self.query_one("#detail", WorkspaceDetail)
            detail.remove_children()
        else:
            empty.remove_class("-visible")
            self._render_detail_for(entries[0])

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted,
    ) -> None:
        idx = event.option_index
        if idx < 0 or idx >= len(self._entries):
            return
        self._render_detail_for(self._entries[idx])
        self._arm_status_debounce()

    def _selected_entry(self) -> Optional[WorkspaceEntry]:
        """The workspace currently highlighted in the list, or None."""
        option_list = self.query_one("#ws_list", OptionList)
        idx = option_list.highlighted
        if idx is None:
            return None
        if idx < 0 or idx >= len(self._entries):
            return None
        return self._entries[idx]

    def _render_detail_for(self, entry: WorkspaceEntry) -> None:
        detail = self.query_one("#detail", WorkspaceDetail)
        if entry.error:
            detail.remove_children()
            error_text = Text(
                f"Error loading {entry.path.name}:\n{entry.error}",
                style="red",
            )
            detail.mount(Static(error_text))
        elif entry.ws is None:
            detail.remove_children()
            empty_text = Text(f"{entry.path.name}\n(no config)", style="dim")
            detail.mount(Static(empty_text))
        else:
            cached = self._status_cache.get(entry.path)
            if cached is not None:
                detail.show_status(cached, entry.path, entry.ws)
            else:
                detail.show_config_only(entry.path, entry.ws)

    def _arm_status_debounce(self) -> None:
        """Schedule a local status refresh after 250 ms, cancelling any pending one."""
        if self._debounce_timer is not None:
            self._debounce_timer.stop()
            self._debounce_timer = None
        entry = self._selected_entry()
        if entry is None or entry.archived or entry.ws is None:
            return
        self._debounce_timer = self.set_timer(
            0.25, lambda: self._debounced_refresh(entry)
        )

    def _debounced_refresh(self, entry: WorkspaceEntry) -> None:
        self._debounce_timer = None
        current = self._selected_entry()
        if current is None or current.path != entry.path:
            return
        self.refresh_status(entry, fetch=False)

    # ---- the runner (§3.5) ---------------------------------------------

    def run_operation(
        self,
        label: str,
        fn: Callable[[], Any],
        *,
        then: Callable[[Any], None] | None = None,
        quiet: bool = False,
        reload: bool = False,
        invalidate: Path | None = None,
    ) -> None:
        """Gate on _busy, then launch the worker."""
        if self._busy:
            if not quiet:
                self.notify("An operation is already running", severity="warning")
            return
        self._busy = True
        # Capture DOM reference on the main thread — the worker cannot query_one.
        try:
            log = self.query_one("#log", OperationLog)
        except Exception:
            log = None
        self.run_operation_worker = self._run_worker(
            label, fn, then=then, quiet=quiet,
            reload=reload, invalidate=invalidate,
            log=log,
        )

    @work(thread=True, group="op", exit_on_error=False)
    def _run_worker(
        self,
        label: str,
        fn: Callable[[], Any],
        *,
        then: Callable[[Any], None] | None = None,
        quiet: bool = False,
        reload: bool = False,
        invalidate: Path | None = None,
        log: OperationLog | None = None,
    ) -> None:
        from ow.utils.display import redirect_output
        import typer

        if not quiet:
            self.app.call_from_thread(self._log_header, label)

        result: Any = None
        exit_code: int | None = None
        try:
            sink = TuiSink(self)
            with redirect_output(sink):
                result = fn()
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
        except typer.BadParameter as exc:
            exit_code = 2
            if not quiet and log is not None:
                self.app.call_from_thread(
                    log.write, Text(str(exc), style="red")
                )
        except Exception as exc:
            exit_code = 1
            if not quiet and log is not None:
                self.app.call_from_thread(log.write, Text(repr(exc), style="red"))
        finally:
            self.app.call_from_thread(
                self._finish, label, exit_code, quiet, result, then,
                reload, invalidate,
            )

    def _finish(
        self,
        label: str,
        exit_code: int | None,
        quiet: bool,
        result: Any,
        then: Callable[[Any], None] | None,
        reload: bool,
        invalidate: Path | None,
    ) -> None:
        try:
            log = self.query_one("#log", OperationLog)
            if not quiet:
                # exit_code is None on a plain return; an explicit 0 means
                # SystemExit(0), also a success — only nonzero is a failure.
                if exit_code is not None and exit_code != 0:
                    log.write(f"{label}: failed (exit {exit_code})")
                else:
                    log.write(f"{label}: done")
        except Exception as e:
            import sys
            print(f"Warning: _finish log write failed: {e}", file=sys.stderr)
        try:
            if invalidate is not None:
                self._status_cache.pop(invalidate, None)
            # SystemExit(0) is a success like a plain return: run the
            # callback for either, not only when nothing was raised at all.
            if exit_code in (None, 0) and then is not None:
                then(result)
            if reload:
                self.reload_workspaces()
        finally:
            self._busy = False
            self._hide_progress_row()
            self.run_operation_worker = None

    def _log_header(self, label: str) -> None:
        try:
            log = self.query_one("#log", OperationLog)
            log.write(Text.from_markup(f"── [bold]{label}[/] ──"))
            self.query_one("#task_label", Static).update(label)
            self._show_progress_row()
        except Exception as e:
            import sys
            print(f"Warning: _log_header failed: {e}", file=sys.stderr)

    # ---- focus navigation ----------------------------------------------

    def action_focus_next_pane(self) -> None:
        focus_order = ["#ws_list", "#detail", "#log"]
        current = self.app.focused
        if current is None:
            self.query_one("#ws_list", OptionList).focus()
            return
        current_id = current.id
        try:
            cur_idx = focus_order.index(f"#{current_id}" if current_id else "")
        except ValueError:
            cur_idx = -1
        nxt = focus_order[(cur_idx + 1) % len(focus_order)]
        self.query_one(nxt).focus()

    def action_focus_previous_pane(self) -> None:
        focus_order = ["#ws_list", "#detail", "#log"]
        current = self.app.focused
        if current is None:
            self.query_one("#log").focus()
            return
        current_id = current.id
        try:
            cur_idx = focus_order.index(f"#{current_id}" if current_id else "")
        except ValueError:
            cur_idx = 0
        prev = focus_order[(cur_idx - 1) % len(focus_order)]
        self.query_one(prev).focus()

    def action_focus_detail(self) -> None:
        self.query_one("#detail", WorkspaceDetail).focus()

    def action_reload_workspaces(self) -> None:
        self.reload_workspaces()

    def action_clear_log(self) -> None:
        self.query_one("#log", OperationLog).clear()

    def action_request_quit(self) -> None:
        if self._busy:
            self.notify("Operation in progress", severity="warning")
            return
        self.app.exit()

    def action_cursor_down(self) -> None:
        ol = self.query_one("#ws_list", OptionList)
        if ol.has_focus:
            ol.action_cursor_down()

    def action_cursor_up(self) -> None:
        ol = self.query_one("#ws_list", OptionList)
        if ol.has_focus:
            ol.action_cursor_up()

    # ---- status (§4.1) -------------------------------------------------

    def refresh_status(self, entry: WorkspaceEntry, *, fetch: bool) -> None:
        self.run_operation(
            f"status {entry.name}",
            lambda: gather_workspace_status(
                entry.ws, entry.path, self._config, fetch=fetch
            ),
            then=self._show_status,
            quiet=not fetch,
            invalidate=entry.path,
        )

    def _show_status(self, status: WorkspaceStatus) -> None:
        self._status_cache[status.ws_dir] = status
        entry = self._selected_entry()
        if entry is None or entry.path != status.ws_dir:
            return
        if entry.ws is None:
            return
        detail = self.query_one("#detail", WorkspaceDetail)
        detail.show_status(status, entry.path, entry.ws)

    def action_status_local(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.notify("No workspace selected", severity="warning")
            return
        if entry.archived:
            self.notify("Cannot show status for archived workspace", severity="warning")
            return
        if entry.ws is None:
            self.notify("Workspace config not loaded", severity="warning")
            return
        self.refresh_status(entry, fetch=False)

    def action_fetch_status(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.notify("No workspace selected", severity="warning")
            return
        if entry.archived:
            self.notify("Cannot show status for archived workspace", severity="warning")
            return
        if entry.ws is None:
            self.notify("Workspace config not loaded", severity="warning")
            return
        self.refresh_status(entry, fetch=True)

    # ---- apply (§4.2) --------------------------------------------------

    def action_apply(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.notify("No workspace selected", severity="warning")
            return
        if entry.archived:
            self.notify("Cannot apply archived workspace", severity="warning")
            return
        from ow.commands.apply import cmd_apply
        self.run_operation(
            f"apply {entry.name}",
            lambda: cmd_apply(self._config, workspace=str(entry.path)),
            invalidate=entry.path,
        )

    # ---- rebase (§4.3) -------------------------------------------------

    def action_rebase(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.notify("No workspace selected", severity="warning")
            return
        if entry.archived:
            self.notify("Cannot rebase archived workspace", severity="warning")
            return
        from ow.commands.rebase import cmd_rebase
        self.run_operation(
            f"rebase {entry.name} (plan)",
            lambda: cmd_rebase(
                self._config, workspace=str(entry.path), dry_run=True
            ),
            then=lambda _r: self._push_rebase_confirm(entry),
        )

    def _push_rebase_confirm(self, entry: WorkspaceEntry) -> None:
        from ow.commands.rebase import cmd_rebase
        self.app.push_screen(
            ConfirmDialog(
                "Rebase now?",
                details=Text("Refs were just fetched; the rebase reuses them."),
            ),
            callback=lambda ok: ok and self.run_operation(
                f"rebase {entry.name}",
                lambda: cmd_rebase(
                    self._config, workspace=str(entry.path),
                    yes=True, no_fetch=True,
                ),
                invalidate=entry.path,
            ),
        )

    # ---- pull (§4.3b) --------------------------------------------------

    def action_pull(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.notify("No workspace selected", severity="warning")
            return
        if entry.archived:
            self.notify("Cannot pull archived workspace", severity="warning")
            return
        if entry.ws is None:
            self.notify("Workspace config not loaded", severity="warning")
            return
        from ow.commands.pull import cmd_pull
        self.run_operation(
            f"pull {entry.name}",
            lambda: cmd_pull(self._config, workspace=str(entry.path)),
            invalidate=entry.path,
        )

    # ---- switch (§4.3d) -------------------------------------------------

    def action_switch(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.notify("No workspace selected", severity="warning")
            return
        if entry.archived:
            self.notify("Cannot switch archived workspace", severity="warning")
            return
        if entry.ws is None:
            self.notify("Workspace config not loaded", severity="warning")
            return
        from ow.tui.workspace_forms import SwitchScreen
        self.app.push_screen(
            SwitchScreen(),
            callback=lambda req: req and self._do_switch(entry, req),
        )

    def _do_switch(self, entry: WorkspaceEntry, req: Any) -> None:
        from ow.commands import cmd_switch

        def _then(_result: Any) -> None:
            self.notify(f"Switched {entry.name}", severity="information")

        self.run_operation(
            f"switch {entry.name}",
            lambda: cmd_switch(
                self._config,
                target=req.target,
                workspace=str(entry.path),
                create=req.create,
                detach=req.detach,
                only=None,
                dry_run=False,
            ),
            then=_then,
            invalidate=entry.path,
        )

    # ---- reset (§4.3c) -------------------------------------------------

    def action_reset(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.notify("No workspace selected", severity="warning")
            return
        if entry.archived:
            self.notify("Cannot reset archived workspace", severity="warning")
            return
        if entry.ws is None:
            self.notify("Workspace config not loaded", severity="warning")
            return
        from ow.commands.reset import cmd_reset
        self.run_operation(
            f"reset {entry.name} (plan)",
            lambda: cmd_reset(
                self._config, workspace=str(entry.path), dry_run=True,
            ),
            then=lambda _r: self._push_reset_confirm(entry),
        )

    def _push_reset_confirm(self, entry: WorkspaceEntry) -> None:
        from ow.commands.reset import cmd_reset
        self.app.push_screen(
            ConfirmDialog(
                "Reset now?",
                details=Text("Resets every repo to the ref it tracks. Uncommitted changes are kept unless --hard was used."),
            ),
            callback=lambda ok: ok and self.run_operation(
                f"reset {entry.name}",
                lambda: cmd_reset(
                    self._config, workspace=str(entry.path), yes=True,
                ),
                invalidate=entry.path,
            ),
        )

    # ---- prune (§4.4) --------------------------------------------------

    def action_prune(self) -> None:
        from ow.commands.prune import cmd_prune
        self.run_operation(
            "prune (plan)",
            lambda: cmd_prune(dry_run=True),
            then=lambda _r: self._push_prune_confirm(),
        )

    def _push_prune_confirm(self) -> None:
        from ow.commands.prune import cmd_prune
        self.app.push_screen(
            ConfirmDialog(
                "Prune now?",
                details=Text("Surveys dead index entries, stale worktree references, and orphaned branches in bare repos."),
            ),
            callback=lambda ok: ok and self.run_operation(
                "prune",
                lambda: cmd_prune(yes=True),
                reload=True,
            ),
        )

    # ---- archive / unarchive (§4.5) ------------------------------------

    def action_archive_toggle(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.notify("No workspace selected", severity="warning")
            return
        if entry.ws is None:
            self.notify("Workspace config not loaded", severity="warning")
            return
        if entry.archived:
            self._unarchive(entry)
        else:
            self._archive(entry)

    def _archive(self, entry: WorkspaceEntry) -> None:
        from ow.utils.relocate import validate_target

        target = paths.archives_dir() / entry.path.name
        reason = validate_target(entry.path, target)
        if reason is not None:
            self.notify(reason, severity="error")
            return

        details = Text.from_markup(
            f"[bold]Archive workspace[/]\n"
            f"  from  {display_path(entry.path)}\n"
            f"  to    {display_path(target)}\n\n"
            f"Worktrees and branches are kept."
        )
        self.app.push_screen(
            ConfirmDialog("Archive workspace?", details=details),
            callback=lambda ok: ok and self._do_archive(entry, target),
        )

    def _do_archive(self, entry: WorkspaceEntry, target: Path) -> None:
        from ow.commands.archive import execute_archive

        def _archive_fn() -> list[str]:
            paths.archives_dir().mkdir(parents=True, exist_ok=True)
            return execute_archive(entry.path, entry.ws, target)

        def _then(unrepaired: list[str]) -> None:
            if unrepaired:
                log = self.query_one("#log", OperationLog)
                log.write(
                    Text.from_markup(f"[yellow]unrepaired aliases: {', '.join(unrepaired)}[/]")
                )
            self.notify(f"Archived {entry.name}", severity="information")

        self.run_operation(
            f"archive {entry.name}", _archive_fn, then=_then, reload=True,
        )

    def _unarchive(self, entry: WorkspaceEntry) -> None:
        from ow.tui.workspace_forms import PromptScreen

        default_dest = str(Path.cwd() / entry.path.name)
        self.app.push_screen(
            PromptScreen("Restore to", default=default_dest),
            callback=lambda dest: self._unarchive_got_dest(entry, dest),
        )

    def _unarchive_got_dest(self, entry: WorkspaceEntry, dest: str | None) -> None:
        if dest is None:
            return
        from ow.utils.relocate import validate_target
        from ow.commands.mv import resolve_dest

        target = resolve_dest(entry.path, dest)
        reason = validate_target(entry.path, target)
        if reason is not None:
            self.notify(reason, severity="error")
            return

        details = Text.from_markup(
            f"[bold]Restore workspace[/]\n"
            f"  from  {display_path(entry.path)}\n"
            f"  to    {display_path(target)}\n\n"
            f"Will re-render templates."
        )
        self.app.push_screen(
            ConfirmDialog("Restore?", details=details),
            callback=lambda ok: ok and self._do_unarchive(entry, target),
        )

    def _do_unarchive(self, entry: WorkspaceEntry, target: Path) -> None:
        from ow.commands.archive import execute_unarchive

        def _then(unrepaired: list[str]) -> None:
            if unrepaired:
                log = self.query_one("#log", OperationLog)
                log.write(
                    Text.from_markup(f"[yellow]unrepaired aliases: {', '.join(unrepaired)}[/]")
                )
            self.notify(f"Restored {entry.name}", severity="information")

        self.run_operation(
            f"unarchive {entry.name}",
            lambda: execute_unarchive(self._config, entry.path, entry.ws, target),
            then=_then,
            reload=True,
        )

    # ---- move (§4.6) ---------------------------------------------------

    def action_move(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.notify("No workspace selected", severity="warning")
            return
        if entry.archived:
            self.notify("Cannot move archived workspace", severity="warning")
            return
        if entry.ws is None:
            self.notify("Workspace config not loaded", severity="warning")
            return
        from ow.tui.workspace_forms import PromptScreen
        self.app.push_screen(
            PromptScreen("Move to", default=str(entry.path)),
            callback=lambda dest: self._move_got_dest(entry, dest),
        )

    def _move_got_dest(self, entry: WorkspaceEntry, dest: str | None) -> None:
        if dest is None:
            return
        from ow.commands.mv import resolve_dest
        from ow.utils.relocate import validate_target

        target = resolve_dest(entry.path, dest)
        reason = validate_target(entry.path, target)
        if reason is not None:
            self.notify(reason, severity="error")
            return

        parts = [
            f"[bold]Move workspace[/]",
            f"  from  {display_path(entry.path)}",
            f"  to    {display_path(target)}",
        ]
        if target.name != entry.path.name:
            parts.append("")
            parts.append(
                f"  ⚠ renaming changes db_name and dbfilter in odoorc "
                f"to '{target.name}' — the existing Odoo database is not renamed"
            )
        if (entry.path / ".venv").exists():
            parts.append("")
            parts.append(
                "  ⚠ .venv holds absolute paths — run `mise install` in the new location"
            )

        details = Text.from_markup("\n".join(parts))
        self.app.push_screen(
            ConfirmDialog("Move workspace?", details=details),
            callback=lambda ok: ok and self._do_move(entry, target),
        )

    def _do_move(self, entry: WorkspaceEntry, target: Path) -> None:
        from ow.commands.mv import execute_move

        def _then(unrepaired: list[str]) -> None:
            if unrepaired:
                log = self.query_one("#log", OperationLog)
                log.write(
                    Text.from_markup(f"[yellow]unrepaired aliases: {', '.join(unrepaired)}[/]")
                )
            self.notify(f"Moved {entry.name}", severity="information")

        self.run_operation(
            f"move {entry.name}",
            lambda: execute_move(self._config, entry.path, entry.ws, target),
            then=_then,
            reload=True,
        )

    # ---- remove (§4.7) -------------------------------------------------

    def action_remove(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.notify("No workspace selected", severity="warning")
            return
        if entry.archived:
            self.notify("Cannot remove archived workspace", severity="warning")
            return
        if entry.ws is None:
            self.notify("Workspace config not loaded", severity="warning")
            return
        from ow.commands.rm import survey_removal
        self.run_operation(
            f"survey {entry.name}",
            lambda: survey_removal(entry.path, entry.ws),
            then=lambda repos: self._push_remove_confirm(entry, repos),
            quiet=True,
        )

    def _push_remove_confirm(
        self, entry: WorkspaceEntry, repos: list
    ) -> None:

        lines = Text()
        lines.append(f"Remove workspace '{entry.name}'\n", style="bold")
        lines.append("This permanently deletes the workspace directory and worktree branches.\n", style="bold")
        lines.append("Uncommitted changes and unpushed commits will be lost.\n\n", style="bold")
        lines.append(f"  directory:  {display_path(entry.path)}\n")
        lines.append(f"  backup:     {display_path(paths.backups_dir())}/{entry.name}-<timestamp>.toml\n\n")
        for r in repos:
            alias = r.alias
            spec = r.spec.to_spec_str()
            lines.append(f"  {alias}  {spec}")
            if r.dirty:
                lines.append(f"  ⚠ {len(r.dirty)} uncommitted")
            elif r.unpushed:
                lines.append(f"  ⚠ {r.unpushed} unpushed")
            else:
                lines.append("  pushed — safe to delete")
            lines.append("\n")

        self.app.push_screen(
            ConfirmDialog("Remove workspace?", details=lines),
            callback=lambda ok: ok and self._do_remove(entry, repos),
        )

    def _do_remove(self, entry: WorkspaceEntry, repos: list) -> None:
        from ow.commands.rm import execute_removal

        def _then(_result: Any) -> None:
            self.notify(f"Removed {entry.name}", severity="information")

        self.run_operation(
            f"remove {entry.name}",
            lambda: execute_removal(entry.path.name, entry.path, repos),
            then=_then,
            reload=True,
        )

    # ---- open in editor (§4.8) -----------------------------------------

    def action_open_editor(self) -> None:
        if self._busy:
            self.notify("Operation in progress", severity="warning")
            return
        entry = self._selected_entry()
        if entry is None:
            self.notify("No workspace selected", severity="warning")
            return
        if entry.archived:
            self.notify("Cannot open archived workspace", severity="warning")
            return
        editor = self._config.editor
        if not editor:
            self.notify("No editor configured", severity="warning")
            return

        # suspend() must run on the main thread — it stops the asyncio loop.
        # Running it inside run_operation's worker thread is not thread-safe.
        log = self.query_one("#log", OperationLog)
        try:
            with self.app.suspend():
                result = subprocess.run(
                    [*shlex.split(editor), str(entry.path)]
                )
            log.write(f"editor exited with code {result.returncode}")
        except Exception:
            log.write(
                f"editor needs a terminal ow cannot release; "
                f"run: ow open {entry.name}"
            )

    # ---- new workspace (§4.9) ------------------------------------------

    def action_new_workspace(self) -> None:
        from ow.tui.workspace_forms import NewWorkspaceScreen
        self.app.push_screen(
            NewWorkspaceScreen(self._config),
            callback=lambda req: req and self._do_new_workspace(req),
        )

    def _do_new_workspace(self, req: Any) -> None:
        from ow.commands.init import cmd_init

        def _then(_result: Any) -> None:
            self.notify(f"Workspace '{req.name}' created", severity="information")

        self.run_operation(
            f"init {req.name}",
            lambda: cmd_init(
                self._config,
                name=req.name,
                templates=req.templates,
                repos=req.repos,
                configuration=req.configuration,
                parent=req.parent,
                yes=True,
            ),
            then=_then,
            reload=True,
        )

    # ---- edit workspace config (§4.10) ---------------------------------

    def action_edit_config(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.notify("No workspace selected", severity="warning")
            return
        if entry.archived:
            self.notify("Cannot edit archived workspace config", severity="warning")
            return
        if entry.ws is None:
            self.notify("Workspace config not loaded", severity="warning")
            return
        from ow.tui.workspace_forms import WorkspaceConfigScreen
        self.app.push_screen(
            WorkspaceConfigScreen(self._config, entry.path, entry.ws),
            callback=lambda new_ws: new_ws and self._do_save_ws_config(entry, new_ws),
        )

    def _do_save_ws_config(
        self, entry: WorkspaceEntry, new_ws: WorkspaceConfig
    ) -> None:
        config_path = entry.path / MARKER
        write_workspace_config(config_path, new_ws)
        entry.ws = new_ws
        self._status_cache.pop(entry.path, None)
        self._render_detail_for(entry)
        log = self.query_one("#log", OperationLog)
        log.write("workspace config saved")
        self.notify("Config saved", severity="information")
        self.app.push_screen(
            ConfirmDialog("Apply now?"),
            callback=lambda ok: ok and self._do_apply_after_edit(entry),
        )

    def _do_apply_after_edit(self, entry: WorkspaceEntry) -> None:
        from ow.commands.apply import cmd_apply
        self.run_operation(
            f"apply {entry.name}",
            lambda: cmd_apply(self._config, workspace=str(entry.path)),
            invalidate=entry.path,
        )

    # ---- edit global config (§4.11) ------------------------------------

    def action_edit_global_config(self) -> None:
        from ow.tui.global_config import GlobalConfigScreen
        self.app.push_screen(
            GlobalConfigScreen(self._config),
            callback=lambda new_cfg: new_cfg and self._do_save_global_config(new_cfg),
        )

    def _do_save_global_config(self, new_cfg: Config) -> None:
        from ow.utils.config import write_global_config
        write_global_config(new_cfg)
        # Mutate the shared Config object in place — never replace the
        # reference. `self._config` here is the *same object* as
        # `self.app._config` (passed by reference at construction); the
        # command palette's theme picker (Ctrl+P) mutates that shared object
        # directly, on the App. Reassigning `self._config` to a freshly loaded
        # object would make MainScreen's config diverge from the App's, so a
        # later save from this screen would carry the App's *stale*
        # pre-divergence theme value back over whatever the picker set after.
        reloaded = load_global_config()
        self._config.vars = reloaded.vars
        self._config.remotes = reloaded.remotes
        self._config.version = reloaded.version
        self._config.editor = reloaded.editor
        self._config.theme = reloaded.theme
        log = self.query_one("#log", OperationLog)
        log.write("global config saved")
        self.notify("Global config saved", severity="information")

    # ---- help ----------------------------------------------------------

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())


class DashboardApp(App[None]):
    TITLE = "ow"
    CSS_PATH = None

    DEFAULT_CSS = """
    CommandPalette > Vertical {
        max-width: 80;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel", "Cancel", show=False, priority=True),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._config = config
        self.main_screen: MainScreen | None = None
        self._theme_previewing = False
        """True while the command palette is open and a theme is highlighted."""

    def on_mount(self) -> None:
        # Apply saved theme
        try:
            self.theme = self._config.theme
        except Exception:
            self.notify(
                f"Theme '{self._config.theme}' is not available, using default.",
                severity="warning",
            )
        self.main_screen = MainScreen(self._config)
        self.push_screen(self.main_screen)

    async def action_cancel(self) -> None:
        # Check explicit MainScreen reference (no private API)
        if self.main_screen is not None and self.main_screen._busy:
            from ow.utils.git import terminate_children
            terminate_children()
            if self.main_screen.run_operation_worker is not None:
                self.main_screen.run_operation_worker.cancel()
            return
        # No busy MainScreen — dismiss top modal or confirm quit
        top = self.screen
        if isinstance(top, ModalScreen):
            # Ctrl+C is a `priority=True` binding: it is checked the
            # instant its Key event arrives, synchronously — which can, in
            # principle, run *before* an already-forwarded but
            # not-yet-processed message on some other widget's own queue
            # (e.g. a confirm keypress or button click on this same modal,
            # sent right before Ctrl+C) is even dequeued. A bounded,
            # near-zero-cost yield gives that pending message a chance to
            # finish first; if it did, `self.screen` is no longer `top`
            # and there is nothing left to cancel. This is cheap insurance,
            # not a hard guarantee: closing it completely would need
            # Ctrl+C's own dispatch to wait on that pending message, which
            # would make the legitimate "interrupt now" gesture laggy for
            # every other use of Ctrl+C — not an acceptable trade for a
            # race that in practice needs two keys delivered in the exact
            # same terminal write, which no human keystroke sequence does.
            for _ in range(5):
                if self.screen is not top:
                    return
                await asyncio.sleep(0)
            if self.screen is top:
                top.dismiss(None)
        elif self.main_screen is not None and not self.main_screen._busy:
            # Confirm before quitting
            self.push_screen(
                ConfirmDialog("Quit the dashboard?"),
                callback=lambda ok: ok and self.exit(),
            )
        else:
            self.exit()

    def watch_theme(self, old_theme: str, new_theme: str) -> None:
        """Persist theme changes — but NOT during live preview.

        The command palette highlights themes live as the user arrows
        through the list. During preview, ``_theme_previewing`` is True
        and this watcher skips persistence. Commit (Enter) and cancel
        (Escape) are handled in ``on_command_palette_closed``.

        Guard: do not write during the initial ``on_mount`` application
        of the already-loaded theme. If ``new_theme == self._config.theme``,
        the theme is already on disk and writing would be churn.
        """
        if self._theme_previewing:
            return
        if new_theme == self._config.theme:
            return
        self._config.theme = new_theme
        try:
            write_global_config(self._config)
        except Exception as exc:
            self.notify(f"Failed to save theme: {exc}", severity="error")

    def on_command_palette_option_highlighted(
        self, event: CommandPalette.OptionHighlighted
    ) -> None:
        """Preview the highlighted theme live in the command palette.

        Textual's built-in command palette (Ctrl+P → \"theme\") shows a list
        of themes. When the user arrows through the list, every highlight
        change fires OptionHighlighted on the CommandPalette, which reposts
        as CommandPalette.OptionHighlighted to the App.

        ThemeProvider sets each theme option's ``.hit.command`` to
        ``functools.partial(set_app_theme, <theme_name>)``. Non-theme
        commands (system commands, user providers) use plain callables.
        Guard by checking for a ``functools.partial`` wrapping the
        ``set_app_theme`` closure — only those are theme previews.

        Preview sets ``self.theme`` directly (NOT via the partial) and
        raises ``_theme_previewing`` so ``watch_theme`` skips persistence.
        """
        option = event.highlighted_event.option
        if option is None or option.hit is None:
            self._theme_previewing = False
            return
        callable_ = option.hit.command
        if isinstance(callable_, functools.partial) and callable_.func.__name__ == "set_app_theme":
            theme_name = callable_.keywords.get("name") or (callable_.args[0] if callable_.args else None)
            if theme_name is not None:
                self._theme_previewing = True
                self.theme = theme_name
                # Textual's refresh_css() doesn't re-resolve CSS variables on
                # the *current* screen (only on other screens in the stack).
                # The CommandPalette uses design tokens ($surface, $panel-darken-1)
                # that must be re-resolved when the theme changes, otherwise the
                # palette's own colors don't reflect the previewed theme.
                # Schedule the stylesheet update via call_next to ensure it runs
                # AFTER refresh_css (which is also scheduled via call_next).
                def _update_palette_styles() -> None:
                    palette = self.screen
                    if isinstance(palette, CommandPalette):
                        self.stylesheet.update(palette, animate=False)
                self.call_next(_update_palette_styles)
        else:
            self._theme_previewing = False

    def on_command_palette_closed(
        self, event: CommandPalette.Closed
    ) -> None:
        """Handle the command palette closing.

        Commit (Enter): ``option_selected=True``. If a theme was being
        previewed, persist it to disk. The ``call_later`` in Textual's
        palette will also call ``set_app_theme(name)`` — but since the
        reactive is already at that value, it won't re-fire, so we
        persist here instead.

        Cancel (Escape): ``option_selected=False``. Revert the display
        to the theme that was active when the palette opened.
        """
        self._theme_previewing = False
        if event.option_selected:
            # Commit: persist the current theme (already set by preview).
            if self.theme != self._config.theme:
                self._config.theme = self.theme
                try:
                    write_global_config(self._config)
                except Exception as exc:
                    self.notify(f"Failed to save theme: {exc}", severity="error")
        else:
            # Cancel: revert to the original theme.
            if self.theme != self._config.theme:
                self.theme = self._config.theme


def run_dashboard(config: Config) -> None:
    """Build the dashboard app and run it."""
    DashboardApp(config).run()
