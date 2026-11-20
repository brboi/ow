"""The `ow switch` lookup: where a branch lives, and what to do about it (§6)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

from ow.core import git
from ow.core.facts import RepoFacts
from ow.core.plan import GitStep


@dataclass(frozen=True)
class Lookup:
    """What one repo can do with the branch.

    `mode` is the §6 case: `local` (switch to it), `track` (create it from the
    one remote holding it, cached), `remote-fetch` (same, after a fetch),
    `create` (`-c` when nothing is found) or `none`. `remote` is the remote a
    `track`/`remote-fetch` came from; `ambiguous` holds the remotes of a branch
    ow refuses to guess between (mode `none`, never reached as a plan: the
    all-or-nothing pre-flight refuses the whole batch first).
    """

    mode: Literal["local", "track", "remote-fetch", "create", "none"]
    remote: str | None = None
    ambiguous: tuple[str, ...] = ()


def lookup(
    facts: RepoFacts,
    branch: str,
    cached: frozenset[str],
    on_remotes: Mapping[str, tuple[str, ...]],
    *,
    create: bool = False,
) -> Lookup:
    """The §6 decision table for one repo.

    `cached` holds the bare's branch names with two ref components stripped:
    `refs/heads/<b>` contributes `<b>`, `refs/remotes/<r>/<b>` contributes
    `<r>/<b>`. `on_remotes` maps each remote to the refs `ls-remote --heads
    <r> <branch>` found on it, empty when the remote does not publish the
    branch. A local branch wins over every remote; the remotes of both inputs
    count together, so a branch cached on one and published on another is
    ambiguous, not half-found. Nothing found is `none`, or `create` when `-c`
    asked for the branch (`-c` runs the same lookup, §6).
    """
    if branch in cached:
        return Lookup("local")
    holders = tuple(
        remote
        for remote in facts.remotes
        if f"{remote}/{branch}" in cached or on_remotes.get(remote)
    )
    if len(holders) > 1:
        return Lookup("none", None, holders)
    if holders:
        remote = holders[0]
        if f"{remote}/{branch}" in cached:
            return Lookup("track", remote)
        return Lookup("remote-fetch", remote)
    return Lookup("create" if create else "none")


def steps(
    facts: RepoFacts, branch: str, result: Lookup, *, create_base: str | None = None
) -> tuple[GitStep, ...]:
    """The git steps for one repo (§6); the caller refused what cannot run.

    `create_base` is the fully-qualified base a `create` starts from: `--base`
    parsed, else the repo's default base. Every creation ends with the
    `branch.<b>.ow-base` write; the config step is skipped without a base,
    which the caller turns into a refusal first.
    """
    path = str(facts.path)
    if result.mode == "local":
        return (GitStep(("git", "-C", path, "switch", branch)),)
    if result.mode in ("track", "remote-fetch"):
        planned: list[GitStep] = []
        if result.mode == "remote-fetch":
            planned.append(GitStep(("git", "-C", path, "fetch", result.remote, branch)))
        planned.append(
            GitStep(
                ("git", "-C", path, "switch", "-c", branch, "--track", f"{result.remote}/{branch}")
            )
        )
        planned.extend(_config_steps(path, branch, facts.default_base))
        return tuple(planned)
    if result.mode == "create":
        created = [
            GitStep(("git", "-C", path, "switch", "--no-track", "-c", branch, create_base))
        ]
        created.extend(_config_steps(path, branch, create_base))
        return tuple(created)
    return ()


def detach_steps(facts: RepoFacts) -> tuple[GitStep, ...]:
    """`git switch --detach <default base>` (§6); the caller refused a missing default base."""
    return (GitStep(("git", "-C", str(facts.path), "switch", "--detach", facts.default_base)),)


def hint(facts: RepoFacts, branch: str, remotes: tuple[str, ...]) -> str:
    """The by-hand command that resolves a two-remote ambiguity (§6), origin first."""
    remote = "origin" if "origin" in remotes else remotes[0]
    return git.show(
        ("git", "-C", str(facts.path), "switch", "-c", branch, "--track", f"{remote}/{branch}")
    )


def _config_steps(path: str, branch: str, base: str | None) -> tuple[GitStep, ...]:
    """The `branch.<b>.ow-base` write every creation ends with; none without a base."""
    if base is None:
        return ()
    return (GitStep(("git", "-C", path, "config", f"branch.{branch}.ow-base", base)),)
