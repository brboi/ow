import sys
from pathlib import Path
from typing import Any

from rich.markup import escape
from rich.text import Text

from ow.utils import paths
from ow.utils.config import BranchSpec, Config, WorkspaceConfig, select_aliases, write_workspace_config
from ow.utils.display import console, err_console, task_progress
from ow.utils.git import (
    get_all_remote_refs,
    get_configured_upstream,
    get_upstream,
    get_worktree_branch,
    git,
    in_progress_operation,
    ordered_remotes,
    parallel_per_repo,
    repo_remotes,
    rev_parse,
    set_branch_upstream,
)
from ow.utils.resolver import resolve_workspace
from ow.utils.switch_plan import SwitchFacts, SwitchPlan, plan_switch


def _tracking_matches(worktree: Path, ref: str) -> list[str]:
    """Remote-tracking branches whose short name is exactly `ref`."""
    return [
        r for r in get_all_remote_refs(worktree) if r.rsplit("/", 1)[-1] == ref
    ]


def _dwim_remote(worktree: Path, ref: str) -> str | None:
    """The single remote `ref` could be branched off, or None.

    None both when `ref` already resolves here — a local branch, a tag, a
    sha, a fully-qualified remote branch — and when no unique
    remote-tracking branch carries that short name, which is git's own
    rule for refusing to guess.
    """
    if rev_parse(worktree, ref) is not None:
        return None
    matches = _tracking_matches(worktree, ref)
    if len(matches) != 1:
        return None
    return matches[0].rsplit("/", 1)[0]


def _resolves(worktree: Path, ref: str) -> bool:
    """Would a switch to `ref` find anything here, without fetching first?"""
    return rev_parse(worktree, ref) is not None or len(_tracking_matches(worktree, ref)) == 1


def _remote_has(worktree: Path, remote: str, branch: str) -> bool:
    """Does `remote` publish `branch`? One round trip, writing nothing."""
    result = git(
        worktree, "ls-remote", "--heads", remote, f"refs/heads/{branch}",
        quiet=True, capture_output=True, text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _fetch_target(worktree: Path, target: str, alias_remotes: dict) -> None:
    """Bring `target` into this repo's refs, once, before giving up on it.

    Bare repos are cloned `--single-branch`, so a plain `git fetch <remote>`
    only refreshes the branches the remote's refspec already maps: a branch
    nobody has ever fetched stays invisible however often it runs. ow
    fetches it by explicit refspec instead, like every other command here,
    which also spares a full fetch of an Odoo-sized repository.

    Which remote to ask is settled first, by asking all of them at once:
    `ls-remote` is a single round trip that writes nothing, so the probes
    can run concurrently without two fetches contending for the same
    packed-refs lock — and a branch that exists nowhere costs one round
    trip in total rather than one per remote, in series.

    The remotes come from the repo first, and only then from the global
    config: a bare repo keeps every remote it was set up with, while the
    config describes what new workspaces should get. A workspace whose
    alias has no `[remotes.<alias>]` entry any more would otherwise be
    told its branch does not exist, without a single fetch being attempted.
    """
    remotes = repo_remotes(worktree)
    remotes += [r for r in ordered_remotes(alias_remotes) if r not in remotes]
    qualifier, _, branch = target.partition("/")
    if branch and qualifier in remotes:
        candidates = [(qualifier, branch)]
    else:
        candidates = [(remote, target) for remote in remotes]

    probes = {
        remote: (lambda w=worktree, r=remote, b=branch_name: _remote_has(w, r, b))
        for remote, branch_name in candidates
    }
    # parallel_per_repo is keyed by a label, not by an alias: here one label
    # per remote of a single repo. It is used for its interrupt handling —
    # a Ctrl-C must kill the git children, not join them.
    carriers = parallel_per_repo(probes)

    for remote, branch_name in candidates:
        if carriers.get(remote) is not True:
            continue
        git(
            worktree, "fetch", remote,
            f"+refs/heads/{branch_name}:refs/remotes/{remote}/{branch_name}",
            quiet=True, capture_output=True, text=True,
        )
        if _resolves(worktree, target):
            return


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
        return SwitchFacts(alias=alias, dwim_remote=_dwim_remote(worktree, target))

    _fetch_target(worktree, target, alias_remotes)

    return SwitchFacts(
        alias=alias,
        target_resolvable=_resolves(worktree, target),
        dwim_remote=_dwim_remote(worktree, target),
    )


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
    plan: SwitchPlan, worktree: Path, *, target: str | None, create: str | None, detach: bool,
) -> bool:
    """Run the switch, and record the upstream a DWIM implies. True on success."""
    console.print(f"  {plan.alias}:", markup=False)
    result = git(worktree, *plan.args)
    if result.returncode != 0:
        err_console.print(
            f"\n  [red]Error[/] in [bold]{escape(plan.alias)}[/]: git {escape(' '.join(plan.args))} failed"
        )
        err_console.print("    git's output above says why", markup=False)
        err_console.print(f"    cd {worktree}", markup=False)
        err_console.print(f"    then re-run: {_repro_hint(target, create, detach, plan.alias)}\n", markup=False)
        return False
    if plan.upstream is not None:
        remote, branch = plan.upstream
        set_branch_upstream(paths.repos_dir() / f"{plan.alias}.git", branch, remote, branch)
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

    # `@{u}` is blind to the branches ow attached itself — they track a ref
    # fetched outside the remote's refspec — so the config pair answers
    # when git's own shorthand cannot.
    upstream = get_upstream(worktree) or get_configured_upstream(worktree)
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

    needs_resolution = target is not None

    tasks: dict[str, Any] = {
        alias: (
            lambda w=ws_dir / alias, a=alias: gather_switch_facts(
                w, a, target, needs_resolution=needs_resolution, alias_remotes=config.remotes.get(alias, {}),
            )
        )
        for alias in aliases
    }
    # The pre-flight can reach the network — a target nobody fetched yet is
    # looked up on every remote — and silence for that long reads as a hang.
    with task_progress(f"Checking {target or create}", len(tasks)) as advance:
        results = parallel_per_repo(tasks, on_done=lambda _alias: advance())

    plans: list[SwitchPlan] = []
    preflight_failed = False
    for alias in aliases:
        result = results[alias]
        if isinstance(result, Exception):
            plan = SwitchPlan(alias=alias, skip_reason=f"could not analyse — {result}")
        else:
            plan = plan_switch(result, target=target, create=create, detach=detach)
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
        if not _execute(plan, worktree, target=target, create=create, detach=detach):
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
