import io

from rich.console import Console

from ow.utils.display import _make_console, console, counts, err_console, print_git_result


def test_counts_behind_ahead():
    result = counts(3, 5)
    assert "yellow" in result
    assert "green" in result
    assert "↓3" in result
    assert "↑5" in result


def test_counts_zero_values():
    result = counts(0, 0)
    assert result.count("dim") == 2
    assert "↓0" in result
    assert "↑0" in result


# ---------------------------------------------------------------------------
# Console configuration
# ---------------------------------------------------------------------------


def test_console_does_not_recolor_plain_text():
    """Rich's ReprHighlighter must not repaint branch names, numbers or parens."""
    buf = io.StringIO()
    c = _make_console(file=buf, force_terminal=True, width=200)
    c.print("origin/18.0 (not applied) Cherry-picking 1/1")
    assert buf.getvalue() == "origin/18.0 (not applied) Cherry-picking 1/1\n"


def test_console_does_not_wrap_long_lines():
    """Status lines and compare URLs must survive a narrow / non-tty width."""
    buf = io.StringIO()
    c = _make_console(file=buf, force_terminal=True, no_color=True, width=20)
    line = "https://github.com/odoo/odoo/compare/18.0...saas-18.4-something-long"
    c.print(line)
    assert buf.getvalue() == line + "\n"


def test_module_consoles_share_the_safe_configuration():
    """Guard against a future `Console()` sneaking back in beside _make_console."""
    for c in (console, err_console):
        # Rich keeps ReprHighlighter around and gates it on this flag at print time.
        assert c._highlight is False
        assert c.soft_wrap is True


def test_err_console_writes_to_stderr():
    assert err_console.stderr is True
    assert console.stderr is False


# ---------------------------------------------------------------------------
# print_git_result
# ---------------------------------------------------------------------------


def test_print_git_result_success_goes_to_stdout(capsys):
    print_git_result("community", "fetch", ["origin", "master"], True)
    captured = capsys.readouterr()
    assert "[community]" in captured.out
    assert "✓" in captured.out
    assert captured.err == ""


def test_print_git_result_failure_goes_to_stderr(capsys):
    print_git_result("community", "fetch", ["origin", "master"], False, "not found")
    captured = capsys.readouterr()
    assert "[community]" in captured.err
    assert "✗" in captured.err
    assert "not found" in captured.err
    assert captured.out == ""


def test_print_git_result_error_preserves_bracketed_git_output(capsys):
    """git stderr is data, not markup: [rejected] is the diagnosis, it must survive."""
    print_git_result(
        "community", "push", ["origin", "master"], False,
        "! [rejected] master -> master (non-fast-forward)",
    )
    captured = capsys.readouterr()
    assert "[rejected]" in captured.err


def test_print_git_result_error_with_markup_close_does_not_raise(capsys):
    print_git_result("community", "fetch", ["origin", "master"], False, "bad ref [/] here")
    captured = capsys.readouterr()
    assert "[/]" in captured.err


def test_console_is_rich_console():
    assert isinstance(console, Console)


# ---------------------------------------------------------------------------
# Spinner (console.status)
#
# Regression guard, not a red-green cycle: the hand-rolled Spinner this
# replaced wrote "\r..." to stdout unconditionally, corrupting redirected
# output. Rich only animates on a terminal — lock that in.
# ---------------------------------------------------------------------------


def test_status_spinner_emits_nothing_when_redirected():
    buf = io.StringIO()
    c = _make_console(file=buf, force_terminal=False)
    with c.status("Fetching 3 ref(s)"):
        pass
    assert buf.getvalue() == ""


def test_status_spinner_animates_on_a_terminal():
    buf = io.StringIO()
    c = _make_console(file=buf, force_terminal=True)
    with c.status("Fetching 3 ref(s)"):
        pass
    assert "Fetching 3 ref(s)" in buf.getvalue()


def test_status_spinner_still_runs_the_wrapped_work():
    calls = []
    c = _make_console(file=io.StringIO(), force_terminal=False)
    with c.status("Setting up 2 repo(s)"):
        calls.append("ran")
    assert calls == ["ran"]
