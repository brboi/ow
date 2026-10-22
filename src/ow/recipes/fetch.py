"""Fetch recipe — narrow per-remote fetch steps."""

from __future__ import annotations

from typing import Sequence

from ow.core.facts import RepoFacts
from ow.core.plan import GitStep


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
