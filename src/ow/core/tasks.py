"""Multi-workspace task derivation, port coordination, and task-level generated files."""

from __future__ import annotations

import contextlib
import hashlib
import tomllib
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import tomli_w

from ow import term
from ow.core import fs, index
from ow.core.paths import task_locks_dir
from ow.core.session import Session, open_session
from ow.errors import PluginError, UsageError

if TYPE_CHECKING:
    from ow.core.plugins import GeneratedFile, Plugins
    from ow.core.workspace import Workspace


def validate_name(name: str, *, kind: str = "task") -> str:
    """Validate a workspace or task name so it is a safe single path segment."""
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


@dataclass(frozen=True)
class Task:
    """A logical group of flat workspaces belonging to the same task."""

    name: str
    workspaces: tuple[Workspace, ...]
    common_parent: Path | None  # shared direct parent directory when all members live side-by-side

    @property
    def files_dir(self) -> Path | None:
        """Directory where task-level generated files (e.g. `<task>.code-workspace`) live."""
        if self.common_parent is not None:
            return self.common_parent
        if self.workspaces:
            return self.workspaces[0].root.parent
        return None


def _common_parent(workspaces: tuple[Workspace, ...]) -> Path | None:
    """Return the shared direct parent directory if every workspace has the same parent."""
    if not workspaces:
        return None
    parents = {ws.root.parent.resolve() for ws in workspaces}
    if len(parents) == 1:
        return next(iter(parents))
    return None


def read_task_tag(path: Path) -> str | None:
    """Cheaply read `[workspace].task` from `.ow/config.toml` without opening a full Session."""
    cfg = path / ".ow" / "config.toml"
    if not cfg.is_file():
        return None
    try:
        with open(cfg, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    ws_table = data.get("workspace")
    if not isinstance(ws_table, dict):
        return None
    task_val = ws_table.get("task")
    if isinstance(task_val, str) and task_val.strip():
        return task_val.strip()
    return None


def _load_grouped(
    plugins: Plugins, *, only_task: str | None = None
) -> dict[str, list[Session]]:
    grouped: dict[str, list[Session]] = {}
    for path in index.read():
        tag = read_task_tag(path)
        if tag is None:
            continue
        if only_task is not None and tag != only_task:
            continue
        try:
            session = open_session(str(path), need_workspace=False, plugins=plugins)
        except Exception:
            continue
        ws = session.workspace
        if ws is None or not ws.task:
            continue
        grouped.setdefault(ws.task, []).append(session)
    return grouped


def load_tasks(plugins: Plugins, *, only_task: str | None = None) -> dict[str, Task]:
    """Derive tasks from the workspace index, skipping workspaces with no matching task tag."""
    grouped = _load_grouped(plugins, only_task=only_task)
    result: dict[str, Task] = {}
    for name, sessions in grouped.items():
        ws_tuple = tuple(s.workspace for s in sessions if s.workspace is not None)
        result[name] = Task(
            name=name,
            workspaces=ws_tuple,
            common_parent=_common_parent(ws_tuple),
        )
    return result


def find_task(name: str, plugins: Plugins) -> Task | None:
    """Return the Task named `name`, or None when no workspace belongs to it."""
    return load_tasks(plugins, only_task=name).get(name)


def load_task_sessions(name: str, plugins: Plugins) -> tuple[Task, tuple[Session, ...]] | None:
    """Return `(Task, sessions)` for `name` without opening every workspace Session twice."""
    sessions = _load_grouped(plugins, only_task=name).get(name)
    if not sessions:
        return None
    ws_tuple = tuple(s.workspace for s in sessions if s.workspace is not None)
    task = Task(name=name, workspaces=ws_tuple, common_parent=_common_parent(ws_tuple))
    return task, tuple(sessions)


def next_free_port(
    task_name: str,
    plugins: Plugins,
    *,
    base_port: int = 8069,
    extra_used: Collection[int] = (),
) -> int:
    """Return the lowest valid port >= `base_port` not already assigned in `task_name`."""
    if not isinstance(base_port, int) or isinstance(base_port, bool) or not 1 <= base_port <= 65535:
        base_port = 8069
    used: set[int] = {p for p in extra_used if isinstance(p, int) and not isinstance(p, bool)}
    task = find_task(task_name, plugins)
    if task is not None:
        for ws in task.workspaces:
            for key in ("odoo.http_port", "odoo.gevent_port"):
                port = ws.settings.get(key)
                if isinstance(port, int) and not isinstance(port, bool):
                    used.add(port)
    port = base_port
    while port in used:
        port += 1
        if port > 65535:
            raise UsageError(f"no free TCP port available >= {base_port} for task {task_name!r}")
    return port


def _lock_path(task_name: str) -> Path:
    digest = hashlib.sha256(task_name.encode("utf-8")).hexdigest()[:16]
    return task_locks_dir() / f"{digest}.lock.toml"


@contextlib.contextmanager
def task_flock(task_name: str) -> Iterator[None]:
    """Serialize port allocation and task-file writes for `task_name` with an exclusive flock."""
    with fs.sidecar_lock(_lock_path(task_name)):
        yield


def _read_task_lock(task_name: str) -> dict[str, str]:
    path = _lock_path(task_name)
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    files = data.get("files")
    if not isinstance(files, dict):
        return {}
    return {str(k): str(v) for k, v in files.items() if isinstance(v, str)}


def _write_task_lock(task_name: str, entries: dict[str, str]) -> None:
    path = _lock_path(task_name)
    if not entries:
        path.unlink(missing_ok=True)
        return
    payload = tomli_w.dumps({"task": task_name, "files": entries}).encode("utf-8")
    fs.atomic_write(path, payload, suffix=".toml")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def task_owned_files(task_name: str) -> tuple[Path, ...]:
    """Return existing task-level generated files recorded in `task_name`'s lock."""
    return tuple(Path(key) for key in _read_task_lock(task_name) if Path(key).exists())


def sync_task_files(task: Task, plugins: Plugins) -> tuple[Path, ...]:
    """Generate and atomically write task-level files (e.g. `<task>.code-workspace`).

    Respects ownership: a file edited by the user (SHA-256 differs from lock) is never overwritten.
    Validates relative paths and symlinks, serializes updates via flock, and prunes stale owned files.
    """
    target_dir = task.files_dir
    if target_dir is None:
        return ()

    proposals: list[tuple[str, GeneratedFile]] = []
    owner_of: dict[str, str] = {}
    for plugin_name, answer in plugins.call("ow_task_files", task=task):
        if not answer:
            continue
        for gf in answer:
            path_key = gf.path.as_posix()
            reason = fs.unsafe_relative(gf.path)
            if reason:
                raise PluginError(plugin_name, "ow_task_files", f"{path_key}: {reason}")
            if path_key in owner_of:
                raise PluginError(
                    f"{owner_of[path_key]} and {plugin_name}",
                    "ow_task_files",
                    f"both produce {path_key}",
                )
            owner_of[path_key] = plugin_name
            proposals.append((plugin_name, gf))

    with task_flock(task.name):
        lock = _read_task_lock(task.name)
        written: list[Path] = []
        new_lock: dict[str, str] = {}
        proposed_keys: set[str] = set()

        for _plugin_name, gf in proposals:
            ancestry = fs.ancestry_problem(target_dir, gf.path)
            if ancestry:
                term.warn(f"{gf.path.as_posix()}: skipped ({ancestry})")
                continue
            dest = target_dir / gf.path
            key = str(dest)
            proposed_keys.add(key)
            digest = _sha256(gf.data)

            st = fs.destination_stat(dest)
            if st is not None:
                problem = fs.unsafe_destination(gf.path.as_posix(), st)
                if problem:
                    term.warn(f"{dest}: skipped ({problem})")
                    if key in lock:
                        new_lock[key] = lock[key]
                    continue
                try:
                    current = dest.read_bytes()
                except OSError:
                    if key in lock:
                        new_lock[key] = lock[key]
                    continue
                current_digest = _sha256(current)
                if current_digest == digest:
                    new_lock[key] = digest
                    continue
                if key in lock and lock[key] != current_digest:
                    # User edited this file: keep it untouched and keep old lock entry.
                    term.warn(f"{dest}: not overwritten (yours)")
                    new_lock[key] = lock[key]
                    continue
                if key not in lock:
                    # Pre-existing unmanaged file: do not clobber.
                    term.warn(f"{dest}: not overwritten (yours)")
                    continue

            fs.atomic_write(dest, gf.data, mode=gf.mode, prefix=".ow-tmp-")
            new_lock[key] = digest
            written.append(dest)

        # Clean up previously owned files that are no longer proposed (e.g. files_dir moved).
        for old_key, recorded_digest in lock.items():
            if old_key in proposed_keys:
                continue
            old_path = Path(old_key)
            st = fs.destination_stat(old_path)
            if st is None:
                continue
            if fs.unsafe_destination(old_path.name, st):
                continue
            try:
                if _sha256(old_path.read_bytes()) == recorded_digest:
                    old_path.unlink()
            except OSError:
                new_lock[old_key] = recorded_digest

        _write_task_lock(task.name, new_lock)
        return tuple(written)


def cleanup_task_files(task_name: str) -> tuple[Path, ...]:
    """Remove owned task files whose contents still match the lock when a task has no members left."""
    with task_flock(task_name):
        lock = _read_task_lock(task_name)
        removed: list[Path] = []
        remaining: dict[str, str] = {}
        for key, recorded_digest in lock.items():
            path = Path(key)
            st = fs.destination_stat(path)
            if st is None:
                continue
            if fs.unsafe_destination(path.name, st):
                continue
            try:
                current_digest = _sha256(path.read_bytes())
            except OSError:
                remaining[key] = recorded_digest
                continue
            if current_digest == recorded_digest:
                try:
                    path.unlink()
                    removed.append(path)
                except OSError:
                    remaining[key] = recorded_digest
            else:
                # User modified the file: keep it and stop owning it.
                pass
        _write_task_lock(task_name, remaining)
        return tuple(removed)
