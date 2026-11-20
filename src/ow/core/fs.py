"""Filesystem primitives: atomic writes, sidecar file locks, and path-safety checks."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import tempfile
from collections.abc import Callable, Collection, Iterator
from pathlib import Path, PurePosixPath
from typing import BinaryIO


@contextlib.contextmanager
def sidecar_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive flock on a sidecar `.lock` file next to `path`."""
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_write(
    dest: Path,
    data: bytes | Callable[[BinaryIO], None],
    *,
    mode: int = 0o644,
    prefix: str | None = None,
    suffix: str = ".tmp",
) -> None:
    """Write `data` (bytes or stream writer) to `dest` via a temp file and `os.replace`.

    Removes the temp file on any error, including `KeyboardInterrupt`.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=dest.parent,
        prefix=prefix or f".{dest.name}.",
        suffix=suffix,
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            if callable(data):
                data(handle)
            else:
                handle.write(data)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def destination_stat(dest: Path) -> os.stat_result | None:
    """`lstat(dest)`, or None when nothing is there — never follows symlinks."""
    try:
        return dest.lstat()
    except FileNotFoundError:
        return None


def unsafe_destination(name: str, st: os.stat_result) -> str | None:
    """Why a destination is unsafe to write, or None."""
    if stat.S_ISLNK(st.st_mode):
        return f"{name} is a symlink"
    if stat.S_ISDIR(st.st_mode):
        return f"{name} is a directory"
    if not stat.S_ISREG(st.st_mode):
        return f"{name} is not a regular file"
    return None


def ancestry_problem(root: Path, rel: PurePosixPath) -> str | None:
    """The first parent directory of `rel` under `root` that is unsafe to traverse."""
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


def unsafe_relative(
    rel: PurePosixPath,
    *,
    reserved: Collection[PurePosixPath] = (),
) -> str | None:
    """Why `rel` may never be a generated or locked destination, or None."""
    if rel.is_absolute():
        return "must be a relative path"
    parts = rel.parts
    if not parts:
        return "must not be empty"
    if any(part in ("", ".", "..") for part in parts):
        return "must not contain '.' or '..' segments"
    if rel in reserved:
        return "may not target the workspace config or the rendered lock"
    return None


def json_text(data: object) -> str:
    """Serialize to JSON with two-space indentation and a trailing newline."""
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
