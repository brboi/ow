"""Run the shared fetch, then the status table.

Prints all fetch lines first, in workspace repo order, before any of them runs.
The repos are then fetched in parallel and the failures are reported in workspace
order. The table is gathered after the fetch, so it shows the remote-tracking
refs the fetch just moved. A repo with nothing to fetch (no base and no upstream,
or a broken worktree) contributes no line. A failure shows git's stdout and
stderr and gives exit code 1, and the other repos go on.

`--only` limits both the fetches and the table to the named repos.
"""

from __future__ import annotations

from typing import Sequence

from ow.commands.status import print_table
from ow.core.facts import gather_all
from ow.core.plan import run_fetches
from ow.core.session import Session
from ow.errors import UsageError
from ow.recipes.fetch import fetch_steps


def run(session: Session, *, only: Sequence[str]) -> int:
    """Fetch the selected repos, print the table; 1 when any fetch failed."""
    if session.workspace is None:
        raise UsageError("not inside an ow workspace; pass -w <name|path> or run `ow init`")

    locations = tuple(
        location for location in session.locations if not only or location.name in only
    )
    if not locations:
        return 0

    root = session.workspace.root
    steps_by_repo = {
        facts.name: steps
        for facts in gather_all(locations)
        if (steps := fetch_steps(facts))
    }
    failures = run_fetches(steps_by_repo, root, hint=None)

    print_table(session, gather_all(locations))
    return 1 if any(failure is not None for failure in failures.values()) else 0