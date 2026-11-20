"""`ow switch` — every repo onto one branch, or off them all (§6)."""

from __future__ import annotations

from pathlib import Path

from ow import term
from ow.commands._shared import refresh_after
from ow.core import bases, git
from ow.core.facts import RepoFacts, gather_all
from ow.core.plan import Batch, Outcome, RepoPlan, run_batch
from ow.core.session import Session
from ow.errors import UsageError
from ow.recipes import preflight
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
    """Switch every repo (or `--only`) to a branch, create it with `-c`, or `--detach`.

    Returns the exit code: 1 when a repo was refused or failed (also on `-n`),
    plus the refresh's code when the batch moved something. The pre-flight is
    all-or-nothing (§6): a repo with an operation in progress, a broken
    worktree, the branch checked out in another worktree or published on two
    remotes refuses the whole batch, each repo as a `# <repo>: <text>` line.
    A repo that has the branch nowhere is listed (`# <repo>: no <b>`) and stays;
    when no repo has it, that is a usage error pointing at `-c`. A missing base
    (`--detach`, or a `-c` with neither `--base` nor a default) refuses only
    that repo (§5). Additive: no prompt, each line printed as it runs; a failure
    lists which repos switched and which did not, and a re-run is idempotent.
    """
    assert session.workspace is not None
    root = session.workspace.root
    locations = [loc for loc in session.locations if not only or loc.name in only]
    branch = target if target is not None else create

    infos: list[tuple[RepoFacts, recipe.Lookup | None, bool]] = []
    for facts in gather_all(locations):
        found: recipe.Lookup | None = None
        elsewhere = False
        if branch is not None:
            found = recipe.lookup(
                facts,
                branch,
                _cached(facts.bare),
                _on_remotes(facts.bare, facts.remotes, branch),
                create=create is not None,
            )
            elsewhere = _checked_out_elsewhere(facts.bare, facts.path, branch)
        infos.append((facts, found, elsewhere))

    blockers: list[tuple[str, str]] = []
    for facts, found, elsewhere in infos:
        reason = preflight.refusal(facts, "switch")
        if reason is None and elsewhere:
            reason = f"{branch} is checked out in another worktree"
        if reason is None and found is not None and found.ambiguous:
            reason = (
                f"{branch} is on remotes {', '.join(sorted(found.ambiguous))}; "
                f"run {recipe.hint(facts, branch, found.ambiguous)}"
            )
        if reason is not None:
            blockers.append((facts.name, reason))

    if blockers:
        return _refuse_all(root, infos, blockers, dry_run)

    if branch is not None and all(found.mode == "none" for _f, found, _e in infos):
        raise UsageError(f"no repo has {branch}; create it with `ow switch -c {branch}`")

    plans: list[RepoPlan] = []
    for facts, found, _elsewhere in infos:
        if detach:
            if facts.default_base is None:
                plans.append(RepoPlan(repo=facts.name, refusal=NO_BASE))
            else:
                plans.append(RepoPlan(repo=facts.name, steps=recipe.detach_steps(facts)))
            continue
        assert found is not None  # no branch only with --detach, and cli refuses that
        if found.mode == "none":
            plans.append(RepoPlan(repo=facts.name, notes=(f"no {branch}",)))
            continue
        create_base = _create_base(facts, base) if found.mode == "create" else None
        if found.mode == "create" and create_base is None:
            plans.append(RepoPlan(repo=facts.name, refusal=NO_BASE))
        elif found.mode in ("track", "remote-fetch") and facts.default_base is None:
            plans.append(RepoPlan(repo=facts.name, refusal=NO_BASE))
        else:
            plans.append(
                RepoPlan(
                    repo=facts.name,
                    steps=recipe.steps(facts, branch, found, create_base=create_base),
                )
            )

    outcome = run_batch(
        Batch(root=root, repos=tuple(plans)), prompt=False, dry_run=dry_run, yes=True
    )
    code = outcome.exit_code
    if outcome.failed:
        _report_split(outcome)
    if outcome.mutated:
        code = max(code, refresh_after(session))
    return code


def _refuse_all(
    root: Path,
    infos: list[tuple[RepoFacts, recipe.Lookup | None, bool]],
    blockers: list[tuple[str, str]],
    dry_run: bool,
) -> int:
    """Every repo refused, each line naming the cause; the batch never runs (§6)."""
    reasons = dict(blockers)
    first, reason = blockers[0]
    plans = []
    for facts, _found, _elsewhere in infos:
        own = reasons.get(facts.name)
        text = own if own is not None else f"not switched: {first}: {reason}"
        plans.append(RepoPlan(repo=facts.name, refusal=text))
    outcome = run_batch(
        Batch(root=root, repos=tuple(plans)), prompt=False, dry_run=dry_run, yes=True
    )
    return outcome.exit_code


def _report_split(outcome: Outcome) -> None:
    """Which repos switched and which did not, when a failure split the workspace (§6)."""
    if outcome.ok:
        term.out(f"switched: {', '.join(outcome.ok)}")
    term.out(f"not switched: {', '.join(outcome.failed)}")


def _create_base(facts: RepoFacts, base: str | None) -> str | None:
    """The base a `-c` creation starts from: `--base` parsed per repo, else the default (§5)."""
    if base is not None:
        remotes = set(facts.remotes) or {"origin"}
        return bases.parse_base(base, lambda segment: segment in remotes)
    return facts.default_base


def _cached(bare: Path) -> frozenset[str]:
    """The bare's branch names, two ref components stripped: `<b>`, `<r>/<b>`."""
    output = git.read(
        [
            "git", "-C", str(bare), "for-each-ref",
            "--format=%(refname:lstrip=2)", "refs/heads", "refs/remotes",
        ]
    )
    return frozenset(output.split("\n")) if output else frozenset()


def _on_remotes(bare: Path, remotes: tuple[str, ...], branch: str) -> dict[str, tuple[str, ...]]:
    """Each remote's refs matching `branch` (`ls-remote --heads`), empty when it publishes none."""
    found: dict[str, tuple[str, ...]] = {}
    for remote in remotes:
        result = git.run(["git", "-C", str(bare), "ls-remote", "--heads", remote, branch])
        lines = (
            tuple(line for line in result.stdout.splitlines() if line.strip())
            if result.returncode == 0
            else ()
        )
        found[remote] = lines
    return found


def _checked_out_elsewhere(bare: Path, worktree: Path, branch: str) -> bool:
    """Whether another worktree of this bare has `branch` checked out (§6)."""
    listing = git.read(["git", "-C", str(bare), "worktree", "list", "--porcelain"])
    current: Path | None = None
    for line in listing.split("\n"):
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):])
        elif current is not None and line == f"branch refs/heads/{branch}":
            if current.resolve() != worktree.resolve():
                return True
    return False
