"""`ow init` — initialize, compose, adopt, or repair the current directory as an ow workspace."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ow import term
from ow.commands import add as add_cmd
from ow.commands._repos import resolve_base
from ow.commands._shared import (
    known_repo_catalog,
    print_dropped,
    refresh_workspace,
    remotes_of,
    run_steps,
)
from ow.commands._specs import deduplicate_name, parse_unit_spec
from ow.commands._workspace import (
    AddResult,
    UnitMode,
    UnitResult,
    create_workspace_unit,
    create_workspace_units,
    report_result,
    resolve_required_spec,
    stack_info,
    stack_job,
    sync_multi_files,
    unique_setting_values,
)
from ow.core import bases, config, git, index, stacks
from ow.core.paths import repos_dir
from ow.core.plan import GitStep
from ow.core.plugins import ResolvedSpec
from ow.core.session import (
    LEGACY_ROOT_KEYS,
    global_settings,
    open_session,
    vars_target,
    workspace_plugins,
)
from ow.core.workspace import build_workspace, derive_repos, find_root
from ow.errors import OwError, UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


def _legacy_bases(rejected: Mapping[str, object]) -> dict[str, tuple[str, str]]:
    """The `[repos]` entries in the ow 3.0.0a1 format: repo -> (qualified base, feature branch).

    That format spelled `<base>..<feature>`; the base now lives in `branch.<feature>.ow-base`
    and `[repos]` only holds the repo's default base.
    """
    legacy: dict[str, tuple[str, str]] = {}
    for name, raw in rejected.items():
        if not isinstance(raw, str) or ".." not in raw:
            continue
        try:
            base_text, feature = bases.split_range(raw)
            if feature is not None:
                legacy[name] = (bases.parse_base_for(base_text, name), feature)
        except bases.BaseError:
            continue
    return legacy


def _migrate_legacy_bases(
    root: Path, legacy: Mapping[str, tuple[str, str]], *, dry_run: bool
) -> None:
    """Record each legacy entry's base as `branch.<feature>.ow-base`, unless that branch has one."""
    for name, (base, feature) in legacy.items():
        bare = repos_dir() / f"{name}.git"
        term.out(f"# {name}: [repos] entry in the 3.0.0a1 format; its base is now {base}")
        if not git.has_ref(bare, f"refs/heads/{feature}"):
            term.warn(f"{name}: branch {feature} not found; only the default base {base} is recorded")
            continue
        if git.read(["git", "-C", str(bare), "config", "--get", f"branch.{feature}.ow-base"], check=False):
            continue
        argv = ("git", "-C", str(bare), "config", f"branch.{feature}.ow-base", base)
        if run_steps(name, [GitStep(argv)], root=root, dry_run=dry_run) != 0:
            raise OwError(f"{name}: could not record the base of {feature}")


def _legacy_layout(
    unknown: Mapping[str, object],
    values: Mapping[str, object],
    declared: Mapping[str, Any],
) -> tuple[dict[str, object], set[str]]:
    """What `ow init` rewrites of an ow 3.0.0a1 or 2.x `.ow/config.toml`: `(moved, forget)`.

    `version` and `templates` go. Each `[vars]` key that is a declared `odoo.<key>` setting moves
    to `[odoo]` unless `[odoo]` already sets it (its value wins and the `[vars]` one goes);
    any other `[vars]` key stays: an unknown key like the rest. `moved` maps the new keys to their values and
    `forget` holds the old keys the rewrite removes.
    """
    moved: dict[str, object] = {}
    forget: set[str] = set()
    for key, value in unknown.items():
        if key in LEGACY_ROOT_KEYS:
            term.out(f"# {key}: removed from .ow/config.toml (ow 3.0.0a1 format)")
            forget.add(key)
        elif key.startswith("vars."):
            target = vars_target(key, value, declared)
            if target is None:
                continue  # an unknown key like any other: the session warns about it
            forget.add(key)
            if target in values:
                term.warn(f"{key} dropped: [odoo] already sets {target}")
            else:
                term.out(f"# {key} moves to {target}")
                moved[target] = value
    return moved, forget


def _repair_and_adopt_single(
    root: Path,
    plugins: Plugins,
    *,
    dry_run: bool = False,
) -> tuple[UnitResult, int]:
    """Repair prunable worktrees, adopt any missing `[repos]` entries, re-index, and refresh."""
    plugins = workspace_plugins(root, plugins)[0]
    declared = {s.key: s for s in plugins.settings()}
    is_stack = stacks.is_stack_dir(root)
    config_path = (root.parent / ".ow" / "config.toml") if is_stack else (root / ".ow" / "config.toml")
    is_new_config = not is_stack and not config_path.is_file()

    layer_unknown: Mapping[str, object] = {}
    root_unknown: Mapping[str, object] = {}
    if is_stack:
        ws_layer, stacks_cfg = config.read_multi_config(config_path, declared, remotes_of)
        root_unknown = ws_layer.unknown
        sc = stacks_cfg.get(root.name)
        accepted = dict(sc.accepted_repos) if sc else {}
        rejected = dict(sc.rejected_repos) if sc else {}
        ws_values = dict(sc.layer.values) if sc else {}
    elif not is_new_config:
        ws_layer, accepted, rejected = config.read_workspace_config(
            config_path, declared, remotes_of
        )
        layer_unknown = ws_layer.unknown
        ws_values = dict(ws_layer.values)
    else:
        accepted = {}
        rejected = {}
        ws_values = {}

    legacy = _legacy_bases(rejected)
    migrated_keys = {f"repos.{name}" for name in legacy}
    accepted.update({name: base for name, (base, _feature) in legacy.items()})
    _migrate_legacy_bases(root, legacy, dry_run=dry_run)
    moved, forget = _legacy_layout(layer_unknown, ws_values, declared)
    root_only = {key: value for key, value in root_unknown.items() if key in LEGACY_ROOT_KEYS}
    _no_moves, root_forget = _legacy_layout(root_only, {}, declared)
    ws_values.update(moved)

    if dry_run:
        initial_locations = derive_repos(root, accepted)
        for loc in initial_locations:
            if loc.problem == "prunable":
                argv = ("git", "-C", str(loc.bare), "worktree", "repair", str(loc.path))
                term.out(git.show(argv, root))
            elif loc.problem == "forgotten":
                term.warn(
                    f"{loc.name}: worktree was forgotten by {loc.bare.name}; "
                    f"remove {loc.path} and re-add it"
                )
            elif loc.name not in accepted:
                try:
                    inferred, src = resolve_base(loc.name, None, accepted)
                    term.out(f"# {loc.name}: default base {inferred} ({src})")
                except UsageError as exc:
                    term.warn(f"{loc.name}: {exc}")
        if is_new_config:
            term.out(f"# initialize empty workspace at {git.display_path(root)}")
        preview_ws = build_workspace(root, initial_locations, ws_values, is_stack=is_stack)
        info = stack_info(preview_ws, plugins)
        return (
            UnitResult(
                name=root.name,
                root=root,
                is_stack=is_stack,
                action="planned",
                note=info.note,
            ),
            0,
        )

    # Repair prunable worktrees first so derive_repos sees them healthy.
    initial_locations = derive_repos(root, accepted)
    repaired_any = False
    for loc in initial_locations:
        if loc.problem == "prunable":
            argv = ("git", "-C", str(loc.bare), "worktree", "repair", str(loc.path))
            term.out(git.show(argv, root))
            res = git.run(argv)
            if res.returncode != 0:
                term.warn(f"{loc.name}: worktree repair failed: {res.stderr.strip()}")
            else:
                repaired_any = True
        elif loc.problem == "forgotten":
            term.warn(
                f"{loc.name}: worktree was forgotten by {loc.bare.name}; "
                f"remove {loc.path} and re-add it"
            )

    locations = derive_repos(root, accepted)
    changed = is_new_config or bool(legacy) or bool(forget) or bool(root_forget)
    for loc in locations:
        if loc.name not in accepted and loc.problem != "forgotten":
            try:
                inferred, src = resolve_base(loc.name, None, accepted)
                term.out(f"# {loc.name}: default base {inferred} ({src})")
                accepted[loc.name] = inferred
                changed = True
            except UsageError as exc:
                term.warn(f"{loc.name}: {exc}")

    def write_config(known: Mapping[str, Any] = declared) -> None:
        if is_stack:
            dropped = config.write_stack(
                config_path,
                root.name,
                accepted,
                ws_values,
                declared=known,
                remotes_of=remotes_of,
            )
            if root_forget:
                config.update_multi_workspace(
                    config_path, lambda current: current, declared=known, forget=root_forget
                )
        else:
            dropped = config.update_workspace(
                config_path,
                accepted,
                lambda _current: ws_values,
                declared=known,
                remotes_of=remotes_of,
                forget=forget,
            )
        print_dropped({key: value for key, value in dropped.items() if key not in migrated_keys})

    if changed:
        write_config()

    if not is_stack:
        index.add(root)
    else:
        index.add(root.parent.resolve())

    session = open_session(str(root), need_workspace=True, plugins=plugins, cwd=root)
    assert session.workspace is not None
    if is_new_config:
        unique_values = unique_setting_values(session, plugins)
        if unique_values:
            ws_values.update(unique_values)
            write_config(session.declared)  # the keys of in-repo plugins are known to the session
            changed = True
            session = session.reload()
    for warning in session.warnings:
        term.warn(warning)
    if is_new_config:
        session.plugins.call("ow_workspace_created", ws=session.workspace)
    code = refresh_workspace(session)

    info = stack_info(session.workspace, session.plugins)
    action = "created" if is_new_config else ("updated" if (changed or repaired_any) else "unchanged")
    unit = UnitResult(
        name=session.workspace.name,
        root=root,
        is_stack=is_stack,
        action=action,
        note=info.note,
    )
    return unit, code


def _has_unrelated_child_workspaces(target_dir: Path) -> bool:
    """True when `target_dir` holds direct child standalone workspaces (e.g. `~/dev`)."""
    try:
        for entry in target_dir.iterdir():
            if entry.name.startswith(".") or not entry.is_dir():
                continue
            if (entry / ".ow" / "config.toml").is_file():
                return True
    except OSError:
        return False
    return False


def run(
    plugins: Plugins,
    specs: Sequence[str] = (),
    *,
    as_name: str | None = None,
    dry_run: bool = False,
    yes: bool = False,
    cwd: Path | None = None,
) -> int:
    """Initialize, compose, adopt, or repair the current directory as an ow workspace."""
    target_dir = (cwd or Path.cwd()).resolve()
    result = _execute(
        plugins,
        target_dir,
        specs,
        as_name=as_name,
        dry_run=dry_run,
        yes=yes,
    )
    return report_result(result, create_verb="initialized")


def _execute(
    plugins: Plugins,
    target_dir: Path,
    specs: Sequence[str],
    *,
    as_name: str | None,
    dry_run: bool,
    yes: bool,
) -> AddResult:
    # 1. Existing multi-stack workspace root
    existing_multi = stacks.detect_multi_workspace(target_dir)
    if existing_multi is not None:
        if as_name is not None:
            raise UsageError(
                "--as is only used when promoting an existing mono-stack workspace to multi-stack"
            )
        repaired_list: list[UnitResult] = []
        exit_code = 0
        for stack_path in existing_multi:
            ws_info, code = _repair_and_adopt_single(stack_path, plugins, dry_run=dry_run)
            repaired_list.append(ws_info)
            exit_code = max(exit_code, code)
        ws_files: tuple[Path, ...] = ()
        if not dry_run:
            index.add(target_dir)
            ws_files = sync_multi_files(target_dir, plugins)

        if specs:
            add_res = add_cmd.apply_specs_to_existing(
                plugins,
                specs,
                cwd=target_dir,
                as_name=as_name,
                dry_run=dry_run,
                yes=yes,
            )
            return replace(
                add_res,
                workspace_files=add_res.workspace_files or ws_files,
                exit_code=max(exit_code, add_res.exit_code),
            )
        return AddResult(
            workspaces=tuple(repaired_list),
            workspace_files=ws_files,
            exit_code=exit_code,
        )

    # 2. Existing mono-stack workspace or stack (or directory with adoptable/interrupted ow worktrees)
    has_own_config = (target_dir / ".ow" / "config.toml").is_file() or stacks.is_stack_dir(target_dir)
    adoptable_locs = () if has_own_config else derive_repos(target_dir)
    if has_own_config or adoptable_locs:
        if not specs:
            if as_name is not None:
                raise UsageError(
                    "--as is only used when promoting a mono-stack workspace with <name>=<spec>"
                )
            repaired, code = _repair_and_adopt_single(target_dir, plugins, dry_run=dry_run)
            ws_files = ()
            if not dry_run and repaired.is_stack:
                ws_files = sync_multi_files(target_dir.parent.resolve(), plugins)
            return AddResult(workspaces=(repaired,), workspace_files=ws_files, exit_code=code)

        # Interrupted `ow init` (worktrees present, no config.toml yet) with 1 unnamed spec:
        # resume directly so broken `initializing` worktrees are cleaned up before refresh.
        if not has_own_config and len(specs) == 1:
            unit = parse_unit_spec(specs[0], allow_run_args=False)
            if unit.name is None:
                if as_name is not None:
                    raise UsageError(
                        "--as is only used when promoting a mono-stack workspace with <name>=<spec>"
                    )
                resume_plugins = workspace_plugins(target_dir, plugins)[0]
                catalog = known_repo_catalog(resume_plugins)
                settings = global_settings(resume_plugins)
                resolved = resolve_required_spec(unit.spec, resume_plugins, catalog, settings)
                created, code = create_workspace_unit(
                    resume_plugins,
                    root=target_dir,
                    resolved=resolved,
                    mode=UnitMode.mono(allow_existing_dir=True, allow_attach=True),
                    dry_run=dry_run,
                )
                return AddResult(workspaces=(created,), exit_code=code)

        _repaired, code = _repair_and_adopt_single(target_dir, plugins, dry_run=dry_run)
        add_res = add_cmd.apply_specs_to_existing(
            plugins,
            specs,
            cwd=target_dir,
            as_name=as_name,
            dry_run=dry_run,
            yes=yes,
        )
        return replace(add_res, exit_code=max(code, add_res.exit_code))

    # Prevent accidental `ow init` inside a subdirectory of an existing workspace
    enclosing = find_root(target_dir.parent)
    if enclosing is not None:
        raise UsageError(
            f"{target_dir} is inside existing workspace {enclosing}; "
            "run `ow init` in a separate directory or `ow add` from the workspace"
        )

    # Prevent accidental `ow init` in a container holding unrelated standalone workspaces (e.g. ~/dev)
    if _has_unrelated_child_workspaces(target_dir):
        raise UsageError(
            f"{target_dir} already contains standalone ow workspaces; "
            "create and cd into a new directory before running `ow init`"
        )

    if as_name is not None:
        raise UsageError(
            "--as is only used when promoting an existing mono-stack workspace to multi-stack"
        )

    # 3. Fresh directory: 0 specs -> empty mono-stack workspace (like `git init`)
    if not specs:
        created, code = _repair_and_adopt_single(target_dir, plugins, dry_run=dry_run)
        return AddResult(workspaces=(created,), exit_code=code)

    # Parse all specs before resolving targets, and resolve targets before mutating disk
    units = [parse_unit_spec(raw, allow_run_args=False) for raw in specs]
    if not (len(units) == 1 and units[0].name is None):
        seen_explicit: set[str] = set()
        for unit in units:
            if unit.name is not None:
                if unit.name in seen_explicit:
                    raise UsageError(f"duplicate stack name {unit.name!r}")
                seen_explicit.add(unit.name)

    catalog = known_repo_catalog(plugins)
    settings = global_settings(plugins)
    parsed: list[tuple[str | None, ResolvedSpec]] = [
        (unit.name, resolve_required_spec(unit.spec, plugins, catalog, settings))
        for unit in units
    ]

    # 4. Single unnamed spec -> mono-stack workspace in `target_dir`
    if len(parsed) == 1 and parsed[0][0] is None:
        _name, resolved = parsed[0]
        created_one, code = create_workspace_unit(
            plugins,
            root=target_dir,
            resolved=resolved,
            mode=UnitMode.mono(allow_existing_dir=True),
            dry_run=dry_run,
        )
        return AddResult(workspaces=(created_one,), exit_code=code)

    # 5. Multi-stack workspace: 1+ named specs or 2+ specs
    taken: set[str] = set()
    planned: list[tuple[str, ResolvedSpec]] = []
    for explicit_name, resolved in parsed:
        if explicit_name is not None:
            taken.add(explicit_name)
            planned.append((explicit_name, resolved))
        else:
            planned.append((deduplicate_name(resolved.name, taken), resolved))

    ws_files = ()
    pending: list[dict[str, object]] = []
    jobs = [stack_job(target_dir, stack_name, resolved) for stack_name, resolved in planned]
    created: tuple[UnitResult, ...] = ()
    exit_code = 0
    try:
        created, exit_code = create_workspace_units(
            plugins, jobs, dry_run=dry_run, pending=pending
        )
    finally:
        if not dry_run and any(stacks.is_stack_dir(target_dir / name) for name, _ in planned):
            index.add(target_dir)
            ws_files = sync_multi_files(target_dir, plugins)

    return AddResult(
        workspaces=created,
        workspace_files=ws_files,
        exit_code=exit_code,
    )
