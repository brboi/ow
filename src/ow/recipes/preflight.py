"""Pre-flight refusal checks."""

from __future__ import annotations

from pathlib import Path

from ow.core import git
from ow.core.facts import RepoFacts

# Mutating commands that can't continue if an operation is in progress
MUTATING = frozenset(("pull", "rebase", "reset", "switch", "repo rm", "rm"))


def refusal(
    facts: RepoFacts, command: str, *, autostash: bool = False, root: Path | None = None
) -> str | None:
    """Return the refusal message for command on this repository, or None."""
    if facts.problem:
        return f"worktree is broken ({facts.problem}); run `ow init`"

    if command in MUTATING and facts.operation:
        # bisect has no --abort; `git bisect reset` returns to the original HEAD
        abort = ["bisect", "reset"] if facts.operation == "bisect" else [facts.operation, "--abort"]
        hint = git.show(["git", "-C", str(facts.path), *abort], root)
        return f"{facts.operation} in progress; finish it or run `{hint}`"

    if command == "rebase" and not autostash and facts.modified:
        return "modified files; commit, stash, or pass --autostash"

    if command in ("pull", "rebase", "reset"):
        if facts.branch is None and facts.base is None:
            return "no default base; run `ow init`"

    return None
