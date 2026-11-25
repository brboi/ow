"""Shared workspace/stack creation, worktree reconciliation, and result reporting service.

`ow init` and `ow add` follow the one shape of every git command: gather what each repo's
worktree is, plan every step of every unit (a workspace or stack) before touching disk, run
the whole plan as one batch (`-n` stops after printing it), then record the bases, pin the
stack settings and refresh the generated files.
"""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ow import term
from ow.commands._repos import (
    add_worktree,
    attach_pr_to_worktree,
    cleanup_worktree,
    ensure_bare,
    removal_blocker,
    repair_step,
    resolve_base,
    worktree_state,
)
from ow.commands._shared import known_repo_catalog, print_dropped, refresh_workspace, remotes_of, run_steps
from ow.commands._specs import SPEC_USAGE_HINT, resolve_composite_spec
from ow.core import config, git, index, stacks
from ow.core.bases import parse_base_for
from ow.core.paths import repos_dir
from ow.core.plan import Batch, OwStep, RepoPlan, Step, run_batch
from ow.core.plugins import KnownRepo, ResolvedRepo, ResolvedSpec, StackInfo
from ow.core.session import Session, global_settings, open_session
from ow.core.workspace import Workspace, build_workspace, derive_repos, stray_checkouts
from ow.errors import OwError, UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


@dataclass(frozen=True)
class UnitMode:
    """Configuration modes governing how a unit is created or updated."""

    is_stack: bool = False
    parent_multi_root: Path | None = None
    register_index: bool = True
    detach: bool = False
    allow_existing_dir: bool = False
    allow_attach: bool = False

    @classmethod
    def mono(
        cls,
        *,
        allow_existing_dir: bool = False,
        allow_attach: bool = False,
    ) -> UnitMode:
        return cls(
            is_stack=False,
            parent_multi_root=None,
            register_index=True,
            detach=False,
            allow_existing_dir=allow_existing_dir,
            allow_attach=allow_attach,
        )

    @classmethod
    def stack(
        cls,
        parent_multi_root: Path,
        *,
        allow_existing_dir: bool = False,
        allow_attach: bool = False,
    ) -> UnitMode:
        return cls(
            is_stack=True,
            parent_multi_root=parent_multi_root.resolve(),
            register_index=False,
            detach=False,
            allow_existing_dir=allow_existing_dir,
            allow_attach=allow_attach,
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
        )


@dataclass(frozen=True)
class UnitJob:
    """One workspace or stack to create or update: where, what, and how."""

    root: Path
    resolved: ResolvedSpec
    mode: UnitMode


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


def merge_specs(specs: Sequence[ResolvedSpec]) -> ResolvedSpec:
    """One spec holding the repos of every spec in `specs`, for specs that target one workspace.

    An explicit repo wins over an auto-discovered companion of the same name; two explicit
    repos of one name are a usage error, as they are inside one composite spec.
    """
    if len(specs) == 1:
        return specs[0]
    merged: dict[str, ResolvedRepo] = {}
    for spec in specs:
        for repo in spec.repos:
            known = merged.get(repo.name)
            if known is None or (repo.explicit and not known.explicit):
                merged[repo.name] = repo
            elif repo.explicit:
                raise UsageError(f"repo {repo.name!r} is given more than once; add it in one spec")
    return ResolvedSpec(
        name=specs[0].name,
        repos=tuple(merged.values()),
        source=",".join(spec.source for spec in specs),
    )


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


def _remove_worktrees(works: Sequence[_RepoWork]) -> bool:
    """Remove the worktrees `works` created, newest first; False when one held work and stayed."""
    clean = True
    for work in reversed(works):
        if not work.adds_worktree:
            continue
        try:
            cleanup_worktree(work.bare, work.wt_path)
        except OwError as exc:
            term.warn(str(exc))
            clean = False
    return clean


def _rollback_workspace(
    root: Path,
    works: Sequence[_RepoWork],
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
    clean = _remove_worktrees(works)
    if remove_root and root.exists():
        if not clean:
            term.warn(f"{root.name}: leaving {root} in place, a worktree there still holds work")
            return
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

    state: Literal["missing", "healthy", "moved", "broken"]
    base: str
    branch: str | None
    remote: str | None
    attach: bool  # an existing worktree that gets a PR or branch attached


@dataclass
class _RepoWork:
    """One repo of a unit: where it goes, and what the plan decided about it."""

    spec: ResolvedRepo
    bare: Path
    wt_path: Path
    base: str | None = None  # the decided base; known at plan time, or once a deferred plan ran
    adds_worktree: bool = False  # this command creates the worktree, and undoes it on failure
    records_base: bool = False  # the base goes to `[repos]`: a new repo, an attach, or no entry yet
    changed: bool = False  # the workspace differs from before: a step runs, or a base is recorded


@dataclass
class _Unit:
    """A workspace or stack as planned: the decisions every step and the finish rely on."""

    job: UnitJob
    root: Path
    name: str
    effective_stack: bool
    existed_before: bool
    is_existing_ws: bool
    config_path: Path
    accepted: dict[str, str]
    ws_values: dict[str, object]
    settings: Mapping[str, object]
    works: list[_RepoWork] = field(default_factory=list)
    plans: list[RepoPlan] = field(default_factory=list)

    def label(self, work: _RepoWork) -> str:
        """The name a repo of this unit goes by in the batch: stacks qualify it."""
        return f"{self.name}/{work.spec.name}" if self.effective_stack else work.spec.name


def _effective_branch(spec: ResolvedRepo, mode: UnitMode) -> str | None:
    """The branch `spec`'s worktree is on: none when detached, else the spec's."""
    return None if mode.detach else spec.branch


def _plan_worktree(
    spec: ResolvedRepo,
    *,
    ws_name: str,
    mode: UnitMode,
    inferred: dict[str, str],
    bare: Path,
    wt_path: Path,
    dry_run: bool = False,
) -> _WorktreePlan:
    """Decide `spec`'s effective base, branch and remote, and whether its worktree is added, kept or attached.

    A base that cannot be resolved raises, except in a dry run, which assumes `origin/HEAD` until fetched.
    A feature branch whose base falls back to `origin/HEAD` is warned about.
    """
    effective_branch = _effective_branch(spec, mode)
    if spec.base is not None:
        base = parse_base_for(spec.base, spec.name)
    elif spec.name in inferred:
        base = inferred[spec.name]
    else:
        try:
            base, source = resolve_base(spec.name, None, inferred)
        except UsageError:
            if not dry_run:
                raise
            base = "origin/HEAD"
            term.out(f"# {spec.name}: default base unresolved until fetched")
        else:
            term.out(f"# {spec.name}: default base {base} ({source})")
            if source == "origin/HEAD" and effective_branch is not None and spec.fetch_ref is None:
                term.warn(
                    f"{spec.name}: no base known for {effective_branch}; using {base} "
                    f"(origin/HEAD). Spell it: {spec.name}@<base>..{effective_branch}"
                )

    state = worktree_state(bare, wt_path)
    if state == "foreign":
        raise UsageError(f"{wt_path} already exists and is not a worktree of {spec.name}")
    if state == "forgotten":
        raise UsageError(
            f"{wt_path} is a worktree that {bare.name} forgot (a `git worktree prune` after moving "
            "the workspace); its files are untouched. Move it aside, add the repo again, "
            "and copy your changes back"
        )
    attach = (
        state in ("healthy", "moved")
        and mode.allow_attach
        and (spec.fetch_ref is not None or effective_branch is not None)
    )
    if attach and effective_branch is None:
        raise UsageError(f"cannot attach {spec.name!r} in workspace {ws_name!r} without a target branch")
    return _WorktreePlan(state, base, effective_branch, spec.remote, attach)


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
            head_remote=spec.head_remote,
            head_url=spec.head_url,
        )
    return add_worktree(
        bare,
        wt_path,
        branch=plan.branch,
        base=plan.base,
        fetch_ref=spec.fetch_ref,
        remote_hint=plan.remote,
        ow_pr=spec.pr,
        head_remote=spec.head_remote,
        head_url=spec.head_url,
        commit=spec.commit,
        detach=mode.detach,
    )


def _repo_steps(
    spec: ResolvedRepo, plan: _WorktreePlan, *, bare: Path, wt_path: Path, mode: UnitMode
) -> tuple[tuple[Step, ...], tuple[str, ...]]:
    """The steps and notes for one repo: repair a moved worktree, keep a healthy one, remove what
    an interrupted creation left (only when it holds no work), or add and attach."""
    steps: list[Step] = []
    if plan.state == "moved":
        steps.append(repair_step(bare, wt_path))
    if plan.state in ("healthy", "moved") and not plan.attach:
        _warn_if_commit_differs(spec, wt_path)
        return tuple(steps), ("already exists",)
    if plan.state == "broken":
        blocker = removal_blocker(wt_path)
        if blocker is not None:
            raise UsageError(f"{spec.name}: cannot reuse {git.display_path(wt_path)}: {blocker}")
        steps.append(
            OwStep(
                shown=f"# {spec.name}: remove incomplete worktree at {git.display_path(wt_path)}",
                run=lambda: cleanup_worktree(bare, wt_path),
            )
        )
    steps.extend(_worktree_steps(spec, plan, bare=bare, wt_path=wt_path, mode=mode))
    return tuple(steps), ()


def _plan_repo(
    unit: _Unit,
    spec: ResolvedRepo,
    inferred: dict[str, str],
    catalog: Mapping[str, KnownRepo],
    *,
    dry_run: bool,
    after_deferred: bool,
) -> tuple[_RepoWork, bool]:
    """Plan one repo of `unit`; returns its work and whether it had to be deferred.

    A repo whose bare is not cloned yet cannot be planned beyond cloning it: its base, branch
    lookups and worktree steps depend on what the clone holds, so they are planned and run right
    after the clone. A later repo that needs a base inferred from a deferred one waits likewise.
    """
    mode = unit.job.mode
    bare = repos_dir() / f"{spec.name}.git"
    wt_path = unit.root / spec.name
    work = _RepoWork(spec, bare, wt_path)

    defer = not bare.is_dir() or (
        after_deferred and not dry_run and spec.base is None and spec.name not in inferred
    )
    if not defer:
        plan = _plan_worktree(
            spec,
            ws_name=unit.name,
            mode=mode,
            inferred=inferred,
            bare=bare,
            wt_path=wt_path,
            dry_run=dry_run,
        )
        steps, notes = _repo_steps(spec, plan, bare=bare, wt_path=wt_path, mode=mode)
        _decide(unit, work, plan, steps)
        inferred[spec.name] = plan.base
        unit.plans.append(RepoPlan(repo=unit.label(work), steps=steps, notes=notes))
        return work, False

    steps_list: list[Step] = []
    if not bare.is_dir():
        url = repo_url(spec.name, spec.url, catalog)
        assert url is not None  # `_validate_unit_repos` checked it
        steps_list.append(
            OwStep(
                shown=f"# {spec.name}: clone bare repo from {url}",
                run=lambda: ensure_bare(spec.name, url),
            )
        )
    target_desc = _effective_branch(spec, mode) or spec.commit or spec.base or "default base"

    def _plan_after_clone() -> None:
        plan = _plan_worktree(
            spec,
            ws_name=unit.name,
            mode=mode,
            inferred=inferred,
            bare=bare,
            wt_path=wt_path,
            dry_run=False,
        )
        steps, _notes = _repo_steps(spec, plan, bare=bare, wt_path=wt_path, mode=mode)
        _decide(unit, work, plan, steps)
        inferred[spec.name] = plan.base
        if run_steps(unit.label(work), steps, root=unit.root) != 0:
            raise OwError(f"failed to add repository {spec.name!r} for workspace {unit.name!r}")

    steps_list.append(
        OwStep(
            shown=f"# {spec.name}: add worktree at {git.display_path(wt_path)} ({target_desc})",
            run=_plan_after_clone,
        )
    )
    inferred[spec.name] = spec.base or "origin/HEAD"
    unit.plans.append(RepoPlan(repo=unit.label(work), steps=tuple(steps_list)))
    return work, True


def _decide(unit: _Unit, work: _RepoWork, plan: _WorktreePlan, steps: Sequence[Step]) -> None:
    """Record on `work` what its plan does: which base is kept, and what changes."""
    work.base = plan.base
    work.adds_worktree = plan.state in ("missing", "broken")
    work.records_base = work.adds_worktree or plan.attach or work.spec.name not in unit.accepted
    work.changed = bool(steps) or work.records_base


def _load_sibling_stacks(
    parent_multi_root: Path | None, current_root: Path, plugins: Plugins
) -> tuple[Workspace, ...]:
    if parent_multi_root is None:
        return ()
    loaded = stacks.load_multi_workspace(parent_multi_root, plugins)
    if loaded is None:
        return ()
    return tuple(s for s in loaded[0].stacks if s.root.resolve() != current_root)


def _plan_unit(plugins: Plugins, job: UnitJob, *, dry_run: bool) -> _Unit:
    """Gather what `job`'s root and repos are and plan every step; touches nothing on disk.

    Raises `UsageError` for anything the command must refuse (a foreign directory, a forgotten
    worktree, uncommitted work an attach would lose, a worktree that is not provably empty).
    """
    mode = job.mode
    catalog = known_repo_catalog(plugins)
    settings: Mapping[str, object] = global_settings(plugins)
    declared = {s.key: s for s in plugins.settings()}

    root = job.root.resolve()
    ws_name = stacks.validate_name(root.name, kind="stack" if mode.is_stack else "workspace")
    repo_specs = list(job.resolved.repos)

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

    unit = _Unit(
        job=job,
        root=root,
        name=ws_name,
        effective_stack=effective_stack,
        existed_before=existed_before,
        is_existing_ws=is_existing_ws,
        config_path=config_path,
        accepted=accepted,
        ws_values=ws_values,
        settings=settings,
    )
    if not mode.detach:
        kind = "stack" if effective_stack else "workspace"
        verb = "update" if is_existing_ws else "create"
        term.out(f"# {verb} {kind} {ws_name} at {git.display_path(root)}")

    inferred = dict(accepted)
    after_deferred = False
    for spec in repo_specs:
        work, deferred = _plan_repo(
            unit, spec, inferred, catalog, dry_run=dry_run, after_deferred=after_deferred
        )
        unit.works.append(work)
        after_deferred = after_deferred or deferred
    return unit


def _dry_run_result(
    plugins: Plugins, unit: _Unit, pending: list[dict[str, object]] | None
) -> UnitResult:
    """The `planned` result of a dry run, with the settings the stack would pin."""
    mode = unit.job.mode
    if unit.effective_stack or mode.parent_multi_root is not None or pending is not None:
        siblings = _load_sibling_stacks(mode.parent_multi_root, unit.root, plugins)
        planned_names = tuple(dict.fromkeys([*unit.accepted.keys(), *(w.spec.name for w in unit.works)]))
        pin_stack_values(
            plugins,
            unit.ws_values,
            ws=None,
            siblings=siblings,
            pending=pending,
            global_settings=unit.settings,
            repo_names=planned_names,
        )
    preview_ws = build_workspace(unit.root, (), unit.ws_values, is_stack=unit.effective_stack)
    info = stack_info(preview_ws, plugins)
    return UnitResult(
        name=unit.name,
        root=unit.root,
        is_stack=unit.effective_stack,
        action="planned",
        note=info.note,
    )


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


def _finish_unit(
    plugins: Plugins,
    unit: _Unit,
    failed: set[str],
    *,
    pending: list[dict[str, object]] | None,
) -> tuple[UnitResult, int]:
    """Record the bases of the repos that made it, pin settings, open the session and refresh files.

    A new workspace that fails here is rolled back whole; an existing one keeps what it has.
    """
    mode = unit.job.mode
    root = unit.root
    declared = {s.key: s for s in plugins.settings()}
    ok_works = [w for w in unit.works if unit.label(w) not in failed]
    for work in ok_works:
        if work.records_base and work.base is not None:
            unit.accepted[work.spec.name] = work.base

    created_config = False
    indexed = False

    def persist(known: Mapping[str, object] = declared) -> Mapping[str, object]:
        if unit.effective_stack:
            return config.write_stack(
                unit.config_path,
                unit.name,
                unit.accepted,
                unit.ws_values,
                declared=known,
                remotes_of=remotes_of,
            )
        return config.write_workspace(
            unit.config_path,
            unit.accepted,
            unit.ws_values,
            declared=known,
            remotes_of=remotes_of,
        )

    def _rollback() -> None:
        if not unit.is_existing_ws:
            if unit.effective_stack and created_config:
                config.remove_stack(unit.config_path, unit.name)
            _rollback_workspace(
                root,
                unit.works,
                indexed=indexed,
                remove_root=not unit.existed_before,
                created_config=created_config and not unit.effective_stack,
            )

    def _mark_indexed() -> None:
        nonlocal indexed
        indexed = True

    with contextlib.ExitStack() as cleanup_stack:
        cleanup_stack.callback(_rollback)
        root.mkdir(parents=True, exist_ok=True)

        stack_already_declared = unit.effective_stack and (
            unit.name in config.stack_names_in_config(unit.config_path)
        )
        if (unit.effective_stack and not stack_already_declared) or (
            not unit.effective_stack and not unit.config_path.is_file()
        ):
            persist()
            created_config = True
        mutated_any = created_config or any(w.changed for w in ok_works)

        _apply_stack_defaults(
            plugins,
            root=root,
            mode=mode,
            effective_stack=unit.effective_stack,
            ws_values=unit.ws_values,
            settings=unit.settings,
            pending=pending,
            persist=persist,
            mark_indexed=_mark_indexed,
            accepted=unit.accepted,
        )

        session = open_session(str(root), need_workspace=True, plugins=plugins, cwd=root)
        assert session.workspace is not None
        if not unit.is_existing_ws:
            unique_values = unique_setting_values(session, plugins)
            if unique_values:
                unit.ws_values.update(unique_values)
                print_dropped(persist(session.declared))  # the session knows the in-repo plugins' keys
                session = session.reload()
        for warning in session.warnings:
            term.warn(warning)

        if not unit.is_existing_ws:
            session.plugins.call("ow_workspace_created", ws=session.workspace)
        refreshed = refresh_workspace(session)

        info = stack_info(session.workspace, session.plugins)
        action: Literal["created", "updated", "unchanged"] = (
            "created"
            if not unit.is_existing_ws
            else ("updated" if mutated_any else "unchanged")
        )
        result = UnitResult(
            name=session.workspace.name,
            root=root,
            is_stack=unit.effective_stack,
            action=action,
            note=info.note,
        )
        cleanup_stack.pop_all()
        return result, refreshed


def create_workspace_units(
    plugins: Plugins,
    jobs: Sequence[UnitJob],
    *,
    dry_run: bool = False,
    pending: list[dict[str, object]] | None = None,
) -> tuple[tuple[UnitResult, ...], int]:
    """Create, resume, update, or dry-run the workspaces and stacks of `jobs`.

    Gathers and plans every unit first (refusals land before anything changed), prints the whole
    plan, then runs it as one batch; `dry_run` stops after the plan. Units whose repos all made it
    are finished (bases recorded, settings pinned, files refreshed). A new unit with a failed
    repo is undone, an existing one keeps the repos that were added, and the command fails.
    """
    roots = [job.root.resolve() for job in jobs]
    if len(set(roots)) != len(roots):
        raise UsageError("two specs target the same workspace; put their repos in one spec")

    units = [_plan_unit(plugins, job, dry_run=dry_run) for job in jobs]
    parents = {unit.job.mode.parent_multi_root for unit in units}
    if len(units) == 1:
        batch_root: Path | None = units[0].root
    elif len(parents) == 1 and None not in parents:
        batch_root = next(iter(parents))
    else:
        batch_root = None
    batch = Batch(root=batch_root, repos=tuple(plan for unit in units for plan in unit.plans))

    if dry_run:
        run_batch(batch, prompt=True, dry_run=True, yes=True)
        return tuple(_dry_run_result(plugins, unit, pending) for unit in units), 0

    try:
        outcome = run_batch(batch, prompt=True, dry_run=False, yes=True)
    except BaseException:
        for unit in reversed(units):
            if unit.is_existing_ws:
                _remove_worktrees(unit.works)
            else:
                _rollback_workspace(
                    unit.root,
                    unit.works,
                    indexed=False,
                    remove_root=not unit.existed_before,
                    created_config=False,
                )
        raise

    failed = set(outcome.failed) | set(outcome.refused)
    results: list[UnitResult] = []
    failures: list[str] = []
    exit_code = 0
    for unit in units:
        unit_failed = [w for w in unit.works if unit.label(w) in failed]
        if unit_failed:
            failures.extend(
                f"failed to add repository {w.spec.name!r} for workspace {unit.name!r}"
                for w in unit_failed
            )
            if not unit.is_existing_ws:
                _rollback_workspace(
                    unit.root,
                    unit.works,
                    indexed=False,
                    remove_root=not unit.existed_before,
                    created_config=False,
                )
                continue
            _remove_worktrees(unit_failed)
        result, code = _finish_unit(plugins, unit, failed, pending=pending)
        results.append(result)
        exit_code = max(exit_code, code)
    if failures:
        raise OwError("; ".join(failures))
    return tuple(results), exit_code


def create_workspace_unit(
    plugins: Plugins,
    *,
    root: Path,
    resolved: ResolvedSpec,
    mode: UnitMode,
    dry_run: bool = False,
    pending: list[dict[str, object]] | None = None,
) -> tuple[UnitResult, int]:
    """Create, resume, update, or dry-run one workspace or stack at `root`."""
    results, code = create_workspace_units(
        plugins, [UnitJob(root, resolved, mode)], dry_run=dry_run, pending=pending
    )
    return results[0], code


def existing_unit_job(root: Path, resolved: ResolvedSpec) -> UnitJob:
    """The job that adds missing repos or attaches PR/branch specs into the workspace or stack at `root`."""
    if stacks.is_stack_dir(root):
        mode = UnitMode.stack(root.parent.resolve(), allow_existing_dir=True, allow_attach=True)
    else:
        mode = UnitMode.mono(allow_existing_dir=True, allow_attach=True)
    return UnitJob(root, resolved, mode)


def stack_job(multi_root: Path, stack_name: str, resolved: ResolvedSpec) -> UnitJob:
    """The job that creates stack `stack_name` under `multi_root`, or updates it when it exists."""
    target = multi_root / stack_name
    if stacks.is_stack_dir(target):
        return existing_unit_job(target, resolved)
    return UnitJob(target, resolved, UnitMode.stack(multi_root))
