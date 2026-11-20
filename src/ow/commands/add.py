"""`ow add` — create one or more workspaces from PR targets, bases, or explicit repos."""

from __future__ import annotations

import contextlib
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

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
from ow.core.workspace import derive_repos, stray_checkouts
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


def _repo_url(name: str, explicit_url: str | None, catalog: Mapping[str, KnownRepo]) -> str | None:
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


def _is_resumable_workspace(root: Path) -> bool:
    """Whether an existing `root` is an ow workspace (complete or interrupted mid-`ow add`).

    Plain non-ow directories (empty or holding unrelated files) return False so `ow add`
    never clobbers or adopts an arbitrary directory.
    """
    if not root.is_dir():
        return False
    if (root / ".ow" / "config.toml").exists() or (root / ".ow").is_dir():
        return True
    locations = derive_repos(root)
    return bool(locations) and not stray_checkouts(root, locations)


def _worktree_state(bare: Path, wt_path: Path) -> Literal["missing", "healthy", "broken", "foreign"]:
    """Classify an existing worktree path for idempotent `ow add`."""
    if not wt_path.exists() and not wt_path.is_symlink():
        return "missing"
    if wt_path.is_symlink() or not wt_path.is_dir():
        return "foreign"

    git_entry = wt_path / ".git"
    if not git_entry.exists() and not git_entry.is_symlink():
        try:
            has_entries = any(wt_path.iterdir())
        except OSError:
            return "foreign"
        return "foreign" if has_entries else "broken"
    if git_entry.is_dir() or not git_entry.is_file():
        return "foreign"

    try:
        content = git_entry.read_text(encoding="utf-8").strip()
    except OSError:
        return "broken"
    if not content.startswith("gitdir: "):
        return "foreign"

    gitdir_str = content[len("gitdir: "):].strip()
    gitdir = Path(gitdir_str) if os.path.isabs(gitdir_str) else (wt_path / gitdir_str)
    try:
        gitdir = gitdir.resolve()
        bare_resolved = bare.resolve()
    except OSError:
        return "broken"

    if gitdir.parent.name != "worktrees" or gitdir.parent.parent != bare_resolved:
        return "foreign"
    if not gitdir.is_dir():
        return "broken"

    stored_gitdir_file = gitdir / "gitdir"
    if not stored_gitdir_file.is_file():
        return "broken"
    try:
        stored_target = stored_gitdir_file.read_text(encoding="utf-8").strip()
        if (gitdir / stored_target).resolve() != git_entry.resolve():
            return "broken"
    except OSError:
        return "broken"

    locked_file = gitdir / "locked"
    if locked_file.is_file():
        try:
            if locked_file.read_text(encoding="utf-8").strip().startswith("initializing"):
                return "broken"
        except OSError:
            return "broken"

    head_check = git.run(["git", "-C", str(wt_path), "rev-parse", "--verify", "--quiet", "HEAD"])
    if head_check.returncode != 0:
        return "broken"
    return "healthy"


def _cleanup_worktree(bare: Path, wt: Path) -> None:
    """Best-effort removal of a worktree, including one left locked `initializing`."""
    try:
        git.run(["git", "-C", str(bare), "worktree", "unlock", str(wt)])
    except Exception:
        pass
    try:
        git.run(["git", "-C", str(bare), "worktree", "remove", "--force", str(wt)])
    except Exception:
        pass
    if wt.exists() or wt.is_symlink():
        shutil.rmtree(wt, ignore_errors=True)
    try:
        git.run(["git", "-C", str(bare), "worktree", "prune"])
    except Exception:
        pass


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
        _cleanup_worktree(bare, wt)
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
    """Create (or idempotently resume/update) a workspace from a resolved target or name."""
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

    existed_before = root.exists()
    if existed_before and not _is_resumable_workspace(root):
        raise UsageError(f"directory already exists: {root}")

    # Validate every repo can be located (and no foreign directory collides) before mutating disk.
    for spec in repo_specs:
        bare = repos_dir() / f"{spec.name}.git"
        if not bare.is_dir() and _repo_url(spec.name, spec.url, catalog) is None:
            raise UsageError(
                f"unknown repo {spec.name!r}; pass a URL first with `ow repo add {spec.name} <url>`"
            )
        if existed_before:
            wt_path = root / spec.name
            if (wt_path.exists() or wt_path.is_symlink()) and _worktree_state(bare, wt_path) == "foreign":
                raise UsageError(
                    f"{wt_path} already exists and is not a worktree of {spec.name}"
                )

    config_path = root / ".ow" / "config.toml"
    if existed_before and config_path.exists():
        existing_layer, existing_accepted, _rejected, existing_meta = config.read_workspace_config(
            config_path, declared, remotes_of
        )
        accepted: dict[str, str] = dict(existing_accepted)
        ws_values: dict[str, object] = dict(existing_layer.values)
        effective_task = task if task is not None else existing_meta.task
    else:
        accepted = {}
        ws_values = {}
        effective_task = task

    if not existed_before:
        root.mkdir(parents=True, exist_ok=False)

    created_ok = False
    indexed = False
    added_worktrees: list[tuple[Path, Path]] = []
    in_progress: tuple[Path, Path] | None = None
    try:
        # Write `.ow/config.toml` early so even an interrupted `ow add` leaves a valid
        # workspace that `ow refresh`, `ow status`, `ow rm`, and a retried `ow add` recognize.
        if not config_path.exists():
            config.write_workspace(
                config_path,
                accepted,
                ws_values,
                declared=declared,
                remotes_of=remotes_of,
                task=effective_task,
            )

        for spec in repo_specs:
            url = _repo_url(spec.name, spec.url, catalog)
            bare = repos_dir() / f"{spec.name}.git"
            if not bare.is_dir():
                assert url is not None
                ensure_bare(spec.name, url)

            effective_base_raw = base or spec.base
            if effective_base_raw is not None:
                effective_base = parse_base_for(effective_base_raw, spec.name)
            elif spec.name in accepted:
                effective_base = accepted[spec.name]
            else:
                effective_base, base_src = resolve_base(spec.name, None, accepted)
                term.out(f"# {spec.name}: default base {effective_base} ({base_src})")

            wt_path = root / spec.name
            state = _worktree_state(bare, wt_path)
            if state == "healthy":
                term.out(f"# {spec.name}: already exists")
                if base is not None or spec.name not in accepted:
                    accepted[spec.name] = effective_base
                continue
            if state == "foreign":
                raise UsageError(
                    f"{wt_path} already exists and is not a worktree of {spec.name}"
                )
            if state == "broken":
                term.warn(f"{spec.name}: cleaning up incomplete worktree at {wt_path}")
                _cleanup_worktree(bare, wt_path)

            effective_branch = None if detach else (branch if branch is not None else spec.branch)
            ow_pr_val = spec.metadata.get("ow_pr")
            ow_pr = ow_pr_val if isinstance(ow_pr_val, str) else None
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
            in_progress = (bare, wt_path)
            if run_steps(spec.name, steps, root=root) != 0:
                raise OwError(f"failed to add repository {spec.name!r} for workspace {ws_name!r}")
            in_progress = None
            added_worktrees.append((bare, wt_path))
            accepted[spec.name] = effective_base
            # Checkpoint progress into `.ow/config.toml` after each completed worktree.
            config.write_workspace(
                config_path,
                accepted,
                ws_values,
                declared=declared,
                remotes_of=remotes_of,
                task=effective_task,
            )

        assigned_port: int | None = None
        assigned_gevent_port: int | None = None

        # Hold the task flock across port allocation + config write + index registration
        # so concurrent `ow add -t <task>` calls cannot allocate the same ports.
        lock_cm = tasks.task_flock(effective_task) if effective_task is not None else contextlib.nullcontext()
        with lock_cm:
            if effective_task is not None and settings.get("odoo.enabled", True):
                if "odoo.http_port" not in ws_values:
                    base_port_raw = settings.get("odoo.http_port", 8069)
                    base_port = (
                        base_port_raw
                        if isinstance(base_port_raw, int) and not isinstance(base_port_raw, bool)
                        else 8069
                    )
                    assigned_port = tasks.next_free_port(effective_task, plugins, base_port=base_port)
                    ws_values["odoo.http_port"] = assigned_port
                else:
                    existing_http = ws_values["odoo.http_port"]
                    if isinstance(existing_http, int) and not isinstance(existing_http, bool):
                        assigned_port = existing_http

                if "odoo.gevent_port" not in ws_values:
                    base_gevent_raw = settings.get("odoo.gevent_port", 8072)
                    base_gevent = (
                        base_gevent_raw
                        if isinstance(base_gevent_raw, int) and not isinstance(base_gevent_raw, bool)
                        else 8072
                    )
                    extra = (assigned_port,) if assigned_port is not None else ()
                    assigned_gevent_port = tasks.next_free_port(
                        effective_task, plugins, base_port=base_gevent, extra_used=extra
                    )
                    ws_values["odoo.gevent_port"] = assigned_gevent_port
                else:
                    existing_gevent = ws_values["odoo.gevent_port"]
                    if isinstance(existing_gevent, int) and not isinstance(existing_gevent, bool):
                        assigned_gevent_port = existing_gevent

            dropped = config.write_workspace(
                config_path,
                accepted,
                ws_values,
                declared=declared,
                remotes_of=remotes_of,
                task=effective_task,
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
            task=effective_task,
            repos=tuple(r.name for r in session.workspace.repos),
            http_port=final_port if isinstance(final_port, int) and not isinstance(final_port, bool) else assigned_port,
            gevent_port=final_gevent if isinstance(final_gevent, int) and not isinstance(final_gevent, bool) else assigned_gevent_port,
            source=source,
            metadata=metadata,
        )
        return created, refreshed
    except BaseException:
        if not created_ok:
            if not existed_before:
                to_clean = list(added_worktrees)
                if in_progress is not None:
                    to_clean.append(in_progress)
                _rollback_workspace(root, to_clean, indexed=indexed)
            elif in_progress is not None:
                _cleanup_worktree(*in_progress)
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
