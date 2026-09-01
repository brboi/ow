"""Tests for display.redirect_output — the output sink for the dashboard."""

import sys
from unittest.mock import patch

import pytest
from rich.text import Text

from ow.utils import display as _display
from ow.utils import git as _git


def _make_sink():
    """A sink that records lines and tasks, returns the recorder."""
    lines = []
    tasks = []

    def line(renderable):
        lines.append(renderable)

    class _T:
        def __init__(self, label, total):
            self.label = label
            self.total = total
            self.advances = 0

        def advance(self):
            self.advances += 1

        def done(self):
            pass

    def task(label, total):
        t = _T(label, total)
        tasks.append(t)
        return t

    return _display.OutputSink(line=line, task=task), lines, tasks


def test_redirect_captures_styled_console_output(capsys):
    sink, lines, _ = _make_sink()

    with _display.redirect_output(sink):
        _display.console.print("[bold]hi[/]")
    assert len(lines) == 1
    rich_line = lines[0]
    assert isinstance(rich_line, Text)
    assert rich_line.plain == "hi"
    # bold style survives the trip — the render hook forwards renderables as-is
    style = lines[0].spans[0].style
    assert "bold" in str(style)


def test_redirect_captures_bare_print_to_stderr(capsys):
    sink, lines, _ = _make_sink()

    with _display.redirect_output(sink):
        print("plain line", file=sys.stderr)

    captured = capsys.readouterr()
    assert captured.err == "", "nothing may reach real stderr while redirected"
    plains = [r.plain for r in lines if isinstance(r, Text)]
    assert "plain line" in plains


def test_redirect_flushes_partial_line_on_exit(capsys):
    sink, lines, _ = _make_sink()

    with _display.redirect_output(sink):
        sys.stdout.write("no newline")

    captured = capsys.readouterr()
    assert captured.out == ""
    plains = [r.plain for r in lines if isinstance(r, Text)]
    assert "no newline" in plains


def test_redirect_restores_streams_on_exit(capsys):
    sink, _, _ = _make_sink()
    out_before = sys.stdout
    err_before = sys.stderr

    with _display.redirect_output(sink):
        assert sys.stdout is not out_before
        assert sys.stderr is not err_before

    assert sys.stdout is out_before
    assert sys.stderr is err_before
    # and output flows again
    print("after")
    assert "after" in capsys.readouterr().out


def test_redirect_nesting_raises():
    sink, _, _ = _make_sink()

    with _display.redirect_output(sink):
        with pytest.raises(RuntimeError, match="already redirected"):
            with _display.redirect_output(sink):
                pass


def test_redirect_cleans_up_on_inner_error(capsys):
    """An exception inside the context still restores streams and hooks."""
    sink, _, _ = _make_sink()
    out_before = sys.stdout

    with pytest.raises(ValueError):
        with _display.redirect_output(sink):
            print("inside")
            raise ValueError("boom")

    assert sys.stdout is out_before
    # and post-redirect console output still reaches the real terminal
    _display.console.print("after")
    assert "after" in capsys.readouterr().out


def test_task_progress_routes_through_sink_when_active():
    sink, _, tasks = _make_sink()

    with _display.redirect_output(sink):
        with _display.task_progress("fetching", total=3) as advance:
            advance()
            advance()

    assert len(tasks) == 1
    assert tasks[0].label == "fetching"
    assert tasks[0].total == 3
    assert tasks[0].advances == 2


def test_task_progress_falls_back_to_rich_when_no_sink():
    """No sink => the existing Rich Progress path still works (no crash)."""
    with _display.task_progress("noop", total=1) as advance:
        advance()


def test_git_run_cmd_routes_to_sink(capsys):
    sink, lines, _ = _make_sink()

    with _display.redirect_output(sink):
        result = _git.run_cmd(["git", "--version"])

    captured = capsys.readouterr()
    assert captured.out == "", "git output must not paint fd 1"
    assert result.returncode == 0
    plains = [r.plain for r in lines if isinstance(r, Text)]
    assert any("git version" in p for p in plains), plains


def test_git_probe_calls_unchanged_by_sink():
    """Probes pass capture_output=True; sink must not touch them."""
    sink, lines, _ = _make_sink()

    with _display.redirect_output(sink):
        result = _git._run(["git", "--version"], capture_output=True, text=True)

    assert result.returncode == 0
    assert "git version" in result.stdout
    # Probe output stays captured (not forwarded to sink)
    plains = [r.plain for r in lines if isinstance(r, Text)]
    assert not any("git version" in p for p in plains)
