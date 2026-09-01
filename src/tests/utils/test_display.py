import io

import pytest
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
# confirm()
# ---------------------------------------------------------------------------


def test_confirm_returns_false_on_eof():
    """EOF at the prompt returns False."""
    from unittest.mock import patch
    from ow.utils import display as _display

    with patch.object(_display.Confirm, "ask", side_effect=EOFError):
        assert _display.confirm() is False


def test_confirm_returns_true_on_yes():
    """'y' at the prompt returns True."""
    from unittest.mock import patch
    from ow.utils import display as _display

    with patch.object(_display.Confirm, "ask", return_value=True) as asked:
        assert _display.confirm("Are you sure?") is True
        assert asked.call_args.kwargs["default"] is False


def test_confirm_returns_false_on_no():
    """'n' at the prompt returns False (the explicit default)."""
    from unittest.mock import patch
    from ow.utils import display as _display

    with patch.object(_display.Confirm, "ask", return_value=False):
        assert _display.confirm() is False


def test_confirm_refuses_under_a_sink():
    """A redirect has no visible terminal to read from — fail loud, not hang."""
    from ow.utils import display as _display

    def dummy_line(_): pass
    def dummy_task(_l, _t):
        class _T:
            def advance(self): pass
            def done(self): pass
        return _T()
    sink = _display.OutputSink(line=dummy_line, task=dummy_task)

    with _display.redirect_output(sink):
        with pytest.raises(RuntimeError, match="redirected"):
            _display.confirm()
