import io

from rich.console import Console
from rich.text import Text

from ow.utils.display import counts


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


def test_print_git_result_success():
    from ow.utils.display import print_git_result
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, no_color=True, width=80)
    import ow.utils.display as display_mod
    orig = display_mod.console
    display_mod.console = console
    try:
        print_git_result("community", "fetch", ["origin", "master"], True)
    finally:
        display_mod.console = orig
    output = buf.getvalue()
    assert "[community]" in output
    assert "✓" in output


def test_print_git_result_failure():
    from ow.utils.display import print_git_result
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, no_color=True, width=80)
    import ow.utils.display as display_mod
    orig = display_mod.console
    display_mod.console = console
    try:
        print_git_result("community", "fetch", ["origin", "master"], False, "not found")
    finally:
        display_mod.console = orig
    output = buf.getvalue()
    assert "[community]" in output
    assert "✗" in output
    assert "not found" in output


def test_console_is_rich_console():
    from ow.utils.display import console
    assert isinstance(console, Console)
