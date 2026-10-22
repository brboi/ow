"""The pushed-branch rule and the steps that remove a branch."""

from __future__ import annotations

from pathlib import Path

from ow.core import git
from ow.core.plan import OwStep, Step
from ow.errors import Refused


def orphan_commits(bare: Path, head: str) -> int:
    """Commits reachable from `head` and from no branch nor remote-tracking ref."""
    output = git.read(
        ["git", "-C", str(bare), "rev-list", "--count", head, "--not", "--branches", "--remotes"]
    )
    return int(output)


def branch_is_pushed(bare: Path, branch: str) -> bool:
    """Whether some remote-tracking ref contains `branch`'s tip.

    A branch that cannot be resolved answers "not pushed": the safe direction.
    """
    result = git.run(
        [
            "git", "-C", str(bare), "for-each-ref", "--count=1",
            "--contains", f"refs/heads/{branch}", "refs/remotes",
        ]
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def branch_removal(bare: Path, branch: str) -> tuple[tuple[Step, ...], tuple[str, ...]]:
    """The steps that delete `branch`, or the note that keeps it and says how to delete it.

    The pushed branch's step re-reads the tip before deleting, so a commit made while the
    prompt was open refuses instead of vanishing with the branch.
    """
    delete_line = git.show(("git", "-C", str(bare), "branch", "-D", branch))
    if not branch_is_pushed(bare, branch):
        return (), (f"kept unpushed branch {branch}; delete it with `{delete_line}`",)

    tip = _tip(bare, branch)

    def delete() -> None:
        if _tip(bare, branch) != tip:
            raise Refused(f"branch {branch} moved since planning; re-run to delete it")
        git.run(("git", "-C", str(bare), "branch", "-D", branch))

    return (OwStep(shown=delete_line, run=delete),), ()


def _tip(bare: Path, branch: str) -> str:
    """The SHA `refs/heads/<branch>` points at in `bare`."""
    return git.read(["git", "-C", str(bare), "rev-parse", f"refs/heads/{branch}"])
