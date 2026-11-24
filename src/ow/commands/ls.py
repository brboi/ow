"""`ow ls` — list indexed workspaces (mono-stack and multi-stack, tab-separated)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.core import index, stacks
from ow.core.session import open_session

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


def run(plugins: Plugins) -> int:
    """List the index's workspaces, in index order; dead entries degrade to `missing`.

    `plugins` is the built-in set: no workspace is the command's own, and each listed one
    is opened with its in-repo plugins.
    """
    for path in index.read():
        name, detail = _inspect(path, plugins)
        term.out(f"{name}\t{_display(path)}\t{detail}")
    return 0


def _inspect_single(path: Path, plugins: Plugins) -> tuple[str, str]:
    """`(name, repos)` of the workspace or stack at `path`, or why it cannot be read."""
    try:
        session = open_session(str(path), need_workspace=False, plugins=plugins)
    except Exception:
        return path.name, "unreadable"
    if session.workspace is None:
        return path.name, "unreadable"
    repos = ", ".join(
        f"{loc.name}:{loc.branch or loc.default_base or '(unset)'}" for loc in session.locations
    )
    return session.workspace.name, repos


def _inspect(path: Path, plugins: Plugins) -> tuple[str, str]:
    """`(name, detail)` of an indexed `path`: its repos or stacks, or why it cannot be read."""
    if not path.exists():
        return path.name, "missing"
    stack_paths = stacks.detect_multi_workspace(path)
    if stack_paths:
        names = ", ".join(_inspect_single(sp, plugins)[0] for sp in stack_paths)
        return path.name, f"stacks: {names}"
    if (path / ".ow" / "config.toml").exists():
        return _inspect_single(path, plugins)
    return path.name, "unreadable"


def _display(path: Path) -> str:
    """`~/…` under `$HOME`, else absolute: a listing column, so no shell quoting."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)
