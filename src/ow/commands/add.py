"""`ow add` — create one or more workspaces from PR targets, bases, or explicit repos."""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ow import term
from ow.commands._repos import add_worktree, ensure_bare, parse_base_for, resolve_base
from ow.commands._shared import (
    known_repo_catalog,
    print_dropped,
    refresh_workspace,
    remotes_of,
    resolve_known_repo,
    run_steps,
)
from ow.core import config, index, tasks
from ow.core.json_schema import envelope
from ow.core.paths import global_config, repos_dir
from ow.core.plugins import KnownRepo, ResolvedRepo, ResolvedTarget
from ow.core.session import open_session
from ow.errors import UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


@dataclass(frozen=True)
class CreatedWorkspace:
    """Result for one workspace created by `ow add`."""

    name: str
    root: Path
    task: str | None
    repos: tuple[str, ...]
    http_port: int | None = None
    source: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AddResult:
    """Typed result returned by `ow add`."""

    workspaces: tuple[CreatedWorkspace, ...]
    task: str | None = None
    task_files: tuple[Path, ...] = ()
    exit_code: int = 0


def _global_settings(plugins: Plugins) -> dict[str, object]:
    """Resolve effective global settings (defaults + global layer) without a workspace."""
    declared = {s.key: s for s in plugins.settings()}
    layer = config.read_global(global_config(), declared)
    values, _origins = config.resolve_settings(
        declared,
        layer,
        config.Layer(values={}, unknown={}, wrong_type=()),
        ws_for_defaults=None,
    )
    return values


def _resolve_single_part(
    part: str,
    plugins: Plugins,
    catalog: dict[str, KnownRepo],
    settings: dict[str, object],
) -> ResolvedTarget | None:
    """Ask plugins to resolve `part`; return the first non-None ResolvedTarget."""
    for _plugin, answer in plugins.call(
        "ow_resolve_target",
        target=part,
        known_repos=catalog,
        settings=settings,
    ):
        if isinstance(answer, ResolvedTarget):
            return answer
    return None


def _resolve_composite_target(
    spec: str,
    plugins: Plugins,
    catalog: dict[str, KnownRepo],
    settings: dict[str, object],
) -> ResolvedTarget | None:
    """Resolve a target or `+`-joined composite target (e.g. `iap-apps#42+enterprise`)."""
    parts = [p.strip() for p in spec.split("+") if p.strip()]
    if not parts:
        return None

    resolved_parts: list[ResolvedTarget] = []
    extra_repo_names: list[str] = []

    for part in parts:
        res = _resolve_single_part(part, plugins, catalog, settings)
        if res is not None:
            resolved_parts.append(res)
        elif len(parts) > 1:
            # In a composite spec like `iap-apps#42+enterprise`, bare repo names are allowed.
            known = resolve_known_repo(part, catalog)
            if known is not None:
                extra_repo_names.append(known.name)
            elif (repos_dir() / f"{part}.git").is_dir():
                extra_repo_names.append(part)
            else:
                raise UsageError(f"unknown target or repo {part!r} in {spec!r}")
        else:
            return None

    if not resolved_parts:
        return None

    merged_repos: dict[str, ResolvedRepo] = {}
    sources: list[str] = []
    for res in resolved_parts:
        sources.append(res.source)
        for repo in res.repos:
            merged_repos[repo.name] = repo

    shared_base = next((r.base for r in merged_repos.values() if r.base), None)
    for name in extra_repo_names:
        if name not in merged_repos:
            known = catalog.get(name)
            merged_repos[name] = ResolvedRepo(
                name=name,
                url=known.url if known else None,
                branch=None,
                base=shared_base,
            )

    repos_tuple = tuple(merged_repos.values())
    for _plugin, expanded in plugins.call(
        "ow_expand_workspace_repos",
        repos=repos_tuple,
        known_repos=catalog,
        settings=settings,
    ):
        if expanded is not None:
            repos_tuple = tuple(expanded)

    return ResolvedTarget(
        name=resolved_parts[0].name,
        repos=repos_tuple,
        source="+".join(sources),
        metadata=dict(resolved_parts[0].metadata),
    )


def _repo_url(name: str, explicit_url: str | None, catalog: dict[str, KnownRepo]) -> str | None:
    if explicit_url:
        return explicit_url
    known = resolve_known_repo(name, catalog)
    return known.url if known else None


def _default_fresh_repos(settings: dict[str, object], catalog: dict[str, KnownRepo]) -> tuple[str, ...]:
    """Default repo list for a fresh workspace (`ow add my-fix --base 18.0`) when `--repo` is omitted."""
    if not settings.get("odoo.enabled", True):
        return ()
    raw = settings.get("odoo.default_repos")
    if isinstance(raw, list):
        resolved: list[str] = []
        for item in raw:
            if not isinstance(item, str) or not item:
                continue
            known = resolve_known_repo(item, catalog)
            resolved.append(known.name if known else item)
        return tuple(dict.fromkeys(resolved))
    return ("community", "enterprise")


def create_workspace(
    plugins: Plugins,
    *,
    parent_dir: Path,
    target: str,
    name: str | None = None,
    task: str | None = None,
    base: str | None = None,
    branch: str | None = None,
    repos: Sequence[str] = (),
    register_index: bool = True,
) -> tuple[CreatedWorkspace, int]:
    """Create a single workspace from a resolved target (PR, etc.) or fresh workspace name."""
    catalog = known_repo_catalog(plugins)
    settings = _global_settings(plugins)
    declared = {s.key: s for s in plugins.settings()}

    resolved = _resolve_composite_target(target, plugins, catalog, settings)

    if resolved is not None:
        ws_name = name or resolved.name
        repo_specs = list(resolved.repos)
        source = resolved.source
        metadata = dict(resolved.metadata)
        # Allow adding extra explicit --repo items on top of a resolved target
        existing_names = {r.name for r in repo_specs}
        shared_base = base or next((r.base for r in repo_specs if r.base), None)
        for extra in repos:
            known = resolve_known_repo(extra, catalog)
            canonical = known.name if known else extra
            if canonical not in existing_names:
                repo_specs.append(
                    ResolvedRepo(
                        name=canonical,
                        url=known.url if known else None,
                        branch=branch,
                        base=shared_base,
                    )
                )
                existing_names.add(canonical)
    else:
        ws_name = name or target
        source = None
        metadata = {}
        raw_repos = list(repos) if repos else list(_default_fresh_repos(settings, catalog))
        repo_specs = []
        seen: set[str] = set()
        for rname in raw_repos:
            known = resolve_known_repo(rname, catalog)
            canonical = known.name if known else rname
            if canonical in seen:
                continue
            seen.add(canonical)
            repo_specs.append(
                ResolvedRepo(
                    name=canonical,
                    url=known.url if known else None,
                    branch=branch,
                    base=base,
                )
            )

    root = (parent_dir / ws_name).resolve()
    if root.exists():
        raise UsageError(f"directory already exists: {root}")

    # Validate every repo can be located before creating the directory.
    for spec in repo_specs:
        bare = repos_dir() / f"{spec.name}.git"
        if not bare.is_dir() and _repo_url(spec.name, spec.url, catalog) is None:
            raise UsageError(
                f"unknown repo {spec.name!r}; pass a URL first with `ow repo add {spec.name} <url>`"
            )

    root.mkdir(parents=True, exist_ok=False)
    created_ok = False
    try:
        accepted: dict[str, str] = {}
        failed = False

        for spec in repo_specs:
            url = _repo_url(spec.name, spec.url, catalog)
            bare = repos_dir() / f"{spec.name}.git"
            if not bare.is_dir():
                assert url is not None
                ensure_bare(spec.name, url)

            effective_base_raw = base or spec.base
            if effective_base_raw is not None:
                effective_base = parse_base_for(effective_base_raw, spec.name)
            else:
                effective_base, base_src = resolve_base(spec.name, None, accepted)
                term.out(f"# {spec.name}: default base {effective_base} ({base_src})")

            effective_branch = branch if branch is not None else spec.branch
            steps = add_worktree(
                bare,
                root / spec.name,
                branch=effective_branch,
                base=effective_base,
                fetch_ref=spec.fetch_ref,
                remote_hint=spec.remote,
            )
            if run_steps(spec.name, steps, root=root) != 0:
                failed = True
                continue
            accepted[spec.name] = effective_base

        ws_values: dict[str, object] = {}
        assigned_port: int | None = None
        if task is not None and settings.get("odoo.enabled", True):
            base_port_raw = settings.get("odoo.http_port", 8069)
            base_port = int(base_port_raw) if isinstance(base_port_raw, int) else 8069
            assigned_port = tasks.next_free_port(task, plugins, base_port=base_port)
            ws_values["odoo.http_port"] = assigned_port

        dropped = config.write_workspace(
            root / ".ow" / "config.toml",
            accepted,
            ws_values,
            declared=declared,
            remotes_of=remotes_of,
            task=task,
        )
        print_dropped(dropped)

        if register_index:
            index.add(root)

        session = open_session(str(root), need_workspace=True, plugins=plugins)
        assert session.workspace is not None
        for warning in session.warnings:
            term.warn(warning)

        plugins.call("ow_workspace_created", ws=session.workspace)
        refreshed = refresh_workspace(session)
        created_ok = True

        final_port = session.workspace.settings.get("odoo.http_port")
        created = CreatedWorkspace(
            name=session.workspace.name,
            root=root,
            task=task,
            repos=tuple(r.name for r in session.workspace.repos),
            http_port=final_port if isinstance(final_port, int) else assigned_port,
            source=source,
            metadata=metadata,
        )
        return created, max(1 if failed else 0, refreshed)
    except BaseException:
        if not created_ok and root.exists():
            shutil.rmtree(root, ignore_errors=True)
        raise


def run(
    plugins: Plugins,
    targets: Sequence[str],
    *,
    name: str | None = None,
    task: str | None = None,
    base: str | None = None,
    branch: str | None = None,
    repos: Sequence[str] = (),
    cwd: Path | None = None,
    json_output: bool = False,
) -> int:
    """Create one or more workspaces; returns exit code (0 on success, 1 on partial failure)."""
    if not targets:
        raise UsageError("pass at least one target (PR like enterprise#12345 or workspace name)")
    if name is not None and len(targets) > 1:
        raise UsageError("--name can only be used with a single target")

    parent = (cwd or Path.cwd()).resolve()

    with term.json_mode(json_output):
        created_list: list[CreatedWorkspace] = []
        exit_code = 0

        for target in targets:
            created, code = create_workspace(
                plugins,
                parent_dir=parent,
                target=target,
                name=name,
                task=task,
                base=base,
                branch=branch,
                repos=repos,
            )
            created_list.append(created)
            exit_code = max(exit_code, code)

        task_files: tuple[Path, ...] = ()
        if task is not None:
            loaded_task = tasks.find_task(task, plugins)
            if loaded_task is not None:
                task_files = tasks.sync_task_files(loaded_task, plugins)

        result = AddResult(
            workspaces=tuple(created_list),
            task=task,
            task_files=task_files,
            exit_code=exit_code,
        )

        if json_output:
            term.emit_json(envelope({"result": result}))
        else:
            for ws in created_list:
                port_note = f" (port {ws.http_port})" if task and ws.http_port else ""
                term.out(f"created {ws.name} at {ws.root}{port_note}")
            for tf in task_files:
                term.out(f"updated {tf}")

        return exit_code
