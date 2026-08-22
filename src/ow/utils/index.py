"""A discovery index of known workspaces.

Deliberately not a database. The only truth about a workspace is the
.ow/config.toml on disk; this file just remembers where to look. If it is
wrong, stale or deleted, everything still works — `ow ls` under-reports and
name lookup fails with a message, and the next successful resolution puts
the path back.
"""

import contextlib
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

from ow.utils import paths

MARKER = Path(".ow") / "config.toml"

# Long enough that no honest writer is still holding the lock, short enough
# that a crashed one does not make `ow ls` feel hung.
_LOCK_TIMEOUT = 5.0
_LOCK_RETRY = 0.002

_warned_readonly = False


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    """Serialise a read-modify-write of the index, best effort.

    An O_EXCL lockfile beside the index — no dependency, and the same
    directory _write already writes its temp file into. Best effort in both
    directions: if the lock cannot be taken at all (a read-only directory,
    a filesystem without O_EXCL semantics) or is still held after the
    timeout (a writer that crashed before unlinking), carry on unlocked.
    Losing an entry to a race is bad; refusing to run `ow ls` because of a
    stale lockfile would be worse, and this file self-heals either way.
    """
    target = paths.index_file()
    lock = target.with_name(f"{target.name}.lock")
    fd = None
    deadline = time.monotonic() + _LOCK_TIMEOUT
    while True:
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if time.monotonic() >= deadline:
                # The lock outlived the timeout: a previous writer crashed.
                # Break it so every later call does not pay the full timeout.
                print(f"Breaking stale lock: {lock}", file=sys.stderr)
                with contextlib.suppress(OSError):
                    lock.unlink()
                break
            time.sleep(_LOCK_RETRY)
            continue
        except OSError:
            break
        break

    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
            with contextlib.suppress(OSError):
                lock.unlink()


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
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # A per-process suffix avoids two concurrent writers interleaving on the
        # same temp name; with_name (not with_suffix) appends rather than
        # replacing, so it doesn't collide if the index were ever renamed to
        # something containing a dot.
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        tmp.write_text("".join(f"{p}\n" for p in entries), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        global _warned_readonly
        if not _warned_readonly:
            _warned_readonly = True
            print(
                f"Warning: cannot write workspace index ({exc}); continuing without cache.",
                file=sys.stderr,
            )


def _read() -> tuple[list[Path], bool]:
    """The entries worth keeping, and whether the file still says otherwise."""
    target = paths.index_file()
    if not target.exists():
        return [], False

    seen: list[Path] = []
    pruned = False
    for line in target.read_text(encoding="utf-8").splitlines():
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
    return seen, pruned


def list_workspaces() -> list[Path]:
    """A read-only snapshot of the index file.

    For callers that must not mutate state, such as shell completion.
    """
    target = paths.index_file()
    if not target.exists():
        return []
    seen: list[Path] = []
    for line in target.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        candidate = Path(line)
        if candidate in seen:
            continue
        seen.append(candidate)
    return seen


def known_workspaces() -> list[Path]:
    """Every remembered workspace that still exists, pruning as it reads."""
    seen, pruned = _read()
    if not pruned:
        # The overwhelmingly common case, and the one that must stay cheap:
        # no write, so no lock, so `ow ls` never waits on anyone.
        return seen

    with _locked():
        seen, pruned = _read()
        if pruned:
            _write(seen)
    return seen


def remember(ws_dir: Path) -> None:
    resolved = ws_dir.resolve()
    entries, pruned = _read()
    if resolved in entries and not pruned:
        return

    # Everything below writes, so re-read inside the lock: the entries read
    # above may already be stale, and writing them back is exactly how a
    # concurrent `ow init` loses its workspace.
    with _locked():
        entries, _ = _read()
        if resolved not in entries:
            entries = [*entries, resolved]
        _write(entries)


def find_by_name(name: str) -> list[Path]:
    return [p for p in known_workspaces() if p.name == name]

def forget(ws_dir: Path) -> None:
    """Remove a workspace from the discovery index.

    The inverse of remember(). Does not touch the workspace directory itself
    or any bare repo — those are the caller's responsibility. A workspace not
    in the index is a no-op, not an error.
    """
    resolved = ws_dir.resolve()
    entries, pruned = _read()
    if resolved not in entries and not pruned:
        return

    with _locked():
        entries, _ = _read()
        entries = [e for e in entries if e != resolved]
        _write(entries)
