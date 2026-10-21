"""Repository facts gathered from git."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ow.core import git


@dataclass(frozen=True)
class RepoFacts:
    """Facts about a repository gathered from git."""

    name: str
    path: Path
    bare: Path
    remotes: tuple[str, ...]
    head: str | None
    branch: str | None
    upstream: str | None
    upstream_ab: tuple[int, int] | None
    base: str | None
    default_base: str | None
    explicit_base: str | None
    operation: str | None
    modified: tuple[str, ...]
    untracked: tuple[str, ...]
    problem: str | None


# Operations to detect, in priority order (first one present wins), as
# (path git knows about, name reported to the user).
_OPERATIONS = (
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("MERGE_HEAD", "merge"),
    ("REVERT_HEAD", "revert"),
    ("BISECT_LOG", "bisect"),
)


def operation_in_progress(path: Path) -> str | None:
    """Return the git operation in progress in the worktree at path, or None.

    Runs `git -C <path> rev-parse --git-path <name>` for every operation git tracks and
    tests that the resolved path exists. Paths are resolved against `path` because git
    prints them relative to the `-C` directory; for a linked worktree the operation
    files live under the worktree's own gitdir (`.git/worktrees/<id>/`), so the bare
    repo must not be used as the base.

    A failing git call raises GitFailed: the caller decides whether that is a problem.
    """
    args = ["git", "-C", str(path), "rev-parse"]
    for git_name, _ in _OPERATIONS:
        args.extend(["--git-path", git_name])

    git_paths = git.read(args, check=True).split("\n")

    for (_git_name, operation), git_path in zip(_OPERATIONS, git_paths):
        if not git_path:
            continue
        resolved = Path(git_path)
        if not resolved.is_absolute():
            resolved = path / resolved
        if resolved.exists():
            return operation

    return None


def gather_facts(location) -> RepoFacts:
    """Gather facts about a repository from git (git reads only).

    A location that already carries a problem ("prunable"/"forgotten") returns minimal
    facts without running git and keeps that problem unchanged. A healthy location runs
    git, and a failing git call raises GitFailed: `problem` is only ever the location's,
    so inventing one here (e.g. from a failed `git status`) would lie about the worktree.
    """
    name = location.name
    path = location.path
    bare = location.bare
    default_base = location.default_base
    explicit_base = location.explicit_base
    problem = location.problem
    
    # If there's a problem, return minimal facts without git calls
    if problem:
        return RepoFacts(
            name=name,
            path=path,
            bare=bare,
            remotes=(),
            head=None,
            branch=None,
            upstream=None,
            upstream_ab=None,
            base=explicit_base or default_base,
            default_base=default_base,
            explicit_base=explicit_base,
            operation=None,
            modified=(),
            untracked=(),
            problem=problem,
        )
    
    # Read git status --porcelain=v2 --branch
    status_output = git.read(
        ["git", "-C", str(path), "status", "--porcelain=v2", "--branch"],
        check=True,
    )
    
    # Parse branch headers
    branch = None
    head = None
    upstream = None
    upstream_ab = None
    
    for line in status_output.split("\n"):
        if line.startswith("# branch.oid "):
            oid = line[13:].strip()
            if oid != "(initial)":
                head = oid
        elif line.startswith("# branch.head "):
            branch_name = line[14:].strip()
            if branch_name != "(detached)":
                branch = branch_name
            else:
                # Detached HEAD
                branch = None
        elif line.startswith("# branch.upstream "):
            upstream = line[18:].strip()
        elif line.startswith("# branch.ab "):
            ab_part = line[12:].strip()
            # Format: +A -B
            match = re.match(r"\+(\d+)\s+-(\d+)", ab_part)
            if match:
                upstream_ab = (int(match.group(1)), int(match.group(2)))
    
    # Parse file entries
    modified = []
    untracked = []
    
    for line in status_output.split("\n"):
        if not line or line.startswith("#"):
            continue
        
        parts = line.split(None, 1)
        if not parts:
            continue
        
        entry_type = parts[0]
        
        if entry_type == "1" and len(parts) > 1:
            # Ordinary changed entry: 1 <XY> ... <path>
            # Parse XY status
            rest = parts[1]
            fields = rest.split()
            if len(fields) >= 6:
                xy = fields[0]
                path_str = fields[6]
                # If the entry is modified (not just in one side)
                if xy != "..":
                    modified.append(path_str)
        elif entry_type == "2" and len(parts) > 1:
            # Renamed/copied entry: 2 <XY> ... <score> <path> <origPath>
            rest = parts[1]
            fields = rest.split()
            if len(fields) >= 8:
                xy = fields[0]
                path_str = fields[7]
                if xy != "..":
                    modified.append(path_str)
        elif entry_type == "u" and len(parts) > 1:
            # Unmerged entry
            rest = parts[1]
            fields = rest.split()
            if len(fields) >= 6:
                path_str = fields[6]
                modified.append(path_str)
        elif entry_type == "?":
            # Untracked file
            rest = parts[1]
            fields = rest.split()
            if fields:
                path_str = fields[0]
                untracked.append(path_str)
    
    # Read remotes from the bare repo (the worktree itself has none)
    remotes_output = git.read(["git", "-C", str(bare), "remote"], check=True)
    remotes = tuple(remotes_output.split("\n")) if remotes_output else ()
    
    # Check for operations in progress
    operation = operation_in_progress(path)
    
    return RepoFacts(
        name=name,
        path=path,
        bare=bare,
        remotes=remotes,
        head=head,
        branch=branch,
        upstream=upstream,
        upstream_ab=upstream_ab,
        base=explicit_base or default_base,
        default_base=default_base,
        explicit_base=explicit_base,
        operation=operation,
        modified=tuple(modified),
        untracked=tuple(untracked),
        problem=problem,
    )


def gather_all(locations: Sequence) -> tuple[RepoFacts, ...]:
    """Gather facts for multiple repositories in parallel.
    
    Args:
        locations: Sequence of RepoLocation objects.
    
    Returns:
        Tuple of RepoFacts, one per location.
    """
    with ThreadPoolExecutor() as executor:
        results = executor.map(gather_facts, locations)
    
    return tuple(results)
