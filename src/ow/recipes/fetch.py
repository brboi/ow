"""Fetch recipe — narrow per-remote fetch steps."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ow.core import git
from ow.core.facts import RepoFacts, gather_all
from ow.core.plan import GitStep, run_fetches
from ow.errors import UsageError

if TYPE_CHECKING:
    from ow.core.session import Session

FETCH_FAILED = "fetch failed; pass --no-fetch to work on cached refs"


def fetch_then_gather(
    session: Session,
    only: Sequence[str],
    *,
    no_fetch: bool,
    extra: Sequence[str] = (),
    hint: str | None = "pass --no-fetch to work on cached refs",
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
        failures = run_fetches(steps_by_repo, root, hint=hint)
        facts = gather_all(locations)

    return root, facts, failures


def fetch_steps(facts: RepoFacts, extra: Sequence[str] = ()) -> tuple[GitStep, ...]:
    """Compute the fetch steps for one repository.

    The base is fetched in its own step first (extras on its remote ride
    along); the upstream, when different, gets a separate step so a deleted
    upstream branch cannot block the base fetch. Every argv uses an explicit
    refspec so it works on bares with no remote.<r>.fetch config and never
    resolves a tag.
    """
    if facts.problem:
        return ()

    base_remote, base_branch = _split(facts.base, facts.remotes) if facts.base else (None, None)

    upstream = facts.upstream if facts.upstream and facts.upstream != facts.base else None
    up_remote, up_branch = _split(upstream, facts.remotes) if upstream else (None, None)

    extras: dict[str, set[str]] = {}
    for ref in extra:
        remote, branch = _split(ref, facts.remotes)
        if remote:
            extras.setdefault(remote, set()).add(branch)

    if base_remote is None and up_remote is None and not extras:
        return ()

    steps: list[GitStep] = []

    if base_remote is not None:
        branches = {base_branch} | extras.pop(base_remote, set())
        steps.append(_step(facts.bare, base_remote, branches))

    if up_remote is not None:
        # The comment marks this as the upstream step: run_fetches turns a
        # "couldn't find remote ref" failure here into a note, not an error.
        steps.append(
            GitStep(
                _argv(facts.bare, up_remote, {up_branch}),
                comment=f"upstream {up_remote}/{up_branch}",
                mutating=False,
            )
        )

    for remote in facts.remotes:
        if remote in extras:
            steps.append(_step(facts.bare, remote, extras.pop(remote)))

    return tuple(steps)


def _argv(bare, remote: str, branches: set[str]) -> tuple[str, ...]:
    argv = ["git", "-C", str(bare), "fetch", remote]
    for branch in sorted(branches):
        argv.append(f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}")
    return tuple(argv)


def _step(bare, remote: str, branches: set[str]) -> GitStep:
    return GitStep(_argv(bare, remote, branches), mutating=False)


def _split(ref: str, remotes: tuple[str, ...]) -> tuple[str | None, str | None]:
    """Split <remote>/<branch> by the longest matching remote name."""
    if "/" not in ref:
        return (None, None)

    longest = ""
    for remote in remotes:
        if ref.startswith(remote + "/") and len(remote) > len(longest):
            longest = remote

    if not longest:
        return (None, None)
    return (longest, ref[len(longest) + 1 :])
