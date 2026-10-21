"""Pre-flight refusal checks."""

from __future__ import annotations

from ow.core.facts import RepoFacts

# Mutating commands that can't continue if an operation is in progress
MUTATING = frozenset(
    ("pull", "rebase", "reset", "switch", "switch --detach", "repo rm", "rm")
)


def refusal(facts: RepoFacts, command: str, *, autostash: bool = False) -> str | None:
    """Check if a command should be refused for this repository.
    
    Args:
        facts: RepoFacts for the repository.
        command: One of: status, fetch, pull, rebase, reset, switch, switch --detach, repo rm, rm.
        autostash: Whether --autostash flag is set (for rebase only).
    
    Returns:
        Refusal message, or None if the command is allowed.
    """
    # Problem with the worktree
    if facts.problem:
        return f"worktree is broken ({facts.problem}); run `ow init`"
    
    # Operation in progress on mutating commands
    if command in MUTATING and facts.operation:
        return f"{facts.operation} in progress; finish it or run `git -C {facts.path} {facts.operation} --abort`"
    
    # Modified tracked files on rebase without autostash
    if command == "rebase" and not autostash and facts.modified:
        return "modified files; commit, stash, or pass --autostash"
    
    # Missing default base when needed
    if command in ("switch --detach", "pull", "rebase", "reset"):
        if facts.branch is None and facts.base is None:
            return "no default base; run `ow init`"
    
    return None
