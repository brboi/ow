"""Base branch parsing and inference."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

from ow.core.git import run as git_run
from ow.errors import UsageError

_SHA = re.compile(r"[0-9a-f]{7,40}")


class BaseError(UsageError):
    """An error in base branch parsing or validation."""

    exit_code = 2


def parse_base(ref: str, remotes: Callable[[str], bool] | None = None) -> str:
    """Normalize a base ref to `<remote>/<branch>`, matching the longest known remote.

    Raises BaseError if the ref is not a valid remote-tracking branch.
    """
    if remotes is None:
        remotes = lambda r: r == "origin"

    ref = ref.strip()
    if not ref:
        raise BaseError("base cannot be empty")

    if ref.startswith("refs/remotes/"):
        ref = ref[len("refs/remotes/"):]
        if "/" not in ref:
            raise BaseError(f"base must name a branch after refs/remotes/: {ref}")
    elif ref.startswith("refs/"):
        raise BaseError(f"base cannot use refs/ prefix: {ref}")

    if _SHA.fullmatch(ref):
        raise BaseError(f"base cannot be a SHA: {ref}")

    parts = ref.split("/")
    prefix_len = 0
    for n in range(len(parts) - 1, 0, -1):
        if remotes("/".join(parts[:n])):
            prefix_len = n
            break

    if prefix_len:
        branch = "/".join(parts[prefix_len:])
        qualified = ref
    else:
        branch = ref
        qualified = f"origin/{ref}"

    if git_run(["git", "check-ref-format", "--branch", branch]).returncode != 0:
        raise BaseError(f"invalid branch name: {branch}")

    return qualified


def is_upstream_base(upstream: str | None, base: str | None) -> bool:
    """Whether both are set and equal."""
    return upstream is not None and base is not None and upstream == base


def infer_default_base(
    others: Mapping[str, str],
    origin_has: Callable[[str], bool],
    origin_head: str | None,
) -> tuple[str, str] | None:
    """Infer a default base from the other repos, then from origin/HEAD.

    Returns `(origin/<branch>, source)` or None when neither has an answer.
    """
    if others:
        bases = set(others.values())
        if len(bases) == 1:
            common_base = bases.pop()
            branch = common_base.split("/", 1)[-1]
            if origin_has(branch):
                names = ", ".join(sorted(others))
                return (f"origin/{branch}", f"shared by {names}")

    if origin_head:
        branch = origin_head.removeprefix("origin/")
        return (f"origin/{branch}", "origin/HEAD")

    return None
