"""`ow trust` — trust, untrust, or list in-repo plugin files."""

from __future__ import annotations

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


def run(
    plugins: Plugins,
    *,
    path: str | None = None,
    workspace: str | None = None,
    untrust: bool = False,
    list_all: bool = False,
    cwd: Path | None = None,
) -> int:
    """Trust, untrust, or list in-repo plugin files."""
    if list_all:
        if path is not None or untrust:
            raise UsageError("--list cannot be combined with PATH or --untrust")
        for entry in trust.list_trusted():
            term.out(git.display_path(entry))
        return 0

    base_cwd = (cwd or Path.cwd()).resolve()

    if path is not None:
        if workspace is not None:
            raise UsageError("pass either PATH or -w/--workspace, not both")
        target = Path(path)
        if not target.is_absolute():
            target = base_cwd / target
        if untrust:
            resolved, removed = trust.untrust_path(target)
            verb = "untrusted" if removed else "was not trusted"
            term.out(f"{verb} {git.display_path(resolved)}")
            return 0
        resolved = trust.trust_path(target)
        term.out(f"trusted {git.display_path(resolved)}")
        return 0

    root, kind, discovered = _discover_workspace_plugins(workspace, cwd=base_cwd)
    if not discovered:
        term.out("no in-repo plugins found in this workspace")
        return 0

    for item in discovered:
        if untrust:
            resolved, removed = trust.untrust_path(item.path)
            verb = "untrusted" if removed else "was not trusted"
            term.out(f"{verb} {git.display_path(resolved)}")
        else:
            resolved = trust.trust_path(item.path)
            term.out(f"trusted {git.display_path(resolved)}")

    if untrust:
        return 0

    if kind == "multi":
        return refresh_cmd.run_multi(root, plugins)
    session = open_session(str(root), need_workspace=True, plugins=plugins, cwd=root)
    for warning in session.warnings:
        term.warn(warning)
    return refresh_cmd.run(session)
