"""Copy-once local seeds: the workspace files ow did not generate.

`$XDG_CONFIG_HOME/ow/local/` is an optional source of files the user wants
in every workspace. Init copies the ones a workspace does not have yet into
`<workspace>/.local/` — before addon scanning looks there — and never
replaces one that already exists. There is no interpolation and no second
render pass: a seed file is copied byte for byte, once.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_LOCAL = ".local"


@dataclass(frozen=True)
class SeedResult:
    """What one seed run did, by workspace-relative path.

    `copied` and `kept` are `.local/...` paths; `errors` names the source
    entry or destination that refused the copy.
    """

    copied: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class _SeedFile:
    relative: str
    source: Path
    mode: int


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _walk_source(root: Path, directory: Path, found: list[_SeedFile], errors: list[str]) -> None:
    """Collect `directory`'s regular files, or say what is in the way.

    lstat only: a symlink is an entry to reject, never a door to walk
    through, so a source tree cannot smuggle in anything outside itself.
    """
    try:
        entries = sorted(directory.iterdir())
    except OSError as exc:
        errors.append(f"cannot read {directory}: {exc}")
        return
    for entry in entries:
        relative = _relative(root, entry)
        try:
            st = entry.lstat()
        except OSError as exc:
            errors.append(f"cannot stat {relative} in {root}: {exc}")
            continue
        if stat.S_ISLNK(st.st_mode):
            errors.append(f"{relative} is a symlink in {root}")
        elif stat.S_ISDIR(st.st_mode):
            _walk_source(root, entry, found, errors)
        elif stat.S_ISREG(st.st_mode):
            # rwx only: a special bit on a seed has no business surviving a copy.
            found.append(_SeedFile(relative, entry, stat.S_IMODE(st.st_mode) & 0o777))
        else:
            errors.append(f"{relative} is not a regular file in {root}")


def _scan_source(source: Path) -> tuple[tuple[_SeedFile, ...], tuple[str, ...]] | None:
    """`source`'s regular files and every reason the tree is unusable.

    None when `source` does not exist: an absent local/ is simply no seeds.
    """
    try:
        st = source.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return (), (f"cannot stat seed source {source}: {exc}",)
    if stat.S_ISLNK(st.st_mode):
        return (), (f"seed source {source} is a symlink",)
    if not stat.S_ISDIR(st.st_mode):
        return (), (f"seed source {source} is not a directory",)

    found: list[_SeedFile] = []
    errors: list[str] = []
    _walk_source(source, source, found, errors)
    found.sort(key=lambda seed: seed.relative)
    return tuple(found), tuple(errors)


def _directory_problem(directory: Path, label: str) -> str | None:
    """Why `directory` may not be written through, or None when it is fine."""
    try:
        st = directory.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"cannot stat {label}: {exc}"
    if stat.S_ISLNK(st.st_mode):
        return f"{label} is a symlink"
    if not stat.S_ISDIR(st.st_mode):
        return f"{label} is not a directory"
    return None


def _ancestry_problem(root: Path, relative: str) -> str | None:
    """The first `.local` ancestor of `relative` that is unsafe to write."""
    parts = (_LOCAL, *PurePosixPath(relative).parts[:-1])
    for depth in range(1, len(parts) + 1):
        label = "/".join(parts[:depth])
        problem = _directory_problem(root / label, label)
        if problem is not None:
            return problem
    return None


def _destination_state(label: str, destination: Path) -> tuple[str | None, str | None]:
    """Whether `destination` is kept or to copy — or why neither is allowed."""
    try:
        st = destination.lstat()
    except FileNotFoundError:
        return "copy", None
    except OSError as exc:
        return None, f"cannot stat {label}: {exc}"
    if stat.S_ISLNK(st.st_mode):
        return None, f"{label} is a symlink"
    if stat.S_ISDIR(st.st_mode):
        # Directories exist only as ancestors; a file may not become one.
        return None, f"{label} is a directory"
    if not stat.S_ISREG(st.st_mode):
        return None, f"{label} is not a regular file"
    return "kept", None


def _copy_once(seed: _SeedFile, destination: Path) -> None:
    """Copy `seed`'s bytes and mode to a `destination` that is not there.

    Same-directory temp plus `os.replace`: this policy never overwrites a
    seed again, so an interrupted copy would otherwise leave a truncated
    file nothing repairs.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), seed.mode)
            handle.write(seed.source.read_bytes())
        os.replace(tmp_name, destination)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _unique(messages: list[str]) -> tuple[str, ...]:
    # One unusable ancestor serves every file under it; the user needs it once.
    return tuple(dict.fromkeys(messages))


def seed_local_files(source: Path, root: Path) -> SeedResult:
    """Copy `source`'s regular files into `root/.local`, once.

    The whole source tree is inspected before anything is written, and one
    unusable entry or destination conflict means nothing gets copied at all:
    the run has a single job, naming what to fix, and re-running it is safe
    because seeding only ever adds files that are still missing. Existing
    destination regular files are kept byte for byte, so a workspace file
    the user has touched is never overwritten.
    """
    scanned = _scan_source(source)
    if scanned is None:
        return SeedResult()
    seeds, preflight = scanned
    if preflight:
        return SeedResult((), (), _unique(list(preflight)))

    errors: list[str] = []
    kept: list[str] = []
    pending: list[tuple[_SeedFile, Path]] = []
    for seed in seeds:
        destination = root / _LOCAL / seed.relative
        label = f"{_LOCAL}/{seed.relative}"
        state: str | None = None
        problem = _ancestry_problem(root, seed.relative)
        if problem is None:
            state, problem = _destination_state(label, destination)
        if problem is not None:
            errors.append(problem)
        elif state == "kept":
            kept.append(label)
        else:
            pending.append((seed, destination))

    if errors:
        return SeedResult((), (), _unique(errors))

    copied: list[str] = []
    for seed, destination in pending:
        label = f"{_LOCAL}/{seed.relative}"
        try:
            _copy_once(seed, destination)
        except OSError as exc:
            errors.append(f"cannot copy {label}: {exc}")
            continue
        copied.append(label)

    return SeedResult(tuple(copied), tuple(kept), tuple(errors))