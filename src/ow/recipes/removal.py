"""The "pushed" rule of §7: is a branch tip already on a remote?

A branch is **pushed** when its tip is equal to, or an ancestor of, some `refs/remotes/*`
tip, so deleting the label loses nothing a remote has not got. A branch just created from
its base counts as pushed: its tip *is* the base's tip, which the remote has. Anything
else is unpushed, and "not pushed" is the only safe direction to be wrong in, so an empty
remote set answers "not pushed".
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from pathlib import Path

from ow.core import git


def is_pushed(
    tip: str,
    remote_tips: Collection[str],
    contains: Callable[[str, str], bool],
) -> bool:
    """Whether `tip` is contained in some remote-tracking tip (§7).

    `contains(ancestor, descendant)` answers "is the first commit an ancestor of, or
    equal to, the second": `git merge-base --is-ancestor` in production, a table in the
    decision tests. Pure — the caller decides where the tips come from.
    """
    return any(contains(tip, remote_tip) for remote_tip in remote_tips)


def remote_tips(bare: Path) -> tuple[str, ...]:
    """Every `refs/remotes/*` tip in `bare`: the "some remote has it" of §7."""
    output = git.read(
        ["git", "-C", str(bare), "for-each-ref", "--format=%(objectname)", "refs/remotes"]
    )
    return tuple(line.strip() for line in output.split("\n") if line.strip())


def contains_in(bare: Path) -> Callable[[str, str], bool]:
    """The `contains` predicate for `bare`: `git merge-base --is-ancestor <a> <b>` (§7)."""

    def contains(ancestor: str, descendant: str) -> bool:
        argv = ("git", "-C", str(bare), "merge-base", "--is-ancestor", ancestor, descendant)
        return git.run(argv).returncode == 0

    return contains


def branch_is_pushed(bare: Path, branch: str) -> bool:
    """The §7 rule for one branch of `bare`, on the real git reads.

    An unresolvable branch answers "not pushed": claiming work is saved when the label
    cannot even be found is the dangerous direction.
    """
    result = git.run(
        ["git", "-C", str(bare), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]
    )
    tip = result.stdout.strip()
    if result.returncode != 0 or not tip:
        return False
    return is_pushed(tip, remote_tips(bare), contains_in(bare))
