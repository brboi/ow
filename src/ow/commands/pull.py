"""`ow pull` — fetch, then fast-forward each repo onto the ref it follows (§7).

The fetch runs first and visibly, unless `--no-fetch`; a repo whose fetch failed is
skipped with `# <repo>: fetch failed; working on cached refs` and the others go on
(the run exits 1). The plan is built after the fetch, from fresh refs: a
`merge --ff-only <followed ref>` line only when that ref is strictly ahead of HEAD, a
`diverged from <ref> — run ow rebase` note when both sides have commits, and no line
at all when HEAD already contains the ref. Nothing is rewritten, so there is no prompt.
`-n` prints the same lines (the fetch still runs) and executes nothing.

A mutating batch is followed by the one refresh of §7, and the command's exit code is
the max of the batch's and the refresh's.
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
from ow.recipes.pull import gather_pull, plan_pull


def run(
    session: Session,
    *,
    only: Sequence[str],
    no_fetch: bool,
    dry_run: bool,
) -> int:
    """Fetch, plan, run and refresh; the max of the batch's and the refresh's code."""
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

    plans = tuple(_plan(repo_facts, failures) for repo_facts in facts)
    outcome = run_batch(
        Batch(root=root, repos=plans), prompt=False, dry_run=dry_run, yes=True
    )
    refreshed = refresh_after(session) if outcome.mutated else 0
    return max(outcome.exit_code, refreshed)


def _plan(facts: RepoFacts, failures: dict[str, object]) -> RepoPlan:
    """One repo's plan: a refusal, or the pull recipe's step."""
    if failures.get(facts.name) is not None:
        return RepoPlan(facts.name, refusal="fetch failed; working on cached refs")

    refused = refusal(facts, "pull")
    if refused is not None:
        return RepoPlan(facts.name, refusal=refused)

    return plan_pull(facts, gather_pull(facts))