"""Output-sink bridge for the dashboard.

The dashboard runs operations on a worker thread and captures every
print/Rich renderable/git subprocess line into the in-TUI log pane.
`TuiSink` is the `OutputSink` implementation that makes this work: it
forwards lines to `RichLog.write` and drives the progress row, all
through `App.call_from_thread` so the worker thread never touches
Textual widgets directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.console import RenderableType

from ow.utils.display import OutputSink, SinkTask

if TYPE_CHECKING:
    from textual.app import App


class _SinkTask:
    """A progress counter driven from a worker thread.

    `advance` and `done` hop to the app thread via `call_from_thread`
    because Textual widgets must only be mutated on the main thread.
    """

    def __init__(self, app: App[Any], task_id: int) -> None:
        self._app = app
        self._task_id = task_id

    def advance(self) -> None:
        self._app.call_from_thread(self._do_advance)

    def done(self) -> None:
        self._app.call_from_thread(self._do_done)

    def _do_advance(self) -> None:
        from textual.widgets import ProgressBar
        bar = self._app.query_one("#task_bar", ProgressBar)
        bar.advance(1)

    def _do_done(self) -> None:
        from textual.containers import Horizontal
        row = self._app.query_one("#progress", Horizontal)
        row.styles.display = "none"


class TuiSink(OutputSink):
    """The dashboard's `OutputSink`: every line goes to `#log`.

    Constructed by `DashboardApp.run_operation` for each operation;
    the `with redirect_output(sink):` block in the worker installs it
    as the global sink, so every `print`, `console.print` and git
    subprocess line ends up in the log pane.
    """

    def __init__(self, app: App[Any]) -> None:
        self._app = app
        super().__init__(line=self._line, task=self._task)

    def _line(self, renderable: RenderableType) -> None:
        from textual.widgets import RichLog
        log = self._app.query_one("#log", RichLog)
        self._app.call_from_thread(log.write, renderable)

    def _task(self, label: str, total: int) -> SinkTask:
        task_id = id(object())
        self._app.call_from_thread(self._start_task, label, total)
        return _SinkTask(self._app, task_id)

    def _start_task(self, label: str, total: int) -> None:
        from textual.containers import Horizontal
        from textual.widgets import ProgressBar, Static
        row = self._app.query_one("#progress", Horizontal)
        row.styles.display = "block"
        lbl = self._app.query_one("#task_label", Static)
        lbl.update(label)
        bar = self._app.query_one("#task_bar", ProgressBar)
        bar.total = total
        bar.update(progress=0)
