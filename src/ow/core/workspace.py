"""Workspace derivation from git worktrees and repo configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping

from ow.core.bases import BaseError, parse_base
from ow.core.git import read as git_read
from ow.core.paths import repos_dir


@dataclass(frozen=True)
class Repo:
    """A repo in a workspace as plugins see it."""

    name: str
    path: Path  # absolute worktree path
    branch: str | None  # None = detached
    base: str | None  # explicit_base or default_base


@dataclass(frozen=True)
class RepoLocation:
    """A repo location found in the workspace directory tree (core only)."""

    name: str
    path: Path
    bare: Path
    worktree_id: str
    default_base: str | None
    problem: Literal["prunable", "forgotten"] | None
    branch: str | None  # None = detached or unknown
    explicit_base: str | None
    head: str | None  # SHA when detached, else None


@dataclass(frozen=True)
class Workspace:
    """A workspace: a collection of repos under one directory."""

    root: Path
    name: str
    repos: tuple[Repo, ...]
    settings: Mapping[str, object]  # MappingProxyType in practice


def find_root(start: Path | None = None) -> Path | None:
    """Walk up the directory tree to find a workspace root.
    
    Returns the first directory that contains .ow/config.toml, or None.
    """
    if start is None:
        start = Path.cwd()

    start = start.resolve()

    current = start
    while True:
        if (current / ".ow" / "config.toml").exists():
            return current

        parent = current.parent
        if parent == current:
            # Reached root of filesystem
            return None
        current = parent


def derive_repos(
    root: Path, default_bases: Mapping[str, str] | None = None
) -> tuple[RepoLocation, ...]:
    """Derive repos from worktree subdirectories in a workspace.
    
    Each direct subdirectory whose .git is a file with gitdir pointing to
    <repos_dir()>/<name>.git/worktrees/<id> is a repo <name>.
    
    Args:
        root: workspace root directory
        default_bases: mapping of repo name to default base (optional)
    
    Returns:
        tuple of RepoLocation instances, ordered by default_bases keys first,
        then by directory name
    """
    if default_bases is None:
        default_bases = {}

    repos_directory = repos_dir()
    locations = []
    seen_names = set()

    # Try to read all direct subdirectories
    try:
        subdirs = sorted(d for d in root.iterdir() if d.is_dir())
    except OSError:
        return ()

    # Process each subdirectory
    for subdir in subdirs:
        git_file = subdir / ".git"

        # Check if .git is a file (not a directory)
        try:
            if not git_file.is_file():
                continue
        except OSError:
            continue

        # Read the .git file content
        try:
            with open(git_file, "r") as f:
                content = f.read().strip()
        except OSError:
            continue

        # Parse gitdir line: "gitdir: <path>"
        if not content.startswith("gitdir: "):
            continue

        gitdir_str = content[8:].strip()  # Remove "gitdir: " prefix

        # Resolve gitdir relative to subdir if it's relative
        if not os.path.isabs(gitdir_str):
            gitdir = subdir / gitdir_str
        else:
            gitdir = Path(gitdir_str)

        gitdir = gitdir.resolve()

        # Check if gitdir matches the pattern: <repos_dir>/<name>.git/worktrees/<id>
        try:
            # Expected structure:
            # gitdir = <repos_dir>/<name>.git/worktrees/<id>
            # gitdir.parent = <repos_dir>/<name>.git/worktrees
            # gitdir.parent.name = "worktrees"
            # gitdir.parent.parent = <repos_dir>/<name>.git
            if gitdir.parent.name != "worktrees":
                continue

            worktree_id = gitdir.name
            bare_candidate = gitdir.parent.parent
            name = bare_candidate.stem  # Remove .git suffix

            # Verify the full path matches
            expected_bare = repos_directory / f"{name}.git"
            if bare_candidate.resolve() != expected_bare.resolve():
                continue

            # Skip if we already saw this name (first one wins)
            if name in seen_names:
                continue
            seen_names.add(name)

            bare = expected_bare

            # Check for prunable or forgotten status
            problem = None
            branch = None
            explicit_base = None
            head = None

            # Check if worktree directory exists
            worktree_dir = bare / "worktrees" / worktree_id
            if not worktree_dir.exists():
                problem = "forgotten"
            else:
                # Check if gitdir still points to this .git
                worktree_gitdir = worktree_dir / "gitdir"
                try:
                    if worktree_gitdir.exists():
                        with open(worktree_gitdir, "r") as f:
                            stored_gitdir = f.read().strip()
                        if Path(stored_gitdir).resolve() != git_file.resolve():
                            problem = "prunable"
                except OSError:
                    problem = "prunable"

            # Read branch and HEAD from worktree directory (if it exists)
            if not problem:
                head_file = worktree_dir / "HEAD"
                try:
                    if head_file.exists():
                        with open(head_file, "r") as f:
                            head_content = f.read().strip()
                        if head_content.startswith("ref: "):
                            # On a branch
                            ref = head_content[5:]  # Remove "ref: " prefix
                            if ref.startswith("refs/heads/"):
                                branch = ref[11:]  # Remove "refs/heads/"
                        else:
                            # Detached HEAD: store the SHA
                            head = head_content
                except OSError:
                    pass

            # Read explicit_base from bare repo config (only if on a branch)
            if branch and not problem:
                try:
                    explicit_base = git_read(
                        ["git", "-C", str(bare), "config", "--get", f"branch.{branch}.ow-base"],
                        check=False,
                    )
                    if not explicit_base:
                        explicit_base = None
                except Exception:
                    explicit_base = None

            # Get default_base from the mapping
            default_base = default_bases.get(name)

            locations.append(
                RepoLocation(
                    name=name,
                    path=subdir,
                    bare=bare,
                    worktree_id=worktree_id,
                    default_base=default_base,
                    problem=problem,
                    branch=branch,
                    explicit_base=explicit_base,
                    head=head,
                )
            )

        except (OSError, ValueError, IndexError):
            continue

    # Sort: first by default_bases keys in order, then by name
    def sort_key(loc: RepoLocation) -> tuple:
        if loc.name in default_bases:
            # Return a tuple that sorts earlier: (0, position in default_bases)
            position = list(default_bases.keys()).index(loc.name)
            return (0, position, loc.name)
        else:
            # Return a tuple that sorts later: (1, name)
            return (1, loc.path.name, loc.name)

    locations.sort(key=sort_key)

    return tuple(locations)


def build_workspace(
    root: Path,
    locations: tuple[RepoLocation, ...],
    settings: dict[str, object],
) -> Workspace:
    """Build a Workspace from RepoLocations and settings.
    
    Args:
        root: workspace root directory
        locations: tuple of RepoLocation instances
        settings: settings dict (will be wrapped in MappingProxyType)
    
    Returns:
        Workspace instance
    """
    repos = []
    for loc in locations:
        repo = Repo(
            name=loc.name,
            path=loc.path,
            branch=loc.branch,
            base=loc.explicit_base or loc.default_base,
        )
        repos.append(repo)

    return Workspace(
        root=root,
        name=root.name,
        repos=tuple(repos),
        settings=MappingProxyType(settings),
    )
