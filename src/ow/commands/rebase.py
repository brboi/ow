import sys
from dataclasses import dataclass
from typing import Any

from ow.utils.display import console
from rich.text import Text
from ow.utils.drift import warn_if_drifted
from ow.utils.refs import fetch_workspace_refs
from ow.utils.resolver import resolve_workspace
from ow.utils.config import Config
from ow.utils.git import (
    get_rev_list_count,
    get_worktree_branch,
    git_cherry_pick,
    git_log_oneline,
    git_merge_base_fork_point,
    git_rebase,
    git_reset_hard,
    git_rev_list,
    git_switch,
    parallel_per_repo,
    resolve_spec,
)


def _report_conflict(alias: str, worktree_path, onto_ref: str) -> None:
    """Print conflict resolution instructions."""
    console.print(
        f"\n  [red]CONFLICT[/] in [bold]{alias}[/] rebasing onto {onto_ref}",
    )
    console.print("    resolve conflicts, then:")
    console.print(f"      cd {worktree_path}")
    console.print("      git rebase --continue")
    console.print("    or abort:")
    console.print("      git rebase --abort\n")


@dataclass
class RebasePlan:
    """Plan for rebasing a single repo."""
    alias: str
    track_ref: str
    upstream: str | None
    is_detached: bool
    local_commits: int
    unpushed_commits: int
    fork_point: str | None
    commits_to_reapply: list[str]
    upstream_rewritten: bool
    has_conflicts: bool


def _analyze_repo_for_rebase(
    worktree, track_ref: str, upstream: str | None, alias: str, is_detached: bool
) -> RebasePlan:
    """Analyze the rebase situation for a single repo."""
    rebase_merge = worktree / ".git" / "rebase-merge"
    has_conflicts = rebase_merge.exists()

    local_commits, _ = get_rev_list_count(worktree, "HEAD", upstream or track_ref)

    fork_point = None
    commits_to_reapply: list[str] = []
    upstream_rewritten = False
    unpushed_commits = 0

    if upstream:
        unpushed_commits, _ = get_rev_list_count(worktree, "HEAD", upstream)
        branch = get_worktree_branch(worktree)
        if branch:
            fork_point = git_merge_base_fork_point(worktree, upstream, branch)
        if fork_point:
            commits_to_reapply = git_rev_list(worktree, f"{fork_point}..HEAD", reverse=True)
        upstream_rewritten = fork_point is None and unpushed_commits > 0

    return RebasePlan(
        alias=alias,
        track_ref=track_ref,
        upstream=upstream,
        is_detached=is_detached,
        local_commits=local_commits,
        unpushed_commits=unpushed_commits,
        fork_point=fork_point,
        commits_to_reapply=commits_to_reapply,
        upstream_rewritten=upstream_rewritten,
        has_conflicts=has_conflicts,
    )


def _display_rebase_summary(plans: list[RebasePlan]) -> None:
    """Display rebase summary for all repos."""
    for p in plans:
        parts = [p.track_ref]
        if p.upstream:
            parts.append(f"← {p.upstream}")
        parts.append(f"({p.local_commits} commits)")

        markers = []
        if p.upstream_rewritten:
            if p.fork_point:
                markers.append("[yellow]rewritten, recoverable[/]")
            else:
                markers.append("[red]rewritten, no fork-point[/]")
        elif p.unpushed_commits > 0 and p.upstream:
            markers.append(f"[yellow]{p.unpushed_commits} unpushed[/]")
        if p.has_conflicts:
            markers.append("[red]in progress[/]")
        if markers:
            parts.append("[" + ", ".join(markers) + "]")

        console.print(f"  {p.alias}: {' → '.join(parts)}")


def _recover_with_cherry_pick(worktree, upstream: str, commits: list[str]) -> str | None:
    """Reset hard to upstream and cherry-pick commits.

    Returns None on success, or the failing commit hash on conflict.
    """
    git_reset_hard(worktree, upstream)

    for i, commit in enumerate(commits, 1):
        msg = git_log_oneline(worktree, commit)
        console.print(f"    Cherry-picking {i}/{len(commits)}: {msg}")
        result = git_cherry_pick(worktree, commit)
        if result.returncode != 0:
            return commit

    return None


def _do_rebase(worktree, upstream: str | None, track_ref: str) -> bool:
    """Execute rebase onto upstream then track_ref. Returns True on success."""
    if upstream:
        result = git_rebase(worktree, upstream)
        if result.returncode != 0:
            return False
    result = git_rebase(worktree, track_ref)
    return result.returncode == 0


def cmd_rebase(config: Config, workspace: str | None = None) -> None:
    """Fetch and rebase all repos in the current workspace."""
    ws_dir, ws = resolve_workspace(config, name=workspace)

    warn_if_drifted(ws, ws_dir)

    resolved_tracks, resolved_upstreams, _ = fetch_workspace_refs(
        ws, ws_dir, config, fetch_upstreams=True,
        resolve_fn=resolve_spec, spinner_prefix="Preparing",
    )

    # Parallelize rebase analysis
    analysis_tasks: dict[str, Any] = {}
    for alias, spec in ws.repos.items():
        worktree = ws_dir / alias
        if not worktree.exists():
            continue
        track_ref = resolved_tracks[alias]
        upstream = resolved_upstreams.get(alias)
        analysis_tasks[alias] = (
            lambda w=worktree, t=track_ref, u=upstream, a=alias, d=spec.is_detached:
            _analyze_repo_for_rebase(w, t, u, a, d)
        )

    if analysis_tasks:
        analysis_results = parallel_per_repo(analysis_tasks)
    else:
        analysis_results = {}

    plans: list[RebasePlan] = []
    for alias in ws.repos:
        result = analysis_results.get(alias)
        if result is not None and not isinstance(result, Exception):
            plans.append(result)

    if not plans:
        return

    header = Text(f"[{ws_dir.name}]", style="bold cyan")
    console.print(header)
    _display_rebase_summary(plans)

    has_rewritten_no_fork = any(
        p.upstream_rewritten and p.fork_point is None
        for p in plans
    )
    if has_rewritten_no_fork:
        console.print(f"\n  [red]Error:[/] Cannot recover some repos - fork-point not found.")
        console.print("  Manual recovery required:")
        for p in plans:
            if p.upstream_rewritten and p.fork_point is None:
                console.print(f"    {p.alias}:")
                console.print("      git reflog HEAD | head -20  # find previous state")
                console.print("      git cherry-pick <commit>...  # manually reapply")
        console.print()

    has_recoverable = any(
        p.upstream_rewritten and p.fork_point is not None
        for p in plans
    )
    if has_recoverable:
        console.print(f"\n  [yellow]Recovery:[/] reset --hard + cherry-pick for rewritten upstreams")
        for p in plans:
            if p.upstream_rewritten and p.fork_point:
                console.print(f"    {p.alias}: {len(p.commits_to_reapply)} commits to reapply")

    has_warnings = any(
        p.unpushed_commits > 0 and p.upstream and not p.upstream_rewritten
        for p in plans
    )
    if has_warnings:
        console.print(f"\n  [yellow]Warning:[/] unpushed commits may cause conflicts")

    try:
        response = input("\nProceed? [Y/n] ")
    except EOFError:
        response = ""

    if response.lower() == "n":
        console.print("Aborted.")
        return

    failed = []
    for plan in plans:
        worktree = ws_dir / plan.alias

        if plan.has_conflicts:
            console.print(
                f"  Skipping {plan.alias}: rebase already in progress",
            )
            continue

        if plan.upstream_rewritten and plan.fork_point is None:
            console.print(
                f"  Skipping {plan.alias}: no fork-point, manual recovery required",
            )
            continue

        console.print(f"  {plan.alias}:")

        if plan.is_detached:
            git_switch(worktree, plan.track_ref, detach=True, check=True)
            console.print("    Done (detached).")
        elif plan.upstream_rewritten and plan.fork_point and plan.upstream:
            failed_commit = _recover_with_cherry_pick(
                worktree, plan.upstream, plan.commits_to_reapply
            )
            if failed_commit:
                console.print(
                    f"\n    [red]CONFLICT[/] cherry-picking {failed_commit}",
                )
                console.print("    resolve conflicts, then:")
                console.print(f"      cd {worktree}")
                console.print("      git cherry-pick --continue")
                console.print("    or abort:")
                console.print("      git cherry-pick --abort\n")
                failed.append(plan.alias)
            else:
                console.print("    Done (recovered).")
        else:
            if not _do_rebase(worktree, plan.upstream, plan.track_ref):
                _report_conflict(
                    plan.alias, worktree, plan.upstream or plan.track_ref
                )
                failed.append(plan.alias)
            else:
                console.print("    Done.")

    if failed:
        sys.exit(1)
