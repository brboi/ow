"""The `ow switch` lookup: where a branch lives, and what to do about it."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ow.core import git
from ow.core.facts import RepoFacts
from ow.core.plan import GitStep


@dataclass(frozen=True)
class Lookup:
    """What one repo can do with the branch.

    `mode` is `local` (switch to it), `track` (create it from the one remote
    holding it, cached), `remote-fetch` (same, after a fetch), `create` (`-c`
    when nothing is found), `ambiguous` (two remotes publish it) or `none`.
    `remote` is the remote a `track`/`remote-fetch` came from; `ambiguous`
    holds the remotes ow refuses to guess between.
    """

    mode: Literal["local", "track", "remote-fetch", "create", "ambiguous", "none"]
    remote: str | None = None
    ambiguous: tuple[str, ...] = ()


def lookup(
    facts: RepoFacts,
    branch: str,
    local: frozenset[str],
    cached: frozenset[str],
    on_remotes: Collection[str],
    *,
    create: bool = False,
) -> Lookup:
    """The decision table for one repo.

    `local` holds `refs/heads/<b>` names; `cached` holds remote-tracking names as
    `<r>/<b>`; `on_remotes` names the remotes ls-remote found the branch on, and
    is only queried for repos with no local or cached branch. A cached holder
    wins over a live one; two holders of either kind are ambiguous.
    """
    if branch in local:
        return Lookup("local")
    holders = tuple(remote for remote in facts.remotes if f"{remote}/{branch}" in cached)
    if len(holders) > 1:
        return Lookup("ambiguous", None, holders)
    if holders:
        return Lookup("track", holders[0])
    published = tuple(remote for remote in facts.remotes if remote in on_remotes)
    if len(published) > 1:
        return Lookup("ambiguous", None, published)
    if published:
        return Lookup("remote-fetch", published[0])
    return Lookup("create" if create else "none")


def steps(
    facts: RepoFacts,
    branch: str,
    result: Lookup,
    *,
    create_base: str | None = None,
    repair_base: str | None = None,
) -> tuple[GitStep, ...]:
    """The git steps for one repo; the caller refused what cannot run.

    `create_base` is the base a `create` starts from. `repair_base`, when given,
    is the ow-base written onto a local branch that lacks one.
    """
    path = str(facts.path)
    if result.mode == "local":
        planned: list[GitStep] = [GitStep(("git", "-C", path, "switch", branch))]
        if repair_base is not None:
            planned.append(_config_step(path, branch, repair_base))
        return tuple(planned)
    if result.mode in ("track", "remote-fetch"):
        assert result.remote is not None
        planned = []
        if result.mode == "remote-fetch":
            planned.append(
                GitStep(
                    (
                        "git", "-C", path, "fetch", result.remote,
                        f"+refs/heads/{branch}:refs/remotes/{result.remote}/{branch}",
                    )
                )
            )
        planned.append(
            GitStep(
                (
                    "git", "-C", path, "switch", "-c", branch, "--no-track",
                    f"{result.remote}/{branch}",
                )
            )
        )
        planned.extend(tracking_steps(path, branch, result.remote))
        assert facts.default_base is not None
        planned.append(_config_step(path, branch, facts.default_base))
        return tuple(planned)
    assert result.mode == "create" and create_base is not None
    return (
        GitStep(("git", "-C", path, "switch", "--no-track", "-c", branch, create_base)),
        _config_step(path, branch, create_base),
    )


def detach_steps(facts: RepoFacts) -> tuple[GitStep, ...]:
    """`git switch --detach <default base>`; the caller refused a missing default base."""
    return (GitStep(("git", "-C", str(facts.path), "switch", "--detach", facts.default_base)),)


def hint(facts: RepoFacts, branch: str, remotes: tuple[str, ...], root: Path | None = None) -> str:
    """The by-hand fetch, switch, and tracking commands resolving a two-remote ambiguity."""
    remote = "origin" if "origin" in remotes else remotes[0]
    path = str(facts.path)
    shown = [
        git.show(
            (
                "git", "-C", path, "fetch", remote,
                f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}",
            ),
            root,
        ),
        git.show(
            ("git", "-C", path, "switch", "-c", branch, "--no-track", f"{remote}/{branch}"),
            root,
        ),
    ]
    shown.extend(git.show(step.argv, root) for step in tracking_steps(path, branch, remote))
    return "; ".join(shown)


def _config_step(path: str, branch: str, base: str) -> GitStep:
    """The `branch.<b>.ow-base` write every creation or repair ends with."""
    return GitStep(("git", "-C", path, "config", f"branch.{branch}.ow-base", base))


def tracking_steps(path: str, branch: str, remote: str) -> tuple[GitStep, GitStep]:
    """The `branch.<b>.remote` and `merge` writes that make `<remote>/<b>` the upstream."""
    return (
        GitStep(("git", "-C", path, "config", f"branch.{branch}.remote", remote)),
        GitStep(("git", "-C", path, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")),
    )
