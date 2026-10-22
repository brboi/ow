"""`ow reset` — fetch, then put each repo back on the ref it follows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from ow.commands._shared import refresh_after
from ow.commands._sync import FETCH_FAILED, fetch_then_gather
from ow.core import git
from ow.core.facts import RepoFacts
from ow.core.plan import Batch, RepoPlan, run_batch
from ow.core.session import Session
from ow.recipes.preflight import refusal
from ow.recipes.reset import gather_reset, plan_reset


def run(
    session: Session,
    *,
    only: Sequence[str],
    hard: bool,
    no_fetch: bool,
    dry_run: bool,
    yes: bool,
) -> int:
    """Fetch, plan, prompt, run and refresh; the max of the batch's and the refresh's code."""
    root, facts, failures = fetch_then_gather(session, only, no_fetch=no_fetch)
    if not facts:
        return 0

    plans = tuple(
        _plan(repo_facts, failures, hard=hard, root=root) for repo_facts in facts
    )
    outcome = run_batch(
        Batch(root=root, repos=plans), prompt=True, dry_run=dry_run, yes=yes
    )
    refreshed = refresh_after(session) if outcome.mutated else 0
    return max(outcome.exit_code, refreshed)


def _plan(
    facts: RepoFacts,
    failures: Mapping[str, git.GitFailed | None],
    *,
    hard: bool,
    root: Path,
) -> RepoPlan:
    """One repo's plan: a refusal, or the reset recipe's step and notes."""
    if failures.get(facts.name) is not None:
        return RepoPlan(facts.name, refusal=FETCH_FAILED)

    refused = refusal(facts, "reset", root=root)
    if refused is not None:
        return RepoPlan(facts.name, refusal=refused)

    return plan_reset(facts, gather_reset(facts), hard=hard)