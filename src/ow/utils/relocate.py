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
from ow.utils.config import Config, WorkspaceConfig
from ow.utils.git import run_cmd
from ow.utils.render import RenderResult
from ow.utils.workspace import refresh_workspace, require_mise


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
    every file is already at the new path, and `ow init` re-creates a
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


def refresh_after_relocation(config: Config, ws: WorkspaceConfig, target: Path) -> RenderResult | None:
    """Bring `target`'s generated files in line with `ws`/`config`, once the move is done.

    A workspace still on schema 1 — its own file or the global config — is
    left exactly as it is: converting it is `ow render`'s job alone, never
    a side effect of `mv`/`unarchive`. `None` says so plainly; the caller
    points at `ow render -w target` and touches neither the manifest nor
    the lock. A schema-2 workspace gets mise's one prerequisite check, then
    a full refresh. Either failure comes back as `RenderResult.errors` —
    the move has already happened, and there is nothing here to roll back.
    """
    if config.version == 1 or ws.version == 1:
        return None
    try:
        require_mise()
    except ValueError as exc:
        return RenderResult(errors=(str(exc),))
    return refresh_workspace(config, ws, target, trust=False)
