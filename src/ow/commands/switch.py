"""`ow switch` — every repo onto one branch, or off them all."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ow import term
from ow.commands._shared import refresh_after
from ow.core import bases, git
from ow.core.facts import RepoFacts, gather_all
from ow.core.plan import Batch, GitStep, Outcome, RepoPlan, run_batch
from ow.core.session import Session
from ow.errors import UsageError
from ow.recipes import preflight, removal
from ow.recipes import switch as recipe

NO_BASE = "no default base; run `ow init`"


def run(
    session: Session,
    target: str | None,
    *,
    create: str | None,
    base: str | None,
    detach: bool,
    only: tuple[str, ...],
    dry_run: bool,
) -> int:
    """Switch every repo (or --only) to a branch, create it with -c, or --detach.

    Pre-flight is all-or-nothing: an operation in progress, a broken worktree,
    the branch checked out elsewhere, a detached HEAD with orphaned commits, an
    unreachable remote or two remotes publishing the branch refuses every repo.
    A repo the branch is nowhere on is listed (`no <b>`) and stays. A failure
    lists which repos switched and which did not; a re-run converges.
    """
    assert session.workspace is not None
    root = session.workspace.root
    locations = [loc for loc in session.locations if not only or loc.name in only]
    facts_list = gather_all(locations)
    branch = target if target is not None else create

    blockers: list[tuple[str, str]] = []
    for facts in facts_list:
        reason = preflight.refusal(facts, "switch", root=root)
        if reason is None and branch is not None:
            if _checked_out_elsewhere(facts.bare, facts.path, branch):
                reason = f"{branch} is checked out in another worktree"
        if reason is None:
            reason = _orphan_refusal(facts, root)
        if reason is not None:
            blockers.append((facts.name, reason))
    if blockers:
        return _refuse_all(root, facts_list, blockers, dry_run)

    found: dict[str, recipe.Lookup] = {}
    cached: dict[str, frozenset[str]] = {}
    if branch is not None:
        local = {facts.name: _local_branches(facts.bare) for facts in facts_list}
        cached = {facts.name: _remote_branches(facts.bare) for facts in facts_list}
        published, failure = _query_remotes(facts_list, branch, local, cached)
        if failure is not None:
            name, reason = failure
            return _refuse_all(root, facts_list, [(name, reason)], dry_run)
        for facts in facts_list:
            found[facts.name] = recipe.lookup(
                facts,
                branch,
                local[facts.name],
                cached[facts.name],
                published[facts.name],
                create=create is not None,
            )
        ambiguous = [
            (facts.name, _ambiguous_reason(facts, branch, found[facts.name], root))
            for facts in facts_list
            if found[facts.name].mode == "ambiguous"
        ]
        if ambiguous:
            return _refuse_all(root, facts_list, ambiguous, dry_run)
        if all(found[facts.name].mode == "none" for facts in facts_list):
            raise UsageError(f"no repo has {branch}; create it with `ow switch -c {branch}`")

    plans = [
        _plan(facts, branch, found.get(facts.name), cached.get(facts.name, frozenset()), base, detach)
        for facts in facts_list
    ]
    outcome = run_batch(
        Batch(root=root, repos=tuple(plans)), prompt=False, dry_run=dry_run, yes=True
    )
    code = outcome.exit_code
    if outcome.failed:
        _report_split(outcome, plans)
    if outcome.mutated:
        code = max(code, refresh_after(session))
    return code


def _plan(
    facts: RepoFacts,
    branch: str | None,
    result: recipe.Lookup | None,
    cached: frozenset[str],
    base: str | None,
    detach: bool,
) -> RepoPlan:
    """One repo's plan: detach, stay, or switch/create with the right base."""
    if detach:
        if facts.default_base is None:
            return RepoPlan(repo=facts.name, refusal=NO_BASE)
        return RepoPlan(repo=facts.name, steps=recipe.detach_steps(facts))

    assert result is not None and branch is not None
    if result.mode == "none":
        return RepoPlan(repo=facts.name, notes=(f"no {branch}",))
    if result.mode == "create":
        create_base = _create_base(facts, base)
        if create_base is None:
            return RepoPlan(repo=facts.name, refusal=NO_BASE)
        return RepoPlan(repo=facts.name, steps=_create_steps(facts, branch, create_base, cached))
    if result.mode in ("track", "remote-fetch"):
        if facts.default_base is None:
            return RepoPlan(repo=facts.name, refusal=NO_BASE)
        notes = _base_ignored(branch) if base is not None else ()
        return RepoPlan(repo=facts.name, steps=recipe.steps(facts, branch, result), notes=notes)
    assert result.mode == "local"
    repair = _repair_base(facts, branch, base)
    notes = _base_ignored(branch) if base is not None and repair is None else ()
    return RepoPlan(
        repo=facts.name,
        steps=recipe.steps(facts, branch, result, repair_base=repair),
        notes=notes,
    )


def _base_ignored(branch: str) -> tuple[str, ...]:
    """The note for `-c --base` finding the branch already there, so `--base` did nothing."""
    return (f"--base ignored; {branch} already exists",)


def _create_steps(
    facts: RepoFacts, branch: str, create_base: str, cached: frozenset[str]
) -> tuple[GitStep, ...]:
    """The create steps, fetching the base first when it is not cached."""
    steps: list[GitStep] = []
    if create_base not in cached:
        remote, base_branch = create_base.split("/", 1)
        refspec = f"+refs/heads/{base_branch}:refs/remotes/{remote}/{base_branch}"
        steps.append(GitStep(("git", "-C", str(facts.path), "fetch", remote, refspec)))
    steps.extend(recipe.steps(facts, branch, recipe.Lookup("create"), create_base=create_base))
    return tuple(steps)


def _refuse_all(
    root: Path,
    facts_list: tuple[RepoFacts, ...],
    blockers: list[tuple[str, str]],
    dry_run: bool,
) -> int:
    """Refuse every repo: each cause once, `nothing switched` for the rest."""
    reasons = dict(blockers)
    plans = tuple(
        RepoPlan(repo=facts.name, refusal=reasons.get(facts.name, "nothing switched"))
        for facts in facts_list
    )
    outcome = run_batch(Batch(root=root, repos=plans), prompt=False, dry_run=dry_run, yes=True)
    return outcome.exit_code


def _report_split(outcome: Outcome, plans: list[RepoPlan]) -> None:
    """Which repos switched and which did not, after a failure split the workspace."""
    switched = [plan.repo for plan in plans if plan.steps and plan.repo in outcome.ok]
    not_switched = [plan.repo for plan in plans if plan.repo not in switched]
    if switched:
        term.out(f"switched: {', '.join(switched)}")
    term.out(f"not switched: {', '.join(not_switched)}")


def _ambiguous_reason(facts: RepoFacts, branch: str, result: recipe.Lookup, root: Path) -> str:
    """The refusal for a branch two remotes publish, with the by-hand command."""
    remotes = ", ".join(sorted(result.ambiguous))
    command = recipe.hint(facts, branch, result.ambiguous, root)
    return f"{branch} is on remotes {remotes}; run {command}"


def _orphan_refusal(facts: RepoFacts, root: Path) -> str | None:
    """A detached HEAD with commits no branch holds would lose them on a switch."""
    if facts.branch is not None or facts.head is None:
        return None
    if removal.orphan_commits(facts.bare, facts.head) == 0:
        return None
    shown = git.show(("git", "-C", str(facts.path), "switch", "-c"), root)
    return f"commits on no branch; save them with `{shown} <b>`"


def _query_remotes(
    facts_list: tuple[RepoFacts, ...],
    branch: str,
    local: dict[str, frozenset[str]],
    cached: dict[str, frozenset[str]],
) -> tuple[dict[str, set[str]], tuple[str, str] | None]:
    """Remotes publishing `branch` (ls-remote, in parallel); the first failure aborts."""
    published: dict[str, set[str]] = {facts.name: set() for facts in facts_list}
    jobs = [
        (facts, remote)
        for facts in facts_list
        if branch not in local[facts.name] and not _cached_holder(facts, branch, cached[facts.name])
        for remote in facts.remotes
    ]
    if not jobs:
        return published, None
    with ThreadPoolExecutor() as pool:
        futures = {
            pool.submit(git.remote_has_branch, facts.bare, remote, branch): (facts, remote)
            for facts, remote in jobs
        }
        for future, (facts, remote) in futures.items():
            try:
                if future.result():
                    published[facts.name].add(remote)
            except git.GitFailed as failure:
                return published, (facts.name, _ls_remote_reason(remote, failure))
    return published, None


def _ls_remote_reason(remote: str, failure: git.GitFailed) -> str:
    """The whole-run refusal for an unreachable remote, git's reason included."""
    detail = next((line for line in failure.stderr.splitlines() if line.strip()), "no output")
    hint = git.auth_hint(failure.stderr)
    return f"ls-remote {remote} failed: {detail}" + (f"; {hint}" if hint else "")


def _cached_holder(facts: RepoFacts, branch: str, cached: frozenset[str]) -> bool:
    """Whether a current remote of the repo already tracks `branch`."""
    return any(f"{remote}/{branch}" in cached for remote in facts.remotes)


def _create_base(facts: RepoFacts, base: str | None) -> str | None:
    """The base a `-c` creation starts from: `--base` parsed, else the default base."""
    if base is not None:
        remotes = set(facts.remotes) or {"origin"}
        return bases.parse_base(base, lambda segment: segment in remotes)
    return facts.default_base


def _repair_base(facts: RepoFacts, branch: str, base: str | None) -> str | None:
    """The base to write for a local branch that lacks `branch.<b>.ow-base`, else None."""
    if _ow_base(facts.bare, branch) is not None:
        return None
    return _create_base(facts, base)


def _ow_base(bare: Path, branch: str) -> str | None:
    """The stored `branch.<b>.ow-base` of a bare, or None."""
    result = git.run(["git", "-C", str(bare), "config", "--get", f"branch.{branch}.ow-base"])
    return result.stdout.strip() if result.returncode == 0 else None


def _local_branches(bare: Path) -> frozenset[str]:
    """Local branch names of a bare."""
    output = git.read(
        ["git", "-C", str(bare), "for-each-ref", "--format=%(refname:lstrip=2)", "refs/heads"]
    )
    return frozenset(output.split("\n")) if output else frozenset()


def _remote_branches(bare: Path) -> frozenset[str]:
    """Remote-tracking ref names of a bare, as `<remote>/<branch>`."""
    output = git.read(
        ["git", "-C", str(bare), "for-each-ref", "--format=%(refname:lstrip=2)", "refs/remotes"]
    )
    return frozenset(output.split("\n")) if output else frozenset()


def _checked_out_elsewhere(bare: Path, worktree: Path, branch: str) -> bool:
    """Whether another worktree of this bare has `branch` checked out."""
    listing = git.read(["git", "-C", str(bare), "worktree", "list", "--porcelain"])
    current: Path | None = None
    for line in listing.split("\n"):
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):])
        elif current is not None and line == f"branch refs/heads/{branch}":
            if current.resolve() != worktree.resolve():
                return True
    return False
