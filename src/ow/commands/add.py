"""`ow add` — evolve an initialized workspace by adding repos, attaching PRs, or adding stacks."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.commands._promote import infer_existing_stack_name, promote_to_multi_workspace
from ow.commands._shared import known_repo_catalog
from ow.commands._targets import UnitSpec, deduplicate_name, parse_unit_spec
from ow.commands._workspace import (
    AddResult,
    UnitMode,
    UnitResult,
    add_into_workspace,
    create_workspace_unit,
    report_result,
    resolve_required_target,
    sync_multi_files,
    upsert_stack,
)
from ow.core import index, stacks
from ow.core.plugins import ResolvedTarget
from ow.core.session import Session, global_settings, open_session
from ow.core.workspace import Workspace, find_root
from ow.errors import UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


def _open_workspace_session(plugins: Plugins, root: Path) -> Session:
    session = open_session(str(root), need_workspace=True, plugins=plugins, cwd=root)
    for warning in session.warnings:
        term.warn(warning)
    assert session.workspace is not None
    return session


def _resolve_units(
    units: Sequence[UnitSpec],
    plugins: Plugins,
    *,
    unnamed_ws: Workspace | None,
    named_ws: Workspace | None,
) -> list[tuple[UnitSpec, ResolvedTarget]]:
    """Resolve each unit with its workspace's settings and plugin enablement.

    `unnamed_ws` serves units added into an existing workspace, `named_ws` the `<name>=<spec>`
    stack units; a `None` workspace means global settings.
    """
    resolved: list[tuple[UnitSpec, ResolvedTarget]] = []
    for unit in units:
        ws = named_ws if unit.name is not None else unnamed_ws
        catalog = known_repo_catalog(plugins, ws)
        settings = ws.settings if ws is not None else global_settings(plugins)
        resolved.append((unit, resolve_required_target(unit.spec, plugins, catalog, settings, ws)))
    return resolved


def _is_empty_mono_workspace(session: Session) -> bool:
    """Whether a mono-stack workspace has no repos and no workspace-level config."""
    return (
        not session.locations
        and not session.accepted_bases
        and not session.rejected_bases
        and not session.workspace_layer.values
        and not session.workspace_layer.unknown
        and not session.workspace_layer.wrong_type
    )


def _apply_mono_specs(
    plugins: Plugins,
    ws_root: Path,
    units: Sequence[UnitSpec],
    *,
    branch: str | None,
    as_name: str | None,
    dry_run: bool,
    yes: bool,
) -> AddResult:
    has_named = any(u.name is not None for u in units)
    has_unnamed = any(u.name is None for u in units)
    if has_named and has_unnamed:
        raise UsageError(
            "cannot mix named stacks (<name>=<spec>) and unnamed repo targets in a single command"
        )

    if not has_named:
        if as_name is not None:
            raise UsageError(
                "--as is only used when promoting a mono-stack workspace with <name>=<spec>"
            )
        session = _open_workspace_session(plugins, ws_root)
        parsed_units = _resolve_units(
            units, plugins, unnamed_ws=session.workspace, named_ws=None
        )
        created_list: list[UnitResult] = []
        exit_code = 0
        for _unit, resolved in parsed_units:
            created, code = add_into_workspace(
                plugins, ws_root, resolved, branch=branch, dry_run=dry_run
            )
            created_list.append(created)
            exit_code = max(exit_code, code)
        return AddResult(
            workspaces=tuple(created_list),
            workspace_root=ws_root,
            multi_stack=False,
            dry_run=dry_run,
            exit_code=exit_code,
        )

    # Named stack(s) inside a mono-stack workspace -> convert empty workspace or promote to multi-stack.
    new_names: list[str] = []
    seen_new: set[str] = set()
    for unit in units:
        assert unit.name is not None
        if unit.name in seen_new:
            raise UsageError(f"duplicate stack name {unit.name!r}")
        seen_new.add(unit.name)
        new_names.append(unit.name)

    session = _open_workspace_session(plugins, ws_root)
    assert session.workspace is not None
    empty_mono = _is_empty_mono_workspace(session)
    if empty_mono and as_name is not None:
        raise UsageError(
            "--as is only used when promoting a non-empty mono-stack workspace to multi-stack"
        )

    parsed_units = _resolve_units(
        units, plugins, unnamed_ws=None, named_ws=session.workspace
    )

    pending: list[dict[str, object]] = []
    existing_name: str | None = None
    if not empty_mono:
        existing_name = infer_existing_stack_name(session, seen_new, explicit=as_name)
        moved_root, promo_code = promote_to_multi_workspace(
            session,
            existing_name=existing_name,
            new_names=new_names,
            dry_run=dry_run,
            yes=yes,
            pending=pending,
        )
        if promo_code != 0 or (not dry_run and moved_root is None):
            return AddResult(
                workspaces=(),
                workspace_root=ws_root,
                multi_stack=False,
                dry_run=dry_run,
                exit_code=promo_code,
            )

    created_list = []
    exit_code = 0
    ws_files: tuple[Path, ...] = ()
    try:
        for unit, resolved in parsed_units:
            assert unit.name is not None
            created, code = create_workspace_unit(
                plugins,
                root=ws_root / unit.name,
                resolved=resolved,
                mode=UnitMode.stack(ws_root, dry_run=dry_run),
                branch=branch,
                pending=pending,
            )
            created_list.append(created)
            exit_code = max(exit_code, code)
    finally:
        if not dry_run:
            if empty_mono and any(stacks.is_stack_config(ws_root / n) for n in new_names):
                (ws_root / ".ow" / "config.toml").unlink(missing_ok=True)
                (ws_root / ".ow" / ".config.toml.lock").unlink(missing_ok=True)
                index.add(ws_root)
            ws_files = sync_multi_files(ws_root, plugins)

    return AddResult(
        workspaces=tuple(created_list),
        workspace_root=ws_root,
        multi_stack=True,
        workspace_files=ws_files,
        promoted_from=existing_name if (not dry_run and existing_name is not None) else None,
        dry_run=dry_run,
        exit_code=exit_code,
    )


def _apply_multi_specs(
    plugins: Plugins,
    multi_root: Path,
    units: Sequence[UnitSpec],
    *,
    default_stack: Path | None,
    taken: set[str],
    branch: str | None,
    dry_run: bool,
) -> AddResult:
    stack_ws = (
        _open_workspace_session(plugins, default_stack).workspace
        if default_stack is not None
        else None
    )
    parsed_units = _resolve_units(units, plugins, unnamed_ws=stack_ws, named_ws=None)
    created_list: list[UnitResult] = []
    exit_code = 0
    ws_files: tuple[Path, ...] = ()
    pending: list[dict[str, object]] = []

    try:
        for unit, resolved in parsed_units:
            if unit.name is not None:
                taken.add(unit.name)
                created, code = upsert_stack(
                    plugins,
                    multi_root,
                    unit.name,
                    resolved,
                    branch=branch,
                    dry_run=dry_run,
                    pending=pending,
                )
            elif default_stack is not None:
                created, code = add_into_workspace(
                    plugins,
                    default_stack,
                    resolved,
                    branch=branch,
                    dry_run=dry_run,
                    pending=pending,
                )
            else:
                stack_name = deduplicate_name(resolved.name, taken)
                created, code = upsert_stack(
                    plugins,
                    multi_root,
                    stack_name,
                    resolved,
                    branch=branch,
                    dry_run=dry_run,
                    pending=pending,
                )
            created_list.append(created)
            exit_code = max(exit_code, code)
    finally:
        if not dry_run:
            index.add(multi_root)
            ws_files = sync_multi_files(multi_root, plugins)

    return AddResult(
        workspaces=tuple(created_list),
        workspace_root=multi_root,
        multi_stack=True,
        workspace_files=ws_files,
        dry_run=dry_run,
        exit_code=exit_code,
    )


def apply_specs_to_existing(
    plugins: Plugins,
    specs: Sequence[str],
    *,
    cwd: Path,
    branch: str | None = None,
    as_name: str | None = None,
    dry_run: bool = False,
    yes: bool = False,
) -> AddResult:
    """Apply `specs` inside an existing mono-stack or multi-stack workspace."""
    ws_root = find_root(cwd)
    multi_stacks = stacks.detect_multi_workspace(cwd) if ws_root is None else None
    if ws_root is None and multi_stacks is None:
        raise UsageError(
            "not inside an ow workspace; run `ow init` first (or `ow run --tmp` for an ephemeral run)"
        )

    units = [parse_unit_spec(raw, allow_run_args=False) for raw in specs]

    # Context 1: Mono-stack workspace (1 level)
    if ws_root is not None and not stacks.is_stack_config(ws_root):
        return _apply_mono_specs(
            plugins,
            ws_root,
            units,
            branch=branch,
            as_name=as_name,
            dry_run=dry_run,
            yes=yes,
        )

    if as_name is not None:
        raise UsageError("--as is only used when promoting a mono-stack workspace to multi-stack")

    # Context 2: Inside a stack of a multi-stack workspace
    if ws_root is not None and stacks.is_stack_config(ws_root):
        multi_root = ws_root.parent.resolve()
        existing = stacks.detect_multi_workspace(multi_root) or ()
        return _apply_multi_specs(
            plugins,
            multi_root,
            units,
            default_stack=ws_root,
            taken={p.name for p in existing},
            branch=branch,
            dry_run=dry_run,
        )

    # Context 3: At a multi-stack workspace root
    assert multi_stacks is not None
    return _apply_multi_specs(
        plugins,
        cwd.resolve(),
        units,
        default_stack=None,
        taken={p.name for p in multi_stacks},
        branch=branch,
        dry_run=dry_run,
    )


def run(
    plugins: Plugins,
    targets: Sequence[str],
    *,
    branch: str | None = None,
    as_name: str | None = None,
    dry_run: bool = False,
    yes: bool = False,
    cwd: Path | None = None,
    json_output: bool = False,
) -> int:
    """Add repos, attach PRs, or add stacks inside an existing workspace."""
    if not targets:
        raise UsageError("pass at least one target (e.g. upgrade, enterprise#12345, iap=iap#4321)")

    base_cwd = (cwd or Path.cwd()).resolve()
    with term.json_mode(json_output):
        result = apply_specs_to_existing(
            plugins,
            targets,
            cwd=base_cwd,
            branch=branch,
            as_name=as_name,
            dry_run=dry_run,
            yes=yes,
        )
        return report_result(result, json_output=json_output, create_verb="ready")
