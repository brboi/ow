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


def _still_there(candidate: Path) -> bool:
    """Does this entry still look like a workspace?

    Only a clean answer from the filesystem — the marker is not there —
    may cost an entry its place. A stat that *errors* means the index
    cannot tell: an unreadable parent, a mount that went away, a stale NFS
    handle. Path.exists() propagates exactly those rather than returning
    False, which would otherwise take down `ow ls`, `ow prune` and every
    name lookup with a traceback, and close the only recovery path. So
    ignorance keeps the entry — a mount coming back must not have cost the
    user their index.
    """
    try:
        return (candidate / MARKER).exists()
    except OSError:
        return True


def _write(entries: list[Path]) -> None:
    target = paths.index_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    # A per-process suffix avoids two concurrent writers interleaving on the
    # same temp name; with_name (not with_suffix) appends rather than
    # replacing, so it doesn't collide if the index were ever renamed to
    # something containing a dot.
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
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
        if not _still_there(candidate):
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
