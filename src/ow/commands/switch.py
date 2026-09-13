import sys
from pathlib import Path
from typing import Any

from rich.markup import escape
from rich.text import Text

from ow.utils.config import BranchSpec, Config, WorkspaceConfig, select_aliases, write_workspace_config
from ow.utils.display import console, err_console
from ow.utils.git import (
    get_all_remote_refs,
    get_upstream,
    get_worktree_branch,
    git,
    in_progress_operation,
    ordered_remotes,
    parallel_per_repo,
    rev_parse,
)
from ow.utils.resolver import resolve_workspace
from ow.utils.switch_plan import SwitchFacts, SwitchPlan, plan_switch


def _switch_args(target: str | None, create: str | None, detach: bool) -> tuple[str, ...]:
    """The `git switch` invocation, decided once for the whole run.

    `--guess` is passed explicitly for the plain form rather than relied
    on as git's default, so a user's `checkout.guess = false` cannot
    silently turn DWIM off underneath `ow switch`.
    """
    if create is not None:
        return ("switch", "-c", create) + ((target,) if target else ())
    assert target is not None  # validated by cmd_switch before this is ever called
    if detach:
        return ("switch", "--detach", target)
    return ("switch", "--guess", target)


def _resolves(worktree: Path, ref: str) -> bool:
    """Would `git switch` find `ref` here, without ourselves fetching?

    A direct ref (local branch, tag, sha, fully-qualified remote branch)
    resolves on its own; otherwise this mirrors git's own DWIM — a short
    name that matches exactly one already-known remote-tracking branch.
    """
    if rev_parse(worktree, ref) is not None:
        return True
    matches = [r for r in get_all_remote_refs(worktree) if r.rsplit("/", 1)[-1] == ref]
    return len(matches) == 1


def gather_switch_facts(
    worktree: Path,
    alias: str,
    target: str | None,
    *,
    needs_resolution: bool,
    alias_remotes: dict,
) -> SwitchFacts:
    """Observe one repo. No decisions are taken here.

    Resolution is local-first: a fetch only happens when `target` is
    unknown here, and at most once, against every remote configured for
    this repo.
    """
    if not worktree.exists():
        return SwitchFacts(alias=alias, worktree_missing=True)

    busy = in_progress_operation(worktree)
    if busy is not None:
        return SwitchFacts(alias=alias, busy=busy)

    if not needs_resolution:
        return SwitchFacts(alias=alias)

    assert target is not None
    if _resolves(worktree, target):
        return SwitchFacts(alias=alias)

    for remote in ordered_remotes(alias_remotes):
        git(worktree, "fetch", remote, quiet=True)

    return SwitchFacts(alias=alias, target_resolvable=_resolves(worktree, target))


def _repro_hint(target: str | None, create: str | None, detach: bool, alias: str) -> str:
    parts = ["ow switch"]
    if create is not None:
        parts += ["-c", create]
    if detach:
        parts.append("--detach")
    if target:
        parts.append(target)
    parts += ["--only", alias]
    return " ".join(parts)


def _report_skip(plan: SwitchPlan) -> None:
    err_console.print(f"  {plan.alias}: {plan.skip_reason}", markup=False)
    if plan.resume:
        cont, abort = plan.resume
        err_console.print(f"    resume with: {cont}", markup=False)
        err_console.print(f"    or abort:    {abort}", markup=False)


def _display_dry_run(ws_name: str, plans: list[SwitchPlan], ws_dir: Path) -> None:
    console.print(Text(f"[{ws_name}]", style="bold cyan"))
    console.print("\n[dim]Would run:[/]")
    for plan in plans:
        console.print(f"  [{plan.alias}] cd {ws_dir / plan.alias}", markup=False)
        console.print(f"  [{plan.alias}] git {' '.join(plan.args)}", markup=False)


def _execute(
    alias: str, worktree: Path, args: tuple[str, ...], *, target: str | None, create: str | None, detach: bool,
) -> bool:
    """Run the switch. Returns True on success."""
    console.print(f"  {alias}:", markup=False)
    result = git(worktree, *args)
    if result.returncode != 0:
        err_console.print(
            f"\n  [red]Error[/] in [bold]{escape(alias)}[/]: git {escape(' '.join(args))} failed"
        )
        err_console.print("    git's output above says why", markup=False)
        err_console.print(f"    cd {worktree}", markup=False)
        err_console.print(f"    then re-run: {_repro_hint(target, create, detach, alias)}\n", markup=False)
        return False
    console.print("    Done.")
    return True


def _new_spec(worktree: Path, *, target: str | None, create: str | None, old: BranchSpec) -> BranchSpec:
    """What the repo really is now, read from disk rather than assumed."""
    branch = get_worktree_branch(worktree)
    if branch is None:
        # Detached: the bare spec is the ref the user asked for, not the
        # sha it happens to resolve to — that is what a future `ow apply`
        # or `ow switch` would need to re-detach onto the same place.
        assert target is not None
        return BranchSpec(base_ref=target)

    upstream = get_upstream(worktree)
    if upstream:
        return BranchSpec(base_ref=upstream, local_branch=branch)

    start = target if (create is not None and target) else old.base_ref
    return BranchSpec(base_ref=start, local_branch=branch)


def _display_summary(
    ws_name: str, plans: list[SwitchPlan], touched: dict[str, BranchSpec], old_repos: dict[str, BranchSpec],
) -> None:
    console.print(Text(f"[{ws_name}]", style="bold cyan"))
    width = max((len(p.alias) for p in plans), default=0)
    for plan in plans:
        old = old_repos[plan.alias].to_spec_str()
        if plan.alias in touched:
            new = touched[plan.alias].to_spec_str()
            console.print(f"  {plan.alias.ljust(width)}  {old} → {new}")
        else:
            console.print(f"  {plan.alias.ljust(width)}  {old}  [red]failed[/]")


def cmd_switch(
    config: Config,
    target: str | None = None,
    workspace: str | None = None,
    *,
    create: str | None = None,
    detach: bool = False,
    only: str | None = None,
    dry_run: bool = False,
) -> None:
    """`git switch`, one repo at a time, across a workspace.

    Every repo is pre-flighted — worktree present, no operation already
    in progress, target resolvable — before any of them is touched: this
    is not `ow reset`'s skip-and-continue, because switching half a
    workspace and refusing the rest leaves it straddling two states.
    Resolution tries local refs first and only fetches, once per repo,
    when the target is not already known.

    Once a repo has actually moved, `.ow/config.toml` is rewritten from
    what git now reports for it — an upstream when the branch tracks
    one, the start point (or the repo's own prior base ref) when it does
    not, a bare ref when it ends up detached. Templates are not
    re-rendered here; that is `ow apply`'s job.
    """
    if detach and create is not None:
        err_console.print("Error: --detach cannot be combined with -c/--create", markup=False)
        sys.exit(2)
    if target is None and create is None:
        err_console.print("Error: a TARGET or -c NEW is required", markup=False)
        sys.exit(2)

    ws_dir, ws = resolve_workspace(name=workspace)
    aliases = select_aliases(list(ws.repos), only)
    if not aliases:
        return

    args = _switch_args(target, create, detach)
    needs_resolution = target is not None

    tasks: dict[str, Any] = {
        alias: (
            lambda w=ws_dir / alias, a=alias: gather_switch_facts(
                w, a, target, needs_resolution=needs_resolution, alias_remotes=config.remotes.get(alias, {}),
            )
        )
        for alias in aliases
    }
    results = parallel_per_repo(tasks)

    plans: list[SwitchPlan] = []
    preflight_failed = False
    for alias in aliases:
        result = results[alias]
        if isinstance(result, Exception):
            plan = SwitchPlan(alias=alias, skip_reason=f"could not analyse — {result}")
        else:
            plan = plan_switch(result, args)
        plans.append(plan)
        if plan.is_skipped:
            preflight_failed = True

    if preflight_failed:
        for plan in plans:
            if plan.is_skipped:
                _report_skip(plan)
        sys.exit(2)

    if dry_run:
        _display_dry_run(ws_dir.name, plans, ws_dir)
        return

    touched: dict[str, BranchSpec] = {}
    exec_failed = False
    for plan in plans:
        worktree = ws_dir / plan.alias
        old = ws.repos[plan.alias]
        if not _execute(plan.alias, worktree, plan.args, target=target, create=create, detach=detach):
            exec_failed = True
            continue
        touched[plan.alias] = _new_spec(worktree, target=target, create=create, old=old)

    if touched:
        new_repos = dict(ws.repos)
        new_repos.update(touched)
        new_ws = WorkspaceConfig(repos=new_repos, templates=ws.templates, vars=ws.vars)
        write_workspace_config(ws_dir / ".ow" / "config.toml", new_ws)

    _display_summary(ws_dir.name, plans, touched, ws.repos)

    if touched:
        console.print(
            "\n[dim]Templates are not re-rendered by a switch — run `ow apply` if you need them refreshed.[/]"
        )

    if exec_failed:
        sys.exit(1)
