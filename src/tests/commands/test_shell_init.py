"""`ow shell-init` — the snippet has to parse, and `ow cd` has to reach it."""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ow.commands.cd import cmd_cd
from ow.commands.shell_init import cmd_shell_init
from ow.utils.config import BranchSpec, WorkspaceConfig, write_workspace_config
from ow.utils import index


MARKER = Path(".ow") / "config.toml"


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_snippet_parses(shell, tmp_path, capsys):
    """The bash/zsh snippet has to be real shell syntax."""
    capsys.readouterr()
    cmd_shell_init(shell)
    snippet = capsys.readouterr().out

    f = tmp_path / "snippet.sh"
    f.write_text(snippet)
    result = subprocess.run(["bash", "-n", str(f)], capture_output=True)
    assert result.returncode == 0, result.stderr

    assert "command ow cd" in snippet


def test_fish_snippet_parses_when_fish_is_available(tmp_path, capsys):
    if not shutil.which("fish"):
        pytest.skip("fish is not installed")

    capsys.readouterr()
    cmd_shell_init("fish")
    snippet = capsys.readouterr().out

    f = tmp_path / "snippet.fish"
    f.write_text(snippet)
    result = subprocess.run(["fish", "-n", str(f)], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_an_unsupported_shell_exits_nonzero(capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_shell_init("tcsh")

    assert exc.value.code == 1
    assert "bash, zsh, fish" in capsys.readouterr().err


def test_cd_prints_the_workspace_path(tmp_path, capsys, monkeypatch, xdg):
    """`ow cd` prints one absolute path on stdout, nothing else."""
    ws = tmp_path / "workspaces" / "parrot"
    ws.mkdir(parents=True)
    (ws / ".ow").mkdir()
    write_workspace_config(
        ws / MARKER,
        WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=["common"]),
    )
    index.remember(ws)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    capsys.readouterr()
    cmd_cd("parrot")
    out = capsys.readouterr().out

    assert out.strip() == str(ws)
