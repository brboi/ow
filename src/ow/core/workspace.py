"""Workspace derivation from git worktrees and repo configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping, Sequence

from ow.core.git import read as git_read
from ow.core.index import read as read_index
from ow.core.paths import repos_dir
from ow.errors import UsageError


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
    """The first directory at or above `start` holding `.ow/config.toml`, or None."""
    if start is None:
        start = Path.cwd()

    current = start.resolve()
    while True:
        if (current / ".ow" / "config.toml").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def derive_repos(
    root: Path, default_bases: Mapping[str, str] | None = None
) -> tuple[RepoLocation, ...]:
    """Repos whose `.git` file points at `<repos_dir>/<name>.git/worktrees/<id>`.

    Ordered by `default_bases` keys first, then by directory name. When two
    directories point at the same bare, the first wins; the other is a stray.
    """
    if default_bases is None:
        default_bases = {}

    repos_directory = repos_dir()
    locations = []
    seen_names = set()

    try:
        subdirs = sorted(d for d in root.iterdir() if d.is_dir())
    except OSError:
        return ()

    for subdir in subdirs:
        git_file = subdir / ".git"

        try:
            if not git_file.is_file():
                continue
        except OSError:
            continue

        try:
            with open(git_file, "r") as f:
                content = f.read().strip()
        except OSError:
            continue

        if not content.startswith("gitdir: "):
            continue

        gitdir_str = content[8:].strip()

        if os.path.isabs(gitdir_str):
            gitdir = Path(gitdir_str)
        else:
            gitdir = subdir / gitdir_str
        gitdir = gitdir.resolve()

        try:
            if gitdir.parent.name != "worktrees":
                continue

            worktree_id = gitdir.name
            bare_candidate = gitdir.parent.parent
            name = bare_candidate.stem

            expected_bare = repos_directory / f"{name}.git"
            if bare_candidate.resolve() != expected_bare.resolve():
                continue

            if name in seen_names:
                continue
            seen_names.add(name)

            bare = expected_bare
            problem = None
            branch = None
            explicit_base = None
            head = None

            worktree_dir = bare / "worktrees" / worktree_id
            if not worktree_dir.exists():
                problem = "forgotten"
            else:
                worktree_gitdir = worktree_dir / "gitdir"
                try:
                    if worktree_gitdir.exists():
                        with open(worktree_gitdir, "r") as f:
                            stored_gitdir = f.read().strip()
                        if (worktree_dir / stored_gitdir).resolve() != git_file.resolve():
                            problem = "prunable"
                except OSError:
                    problem = "prunable"

            if not problem:
                head_file = worktree_dir / "HEAD"
                try:
                    if head_file.exists():
                        with open(head_file, "r") as f:
                            head_content = f.read().strip()
                        if head_content.startswith("ref: "):
                            ref = head_content[5:]
                            if ref.startswith("refs/heads/"):
                                branch = ref[11:]
                        else:
                            head = head_content
                except OSError:
                    pass

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

            locations.append(
                RepoLocation(
                    name=name,
                    path=subdir,
                    bare=bare,
                    worktree_id=worktree_id,
                    default_base=default_bases.get(name),
                    problem=problem,
                    branch=branch,
                    explicit_base=explicit_base,
                    head=head,
                )
            )

        except (OSError, ValueError, IndexError):
            continue

    order = list(default_bases.keys())
    locations.sort(
        key=lambda loc: (0, order.index(loc.name), loc.name)
        if loc.name in default_bases
        else (1, loc.path.name, loc.name)
    )

    return tuple(locations)


def build_workspace(
    root: Path,
    locations: tuple[RepoLocation, ...],
    settings: dict[str, object],
) -> Workspace:
    """Build a Workspace from RepoLocations and settings."""
    repos = tuple(
        Repo(
            name=loc.name,
            path=loc.path,
            branch=loc.branch,
            base=loc.explicit_base or loc.default_base,
        )
        for loc in locations
    )

    return Workspace(
        root=root,
        name=root.name,
        repos=repos,
        settings=MappingProxyType(settings),
    )


def resolve_root(workspace: str | None = None, *, cwd: Path | None = None) -> Path:
    """Resolve a workspace name or path to its root directory.

    An index entry whose config is gone is dead: it is reported with a `ow prune` hint
    rather than recreated.
    """
    if cwd is None:
        cwd = Path.cwd()

    if workspace is None:
        root = find_root(cwd)
        if root is None:
            raise UsageError(
                "not inside an ow workspace; pass -w <name|path> or run `ow init`"
            )
        return root

    candidate = Path(workspace)
    if not candidate.is_absolute():
        candidate = cwd / candidate

    if "/" in workspace or workspace in (".", "..") or candidate.is_dir():
        candidate = candidate.resolve()
        if not (candidate / ".ow" / "config.toml").exists():
            raise UsageError(f"not an ow workspace; no .ow/config.toml in {candidate}")
        return candidate

    entries = [e for e in read_index() if e.name == workspace]
    alive = [e for e in entries if (e / ".ow" / "config.toml").exists()]
    if len(alive) == 1:
        return alive[0]
    if len(alive) > 1:
        paths = ", ".join(str(e) for e in alive)
        raise UsageError(f"workspace '{workspace}' is ambiguous; matches: {paths}")
    if entries:
        raise UsageError(
            f"workspace '{workspace}' is missing ({entries[0]}); run `ow prune`"
        )

    known = sorted({e.name for e in read_index() if (e / ".ow" / "config.toml").exists()})
    if known:
        raise UsageError(f"unknown workspace '{workspace}'; known: {', '.join(known)}")
    raise UsageError(
        f"unknown workspace '{workspace}'; nothing indexed yet; run `ow init` in it"
    )


def stray_checkouts(root: Path, locations: Sequence[RepoLocation]) -> tuple[Path, ...]:
    """Direct subdirectories holding a `.git` that no RepoLocation accounts for.

    Catches a second worktree of the same bare, a plain clone and a foreign worktree.
    """
    accounted = {loc.path.resolve() for loc in locations}
    strays = []

    try:
        subdirs = sorted(d for d in root.iterdir() if d.is_dir())
    except OSError:
        return ()

    for subdir in subdirs:
        resolved = subdir.resolve()
        if resolved in accounted:
            continue
        try:
            if (subdir / ".git").exists():
                strays.append(resolved)
        except OSError:
            pass

    return tuple(strays)
