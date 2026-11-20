"""The ref a repository follows."""

from __future__ import annotations

from ow.core.bases import is_upstream_base
from ow.core.facts import RepoFacts


def followed_ref(facts: RepoFacts) -> str | None:
    """Return the upstream when it differs from the base, else the base.

    `facts.base` is already `explicit_base or default_base`, so None means refuse.
    """
    if facts.branch and facts.upstream and not is_upstream_base(
        facts.upstream, facts.base
    ):
        return facts.upstream
    return facts.base
