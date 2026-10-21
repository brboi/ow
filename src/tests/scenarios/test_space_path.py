"""A workspace path containing a space: every printed line round-trips through bash."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ow.core.paths import repos_dir


def test_space_path_lines_run_through_bash(lab):
    remote = lab.remote("community", {"18.0": {"README.md": "c"}}, head="18.0")
    assert lab.ow("repo", "add", "community", f"file://{remote}").exit_code == 0

    ws = lab.home / "ws" / "my voip"
    result = lab.ow("init", str(ws), "community", "--base", "18.0")
    assert result.exit_code == 0
    assert (ws / "community" / ".git").is_file()

    # A second repo added inside the space-path workspace: its printed lines are
    # the specimen (plan §1.7). Each one, run through `bash -c` from the
    # workspace root, must give the same result.
    other = lab.remote("extra", {"18.0": {"README.md": "c"}}, head="18.0")
    result = lab.ow("repo", "add", "extra", f"file://{other}", "-w", str(ws))
    assert result.exit_code == 0
    lines = [line for line in result.stdout.splitlines() if line.startswith("git ")]

    assert any("worktree add --detach" in line for line in lines)
    add_line = next(line for line in lines if "worktree add --detach" in line)

    # Undo the worktree, then replay the printed line verbatim through bash.
    bare = repos_dir() / "extra.git"
    lab.git(bare, "worktree", "remove", str(ws / "extra"))
    assert not (ws / "extra" / ".git").exists()

    replay = subprocess.run(
        ["bash", "-c", add_line], cwd=str(ws), capture_output=True, text=True
    )
    assert replay.returncode == 0, replay.stderr
    assert (ws / "extra" / ".git").is_file()
    assert Path(lab.git(ws / "extra", "rev-parse", "HEAD")) == Path(
        lab.git(bare, "rev-parse", "origin/18.0")
    )
