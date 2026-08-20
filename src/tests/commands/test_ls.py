"""`ow ls` — list every known workspace, its path, and its repos.

No git: that is what `ow status` is for. Test 6 pins this at the single
subprocess choke point (`ow.utils.git._run`) rather than at some downstream
symptom, so a future command that starts calling git by a different path
still trips it.

Every test here reaches the index, so every test takes the `xdg` fixture.
Without it a test writes into the developer's real XDG state directory.
"""

import shutil
from pathlib import Path
from unittest.mock import patch

from ow.commands.ls import cmd_ls
from ow.utils import index
from ow.utils.config import WorkspaceConfig, parse_branch_spec, write_workspace_config


def _make_ws(base: Path, name: str, repos: dict[str, str] | None = None) -> Path:
    """A real workspace on disk, remembered in the index."""
    ws_dir = base / name
    write_workspace_config(
        ws_dir / ".ow" / "config.toml",
        WorkspaceConfig(
            repos={alias: parse_branch_spec(s) for alias, s in (repos or {}).items()},
            templates=["common"],
        ),
    )
    index.remember(ws_dir)
    return ws_dir


# ---------------------------------------------------------------------------
# 1. Two known workspaces: both appear, with name and aliases.
# ---------------------------------------------------------------------------

def test_two_workspaces_both_appear(tmp_path, capsys, xdg):
    _make_ws(tmp_path, "alpha", {"community": "master"})
    _make_ws(tmp_path, "beta", {"enterprise": "master..fix"})

    cmd_ls()

    out = capsys.readouterr().out
    assert "alpha" in out
    assert "beta" in out
    assert "community" in out
    assert "enterprise" in out


# ---------------------------------------------------------------------------
# 2. The home directory is abbreviated to ~.
# ---------------------------------------------------------------------------

def test_home_is_abbreviated(tmp_path, capsys, xdg, monkeypatch):
    fake_home = tmp_path / "home" / "dev"
    fake_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    ws_dir = _make_ws(fake_home, "myws")

    cmd_ls()

    out = capsys.readouterr().out
    # Built independently of the production helper: the literal string that
    # a correct implementation must produce, not a call to the same code.
    assert "~/myws" in out
    assert str(ws_dir) not in out


# ---------------------------------------------------------------------------
# 3. Empty index: message, exit 0, no exception.
# ---------------------------------------------------------------------------

def test_empty_index_reports_and_exits_cleanly(capsys, xdg):
    cmd_ls()  # must not raise

    out = capsys.readouterr().out
    assert "ow init" in out


# ---------------------------------------------------------------------------
# 4. A workspace with invalid TOML is marked in error, others still listed.
# ---------------------------------------------------------------------------

def test_broken_config_marked_error_others_still_listed(tmp_path, capsys, xdg):
    good = _make_ws(tmp_path, "good", {"community": "master"})
    broken = tmp_path / "broken"
    (broken / ".ow").mkdir(parents=True)
    (broken / ".ow" / "config.toml").write_text("this is [ not valid toml")
    index.remember(broken)

    cmd_ls()

    out = capsys.readouterr().out
    # The bug this guards: an implementation that lets the TOML error
    # propagate would blow up before "good" is ever printed. Only checking
    # exit behaviour would miss a "found the first error and stopped" bug
    # that still exits 0 but silently drops "good".
    assert "good" in out
    assert "community" in out
    assert "broken" in out
    assert "error" in out.lower()


# ---------------------------------------------------------------------------
# 5. A workspace whose directory vanished does not appear.
# ---------------------------------------------------------------------------

def test_vanished_workspace_does_not_appear(tmp_path, capsys, xdg):
    live = _make_ws(tmp_path, "live")
    gone = _make_ws(tmp_path, "gone")
    shutil.rmtree(gone)

    cmd_ls()

    out = capsys.readouterr().out
    assert "live" in out
    assert "gone" not in out


# ---------------------------------------------------------------------------
# 6. No git subprocess is ever launched.
# ---------------------------------------------------------------------------

def test_never_runs_git(tmp_path, capsys, xdg):
    _make_ws(tmp_path, "alpha", {"community": "master"})

    with patch("ow.utils.git._run") as mock_run:
        cmd_ls()

    mock_run.assert_not_called()
