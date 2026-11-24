"""Workspace derivation from git worktrees and repo configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from ow.core import config
from ow.core.git import GitFailed
from ow.core.git import read as git_read
from ow.core.index import read as read_index
from ow.core.paths import repos_dir
from ow.errors import UsageError


@dataclass(frozen=True, kw_only=True)
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


@dataclass(frozen=True, kw_only=True)
class Workspace:
    """A workspace (or one stack inside a multi-stack workspace): repos under one directory."""

    root: Path
    name: str
    repos: tuple[Repo, ...]
    settings: Mapping[str, object]  # MappingProxyType in practice
    is_stack: bool = False
    # Setting key -> layer that supplied its value ("stack", "workspace" or "global");
    # a key absent here holds its declared default.
    origins: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def is_explicit(self, key: str) -> bool:
        """Whether the user set `key` in a config layer (so it is not a declared default)."""
        return self.origins.get(key, "default") != "default"


def find_root(start: Path | None = None) -> Path | None:
    """The enclosing mono-stack workspace or stack directory at or above `start`, or None.

    When an ancestor `.ow/config.toml` declares a multi-stack workspace (`[stacks]`),
    returns `<root>/<stack>` if `start` is inside a declared stack, else `None` (so
    callers at the multi-stack root detect the container via `detect_multi_workspace`).
    """
    if start is None:
        start = Path.cwd()

    resolved_start = start.resolve()
    current = resolved_start
    while True:
        cfg = current / ".ow" / "config.toml"
        if cfg.exists():
            if stack_names := config.stack_names_in_config(cfg):
                if resolved_start == current:
                    return None
                try:
                    rel = resolved_start.relative_to(current)
                except ValueError:
                    return None
                top_part = rel.parts[0]
                if top_part in stack_names:
                    stack_dir = current / top_part
                    if stack_dir.is_dir():
                        return stack_dir.resolve()
                return None
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
            with open(git_file) as f:
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
                    if not worktree_gitdir.exists():
                        problem = "prunable"
                    else:
                        with open(worktree_gitdir) as f:
                            stored_gitdir = f.read().strip()
                        if (worktree_dir / stored_gitdir).resolve() != git_file.resolve():
                            problem = "prunable"
                except OSError:
                    problem = "prunable"

            if not problem:
                head_file = worktree_dir / "HEAD"
                try:
                    if head_file.exists():
                        with open(head_file) as f:
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
                except (GitFailed, OSError):
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
    *,
    is_stack: bool = False,
    origins: Mapping[str, str] | None = None,
) -> Workspace:
    """Build a Workspace from RepoLocations and settings.

    `origins` maps each setting key to the layer that supplied it (as resolved by
    `config.resolve_settings`); keys at their default may be omitted or marked "default".
    """
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
        is_stack=is_stack,
        origins=MappingProxyType({k: v for k, v in (origins or {}).items() if v != "default"}),
    )


def _entry_kind(path: Path) -> Literal["workspace", "multi"] | None:
    """Classify an indexed or target directory as mono-stack workspace/stack, multi-stack workspace, or dead."""
    from ow.core.stacks import detect_multi_workspace, is_stack_dir

    cfg = path / ".ow" / "config.toml"
    if cfg.exists():
        if config.stack_names_in_config(cfg):
            return "multi" if detect_multi_workspace(path) else None
        return "workspace"
    if path.is_dir() and is_stack_dir(path):
        return "workspace"
    return None


def resolve_target_root(
    workspace: str | None = None, *, cwd: Path | None = None
) -> tuple[Path, Literal["workspace", "multi"]]:
    """Resolve a workspace or stack name/path to `(root, kind)`.

    Supports:
    - implicit `cwd` lookup (mono-stack workspace via `find_root`, or multi-stack root via `detect_multi_workspace`);
    - direct child or filesystem path (prioritizing `./<stack>` inside a multi-stack workspace);
    - `<workspace>/<stack>` disambiguation against the index;
    - indexed workspace name (mono-stack or multi-stack);
    - unique stack name across indexed multi-stack workspaces.
    """
    from ow.core.stacks import detect_multi_workspace, is_stack_dir

    if cwd is None:
        cwd = Path.cwd()

    if workspace is None:
        root = find_root(cwd)
        if root is not None:
            return root, "workspace"
        if detect_multi_workspace(cwd):
            return cwd.resolve(), "multi"
        raise UsageError(
            "not inside an ow workspace; pass -w <name|path> or run `ow init`"
        )

    candidate = Path(workspace)
    if not candidate.is_absolute():
        candidate = cwd / candidate

    cand_kind = _entry_kind(candidate)
    if cand_kind is not None:
        return candidate.resolve(), cand_kind

    # `<workspace>/<stack>` disambiguation against indexed multi-stack workspaces
    if (
        "/" in workspace
        and not workspace.startswith(("/", ".", "~"))
        and workspace.count("/") == 1
    ):
        ws_part, stack_part = workspace.split("/", 1)
        if ws_part and stack_part:
            stack_matches: list[Path] = []
            for entry in read_index():
                if entry.name == ws_part and detect_multi_workspace(entry):
                    child = entry / stack_part
                    if child.is_dir() and is_stack_dir(child):
                        stack_matches.append(child.resolve())
            if len(stack_matches) == 1:
                return stack_matches[0], "workspace"
            if len(stack_matches) > 1:
                paths = ", ".join(str(p) for p in stack_matches)
                raise UsageError(f"stack '{workspace}' is ambiguous; matches: {paths}")

    if "/" in workspace or workspace in (".", ".."):
        resolved = candidate.resolve()
        raise UsageError(f"not an ow workspace; no .ow/config.toml in {resolved}")

    entries = [e for e in read_index() if e.name == workspace]
    alive = [(e, kind) for e in entries if (kind := _entry_kind(e)) is not None]
    if len(alive) == 1:
        return alive[0][0].resolve(), alive[0][1]
    if len(alive) > 1:
        paths = ", ".join(str(e) for e, _ in alive)
        raise UsageError(f"workspace '{workspace}' is ambiguous; matches: {paths}")
    if entries:
        raise UsageError(
            f"workspace '{workspace}' is missing ({entries[0]}); run `ow prune`"
        )

    # Unique stack name across indexed multi-stack workspaces
    stack_hits: list[tuple[Path, Path]] = []
    for entry in read_index():
        stacks = detect_multi_workspace(entry)
        if not stacks:
            continue
        for s in stacks:
            if s.name == workspace:
                stack_hits.append((entry, s))
    if len(stack_hits) == 1:
        return stack_hits[0][1].resolve(), "workspace"
    if len(stack_hits) > 1:
        hints = ", ".join(f"{parent.name}/{stack.name}" for parent, stack in stack_hits)
        raise UsageError(
            f"stack '{workspace}' is ambiguous; matches: {hints}; specify <workspace>/<stack>"
        )

    known = sorted({e.name for e in read_index() if _entry_kind(e) is not None})
    if known:
        raise UsageError(f"unknown workspace '{workspace}'; known: {', '.join(known)}")
    raise UsageError(
        f"unknown workspace '{workspace}'; nothing indexed yet; run `ow init`"
    )


def resolve_root(workspace: str | None = None, *, cwd: Path | None = None) -> Path:
    """Resolve a mono-stack workspace or stack name/path to its root directory.

    Raises UsageError when the resolved target is a multi-stack workspace container.
    """
    from ow.core.stacks import detect_multi_workspace

    root, kind = resolve_target_root(workspace, cwd=cwd)
    if kind == "multi":
        stacks = detect_multi_workspace(root) or ()
        names = ", ".join(s.name for s in stacks)
        if workspace is None:
            raise UsageError(
                f"'{root.name}' is a multi-stack workspace; specify a stack with -w <stack> (available: {names})"
            )
        raise UsageError(
            f"'{root.name}' is a multi-stack workspace; specify a stack as {root.name}/<stack> (available: {names})"
        )
    return root


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
                strays.append(subdir)
        except OSError:
            pass

    return tuple(strays)
