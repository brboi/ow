"""`ow rebase` — fetch, then the guarded rebase recipe (§7).

Fetch first and visibly, then plan against the refs it just moved: the upstream
step reads the upstream reflog a fetch appends to, so the facts are gathered
twice — once for the fetch steps, once for the plan.
"""

from __future__ import annotations

from collections.abc import Sequence

from ow.commands._shared import refresh_after
from ow.core import git
from ow.core.facts import RepoFacts, gather_all
from ow.core.plan import Batch, GitStep, RepoPlan, run_batch, run_fetches
from ow.core.session import Session
from ow.errors import UsageError
from ow.recipes.fetch import fetch_steps
from ow.recipes.preflight import refusal
from ow.recipes.rebase import gather_rebase, plan_rebase


def run(
    session: Session,
    *,
    only: Sequence[str],
    base: str | None,
    autostash: bool,
    no_fetch: bool,
    dry_run: bool,
    yes: bool,
) -> int:
    """Fetch, plan, prompt, run, refresh; the max of the batch's and the refresh's code."""
    if session.workspace is None:
        raise UsageError("not inside an ow workspace; pass -w <name|path> or run `ow init`")

    locations = tuple(
        location for location in session.locations if not only or location.name in only
    )
    if not locations:
        return 0

    root = session.workspace.root
    before = gather_all(locations)

    failures: dict[str, git.GitFailed | None] = {}
    if no_fetch:
        facts = before
    else:
        steps_by_repo: dict[str, tuple[GitStep, ...]] = {}
        for facts_ in before:
            steps = fetch_steps(facts_)
            if steps:
                steps_by_repo[facts_.name] = steps
        failures = run_fetches(steps_by_repo, root)
        # The fetch moved the remote-tracking refs the plan is about.
        facts = gather_all(locations)

    plans = tuple(
        _plan(facts_, failures, base=base, autostash=autostash) for facts_ in facts
    )
    outcome = run_batch(
        Batch(root=root, repos=plans), prompt=True, dry_run=dry_run, yes=yes
    )
    refreshed = refresh_after(session) if outcome.mutated else 0
    return max(outcome.exit_code, refreshed)


def _plan(
    facts: RepoFacts,
    failures: dict[str, git.GitFailed | None],
    *,
    base: str | None,
    autostash: bool,
) -> RepoPlan:
    """One repo's plan: a fetch-failure or pre-flight refusal, else the recipe's steps."""
    if failures.get(facts.name) is not None:
        return RepoPlan(
            facts.name, refusal="fetch failed; pass --no-fetch to work on cached refs"
        )

    refused = refusal(facts, "rebase", autostash=autostash)
    if refused is not None:
        return RepoPlan(facts.name, refusal=refused)

    return plan_rebase(facts, gather_rebase(facts), new_base=base, autostash=autostash)