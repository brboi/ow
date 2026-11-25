"""Multi-stack workspace derivation, port coordination, and workspace-level generated files."""

from __future__ import annotations

import contextlib
import re
import socket
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ow.core import config, fs, refresh
from ow.errors import UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins
    from ow.core.session import Session
    from ow.core.workspace import Workspace


def sanitize_slug(raw: str, fallback: str = "workspace") -> str:
    """Turn an arbitrary branch/bundle string into a safe single path segment."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-")
    return cleaned or fallback


def validate_name(name: str, *, kind: str = "stack") -> str:
    """Validate a workspace or stack name so it is a safe single path segment."""
    cleaned = name.strip()
    if not cleaned:
        raise UsageError(f"{kind} name cannot be empty")
    if (
        cleaned in (".", "..")
        or cleaned.startswith(".")
        or "/" in cleaned
        or "\\" in cleaned
        or ".." in cleaned
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned)
    ):
        raise UsageError(
            f"invalid {kind} name {name!r}; must be a single path segment without '/', '\\\\', '..' or leading '.'"
        )
    return cleaned


@dataclass(frozen=True, kw_only=True)
class MultiWorkspace:
    """A multi-stack workspace whose direct subdirectories are named stacks."""

    root: Path
    name: str
    stacks: tuple[Workspace, ...]


def is_stack_dir(path: Path) -> bool:
    """Whether `path` is a declared stack subdirectory of its parent multi-stack workspace."""
    try:
        resolved = path.resolve()
        parent_cfg = resolved.parent / ".ow" / "config.toml"
        if not parent_cfg.is_file():
            return False
        return resolved.name in config.stack_names_in_config(parent_cfg)
    except OSError:
        return False


def detect_multi_workspace(path: Path) -> tuple[Path, ...] | None:
    """Return sorted child stack paths if `path` is a multi-stack workspace root, else None.

    A directory is a multi-stack workspace root iff `<path>/.ow/config.toml` declares `[stacks]`
    and at least one declared stack directory exists on disk.
    """
    try:
        resolved = path.resolve()
        if not resolved.is_dir():
            return None
        names = config.stack_names_in_config(resolved / ".ow" / "config.toml")
        stacks = [
            (resolved / name).resolve()
            for name in names
            if (resolved / name).is_dir()
        ]
        return tuple(stacks) if stacks else None
    except OSError:
        return None


def parent_multi_workspace(ws_root: Path) -> Path | None:
    """Return the enclosing multi-stack workspace root when `ws_root` is a stack, else None."""
    resolved = ws_root.resolve()
    if is_stack_dir(resolved):
        return resolved.parent.resolve()
    return None


def load_multi_workspace(
    root: Path,
    plugins: Plugins,
    *,
    discover_repo_plugins: bool = True,
) -> tuple[MultiWorkspace, tuple[Session, ...]] | None:
    """Open every stack Session under multi-stack workspace `root`."""
    from dataclasses import replace

    from ow.core.session import open_session
    from ow.core.workspace import derive_repos

    stack_paths = detect_multi_workspace(root)
    if not stack_paths:
        return None
    resolved_root = root.resolve()

    repo_plugin_warnings: tuple[str, ...] = ()
    if discover_repo_plugins:
        repo_dirs = [
            (loc.name, loc.path)
            for stack_path in stack_paths
            for loc in derive_repos(stack_path)
        ]
        plugins, repo_plugin_warnings = plugins.with_repo_plugins(repo_dirs)

    sessions: list[Session] = []
    workspaces: list[Workspace] = []
    for stack_path in stack_paths:
        try:
            sess = open_session(
                str(stack_path),
                need_workspace=False,
                plugins=plugins,
                discover_repo_plugins=False,
            )
        except Exception:
            continue
        if sess.workspace is not None:
            sessions.append(sess)
            workspaces.append(sess.workspace)
    if not workspaces:
        return None
    if repo_plugin_warnings and sessions:
        sessions[0] = replace(
            sessions[0], warnings=repo_plugin_warnings + sessions[0].warnings
        )
    multi = MultiWorkspace(
        root=resolved_root,
        name=resolved_root.name,
        stacks=tuple(workspaces),
    )
    return multi, tuple(sessions)


def next_free_port(
    used: Collection[int],
    *,
    base_port: int = 8069,
) -> int:
    """Return the lowest valid TCP port >= `base_port` not in `used`."""
    start = config.as_int(base_port)
    if start is None or not 1 <= start <= 65535:
        start = 8069
    taken: set[int] = {port for p in used if (port := config.as_int(p)) is not None}
    port = start
    while port in taken:
        port += 1
        if port > 65535:
            raise UsageError(f"no free TCP port available >= {start}")
    return port


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """Whether `host:port` is currently unbound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


@contextlib.contextmanager
def workspace_flock(root: Path) -> Iterator[None]:
    """Serialize port allocation across the stacks of multi-stack root `root` with an exclusive flock."""
    with fs.sidecar_lock(root / refresh.RENDERED_LOCK):
        yield


def project_multi_workspace_files(multi: MultiWorkspace, plugins: Plugins) -> refresh.Projection:
    """Inspect the multi-stack root's generated files (e.g. `<name>.code-workspace`) against `<root>/.ow/rendered.lock.toml`."""
    # `answers` is bound to the first stack (see `CoreSpec.ow_multi_workspace_files`); with no stack there is nothing to bind and no plugin is asked.
    answered = (
        plugins.call("ow_multi_workspace_files", multi=multi, answers=plugins.answers(multi.stacks[0]))
        if multi.stacks
        else []
    )
    files_by_plugin, extra_diags = refresh.collect_contributions("ow_multi_workspace_files", answered)
    files, diagnostics = refresh.flatten_contributions(plugins, {}, files_by_plugin, extra_diags)
    stack_prefixes = tuple(f"{s.name}/" for s in multi.stacks)
    return refresh.project_files(
        multi.root,
        files,
        diagnostics,
        ignore_lock_prefixes=stack_prefixes,
    )


def sync_multi_workspace_files(multi: MultiWorkspace, plugins: Plugins) -> tuple[Path, ...]:
    """Write the multi-stack root's missing or stale generated files; return the absolute paths written.

    Same ownership rules as a workspace refresh: a hand-edited file is reported and never overwritten, and a file no plugin produces any more is left in place.
    """
    projection = project_multi_workspace_files(multi, plugins)
    written, _owned = refresh.write_files(multi.root, projection)
    refresh.report(projection, ())
    return tuple(multi.root / path for path in written)
