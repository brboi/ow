"""The ref a repo follows."""

from __future__ import annotations

from ow.core.facts import RepoFacts


def followed_ref(facts: RepoFacts) -> str | None:
    """Determine the ref a repository follows.
    
    Decision table per spec §5:
    - On a branch with an upstream: @{u} (but if upstream == base, treat as no upstream)
    - On a branch without an upstream: the base
    - On a detached HEAD: the default base
    - Missing default base: None (command will refuse that repo)
    
    Args:
        facts: RepoFacts for the repository.
    
    Returns:
        The fully-qualified ref to follow, or None if no default base when needed.
    """
    # Check if upstream equals base (treat as no upstream)
    if facts.upstream and facts.base and facts.upstream == facts.base:
        upstream = None
    else:
        upstream = facts.upstream
    
    # On a branch with an upstream (not equal to base)
    if facts.branch and upstream:
        return upstream
    
    # On a branch without an upstream, or on a detached HEAD
    if facts.base:
        return facts.base
    
    # On a detached HEAD with no base
    if facts.default_base:
        return facts.default_base
    
    # No default base when needed
    return None
