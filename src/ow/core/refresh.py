"""Generated file ownership, lock tracking, and atomic writes, for any root (a workspace or a multi-stack root).

`.ow/rendered.lock.toml` records the SHA-256 of what ow last wrote or adopted at each path; bytes that no longer match are "yours" and ow never rewrites them. Refresh never deletes a generated file: one no plugin produces any more just loses its lock entry. The only deletion is `remove_owned_files`, when ow removes the root itself.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import stat
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

import tomli_w

from ow.core import fs
from ow.core.plugins import Diagnostic, GeneratedFile, Plugins
from ow.errors import OwError, PluginError

if TYPE_CHECKING:
    from ow.core.session import Session
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


def _unsafe_relative(rel: PurePosixPath) -> str | None:
    """Why `rel` may never be a generated or locked destination, or None."""
    return fs.unsafe_relative(rel, reserved=_RESERVED_DESTINATIONS)


def read_lock(root: Path) -> dict[str, str]:
    """The rendered lock at `root`, or `{}` if it does not exist yet.

    Corrupt structure (bad TOML, a non-table, a non-hex value) raises `OwError` naming the lock path.
    """
    lock_rel = PurePosixPath(RENDERED_LOCK.as_posix())
    ancestry_problem = fs.ancestry_problem(root, lock_rel)
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
        diag = Diagnostic(level="warning", text=f"malformed lock at {RENDERED_LOCK.as_posix()}; files that differ from ow's are treated as yours")
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
    ancestry_problem = fs.ancestry_problem(root, lock_rel)
    if ancestry_problem:
        raise OwError(f"rendered lock: {ancestry_problem}")

    lock_path = root / RENDERED_LOCK
    st = fs.destination_stat(lock_path)
    if st is not None:
        problem = fs.unsafe_destination(RENDERED_LOCK.as_posix(), st)
        if problem:
            raise OwError(f"rendered lock: {problem}")

    body = dumps_lock(lock)
    fs.atomic_write(lock_path, body, mode=0o644, prefix=".rendered.lock.", suffix=".tmp")


def _write_lock_if_changed(root: Path, lock: dict[str, str]) -> None:
    """Write the lock only when its serialized bytes differ from disk."""
    lock_path = root / RENDERED_LOCK
    try:
        old = lock_path.read_bytes()
    except OSError:
        old = b""
    if old != dumps_lock(lock):
        _write_lock_file(root, lock)


def classify_proposals(
    root: Path,
    files: Sequence[tuple[str, GeneratedFile]],
    lock: Mapping[str, str],
) -> tuple[FileStatus, ...]:
    """Classify proposed `(owner, GeneratedFile)` entries under `root` against `lock`."""
    file_statuses: list[FileStatus] = []
    for owner, gf in files:
        path_key = gf.path.as_posix()
        dest = root / gf.path

        ancestry = fs.ancestry_problem(root, gf.path)
        if ancestry:
            file_statuses.append(
                FileStatus(gf.path, owner, "skipped", gf.data, None, ancestry, gf.mode)
            )
            continue

        st = fs.destination_stat(dest)
        if st is None:
            file_statuses.append(FileStatus(gf.path, owner, "absent", gf.data, None, None, gf.mode))
            continue

        problem = fs.unsafe_destination(path_key, st)
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
            state: Literal["absent", "up to date", "outdated", "yours", "skipped"] = "up to date"
        elif lock.get(path_key) == _sha256_bytes(current):
            state = "outdated"
        else:
            state = "yours"
        file_statuses.append(
            FileStatus(gf.path, owner, state, gf.data, current, None, gf.mode)
        )
    return tuple(file_statuses)


def collect_contributions(
    hook: str, contributions: Iterable[tuple[str, object]]
) -> tuple[dict[str, list[GeneratedFile]], dict[str, list[Diagnostic]]]:
    """Validate each plugin's answer to `hook`: its `GeneratedFile`s and `Diagnostic`s, keyed by plugin.

    Raises `PluginError` on a non-list answer, a bad or reserved path, or two plugins producing one path.
    """
    files_by_plugin: dict[str, list[GeneratedFile]] = {}
    extra_diags: dict[str, list[Diagnostic]] = {}
    owner_of: dict[str, str] = {}  # path -> owner, for the two-owner abort
    for plugin_name, contrib_list in contributions:
        if not contrib_list:
            continue
        if not isinstance(contrib_list, list):
            raise PluginError(
                plugin_name, hook, f"expected list, got {type(contrib_list).__name__}"
            )
        for item in contrib_list:
            if isinstance(item, Diagnostic):
                extra_diags.setdefault(plugin_name, []).append(item)
            elif isinstance(item, GeneratedFile):
                path_key = item.path.as_posix()
                if path_key in owner_of:
                    raise PluginError(
                        f"{owner_of[path_key]} and {plugin_name}", hook, f"both produce {path_key}"
                    )
                reason = _unsafe_relative(item.path)
                if reason:
                    raise PluginError(plugin_name, hook, f"{path_key}: {reason}")
                owner_of[path_key] = plugin_name
                files_by_plugin.setdefault(plugin_name, []).append(item)
            else:
                raise PluginError(
                    plugin_name,
                    hook,
                    f"expected GeneratedFile or Diagnostic, got {type(item).__name__}",
                )
    return files_by_plugin, extra_diags


def project_files(
    root: Path,
    files: Sequence[tuple[str, GeneratedFile]],
    diagnostics: Sequence[tuple[str, Diagnostic]] = (),
    *,
    lock_root: Path | None = None,
    lock_prefix: PurePosixPath | None = None,
    ignore_lock_prefixes: Sequence[str] = (),
) -> Projection:
    """Compare the proposed `(owner, GeneratedFile)` entries with the lock at `lock_root` (or `root`).

    When `lock_prefix` is given, only lock entries under `<lock_prefix>/` are inspected (stripped
    of that prefix). When `ignore_lock_prefixes` is given, lock entries starting with any of those
    prefixes (unless proposed here) are ignored for `stale_lock`.
    A malformed lock adds a core warning; a symlinked parent marks just that file `skipped`.
    """
    effective_lock_root = lock_root if lock_root is not None else root
    full_lock, core_diag = _read_lock_or_warn(effective_lock_root)
    if lock_prefix is not None:
        pfx = f"{lock_prefix.as_posix().rstrip('/')}/"
        lock = {k[len(pfx) :]: v for k, v in full_lock.items() if k.startswith(pfx)}
    else:
        lock = full_lock
    all_diagnostics = list(diagnostics)
    if core_diag is not None:
        all_diagnostics.append(("core", core_diag))
    proposed = {gf.path.as_posix() for _owner, gf in files}
    stale = tuple(
        p
        for p in lock
        if p not in proposed
        and not any(p.startswith(ign) for ign in ignore_lock_prefixes)
    )
    return Projection(
        files=classify_proposals(root, files, lock),
        stale_lock=stale,
        diagnostics=tuple(all_diagnostics),
        blocked=any(diag.level == "error" for _, diag in all_diagnostics),
    )


def flatten_contributions(
    plugins: Plugins,
    answers: Mapping[str, list[Diagnostic]],
    files_by_plugin: Mapping[str, list[GeneratedFile]],
    extra_diags: Mapping[str, list[Diagnostic]],
) -> tuple[list[tuple[str, GeneratedFile]], list[tuple[str, Diagnostic]]]:
    """Order contributions by plugin: its diagnostic answers, then its contribution diagnostics, then its files."""
    diagnostics: list[tuple[str, Diagnostic]] = []
    files: list[tuple[str, GeneratedFile]] = []
    for name in plugins.names:
        for diag in answers.get(name, ()):
            diagnostics.append((name, diag))
        for diag in extra_diags.get(name, ()):
            diagnostics.append((name, diag))
        for gf in files_by_plugin.get(name, ()):
            files.append((name, gf))
    return files, diagnostics


def _stack_lock_location(ws: Workspace) -> tuple[Path, PurePosixPath | None]:
    """Return `(lock_root, lock_prefix)` for `ws` (parent root + stack name when `ws.is_stack`)."""
    if ws.is_stack:
        return ws.root.parent, PurePosixPath(ws.name)
    return ws.root, None


def project(
    ws: Workspace,
    plugins: Plugins,
    *,
    multi: object | None = None,
) -> Projection:
    """Inspect generated files and return their status against the lock.

    Raises `PluginError` on a bad path or two owners of one path; a symlinked parent marks just that file `skipped`.
    """
    from ow.term import warn

    found, diagnostic_warnings = plugins.diagnostics(ws, multi)
    for text in diagnostic_warnings:
        warn(text)
    answers = {name: list(diags) for name, diags in found}

    files_by_plugin, extra_diags = collect_contributions(
        "ow_generated_files",
        plugins.call("ow_generated_files", ws, answers=plugins.answers(ws), multi=multi),
    )
    files, diagnostics = flatten_contributions(plugins, answers, files_by_plugin, extra_diags)
    lock_root, lock_prefix = _stack_lock_location(ws)
    return project_files(
        ws.root,
        files,
        diagnostics,
        lock_root=lock_root,
        lock_prefix=lock_prefix,
    )


@contextlib.contextmanager
def _refresh_lock(root: Path):
    """Hold an exclusive sidecar lock while refreshing generated files and the lock file."""
    lock_rel = PurePosixPath(RENDERED_LOCK.as_posix())
    if fs.ancestry_problem(root, lock_rel):
        yield
        return
    with fs.sidecar_lock(root / RENDERED_LOCK):
        yield


def write_files(
    root: Path,
    projection: Projection,
    *,
    lock_root: Path | None = None,
    lock_prefix: PurePosixPath | None = None,
) -> tuple[tuple[PurePosixPath, ...], tuple[PurePosixPath, ...]]:
    """Write what `projection` says is missing or stale under `root`; return `(written, owned)`.

    Nothing is written when the projection is blocked. A destination is re-read right before writing, so a hand edit landing since projection is left alone; a write failure raises `OwError` naming what already landed. Lock entries no file claims any more are dropped; their files stay.
    """
    if projection.blocked:
        return (), ()

    effective_lock_root = lock_root if lock_root is not None else root
    pfx = f"{lock_prefix.as_posix().rstrip('/')}/" if lock_prefix is not None else ""

    with _refresh_lock(effective_lock_root):
        lock, _ = _read_lock_or_warn(effective_lock_root)

        written: list[PurePosixPath] = []
        owned: list[PurePosixPath] = []
        new_lock = dict(lock)  # old entries survive until their path is reclassified
        failure: OwError | None = None

        for status in projection.files:
            path_key = status.path.as_posix()
            lock_key = f"{pfx}{path_key}"
            if status.state in ("absent", "outdated"):
                dest = root / status.path
                if fs.ancestry_problem(root, status.path):
                    continue
                # Re-read right before writing: bytes that changed since projection are
                # a hand edit, which wins. Leave it alone and keep the old lock entry,
                # so the next projection reads it as "yours".
                st = fs.destination_stat(dest)
                if st is not None:
                    if fs.unsafe_destination(path_key, st):
                        continue
                    try:
                        current = dest.read_bytes()
                    except OSError:
                        continue
                    if current != status.current:
                        continue
                try:
                    fs.atomic_write(dest, status.proposal, mode=status.mode)
                except OSError as exc:
                    msg = f"cannot write {path_key}: {exc}"
                    if written:
                        written_paths = ", ".join(p.as_posix() for p in written)
                        msg = f"{msg} (already wrote {written_paths})"
                    failure = OwError(msg)
                    break
                new_lock[lock_key] = _sha256_bytes(status.proposal)
                written.append(status.path)
                owned.append(status.path)
            elif status.state == "up to date":
                new_lock[lock_key] = _sha256_bytes(status.proposal)
                owned.append(status.path)
            # yours / skipped: keep the old entry already in new_lock, if any.

        if failure is None:
            for path_key in projection.stale_lock:
                new_lock.pop(f"{pfx}{path_key}", None)

        _write_lock_if_changed(effective_lock_root, new_lock)
        if failure is not None:
            raise failure

    return tuple(written), tuple(owned)


def remove_lock_prefix(lock_root: Path, prefix: str) -> None:
    """Drop all lock entries under `<prefix>/` from `lock_root`'s rendered lock."""
    lock_rel = PurePosixPath(RENDERED_LOCK.as_posix())
    if fs.ancestry_problem(lock_root, lock_rel):
        return
    if not (lock_root / RENDERED_LOCK).exists():
        return
    pfx = f"{prefix.rstrip('/')}/"
    with _refresh_lock(lock_root):
        lock, _ = _read_lock_or_warn(lock_root)
        new_lock = {k: v for k, v in lock.items() if not k.startswith(pfx)}
        _write_lock_if_changed(lock_root, new_lock)


def migrate_lock_to_prefix(lock_root: Path, prefix: str) -> None:
    """Prefix every existing lock entry at `lock_root` with `<prefix>/` (used during promotion)."""
    lock_rel = PurePosixPath(RENDERED_LOCK.as_posix())
    if fs.ancestry_problem(lock_root, lock_rel):
        return
    if not (lock_root / RENDERED_LOCK).exists():
        return
    pfx = f"{prefix.rstrip('/')}/"
    with _refresh_lock(lock_root):
        lock, _ = _read_lock_or_warn(lock_root)
        new_lock = {
            (k if k.startswith(pfx) else f"{pfx}{k}"): v for k, v in lock.items()
        }
        _write_lock_if_changed(lock_root, new_lock)


def refresh(
    ws: Workspace,
    plugins: Plugins,
    *,
    multi: object | None = None,
) -> tuple[Projection, tuple[PurePosixPath, ...]]:
    """Project `ws`'s generated files and write the missing or stale ones; a blocked projection writes nothing."""
    projection = project(ws, plugins, multi=multi) if multi is not None else project(ws, plugins)
    if projection.blocked:
        return projection, ()
    lock_root, lock_prefix = _stack_lock_location(ws)
    written, owned = write_files(
        ws.root, projection, lock_root=lock_root, lock_prefix=lock_prefix
    )
    plugins.call("ow_refreshed", ws, owned=owned)
    return projection, written


def sync_if_stale(
    ws: Workspace,
    plugins: Plugins,
    *,
    multi: object | None = None,
) -> tuple[PurePosixPath, ...]:
    """Write only missing or stale owned files for `ws`, doing nothing when already up to date."""
    projection = project(ws, plugins, multi=multi) if multi is not None else project(ws, plugins)
    if projection.blocked:
        return ()
    if not any(status.state in ("absent", "outdated") for status in projection.files):
        return ()
    lock_root, lock_prefix = _stack_lock_location(ws)
    written, _owned = write_files(
        ws.root, projection, lock_root=lock_root, lock_prefix=lock_prefix
    )
    if written:
        plugins.call("ow_refreshed", ws, owned=written)
    return written


def _owned_regular(root: Path, rel: PurePosixPath) -> Path | None:
    """`root / rel` when it is a regular file reachable without a symlink, else None."""
    if _unsafe_relative(rel) or fs.ancestry_problem(root, rel):
        return None
    path = root / rel
    st = fs.destination_stat(path)
    if st is None or fs.unsafe_destination(rel.as_posix(), st):
        return None
    return path


def remove_owned_files(root: Path) -> tuple[Path, ...]:
    """Delete the recorded files under `root` that still hold the bytes ow wrote, then its lock.

    This is the only deletion of generated files: ow calls it when it removes the workspace root itself. Hand-edited files stay. The `.ow` directory goes too when nothing else is in it.
    """
    lock_rel = PurePosixPath(RENDERED_LOCK.as_posix())
    if fs.ancestry_problem(root, lock_rel):
        return ()
    removed: list[Path] = []
    lock_path = root / RENDERED_LOCK
    with _refresh_lock(root):
        lock, _ = _read_lock_or_warn(root)
        for key, digest in lock.items():
            path = _owned_regular(root, PurePosixPath(key))
            if path is None:
                continue
            try:
                if _sha256_bytes(path.read_bytes()) == digest:
                    path.unlink()
                    removed.append(path)
            except OSError:
                continue
        st = fs.destination_stat(lock_path)
        if st is not None and not fs.unsafe_destination(RENDERED_LOCK.as_posix(), st):
            with contextlib.suppress(OSError):
                lock_path.unlink()
    # `fs.sidecar_lock` keeps its flock file next to the lock as `.<name>.lock`.
    with contextlib.suppress(OSError):
        lock_path.with_name(f".{lock_path.name}.lock").unlink()
    with contextlib.suppress(OSError):
        lock_path.parent.rmdir()
    return tuple(removed)


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


def _enclosing_multi(ws: Workspace, plugins: Plugins) -> object | None:
    """Load the enclosing `MultiWorkspace` for a stack without re-discovering repo plugins."""
    if not ws.is_stack:
        return None
    from ow.core import stacks

    parent = stacks.parent_multi_workspace(ws.root)
    if parent is None:
        return None
    loaded = stacks.load_multi_workspace(parent, plugins, discover_repo_plugins=False)
    return loaded[0] if loaded is not None else None


def refresh_workspace(session: Session, *, multi: object | None = None) -> int:
    """Refresh the session's workspace and print the summary; 1 when an error diagnostic blocked it."""
    assert session.workspace is not None
    eff_multi = multi if multi is not None else _enclosing_multi(session.workspace, session.plugins)
    projection, written = refresh(session.workspace, session.plugins, multi=eff_multi)
    report(projection, written)
    return 1 if projection.blocked else 0


def refresh_after(session: Session) -> int:
    """Refresh after a git batch: git just moved branches, so re-derive the session first."""
    return refresh_workspace(session.reload())
