"""`ow open` — runs the configured editor on a workspace directory."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ow.commands.open import cmd_open
from ow.utils import index
from ow.utils.config import BranchSpec, Config, WorkspaceConfig, write_workspace_config

MARKER = Path(".ow") / "config.toml"


def _make_workspace(tmp_path: Path, name: str = "parrot") -> Path:
    ws = tmp_path / "workspaces" / name
    ws.mkdir(parents=True)
    (ws / ".ow").mkdir()
    write_workspace_config(
        ws / MARKER,
        WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=["common"]),
    )
    index.remember(ws)
    return ws


def test_open_runs_the_configured_editor(tmp_path, monkeypatch, xdg):
    ws = _make_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = Config(vars={}, remotes={}, editor="myeditor -n")

    with patch(
        "ow.commands.open.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0),
    ) as run:
        cmd_open(config, workspace="parrot")

    run.assert_called_once()
    argv = run.call_args.args[0]
    assert argv[:2] == ["myeditor", "-n"]
    assert argv[-1] == str(ws)


def test_open_defaults_to_code(tmp_path, monkeypatch, xdg):
    """No `editor` key in the config means `code`."""
    ws = _make_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = Config(vars={}, remotes={})

    with patch(
        "ow.commands.open.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0),
    ) as run:
        cmd_open(config, workspace="parrot")

    argv = run.call_args.args[0]
    assert argv[0] == "code"
    assert argv[-1] == str(ws)


def test_open_reports_a_missing_editor(tmp_path, monkeypatch, capsys, xdg):
    _make_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = Config(vars={}, remotes={}, editor="definitely-not-a-real-binary")

    with patch("ow.commands.open.subprocess.run", side_effect=OSError("No such file")):
        with pytest.raises(SystemExit) as exc:
            cmd_open(config, workspace="parrot")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "could not run editor" in err
    assert "config" in err.lower()


def test_open_with_an_empty_editor_exits_nonzero(tmp_path, monkeypatch, capsys, xdg):
    _make_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = Config(vars={}, remotes={}, editor="")

    with pytest.raises(SystemExit) as exc:
        cmd_open(config, workspace="parrot")

    assert exc.value.code == 1
    assert "empty" in capsys.readouterr().err
