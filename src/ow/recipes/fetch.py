"""Fetch recipe — narrow per-remote fetch steps."""

from __future__ import annotations

from ow.core.facts import RepoFacts
from ow.core.plan import GitStep


def fetch_steps(facts: RepoFacts) -> tuple[GitStep, ...]:
    """Compute fetch steps for a repository.
    
    Fetches the base branch and, when there is an upstream, the upstream's branch.
    One git fetch per remote in first-seen order, branches deduplicated per remote.
    
    Args:
        facts: RepoFacts for the repository.
    
    Returns:
        Tuple of fetch steps, or empty tuple if no base or on a problem repo.
    """
    if facts.problem:
        return ()
    
    # If no base and no upstream, no fetch needed
    if facts.base is None and facts.upstream is None:
        return ()
    
    # Collect branches to fetch per remote
    branches_by_remote: dict[str, set[str]] = {}
    
    # Add base branch
    if facts.base:
        # Split <remote>/<branch> by the longest matching name in remotes
        base_remote, base_branch = _split_remote_branch(facts.base, facts.remotes)
        if base_remote:
            if base_remote not in branches_by_remote:
                branches_by_remote[base_remote] = set()
            branches_by_remote[base_remote].add(base_branch)
    
    # Add upstream branch if different from base
    if facts.upstream and facts.upstream != facts.base:
        upstream_remote, upstream_branch = _split_remote_branch(
            facts.upstream, facts.remotes
        )
        if upstream_remote:
            if upstream_remote not in branches_by_remote:
                branches_by_remote[upstream_remote] = set()
            branches_by_remote[upstream_remote].add(upstream_branch)
    
    # Build steps in order of first-seen remote
    seen_remotes = []
    steps = []
    
    for remote in facts.remotes:
        if remote in branches_by_remote and remote not in seen_remotes:
            seen_remotes.append(remote)
            branches = sorted(branches_by_remote[remote])
            
            argv = ["git", "-C", str(facts.bare), "fetch", remote] + branches
            steps.append(GitStep(tuple(argv), mutating=False))
    
    return tuple(steps)


def _split_remote_branch(ref: str, remotes: tuple[str, ...]) -> tuple[str, str]:
    """Split a remote-tracking branch reference.
    
    Splits <remote>/<branch> by the longest matching remote name.
    
    Args:
        ref: Remote-tracking reference like "origin/18.0" or "dev/master-phoenix".
        remotes: Tuple of known remote names.
    
    Returns:
        Tuple of (remote, branch) or ("", "") if no match.
    """
    if "/" not in ref:
        return ("", "")
    
    # Find the longest matching remote
    longest_remote = ""
    for remote in remotes:
        if ref.startswith(remote + "/"):
            if len(remote) > len(longest_remote):
                longest_remote = remote
    
    if longest_remote:
        branch = ref[len(longest_remote) + 1 :]
        return (longest_remote, branch)
    
    return ("", "")
