import io
import sys
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Protocol

from rich.console import Console, NewLine, RenderableType, RenderHook
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm
from rich.text import Text


def _make_console(**kwargs) -> Console:
    """Build a Console safe for CLI output.

    highlight=False keeps Rich's ReprHighlighter from repainting branch names
    and numbers ("origin/18.0"); soft_wrap=True keeps long status lines and
    URLs on one line when stdout is redirected (Rich falls back to 80 columns).
    """
    return Console(highlight=False, soft_wrap=True, **kwargs)


console = _make_console()
err_console = _make_console(stderr=True)


# ---------------------------------------------------------------------------
# Output redirection: sending a command's output somewhere that is not a
# terminal — the dashboard's log pane.
# ---------------------------------------------------------------------------


class SinkTask(Protocol):
    """A progress counter owned by the sink."""

    def advance(self) -> None: ...

    def done(self) -> None: ...


@dataclass(frozen=True)
class OutputSink:
    """Where human-facing output goes while a TUI owns the terminal.

    `line` takes one whole line, as a Rich renderable: styles survive the
    trip, so the log pane shows what the terminal would have shown.
    """

    line: Callable[[RenderableType], None]
    task: Callable[[str, int], SinkTask]


_sink: OutputSink | None = None


def current_sink() -> OutputSink | None:
    """The active sink, or None when output goes straight to the terminal."""
    return _sink


class _SinkHook(RenderHook):
    """Diverts a Console's renderables to a sink instead of its file.

    Returning an empty list is what suppresses the write: Console.print
    applies render hooks before turning renderables into segments, so a
    hook that keeps none leaves the buffer empty and nothing reaches the
    file. The renderables themselves are handed over unrendered, which is
    why the sink can lay them out at its own width.
    """

    def __init__(self, sink: OutputSink) -> None:
        self._sink = sink

    def process_renderables(self, renderables: list) -> list:
        for renderable in renderables:
            # console.print() with no arguments: a bare line break.
            self._sink.line(Text("") if isinstance(renderable, NewLine) else renderable)
        return []


class _LineWriter(io.TextIOBase):
    """A text stream that hands whole lines to a sink.

    Most of ow's output is a bare print(), not a Rich console, so catching
    only the consoles would swallow half of every command's report.
    """

    def __init__(self, line: Callable[[RenderableType], None]) -> None:
        self._line = line
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            head, _, self._buffer = self._buffer.partition("\n")
            self._line(Text(head))
        return len(text)

    def flush(self) -> None:
        if self._buffer:
            self._line(Text(self._buffer))
            self._buffer = ""

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False


@contextmanager
def redirect_output(sink: OutputSink) -> Iterator[None]:
    """Send every print, Rich renderable and git subprocess line to `sink`.

    Not reentrant and not thread-safe: exactly one operation may run under
    it at a time, because it swaps process-global state (sys.stdout, the
    consoles' render hooks). The dashboard enforces that with a busy flag.
    """
    global _sink
    if _sink is not None:
        raise RuntimeError("output already redirected")

    out_writer = _LineWriter(sink.line)
    err_writer = _LineWriter(sink.line)
    out_hook = _SinkHook(sink)
    err_hook = _SinkHook(sink)

    console.push_render_hook(out_hook)
    err_console.push_render_hook(err_hook)
    _sink = sink
    try:
        with redirect_stdout(out_writer), redirect_stderr(err_writer):
            try:
                yield
            finally:
                out_writer.flush()
                err_writer.flush()
    finally:
        _sink = None
        err_console.pop_render_hook()
        console.pop_render_hook()


def counts(behind: int, ahead: int) -> str:
    b_color = "yellow" if behind > 0 else "dim"
    a_color = "green" if ahead > 0 else "dim"
    return f"[{b_color}]↓{behind}[/] [{a_color}]↑{ahead}[/]"


def display_path(path: Path) -> str:
    """Render a path with the home directory abbreviated to ~.

    A full absolute path drowns the useful part of a listing in repetition —
    every entry shares the same long prefix.
    """
    home = Path.home()
    try:
        rel = path.relative_to(home)
    except ValueError:
        return str(path)
    if str(rel) == ".":
        return "~"
    return f"~/{rel}"


def confirm(message: str = "Proceed?") -> bool:
    """Default is no. A destructive command must not proceed unasked.

    Refuses outright when output is redirected: there is no terminal to
    read from, so a prompt there would hang the TUI with an invisible
    question. Callers driving a command from the dashboard confirm in the
    TUI first and pass yes=True.
    """
    if current_sink() is not None:
        raise RuntimeError(
            "confirm() cannot prompt while output is redirected — "
            "the caller must pass yes=True"
        )
    try:
        return Confirm.ask(message, default=False, console=console)
    except (EOFError, KeyboardInterrupt):
        return False


def print_git_result(alias: str, cmd: str, args: list[str], ok: bool, error: str | None = None) -> None:
    cmd_str = f"  [{alias}] git {cmd} {' '.join(args)}"
    text = Text(cmd_str)
    text.append(" ")
    text.append("✓" if ok else "✗", style="green" if ok else "red")
    if ok:
        console.print(text)
        return
    err_console.print(text)
    if error:
        # git stderr is data, not markup: Text() keeps "[rejected]" intact.
        err_console.print(Text(f"  Error: {error}"))


@contextmanager
def task_progress(label: str, total: int, *, console: Console | None = None) -> Iterator:
    """A `label n/total` counter, silent when the output is not a terminal.

    Replaces the fixed-text spinner: the count only became possible once
    parallel_per_repo started reporting completions as they happen (#25).

    Under a sink there is no terminal to animate: the counter is handed to
    the sink, which owns its own progress widget.
    """
    sink = current_sink()
    if sink is not None:
        task = sink.task(label, total)
        try:
            yield task.advance
        finally:
            task.done()
        return

    target = console if console is not None else globals()["console"]
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TextColumn("{task.completed}/{task.total}"),
        console=target,
        disable=not target.is_terminal,
    )
    with progress:
        task_id = progress.add_task(label, total=total)
        yield lambda: progress.advance(task_id)
