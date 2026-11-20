"""Generated file ownership, lock tracking, and atomic writes.

`.ow/rendered.lock.toml` records the SHA-256 of what ow last wrote or adopted at each path; bytes that no longer match are "yours" and ow never rewrites them.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, Sequence

import tomli_w

from ow.core.plugins import Diagnostic, GeneratedFile, Plugins
from ow.errors import OwError, PluginError

if TYPE_CHECKING:
    from ow.core.workspace import Workspace

RENDERED_LOCK = Path(".ow") / "rendered.lock.toml"

_LOCK_HEADER = "# Managed by ow.\n"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")

_RESERVED_DESTINATIONS = frozenset(
    {PurePosixPath(".ow/config.toml"), PurePosixPath(RENDERED_LOCK.as_posix())}
)


@dataclass(frozen=True)
class FileStatus:
    """One file's state compared to the lock and proposal."""

    path: PurePosixPath
    owner: str
    state: Literal["absent", "up to date", "outdated", "yours", "skipped"]
    proposal: bytes
    current: bytes | None
    reason: str | None = None  # skipped / yours detail
    mode: int = 0o644


@dataclass(frozen=True)
class Projection:
    """Inspection of generated files and the lock against a workspace."""

    files: tuple[FileStatus, ...]
    stale_lock: tuple[str, ...]
    diagnostics: tuple[tuple[str, Diagnostic], ...]
    blocked: bool


def _sha256_bytes(data: bytes) -> str:
    """Compute SHA-256 hex of bytes."""
    return hashlib.sha256(data).hexdigest()


def _as_text(data: bytes | None) -> str | None:
    """UTF-8 text, or None for bytes nobody can usefully diff."""
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _unsafe_relative(rel: PurePosixPath) -> str | None:
    """Why `rel` may never be a generated or locked destination, or None."""
    if rel.is_absolute():
        return "must be a relative path"
    parts = rel.parts
    if not parts:
        return "must not be empty"
    if any(part in ("", ".", "..") for part in parts):
        return "must not contain '.' or '..' segments"
    if rel in _RESERVED_DESTINATIONS:
        return "may not target the workspace config or the rendered lock"
    return None


def _ancestry_problem(root: Path, rel: PurePosixPath) -> str | None:
    """The first parent directory of `rel` that is unsafe to write through."""
    current = root
    for part in rel.parts[:-1]:
        current = current / part
        try:
            st = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            return f"cannot stat {current.relative_to(root).as_posix()}: {exc}"
        if stat.S_ISLNK(st.st_mode):
            return f"{current.relative_to(root).as_posix()} is a symlink"
        if not stat.S_ISDIR(st.st_mode):
            return f"{current.relative_to(root).as_posix()} is not a directory"
    return None


def read_lock(root: Path) -> dict[str, str]:
    """The rendered lock at `root`, or `{}` if it does not exist yet.

    Corrupt structure (bad TOML, a non-table, a non-hex value) raises `OwError` naming the lock path.
    """
    lock_rel = PurePosixPath(RENDERED_LOCK.as_posix())
    ancestry_problem = _ancestry_problem(root, lock_rel)
    if ancestry_problem:
        raise OwError(f"rendered lock: {ancestry_problem}")

    lock_path = root / RENDERED_LOCK
    try:
        st = lock_path.lstat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(st.st_mode):
        raise OwError(f"rendered lock at {lock_path.as_posix()} is not a regular file")

    try:
        with open(lock_path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise OwError(f"rendered lock at {lock_path.as_posix()} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise OwError(f"cannot read rendered lock at {lock_path.as_posix()}: {exc}") from exc

    if not isinstance(data, dict):
        raise OwError(f"rendered lock at {lock_path.as_posix()} must be a table")
    for name, value in data.items():
        if not isinstance(name, str) or not isinstance(value, str) or not _SHA256_HEX.fullmatch(value):
            raise OwError(f"rendered lock at {lock_path.as_posix()}: invalid entry {name!r}")
    return data


def _read_lock_or_warn(root: Path) -> tuple[dict[str, str], Diagnostic | None]:
    """The lock, or `{}` and a core warning when the lock file is malformed."""
    try:
        return read_lock(root), None
    except OwError:
        diag = Diagnostic(
            "warning",
            f"malformed lock at {RENDERED_LOCK.as_posix()}; files that differ from ow's are treated as yours",
        )
        return {}, diag


def dumps_lock(lock: dict[str, str]) -> bytes:
    """Serialize `lock` deterministically: sorted keys, the ow header, hex-validated."""
    for name, value in lock.items():
        if not isinstance(value, str) or not _SHA256_HEX.fullmatch(value):
            raise OwError(f"rendered lock: invalid hash for {name!r}")
    body = tomli_w.dumps(dict(sorted(lock.items())))
    return (_LOCK_HEADER + body).encode("utf-8")


def _write_lock_file(root: Path, lock: dict[str, str]) -> None:
    """Write the lock atomically."""
    lock_rel = PurePosixPath(RENDERED_LOCK.as_posix())
    ancestry_problem = _ancestry_problem(root, lock_rel)
    if ancestry_problem:
        raise OwError(f"rendered lock: {ancestry_problem}")

    lock_path = root / RENDERED_LOCK
    body = dumps_lock(lock)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=lock_path.parent, prefix=".rendered.lock.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        os.replace(tmp_name, lock_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _write_lock_if_changed(root: Path, lock: dict[str, str]) -> None:
    """Write the lock only when its serialized bytes differ from disk."""
    lock_path = root / RENDERED_LOCK
    try:
        old = lock_path.read_bytes()
    except OSError:
        old = b""
    if old != dumps_lock(lock):
        _write_lock_file(root, lock)


def _atomic_write(dest: Path, data: bytes, mode: int) -> None:
    """Write `data` to `dest` via a same-directory temp file and `os.replace`.

    `os.replace` swaps whatever sits at `dest`, so the destination inode is never truncated and a link there is never followed.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _destination_stat(dest: Path) -> os.stat_result | None:
    """`lstat(dest)`, or None if nothing is there — never follows a link."""
    try:
        return dest.lstat()
    except FileNotFoundError:
        return None


def _unsafe_destination(name: str, st: os.stat_result) -> str | None:
    """Why a destination is unsafe to write."""
    if stat.S_ISLNK(st.st_mode):
        return f"{name} is a symlink"
    if stat.S_ISDIR(st.st_mode):
        return f"{name} is a directory"
    if not stat.S_ISREG(st.st_mode):
        return f"{name} is not a regular file"
    return None


def project(ws: "Workspace", plugins: Plugins) -> Projection:
    """Inspect generated files and return their status against the lock.

    Raises `PluginError` on a bad path or two owners of one path; a symlinked parent marks just that file `skipped`.
    """
    # Collect per plugin, then flatten in plugin tuple order.
    answers: dict[str, list[Diagnostic]] = {}
    for plugin_name, diag_list in plugins.call("ow_diagnostics", ws):
        if not diag_list:
            continue
        if not isinstance(diag_list, list):
            raise PluginError(
                plugin_name, "ow_diagnostics", f"expected list, got {type(diag_list).__name__}"
            )
        answers[plugin_name] = list(diag_list)

    files_by_plugin: dict[str, list[GeneratedFile]] = {}
    extra_diags: dict[str, list[Diagnostic]] = {}
    owner_of: dict[str, str] = {}  # path -> owner, for the two-owner abort
    for plugin_name, contrib_list in plugins.call(
        "ow_generated_files", ws, answers=plugins.answers(ws)
    ):
        if not contrib_list:
            continue
        if not isinstance(contrib_list, list):
            raise PluginError(
                plugin_name,
                "ow_generated_files",
                f"expected list, got {type(contrib_list).__name__}",
            )
        for item in contrib_list:
            if isinstance(item, Diagnostic):
                extra_diags.setdefault(plugin_name, []).append(item)
            elif isinstance(item, GeneratedFile):
                path_key = item.path.as_posix()
                if path_key in owner_of:
                    raise PluginError(
                        f"{owner_of[path_key]} and {plugin_name}",
                        "ow_generated_files",
                        f"both produce {path_key}",
                    )
                reason = _unsafe_relative(item.path)
                if reason:
                    raise PluginError(plugin_name, "ow_generated_files", f"{path_key}: {reason}")
                owner_of[path_key] = plugin_name
                files_by_plugin.setdefault(plugin_name, []).append(item)
            else:
                raise PluginError(
                    plugin_name,
                    "ow_generated_files",
                    f"expected GeneratedFile or Diagnostic, got {type(item).__name__}",
                )

    lock, core_diag = _read_lock_or_warn(ws.root)

    # Flatten in plugin tuple order: a plugin's ow_diagnostics answers, then
    # its contribution diagnostics, then its files in the order it returned them.
    diagnostics: list[tuple[str, Diagnostic]] = []
    files: list[tuple[str, GeneratedFile]] = []
    for name in plugins.names:
        for diag in answers.get(name, ()):
            diagnostics.append((name, diag))
        for diag in extra_diags.get(name, ()):
            diagnostics.append((name, diag))
        for gf in files_by_plugin.get(name, ()):
            files.append((name, gf))
    if core_diag is not None:
        diagnostics.append(("core", core_diag))

    # Classify each file against the lock, keeping plugin tuple order.
    file_statuses: list[FileStatus] = []
    for owner, gf in files:
        path_key = gf.path.as_posix()
        dest = ws.root / gf.path

        ancestry = _ancestry_problem(ws.root, gf.path)
        if ancestry:
            file_statuses.append(
                FileStatus(
                    gf.path, owner, "skipped", gf.data, None, ancestry, gf.mode
                )
            )
            continue

        st = _destination_stat(dest)
        if st is None:
            file_statuses.append(FileStatus(gf.path, owner, "absent", gf.data, None, None, gf.mode))
            continue

        problem = _unsafe_destination(path_key, st)
        if problem:
            file_statuses.append(
                FileStatus(gf.path, owner, "yours", gf.data, None, problem, gf.mode)
            )
            continue

        try:
            current = dest.read_bytes()
        except OSError as exc:
            file_statuses.append(
                FileStatus(gf.path, owner, "yours", gf.data, None, f"cannot read: {exc}", gf.mode)
            )
            continue

        if current == gf.data:
            state = "up to date"
        elif lock.get(path_key) == _sha256_bytes(current):
            state = "outdated"
        else:
            state = "yours"
        file_statuses.append(
            FileStatus(gf.path, owner, state, gf.data, current, None, gf.mode)
        )

    stale_lock = tuple(p for p in lock if p not in owner_of)
    blocked = any(diag.level == "error" for _, diag in diagnostics)

    return Projection(
        files=tuple(file_statuses),
        stale_lock=stale_lock,
        diagnostics=tuple(diagnostics),
        blocked=blocked,
    )


def refresh(ws: "Workspace", plugins: Plugins) -> tuple[Projection, tuple[PurePosixPath, ...]]:
    """Write the generated files a projection says are missing or stale.

    A destination is re-read right before writing, so a hand edit landing since projection is left alone; a write failure raises `OwError` naming what already landed.
    """
    projection = project(ws, plugins)
    if projection.blocked:
        return projection, ()

    lock, _ = _read_lock_or_warn(ws.root)

    written: list[PurePosixPath] = []
    owned: list[PurePosixPath] = []
    new_lock = dict(lock)  # old entries survive until their path is reclassified
    failure: OwError | None = None

    for status in projection.files:
        path_key = status.path.as_posix()
        if status.state in ("absent", "outdated"):
            dest = ws.root / status.path
            # Re-read right before writing: bytes that changed since projection are
            # a hand edit, which wins. Leave it alone and keep the old lock entry,
            # so the next projection reads it as "yours".
            if _destination_stat(dest) is not None:
                try:
                    current = dest.read_bytes()
                except OSError:
                    continue
                if current != status.current:
                    continue
            try:
                _atomic_write(dest, status.proposal, status.mode)
            except OSError as exc:
                msg = f"cannot write {path_key}: {exc}"
                if written:
                    written_paths = ", ".join(p.as_posix() for p in written)
                    msg = f"{msg} (already wrote {written_paths})"
                failure = OwError(msg)
                break
            new_lock[path_key] = _sha256_bytes(status.proposal)
            written.append(status.path)
            owned.append(status.path)
        elif status.state == "up to date":
            new_lock[path_key] = _sha256_bytes(status.proposal)
            owned.append(status.path)
        # yours / skipped: keep the old entry already in new_lock, if any.

    if failure is None:
        # Completed: drop the entries no plugin produces any more.
        for path_key in projection.stale_lock:
            new_lock.pop(path_key, None)

    _write_lock_if_changed(ws.root, new_lock)
    if failure is not None:
        raise failure

    plugins.call("ow_refreshed", ws, owned=tuple(owned))
    return projection, tuple(written)


def report(projection: Projection, written: Sequence[PurePosixPath]) -> None:
    """Print the refresh summary every refreshing command shares."""
    from ow.term import error, out, warn

    for path in written:
        out(f"wrote {path.as_posix()}")

    for status in projection.files:
        if status.state == "yours":
            if status.reason and "is a directory" in status.reason:
                hint = "move it away and run `ow refresh` to get ow's version back"
            else:
                hint = "delete it and run `ow refresh` to get ow's version back"
            warn(f"{status.path.as_posix()}: not overwritten (yours); {hint}")
        elif status.state == "skipped" and status.reason:
            warn(f"{status.path.as_posix()}: skipped ({status.reason})")

    for plugin_name, diag in projection.diagnostics:
        if diag.level == "warning":
            warn(f"{plugin_name}: {diag.text}")
    for plugin_name, diag in projection.diagnostics:
        if diag.level == "error":
            error(f"{plugin_name}: {diag.text}")

    if projection.blocked:
        error("nothing was written")
