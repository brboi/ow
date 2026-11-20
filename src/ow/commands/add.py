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
from ow.core import config, git, index, tasks
from ow.core.json_schema import envelope
from ow.core.paths import global_config, repos_dir
from ow.core.plugins import KnownRepo, ResolvedRepo, ResolvedTarget
from ow.core.session import open_session
from ow.errors import OwError, UsageError

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
    gevent_port: int | None = None
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


def _expand_repos(
    repos: Sequence[ResolvedRepo],
    plugins: Plugins,
    catalog: dict[str, KnownRepo],
    settings: dict[str, object],
) -> tuple[ResolvedRepo, ...]:
    repos_tuple = tuple(repos)
    for _plugin, expanded in plugins.call(
        "ow_expand_workspace_repos",
        repos=repos_tuple,
        known_repos=catalog,
        settings=settings,
    ):
        if expanded is not None:
            repos_tuple = tuple(expanded)
    return repos_tuple


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
    unknown_parts: list[str] = []

    for part in parts:
        res = _resolve_single_part(part, plugins, catalog, settings)
        if res is not None:
            resolved_parts.append(res)
        elif len(parts) > 1:
            known = resolve_known_repo(part, catalog)
            if known is not None:
                extra_repo_names.append(known.name)
            elif (repos_dir() / f"{part}.git").is_dir():
                extra_repo_names.append(part)
            else:
                unknown_parts.append(part)
        else:
            return None

    if not resolved_parts:
        # If every `+`-separated piece was a known repo (e.g. `community+enterprise`),
        # warn clearly instead of creating a workspace literally named `community+enterprise`.
        if len(parts) > 1 and not unknown_parts and extra_repo_names:
            raise UsageError(
                f"composite target {spec!r} needs at least one PR/target (e.g. enterprise#12345+community); "
                "for fresh workspaces pass -r <repo>"
            )
        # Otherwise treat as a normal fresh workspace name containing `+` (e.g. `fix-a+b`).
        return None

    if unknown_parts:
        raise UsageError(f"unknown target or repo {unknown_parts[0]!r} in {spec!r}")

    # Merge repos giving explicit primary targets precedence over auto-discovered sister PRs.
    merged_repos: dict[str, ResolvedRepo] = {}
    sources: list[str] = []
    for res in resolved_parts:
        sources.append(res.source)
        for repo in res.repos[1:]:
            merged_repos.setdefault(repo.name, repo)
    for res in resolved_parts:
        if res.repos:
            merged_repos[res.repos[0].name] = res.repos[0]

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

    repos_tuple = _expand_repos(tuple(merged_repos.values()), plugins, catalog, settings)

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


def _rollback_workspace(
    root: Path,
    added_worktrees: Sequence[tuple[Path, Path]],
    *,
    indexed: bool,
) -> None:
    """Clean up partially created worktrees, index entry, and workspace directory."""
    if indexed:
        try:
            index.remove(root)
        except Exception:
            pass
    for bare, wt in reversed(added_worktrees):
        try:
            git.run(["git", "-C", str(bare), "worktree", "remove", "--force", str(wt)])
            git.run(["git", "-C", str(bare), "worktree", "prune"])
        except Exception:
            pass
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


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
    resolved: ResolvedTarget | None = None,
    detach: bool = False,
) -> tuple[CreatedWorkspace, int]:
    """Create a single workspace from a resolved target (PR, etc.) or fresh workspace name."""
    if task is not None:
        task = tasks.validate_name(task, kind="task")

    catalog = known_repo_catalog(plugins)
    settings = _global_settings(plugins)
    declared = {s.key: s for s in plugins.settings()}

    if resolved is None:
        resolved = _resolve_composite_target(target, plugins, catalog, settings)

    if resolved is not None:
        ws_name = tasks.validate_name(name or resolved.name, kind="workspace")
        repo_specs = list(resolved.repos)
        source = resolved.source
        metadata = dict(resolved.metadata)
        # Allow adding extra explicit --repo items on top of a resolved target
        existing_names = {r.name for r in repo_specs}
        shared_base = base or next((r.base for r in repo_specs if r.base), None)
        added_extra = False
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
                added_extra = True
        if added_extra:
            repo_specs = list(_expand_repos(repo_specs, plugins, catalog, settings))
    else:
        ws_name = tasks.validate_name(name or target, kind="workspace")
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
    if root.parent != parent_dir.resolve():
        raise UsageError(f"invalid workspace name {ws_name!r}")
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
    indexed = False
    added_worktrees: list[tuple[Path, Path]] = []
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

            effective_branch = None if detach else (branch if branch is not None else spec.branch)
            ow_pr_val = spec.metadata.get("ow_pr")
            ow_pr = ow_pr_val if isinstance(ow_pr_val, str) else None
            wt_path = root / spec.name
            steps = add_worktree(
                bare,
                wt_path,
                branch=effective_branch,
                base=effective_base,
                fetch_ref=spec.fetch_ref,
                remote_hint=spec.remote,
                ow_pr=ow_pr,
                detach=detach,
            )
            if run_steps(spec.name, steps, root=root) != 0:
                failed = True
                continue
            added_worktrees.append((bare, wt_path))
            accepted[spec.name] = effective_base

        if repo_specs and not accepted:
            raise OwError(f"failed to add any repository for workspace {ws_name!r}")

        ws_values: dict[str, object] = {}
        assigned_port: int | None = None
        assigned_gevent_port: int | None = None
        if task is not None and settings.get("odoo.enabled", True):
            base_port_raw = settings.get("odoo.http_port", 8069)
            base_port = (
                base_port_raw
                if isinstance(base_port_raw, int) and not isinstance(base_port_raw, bool)
                else 8069
            )
            base_gevent_raw = settings.get("odoo.gevent_port", 8072)
            base_gevent = (
                base_gevent_raw
                if isinstance(base_gevent_raw, int) and not isinstance(base_gevent_raw, bool)
                else 8072
            )
            assigned_port = tasks.next_free_port(task, plugins, base_port=base_port)
            assigned_gevent_port = tasks.next_free_port(
                task, plugins, base_port=base_gevent, extra_used=(assigned_port,)
            )
            ws_values["odoo.http_port"] = assigned_port
            ws_values["odoo.gevent_port"] = assigned_gevent_port

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
            indexed = True

        session = open_session(str(root), need_workspace=True, plugins=plugins)
        assert session.workspace is not None
        for warning in session.warnings:
            term.warn(warning)

        plugins.call("ow_workspace_created", ws=session.workspace)
        refreshed = refresh_workspace(session)
        created_ok = True

        final_port = session.workspace.settings.get("odoo.http_port")
        final_gevent = session.workspace.settings.get("odoo.gevent_port")
        created = CreatedWorkspace(
            name=session.workspace.name,
            root=root,
            task=task,
            repos=tuple(r.name for r in session.workspace.repos),
            http_port=final_port if isinstance(final_port, int) and not isinstance(final_port, bool) else assigned_port,
            gevent_port=final_gevent if isinstance(final_gevent, int) and not isinstance(final_gevent, bool) else assigned_gevent_port,
            source=source,
            metadata=metadata,
        )
        return created, max(1 if failed else 0, refreshed)
    except BaseException:
        if not created_ok:
            _rollback_workspace(root, added_worktrees, indexed=indexed)
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
    if task is not None:
        task = tasks.validate_name(task, kind="task")

    parent = (cwd or Path.cwd()).resolve()

    with term.json_mode(json_output):
        created_list: list[CreatedWorkspace] = []
        exit_code = 0
        task_files: tuple[Path, ...] = ()

        try:
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
        finally:
            if task is not None and created_list:
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
