"""The pull decision table, as a pure function.

`ow pull` is the half of updating that never moves a repo off its base: it
fast-forwards when it can, and replays the branch on its own remote copy
when someone else pushed to it — what `git pull --rebase` does. Replaying
onto the *base* branch is a different, bigger move, and stays `ow rebase`'s.

Kept free of subprocess and of ow.utils.git for the same reason as
rebase_plan: the decisions are exhaustively testable without a repository.
"""

from dataclasses import dataclass

from ow.utils.rebase_plan import GitStep, _dirty_summary


@dataclass(frozen=True)
class PullFacts:
    """Everything observed about one repo, before any decision is made."""

    alias: str
    target: str
    head: str | None = None  # HEAD's sha, None when it will not resolve
    target_sha: str | None = None
    is_detached: bool = False  # observed from the worktree, not the config
    detached_drift: bool = False  # observation disagrees with the config
    busy: tuple[str, str, str] | None = None  # (operation, continue, abort)
    ff_possible: bool = False  # HEAD is an ancestor of target
    target_merged: bool = False  # target is already an ancestor of HEAD
    target_is_upstream: bool = False  # the target is this branch's own remote copy
    dirty_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class PullPlan:
    alias: str
    target: str
    steps: tuple[GitStep, ...] = ()
    skip_reason: str | None = None
    resume: tuple[str, str] | None = None

    @property
    def is_skipped(self) -> bool:
        return self.skip_reason is not None

    @property
    def is_noop(self) -> bool:
        return not self.steps and not self.is_skipped

    @property
    def detaches(self) -> bool:
        return bool(self.steps) and self.steps[0].args[0] == "switch"

    @property
    def rebases(self) -> bool:
        return bool(self.steps) and self.steps[0].args[0] == "rebase"


def plan_pull(f: PullFacts) -> PullPlan:
    """Turn observed facts into the exact git steps to run."""
    carried = dict(alias=f.alias, target=f.target)

    if f.busy is not None:
        operation, cont, abort = f.busy
        return PullPlan(skip_reason=f"{operation} in progress", resume=(cont, abort), **carried)

    if f.detached_drift:
        return PullPlan(
            skip_reason="worktree state does not match the config — run ow apply", **carried
        )

    if f.head is None or f.target_sha is None:
        return PullPlan(skip_reason=f"could not resolve {f.target}", **carried)

    if f.head == f.target_sha:
        return PullPlan(**carried)

    if not f.ff_possible:
        if f.target_merged:
            # Strictly ahead: the target holds nothing HEAD does not already
            # have. There is nothing to pull — a branch carrying unpushed
            # work is the normal state, not a divergence.
            return PullPlan(**carried)
        if not f.target_is_upstream:
            # The target is the base branch, and HEAD carries work of its
            # own. Replaying it there is a real rebase, with force-push
            # detection and a replay floor to get right — ow rebase owns it.
            return PullPlan(skip_reason=f"diverged from {f.target} — run ow rebase", **carried)
        if f.dirty_files:
            # git rebase refuses outright; saying so beats its wall of text.
            return PullPlan(skip_reason=_dirty_summary(f.dirty_files), **carried)
        # Same branch, two copies: ours and the one on the remote. Replaying
        # ours on top is what `git pull --rebase` does, and it neither
        # invents a merge commit nor moves the branch off its base.
        return PullPlan(steps=(GitStep(("rebase", f.target), f.target),), **carried)

    if f.is_detached:
        # Nothing is attached to the old commit, so there is no branch to
        # fast-forward: follow the ref instead.
        return PullPlan(steps=(GitStep(("switch", "--detach", f.target), f.target),), **carried)

    # --ff-only, not a plain merge: git already refuses when the move would
    # clobber a local modification, and it can never write a merge commit.
    return PullPlan(steps=(GitStep(("merge", "--ff-only", f.target), f.target),), **carried)
