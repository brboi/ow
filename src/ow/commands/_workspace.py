"""Shared workspace/stack creation, worktree reconciliation, and result reporting service."""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ow import term
from ow.commands._repos import (
    add_worktree,
    attach_pr_to_worktree,
    cleanup_worktree,
    ensure_bare,
    resolve_base,
    worktree_state,
)
from ow.commands._shared import known_repo_catalog, print_dropped, refresh_workspace, remotes_of, run_steps
from ow.commands._specs import SPEC_USAGE_HINT, resolve_composite_spec
from ow.core import config, git, index, stacks
from ow.core.bases import parse_base_for
from ow.core.paths import repos_dir
from ow.core.plan import Step
from ow.core.plugins import KnownRepo, ResolvedRepo, ResolvedSpec, StackInfo
from ow.core.session import Session, global_settings, open_session
from ow.core.workspace import Workspace, build_workspace, derive_repos, stray_checkouts
from ow.errors import OwError, UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


@dataclass(frozen=True)
class UnitMode:
    """Configuration modes governing how `create_workspace_unit` creates or updates a unit."""

    is_stack: bool = False
    parent_multi_root: Path | None = None
    register_index: bool = True
    detach: bool = False
    allow_existing_dir: bool = False
    allow_attach: bool = False
    dry_run: bool = False

    @classmethod
    def mono(
        cls,
        *,
        allow_existing_dir: bool = False,
        allow_attach: bool = False,
        dry_run: bool = False,
    ) -> UnitMode:
        return cls(
            is_stack=False,
            parent_multi_root=None,
            register_index=True,
            detach=False,
            allow_existing_dir=allow_existing_dir,
            allow_attach=allow_attach,
            dry_run=dry_run,
        )

    @classmethod
    def stack(
        cls,
        parent_multi_root: Path,
        *,
        allow_existing_dir: bool = False,
        allow_attach: bool = False,
        dry_run: bool = False,
    ) -> UnitMode:
        return cls(
            is_stack=True,
            parent_multi_root=parent_multi_root.resolve(),
            register_index=False,
            detach=False,
            allow_existing_dir=allow_existing_dir,
            allow_attach=allow_attach,
            dry_run=dry_run,
        )

    @classmethod
    def ephemeral(cls, *, is_stack: bool = False, parent_multi_root: Path | None = None) -> UnitMode:
        return cls(
            is_stack=is_stack,
            parent_multi_root=parent_multi_root.resolve() if parent_multi_root is not None else None,
            register_index=False,
            detach=True,
            allow_existing_dir=False,
            allow_attach=False,
            dry_run=False,
        )


@dataclass(frozen=True)
class UnitResult:
    """Result for one workspace or stack created, updated, unchanged, or planned."""

    name: str
    root: Path
    is_stack: bool
    action: Literal["created", "updated", "unchanged", "planned"] = "created"
    note: str | None = None


@dataclass(frozen=True)
class AddResult:
    """Typed result returned by `ow init` and `ow add`."""

    workspaces: tuple[UnitResult, ...]
    workspace_files: tuple[Path, ...] = ()
    exit_code: int = 0


def repo_url(name: str, explicit_url: str | None, catalog: Mapping[str, KnownRepo]) -> str | None:
    """Return `explicit_url` or look up `name` in `catalog`."""
    if explicit_url:
        return explicit_url
    known = catalog.get(name)
    return known.url if known else None


def is_resumable_workspace(root: Path) -> bool:
    """Whether an existing `root` is an ow workspace or stack (complete or interrupted mid-creation)."""
    if not root.is_dir():
        return False
    if (root / ".ow" / "config.toml").is_file() or stacks.is_stack_dir(root):
        return True
    locations = derive_repos(root)
    return bool(locations) and not stray_checkouts(root, locations)


def resolve_required_spec(
    spec: str,
    plugins: Plugins,
    catalog: Mapping[str, KnownRepo],
    settings: Mapping[str, object],
    ws: Workspace | None = None,
) -> ResolvedSpec:
    """Resolve `spec` via plugins/branch/repo rules or raise `UsageError` with syntax hints."""
    resolved = resolve_composite_spec(spec, plugins, catalog, settings, ws)
    if resolved is None:
        raise UsageError(f"unknown spec or repo {spec!r}; {SPEC_USAGE_HINT}")
    return resolved


def stack_info(ws: Workspace, plugins: Plugins) -> StackInfo:
    """Merge the `ow_stack_info` answers for `ws` (empty when no plugin answers)."""
    ports: dict[str, int] = {}
    notes: list[str] = []
    for _plugin, answer in plugins.call("ow_stack_info", ws=ws):
        if isinstance(answer, StackInfo):
            ports.update(answer.ports)
            if answer.note:
                notes.append(answer.note)
    return StackInfo(ports=ports, note=", ".join(notes) or None)


def pin_stack_values(
    plugins: Plugins,
    ws_values: dict[str, object],
    *,
    ws: Workspace | None,
    siblings: Sequence[Workspace],
    pending: list[dict[str, object]] | None,
    global_settings: Mapping[str, object],
    repo_paths: Sequence[Path] | None = None,
    repo_names: Sequence[str] | None = None,
) -> dict[str, object]:
    """Merge the `ow_stack_values` answers into `ws_values` (explicit values win).

    Returns the newly pinned settings. When `pending` is given, the stack's resulting settings
    are appended to it so stacks planned later in the same command see them.
    """
    pinned: dict[str, object] = {}
    for _plugin, contrib in plugins.call(
        "ow_stack_values",
        ws=ws,
        ws_values=ws_values,
        siblings=siblings,
        pending=tuple(pending or ()),
        global_settings=global_settings,
        repo_paths=repo_paths,
        repo_names=repo_names,
    ):
        if isinstance(contrib, Mapping):
            for key, value in contrib.items():
                if key not in ws_values:
                    ws_values[key] = value
                    pinned[key] = value
    if pending is not None:
        pending.append(dict(ws_values))
    return pinned


def sync_multi_files(multi_root: Path, plugins: Plugins) -> tuple[Path, ...]:
    """Sync workspace-level generated files (e.g. `.code-workspace`) for `multi_root`."""
    loaded = stacks.load_multi_workspace(multi_root, plugins)
    if loaded is None:
        return ()
    multi, sessions = loaded
    return stacks.sync_multi_workspace_files(multi, sessions[0].plugins)


def report_result(result: AddResult, *, create_verb: str = "ready") -> int:
    """Print `AddResult` as human-readable lines and return `result.exit_code`."""
    for ws in result.workspaces:
        port_note = f" ({ws.note})" if ws.is_stack and ws.note else ""
        if ws.action == "planned":
            verb = "planned"
        elif ws.action == "unchanged":
            verb = "unchanged"
        elif ws.action == "updated":
            verb = "updated"
        else:
            verb = create_verb
        term.out(f"{verb} {ws.name} at {ws.root}{port_note}")
    for wf in result.workspace_files:
        term.out(f"updated {wf}")
    return result.exit_code


def _rollback_workspace(
    root: Path,
    added_worktrees: Sequence[tuple[Path, Path]],
    *,
    indexed: bool,
    remove_root: bool,
    created_config: bool,
) -> None:
    """Clean up partially created worktrees, index entry, and (when newly created) root dir."""
    if indexed:
        try:
            index.remove(root)
        except Exception as exc:
            term.warn(f"{root.name}: could not remove workspace from index: {exc}")
    for bare, wt in reversed(added_worktrees):
        cleanup_worktree(bare, wt)
    if remove_root and root.exists():
        try:
            shutil.rmtree(root)
        except OSError as exc:
            term.warn(f"{root.name}: could not remove {root}: {exc}")
    elif created_config and (root / ".ow").is_dir():
        try:
            shutil.rmtree(root / ".ow")
        except OSError as exc:
            term.warn(f"{root.name}: could not remove {root / '.ow'}: {exc}")


def _validate_unit_repos(
    root: Path,
    repo_specs: Sequence[ResolvedRepo],
    catalog: Mapping[str, KnownRepo],
    *,
    existed_before: bool,
) -> None:
    """Verify every repo can be located and no foreign directory collides before mutating disk."""
    for spec in repo_specs:
        bare = repos_dir() / f"{spec.name}.git"
        if not bare.is_dir() and repo_url(spec.name, spec.url, catalog) is None:
            raise UsageError(
                f"unknown repo {spec.name!r}; pass a URL first with `ow repo add {spec.name} <url>`"
            )
        if existed_before:
            wt_path = root / spec.name
            if (wt_path.exists() or wt_path.is_symlink()) and worktree_state(bare, wt_path) == "foreign":
                raise UsageError(
                    f"{wt_path} already exists and is not a worktree of {spec.name}"
                )


def _warn_if_commit_differs(spec: ResolvedRepo, wt_path: Path) -> None:
    """Warn if an existing healthy worktree's HEAD differs from a requested pinned commit."""
    if not spec.commit:
        return
    cur_head = git.read(["git", "-C", str(wt_path), "rev-parse", "--verify", "HEAD"], check=False)
    if cur_head and cur_head != spec.commit:
        term.warn(
            f"{spec.name}: already exists at {cur_head[:10]} "
            f"(requested commit {spec.commit[:10]} was not checked out)"
        )


@dataclass(frozen=True)
class _WorktreePlan:
    """What one repo's worktree needs, decided once for both creation and `--dry-run`."""

    state: Literal["missing", "healthy", "broken"]
    base: str
    branch: str | None
    remote: str | None
    attach: bool  # an existing worktree that gets a PR or branch attached


def _effective_branch(spec: ResolvedRepo, mode: UnitMode, branch: str | None) -> str | None:
    """The branch `spec`'s worktree is on: none when detached, `branch` when given, else the spec's."""
    return None if mode.detach else (branch if branch is not None else spec.branch)


def _plan_worktree(
    spec: ResolvedRepo,
    *,
    ws_name: str,
    mode: UnitMode,
    branch: str | None,
    accepted: dict[str, str],
    bare: Path,
    wt_path: Path,
    dry_run: bool = False,
) -> _WorktreePlan:
    """Decide `spec`'s effective base, branch and remote, and whether its worktree is added, kept or attached.

    A base that cannot be resolved raises, except in a dry run, which assumes `origin/HEAD` until fetched.
    """
    if spec.base is not None:
        base = parse_base_for(spec.base, spec.name)
    elif spec.name in accepted:
        base = accepted[spec.name]
    else:
        try:
            base, source = resolve_base(spec.name, None, accepted)
        except UsageError:
            if not dry_run:
                raise
            base = "origin/HEAD"
            term.out(f"# {spec.name}: default base unresolved until fetched")
        else:
            term.out(f"# {spec.name}: default base {base} ({source})")

    state = worktree_state(bare, wt_path)
    if state == "foreign":
        raise UsageError(f"{wt_path} already exists and is not a worktree of {spec.name}")
    effective_branch = _effective_branch(spec, mode, branch)
    remote = (
        None
        if (spec.fetch_ref is None and branch is not None and branch != spec.branch)
        else spec.remote
    )
    attach = (
        state == "healthy"
        and mode.allow_attach
        and (spec.fetch_ref is not None or effective_branch is not None)
    )
    if attach and effective_branch is None:
        raise UsageError(f"cannot attach {spec.name!r} in workspace {ws_name!r} without a target branch")
    return _WorktreePlan(state, base, effective_branch, remote, attach)


def _worktree_steps(
    spec: ResolvedRepo, plan: _WorktreePlan, *, bare: Path, wt_path: Path, mode: UnitMode
) -> tuple[Step, ...]:
    """The git steps that attach a PR or branch to `spec`'s worktree, or add that worktree."""
    if plan.attach:
        assert plan.branch is not None
        return attach_pr_to_worktree(
            bare,
            wt_path,
            branch=plan.branch,
            base=plan.base,
            fetch_ref=spec.fetch_ref,
            remote_hint=plan.remote,
            ow_pr=spec.pr,
        )
    return add_worktree(
        bare,
        wt_path,
        branch=plan.branch,
        base=plan.base,
        fetch_ref=spec.fetch_ref,
        remote_hint=plan.remote,
        ow_pr=spec.pr,
        commit=spec.commit,
        detach=mode.detach,
    )


def _preview_unit_dry_run(
    plugins: Plugins,
    *,
    root: Path,
    ws_name: str,
    resolved: ResolvedSpec,
    mode: UnitMode,
    branch: str | None,
    accepted: dict[str, str],
    ws_values: dict[str, object],
    effective_stack: bool,
    existed_before: bool,
    catalog: Mapping[str, KnownRepo],
    settings: Mapping[str, object],
    pending: list[dict[str, object]] | None,
) -> tuple[UnitResult, int]:
    """Print planned commands and compute the unit result without touching disk or bare repos."""
    repo_specs = list(resolved.repos)
    if existed_before:
        term.out(f"# preview updates in {git.display_path(root)}")
    else:
        kind_label = "stack" if effective_stack else "workspace"
        term.out(f"# create {kind_label} {ws_name} at {git.display_path(root)}")

    for spec in repo_specs:
        bare = repos_dir() / f"{spec.name}.git"
        wt_path = root / spec.name

        if not bare.is_dir():
            url = repo_url(spec.name, spec.url, catalog)
            term.out(f"# {spec.name}: would clone bare repo from {url}")
            target_desc = (
                _effective_branch(spec, mode, branch) or spec.commit or spec.base or "default base"
            )
            term.out(f"# {spec.name}: would add worktree at {git.display_path(wt_path)} ({target_desc})")
            accepted[spec.name] = spec.base or "origin/HEAD"
            continue

        plan = _plan_worktree(
            spec,
            ws_name=ws_name,
            mode=mode,
            branch=branch,
            accepted=accepted,
            bare=bare,
            wt_path=wt_path,
            dry_run=True,
        )
        if plan.state == "healthy" and not plan.attach:
            _warn_if_commit_differs(spec, wt_path)
            term.out(f"# {spec.name}: already exists")
            accepted.setdefault(spec.name, plan.base)
            continue
        if plan.state == "broken":
            term.out(f"# {spec.name}: would clean up incomplete worktree at {wt_path}")

        steps = _worktree_steps(spec, plan, bare=bare, wt_path=wt_path, mode=mode)
        run_steps(spec.name, steps, root=root, dry_run=True)
        accepted[spec.name] = plan.base

    if effective_stack or mode.parent_multi_root is not None or pending is not None:
        siblings = _load_sibling_stacks(mode.parent_multi_root, root, plugins)
        planned_names = tuple(dict.fromkeys([*accepted.keys(), *(s.name for s in repo_specs)]))
        pin_stack_values(
            plugins,
            ws_values,
            ws=None,
            siblings=siblings,
            pending=pending,
            global_settings=settings,
            repo_names=planned_names,
        )

    preview_ws = build_workspace(root, (), ws_values, is_stack=effective_stack)
    info = stack_info(preview_ws, plugins)
    return (
        UnitResult(
            name=ws_name,
            root=root,
            is_stack=effective_stack,
            action="planned",
            note=info.note,
        ),
        0,
    )


def _load_sibling_stacks(
    parent_multi_root: Path | None, current_root: Path, plugins: Plugins
) -> tuple[Workspace, ...]:
    if parent_multi_root is None:
        return ()
    loaded = stacks.load_multi_workspace(parent_multi_root, plugins)
    if loaded is None:
        return ()
    return tuple(s for s in loaded[0].stacks if s.root.resolve() != current_root)


def _reconcile_worktree(
    spec: ResolvedRepo,
    *,
    root: Path,
    ws_name: str,
    mode: UnitMode,
    branch: str | None,
    accepted: dict[str, str],
    catalog: Mapping[str, KnownRepo],
    added_worktrees: list[tuple[Path, Path]],
    set_in_progress: Callable[[tuple[Path, Path] | None], None],
    persist: Callable[[], Mapping[str, object]],
) -> bool:
    """Create, resume, or attach one repo worktree; returns True if the workspace changed."""
    url = repo_url(spec.name, spec.url, catalog)
    bare = repos_dir() / f"{spec.name}.git"
    if not bare.is_dir():
        assert url is not None
        ensure_bare(spec.name, url)

    wt_path = root / spec.name
    plan = _plan_worktree(
        spec, ws_name=ws_name, mode=mode, branch=branch, accepted=accepted, bare=bare, wt_path=wt_path
    )

    if plan.state == "healthy" and not plan.attach:
        _warn_if_commit_differs(spec, wt_path)
        term.out(f"# {spec.name}: already exists")
        if spec.name not in accepted:
            accepted[spec.name] = plan.base
            return True
        return False

    if plan.state == "broken":
        term.warn(f"{spec.name}: cleaning up incomplete worktree at {wt_path}")
        cleanup_worktree(bare, wt_path)

    steps = _worktree_steps(spec, plan, bare=bare, wt_path=wt_path, mode=mode)
    if plan.attach:
        if run_steps(spec.name, steps, root=root) != 0:
            raise OwError(f"failed to attach {spec.name!r} in workspace {ws_name!r}")
    else:
        set_in_progress((bare, wt_path))
        if run_steps(spec.name, steps, root=root) != 0:
            raise OwError(f"failed to add repository {spec.name!r} for workspace {ws_name!r}")
        set_in_progress(None)
        added_worktrees.append((bare, wt_path))
    accepted[spec.name] = plan.base
    persist()
    return True


def _apply_stack_defaults(
    plugins: Plugins,
    *,
    root: Path,
    mode: UnitMode,
    effective_stack: bool,
    ws_values: dict[str, object],
    settings: Mapping[str, object],
    pending: list[dict[str, object]] | None,
    persist: Callable[[], Mapping[str, object]],
    mark_indexed: Callable[[], None],
    accepted: Mapping[str, str] = (),
) -> None:
    """Pin plugin stack settings (e.g. non-colliding ports), write config, and update index."""
    need_defaults = (
        mode.parent_multi_root is not None
        or pending is not None
        or effective_stack
    )
    lock_cm = (
        stacks.workspace_flock(mode.parent_multi_root)
        if mode.parent_multi_root is not None
        else contextlib.nullcontext()
    )
    with lock_cm:
        if need_defaults:
            siblings = _load_sibling_stacks(mode.parent_multi_root, root, plugins)
            locs = derive_repos(root, dict(accepted))
            pin_stack_values(
                plugins,
                ws_values,
                ws=None,
                siblings=siblings,
                pending=pending,
                global_settings=settings,
                repo_paths=tuple(loc.path for loc in locs),
                repo_names=tuple(loc.name for loc in locs),
            )
        dropped = persist()
        print_dropped(dropped)
        if mode.register_index and not effective_stack:
            index.add(root)
            mark_indexed()


def unique_setting_values(session: Session, plugins: Plugins) -> dict[str, str]:
    """`<value>-N` replacements for the `unique` settings `session`'s workspace shares with another one.

    A `unique` setting must differ across workspaces. When the new workspace's value is still
    at its default and equals the value another indexed workspace resolves to, the next free
    `<value>-N` (N >= 2) is returned for that key; settings that do not collide, were set
    explicitly or are not strings are left out. Each other workspace is opened with `plugins`
    plus its own in-repo plugins, so it only holds a key its own plugin set declares: a setting
    of an in-repo plugin never collides with a workspace that lacks that plugin.
    """
    ws = session.workspace
    assert ws is not None
    wanted = {
        key: value
        for key, setting in session.declared.items()
        if setting.unique
        and session.origins.get(key) == "default"
        and isinstance(value := ws.settings.get(key), str)
        and value
    }
    if not wanted:
        return {}

    taken: dict[str, set[str]] = {key: set() for key in wanted}
    for entry in index.read():
        for path in stacks.detect_multi_workspace(entry) or (entry,):
            if path.resolve() == ws.root.resolve():
                continue
            try:
                other = open_session(str(path), need_workspace=False, plugins=plugins)
            except Exception:
                continue
            if other.workspace is None:
                continue
            for key, values in taken.items():
                value = other.workspace.settings.get(key) if key in other.declared else None
                if isinstance(value, str) and value:
                    values.add(value)

    replacements: dict[str, str] = {}
    for key, own in wanted.items():
        if own not in taken[key]:
            continue
        suffix = 2
        while f"{own}-{suffix}" in taken[key]:
            suffix += 1
        replacements[key] = f"{own}-{suffix}"
    return replacements


def create_workspace_unit(
    plugins: Plugins,
    *,
    root: Path,
    resolved: ResolvedSpec,
    mode: UnitMode,
    branch: str | None = None,
    pending: list[dict[str, object]] | None = None,
) -> tuple[UnitResult, int]:
    """Create, resume, update, or dry-run a mono-stack workspace or stack at `root`."""
    catalog = known_repo_catalog(plugins)
    settings: Mapping[str, object] = global_settings(plugins)
    declared = {s.key: s for s in plugins.settings()}

    root = root.resolve()
    ws_name = stacks.validate_name(root.name, kind="stack" if mode.is_stack else "workspace")
    repo_specs = list(resolved.repos)

    existed_before = root.exists()
    is_existing_ws = existed_before and is_resumable_workspace(root)
    if existed_before and not is_existing_ws and not mode.allow_existing_dir:
        raise UsageError(f"directory already exists: {root}")

    _validate_unit_repos(root, repo_specs, catalog, existed_before=existed_before)

    effective_stack = mode.is_stack or (existed_before and stacks.is_stack_dir(root))
    multi_root = (
        mode.parent_multi_root
        if mode.parent_multi_root is not None
        else (root.parent.resolve() if effective_stack else None)
    )
    config_path = (
        (multi_root / ".ow" / "config.toml")
        if (effective_stack and multi_root is not None)
        else (root / ".ow" / "config.toml")
    )

    if effective_stack and config_path.is_file():
        ws_top_layer, stacks_cfg = config.read_multi_config(config_path, declared, remotes_of)
        sc = stacks_cfg.get(ws_name)
        accepted = dict(sc.accepted_repos) if sc else {}
        ws_values = dict(sc.layer.values) if sc else {}
        settings = {**settings, **ws_top_layer.values, **ws_values}
    elif not effective_stack and existed_before and config_path.is_file():
        existing_layer, existing_accepted, _rejected = config.read_workspace_config(
            config_path, declared, remotes_of
        )
        accepted = dict(existing_accepted)
        ws_values = dict(existing_layer.values)
        settings = {**settings, **ws_values}
    else:
        accepted = {}
        ws_values = {}

    if mode.dry_run:
        return _preview_unit_dry_run(
            plugins,
            root=root,
            ws_name=ws_name,
            resolved=resolved,
            mode=mode,
            branch=branch,
            accepted=accepted,
            ws_values=ws_values,
            effective_stack=effective_stack,
            existed_before=existed_before,
            catalog=catalog,
            settings=settings,
            pending=pending,
        )

    if not existed_before:
        root.mkdir(parents=True, exist_ok=False)

    created_config = False
    indexed = False
    added_worktrees: list[tuple[Path, Path]] = []
    in_progress: tuple[Path, Path] | None = None

    def persist() -> Mapping[str, object]:
        if effective_stack:
            return config.write_stack(
                config_path,
                ws_name,
                accepted,
                ws_values,
                declared=declared,
                remotes_of=remotes_of,
            )
        return config.write_workspace(
            config_path,
            accepted,
            ws_values,
            declared=declared,
            remotes_of=remotes_of,
        )

    def _rollback() -> None:
        if not is_existing_ws:
            to_clean = list(added_worktrees)
            if in_progress is not None:
                to_clean.append(in_progress)
            if effective_stack and created_config:
                config.remove_stack(config_path, ws_name)
            _rollback_workspace(
                root,
                to_clean,
                indexed=indexed,
                remove_root=not existed_before,
                created_config=created_config and not effective_stack,
            )
        elif in_progress is not None:
            cleanup_worktree(*in_progress)

    with contextlib.ExitStack() as cleanup_stack:
        cleanup_stack.callback(_rollback)

        stack_already_declared = effective_stack and (ws_name in config.stack_names_in_config(config_path))
        if (effective_stack and not stack_already_declared) or (not effective_stack and not config_path.is_file()):
            persist()
            created_config = True

        def _assign_in_progress(pair: tuple[Path, Path] | None) -> None:
            nonlocal in_progress
            in_progress = pair

        def _mark_indexed() -> None:
            nonlocal indexed
            indexed = True

        mutated_any = created_config
        for spec in repo_specs:
            changed = _reconcile_worktree(
                spec,
                root=root,
                ws_name=ws_name,
                mode=mode,
                branch=branch,
                accepted=accepted,
                catalog=catalog,
                added_worktrees=added_worktrees,
                set_in_progress=_assign_in_progress,
                persist=persist,
            )
            mutated_any = mutated_any or changed

        _apply_stack_defaults(
            plugins,
            root=root,
            mode=mode,
            effective_stack=effective_stack,
            ws_values=ws_values,
            settings=settings,
            pending=pending,
            persist=persist,
            mark_indexed=_mark_indexed,
            accepted=accepted,
        )

        session = open_session(str(root), need_workspace=True, plugins=plugins, cwd=root)
        assert session.workspace is not None
        if not is_existing_ws:
            unique_values = unique_setting_values(session, plugins)
            if unique_values:
                declared.update(session.declared)  # the keys of in-repo plugins, or persisting drops them
                ws_values.update(unique_values)
                print_dropped(persist())
                session = session.reload()
        for warning in session.warnings:
            term.warn(warning)

        if not is_existing_ws:
            session.plugins.call("ow_workspace_created", ws=session.workspace)
        refreshed = refresh_workspace(session)

        info = stack_info(session.workspace, session.plugins)
        action: Literal["created", "updated", "unchanged"] = (
            "created" if not is_existing_ws else ("updated" if mutated_any else "unchanged")
        )
        unit = UnitResult(
            name=session.workspace.name,
            root=root,
            is_stack=effective_stack,
            action=action,
            note=info.note,
        )
        cleanup_stack.pop_all()
        return unit, refreshed


def add_into_workspace(
    plugins: Plugins,
    root: Path,
    resolved: ResolvedSpec,
    *,
    branch: str | None = None,
    dry_run: bool = False,
    pending: list[dict[str, object]] | None = None,
) -> tuple[UnitResult, int]:
    """Add missing repos or attach PR/branch specs into an existing workspace or stack."""
    is_stack = stacks.is_stack_dir(root)
    if is_stack:
        mode = UnitMode.stack(
            root.parent.resolve(),
            allow_existing_dir=True,
            allow_attach=True,
            dry_run=dry_run,
        )
    else:
        mode = UnitMode.mono(
            allow_existing_dir=True,
            allow_attach=True,
            dry_run=dry_run,
        )
    return create_workspace_unit(
        plugins,
        root=root,
        resolved=resolved,
        mode=mode,
        branch=branch,
        pending=pending,
    )


def upsert_stack(
    plugins: Plugins,
    multi_root: Path,
    stack_name: str,
    resolved: ResolvedSpec,
    *,
    branch: str | None = None,
    dry_run: bool = False,
    pending: list[dict[str, object]] | None = None,
) -> tuple[UnitResult, int]:
    """Create a new stack or update an existing stack under `multi_root`."""
    target_stack = multi_root / stack_name
    if stacks.is_stack_dir(target_stack):
        return add_into_workspace(
            plugins,
            target_stack,
            resolved,
            branch=branch,
            dry_run=dry_run,
            pending=pending,
        )
    return create_workspace_unit(
        plugins,
        root=target_stack,
        resolved=resolved,
        mode=UnitMode.stack(multi_root, dry_run=dry_run),
        branch=branch,
        pending=pending,
    )
