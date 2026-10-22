"""`ow shell-init`: the snippet runs in a real bash and a real zsh."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ZSH = shutil.which("zsh") or next(
    (p for p in ("/usr/bin/zsh", "/bin/zsh", "/usr/local/bin/zsh") if Path(p).exists()),
    None,
)


def test_bash_snippet_cds_and_completes(lab, tmp_path):
    ws = lab.ws("voip")
    ws.mkdir(parents=True)

    result = lab.ow("shell-init", "bash")

    assert result.exit_code == 0
    snippet = result.stdout
    assert "command ow cd" in snippet
    assert "_ow_completion" in snippet

    # A stub `ow` on the lab's PATH: `cd` answers a path, anything else fails like
    # the real binary refusing a bogus workspace.
    stub = Path(os.environ["PATH"]) / "ow"
    stub.write_text(f'#!/bin/sh\nif [ "$1" = cd ]; then printf "%s\\n" "{ws}"; exit 0; fi\nexit 1\n')
    stub.chmod(0o755)

    script = tmp_path / "ow-shell-init.sh"
    script.write_text(
        snippet
        + '\ncd "$HOME"\n'
        + "ow cd voip\n"
        + 'printf "%s" "$PWD"\n'
        + "complete -p ow >/dev/null 2>&1 && printf '\\ncomplete-ok'\n"
    )
    proc = subprocess.run(["bash", "--norc", str(script)], capture_output=True, text=True)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"{ws}\ncomplete-ok"


@pytest.mark.skipif(ZSH is None, reason="zsh not installed")
def test_zsh_snippet_runs_without_compinit(lab, tmp_path):
    result = lab.ow("shell-init", "zsh")

    assert result.exit_code == 0
    assert "command ow cd" in result.stdout
    assert "compdef _ow_completion ow" in result.stdout

    # The snippet must survive an `eval` that runs before `compinit`: no `compdef` yet.
    script = tmp_path / "ow-shell-init.zsh"
    script.write_text(result.stdout)
    proc = subprocess.run([ZSH, "-f", str(script)], capture_output=True, text=True)

    assert proc.returncode == 0, proc.stderr


def test_fish_snippet_defines_the_wrapper_and_completion(lab):
    fish = lab.ow("shell-init", "fish")

    assert fish.exit_code == 0
    assert "function ow" in fish.stdout
    assert "command ow cd" in fish.stdout
    assert "complete --command ow" in fish.stdout


def test_an_unknown_shell_is_a_usage_error(lab):
    result = lab.ow("shell-init", "powershell")

    assert result.exit_code == 2
