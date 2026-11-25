"""Shell completion end to end, through typer's private click vendoring.

`commands/shell_init.py` generates the script with `typer._click`, which is why
`typer` is capped below 0.30: these tests break when typer moves that API or changes
the script/`_OW_COMPLETE` protocol, before a user's tab key does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.conftest import make_bare, workspace
from tests.lab import make_executable

SRC = Path(__file__).resolve().parents[1]


def _real_ow_on_path() -> None:
    """Put an `ow` on the lab's PATH that runs this checkout's CLI, and `env` for the script."""
    bin_dir = Path(os.environ["PATH"])
    env = shutil.which("env", path=os.defpath)
    assert env is not None
    (bin_dir / "env").symlink_to(env)
    ow = bin_dir / "ow"
    ow.write_text(f'#!/bin/sh\nPYTHONPATH="{SRC}" exec "{sys.executable}" -m ow "$@"\n')
    make_executable(ow)


def _complete(tmp_path: Path, snippet: str, words: list[str]) -> list[str]:
    """What real bash offers when TAB is pressed after `words` (the last one is partial)."""
    script = tmp_path / "complete.sh"
    script.write_text(
        snippet
        + f"\nCOMP_WORDS=({' '.join(words)})\nCOMP_CWORD={len(words) - 1}\n"
        + '_ow_completion ow\nprintf "%s\\n" "${COMPREPLY[@]}"\n'
    )
    proc = subprocess.run(["bash", "--norc", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines()


def test_the_generated_bash_script_completes_commands_and_workspace_names(lab, tmp_path):
    bare = make_bare(lab)
    workspace(lab, name="voip", bare=bare)
    workspace(lab, name="novva", bare=bare)
    snippet = lab.ow("shell-init", "bash").stdout
    _real_ow_on_path()

    assert "_OW_COMPLETE" in snippet
    assert _complete(tmp_path, snippet, ["ow", "sta"]) == ["status"]
    assert _complete(tmp_path, snippet, ["ow", "cd", "v"]) == ["voip"]
    assert sorted(_complete(tmp_path, snippet, ["ow", "cd", ""])) == ["novva", "voip"]
