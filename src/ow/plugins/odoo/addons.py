"""`addons_path` discovery for an Odoo workspace."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ow.core.workspace import Repo, Workspace


def _is_dir(path: Path) -> bool:
    """`path.is_dir()`, with unreadable paths simply not directories."""
    try:
        return path.is_dir()
    except OSError:
        return False


def _has_manifest(path: Path) -> bool:
    """`path` is a directory holding an addon manifest, unreadable ones excluded."""
    if not _is_dir(path):
        return False
    for name in ("__manifest__.py", "__openerp__.py"):
        try:
            if (path / name).is_file():
                return True
        except OSError:
            return False
    return False


def _find_addon_paths(
    path: Path, exclude: set[Path] | None = None
) -> list[Path]:
    """Addons_path directories under `path`, tolerating unreadable ones.

    An addons_path is a directory whose immediate children include an addon (a
    directory holding `__manifest__.py`/`__openerp__.py`); the walk stops
    descending there. Hidden, `node_modules` and `__pycache__` directories are
    never entered, nor are the `exclude` trees the caller handles itself.
    """
    exclude = set() if exclude is None else exclude
    if not _is_dir(path):
        return []

    result: list[Path] = []
    seen: set[int] = set()
    stack = [path]

    while stack:
        current = stack.pop()
        try:
            st = current.stat()
        except OSError:
            continue
        if st.st_ino in seen:
            continue
        seen.add(st.st_ino)

        try:
            if current.resolve() in exclude:
                continue
        except OSError:
            pass

        try:
            children = list(current.iterdir())
        except OSError:
            continue

        if any(_has_manifest(child) for child in children):
            result.append(current)
            continue

        for child in children:
            if not _is_dir(child):
                continue
            if child.name.startswith(".") or child.name in ("node_modules", "__pycache__"):
                continue
            stack.append(child)

    return sorted(result)


def workspace_addon_paths(ws: Workspace, core: Repo) -> list[Path]:
    """Every addons_path this workspace exposes, in Odoo's resolution order.

    `.local` and loose root addons come first: they exist to shadow the module
    they replace. Then each non-core repo's own addons_paths in workspace order,
    then the core's two fixed directories. Deduplicated without reordering.
    """
    repo_paths = {repo.path for repo in ws.repos}
    candidates: list[Path] = list(_find_addon_paths(ws.root / ".local"))
    candidates += _find_addon_paths(ws.root, exclude=repo_paths)
    for repo in ws.repos:
        if repo.path == core.path:
            continue
        candidates += _find_addon_paths(repo.path)
    candidates.append(core.path / "addons")
    candidates.append(core.path / "odoo" / "addons")

    seen: set[Path] = set()
    result: list[Path] = []
    for path in candidates:
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result
