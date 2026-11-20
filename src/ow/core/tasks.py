"""Multi-workspace task derivation, port coordination, and task-level generated files."""

from __future__ import annotations

import hashlib
import os
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import tomli_w

from ow.core import index
from ow.core.paths import task_locks_dir
from ow.core.session import open_session

if TYPE_CHECKING:
    from ow.core.plugins import GeneratedFile, Plugins
    from ow.core.workspace import Workspace


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


def load_tasks(plugins: Plugins) -> dict[str, Task]:
    """Derive every task from the workspace index (skipping missing/unreadable entries)."""
    grouped: dict[str, list[Workspace]] = {}
    for path in index.read():
        if not (path / ".ow" / "config.toml").exists():
            continue
        try:
            session = open_session(str(path), need_workspace=False, plugins=plugins)
        except Exception:
            continue
        ws = session.workspace
        if ws is None or not ws.task:
            continue
        grouped.setdefault(ws.task, []).append(ws)

    result: dict[str, Task] = {}
    for name, members in grouped.items():
        ws_tuple = tuple(members)
        result[name] = Task(
            name=name,
            workspaces=ws_tuple,
            common_parent=_common_parent(ws_tuple),
        )
    return result


def find_task(name: str, plugins: Plugins) -> Task | None:
    """Return the Task named `name`, or None when no workspace belongs to it."""
    return load_tasks(plugins).get(name)


def next_free_port(task_name: str, plugins: Plugins, *, base_port: int = 8069) -> int:
    """Return the lowest port >= `base_port` not already assigned in `task_name`."""
    task = find_task(task_name, plugins)
    if task is None:
        return base_port
    used: set[int] = set()
    for ws in task.workspaces:
        port = ws.settings.get("odoo.http_port")
        if isinstance(port, int):
            used.add(port)
    port = base_port
    while port in used:
        port += 1
    return port


def _lock_path(task_name: str) -> Path:
    digest = hashlib.sha256(task_name.encode("utf-8")).hexdigest()[:16]
    return task_locks_dir() / f"{digest}.lock.toml"


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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, delete=False, suffix=".toml"
        ) as handle:
            tmp_path = Path(handle.name)
            tomli_w.dump({"task": task_name, "files": entries}, handle)
        os.replace(tmp_path, path)
    except BaseException:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, delete=False, prefix=".ow-tmp-"
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(data)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


def sync_task_files(task: Task, plugins: Plugins) -> tuple[Path, ...]:
    """Generate and atomically write task-level files (e.g. `<task>.code-workspace`).

    Respects ownership: a file edited by the user (SHA-256 differs from lock) is never overwritten.
    """
    target_dir = task.files_dir
    if target_dir is None:
        return ()

    proposals: list[GeneratedFile] = []
    for _plugin, answer in plugins.call("ow_task_files", task=task):
        if answer:
            proposals.extend(answer)

    lock = _read_task_lock(task.name)
    written: list[Path] = []
    new_lock = dict(lock)

    for gf in proposals:
        dest = (target_dir / gf.path.as_posix()).resolve()
        key = str(dest)
        digest = _sha256(gf.data)

        if dest.exists():
            try:
                current = dest.read_bytes()
            except OSError:
                continue
            current_digest = _sha256(current)
            if current_digest == digest:
                new_lock[key] = digest
                written.append(dest)
                continue
            if key in lock and lock[key] != current_digest:
                # User edited this file: keep it untouched.
                continue
            if key not in lock:
                # Pre-existing unmanaged file: do not clobber.
                continue

        _atomic_write(dest, gf.data, gf.mode)
        new_lock[key] = digest
        written.append(dest)

    _write_task_lock(task.name, new_lock)
    return tuple(written)


def cleanup_task_files(task_name: str) -> tuple[Path, ...]:
    """Remove owned task files whose contents still match the lock when a task has no members left."""
    lock = _read_task_lock(task_name)
    removed: list[Path] = []
    remaining: dict[str, str] = {}
    for key, recorded_digest in lock.items():
        path = Path(key)
        if not path.exists():
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
