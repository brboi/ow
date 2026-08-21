import sys
from pathlib import Path
from typing import Any

from rich.markup import escape
from rich.text import Text

from ow.utils.config import Config, WorkspaceConfig, select_aliases
from ow.utils.display import console, err_console
from ow.utils.drift import warn_if_drifted
from ow.utils.git import (
    count_commits,
    count_new_patches,
    count_unpushed,
    dirty_files,
    git,
    in_progress_operation,
    is_ancestor,
    merge_base,
    merge_base_fork_point,
    parallel_per_repo,
    resolve_spec,
    rev_parse,
    worktree_is_detached,
)
from ow.utils.rebase_plan import RebasePlan, RepoFacts, plan_for
from ow.utils.refs import fetch_workspace_refs
from ow.utils.resolver import resolve_workspace


def _bound(worktree, base: str, up_before: str | None, up: str | None = None) -> str | None:
    """The commit after which HEAD's commits are ours to replay.

    Invariant: never older than merge-base(HEAD, base). That is what keeps
    the base branch's own commits out of the replay range — replaying them
    onto a stale upstream is what makes the current implementation destroy
    a second run.

    Three sources, newest wins, all subject to the invariant:
      - merge-base(HEAD, base), the floor
      - merge-base(HEAD, up_before), when we saw the upstream move this run
      - the upstream's fork-point, when an earlier fetch already absorbed a
        force-push and up_before no longer shows it
    """
    b_base = merge_base(worktree, "HEAD", base)
    if b_base is None:
        return None

    best = b_base
    for candidate in (
        merge_base(worktree, "HEAD", up_before) if up_before else None,
        merge_base_fork_point(worktree, up) if up else None,
    ):
        if candidate and candidate != best and is_ancestor(worktree, best, candidate):
            best = candidate
    return best


def gather_facts(
    worktree,
    alias: str,
    base: str,
    up: str | None,
    up_before: str | None,
    is_detached: bool,
) -> RepoFacts:
    """Observe one repo. No decisions are taken here.

    `is_detached` is what the *config* asks for. The worktree's real shape is
    observed here, and a disagreement is itself a fact: planning against the
    config while HEAD has drifted rebases the wrong thing.
    """
    observed_detached = worktree_is_detached(worktree)
    detached_drift = observed_detached != is_detached

    busy = in_progress_operation(worktree)
    if busy is not None:
        return RepoFacts(
            alias=alias, base=base, up=up, is_detached=observed_detached,
            detached_drift=detached_drift, busy=busy,
        )

    if detached_drift:
        return RepoFacts(
            alias=alias, base=base, up=up, is_detached=observed_detached,
            detached_drift=True,
        )

    force_pushed = False
    new_patches = 0
    unpushed = 0

    bound = _bound(worktree, base, up_before, up)

    if up is not None:
        up_now = rev_parse(worktree, up)
        if up_now is not None:
            if up_before and up_before != up_now:
                force_pushed = not is_ancestor(worktree, up_before, up_now)
            new_patches = count_new_patches(worktree, up)
            if bound is not None:
                unpushed = count_unpushed(worktree, bound, up)

    base_merged = is_ancestor(worktree, base, "HEAD")

    replay_from = bound if (force_pushed or new_patches > 0) else base
    replay_count = count_commits(worktree, f"{replay_from}..HEAD") if replay_from else 0

    return RepoFacts(
        alias=alias,
        base=base,
        up=up,
        is_detached=observed_detached,
        dirty_files=tuple(dirty_files(worktree)),
        force_pushed=force_pushed,
        new_patches=new_patches,
        bound=bound,
        base_merged=base_merged,
        replay_count=replay_count,
        unpushed=unpushed,
    )


def _summary_line(plan: RebasePlan, width: int) -> str:
    target = plan.base
    if plan.step1_target:
        target = f"{plan.base} [dim]←[/] {plan.step1_target}"

    if plan.is_skipped:
        state = f"[yellow]skipped[/] — {plan.skip_reason}"
    elif plan.is_noop:
        state = "[dim]up to date[/]"
    elif plan.detaches:
        state = "[dim]detach[/]"
    else:
        state = f"{plan.replay_count} commit(s) to replay"

    markers = []
    if plan.force_pushed:
        markers.append("[yellow]rewritten[/]")
    if plan.unpushed:
        markers.append(f"[yellow]{plan.unpushed} unpushed[/]")
    suffix = f"  [{', '.join(markers)}]" if markers else ""

    return f"  {plan.alias.ljust(width)}  {target}  {state}{suffix}"


def _display_summary(ws_name: str, plans: list[RebasePlan]) -> None:
    console.print(Text(f"[{ws_name}]", style="bold cyan"))
    width = max((len(p.alias) for p in plans), default=0)
    for plan in plans:
        console.print(_summary_line(plan, width))


def _display_dry_run(plans: list[RebasePlan], ws_dir: Path) -> None:
    actionable = [p for p in plans if not p.is_skipped and not p.is_noop]
    if not actionable:
        console.print("\n[dim]Would run: nothing to do[/]")
        return

    console.print("\n[dim]Would run:[/]")
    for plan in actionable:
        console.print(f"  [{plan.alias}] cd {ws_dir / plan.alias}", markup=False)
        for step in plan.steps:
            console.print(f"  [{plan.alias}] git {' '.join(step.args)}", markup=False)


def _report_skip(plan: RebasePlan) -> None:
    err_console.print(f"  Skipping {plan.alias}: {plan.skip_reason}", markup=False)
    if plan.resume:
        cont, abort = plan.resume
        err_console.print(f"    resume with: {cont}", markup=False)
        err_console.print(f"    or abort:    {abort}", markup=False)


def _report_conflict(alias: str, worktree: Path, onto: str) -> None:
    err_console.print(f"\n  [red]CONFLICT[/] in [bold]{escape(alias)}[/] rebasing onto {escape(onto)}")
    err_console.print("    resolve conflicts, then:")
    err_console.print(f"      cd {worktree}", markup=False)
    err_console.print("      git rebase --continue")
    err_console.print("    or abort:")
    err_console.print("      git rebase --abort")
    err_console.print(f"    then re-run: ow rebase --only {escape(alias)}\n")


def _report_failure(alias: str, worktree: Path, onto: str) -> None:
    """A step that failed without leaving a rebase behind.

    No identity configured, a hook that said no, a full disk: prescribing
    `git rebase --continue` here would only produce a second error.
    """
    err_console.print(f"\n  [red]Error[/] in [bold]{escape(alias)}[/]: rebase onto {escape(onto)} failed")
    err_console.print("    git's output above says why; no rebase is in progress")
    err_console.print(f"    cd {worktree}", markup=False)
    err_console.print(f"    then re-run: ow rebase --only {escape(alias)}\n")


def _report_switch_failure(alias: str, worktree: Path, ref: str) -> None:
    err_console.print(f"\n  [red]Error[/] in [bold]{escape(alias)}[/]: could not switch to {escape(ref)}")
    err_console.print(f"    cd {worktree}", markup=False)
    err_console.print(f"    then re-run: ow rebase --only {escape(alias)}\n")


def _confirm() -> bool:
    """Default is no. A destructive command must not proceed unasked."""
    try:
        answer = input("\nProceed? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _execute(plan: RebasePlan, worktree: Path) -> bool:
    """Run a plan's steps. Returns True on success."""
    console.print(f"  {plan.alias}:", markup=False)
    for step in plan.steps:
        result = git(worktree, *step.args)
        if result.returncode != 0:
            if step.args[0] == "switch":
                _report_switch_failure(plan.alias, worktree, step.onto)
            elif in_progress_operation(worktree) is not None:
                _report_conflict(plan.alias, worktree, step.onto)
            else:
                _report_failure(plan.alias, worktree, step.onto)
            return False
    console.print("    Done.")
    return True


def cmd_rebase(
    config: Config,
    workspace: str | None = None,
    *,
    only: str | None = None,
    autostash: bool = False,
    dry_run: bool = False,
    yes: bool = False,
) -> None:
    """Fetch and rebase the repos of a workspace."""
    ws_dir, ws = resolve_workspace(name=workspace)
    aliases = select_aliases(list(ws.repos), only)

    # --only must also narrow drift-checking and fetching, not just execution.
    selected_repos = {a: spec for a, spec in ws.repos.items() if a in aliases}
    selected_ws = WorkspaceConfig(repos=selected_repos, templates=ws.templates, vars=ws.vars)

    warn_if_drifted(selected_ws, ws_dir)

    fetched = fetch_workspace_refs(
        selected_ws, ws_dir, config, fetch_upstreams=True,
        resolve_fn=resolve_spec, spinner_prefix="Preparing",
    )

    failed = False

    tasks: dict[str, Any] = {}
    for alias in aliases:
        worktree = ws_dir / alias
        if not worktree.exists():
            err_console.print(f"  Skipping {alias}: worktree not found at {worktree}", markup=False)
            failed = True
            continue
        # Rebasing onto the stale cached ref would look like a success.
        if alias in fetched.failed:
            err_console.print(f"  Skipping {alias}: fetch failed, refs are stale", markup=False)
            failed = True
            continue
        tasks[alias] = (
            lambda w=worktree, a=alias,
                   b=fetched.tracks.get(alias, ws.repos[alias].base_ref),
                   u=fetched.upstreams.get(alias),
                   ub=fetched.upstream_before.get(alias),
                   d=ws.repos[alias].is_detached:
            gather_facts(w, a, b, u, ub, d)
        )

    if not tasks:
        if failed:
            sys.exit(1)
        return

    results = parallel_per_repo(tasks)
    plans: list[RebasePlan] = []
    for alias in aliases:
        if alias not in results:
            continue
        result = results[alias]
        if isinstance(result, Exception):
            err_console.print(f"  Skipping {alias}: could not analyse — {result}", markup=False)
            failed = True
            continue
        plans.append(plan_for(result, autostash=autostash))

    if not plans:
        if failed:
            sys.exit(1)
        return

    _display_summary(ws_dir.name, plans)

    if dry_run:
        _display_dry_run(plans, ws_dir)
        if failed:
            sys.exit(1)
        return

    actionable = [p for p in plans if not p.is_skipped and not p.is_noop]
    if not actionable and not any(p.is_skipped for p in plans):
        if failed:
            sys.exit(1)
        return

    if actionable and not yes and not _confirm():
        console.print("Aborted.")
        if failed:
            sys.exit(1)
        return

    for plan in plans:
        if plan.is_skipped:
            _report_skip(plan)
            failed = True
            continue
        if plan.is_noop:
            continue
        if not _execute(plan, ws_dir / plan.alias):
            failed = True

    if failed:
        sys.exit(1)
