"""`ow rebase` — fetch, then the guarded rebase recipe."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from ow.commands._shared import refresh_after, remotes_of
from ow.commands._sync import FETCH_FAILED, fetch_then_gather
from ow.core import bases
from ow.core.facts import RepoFacts
from ow.core.plan import Batch, RepoPlan, run_batch
from ow.core.session import Session
from ow.recipes.preflight import refusal
from ow.recipes.rebase import gather_rebase, plan_is_fast_forward, plan_rebase


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
    new_base = _new_base(session, only, base)
    root, facts, failures = fetch_then_gather(
        session, only, no_fetch=no_fetch, extra=(new_base,) if new_base else ()
    )
    if not facts:
        return 0

    plans = tuple(
        _plan(facts_, failures, new_base=new_base, autostash=autostash, root=root)
        for facts_ in facts
    )
    # A pure level-up is the move `ow pull` makes without asking.
    prompt = not all(
        plan_is_fast_forward(facts_, plan) for facts_, plan in zip(facts, plans)
    )
    outcome = run_batch(
        Batch(root=root, repos=plans), prompt=prompt, dry_run=dry_run, yes=yes
    )
    refreshed = refresh_after(session) if outcome.mutated else 0
    return max(outcome.exit_code, refreshed)


def _new_base(session: Session, only: Sequence[str], base: str | None) -> str | None:
    """`--base` as a fully qualified ref, parsed against the selected repos' remotes."""
    if base is None:
        return None
    remotes = {
        remote
        for location in session.locations
        if not only or location.name in only
        for remote in remotes_of(location.name)
    } or {"origin"}
    return bases.parse_base(base, lambda segment: segment in remotes)


def _plan(
    facts: RepoFacts,
    failures: Mapping[str, object],
    *,
    new_base: str | None,
    autostash: bool,
    root: Path | None,
) -> RepoPlan:
    """One repo's plan: a fetch failure or pre-flight refusal, else the recipe's steps."""
    if failures.get(facts.name) is not None:
        return RepoPlan(facts.name, refusal=FETCH_FAILED)

    plan = plan_rebase(
        facts,
        gather_rebase(facts, new_base=new_base),
        new_base=new_base,
        autostash=autostash,
    )
    # A dirty tree only blocks a rebase with steps; an empty plan runs nothing.
    refused = refusal(facts, "rebase", autostash=autostash or not plan.steps, root=root)
    if refused is not None:
        return RepoPlan(facts.name, refusal=refused)
    return plan
