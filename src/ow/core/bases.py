"""Base branch parsing and inference."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence

from ow.core.git import remotes_of
from ow.core.git import run as git_run
from ow.errors import UsageError

_SHA = re.compile(r"[0-9a-f]{7,40}")
GENERIC_BASE_BRANCHES = frozenset({"main", "master"})


class BaseError(UsageError):
    """An error in base branch parsing or validation."""

    exit_code = 2


def split_base(ref: str, remotes: Callable[[str], bool]) -> tuple[str, str]:
    """Split `ref` into `(remote, branch)` at its longest known remote prefix.

    A ref with no known remote prefix is a branch on `origin`.
    """
    parts = ref.split("/")
    for n in range(len(parts) - 1, 0, -1):
        remote = "/".join(parts[:n])
        if remotes(remote):
            return remote, "/".join(parts[n:])
    return "origin", ref


def split_range(text: str) -> tuple[str, str | None]:
    """Split `<base>..<branch>` into `(base, branch)`; `(text, None)` when there is no `..`.

    A git branch name never contains `..`, so the split is unambiguous. Both sides must be
    non-empty.
    """
    base, sep, branch = text.partition("..")
    if not sep:
        return text, None
    base, branch = base.strip(), branch.strip()
    if not base or not branch:
        raise BaseError(f"expected <base>..<branch>, got {text!r}")
    return base, branch


def split_remote_prefix(ref: str, remotes: Sequence[str]) -> tuple[str | None, str]:
    """Split `<remote>/<branch>` at the longest of `remotes` that prefixes `ref`.

    A ref with no such prefix is a plain branch name: `(None, ref)`.
    """
    best = ""
    for remote in remotes:
        if ref.startswith(remote + "/") and len(remote) > len(best):
            best = remote
    if best:
        return best, ref[len(best) + 1 :]
    return None, ref


def parse_base(ref: str, remotes: Callable[[str], bool] | None = None) -> str:
    """Normalize a base ref to `<remote>/<branch>`, matching the longest known remote.

    Raises BaseError if the ref is not a valid remote-tracking branch.
    """
    if remotes is None:

        def remotes(r: str) -> bool:
            return r == "origin"

    ref = ref.strip()
    if not ref:
        raise BaseError("base cannot be empty")

    explicit_remotes = False
    if ref.startswith("refs/remotes/"):
        explicit_remotes = True
        ref = ref[len("refs/remotes/"):]
        if "/" not in ref:
            raise BaseError(f"base must name a branch after refs/remotes/: {ref}")
    elif ref.startswith("refs/"):
        raise BaseError(f"base cannot use refs/ prefix: {ref}")

    if _SHA.fullmatch(ref):
        raise BaseError(f"base cannot be a SHA: {ref}")

    remote, branch = split_base(ref, remotes)
    if branch == ref and explicit_remotes:
        raise BaseError(f"unknown remote in base: refs/remotes/{ref}")
    qualified = f"{remote}/{branch}"

    if (
        branch.startswith("-")
        or "@{" in branch
        or git_run(["git", "check-ref-format", f"refs/heads/{branch}"]).returncode != 0
    ):
        raise BaseError(f"invalid branch name: {branch}")

    return qualified


def parse_base_for(value: str, name: str) -> str:
    """`parse_base` a value, with `name`'s bare remotes as the known set."""
    remotes = set(remotes_of(name)) or {"origin"}
    return parse_base(value, lambda segment: segment in remotes)


def is_base_branch_name(branch: str, base: str | None = None) -> bool:
    """Whether `branch` is a generic base name (`main`/`master`) or matches `base`."""
    if branch in GENERIC_BASE_BRANCHES:
        return True
    if base:
        base_branch = base.split("/", 1)[-1]
        if branch == base_branch:
            return True
    return False


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
