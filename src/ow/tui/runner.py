"""Output-sink bridge for the dashboard.

The dashboard runs operations on a worker thread and captures every
print/Rich renderable/git subprocess line into the in-TUI log pane.
`TuiSink` is the `OutputSink` implementation that makes this work: it
forwards lines to `RichLog.write` and drives the progress row, all
through `App.call_from_thread` so the worker thread never touches
Textual widgets directly.

Every widget it touches (`#log`, `#progress`, `#task_label`,
`#task_bar`) lives on `MainScreen`, not on the App's default screen —
so every lookup is resolved through the screen, never through
`App.query_one`, which only ever searches the app's default screen and
raises `NoMatches` the moment a screen has been pushed on top of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.console import RenderableType

from ow.utils.display import OutputSink, SinkTask

if TYPE_CHECKING:
    from textual.screen import Screen


class _SinkTask:
    """A progress counter driven from a worker thread.

    `advance` and `done` hop to the app thread via `call_from_thread`
    because Textual widgets must only be mutated on the main thread.
    """

    def __init__(self, screen: Screen[Any]) -> None:
        self._screen = screen

    def advance(self) -> None:
        self._screen.app.call_from_thread(self._do_advance)

    def done(self) -> None:
        self._screen.app.call_from_thread(self._do_done)

    def _do_advance(self) -> None:
        from textual.widgets import ProgressBar
        bar = self._screen.query_one("#task_bar", ProgressBar)
        bar.advance(1)

    def _do_done(self) -> None:
        from textual.containers import Horizontal
        row = self._screen.query_one("#progress", Horizontal)
        row.remove_class("-active")


class TuiSink(OutputSink):
    """The dashboard's `OutputSink`: every line goes to `#log`.

    Constructed by `DashboardApp.run_operation` for each operation;
    the `with redirect_output(sink):` block in the worker installs it
    as the global sink, so every `print`, `console.print` and git
    subprocess line ends up in the log pane.
    """

    def __init__(self, screen: Screen[Any]) -> None:
        self._screen = screen
        super().__init__(line=self._line, task=self._task)

    def _line(self, renderable: RenderableType) -> None:
        from textual.widgets import RichLog
        def _write() -> None:
            log = self._screen.query_one("#log", RichLog)
            log.write(renderable)
        self._screen.app.call_from_thread(_write)

    def _task(self, label: str, total: int) -> SinkTask:
        self._screen.app.call_from_thread(self._start_task, label, total)
        return _SinkTask(self._screen)

    def _start_task(self, label: str, total: int) -> None:
        from textual.containers import Horizontal
        from textual.widgets import ProgressBar, Static
        row = self._screen.query_one("#progress", Horizontal)
        row.add_class("-active")
        lbl = self._screen.query_one("#task_label", Static)
        lbl.update(label)
        bar = self._screen.query_one("#task_bar", ProgressBar)
        # A total of 0 means the item count isn't known yet; ProgressBar's
        # indeterminate (barber-pole) mode is total=None, not total=0.
        bar.total = total if total > 0 else None
        bar.update(progress=0)
