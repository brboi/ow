"""Workspace index file operations."""

from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ow.core.paths import index_file


def read() -> tuple[Path, ...]:
    """Read the index, returning absolute paths in order with duplicates removed."""
    index_path = index_file()
    if not index_path.exists():
        return ()

    seen: set[str] = set()
    result: list[Path] = []
    for line in index_path.read_text().split("\n"):
        if not line.strip():
            continue
        path = Path(os.path.abspath(line))
        if str(path) not in seen:
            seen.add(str(path))
            result.append(path)
    return tuple(result)


@contextmanager
def _locked() -> Iterator[None]:
    """Hold an exclusive lock on the index's sidecar lock file."""
    index_path = index_file()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with open(index_path.with_name(index_path.name + ".lock"), "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _write(entries: list[Path]) -> None:
    """Replace the index atomically with `entries`."""
    index_path = index_file()
    temp_fd, temp_path = tempfile.mkstemp(
        dir=index_path.parent, prefix=".tmp-", suffix=".workspaces"
    )
    try:
        with os.fdopen(temp_fd, "w") as f:
            for entry in entries:
                f.write(f"{entry}\n")
        os.replace(temp_path, index_path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def add(path: Path) -> None:
    """Add a workspace path; no-op when already present."""
    abs_path = Path(os.path.abspath(path))
    with _locked():
        current = read()
        if abs_path in current:
            return
        _write([*current, abs_path])


def remove(path: Path) -> None:
    """Remove a workspace path; no-op when absent."""
    abs_path = Path(os.path.abspath(path))
    with _locked():
        current = read()
        if abs_path not in current:
            return
        _write([entry for entry in current if entry != abs_path])
