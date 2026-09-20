"""Explicit, lossless migration away from the version-1 template model.

Migration is a decision plus a mechanical commit, never a rendering pass:
nothing here executes a legacy template, imports Jinja or touches a
workspace file outside `commit_migration`. Legacy sources are inventoried
by name and by bytes against the frozen digest asset, and a lock entry is
retired only where a fixed 3.0 generator would otherwise overwrite a file
the user's own copy produced. Customizations never block the migration —
they retire ownership — while configuration data that cannot be
represented is a blocking diagnostic that stops every write.

`plan_migration`/`commit_migration` are the entry points of the whole
conversion. Their config half belongs to the runtime model in
`ow.utils.config`: `dumps_workspace_config`/`dumps_global_config` are the
only writers of either on-disk format, so this module imports them rather
than serializing anything itself. The dependency is one-way — migration
imports config, config never imports migration.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from ow.utils import generate, paths, render
from ow.utils.config import (
    SCHEMA_VERSION,
    Config,
    LegacyConfig,
    WorkspaceConfig,
    _legacy_document,
    _legacy_templates,
    dumps_global_config,
    dumps_workspace_config,
)
from ow.utils.odoo import is_odoo_main_repo

_DIGEST_ASSET = (
    Path(__file__).resolve().parent.parent / "_static" / "legacy-template-hashes.json"
)

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_BACKUP_DIR_MODE = 0o700
_ATOMIC_FILE_MODE = 0o600

STOCK = "stock"
OVERRIDDEN = "overridden"
CUSTOM = "custom"
CUSTOM_MISSING = "custom-missing"

# The generator table is private to `generate`; migration must not fork the
# list of paths a 3.0 render can overwrite, so it reads the table itself.
GENERATED_OUTPUT_PATHS: tuple[str, ...] = tuple(
    path.as_posix() for path, _, _ in generate._GENERATORS
)


@dataclass(frozen=True)
class MigrationWrite:
    """One planned replacement; `original` is None when the file is absent."""

    path: Path
    original: bytes | None
    replacement: bytes


@dataclass(frozen=True)
class MigrationPlan:
    """What a migration will write, plus its diagnostics.

    `errors` are blockers: any entry makes `commit_migration` refuse the
    whole plan before touching a byte. `warnings` carry the retirement
    report and other findings that never stop the conversion.
    """

    writes: tuple[MigrationWrite, ...] = ()
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class LegacySourceFile:
    """One legacy template source found on disk, and its derivable output.

    `digest` is None when the file could not be read; an unreadable source is
    unprovable, so it counts as customized rather than raising here.
    """

    bundle: str
    source: Path
    relative: str
    output: str
    digest: str | None
    stock: bool


@dataclass(frozen=True)
class LegacyBundle:
    """A declared legacy bundle and how its sources compare to the frozen inventory."""

    name: str
    status: str
    files: tuple[LegacySourceFile, ...]
    frozen_outputs: tuple[str, ...]
    overrides_dir: Path
    workspace_dir: Path


@dataclass(frozen=True)
class RetirementDecision:
    """Which lock entries the retirement report takes away, and what it says.

    `retired` and `kept` partition the locked paths; `report` is the lines a
    caller shows (`MigrationPlan.warnings`) so the user sees which source
    produced which output and why ow stops protecting it.
    """

    retired: tuple[str, ...]
    kept: tuple[str, ...]
    report: tuple[str, ...]


def load_template_digests(
    asset: Path | None = None,
) -> dict[str, dict[str, tuple[str, ...]]]:
    """The frozen last-v2 source inventory: bundle → source path → digests.

    Digests only, never template source text: a stock copy can only be
    recognised by hashing it, and shipping the sources would be a second
    rendering implementation. Malformed structure is a ValueError naming the
    asset, never a silently empty inventory — an empty one would classify
    every shipped bundle as custom.
    """
    path = _DIGEST_ASSET if asset is None else asset
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise OSError(f"cannot read legacy template digests at {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"legacy template digests at {path} are not valid JSON: {exc}"
        ) from exc

    if not isinstance(document, dict) or not document:
        raise ValueError(f"legacy template digests at {path} must be a non-empty object")

    digests: dict[str, dict[str, tuple[str, ...]]] = {}
    for bundle, files in document.items():
        if not isinstance(bundle, str) or not bundle:
            raise ValueError(f"legacy template digests at {path}: invalid bundle {bundle!r}")
        if not isinstance(files, dict) or not files:
            raise ValueError(
                f"legacy template digests at {path}: bundle {bundle!r} must be a non-empty object"
            )
        per_bundle: dict[str, tuple[str, ...]] = {}
        for relative, hashes in files.items():
            if not isinstance(relative, str) or not relative or relative.startswith("/"):
                raise ValueError(
                    f"legacy template digests at {path}: bundle {bundle!r}: "
                    f"invalid source path {relative!r}"
                )
            if not isinstance(hashes, list) or not hashes:
                raise ValueError(
                    f"legacy template digests at {path}: bundle {bundle!r}: "
                    f"{relative!r} lists no digest"
                )
            for digest in hashes:
                if not isinstance(digest, str) or not _SHA256_HEX.fullmatch(digest):
                    raise ValueError(
                        f"legacy template digests at {path}: bundle {bundle!r}: "
                        f"{relative!r}: invalid digest {digest!r}"
                    )
            per_bundle[relative] = tuple(hashes)
        digests[bundle] = per_bundle
    return digests


def legacy_source_output(relative: str) -> str:
    """The workspace-relative output a legacy source derives: `.j2` stripped once."""
    return relative[:-3] if relative.endswith(".j2") else relative


def _legacy_overrides_root() -> Path:
    """The user's own bundle tree, `$XDG_CONFIG_HOME/ow/templates`.

    Named here rather than in `paths` because it is a migration-era location:
    `paths.templates_dir()` and the packaged tree both disappear with the
    rendering engine, and a path only migration still needs belongs with
    migration. Resolved per call, so an isolated XDG in a test is honoured.
    """
    return paths.config_home() / "templates"


def _effective_bundles(
    legacy: "LegacyConfig", workspace_path: Path, repos: Mapping[str, object]
) -> tuple[str, ...]:
    """The bundle names a schema-1 workspace effectively declared.

    `common` always applied and `odoo` applied whenever a declared repo is an
    Odoo core — neither was ever named in the manifest, so both are derived
    here the way the old renderer derived them. The declaration is what the
    file itself says; a name it cannot carry (a path separator, an empty
    string) raises, and the caller turns that into a blocker.
    """
    bundles = ["common", *_legacy_templates(_legacy_document(legacy))]
    if any(is_odoo_main_repo(workspace_path / alias) for alias in repos):
        bundles.append("odoo")
    return tuple(bundles)


def plan_migration(
    config: Config, ws: WorkspaceConfig, workspace_path: Path
) -> MigrationPlan:
    """Everything an explicit conversion would write, after a joint preflight.

    Both sides are planned together and either side's blockers abort every
    write: a workspace whose `vars` cannot be represented must not have its
    lock retired either. Nothing here touches the filesystem — the
    replacements come from the config serializers and the lock's own
    serializer, and the originals are the bytes the records were loaded
    from, so `commit_migration` can refuse a source edited since.

    The retirement half only applies to a schema-1 workspace: it is the
    workspace manifest that declared bundles, and a schema-2 one cannot.
    """
    global_legacy = config.legacy if config.version == 1 else None
    workspace_legacy = ws.legacy if ws.version == 1 else None

    errors: list[str] = []
    for legacy in (global_legacy, workspace_legacy):
        if legacy is not None:
            errors += [f"{legacy.source}: {issue}" for issue in legacy.issues]
    if errors:
        # Blocked plans carry no writes at all, so a caller that ignores the
        # diagnostics still cannot commit a half-migration.
        return MigrationPlan(errors=tuple(errors))

    writes: list[MigrationWrite] = []
    warnings: tuple[str, ...] = ()

    if workspace_legacy is not None:
        digests = load_template_digests()
        try:
            bundles = _effective_bundles(workspace_legacy, workspace_path, ws.repos)
            inventory = inventory_bundles(
                bundles,
                digests,
                overrides_root=_legacy_overrides_root(),
                workspace_templates_root=workspace_path / ".ow" / "templates",
            )
        except ValueError as exc:
            return MigrationPlan(errors=(f"{workspace_legacy.source}: {exc}",))
        try:
            decision = plan_retirement(inventory, render.read_lock(workspace_path))
            lock_write = plan_lock_retirement(workspace_path, decision)
        except (OSError, ValueError) as exc:
            return MigrationPlan(
                errors=(f"{workspace_path / render.RENDERED_LOCK}: {exc}",)
            )
        warnings = decision.report
        if lock_write is not None:
            # Retirement first, both here and in `commit_migration`: it only
            # ever makes ow own less, so an interruption there leaves a
            # schema-1 workspace whose next attempt re-plans the same set.
            writes.append(lock_write)

    if global_legacy is not None:
        migrated = replace(config, version=SCHEMA_VERSION, legacy=None)
        writes.append(
            MigrationWrite(
                path=global_legacy.source,
                original=global_legacy.original,
                replacement=dumps_global_config(migrated),
            )
        )

    if workspace_legacy is not None:
        migrated = replace(ws, version=SCHEMA_VERSION, legacy=None)
        writes.append(
            MigrationWrite(
                path=workspace_legacy.source,
                original=workspace_legacy.original,
                replacement=dumps_workspace_config(migrated),
            )
        )

    return MigrationPlan(writes=tuple(writes), warnings=warnings)


def inventory_bundles(
    bundles: Sequence[str],
    digests: Mapping[str, Mapping[str, Sequence[str]]],
    *,
    overrides_root: Path,
    workspace_templates_root: Path,
) -> tuple[LegacyBundle, ...]:
    """Classify every declared legacy bundle without rendering anything.

    `overrides_root` is the user-local bundle tree (`$XDG_CONFIG_HOME/ow/templates`)
    and `workspace_templates_root` the old `<ws>/.ow/templates` copy; both are
    parameters so no caller's XDG layout is assumed here. A file is stock only
    when its bytes hash to a frozen digest for its own bundle and relative
    path: the old source lock is naming evidence, not proof, so an unprovable
    or extra file makes its bundle overridden — the conservative direction,
    since the retirement it triggers only makes ow own less. A name that is
    not a single path segment is a ValueError naming it, because it could
    only mean scanning outside the two template roots.
    """
    inventory: list[LegacyBundle] = []
    for name in sorted(set(bundles)):
        if not name or "/" in name or name in (".", ".."):
            raise ValueError(f"invalid legacy bundle name {name!r}")
        known = digests.get(name)
        overrides_dir = overrides_root / name
        workspace_dir = workspace_templates_root / name
        files = tuple(
            sorted(
                (
                    _source_file(name, root, path, known)
                    for root in (overrides_dir, workspace_dir)
                    for path in _files_under(root)
                ),
                key=lambda found: (found.relative, found.source.as_posix()),
            )
        )
        if known is None:
            status = CUSTOM if files else CUSTOM_MISSING
            frozen_outputs: tuple[str, ...] = ()
        else:
            status = STOCK if all(found.stock for found in files) else OVERRIDDEN
            frozen_outputs = tuple(sorted({legacy_source_output(rel) for rel in known}))
        inventory.append(
            LegacyBundle(name, status, files, frozen_outputs, overrides_dir, workspace_dir)
        )
    return tuple(inventory)


def plan_retirement(
    inventory: Sequence[LegacyBundle],
    locked: Mapping[str, str],
    *,
    generated: Iterable[str] = GENERATED_OUTPUT_PATHS,
) -> RetirementDecision:
    """Which lock entries to retire, and the report that explains it.

    Pure: it reads the inventory and the lock, never the filesystem. An
    output is retired when a legacy contributor produced it — an override, a
    custom bundle, an unprovable copy — *and* a fixed 3.0 generator produces
    that same path, because a retained entry is what would authorize 3.0 to
    overwrite the user's file. Everything else keeps its entry: a locked path
    no generator produces stays visible as `not rendered` instead of quietly
    losing ow's name. A declared custom bundle whose sources are gone cannot
    be attributed at all, so every generator-produced entry of that
    workspace is retired with an explicit report.
    """
    generated_set = set(generated)
    reasons: dict[str, str] = {}

    for bundle in inventory:
        if bundle.status != CUSTOM_MISSING:
            continue
        for name in locked:
            if name in generated_set:
                reasons.setdefault(
                    name,
                    f"declared bundle {bundle.name!r} has no source under "
                    f"{bundle.overrides_dir} or {bundle.workspace_dir}",
                )

    for bundle in inventory:
        for found in bundle.files:
            if found.stock:
                continue
            if bundle.status == CUSTOM:
                reason = f"custom bundle {bundle.name!r} source {found.source}"
            else:
                reason = f"{found.source} is not a stock {bundle.name!r} source"
            reasons.setdefault(found.output, reason)

    retired = tuple(sorted(name for name in locked if name in reasons and name in generated_set))
    kept = tuple(sorted(set(locked) - set(retired)))

    report: list[str] = []
    for bundle in inventory:
        for found in bundle.files:
            if bundle.status == CUSTOM:
                note = "custom"
            else:
                note = "stock" if found.stock else "not stock"
            report.append(f"legacy source {found.source} -> {found.output} ({note})")
        if bundle.status == CUSTOM_MISSING:
            report.append(
                f"declared bundle {bundle.name!r} has no source under "
                f"{bundle.overrides_dir} or {bundle.workspace_dir}"
            )
        elif not bundle.files and bundle.frozen_outputs:
            report.append(
                f"legacy bundle {bundle.name!r} has no local copy; frozen outputs: "
                + ", ".join(bundle.frozen_outputs)
            )
    for name in retired:
        report.append(f"retired lock entry {name}: {reasons[name]}")

    return RetirementDecision(retired=retired, kept=kept, report=tuple(report))


def plan_lock_retirement(
    root: Path, decision: RetirementDecision
) -> MigrationWrite | None:
    """The lock write that drops `decision.retired`, or None when nothing retires.

    `render.dumps_lock` is the lock's only serializer, so this is a caller of
    that format, never a second writer. `original` is the exact lock bytes on
    disk: `commit_migration` backs them up and refuses to replace a lock
    edited since planning.
    """
    if not decision.retired:
        return None
    locked = render.read_lock(root)
    path = root / render.RENDERED_LOCK
    remaining = {
        name: digest for name, digest in locked.items() if name not in set(decision.retired)
    }
    try:
        original = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    return MigrationWrite(
        path=path, original=original, replacement=render.dumps_lock(remaining)
    )


def backup_path(source: Path, original: bytes) -> Path:
    """Where `source`'s exact bytes are preserved.

    The directory is named after the absolute source path and the file after
    the bytes themselves, so the same original always lands on the same name:
    a retry after an interruption cannot be blocked by a half-written file
    of its own, and a backup whose content changed is detectable.
    """
    identity = hashlib.sha256(str(source.absolute()).encode("utf-8")).hexdigest()
    content = hashlib.sha256(original).hexdigest()
    return paths.backups_dir() / "migrations" / identity / f"{content}.toml"


def write_backup(source: Path, original: bytes) -> Path:
    """Preserve `original` as `source`'s backup; reuse an equal one, refuse a different one.

    Written through a same-directory temp file, flushed, fsynced, then
    `os.replace`d onto the hashed name — a crash leaves a temp file at worst,
    never a truncated backup whose bytes contradict its own name and would
    block every later retry. An existing destination with equal bytes is the
    backup this call would have written, so it is reused; a mismatching one
    is an error naming the path rather than silent data loss.
    """
    dest = backup_path(source, original)
    try:
        existing: bytes | None = dest.read_bytes()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise ValueError(f"cannot read backup {dest}: {exc}") from exc
    if existing is not None:
        if existing == original:
            return dest
        raise ValueError(
            f"backup {dest} exists with different bytes than {source}; refusing to overwrite it"
        )

    directory = dest.parent
    paths.backups_dir().mkdir(parents=True, exist_ok=True)
    (paths.backups_dir() / "migrations").mkdir(mode=_BACKUP_DIR_MODE, exist_ok=True)
    directory.mkdir(mode=_BACKUP_DIR_MODE, exist_ok=True)
    _atomic_write(dest, original)
    return dest


def commit_migration(plan: MigrationPlan) -> None:
    """Apply `plan`: blockers first, then every backup, then atomic replacements.

    Any `plan.errors` aborts before a byte is written, and a source edited
    since planning stops the migration with a rerun request instead of
    clobbering the edit. Backups all precede replacements, and the lock is
    retired before either config (the write order the plan carries is the
    lock first), so an interruption leaves the workspace schema-1 and safely
    retryable, never a replaced config without its backup. Replacements are
    atomic per file, not across files: a global config followed by a failed
    workspace config is explicitly retryable, not falsely rolled back.

    A retry must *re-plan* first: the same plan holds the bytes of the file
    as it was before the interruption, and the byte recheck above will
    refuse it — by design, since only a fresh read can tell what is left to
    do. Callers report that as "rerun the migration", never as a clobber.
    """
    if plan.errors:
        raise ValueError("migration has blocking diagnostics: " + "; ".join(plan.errors))

    ordered = _ordered_writes(plan.writes)
    for write in ordered:
        _recheck(write)
    for write in ordered:
        if write.original is not None:
            write_backup(write.path, write.original)
    for write in ordered:
        _recheck(write)
        write.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(write.path, write.replacement)


def _files_under(root: Path) -> tuple[Path, ...]:
    if not root.is_dir():
        return ()
    return tuple(sorted((path for path in root.rglob("*") if path.is_file())))


def _source_file(
    bundle: str,
    root: Path,
    path: Path,
    known: Mapping[str, Sequence[str]] | None,
) -> LegacySourceFile:
    relative = path.relative_to(root).as_posix()
    try:
        digest: str | None = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        digest = None
    stock = digest is not None and known is not None and digest in known.get(relative, ())
    return LegacySourceFile(bundle, path, relative, legacy_source_output(relative), digest, stock)


def _ordered_writes(
    writes: tuple[MigrationWrite, ...],
) -> tuple[MigrationWrite, ...]:
    """Lock first, then the rest in plan order: retirement precedes config replacement."""
    lock_parts = render.RENDERED_LOCK.parts

    def is_lock(write: MigrationWrite) -> bool:
        return len(write.path.parts) >= 2 and write.path.parts[-2:] == lock_parts

    return tuple(write for write in writes if is_lock(write)) + tuple(
        write for write in writes if not is_lock(write)
    )


def _recheck(write: MigrationWrite) -> None:
    try:
        current: bytes | None = write.path.read_bytes()
    except FileNotFoundError:
        current = None
    except OSError as exc:
        raise ValueError(f"cannot read {write.path} before migration: {exc}") from exc
    if current == write.original:
        return
    if write.original is None:
        raise ValueError(f"{write.path} appeared since migration planning; rerun the migration")
    if current is None:
        raise ValueError(f"{write.path} disappeared since migration planning; rerun the migration")
    raise ValueError(f"{write.path} changed since migration planning; rerun the migration")


def _atomic_write(dest: Path, data: bytes) -> None:
    """Same-directory temp file at 0600, flushed, fsynced, then `os.replace`d."""
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, _ATOMIC_FILE_MODE)
        os.replace(tmp_name, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise