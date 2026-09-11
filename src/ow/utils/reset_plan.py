"""The reset decision table, as a pure function.

`ow reset` puts every repo of a workspace back on the ref its config
names, `git reset` style: the plain form moves the branch and leaves the
working tree exactly as it is, so nothing on disk is lost; `--hard`
discards the working tree too. Untracked files are never touched, for the
same reason `git reset --hard` does not touch them.

Kept free of subprocess and of ow.utils.git for the same reason as
rebase_plan: the decisions are exhaustively testable without a repository.
"""

from dataclasses import dataclass

from ow.utils.rebase_plan import GitStep


@dataclass(frozen=True)
class ResetFacts:
    """Everything observed about one repo, before any decision is made."""

    alias: str
    target: str  # the base ref the config names, resolved
    head: str | None = None  # HEAD's sha, None when it will not resolve
    target_sha: str | None = None
    drift: str | None = None  # why the worktree disagrees with the config
    busy: tuple[str, str, str] | None = None  # (operation, continue, abort)
    dirty_files: tuple[str, ...] = ()
    drop_commits: int = 0  # commits in target..HEAD, gone after the reset
    unbacked: int = 0  # of those, the ones no remote carries


@dataclass(frozen=True)
class ResetPlan:
    alias: str
    target: str
    steps: tuple[GitStep, ...] = ()
    skip_reason: str | None = None
    resume: tuple[str, str] | None = None
    drop_commits: int = 0
    unbacked: int = 0
    discarded: int = 0  # working-tree files the step throws away
    moves: bool = False  # HEAD is not already on the target

    @property
    def is_skipped(self) -> bool:
        return self.skip_reason is not None

    @property
    def is_noop(self) -> bool:
        return not self.steps and not self.is_skipped

    @property
    def loses_work(self) -> bool:
        """Does running this destroy anything a later command could not redo?"""
        return bool(self.unbacked or self.discarded)


def plan_reset(f: ResetFacts, *, hard: bool = False) -> ResetPlan:
    """Turn observed facts into the exact git steps to run."""
    carried = dict(alias=f.alias, target=f.target)

    if f.busy is not None:
        operation, cont, abort = f.busy
        return ResetPlan(skip_reason=f"{operation} in progress", resume=(cont, abort), **carried)

    if f.drift is not None:
        # Resetting a branch the config does not name would throw away work
        # ow was never told about. Realigning is `ow apply`'s job, and it
        # knows how to do it without losing anything.
        return ResetPlan(skip_reason=f"{f.drift} — run ow apply", **carried)

    if f.head is None:
        return ResetPlan(skip_reason="could not resolve HEAD", **carried)

    if f.target_sha is None:
        return ResetPlan(skip_reason=f"could not resolve {f.target}", **carried)

    moves = f.head != f.target_sha
    discarded = len(f.dirty_files) if hard else 0

    if not moves and not discarded:
        return ResetPlan(**carried)

    args = ("reset", "--hard", f.target) if hard else ("reset", f.target)
    return ResetPlan(
        steps=(GitStep(args, f.target),),
        drop_commits=f.drop_commits,
        unbacked=f.unbacked,
        discarded=discarded,
        moves=moves,
        **carried,
    )
