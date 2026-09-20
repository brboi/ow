"""Copy-once local seeds, and create-only worktree repair.

`$XDG_CONFIG_HOME/ow/local/` is an optional source of files the user wants
in every workspace; `seed_local_files` copies the ones a workspace does not
have yet into `<workspace>/.local/`, once, byte for byte, before addon
scanning looks there. `materialize_missing` never deletes, never
commandeers, and never touches a worktree that already exists and checks
out correctly: an existing path is either exactly this repository's
worktree or a naming conflict it reports and leaves alone. Only an absent
alias is ever created.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from ow.utils import paths
from ow.utils.drift import DriftResult, check_all_drift
from ow.utils.git import (
    create_worktree,
    ensure_bare_repo,
    get_worktree_common_dir,
    parallel_per_repo,
    resolve_spec,
    run_cmd,
    worktree_exists,
)

if TYPE_CHECKING:
    from ow.utils.config import BranchSpec, Config, WorkspaceConfig

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


@dataclass(frozen=True)
class MaterializeResult:
    """What one repair pass did, by repo alias.

    `created` and `existing` are disjoint: every declared alias lands in
    exactly one of them, or in `errors`. `drifted` is computed once, after
    the whole pass, against the config `materialize_missing` was given —
    it names every repo whose worktree is not on the branch its own spec
    declares, existing and freshly created alike, so a caller can surface
    what a repair deliberately chose not to touch.
    """

    created: tuple[str, ...] = ()
    existing: tuple[str, ...] = ()
    drifted: tuple[DriftResult, ...] = ()
    errors: dict[str, str] = field(default_factory=dict)


def _is_this_repos_worktree(bare_repo: Path, destination: Path) -> bool:
    """Whether `destination` already checks out `bare_repo`, safely.

    Every condition must hold: a symlink is never trusted regardless of
    where it points, `.git` must be the file form a linked worktree writes
    (never a directory, which would make `destination` a repository of its
    own), the bare repo's own worktree list must agree, and the worktree's
    resolved common dir must be this exact bare repo — both sides resolved,
    so a symlinked $HOME cannot make a healthy worktree look foreign.
    """
    return (
        not destination.is_symlink()
        and (destination / ".git").is_file()
        and worktree_exists(bare_repo, destination)
        and get_worktree_common_dir(destination) == bare_repo.resolve()
    )


def materialize_missing(ws: WorkspaceConfig, config: Config, root: Path) -> MaterializeResult:
    """Create every declared worktree that is not already there. Never repairs one that is.

    An existing path is checked, never rebuilt: valid, it counts as
    `existing`; anything else — a stray file, someone else's checkout, a
    symlink — is a naming conflict reported in `errors` and left exactly as
    found. Absent aliases are fetched and created in parallel, since each is
    an independent network operation; the safety check above is local and
    fast, so it stays sequential.
    """
    bare_repos_dir = paths.repos_dir()
    created: set[str] = set()
    existing: set[str] = set()
    errors: dict[str, str] = {}
    tasks: dict[str, Callable[[], None]] = {}

    def _make_task(alias: str, spec: BranchSpec, bare_repo: Path) -> Callable[[], None]:
        def _task() -> None:
            alias_remotes = config.remotes.get(alias, {})
            ensure_bare_repo(alias, alias_remotes, bare_repos_dir)
            resolved = resolve_spec(bare_repo, spec, alias_remotes)
            run_cmd(["git", "-C", str(bare_repo), "worktree", "prune"], check=True, label=alias)
            create_worktree(bare_repo, root / alias, resolved)
        return _task

    for alias, spec in ws.repos.items():
        destination = root / alias
        bare_repo = bare_repos_dir / f"{alias}.git"
        if destination.exists() or destination.is_symlink():
            if _is_this_repos_worktree(bare_repo, destination):
                existing.add(alias)
            else:
                errors[alias] = "path exists but is not this repository's worktree"
            continue
        tasks[alias] = _make_task(alias, spec, bare_repo)

    if tasks:
        results = parallel_per_repo(tasks)
        for alias, result in results.items():
            if isinstance(result, Exception):
                errors[alias] = str(result)
            else:
                created.add(alias)

    drifted = tuple(check_all_drift(ws, root))
    return MaterializeResult(
        created=tuple(sorted(created)),
        existing=tuple(sorted(existing)),
        drifted=drifted,
        errors=errors,
    )