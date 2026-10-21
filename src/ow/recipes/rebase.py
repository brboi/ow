"""The guarded rebase recipe: the reads, then a pure plan (§7).

The upstream guard exists because a colleague's commits on a branch this
worktree shares must not be replayed through `rebase <upstream>` (that would
rewrite or drop them). It asks three questions, in this order:

1. is the upstream already contained in some value this branch had? then there
   is nothing to take in;
2. did the upstream merely fast-forward past the last value the branch took in?
   then the new commits can be cherry-picked, unless the branch already carries
   their patches;
3. otherwise (a force-push, or no usable reflog) let `--fork-point` sort it out.

The reflog SHAs make the decision stable: reflog indexes move as fetches happen,
so the plan names the commit it means instead of an `@{N}` offset.

Pre-flight refusals are the command's job (`ow.recipes.preflight`); the only
refusal here is the detached HEAD without a base, so the plan is total.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ow.core import git
from ow.core.facts import RepoFacts
from ow.core.plan import GitStep, RepoPlan

AUTOSTASH_MESSAGE = "ow rebase --autostash"


@dataclass(frozen=True)
class RebaseInputs:
    """What the rebase decision needs beyond `RepoFacts` (git reads only).

    `branch_values` and `upstream_values` are the reflogs, newest first, kept
    for the tests and the reasoning; the plan reads only the derived fields.
    """

    branch_values: tuple[str, ...] = ()
    upstream_values: tuple[str, ...] = ()
    # The upstream is an ancestor of some value the branch had: nothing to take in.
    upstream_in_branch_past: bool = False
    # The newest upstream reflog entry contained in a past branch value.
    last_taken: str | None = None
    # That entry is an ancestor of the current upstream (a fast-forward, not a rewrite).
    last_taken_fast_forward: bool = False
    # Upstream commits whose patch is not already in HEAD.
    new_upstream_commits: int = 0
    # The base is already an ancestor of HEAD, so a plain rebase would be a no-op.
    base_is_ancestor: bool = True


def gather_rebase(facts: RepoFacts) -> RebaseInputs:
    """Run the reads of §7 for one repository.

    HEAD is prepended to the branch reflog: a reflog can be missing (a bare repo
    adopted without `core.logAllRefUpdates`), and the current value is still a
    value the branch holds.
    """
    path = facts.path
    upstream = _upstream(facts)

    branch_values: tuple[str, ...] = ()
    if facts.branch is not None:
        values = _reflog(path, f"refs/heads/{facts.branch}")
        if facts.head is not None and facts.head not in values:
            values = (facts.head, *values)
        branch_values = values

    upstream_values: tuple[str, ...] = ()
    upstream_in_branch_past = False
    last_taken: str | None = None
    last_taken_fast_forward = False
    new_upstream_commits = 0

    if upstream is not None:
        upstream_values = _reflog(path, f"refs/remotes/{upstream}")
        upstream_in_branch_past = any(
            _is_ancestor(path, upstream, value) for value in branch_values
        )
        for value in upstream_values:
            if any(_is_ancestor(path, value, past) for past in branch_values):
                last_taken = value
                break
        if last_taken is not None:
            last_taken_fast_forward = _is_ancestor(path, last_taken, upstream)
        if last_taken is not None and last_taken_fast_forward:
            # The flowchart's range is `S..upstream`, but the patch-equivalence
            # question is about HEAD: `--cherry-pick` only drops a commit when it
            # has another side to compare patches with, so HEAD must be that side.
            new_upstream_commits = _count_unpatched(path, upstream)

    base_is_ancestor = True
    if facts.base is not None:
        base_is_ancestor = _is_ancestor(path, facts.base, "HEAD")

    return RebaseInputs(
        branch_values=branch_values,
        upstream_values=upstream_values,
        upstream_in_branch_past=upstream_in_branch_past,
        last_taken=last_taken,
        last_taken_fast_forward=last_taken_fast_forward,
        new_upstream_commits=new_upstream_commits,
        base_is_ancestor=base_is_ancestor,
    )


def plan_rebase(
    facts: RepoFacts,
    inputs: RebaseInputs,
    *,
    new_base: str | None,
    autostash: bool,
) -> RepoPlan:
    """Turn facts and inputs into the exact steps to run (pure).

    `--base <new>` differing from the recorded base always runs the `--onto`
    step: the ordinary branch already contains its old base, so gating it on the
    old base's ancestry would make `--base` a no-op. A re-run after success sees
    `facts.base == <new>` and falls back to the (idempotent) plain base step.

    The `branch.<b>.ow-base` write is simply the step after the rebase in the
    same plan: `run_batch` stops a repo at its first failure, so the write cannot
    run when the rebase failed (a conflict leaves the old value in place).
    `stash pop` last uses the same rule: a failed rebase keeps the stash, and
    the pop is listed among the repo's remaining commands (P15).
    """
    path = str(facts.path)

    if facts.branch is None and facts.base is None:
        return RepoPlan(facts.name, refusal="no default base; run `ow init`")

    steps: list[GitStep] = []

    # 1. The upstream step, only when the upstream is not the base itself.
    upstream = _upstream(facts)
    if facts.branch is not None and upstream is not None:
        if not inputs.upstream_in_branch_past:
            if inputs.last_taken is not None and inputs.last_taken_fast_forward:
                if inputs.new_upstream_commits > 0:
                    steps.append(
                        GitStep(
                            (
                                "git",
                                "-C",
                                path,
                                "cherry-pick",
                                "--empty=drop",
                                f"{inputs.last_taken}..{upstream}",
                            ),
                            comment=(
                                f"{inputs.last_taken[:10]} = {upstream}"
                                " as this branch last took it in"
                            ),
                        )
                    )
            else:
                steps.append(
                    GitStep(("git", "-C", path, "rebase", "--fork-point", upstream))
                )

    # 2. The base step. On a detached HEAD `facts.base` is the default base.
    changing_base = new_base is not None and new_base != facts.base
    if changing_base:
        if facts.base is None:
            # Nothing recorded to bound the replay: the native rebase picks the
            # merge-base with the new base, and is idempotent when already based.
            steps.append(GitStep(("git", "-C", path, "rebase", new_base)))
        else:
            steps.append(
                GitStep(("git", "-C", path, "rebase", "--onto", new_base, facts.base))
            )
        if facts.branch is not None:
            steps.append(
                GitStep(
                    (
                        "git",
                        "-C",
                        path,
                        "config",
                        f"branch.{facts.branch}.ow-base",
                        new_base,
                    )
                )
            )
    elif facts.base is not None and not inputs.base_is_ancestor:
        steps.append(GitStep(("git", "-C", path, "rebase", facts.base)))

    if not steps:
        return RepoPlan(facts.name)

    if autostash and facts.modified:
        # The stash does not move the branch, so it must not be the successful
        # step that triggers the post-batch refresh on a failed rebase.
        steps.insert(
            0,
            GitStep(
                ("git", "-C", path, "stash", "push", "-m", AUTOSTASH_MESSAGE),
                mutating=False,
            ),
        )
        steps.append(GitStep(("git", "-C", path, "stash", "pop"), mutating=False))

    return RepoPlan(facts.name, steps=tuple(steps))


def _upstream(facts: RepoFacts) -> str | None:
    """The upstream that gets its own step: none when it is the base (§7)."""
    if facts.upstream is None or facts.upstream == facts.base:
        return None
    return facts.upstream


def _reflog(path: Path, ref: str) -> tuple[str, ...]:
    """A ref's reflog SHAs, newest first; empty when the ref or its log is missing."""
    result = git.run(["git", "-C", str(path), "reflog", "show", "--format=%H", ref])
    if result.returncode != 0:
        return ()
    return tuple(line for line in result.stdout.splitlines() if line)


def _is_ancestor(path: Path, ancestor: str, descendant: str) -> bool:
    """`git merge-base --is-ancestor`; any failure (a missing ref) counts as no."""
    result = git.run(
        ["git", "-C", str(path), "merge-base", "--is-ancestor", ancestor, descendant]
    )
    return result.returncode == 0


def _count_unpatched(path: Path, upstream: str) -> int:
    """Upstream-side commits of `HEAD...upstream` with no patch-equivalent in HEAD."""
    result = git.run(
        [
            "git",
            "-C",
            str(path),
            "rev-list",
            "--cherry-pick",
            "--right-only",
            "--count",
            f"HEAD...{upstream}",
        ]
    )
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip() or "0")
    except ValueError:
        return 0