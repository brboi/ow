"""Base branch parsing and inference."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Optional

from ow.errors import UsageError


class BaseError(UsageError):
    """An error in base branch parsing or validation."""

    exit_code = 2


def parse_base(ref: str, remotes: Callable[[str], bool] | None = None) -> str:
    """Parse and normalize a base branch reference.
    
    Takes a remote branch: `origin/18.0`, `dev/master-phoenix`, or the shorthand `18.0`.
    Returns the fully qualified form, prefixing `origin/` to shorthands whose first segment
    is not a known remote.
    
    Raises BaseError with a descriptive message if the ref is invalid.
    
    Args:
        ref: The reference string to parse
        remotes: Optional callable that returns True if a string is a known remote name
    """
    if remotes is None:
        remotes = lambda r: r == "origin"
    
    # Strip whitespace
    ref = ref.strip()
    
    # Check for empty
    if not ref:
        raise BaseError("base cannot be empty")
    
    # Check for forbidden characters and patterns
    if ".." in ref:
        raise BaseError(f"base cannot contain '..': {ref}")
    
    if "^" in ref:
        raise BaseError(f"base cannot contain '^': {ref}")
    
    if "~" in ref:
        raise BaseError(f"base cannot contain '~': {ref}")
    
    if ":" in ref:
        raise BaseError(f"base cannot contain ':': {ref}")
    
    if "*" in ref or "?" in ref or "[" in ref or "\\" in ref:
        raise BaseError(f"base contains invalid characters: {ref}")
    
    # Check for leading forbidden characters
    if ref.startswith("-") or ref.startswith("@") or ref.startswith(" "):
        raise BaseError(f"base cannot start with '-', '@', or space: {ref}")
    
    # Check for trailing forbidden characters
    if ref.endswith("/") or ref.endswith(".lock"):
        raise BaseError(f"base cannot end with '/' or '.lock': {ref}")
    
    # Check for spaces anywhere
    if " " in ref:
        raise BaseError(f"base cannot contain spaces: {ref}")
    
    # Handle refs/ prefix
    if ref.startswith("refs/"):
        if ref.startswith("refs/remotes/"):
            # Normalize refs/remotes/<remote>/<branch> to <remote>/<branch>
            # Extract the part after refs/remotes/
            rest = ref[len("refs/remotes/"):]
            if not rest:
                raise BaseError(f"base cannot have empty branch: {ref}")
            return rest
        elif ref.startswith("refs/tags/"):
            raise BaseError(f"base cannot be a tag: {ref}")
        else:
            raise BaseError(f"base cannot use refs/ prefix (except refs/remotes/<r>/<b>): {ref}")
    
    # Check for 7-40 character hex SHA
    if re.match(r"^[0-9a-f]{7,40}$", ref):
        raise BaseError(f"base cannot be a SHA: {ref}")
    
    # Check for <remote>/ with empty branch
    if "/" in ref:
        parts = ref.split("/", 1)
        if len(parts) == 2 and not parts[1]:
            raise BaseError(f"base cannot have empty branch: {ref}")
    
    # Determine if first segment is a known remote
    if "/" in ref:
        first_segment = ref.split("/")[0]
        if remotes(first_segment):
            # Already fully qualified with a known remote
            return ref
        else:
            # Unknown remote, prefix with origin/
            return f"origin/{ref}"
    else:
        # No /, it's a shorthand branch name
        # Prefix with origin/
        return f"origin/{ref}"


def is_upstream_base(upstream: str | None, base: str | None) -> bool:
    """Check if an upstream equals the base.
    
    Returns True if both are not None and equal.
    """
    return upstream is not None and base is not None and upstream == base


def infer_default_base(
    others: Mapping[str, str],
    origin_has: Callable[[str], bool],
    origin_head: str | None,
) -> tuple[str, str] | None:
    """Infer a default base for a new repo.
    
    Rules:
    1. If all other repos share the same default base AND that branch exists in origin,
       use it, with source "shared by repo1, repo2, ...".
    2. Otherwise, if origin_head is set, use it with source "origin/HEAD".
    3. Otherwise return None.
    
    Args:
        others: Maps other repo names to their fully-qualified default bases
        origin_has: Callable that returns True if origin has a given branch
        origin_head: The value of origin/HEAD, if set (e.g., "origin/18.0" or "18.0")
    
    Returns:
        (base, source) tuple or None
    """
    # Rule 1: Check if all other repos agree on a base
    if others:
        bases = list(set(others.values()))
        if len(bases) == 1:
            # All repos have the same base
            common_base = bases[0]
            # Extract the branch name from the fully qualified base
            # e.g., "origin/18.0" -> "18.0"
            if "/" in common_base:
                branch = common_base.split("/", 1)[1]
            else:
                branch = common_base
            
            # Check if origin has this branch
            if origin_has(branch):
                # Repos are sorted as given, so we need to sort the names
                repo_names = sorted(others.keys())
                source = f"shared by {', '.join(repo_names)}"
                return (common_base, source)
    
    # Rule 2: Use origin/HEAD if available
    if origin_head:
        # origin_head might be "origin/18.0" or "18.0", normalize to fully qualified
        if origin_head.startswith("origin/"):
            return (origin_head, "origin/HEAD")
        else:
            return (f"origin/{origin_head}", "origin/HEAD")
    
    # Rule 3: No base found
    return None
