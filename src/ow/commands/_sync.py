"""Shared sync logic: fetch, gather facts, and handle failures."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from ow.core import git
from ow.core.facts import RepoFacts, gather_all
from ow.core.plan import run_fetches
from ow.core.session import Session
from ow.errors import UsageError
from ow.recipes.fetch import fetch_steps

FETCH_FAILED = "fetch failed; pass --no-fetch to work on cached refs"


def fetch_then_gather(
    session: Session,
    only: Sequence[str],
    *,
    no_fetch: bool,
    extra: Sequence[str] = (),
) -> tuple[Path, tuple[RepoFacts, ...], Mapping[str, git.GitFailed | None]]:
    """Gather facts, fetch unless `no_fetch`, re-gather; `(root, facts, failures)`.

    Raises UsageError outside a workspace; `facts == ()` when `--only` selects nothing.
    """
    if session.workspace is None:
        raise UsageError("not inside an ow workspace; pass -w <name|path> or run `ow init`")

    locations = tuple(
        location for location in session.locations if not only or location.name in only
    )
    if not locations:
        return session.workspace.root, (), {}

    root = session.workspace.root
    before = gather_all(locations)

    failures: dict[str, git.GitFailed | None] = {}
    if no_fetch:
        facts = before
    else:
        steps_by_repo = {
            repo_facts.name: steps
            for repo_facts in before
            if (steps := fetch_steps(repo_facts, tuple(extra)))
        }
        failures = run_fetches(steps_by_repo, root)
        facts = gather_all(locations)

    return root, facts, failures
