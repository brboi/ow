"""Byte ownership, ignore rules and safe atomic writes for generated output.

`.ow/rendered.lock.toml` records the SHA-256 of the bytes ow last wrote or
adopted at each output path — not the bytes of the source that produced
them. Locking the output rather than the generator is what lets a hand
edit survive a generator change: a file whose bytes no longer match the
lock is yours, and ow never writes it again. `plan_files` is read-only
inspection; `write_files` is the only place a byte is ever written, and it
refuses to write anything at all when the plan carries an error.
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
from typing import TYPE_CHECKING

import pathspec
import tomli_w

if TYPE_CHECKING:
    from ow.utils.generate import GeneratedFile

RENDERED_LOCK = Path(".ow") / "rendered.lock.toml"

_LOCK_HEADER = "# Managed by ow.\n"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")

_RESERVED_DESTINATIONS = frozenset(
    {PurePosixPath(".ow/config.toml"), PurePosixPath(RENDERED_LOCK.as_posix())}
)

ABSENT = "absent"
UP_TO_DATE = "up to date"
OUTDATED = "outdated"
YOURS = "yours"
NOT_RENDERED = "not rendered"
IGNORED = "ignored"


@dataclass(frozen=True)
class RenderedFile:
    """One workspace path and how its current bytes compare to a proposal."""

    path: str
    state: str
    ow_text: str | None
    your_text: str | None


@dataclass(frozen=True)
class RenderPlan:
    """A read-only inspection of `outputs` and the lock against `root`.

    `outputs` is the subset of the proposal that is nonignored, safety
    checked and has data to write — exactly what `write_files` may act on.
    `states` is every path worth showing, including ignored and retired
    ones that `write_files` never touches. `gate_blockers` starts empty:
    render.py knows nothing about legacy mise shadowing or other
    cross-cutting diagnostics; a caller composes those in with
    `dataclasses.replace`.
    """

    root: Path
    outputs: tuple["GeneratedFile", ...]
    states: tuple[RenderedFile, ...]
    lock: dict[str, str]
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    gate_blockers: tuple[str, ...] = ()

    @property
    def file_gate_failed(self) -> bool:
        return (
            bool(self.errors)
            or bool(self.gate_blockers)
            or any(rendered.state in {YOURS, OUTDATED, ABSENT} for rendered in self.states)
        )


@dataclass(frozen=True)
class RenderResult:
    """What `write_files` did, by workspace-relative path."""

    wrote: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    adopted: tuple[str, ...] = ()
    yours: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def failed(self) -> bool:
        return bool(self.errors)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _as_text(data: bytes | None) -> str | None:
    """utf-8 text, or None for bytes nobody can usefully diff."""
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

    Corrupt structure (bad TOML, a non-table, a non-hex value) is a
    `ValueError` naming the lock path — never silently reset to empty,
    since an empty lock would strip every existing output of the
    protection its lock entry gives it. An unreadable lock raises
    `OSError` naming the path, so a caller reports it rather than guessing.
    """
    lock_rel = PurePosixPath(RENDERED_LOCK.as_posix())
    ancestry_problem = _ancestry_problem(root, lock_rel)
    if ancestry_problem:
        raise ValueError(f"rendered lock: {ancestry_problem}")

    lock_path = root / RENDERED_LOCK
    try:
        st = lock_path.lstat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"rendered lock at {lock_path.as_posix()} is not a regular file")

    try:
        with open(lock_path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"rendered lock at {lock_path.as_posix()} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise OSError(f"cannot read rendered lock at {lock_path.as_posix()}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"rendered lock at {lock_path.as_posix()} must be a table")
    for name, value in data.items():
        if not isinstance(name, str) or not isinstance(value, str) or not _SHA256_HEX.fullmatch(value):
            raise ValueError(f"rendered lock at {lock_path.as_posix()}: invalid entry {name!r}")
    return data


def dumps_lock(lock: dict[str, str]) -> bytes:
    """Serialize `lock` deterministically: sorted keys, the ow header, hex-validated."""
    for name, value in lock.items():
        if not isinstance(value, str) or not _SHA256_HEX.fullmatch(value):
            raise ValueError(f"rendered lock: invalid hash for {name!r}")
    body = tomli_w.dumps(dict(sorted(lock.items())))
    return (_LOCK_HEADER + body).encode("utf-8")


def _write_lock_file(root: Path, lock: dict[str, str]) -> None:
    lock_rel = PurePosixPath(RENDERED_LOCK.as_posix())
    ancestry_problem = _ancestry_problem(root, lock_rel)
    if ancestry_problem:
        raise ValueError(f"rendered lock: {ancestry_problem}")

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


def _atomic_write(dest: Path, data: bytes, mode: int) -> None:
    """Write `data` to `dest` via a same-directory temp file and `os.replace`.

    Never truncates the destination inode and never follows a link at
    `dest`: `os.replace` swaps whatever sits at that path in one step.
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
    if stat.S_ISLNK(st.st_mode):
        return f"{name} is a symlink"
    if stat.S_ISDIR(st.st_mode):
        return f"{name} is a directory"
    if not stat.S_ISREG(st.st_mode):
        return f"{name} is not a regular file"
    return None


def _read_destination(name: str, dest: Path) -> tuple[bytes | None, str | None]:
    """`dest`'s bytes, or the error `name` should be planned with."""
    try:
        return dest.read_bytes(), None
    except OSError as exc:
        return None, f"cannot read {name}: {exc}"


def plan_files(
    root: Path, outputs: tuple["GeneratedFile", ...], ignore: tuple[str, ...]
) -> RenderPlan:
    """Inspect `outputs` and the lock against `root`. Reads; never writes.

    `outputs` is the caller's complete fixed proposal — `data=None` marks
    a path no generator wants in this context, which is handled exactly
    like a lock entry no current proposal produces any more. Every path
    problem, and every unreadable lock or destination, becomes an entry
    in `errors`; nothing here raises.
    """
    try:
        lock = read_lock(root)
    except (OSError, ValueError) as exc:
        return RenderPlan(root=root, outputs=(), states=(), lock={}, errors=(str(exc),))

    errors: list[str] = []
    spec = pathspec.GitIgnoreSpec.from_lines(ignore)

    proposed: dict[str, "GeneratedFile"] = {gf.path.as_posix(): gf for gf in outputs}
    names = sorted(set(proposed) | set(lock))

    states: list[RenderedFile] = []
    safe_outputs: list["GeneratedFile"] = []

    for name in names:
        gf = proposed.get(name)

        if spec.match_file(name):
            if gf is not None or name in lock:
                ow_text = _as_text(gf.data) if gf is not None else None
                states.append(RenderedFile(name, IGNORED, ow_text, None))
            continue

        rel = PurePosixPath(name)
        reason = _unsafe_relative(rel)
        if reason:
            errors.append(f"{name}: {reason}")
            continue
        ancestry_problem = _ancestry_problem(root, rel)
        if ancestry_problem:
            errors.append(f"{name}: {ancestry_problem}")
            continue

        dest = root / rel
        st = _destination_stat(dest)
        if st is not None:
            problem = _unsafe_destination(name, st)
            if problem:
                errors.append(problem)
                continue

        if gf is None or gf.data is None:
            if name in lock and st is not None:
                current, read_error = _read_destination(name, dest)
                if read_error:
                    errors.append(read_error)
                    continue
                states.append(RenderedFile(name, NOT_RENDERED, None, _as_text(current)))
            continue

        safe_outputs.append(gf)
        if st is None:
            states.append(RenderedFile(name, ABSENT, _as_text(gf.data), None))
            continue

        current, read_error = _read_destination(name, dest)
        if read_error:
            errors.append(read_error)
            continue
        if current == gf.data:
            states.append(RenderedFile(name, UP_TO_DATE, _as_text(gf.data), _as_text(current)))
        elif lock.get(name) == _sha256_bytes(current):
            states.append(RenderedFile(name, OUTDATED, _as_text(gf.data), _as_text(current)))
        else:
            states.append(RenderedFile(name, YOURS, _as_text(gf.data), _as_text(current)))

    return RenderPlan(
        root=root,
        outputs=tuple(safe_outputs),
        states=tuple(states),
        lock=lock,
        errors=tuple(errors),
    )


def write_files(plan: RenderPlan) -> RenderResult:
    """Write what `plan` owns. Refuses outright if `plan.errors` is nonempty.

    Every planned write is rechecked against the live filesystem and the
    lock right before it happens: content that changed since `plan_files`
    ran becomes `yours` instead of being overwritten. A write that fails
    is recorded as an error and skipped; every write that already
    succeeded, and its lock entry, survive that failure.
    """
    if plan.errors:
        return RenderResult(errors=plan.errors, warnings=plan.warnings)

    lock = dict(plan.lock)
    wrote: list[str] = []
    updated: list[str] = []
    adopted: list[str] = []
    yours: list[str] = []
    errors: list[str] = []

    ignored = tuple(sorted(r.path for r in plan.states if r.state == IGNORED))
    skipped = tuple(sorted(r.path for r in plan.states if r.state == NOT_RENDERED))

    for gf in sorted(plan.outputs, key=lambda g: g.path.as_posix()):
        assert gf.data is not None
        name = gf.path.as_posix()
        rel = gf.path

        ancestry_problem = _ancestry_problem(plan.root, rel)
        if ancestry_problem:
            errors.append(f"{name}: {ancestry_problem}")
            continue

        dest = plan.root / rel
        st = _destination_stat(dest)
        if st is not None:
            problem = _unsafe_destination(name, st)
            if problem:
                errors.append(problem)
                continue

        try:
            if st is None:
                _atomic_write(dest, gf.data, gf.mode)
                lock[name] = _sha256_bytes(gf.data)
                wrote.append(name)
                continue

            current = dest.read_bytes()
            if current == gf.data:
                lock[name] = _sha256_bytes(current)
                adopted.append(name)
            elif lock.get(name) == _sha256_bytes(current):
                _atomic_write(dest, gf.data, gf.mode)
                lock[name] = _sha256_bytes(gf.data)
                updated.append(name)
            else:
                yours.append(name)
        except OSError as exc:
            errors.append(f"{name}: cannot write: {exc}")
            continue

    try:
        _write_lock_file(plan.root, lock)
    except (OSError, ValueError) as exc:
        errors.append(f"cannot write rendered lock: {exc}")

    return RenderResult(
        wrote=tuple(sorted(wrote)),
        updated=tuple(sorted(updated)),
        adopted=tuple(sorted(adopted)),
        yours=tuple(sorted(yours)),
        ignored=ignored,
        skipped=skipped,
        errors=tuple(errors),
        warnings=plan.warnings,
    )
