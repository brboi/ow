"""Workspace index file operations."""

from __future__ import annotations

from pathlib import Path

from ow.core.fs import atomic_write, sidecar_lock
from ow.core.paths import index_file


def _normalize(path: Path | str) -> Path:
    """Normalize an index path consistently across symlinks (e.g. `/tmp` -> `/private/tmp`)."""
    return Path(path).expanduser().resolve()


def read() -> tuple[Path, ...]:
    """Read the index, returning resolved paths in order with duplicates removed."""
    index_path = index_file()
    if not index_path.exists():
        return ()

    seen: set[str] = set()
    result: list[Path] = []
    for line in index_path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        path = _normalize(line)
        key = str(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return tuple(result)


def _write(entries: list[Path]) -> None:
    """Replace the index atomically with `entries`."""
    payload = "".join(f"{entry}\n" for entry in entries).encode("utf-8")
    atomic_write(index_file(), payload, prefix=".tmp-", suffix=".workspaces")


def add(path: Path) -> None:
    """Add a workspace path; no-op when already present."""
    resolved = _normalize(path)
    with sidecar_lock(index_file()):
        current = read()
        if resolved in current:
            return
        _write([*current, resolved])


def remove(path: Path) -> None:
    """Remove a workspace path; no-op when absent."""
    resolved = _normalize(path)
    with sidecar_lock(index_file()):
        current = read()
        if resolved not in current:
            return
        _write([entry for entry in current if entry != resolved])
