"""`ow init` — initialize, compose, adopt, or repair the current directory as an ow workspace."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.commands import add as add_cmd
from ow.commands._repos import resolve_base
from ow.commands._shared import known_repo_catalog, print_dropped, refresh_workspace, remotes_of
from ow.commands._targets import deduplicate_name, parse_unit_spec
from ow.commands._workspace import (
    AddResult,
    UnitMode,
    UnitResult,
    create_workspace_unit,
    report_result,
    resolve_required_target,
    stack_info,
    sync_multi_files,
)
from ow.core import config, git, index, stacks
from ow.core.plugins import ResolvedTarget
from ow.core.session import global_settings, open_session
from ow.core.workspace import build_workspace, derive_repos, find_root
from ow.errors import UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


def _repair_and_adopt_single(
    root: Path,
    plugins: Plugins,
    *,
    dry_run: bool = False,
) -> tuple[UnitResult, int]:
    """Repair prunable worktrees, adopt any missing `[repos]` entries, re-index, and refresh."""
    declared = {s.key: s for s in plugins.settings()}
    config_path = root / ".ow" / "config.toml"
    is_new_config = not config_path.is_file()

    if not is_new_config:
        ws_layer, accepted, _rejected, meta = config.read_workspace_config(
            config_path, declared, remotes_of
        )
        ws_values = dict(ws_layer.values)
        is_stack = meta.stack
    else:
        accepted = {}
        ws_values = {}
        is_stack = False

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
                repos=tuple(loc.name for loc in initial_locations),
                action="planned",
                ports=info.ports,
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
    changed = is_new_config
    for loc in locations:
        if loc.name not in accepted and loc.problem != "forgotten":
            try:
                inferred, src = resolve_base(loc.name, None, accepted)
                term.out(f"# {loc.name}: default base {inferred} ({src})")
                accepted[loc.name] = inferred
                changed = True
            except UsageError as exc:
                term.warn(f"{loc.name}: {exc}")

    if changed:
        dropped = config.write_workspace(
            config_path,
            accepted,
            ws_values,
            declared=declared,
            remotes_of=remotes_of,
            stack=is_stack,
        )
        print_dropped(dropped)

    if not is_stack:
        index.add(root)
    else:
        index.add(root.parent.resolve())

    session = open_session(str(root), need_workspace=True, plugins=plugins, cwd=root)
    assert session.workspace is not None
    for warning in session.warnings:
        term.warn(warning)
    if is_new_config:
        plugins.call("ow_workspace_created", ws=session.workspace)
    code = refresh_workspace(session)

    info = stack_info(session.workspace, plugins)
    action = "created" if is_new_config else ("updated" if (changed or repaired_any) else "unchanged")
    unit = UnitResult(
        name=session.workspace.name,
        root=root,
        is_stack=is_stack,
        repos=tuple(r.name for r in session.workspace.repos),
        action=action,
        ports=info.ports,
        note=info.note,
    )
    return unit, code


def _has_unrelated_child_workspaces(target_dir: Path) -> bool:
    """True when `target_dir` holds direct child mono-stack workspaces (e.g. `~/dev`)."""
    try:
        for entry in target_dir.iterdir():
            if entry.name.startswith(".") or not entry.is_dir():
                continue
            if (entry / ".ow" / "config.toml").is_file() and not stacks.is_stack_config(entry):
                return True
    except OSError:
        return False
    return False


def run(
    plugins: Plugins,
    specs: Sequence[str] = (),
    *,
    branch: str | None = None,
    as_name: str | None = None,
    dry_run: bool = False,
    yes: bool = False,
    cwd: Path | None = None,
    json_output: bool = False,
) -> int:
    """Initialize, compose, adopt, or repair the current directory as an ow workspace."""
    target_dir = (cwd or Path.cwd()).resolve()

    with term.json_mode(json_output):
        result = _execute(
            plugins,
            target_dir,
            specs,
            branch=branch,
            as_name=as_name,
            dry_run=dry_run,
            yes=yes,
        )
        return report_result(result, json_output=json_output, create_verb="initialized")


def _execute(
    plugins: Plugins,
    target_dir: Path,
    specs: Sequence[str],
    *,
    branch: str | None,
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
                branch=branch,
                as_name=as_name,
                dry_run=dry_run,
                yes=yes,
            )
            return replace(
                add_res,
                workspace_root=target_dir,
                multi_stack=True,
                workspace_files=add_res.workspace_files or ws_files,
                exit_code=max(exit_code, add_res.exit_code),
            )
        return AddResult(
            workspaces=tuple(repaired_list),
            workspace_root=target_dir,
            multi_stack=True,
            workspace_files=ws_files,
            dry_run=dry_run,
            exit_code=exit_code,
        )

    # 2. Existing mono-stack workspace (or directory with adoptable/interrupted ow worktrees)
    has_own_config = (target_dir / ".ow" / "config.toml").is_file()
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
            return AddResult(
                workspaces=(repaired,),
                workspace_root=target_dir,
                multi_stack=False,
                workspace_files=ws_files,
                dry_run=dry_run,
                exit_code=code,
            )

        # Interrupted `ow init` (worktrees present, no config.toml yet) with 1 unnamed spec:
        # resume directly so broken `initializing` worktrees are cleaned up before refresh.
        if not has_own_config and len(specs) == 1:
            unit = parse_unit_spec(specs[0], allow_run_args=False)
            if unit.name is None:
                if as_name is not None:
                    raise UsageError(
                        "--as is only used when promoting a mono-stack workspace with <name>=<spec>"
                    )
                catalog = known_repo_catalog(plugins)
                settings = global_settings(plugins)
                resolved = resolve_required_target(unit.spec, plugins, catalog, settings)
                created, code = create_workspace_unit(
                    plugins,
                    root=target_dir,
                    resolved=resolved,
                    mode=UnitMode.mono(allow_existing_dir=True, allow_attach=True, dry_run=dry_run),
                    branch=branch,
                )
                return AddResult(
                    workspaces=(created,),
                    workspace_root=target_dir,
                    multi_stack=False,
                    dry_run=dry_run,
                    exit_code=code,
                )

        _repaired, code = _repair_and_adopt_single(target_dir, plugins, dry_run=dry_run)
        add_res = add_cmd.apply_specs_to_existing(
            plugins,
            specs,
            cwd=target_dir,
            branch=branch,
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
        return AddResult(
            workspaces=(created,),
            workspace_root=target_dir,
            multi_stack=False,
            dry_run=dry_run,
            exit_code=code,
        )

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
    parsed: list[tuple[str | None, ResolvedTarget]] = [
        (unit.name, resolve_required_target(unit.spec, plugins, catalog, settings))
        for unit in units
    ]

    # 4. Single unnamed spec -> mono-stack workspace in `target_dir`
    if len(parsed) == 1 and parsed[0][0] is None:
        _name, resolved = parsed[0]
        created, code = create_workspace_unit(
            plugins,
            root=target_dir,
            resolved=resolved,
            mode=UnitMode.mono(allow_existing_dir=True, dry_run=dry_run),
            branch=branch,
        )
        return AddResult(
            workspaces=(created,),
            workspace_root=target_dir,
            multi_stack=False,
            dry_run=dry_run,
            exit_code=code,
        )

    # 5. Multi-stack workspace: 1+ named specs or 2+ specs
    taken: set[str] = set()
    planned: list[tuple[str, ResolvedTarget]] = []
    for explicit_name, resolved in parsed:
        if explicit_name is not None:
            taken.add(explicit_name)
            planned.append((explicit_name, resolved))
        else:
            planned.append((deduplicate_name(resolved.name, taken), resolved))

    created_list: list[UnitResult] = []
    exit_code = 0
    ws_files = ()
    pending: list[dict[str, object]] = []
    try:
        for stack_name, resolved in planned:
            created, code = create_workspace_unit(
                plugins,
                root=target_dir / stack_name,
                resolved=resolved,
                mode=UnitMode.stack(target_dir, dry_run=dry_run),
                branch=branch,
                pending=pending,
            )
            created_list.append(created)
            exit_code = max(exit_code, code)
    finally:
        if not dry_run and created_list:
            index.add(target_dir)
            ws_files = sync_multi_files(target_dir, plugins)

    return AddResult(
        workspaces=tuple(created_list),
        workspace_root=target_dir,
        multi_stack=True,
        workspace_files=ws_files,
        dry_run=dry_run,
        exit_code=exit_code,
    )
