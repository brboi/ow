import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from rich.markup import escape
from rich.text import Text

from ow.utils.config import (
    Config,
    report_pending_migration,
    select_aliases,
)
from ow.utils.display import console, err_console
from ow.utils.drift import warn_if_drifted
from ow.utils.git import (
    dirty_files,
    git,
    in_progress_operation,
    is_ancestor,
    parallel_per_repo,
    resolve_spec,
    rev_parse,
    worktree_is_detached,
)
from ow.utils.pull_plan import PullFacts, PullPlan, plan_pull
from ow.utils.refs import fetch_workspace_refs
from ow.utils.resolver import resolve_workspace
from ow.utils.workspace import print_render_pointer, refresh_after_git


def gather_pull_facts(
    worktree: Path, alias: str, target: str, cfg_detached: bool, target_is_upstream: bool
) -> PullFacts:
    """Observe one repo. No decisions are taken here."""
    observed_detached = worktree_is_detached(worktree)
    head = rev_parse(worktree, "HEAD")
    target_sha = rev_parse(worktree, target)

    return PullFacts(
        alias=alias,
        target=target,
        head=head,
        target_sha=target_sha,
        is_detached=observed_detached,
        detached_drift=observed_detached != cfg_detached,
        busy=in_progress_operation(worktree),
        ff_possible=(
            head is not None
            and target_sha is not None
            and is_ancestor(worktree, "HEAD", target)
        ),
        target_merged=(
            head is not None
            and target_sha is not None
            and is_ancestor(worktree, target, "HEAD")
        ),
        target_is_upstream=target_is_upstream,
        dirty_files=tuple(dirty_files(worktree)),
    )


def _summary_line(plan: PullPlan, width: int) -> str:
    if plan.is_skipped:
        state = f"[yellow]skipped[/] — {plan.skip_reason}"
    elif plan.is_noop:
        state = "[dim]up to date[/]"
    elif plan.detaches:
        state = "[dim]detach[/]"
    elif plan.rebases:
        state = "[dim]replay onto it[/]"
    else:
        state = "[dim]fast-forward[/]"

    return f"  {plan.alias.ljust(width)}  {plan.target}  {state}"


def _display_summary(ws_name: str, plans: list[PullPlan]) -> None:
    console.print(Text(f"[{ws_name}]", style="bold cyan"))
    width = max((len(p.alias) for p in plans), default=0)
    for plan in plans:
        console.print(_summary_line(plan, width))


def _display_dry_run(plans: list[PullPlan], ws_dir: Path) -> None:
    actionable = [p for p in plans if not p.is_skipped and not p.is_noop]
    if not actionable:
        console.print("\n[dim]Would run: nothing to do[/]")
        return

    console.print("\n[dim]Would run:[/]")
    for plan in actionable:
        console.print(f"  [{plan.alias}] cd {ws_dir / plan.alias}", markup=False)
        for step in plan.steps:
            console.print(f"  [{plan.alias}] git {' '.join(step.args)}", markup=False)


def _report_skip(plan: PullPlan) -> None:
    err_console.print(f"  Skipping {plan.alias}: {plan.skip_reason}", markup=False)
    if plan.resume:
        cont, abort = plan.resume
        err_console.print(f"    resume with: {cont}", markup=False)
        err_console.print(f"    or abort:    {abort}", markup=False)


def _execute(plan: PullPlan, worktree: Path) -> bool:
    """Run a plan's steps. Returns True on success."""
    console.print(f"  {plan.alias}:", markup=False)
    for step in plan.steps:
        result = git(worktree, *step.args)
        if result.returncode != 0:
            busy = in_progress_operation(worktree)
            if busy is not None:
                operation, cont, abort = busy
                err_console.print(
                    f"\n  [red]CONFLICT[/] in [bold]{escape(plan.alias)}[/] "
                    f"replaying onto {escape(step.onto)}"
                )
                err_console.print("    resolve conflicts, then:")
                err_console.print(f"      cd {worktree}", markup=False)
                err_console.print(f"      {cont}", markup=False)
                err_console.print("    or abort:")
                err_console.print(f"      {abort}", markup=False)
            else:
                err_console.print(
                    f"\n  [red]Error[/] in [bold]{escape(plan.alias)}[/]: "
                    f"git {escape(' '.join(step.args))} failed"
                )
                err_console.print("    git's output above says why", markup=False)
                err_console.print(f"    cd {worktree}", markup=False)
            err_console.print(f"    then re-run: ow pull --only {escape(plan.alias)}\n")
            return False
    console.print("    Done.")
    return True


def cmd_pull(
    config: Config,
    workspace: str | None = None,
    *,
    only: str | None = None,
    dry_run: bool = False,
) -> None:
    """Fetch and bring the repos of a workspace up to date, without moving
    any of them off the base branch they are configured on.

    A repo follows its upstream when it has one — its own branch as pushed
    elsewhere. Behind it, fast-forward; diverged from it, replay on top,
    exactly as `git pull --rebase` does: same branch, same base, nobody
    else's commits. A repo with no upstream may only fast-forward to its
    base ref; carrying its work over to a moved base is a bigger move, with
    force-push detection and a replay floor to get right, and belongs to
    `ow rebase`.
    """
    ws_dir, ws = resolve_workspace(name=workspace)
    report_pending_migration(config, ws, ws_dir)
    aliases = select_aliases(list(ws.repos), only)

    # --only must also narrow drift-checking and fetching, not just execution.
    # `replace` keeps the schema version, the typed overrides and any legacy
    # evidence: a narrowed view of the workspace is still the same workspace.
    selected_ws = replace(ws, repos={alias: ws.repos[alias] for alias in aliases})

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
        if alias in fetched.failed:
            # Fast-forwarding to the stale cached ref would look like a success.
            err_console.print(f"  Skipping {alias}: fetch failed, refs are stale", markup=False)
            failed = True
            continue
        # The upstream when there is one — a branch pushed elsewhere is what
        # "pull" means for it, and the one target this command may replay
        # onto. Otherwise the base ref it was cut from, which it may only
        # fast-forward to.
        upstream = fetched.upstreams.get(alias)
        target = upstream or fetched.tracks.get(alias, ws.repos[alias].base_ref)
        tasks[alias] = (
            lambda w=worktree, a=alias, t=target, d=ws.repos[alias].is_detached,
                   u=upstream is not None:
            gather_pull_facts(w, a, t, d, u)
        )

    if not tasks:
        if failed:
            sys.exit(1)
        print_render_pointer()
        return

    results = parallel_per_repo(tasks)
    plans: list[PullPlan] = []
    for alias in aliases:
        if alias not in results:
            continue
        result = results[alias]
        if isinstance(result, Exception):
            err_console.print(f"  Skipping {alias}: could not analyse — {result}", markup=False)
            failed = True
            continue
        plans.append(plan_pull(result))

    if not plans:
        if failed:
            sys.exit(1)
        print_render_pointer()
        return

    _display_summary(ws_dir.name, plans)

    if dry_run:
        _display_dry_run(plans, ws_dir)
        if failed:
            sys.exit(1)
        print_render_pointer()
        return

    changed: set[str] = set()
    for plan in plans:
        if plan.is_skipped:
            _report_skip(plan)
            failed = True
            continue
        if plan.is_noop:
            continue
        if _execute(plan, ws_dir / plan.alias):
            changed.add(plan.alias)
        else:
            failed = True

    render_failed = refresh_after_git(config, ws, ws_dir, changed=changed, failed=failed)
    if not changed and not failed:
        # Every plan was a no-op: the run reached this boundary having moved
        # nothing, and `refresh_after_git` stays silent on an empty change
        # set — the pointer belongs to the command that decided to skip.
        print_render_pointer()
    if failed or render_failed:
        sys.exit(1)
