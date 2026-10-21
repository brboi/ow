"""`ow shell-init` scenarios (§16): the snippet runs in a real bash — the one test layer
allowed a subprocess.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


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


def test_zsh_and_fish_snippets_print_their_shells(lab):
    zsh = lab.ow("shell-init", "zsh")

    assert zsh.exit_code == 0
    assert "command ow" in zsh.stdout
    assert "compdef _ow_completion ow" in zsh.stdout

    fish = lab.ow("shell-init", "fish")

    assert fish.exit_code == 0
    assert "function ow" in fish.stdout
    assert "command ow cd" in fish.stdout
    assert "complete --command ow" in fish.stdout


def test_an_unknown_shell_is_a_usage_error(lab):
    result = lab.ow("shell-init", "powershell")

    assert result.exit_code == 2
