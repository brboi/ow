"""Moving a workspace without orphaning its worktrees.

A linked worktree's own `.git` file points at the bare repo, which does not
move — but the bare repo's `worktrees/<id>/gitdir` points back at the old
worktree path. `git worktree repair <newpath>`, run against the bare repo,
rewrites it. From git-worktree(1): "running repair in the main worktree and
specifying the new <path> of each linked worktree will reestablish all
connections in both directions".

Shared by `ow mv`, `ow archive` and `ow unarchive`: all three move a
workspace directory and then have to reconnect what is inside it.
"""

import shutil
from pathlib import Path
from typing import Iterable

from ow.utils import paths
from ow.utils.git import run_cmd


def validate_target(ws_dir: Path, target: Path) -> str | None:
    """Why `target` is not a usable destination for `ws_dir`, or None."""
    if target.exists():
        return f"{target} already exists"
    if not target.parent.is_dir():
        return f"{target.parent} does not exist"
    if target == ws_dir or ws_dir in target.parents:
        return f"{target} is inside {ws_dir}"
    return None


def relocate_workspace(ws_dir: Path, target: Path, aliases: Iterable[str]) -> list[str]:
    """Move ws_dir to target, then repair each alias's worktree registration.

    Returns the aliases whose registration could not be repaired — a missing
    bare repo, or a `git worktree repair` that failed. Not fatal on its own:
    every file is already at the new path, and `ow apply` re-creates a
    worktree the bare repo has lost track of.
    """
    shutil.move(str(ws_dir), str(target))

    unrepaired: list[str] = []
    repos_dir = paths.repos_dir()
    for alias in aliases:
        bare_repo = repos_dir / f"{alias}.git"
        if not bare_repo.exists():
            unrepaired.append(alias)
            continue
        result = run_cmd(
            ["git", "-C", str(bare_repo), "worktree", "repair", str(target / alias)],
            quiet=True, label=alias,
        )
        if result.returncode != 0:
            unrepaired.append(alias)
    return unrepaired
