"""`ow add` — create one or more workspaces from PR targets, bases, or explicit repos."""

from __future__ import annotations

import contextlib
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
    run_steps,
)
from ow.commands._targets import expand_repos, resolve_composite_target
from ow.core import config, git, index, tasks
from ow.core.json_schema import envelope
from ow.core.paths import repos_dir
from ow.core.plugins import KnownRepo, ResolvedRepo, ResolvedTarget
from ow.core.session import global_settings, open_session
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


def _repo_url(name: str, explicit_url: str | None, catalog: dict[str, KnownRepo]) -> str | None:
    if explicit_url:
        return explicit_url
    known = catalog.get(name)
    return known.url if known else None


def _default_fresh_repos(settings: dict[str, object], catalog: dict[str, KnownRepo]) -> tuple[str, ...]:
    """Default repo list for a fresh workspace (`ow add my-fix --base 18.0`) when `--repo` is omitted."""
    if not settings.get("odoo.enabled", True):
        return ()
    raw = settings.get("odoo.default_repos")
    if isinstance(raw, list):
        return tuple(dict.fromkeys(item for item in raw if isinstance(item, str) and item))
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
    settings = global_settings(plugins)
    declared = {s.key: s for s in plugins.settings()}

    if resolved is None:
        resolved = resolve_composite_target(target, plugins, catalog, settings)

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
            known = catalog.get(extra)
            if extra not in existing_names:
                repo_specs.append(
                    ResolvedRepo(
                        name=extra,
                        url=known.url if known else None,
                        branch=branch,
                        base=shared_base,
                    )
                )
                existing_names.add(extra)
                added_extra = True
        if added_extra:
            repo_specs = list(expand_repos(repo_specs, plugins, catalog, settings))
    else:
        ws_name = tasks.validate_name(name or target, kind="workspace")
        source = None
        metadata = {}
        raw_repos = list(repos) if repos else list(_default_fresh_repos(settings, catalog))
        repo_specs = []
        seen: set[str] = set()
        for rname in raw_repos:
            if rname in seen:
                continue
            seen.add(rname)
            known = catalog.get(rname)
            repo_specs.append(
                ResolvedRepo(
                    name=rname,
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
                raise OwError(f"failed to add repository {spec.name!r} for workspace {ws_name!r}")
            added_worktrees.append((bare, wt_path))
            accepted[spec.name] = effective_base

        ws_values: dict[str, object] = {}
        assigned_port: int | None = None
        assigned_gevent_port: int | None = None

        # Hold the task flock across port allocation + config write + index registration
        # so concurrent `ow add -t <task>` calls cannot allocate the same ports.
        lock_cm = tasks.task_flock(task) if task is not None else contextlib.nullcontext()
        with lock_cm:
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
        return created, refreshed
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
