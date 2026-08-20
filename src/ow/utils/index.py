"""A discovery index of known workspaces.

Deliberately not a database. The only truth about a workspace is the
.ow/config.toml on disk; this file just remembers where to look. If it is
wrong, stale or deleted, everything still works — `ow ls` under-reports and
name lookup fails with a message, and the next successful resolution puts
the path back.
"""

import os
from pathlib import Path

from ow.utils import paths

MARKER = Path(".ow") / "config.toml"


def _is_workspace(candidate: Path) -> bool:
    return (candidate / MARKER).exists()


def _write(entries: list[Path]) -> None:
    target = paths.index_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text("".join(f"{p}\n" for p in entries))
    os.replace(tmp, target)


def known_workspaces() -> list[Path]:
    """Every remembered workspace that still exists, pruning as it reads."""
    target = paths.index_file()
    if not target.exists():
        return []

    seen: list[Path] = []
    pruned = False
    for line in target.read_text().splitlines():
        line = line.strip()
        if not line:
            pruned = True
            continue
        candidate = Path(line)
        if candidate in seen:
            pruned = True
            continue
        if not _is_workspace(candidate):
            pruned = True
            continue
        seen.append(candidate)

    if pruned:
        _write(seen)
    return seen


def remember(ws_dir: Path) -> None:
    resolved = ws_dir.resolve()
    entries = known_workspaces()
    if resolved not in entries:
        _write([*entries, resolved])


def forget(ws_dir: Path) -> None:
    resolved = ws_dir.resolve()
    entries = [p for p in known_workspaces() if p != resolved]
    _write(entries)


def find_by_name(name: str) -> list[Path]:
    return [p for p in known_workspaces() if p.name == name]
