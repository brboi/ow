import io

from ow.utils.display import _make_console, task_progress


def test_emits_nothing_when_redirected():
    """Same guarantee as the spinner it replaces: redirected output stays clean."""
    buf = io.StringIO()
    console = _make_console(file=buf, force_terminal=False)
    with task_progress("Fetching", 3, console=console) as advance:
        advance()
        advance()
    assert buf.getvalue() == ""


def test_shows_the_label_and_a_count_on_a_terminal():
    console = _make_console(force_terminal=True, width=120)
    with console.capture() as capture:
        with task_progress("Fetching", 3, console=console) as advance:
            advance()
    out = capture.get()
    assert "Fetching" in out
    assert "1/3" in out



def test_runs_the_wrapped_work_regardless(self=None):
    buf = io.StringIO()
    console = _make_console(file=buf, force_terminal=False)
    calls = []
    with task_progress("Setting up", 2, console=console) as advance:
        calls.append("ran")
        advance()
    assert calls == ["ran"]


def test_a_zero_total_does_not_divide_by_zero():
    console = _make_console(force_terminal=True, width=120)
    with console.capture() as capture:
        with task_progress("Nothing", 0, console=console):
            pass
    assert "Nothing" in capture.get()
