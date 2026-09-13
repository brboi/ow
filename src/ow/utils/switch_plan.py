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
    # The remote whose refs/remotes/<remote>/<target> is the only match for
    # a target that is not already a local branch or a direct ref — the
    # DWIM ow has to perform itself (see plan_switch).
    dwim_remote: str | None = None


@dataclass(frozen=True)
class SwitchPlan:
    alias: str
    args: tuple[str, ...] = ()
    # (remote, branch) to record as the new branch's upstream once the
    # switch succeeded; only set for the DWIM form.
    upstream: tuple[str, str] | None = None
    skip_reason: str | None = None
    resume: tuple[str, str] | None = None

    @property
    def is_skipped(self) -> bool:
        return self.skip_reason is not None


def plan_switch(
    f: SwitchFacts, *, target: str | None, create: str | None, detach: bool,
) -> SwitchPlan:
    """Turn observed facts into the exact `git switch` invocation, or the
    reason this repo cannot run it.

    The invocation is per repo, not per run: the same short name can be a
    local branch in one repo and remote-only in another.

    The DWIM is ow's own rather than git's `--guess`. ow's bare repos are
    cloned `--single-branch` and every other branch is fetched by explicit
    refspec, deliberately outside the remote's fetch refspec; git refuses
    to auto-create a branch from a remote-tracking ref its refspec does
    not map, exactly as `git branch --set-upstream-to` refuses to track
    one. So the branch is created from the unique remote-tracking ref and
    the upstream is written afterwards, which is what ow does everywhere
    else it attaches a worktree.
    """
    if f.worktree_missing:
        return SwitchPlan(alias=f.alias, skip_reason="worktree not found — run `ow apply`")
    if f.busy is not None:
        operation, cont, abort = f.busy
        return SwitchPlan(alias=f.alias, skip_reason=f"{operation} in progress", resume=(cont, abort))
    if not f.target_resolvable:
        # Naming the fix matters more here than anywhere else: the common
        # reason a branch is nowhere to be found is that it does not exist
        # yet, and the user meant to start it. git says as much when it
        # refuses a plain `git switch`, and so does ow.
        reason = f"no branch named '{target}' here or on any remote"
        if create is None and not detach:
            reason += f" — create it with `ow switch -c {target}`"
        return SwitchPlan(alias=f.alias, skip_reason=reason)

    if create is not None:
        args = ("switch", "-c", create) + ((target,) if target else ())
        return SwitchPlan(alias=f.alias, args=args)

    assert target is not None  # cmd_switch refuses a run with neither
    if detach:
        return SwitchPlan(alias=f.alias, args=("switch", "--detach", target))
    if f.dwim_remote is not None:
        return SwitchPlan(
            alias=f.alias,
            args=("switch", "-c", target, f"{f.dwim_remote}/{target}"),
            upstream=(f.dwim_remote, target),
        )
    return SwitchPlan(alias=f.alias, args=("switch", target))
