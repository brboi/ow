"""PROTOTYPE — a dashboard built as an ow 3 plugin, using only the plugin interface and the CLI.

From the plugin interface it uses exactly one hook: `ow_commands(app)`, to add `ow tui`.
Everything else goes through the CLI surface, as a child process:
  list       `ow ls`
  status     `ow status -w <ws>`
  mutate     `ow reset -w <ws> -n`  -> confirm dialog -> `ow reset -w <ws> -y --no-fetch`

The `-n` run fetches and prints the plan; the `-y --no-fetch` run re-plans from the same cached
refs, so what was confirmed is what runs. Set OW3_TUI_REFETCH=1 to drop `--no-fetch` from the
second run and watch the guarantee break.
"""

from __future__ import annotations

import os
import subprocess
import threading
import sys
from pathlib import Path

import typer
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Label, ListItem, ListView, RichLog, Static

from ow3 import hookimpl

OW = [sys.executable, str(Path(__file__).with_name("ow3.py"))]  # real plugin: sys.argv[0]
CHILD_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def ow(*args: str) -> subprocess.CompletedProcess:
    # No stdin, own session: a child can neither prompt nor grab the TUI's terminal.
    return subprocess.run([*OW, *args], capture_output=True, text=True, env=CHILD_ENV,
                          stdin=subprocess.DEVNULL, start_new_session=True)


def ow_stream(*args: str):
    p = subprocess.Popen([*OW, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, env=CHILD_ENV, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    assert p.stdout
    for line in p.stdout:
        yield line.rstrip("\n")
    yield f"[exit {p.wait()}]"


class Confirm(ModalScreen[bool]):
    def __init__(self, title: str, plan: str):
        super().__init__()
        self.title_, self.plan = title, plan

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_, id="q")
            yield Static(self.plan, id="plan")
            with Horizontal():
                yield Button("Proceed (y)", id="yes", variant="error")
                yield Button("Cancel (n)", id="no")

    def on_button_pressed(self, e: Button.Pressed) -> None:
        self.dismiss(e.button.id == "yes")

    def key_y(self) -> None:
        self.dismiss(True)

    def key_n(self) -> None:
        self.dismiss(False)


class Dashboard(App):
    CSS = """
    #ws { width: 34; border: round $accent; }
    #status { height: 1fr; border: round $accent; padding: 0 1; }
    #log { height: 12; border: round $accent; }
    Confirm { align: center middle; }
    #dialog { width: 96; height: auto; border: thick $error; background: $surface; padding: 1 2; }
    #plan { margin: 1 0; }
    """
    BINDINGS = [("s", "status", "Status"), ("r", "reset", "Reset"), ("q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield ListView(id="ws")
            with Vertical():
                yield Static("", id="status")
                yield RichLog(id="log", wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "ow tui (PROTOTYPE)"
        self.load_workspaces()

    @property
    def current(self) -> str | None:
        lv = self.query_one("#ws", ListView)
        return lv.highlighted_child.name if lv.highlighted_child else None

    def log_line(self, line: str) -> None:
        self.query_one("#log", RichLog).write(line)

    @work(thread=True, exclusive=True, group="ls")
    def load_workspaces(self) -> None:
        res = ow("ls")
        # `ow ls` is human output: tab-separated name, path, repos. Parsing it is the awkward bit.
        rows = [line.split("\t") for line in res.stdout.splitlines()]
        self.call_from_thread(self._fill, rows)

    def _fill(self, rows) -> None:
        lv = self.query_one("#ws", ListView)
        lv.clear()
        for name, _path, repos in rows:
            lv.append(ListItem(Label(f"{name}\n  {repos}"), name=name))
        lv.index = 0

    def on_list_view_highlighted(self, _e) -> None:
        self.action_status()

    @work(thread=True, exclusive=True, group="status")
    def action_status(self) -> None:
        if not (ws := self.current):
            return
        res = ow("status", "-w", ws)
        self.call_from_thread(self.query_one("#status", Static).update, res.stdout + res.stderr)

    @work(thread=True, exclusive=True, group="op")
    def action_reset(self) -> None:
        if not (ws := self.current):
            return
        self.call_from_thread(self.log_line, f"$ ow reset -w {ws} -n")
        dry = ow("reset", "-w", ws, "-n")
        for line in (dry.stdout + dry.stderr).splitlines():
            self.call_from_thread(self.log_line, line)
        if dry.returncode != 0 or "nothing to do" in dry.stdout:
            return
        plan = dry.stdout.split("\n\n", 1)[-1].strip()  # the fetch lines, then a blank line, then the plan
        ok = self.ask(f"Reset {ws}?", plan)
        if not ok:
            self.call_from_thread(self.log_line, "declined")
            return
        argv = ["reset", "-w", ws, "-y"]
        if not os.environ.get("OW3_TUI_REFETCH"):
            argv.append("--no-fetch")
        self.call_from_thread(self.log_line, f"$ ow {' '.join(argv)}")
        for line in ow_stream(*argv):
            self.call_from_thread(self.log_line, line)
        self.action_status()
        self.load_workspaces()

    def ask(self, title: str, plan: str) -> bool:
        """Block this worker thread until the dialog is dismissed."""
        answer: list[bool] = []
        done = threading.Event()
        self.call_from_thread(self.push_screen, Confirm(title, plan),
                              lambda ok: (answer.append(bool(ok)), done.set()))
        done.wait()
        return answer[0]


@hookimpl
def ow_commands(app: typer.Typer) -> None:
    tui_app = typer.Typer(help="The dashboard (PROTOTYPE).")

    @tui_app.callback(invoke_without_command=True)
    def run(ctx: typer.Context) -> None:
        if ctx.invoked_subcommand is None:
            Dashboard().run()

    app.add_typer(tui_app, name="tui")
