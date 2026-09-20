"""A discovery index of known workspaces.

Deliberately not a database. The only truth about a workspace is the
.ow/config.toml on disk; this file just remembers where to look. If it is
wrong, stale or deleted, everything still works — `ow ls` under-reports and
name lookup fails with a message.

Reading is genuinely read-only: a stale entry is filtered out in memory, and
only lifecycle operations that own the index (`ow init`/`ow render` remember,
`ow mv`/archive/rm forget, `ow prune` drops what died) ever write it. That is
what lets `ow status`, `ow ls` and shell completion run without touching a
byte of the user's state directory.
"""

import contextlib
import os
import sys
import time
from collections.abc import Iterable, Iterator
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


def _read() -> list[Path]:
    """Every entry still worth acting on, filtered in memory. Reads only.

    Blank lines, duplicates and entries whose workspace is gone are dropped
    from the result — never from the file: a read that rewrote it would make
    `ow status`, a completion callback or a dry-run mutate the user's state.
    """
    target = paths.index_file()
    if not target.exists():
        return []

    seen: list[Path] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        candidate = Path(line)
        if candidate in seen:
            continue
        if not _still_there(candidate):
            continue
        seen.append(candidate)
    return seen


def list_workspaces() -> list[Path]:
    """A read-only snapshot of the index file, dead entries included.

    For callers that must not mutate state and cannot afford to lose what
    the file says, such as shell completion and `ow prune`'s own survey.
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
    """Every remembered workspace that still exists. Never writes.

    `ow prune` is what turns the difference between this and
    `list_workspaces()` into a smaller file; a read only reports it.
    """
    return _read()


def remember(ws_dir: Path) -> None:
    """Record a workspace the user just created, moved or rendered."""
    resolved = ws_dir.resolve()
    if resolved in list_workspaces():
        return

    # Everything below writes, so re-read inside the lock: the entries read
    # above may already be stale, and writing them back is exactly how a
    # concurrent `ow init` loses its workspace.
    with _locked():
        entries = list_workspaces()
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
    if resolved not in list_workspaces():
        return

    with _locked():
        _write([entry for entry in list_workspaces() if entry != resolved])


def prune(dead: Iterable[Path]) -> None:
    """Drop exactly `dead` from the index file, under the lock.

    `ow prune` owns this: reads filter dead entries in memory, so dropping
    them for good is an explicit lifecycle action. Re-reads inside the lock
    and removes only the named entries, so a concurrent `remember` cannot
    lose its workspace to the cleanup.
    """
    doomed = set(dead)
    if not doomed:
        return

    with _locked():
        _write([entry for entry in list_workspaces() if entry not in doomed])
