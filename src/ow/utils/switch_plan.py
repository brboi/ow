"""The switch decision table, as a pure function.

`ow switch` runs `git switch` across every repo of a workspace, with
git's own DWIM semantics. Unlike `ow reset` and `ow rebase`, which skip a
misbehaving repo and carry on with the rest, a repo that fails pre-flight
here aborts the whole run before any repo is touched: switching some
repos and leaving others behind would strand the workspace between two
states, which is worse than refusing outright.

Kept free of subprocess and of ow.utils.git for the same reason as
reset_plan and rebase_plan: the decisions are exhaustively testable
without a repository.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SwitchFacts:
    """Everything observed about one repo, before any decision is made."""

    alias: str
    worktree_missing: bool = False
    busy: tuple[str, str, str] | None = None  # (operation, continue, abort)
    # Only meaningful when the command's target needed resolving at all —
    # `-c NEW` with no start point never sets this to False.
    target_resolvable: bool = True


@dataclass(frozen=True)
class SwitchPlan:
    alias: str
    args: tuple[str, ...] = ()
    skip_reason: str | None = None
    resume: tuple[str, str] | None = None

    @property
    def is_skipped(self) -> bool:
        return self.skip_reason is not None


def plan_switch(f: SwitchFacts, args: tuple[str, ...]) -> SwitchPlan:
    """Turn observed facts into the exact `git switch` invocation, or the
    reason this repo cannot run it.

    `args` is the `git switch` invocation the command's own flags settled
    on — identical for every repo of the run; only whether it is safe to
    run here varies per repo.
    """
    if f.worktree_missing:
        return SwitchPlan(alias=f.alias, skip_reason="worktree not found — run `ow apply`")
    if f.busy is not None:
        operation, cont, abort = f.busy
        return SwitchPlan(alias=f.alias, skip_reason=f"{operation} in progress", resume=(cont, abort))
    if not f.target_resolvable:
        return SwitchPlan(alias=f.alias, skip_reason="target not found, even after fetching")
    return SwitchPlan(alias=f.alias, args=args)
