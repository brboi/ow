"""`ow trust` — trust, untrust, or list plugin locations (plugin files or directories)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.commands import refresh as refresh_cmd
from ow.core import git, stacks, trust
from ow.core.session import open_session
from ow.core.workspace import derive_repos, resolve_target_root
from ow.errors import UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


def _discover_workspace_plugins(
    workspace: str | None, *, cwd: Path
) -> tuple[Path, str, tuple[trust.DiscoveredPlugin, ...]]:
    """Resolve `workspace` (or `cwd`) and discover every in-repo plugin across its repos."""
    root, kind = resolve_target_root(workspace, cwd=cwd)
    if kind == "multi":
        stack_paths = stacks.detect_multi_workspace(root) or ()
        repo_dirs = [
            (loc.name, loc.path)
            for stack_path in stack_paths
            for loc in derive_repos(stack_path)
        ]
        return root, kind, trust.discover_repo_plugins(repo_dirs)

    if stacks.is_stack_dir(root):
        parent = root.parent.resolve()
        stack_paths = stacks.detect_multi_workspace(parent) or (root,)
        repo_dirs = [
            (loc.name, loc.path)
            for stack_path in stack_paths
            for loc in derive_repos(stack_path)
        ]
        return parent, "multi", trust.discover_repo_plugins(repo_dirs)

    repo_dirs = [(loc.name, loc.path) for loc in derive_repos(root)]
    return root, kind, trust.discover_repo_plugins(repo_dirs)


def _list() -> None:
    for entry in trust.list_trusted():
        shown = git.display_path(entry)
        term.out(f"{shown}/" if entry.is_dir() else shown)


def _untrust(path: Path) -> None:
    """Revoke `path`'s own entry and say so when a trusted directory still covers it."""
    resolved, removed = trust.untrust_path(path)
    verb = "untrusted" if removed else "was not trusted"
    term.out(f"{verb} {git.display_path(resolved)}")
    covering = trust.trusted_by(resolved)
    if covering is not None:
        term.warn(
            f"{git.display_path(resolved)} is still trusted through "
            f"{git.display_path(covering)}; run `ow trust --untrust {covering}` to revoke it"
        )


def _trust(path: Path) -> None:
    resolved = trust.trust_path(path)
    term.out(f"trusted {git.display_path(resolved)}")


def _choose_locations(
    root: Path, discovered: tuple[trust.DiscoveredPlugin, ...]
) -> tuple[Path, ...]:
    """Ask what to trust: the plugin files found, the workspace root, or its parent directory."""
    choices: list[tuple[str, tuple[Path, ...]]] = []
    if discovered:
        files = tuple(item.path for item in discovered)
        listing = ", ".join(git.display_path(p) for p in files)
        choices.append((f"only the plugin file(s) found: {listing}", files))
    choices.append((f"everything in the workspace root: {git.display_path(root)}", (root,)))
    parent = root.parent
    choices.append(
        (f"everything in the workspace's parent directory: {git.display_path(parent)}", (parent,))
    )

    term.out("Trust runs the plugin code found at a location, including code that arrives later")
    term.out("(for example a PR checked out with `ow add`). What do you want to trust?")
    for number, (label, _paths) in enumerate(choices, start=1):
        term.out(f"  {number}) {label}")
    default = "1" if discovered else None
    hint = ", default 1" if default else ""
    sys.stdout.write(f"Choice [1-{len(choices)}{hint}]: ")
    sys.stdout.flush()
    answer = sys.stdin.readline()
    if answer == "":
        sys.stdout.write("\n")
        raise UsageError("no choice made; nothing trusted")
    answer = answer.strip() or default or ""
    if not answer.isdigit() or not 1 <= int(answer) <= len(choices):
        raise UsageError(f"invalid choice {answer!r}; nothing trusted")
    return choices[int(answer) - 1][1]


def run(
    plugins: Plugins,
    *,
    path: str | None = None,
    workspace: str | None = None,
    untrust: bool = False,
    list_all: bool = False,
    cwd: Path | None = None,
) -> int:
    """Trust, untrust, or list plugin locations: plugin files or whole directories.

    With no PATH, `untrust` revokes the workspace's plugin files; trusting asks what to trust
    on a terminal (the plugin files, the workspace root or its parent directory) and trusts the
    plugin files found otherwise, then refreshes the workspace so they take effect.
    """
    if list_all:
        if path is not None or untrust:
            raise UsageError("--list cannot be combined with PATH or --untrust")
        _list()
        return 0

    base_cwd = (cwd or Path.cwd()).resolve()

    if path is not None:
        if workspace is not None:
            raise UsageError("pass either PATH or -w/--workspace, not both")
        target = Path(path)
        if not target.is_absolute():
            target = base_cwd / target
        if untrust:
            _untrust(target)
        else:
            _trust(target)
        return 0

    root, kind, discovered = _discover_workspace_plugins(workspace, cwd=base_cwd)

    if untrust:
        if not discovered:
            term.out("no in-repo plugins found in this workspace")
        for item in discovered:
            _untrust(item.path)
        return 0

    if term.interactive():
        locations = _choose_locations(root, discovered)
    elif discovered:
        locations = tuple(item.path for item in discovered)
    else:
        term.out("no in-repo plugins found in this workspace")
        return 0
    for location in locations:
        _trust(location)

    if not discovered:
        return 0
    if kind == "multi":
        return refresh_cmd.run_multi(root, plugins)
    session = open_session(str(root), need_workspace=True, plugins=plugins, cwd=root)
    for warning in session.warnings:
        term.warn(warning)
    return refresh_cmd.run(session)
