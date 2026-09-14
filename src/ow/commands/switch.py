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
    create: str | None = None,
) -> SwitchFacts:
    """Observe one repo. No decisions are taken here.

    Resolution is local-first: a fetch only happens when `target` is
    unknown here, and at most once, against every remote this repo has.
    A repo already on the target is the cheapest case of all — no
    resolution, no network — which is what `ow switch master` on a
    workspace that never left master should cost.
    """
    if not worktree.exists():
        return SwitchFacts(alias=alias, worktree_missing=True)

    busy = in_progress_operation(worktree)
    if busy is not None:
        return SwitchFacts(alias=alias, busy=busy)

    branch = get_worktree_branch(worktree)
    resolvable, dwim = True, None
    if needs_resolution and target is not None and target != branch:
        if not _resolves(worktree, target):
            _fetch_target(worktree, target, alias_remotes)
            resolvable = _resolves(worktree, target)
        dwim = _dwim_remote(worktree, target)

    return SwitchFacts(
        alias=alias,
        target_resolvable=resolvable,
        dwim_remote=dwim,
        current_branch=branch,
        create_exists=create is not None and rev_parse(worktree, f"refs/heads/{create}") is not None,
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


def _header(ws_name: str, *, target: str | None, create: str | None, detach: bool) -> Text:
    """`[ws] what this run is about`, in the shape every command uses."""
    if create is not None:
        what = f"create {create}" + (f" from {target}" if target else "")
    elif detach:
        what = f"detach at {target}"
    else:
        what = f"switch to {target}"
    return Text(f"[{ws_name}] {what}", style="bold cyan")


def _summary_line(plan: SwitchPlan, alias_width: int, current: str, spec_width: int) -> str:
    if plan.is_skipped:
        state = f"[yellow]refused[/] — {escape(plan.skip_reason or '')}"
    elif plan.is_noop:
        state = "[dim]already there[/]"
    elif plan.action == "track":
        remote, branch = plan.upstream or ("", "")
        state = f"new branch tracking {remote}/{branch}"
    elif plan.action == "create":
        start = plan.args[3] if len(plan.args) > 3 else None
        state = f"new branch from {start}" if start else "new branch from HEAD"
    elif plan.action == "detach":
        state = "detach"
    else:
        state = "local branch"
    return f"  {plan.alias.ljust(alias_width)}  {escape(current.ljust(spec_width))}  {state}"


def _display_summary(
    ws_name: str, aliases: list[str], plans: list[SwitchPlan], repos: dict[str, BranchSpec], *,
    target: str | None, create: str | None, detach: bool,
) -> None:
    """What each repo is on, and what this run would make of it.

    Printed before anything runs, like `ow reset` and `ow pull` do: a
    switch moves the whole workspace, so what it is about to do to every
    repo belongs above git's output, not after it. A repo the policy
    leaves out appears here too — in the table it belongs to, at the
    order the config lists it in — because a repo that silently drops
    out of the run reads as a bug, not as a decision made on its behalf.
    """
    console.print(_header(ws_name, target=target, create=create, detach=detach))
    plan_by_alias = {p.alias: p for p in plans}
    specs = {a: repos[a].to_spec_str() for a in aliases}
    alias_width = max((len(a) for a in aliases), default=0)
    spec_width = max((len(s) for s in specs.values()), default=0)
    for alias in aliases:
        plan = plan_by_alias.get(alias)
        if plan is not None:
            console.print(_summary_line(plan, alias_width, specs[alias], spec_width))
        else:
            state = "[dim]left alone — detached spec[/]"
            console.print(f"  {alias.ljust(alias_width)}  {escape(specs[alias].ljust(spec_width))}  {state}")


def _report_refusals(refused: list[SwitchPlan]) -> None:
    """Why nothing ran, and the one command that would change that.

    The table above already names every repo and its reason; what is left
    is the part a per-repo list cannot say — that the run did nothing at
    all — and the advice, deduplicated: a whole workspace usually fails
    for the same cause, and the same sentence five times is scrolled past.
    """
    for plan in refused:
        if plan.resume:
            cont, abort = plan.resume
            err_console.print(f"\n  {plan.alias}: resume with: {cont}", markup=False)
            err_console.print(f"    or abort:      {abort}", markup=False)

    err_console.print("\n[red]Nothing was switched[/]: a switch moves the whole workspace or none of it.")
    for hint in dict.fromkeys(p.hint for p in refused if p.hint):
        err_console.print(f"  {hint}", markup=False)


def _display_dry_run(plans: list[SwitchPlan], ws_dir: Path) -> None:
    actionable = [p for p in plans if not p.is_noop]
    if not actionable:
        console.print("\n[dim]Would run: nothing to do[/]")
        return

    console.print("\n[dim]Would run:[/]")
    for plan in actionable:
        console.print(f"  [{plan.alias}] cd {ws_dir / plan.alias}", markup=False)
        console.print(f"  [{plan.alias}] git {' '.join(plan.args)}", markup=False)


def _execute(
    plan: SwitchPlan, worktree: Path, *, target: str | None, create: str | None, detach: bool,
) -> bool:
    """Run the switch, and record the upstream a DWIM implies. True on success.

    The closing `Done.` is the caller's: it carries the spec that was
    written for this repo, which is only known once git has moved it.
    """
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


def cmd_switch(
    config: Config,
    target: str | None = None,
    workspace: str | None = None,
    *,
    create: str | None = None,
    detach: bool = False,
    only: str | None = None,
    dry_run: bool = False,
    include_detached: bool = False,
) -> None:
    """`git switch`, one repo at a time, across a workspace.

    Every repo is pre-flighted — worktree present, no operation already
    in progress, target resolvable, and for `-c` no branch of that name
    yet — before any of them is touched: this is not `ow reset`'s
    skip-and-continue, because switching half a workspace and refusing
    the rest leaves it straddling two states. Resolution tries local
    refs first and only fetches, once per repo, when the target is not
    already known.

    A repo configured detached (a bare ref, no `..branch`) is a pin: the
    config names the exact ref it should sit on, and a run that moves the
    workspace's branches has no business rewriting it. Such repos are
    left alone, unless `--include-detached-specs` says otherwise — or a
    `--only` names one, because naming a repo is insisting on it.

    What each repo is about to do is printed first, as a table, the way
    every other multi-repo command here reports; a repo already on the
    target appears in it and is then left entirely alone.

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

    # A detached spec is a pin, not a laggard: the config names the exact
    # ref that repo should sit on, and moving the workspace's branches is
    # no reason to rewrite it. Pins join the run only when asked for —
    # `--include-detached-specs` for all of them, or a `--only` naming one,
    # because naming a repo is insisting on it.
    if include_detached:
        included = aliases
    else:
        named = {a.strip() for a in only.split(",") if a.strip()} if only else set()
        included = [a for a in aliases if not ws.repos[a].is_detached or a in named]

    tasks: dict[str, Any] = {
        alias: (
            lambda w=ws_dir / alias, a=alias: gather_switch_facts(
                w, a, target, needs_resolution=needs_resolution, create=create,
                alias_remotes=config.remotes.get(alias, {}),
            )
        )
        for alias in included
    }
    # The pre-flight can reach the network — a target nobody fetched yet is
    # looked up on every remote — and silence for that long reads as a hang.
    results: dict[str, Any] = {}
    if tasks:
        with task_progress("Checking repo(s)", len(tasks)) as advance:
            results = parallel_per_repo(tasks, on_done=lambda _alias: advance())

    plans: list[SwitchPlan] = []
    for alias in included:
        result = results[alias]
        if isinstance(result, Exception):
            plan = SwitchPlan(alias=alias, skip_reason=f"could not analyse — {result}")
        else:
            plan = plan_switch(result, target=target, create=create, detach=detach)
        plans.append(plan)

    _display_summary(ws_dir.name, aliases, plans, ws.repos, target=target, create=create, detach=detach)

    refused = [p for p in plans if p.is_skipped]
    if refused:
        _report_refusals(refused)
        sys.exit(2)

    excluded = [a for a in aliases if a not in {p.alias for p in plans}]
    if excluded and not include_detached:
        # The table already says "left alone" per repo; what it cannot teach
        # is the flag that includes them, and a policy nobody can discover
        # is indistinguishable from a bug.
        console.print(
            f"\n[dim]{len(excluded)} detached repo(s) left alone — "
            "pass --include-detached-specs to switch them too.[/]"
        )

    if dry_run:
        _display_dry_run(plans, ws_dir)
        return

    runnable = [p for p in plans if not p.is_noop]
    if not runnable:
        return

    console.print()
    touched: dict[str, BranchSpec] = {}
    exec_failed = False
    for plan in runnable:
        worktree = ws_dir / plan.alias
        old = ws.repos[plan.alias]
        if not _execute(plan, worktree, target=target, create=create, detach=detach):
            exec_failed = True
            continue
        # What git was actually given, not what was typed: a remote-only
        # `master` was detached at `<remote>/master`, and that is the ref
        # this repo is pinned to now.
        spec = _new_spec(worktree, target=plan.resolved_target or target, create=create, old=old)
        touched[plan.alias] = spec
        # The spec, next to the repo that produced it: it is what was just
        # written to the config, and it is only knowable after the switch.
        console.print(f"    Done. [dim]now {escape(spec.to_spec_str())}[/]")

    if touched:
        new_repos = dict(ws.repos)
        new_repos.update(touched)
        new_ws = WorkspaceConfig(repos=new_repos, templates=ws.templates, vars=ws.vars)
        write_workspace_config(ws_dir / ".ow" / "config.toml", new_ws)
        console.print(
            "\n[dim]Templates are not re-rendered by a switch — run `ow apply` if you need them refreshed.[/]"
        )

    if exec_failed:
        if touched:
            # A failure here is the one case the pre-flight cannot prevent,
            # and it leaves exactly what the pre-flight exists to avoid.
            stranded = ", ".join(p.alias for p in runnable if p.alias not in touched)
            err_console.print(
                f"  [yellow]The workspace is split[/]: {escape(', '.join(touched))} moved, "
                f"{escape(stranded)} did not."
            )
        sys.exit(1)
