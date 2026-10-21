"""`ow reset` — fetch, then put each repo back on the ref it follows (§7).

The fetch runs first and visibly, unless `--no-fetch`; a repo whose fetch failed is
skipped with `# <repo>: fetch failed; working on cached refs` and the others go on
(the run exits 1). Each repo's plan is built after the fetch, from fresh refs, and
prints before the prompt:

    # community: drops 1 commit(s) on no remote
    # community: modified: odoo-bin          (only under --hard, one line per path)
    git -C community reset --hard origin/18.0

The reset is mixed unless `--hard` is given; a repo already at the followed ref is a
no-op, except under `--hard` with a dirty tree, where the step still runs to restore
the files. The prompt is §13's: `-n` prints the plan and stops, `-y` skips the
question, and no terminal without `-y` is a usage error (exit 2). A mutating batch is
followed by the one refresh of §7, and the exit code is the max of the batch's, the
refusals' and the refresh's.
"""

from __future__ import annotations

from typing import Sequence

from ow.commands._shared import refresh_after
from ow.core.facts import RepoFacts, gather_all
from ow.core.plan import Batch, RepoPlan, run_batch, run_fetches
from ow.core.session import Session
from ow.errors import UsageError
from ow.recipes.fetch import fetch_steps
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
    if session.workspace is None:
        raise UsageError("not inside an ow workspace; pass -w <name|path> or run `ow init`")

    locations = tuple(
        location for location in session.locations if not only or location.name in only
    )
    if not locations:
        return 0

    root = session.workspace.root
    before = gather_all(locations)

    failures: dict[str, object] = {}
    if no_fetch:
        facts = before
    else:
        steps_by_repo = {
            repo_facts.name: steps
            for repo_facts in before
            if (steps := fetch_steps(repo_facts))
        }
        failures = run_fetches(steps_by_repo, root)
        facts = gather_all(locations)

    plans = tuple(_plan(repo_facts, failures, hard=hard) for repo_facts in facts)
    outcome = run_batch(
        Batch(root=root, repos=plans), prompt=True, dry_run=dry_run, yes=yes
    )
    refreshed = refresh_after(session) if outcome.mutated else 0
    return max(outcome.exit_code, refreshed)


def _plan(facts: RepoFacts, failures: dict[str, object], *, hard: bool) -> RepoPlan:
    """One repo's plan: a refusal, or the reset recipe's step and notes."""
    if failures.get(facts.name) is not None:
        return RepoPlan(facts.name, refusal="fetch failed; working on cached refs")

    refused = refusal(facts, "reset")
    if refused is not None:
        return RepoPlan(facts.name, refusal=refused)

    return plan_reset(facts, gather_reset(facts), hard=hard)