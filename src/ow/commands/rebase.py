import sys
from dataclasses import dataclass
from typing import Any

from ow.utils.display import c
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
    print(
        f"\n  {c('CONFLICT', 31)} in {c(alias, 1)} rebasing onto {onto_ref}",
        file=sys.stderr,
    )
    print("    resolve conflicts, then:", file=sys.stderr)
    print(f"      cd {worktree_path}", file=sys.stderr)
    print("      git rebase --continue", file=sys.stderr)
    print("    or abort:", file=sys.stderr)
    print("      git rebase --abort\n", file=sys.stderr)


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
                markers.append(c("rewritten, recoverable", 33))
            else:
                markers.append(c("rewritten, no fork-point", 31))
        elif p.unpushed_commits > 0 and p.upstream:
            markers.append(c(f"{p.unpushed_commits} unpushed", 33))
        if p.has_conflicts:
            markers.append(c("in progress", 31))
        if markers:
            parts.append("[" + ", ".join(markers) + "]")

        print(f"  {p.alias}: {' → '.join(parts)}")


def _recover_with_cherry_pick(worktree, upstream: str, commits: list[str]) -> str | None:
    """Reset hard to upstream and cherry-pick commits.

    Returns None on success, or the failing commit hash on conflict.
    """
    git_reset_hard(worktree, upstream)

    for i, commit in enumerate(commits, 1):
        msg = git_log_oneline(worktree, commit)
        print(f"    Cherry-picking {i}/{len(commits)}: {msg}")
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

    print(c(f"[{ws_dir.name}]", 1, 36))
    _display_rebase_summary(plans)

    has_rewritten_no_fork = any(
        p.upstream_rewritten and p.fork_point is None
        for p in plans
    )
    if has_rewritten_no_fork:
        error_label = c("Error:", 31)
        print(f"\n  {error_label} Cannot recover some repos - fork-point not found.", file=sys.stderr)
        print("  Manual recovery required:", file=sys.stderr)
        for p in plans:
            if p.upstream_rewritten and p.fork_point is None:
                print(f"    {p.alias}:", file=sys.stderr)
                print("      git reflog HEAD | head -20  # find previous state", file=sys.stderr)
                print("      git cherry-pick <commit>...  # manually reapply", file=sys.stderr)
        print()

    has_recoverable = any(
        p.upstream_rewritten and p.fork_point is not None
        for p in plans
    )
    if has_recoverable:
        recovery_label = c("Recovery:", 33)
        print(f"\n  {recovery_label} reset --hard + cherry-pick for rewritten upstreams", file=sys.stderr)
        for p in plans:
            if p.upstream_rewritten and p.fork_point:
                print(f"    {p.alias}: {len(p.commits_to_reapply)} commits to reapply", file=sys.stderr)

    has_warnings = any(
        p.unpushed_commits > 0 and p.upstream and not p.upstream_rewritten
        for p in plans
    )
    if has_warnings:
        warning_label = c("Warning:", 33)
        print(f"\n  {warning_label} unpushed commits may cause conflicts", file=sys.stderr)

    try:
        response = input("\nProceed? [Y/n] ")
    except EOFError:
        response = ""

    if response.lower() == "n":
        print("Aborted.")
        return

    failed = []
    for plan in plans:
        worktree = ws_dir / plan.alias

        if plan.has_conflicts:
            print(
                f"  Skipping {plan.alias}: rebase already in progress",
                file=sys.stderr,
            )
            continue

        if plan.upstream_rewritten and plan.fork_point is None:
            print(
                f"  Skipping {plan.alias}: no fork-point, manual recovery required",
                file=sys.stderr,
            )
            continue

        print(f"  {plan.alias}:")

        if plan.is_detached:
            git_switch(worktree, plan.track_ref, detach=True, check=True)
            print("    Done (detached).")
        elif plan.upstream_rewritten and plan.fork_point and plan.upstream:
            failed_commit = _recover_with_cherry_pick(
                worktree, plan.upstream, plan.commits_to_reapply
            )
            if failed_commit:
                print(
                    f"\n    {c('CONFLICT', 31)} cherry-picking {failed_commit}",
                    file=sys.stderr,
                )
                print("    resolve conflicts, then:", file=sys.stderr)
                print(f"      cd {worktree}", file=sys.stderr)
                print("      git cherry-pick --continue", file=sys.stderr)
                print("    or abort:", file=sys.stderr)
                print("      git cherry-pick --abort\n", file=sys.stderr)
                failed.append(plan.alias)
            else:
                print("    Done (recovered).")
        else:
            if not _do_rebase(worktree, plan.upstream, plan.track_ref):
                _report_conflict(
                    plan.alias, worktree, plan.upstream or plan.track_ref
                )
                failed.append(plan.alias)
            else:
                print("    Done.")

    if failed:
        sys.exit(1)
