"""The pushed-branch rule, branch deletion steps, and workspace removal planning."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ow.core import git
from ow.core.facts import RepoFacts
from ow.core.plan import GitStep, OwStep, RepoPlan, Step
from ow.core.workspace import RepoLocation, stray_checkouts
from ow.errors import Refused
from ow.recipes import preflight

DIRTY_HINT = "commit, stash or discard, then re-run"


def orphan_commits(bare: Path, head: str) -> int:
    """Commits reachable from `head` and from no branch nor remote-tracking ref."""
    output = git.read(
        ["git", "-C", str(bare), "rev-list", "--count", head, "--not", "--branches", "--remotes"]
    )
    return int(output)


def branch_is_pushed(bare: Path, branch: str) -> bool:
    """Whether some remote-tracking ref or fetched PR ref contains `branch`'s tip.

    A branch that cannot be resolved answers "not pushed": the safe direction.
    """
    result = git.run(
        [
            "git", "-C", str(bare), "for-each-ref", "--count=1",
            "--contains", f"refs/heads/{branch}", "refs/remotes", "refs/ow/pr",
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
    ow_pr_res = git.run(["git", "-C", str(bare), "config", "--get", f"branch.{branch}.ow-pr"])
    ow_pr = ow_pr_res.stdout.strip() if ow_pr_res.returncode == 0 else ""

    def delete() -> None:
        if _tip(bare, branch) != tip:
            raise Refused(f"branch {branch} moved since planning; re-run to delete it")
        argv = ("git", "-C", str(bare), "branch", "-D", branch)
        result = git.run(argv)
        if result.returncode != 0:
            raise git.GitFailed(argv, result.returncode, result.stderr, result.stdout)
        # Clean up any PR fetch ref associated with this branch.
        if ow_pr and "#" in ow_pr:
            num = ow_pr.rpartition("#")[2]
            if num.isdigit():
                git.run(("git", "-C", str(bare), "update-ref", "-d", f"refs/ow/pr/{num}"))
        git.run(("git", "-C", str(bare), "update-ref", "-d", f"refs/ow/pr/{branch}"))

    return (OwStep(shown=delete_line, run=delete),), ()


def workspace_refusals(
    facts: Sequence[RepoFacts], root: Path, locations: Sequence[RepoLocation]
) -> tuple[tuple[str, str], ...]:
    """Why removing a workspace is refused: dirty files, unsafe HEADs, unmanaged checkouts."""
    lines: list[tuple[str, str]] = []
    for repo_facts in facts:
        reason = preflight.refusal(repo_facts, "rm", root=root)
        if reason is not None:
            lines.append((repo_facts.name, reason))
            continue
        for path in repo_facts.modified:
            lines.append((repo_facts.name, f"modified {path}; {DIRTY_HINT}"))
        for path in repo_facts.untracked:
            lines.append((repo_facts.name, f"untracked {path}; {DIRTY_HINT}"))
        if repo_facts.branch is None and repo_facts.head is not None:
            orphans = orphan_commits(repo_facts.bare, repo_facts.head)
            if orphans:
                lines.append(
                    (
                        repo_facts.name,
                        f"{orphans} commit(s) on no branch; keep them with "
                        f"`git -C {git.display_path(repo_facts.path)} switch -c <b>`, or discard them,"
                        " then re-run",
                    )
                )
    for stray in stray_checkouts(root, locations):
        lines.append((stray.name, "a checkout ow does not manage; move it away or delete it, then re-run"))
    return tuple(lines)


def repo_removal_plan(repo_facts: RepoFacts) -> RepoPlan:
    """One repo's removal plan: worktree goes, and its branch goes only when pushed."""
    steps: list[Step] = [
        GitStep(("git", "-C", str(repo_facts.bare), "worktree", "remove", str(repo_facts.path)))
    ]
    notes: tuple[str, ...] = ()
    if repo_facts.branch is not None:
        branch_steps, notes = branch_removal(repo_facts.bare, repo_facts.branch)
        steps.extend(branch_steps)
    return RepoPlan(repo=repo_facts.name, steps=tuple(steps), notes=notes)


def outside_entries(root: Path, repos: Sequence[Path]) -> str:
    """The top-level entries `rm -rf` takes besides the repo worktrees, with file counts."""
    entries: list[str] = []
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        if any(entry.is_relative_to(repo) for repo in repos):
            continue
        if not entry.is_dir():
            entries.append(entry.name)
            continue
        count = sum(1 for child in entry.rglob("*") if child.is_file())
        plural = "s" if count != 1 else ""
        entries.append(f"{entry.name}/ ({count} file{plural})" if count else f"{entry.name}/")
    return ", ".join(entries)


def _tip(bare: Path, branch: str) -> str:
    """The SHA `refs/heads/<branch>` points at in `bare`."""
    return git.read(["git", "-C", str(bare), "rev-parse", f"refs/heads/{branch}"])
