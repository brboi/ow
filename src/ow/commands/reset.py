import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from rich.markup import escape
from rich.text import Text

from ow.utils.config import (
    BranchSpec,
    Config,
    report_pending_migration,
    select_aliases,
)
from ow.utils.display import confirm, console, err_console
from ow.utils.drift import DriftResult, check_drift
from ow.utils.git import (
    count_commits,
    count_unbacked_commits,
    dirty_files,
    get_upstream,
    git,
    in_progress_operation,
    parallel_per_repo,
    resolve_spec,
    rev_parse,
)
from ow.utils.refs import fetch_workspace_refs
from ow.utils.reset_plan import ResetFacts, ResetPlan, plan_reset
from ow.utils.resolver import resolve_workspace
from ow.utils.workspace import print_render_pointer, refresh_after_git


def _drift_reason(result: DriftResult) -> str | None:
    """Why the worktree is not the one the config describes, in one clause.

    DriftResult.message names the alias, which the summary already does.
    """
    if not result.is_drifted:
        return None
    if result.spec.is_detached:
        return f"on branch {result.actual_branch}, config says detached at {result.spec.base_ref}"
    if result.actual_branch is None:
        return f"detached, config says branch {result.spec.local_branch}"
    return f"on branch {result.actual_branch}, config says {result.spec.local_branch}"


def gather_reset_facts(worktree: Path, alias: str, spec: BranchSpec, target: str) -> ResetFacts:
    """Observe one repo. No decisions are taken here."""
    head = rev_parse(worktree, "HEAD")
    target_sha = rev_parse(worktree, target)

    drop_commits = 0
    unbacked = 0
    if head is not None and target_sha is not None:
        drop_commits = count_commits(worktree, f"{target}..HEAD")
        if drop_commits:
            counted = count_unbacked_commits(worktree, target)
            # git could not tell: assume the worst rather than promise the
            # commits are safe somewhere.
            unbacked = drop_commits if counted is None else counted

    return ResetFacts(
        alias=alias,
        target=target,
        head=head,
        target_sha=target_sha,
        drift=_drift_reason(check_drift(worktree, spec, alias)),
        busy=in_progress_operation(worktree),
        dirty_files=tuple(dirty_files(worktree)),
        drop_commits=drop_commits,
        unbacked=unbacked,
    )


def _summary_line(plan: ResetPlan, width: int) -> str:
    if plan.is_skipped:
        state = f"[yellow]skipped[/] — {plan.skip_reason}"
    elif plan.is_noop:
        state = "[dim]already there[/]"
    else:
        parts = []
        if plan.drop_commits:
            parts.append(f"drop {plan.drop_commits} commit(s)")
        elif plan.moves:
            parts.append("move")
        if plan.discarded:
            parts.append(f"discard {plan.discarded} file(s)")
        state = ", ".join(parts)

    marker = f"  [[yellow]{plan.unbacked} on no remote[/]]" if plan.unbacked else ""
    return f"  {plan.alias.ljust(width)}  {plan.target}  {state}{marker}"


def _display_summary(ws_name: str, plans: list[ResetPlan], *, hard: bool) -> None:
    header = f"[{ws_name}] --hard" if hard else f"[{ws_name}]"
    console.print(Text(header, style="bold cyan"))
    width = max((len(p.alias) for p in plans), default=0)
    for plan in plans:
        console.print(_summary_line(plan, width))

    actionable = [p for p in plans if not p.is_skipped and not p.is_noop]
    if not actionable:
        return

    if not hard and any(p.drop_commits for p in actionable):
        console.print(
            "\n[dim]Dropped commits stay in the working tree as unstaged changes.[/]\n"
            "[dim]`ow reset --hard` discards those too.[/]"
        )
    if hard:
        console.print(
            "\n[dim]Untracked files are left alone, exactly as `git reset --hard` leaves them.[/]"
        )
    if any(p.unbacked for p in actionable):
        total = sum(p.unbacked for p in actionable)
        console.print(
            f"[yellow]{total} commit(s) are on no remote[/]: after this they are reachable\n"
            "only through the reflog of the repo that held them."
        )


def _display_dry_run(plans: list[ResetPlan], ws_dir: Path) -> None:
    actionable = [p for p in plans if not p.is_skipped and not p.is_noop]
    if not actionable:
        console.print("\n[dim]Would run: nothing to do[/]")
        return

    console.print("\n[dim]Would run:[/]")
    for plan in actionable:
        console.print(f"  [{plan.alias}] cd {ws_dir / plan.alias}", markup=False)
        for step in plan.steps:
            console.print(f"  [{plan.alias}] git {' '.join(step.args)}", markup=False)


def _report_skip(plan: ResetPlan) -> None:
    err_console.print(f"  Skipping {plan.alias}: {plan.skip_reason}", markup=False)
    if plan.resume:
        cont, abort = plan.resume
        err_console.print(f"    resume with: {cont}", markup=False)
        err_console.print(f"    or abort:    {abort}", markup=False)


def _execute(plan: ResetPlan, worktree: Path) -> bool:
    """Run a plan's steps. Returns True on success."""
    console.print(f"  {plan.alias}:", markup=False)
    for step in plan.steps:
        result = git(worktree, *step.args)
        if result.returncode != 0:
            err_console.print(
                f"\n  [red]Error[/] in [bold]{escape(plan.alias)}[/]: "
                f"git {escape(' '.join(step.args))} failed"
            )
            err_console.print("    git's output above says why", markup=False)
            err_console.print(f"    cd {worktree}", markup=False)
            err_console.print(f"    then re-run: ow reset --only {escape(plan.alias)}\n")
            return False
    console.print("    Done.")
    return True


def cmd_reset(
    config: Config,
    workspace: str | None = None,
    *,
    only: str | None = None,
    hard: bool = False,
    fetch: bool = False,
    dry_run: bool = False,
    yes: bool = False,
) -> None:
    """Put the repos of a workspace back on the refs they follow.

    `git reset`, one repo at a time, onto the ref `git reset @{u}` would
    have used: the branch's upstream when it has one, the base ref it was
    cut from otherwise. Resetting an attached branch onto its base would
    throw the whole branch away, which is not what resetting a repo means.

    The plain form moves HEAD and leaves the working tree untouched, so
    the content of the dropped commits is still on disk as unstaged
    changes and nothing is lost; `--hard` discards the working tree as
    well. Untracked files are never touched.

    No fetch unless asked: this resets to the refs already in the bare
    repos, the way `git reset origin/master` does. `--fetch` refreshes
    them first, which is what an upstream that was force-pushed needs.
    """
    ws_dir, ws = resolve_workspace(name=workspace)
    report_pending_migration(config, ws, ws_dir)
    aliases = select_aliases(list(ws.repos), only)

    # --only must also narrow resolution and fetching, not just execution.
    # `replace` keeps the schema version, the typed overrides and any legacy
    # evidence: a narrowed view of the workspace is still the same workspace.
    selected_ws = replace(ws, repos={alias: ws.repos[alias] for alias in aliases})

    resolved = fetch_workspace_refs(
        selected_ws, ws_dir, config, fetch_upstreams=True,
        resolve_fn=resolve_spec, spinner_prefix="Checking", fetch=fetch,
    )

    failed = False

    tasks: dict[str, Any] = {}
    for alias in aliases:
        worktree = ws_dir / alias
        if not worktree.exists():
            err_console.print(f"  Skipping {alias}: worktree not found at {worktree}", markup=False)
            failed = True
            continue
        if alias in resolved.failed:
            reason = "fetch failed, refs are stale" if fetch else "could not resolve refs locally"
            err_console.print(f"  Skipping {alias}: {reason}", markup=False)
            failed = True
            continue
        # @{u} first: the branch's own remote copy is what a repo is reset
        # to. get_upstream reads git's own config and needs no network, so
        # it answers even when nothing was fetched; the resolved upstream
        # covers a branch git is not tracking yet. A detached worktree has
        # neither, and falls back to the base ref, which is its whole spec.
        upstream = get_upstream(worktree) or resolved.upstreams.get(alias)
        target = upstream or resolved.tracks.get(alias, ws.repos[alias].base_ref)
        tasks[alias] = (
            lambda w=worktree, a=alias, s=ws.repos[alias], t=target:
            gather_reset_facts(w, a, s, t)
        )

    if not tasks:
        if failed:
            sys.exit(1)
        print_render_pointer()
        return

    results = parallel_per_repo(tasks)
    plans: list[ResetPlan] = []
    for alias in aliases:
        if alias not in results:
            continue
        result = results[alias]
        if isinstance(result, Exception):
            err_console.print(f"  Skipping {alias}: could not analyse — {result}", markup=False)
            failed = True
            continue
        plans.append(plan_reset(result, hard=hard))

    if not plans:
        if failed:
            sys.exit(1)
        print_render_pointer()
        return

    _display_summary(ws_dir.name, plans, hard=hard)

    if dry_run:
        _display_dry_run(plans, ws_dir)
        if failed:
            sys.exit(1)
        print_render_pointer()
        return

    actionable = [p for p in plans if not p.is_skipped and not p.is_noop]
    if not actionable:
        for plan in plans:
            if plan.is_skipped:
                _report_skip(plan)
                failed = True
        if failed:
            sys.exit(1)
        print_render_pointer()
        return

    if not yes and not confirm():
        console.print("Aborted.")
        print_render_pointer()
        sys.exit(2)

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
    if failed or render_failed:
        sys.exit(1)
