"""`ow trust` scenarios: confirmation without a terminal, and a trust file that cannot be read."""

from __future__ import annotations

from ow.core import trust
from ow.core.paths import trusted_plugins_file
from tests.conftest import add_repo

_PLUGIN = (
    "from ow.plugin_api import hookimpl\n"
    "OW_API_VERSION = 1\n"
    'PLUGIN_NAME = "demo_trust"\n'
)


def _workspace_with_a_plugin(lab, name: str = "with-plugin"):
    add_repo(
        lab,
        "demo",
        {"main": {"pyproject.toml": '[tool.ow]\nplugin = "ow_plugin.py"\n', "ow_plugin.py": _PLUGIN}},
        base="main",
    )
    ws = lab.home / name
    ws.mkdir(parents=True)
    assert lab.ow("init", "demo@main", cwd=ws).exit_code == 0
    return ws


def test_trust_without_a_terminal_needs_y_and_trusts_nothing_otherwise(lab):
    """X12: nobody read the code, so `ow trust` follows the rule of every confirmation: exit 2."""
    ws = _workspace_with_a_plugin(lab)

    res = lab.ow("trust", cwd=ws)

    assert res.exit_code == 2
    assert "re-run with -y" in res.stderr
    assert trust.list_trusted() == ()

    ok = lab.ow("trust", "-y", cwd=ws)

    assert ok.exit_code == 0, ok.stderr + ok.stdout
    assert trust.list_trusted() == ((ws / "demo" / "ow_plugin.py").resolve(),)


def test_trust_an_explicit_path_needs_no_confirmation(lab):
    ws = _workspace_with_a_plugin(lab)

    res = lab.ow("trust", str(ws / "demo" / "ow_plugin.py"), cwd=lab.home)

    assert res.exit_code == 0, res.stderr + res.stdout


def test_a_corrupt_trust_file_is_an_error_that_changes_nothing(lab):
    """R8: an unreadable trust file never reads as empty, so nothing rewrites it from one entry."""
    ws = _workspace_with_a_plugin(lab)
    registry = trusted_plugins_file()
    registry.parent.mkdir(parents=True, exist_ok=True)
    corrupt = b'trusted = ["/some/where/kept.py",\n  this is not toml'
    registry.write_bytes(corrupt)

    for argv in (
        ("trust", "-y"),
        ("trust", str(ws / "demo" / "ow_plugin.py")),
        ("trust", "--untrust", str(ws / "demo" / "ow_plugin.py")),
        ("trust", "--list"),
    ):
        res = lab.ow(*argv, cwd=ws)
        assert res.exit_code == 2, argv
        assert "cannot read the trust file" in res.stderr, argv
        assert registry.read_bytes() == corrupt, argv


def test_a_trust_file_with_the_wrong_shape_is_an_error_too(lab):
    ws = _workspace_with_a_plugin(lab)
    registry = trusted_plugins_file()
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text('trusted = "/a/single/string"\n')

    res = lab.ow("trust", "-y", cwd=ws)

    assert res.exit_code == 2
    assert "must hold `trusted = [...]`" in res.stderr
    assert registry.read_text() == 'trusted = "/a/single/string"\n'


def test_trusting_keeps_the_other_entries(lab):
    ws = _workspace_with_a_plugin(lab)
    registry = trusted_plugins_file()
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text('trusted = ["/kept/one.py", "/kept/two.py"]\n')

    assert lab.ow("trust", "-y", cwd=ws).exit_code == 0

    entries = {str(path) for path in trust.list_trusted()}
    assert entries == {"/kept/one.py", "/kept/two.py", str((ws / "demo" / "ow_plugin.py").resolve())}
