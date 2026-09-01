"""Tests for the output-sink bridge (runner.py).

Builds a minimal DashboardApp stub with the widgets the sink drives,
then exercises run_operation through the four failure/capture paths.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Callable
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import ProgressBar, Static

from ow.tui.runner import TuiSink
from ow.tui.widgets import OperationLog
from ow.utils import display
from ow.utils.display import console


# ---------------------------------------------------------------------------
# Minimal dashboard stub — just enough to host the sink
# ---------------------------------------------------------------------------


class _StubApp(App[None]):
    """A bare-bones dashboard stand-in for the runner tests.

    Has the widgets the sink drives (#log, #progress, #task_label,
    #task_bar) and a `run_operation` that matches the plan's contract.
    """

    CSS = """
    #progress { display: none; height: 1; }
    #log { height: 1fr; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Vertical(
            Horizontal(
                Static("", id="task_label"),
                ProgressBar(total=1, id="task_bar", show_eta=False, show_percentage=False),
                id="progress",
            ),
            OperationLog(id="log"),
        )

    # -- run_operation (plan §3.5 signature) --------------------------------

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
        if self._busy:
            if not quiet:
                self.notify("An operation is already running", severity="warning")
            return
        self._busy = True
        self._run_worker(label, fn, then=then, quiet=quiet)

    @work(thread=True, group="op", exit_on_error=False)
    def _run_worker(
        self,
        label: str,
        fn: Callable[[], Any],
        *,
        then: Callable[[Any], None] | None = None,
        quiet: bool = False,
    ) -> None:
        log = self.query_one("#log", OperationLog)
        if not quiet:
            self.call_from_thread(log.write, f"── {label} ──")
        result: Any = None
        exit_code: int | None = None
        try:
            sink = TuiSink(self)
            with display.redirect_output(sink):
                result = fn()
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
        except Exception as exc:
            if not quiet:
                from rich.text import Text
                self.call_from_thread(log.write, Text(repr(exc), style="red"))
            exit_code = 1
        finally:
            self.call_from_thread(self._finish, label, exit_code, quiet, result, then)

    def _finish(
        self,
        label: str,
        exit_code: int | None,
        quiet: bool,
        result: Any,
        then: Callable[[Any], None] | None,
    ) -> None:
        log = self.query_one("#log", OperationLog)
        if not quiet:
            if exit_code is not None:
                log.write(f"{label}: failed (exit {exit_code})")
            else:
                log.write(f"{label}: done")
        if exit_code is None and then is not None:
            then(result)
        self._busy = False
        self.query_one("#progress", Horizontal).styles.display = "none"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log_lines(app: _StubApp) -> list[str]:
    """Extract plain-text lines from the log widget."""
    log = app.query_one("#log", OperationLog)
    lines: list[str] = []
    for line in log.lines:
        lines.append(line.plain if hasattr(line, "plain") else str(line))
    return lines


async def _run_app(fn_body: Callable[[_StubApp], None]) -> list[str]:
    """Start the stub, run fn_body, wait for workers, return log lines."""
    app = _StubApp()
    async with app.run_test() as pilot:
        fn_body(app)
        # Give the thread worker time to finish.
        await pilot.pause()
        for _ in range(100):
            if not app._busy:
                break
            await pilot.pause()
        # Extra pauses for call_from_thread writes to land.
        await pilot.pause()
        await pilot.pause()
        return _log_lines(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sys_exit_logs_failed():
    """sys.exit(1) leaves the app running and logs 'failed (exit 1)'."""

    def body(app: _StubApp) -> None:
        def fn() -> None:
            sys.exit(1)
        app.run_operation("test", fn)

    lines = asyncio.run(_run_app(body))
    assert any("failed (exit 1)" in line for line in lines), f"got: {lines}"


def test_runtime_error_logs_repr():
    """A RuntimeError logs its repr and leaves the app running."""

    def body(app: _StubApp) -> None:
        def fn() -> None:
            raise RuntimeError("boom")
        app.run_operation("test", fn)

    lines = asyncio.run(_run_app(body))
    assert any("RuntimeError" in line and "boom" in line for line in lines), f"got: {lines}"


def test_print_and_console_print_both_captured():
    """A fn that prints and calls console.print produces both lines in #log."""

    def body(app: _StubApp) -> None:
        def fn() -> None:
            print("plain line")
            console.print("[bold]rich line[/]")
        app.run_operation("test", fn)

    lines = asyncio.run(_run_app(body))
    # Both lines must appear, in call order.
    plain_idx = next((i for i, l in enumerate(lines) if "plain line" in l), None)
    rich_idx = next((i for i, l in enumerate(lines) if "rich line" in l), None)
    assert plain_idx is not None, f"'plain line' not found in {lines}"
    assert rich_idx is not None, f"'rich line' not found in {lines}"
    assert plain_idx < rich_idx, f"wrong order: {lines}"


def test_busy_refuses_second_operation():
    """Starting a second operation while _busy is refused."""

    def body(app: _StubApp) -> None:
        import threading
        barrier = threading.Event()

        def slow_fn() -> None:
            barrier.wait(timeout=5)

        app.run_operation("slow", slow_fn)
        # Wait until the worker is actually running.
        for _ in range(50):
            if app._busy:
                break
            import time; time.sleep(0.01)

        # This should be refused.
        app.run_operation("second", lambda: None)

        # Let the slow one finish.
        barrier.set()

    lines = asyncio.run(_run_app(body))
    # The second operation must NOT appear.
    assert not any("second" in line and "done" in line for line in lines), f"got: {lines}"
