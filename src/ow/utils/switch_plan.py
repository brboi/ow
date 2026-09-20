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
    # The branch the worktree is on, None when it is detached. A repo
    # already on the target has nothing to do: git would only answer
    # "Already on 'x'", and rewriting its spec would churn the config of
    # a repo that never moved.
    current_branch: str | None = None
    # `-c NEW` where NEW is already a branch here. git refuses it, so the
    # run must refuse too — before any repo moves, rather than halfway
    # through, which is the whole point of an all-or-nothing pre-flight.
    create_exists: bool = False


@dataclass(frozen=True)
class SwitchPlan:
    alias: str
    args: tuple[str, ...] = ()
    # What this repo is about to do, for the summary: "switch" onto an
    # existing local branch, "track" a remote-only one, "create" a new
    # one, "detach", or "noop" for a repo already on the target — which
    # is reported and never run.
    action: str = "switch"
    # (remote, branch) to record as the new branch's upstream once the
    # switch succeeded; only set for the DWIM form.
    upstream: tuple[str, str] | None = None
    # The ref the args really name, once ow's DWIM has qualified it: the
    # caller writes the config from what was switched to, not from what
    # was typed, and `master` resolved through the `upstream` remote is
    # not `origin/master`. Deliberately None on the `track` and plain
    # paths: those end up on a branch, and the spec is then read back from
    # git's own `branch.<name>.remote/merge` — the ref this field carries
    # would be the same one, spelled twice.
    resolved_target: str | None = None
    skip_reason: str | None = None
    # The command that would make this run possible. Kept apart from the
    # reason because a whole workspace usually fails for the same cause,
    # and advice repeated once per repo stops being read.
    hint: str | None = None
    # (continue, abort) for a repo caught mid-rebase or mid-merge.
    resume: tuple[str, str] | None = None

    @property
    def is_skipped(self) -> bool:
        return self.skip_reason is not None

    @property
    def is_noop(self) -> bool:
        return self.action == "noop"


def _qualify(f: SwitchFacts, target: str | None) -> str | None:
    """`target`, named the way git will actually find it here.

    A short name that only exists as one remote-tracking ref is replaced
    by `<remote>/<name>`; anything that already resolves locally — a
    branch, a tag, a sha, an already-qualified ref — is left as typed,
    since `dwim_remote` is None for those.
    """
    if target is None or f.dwim_remote is None:
        return target
    return f"{f.dwim_remote}/{target}"


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

    The same guess settles `--detach` and `-c NEW <start>`, where git
    does not guess at all: it either refuses the ref outright, or — with
    `--detach` — turns it into a branch creation and then rejects its own
    combination. Both get the remote-tracking ref by name instead.
    """
    if f.worktree_missing:
        return SwitchPlan(
            alias=f.alias, skip_reason="worktree not found", hint="run `ow init` in the workspace to create it",
        )
    if f.busy is not None:
        operation, cont, abort = f.busy
        return SwitchPlan(alias=f.alias, skip_reason=f"{operation} in progress", resume=(cont, abort))
    if not f.target_resolvable:
        # Naming the fix matters more here than anywhere else: the common
        # reason a branch is nowhere to be found is that it does not exist
        # yet, and the user meant to start it. git says as much when it
        # refuses a plain `git switch`, and so does ow.
        return SwitchPlan(
            alias=f.alias,
            skip_reason=f"no branch named '{target}' here or on any remote",
            hint=(f"create it with `ow switch -c {target}`" if create is None and not detach else None),
        )

    if create is not None:
        if f.create_exists:
            return SwitchPlan(
                alias=f.alias,
                skip_reason=f"branch '{create}' already exists here",
                hint=f"switch to it with `ow switch {create}`",
            )
        # A start point is a ref, and git guesses nothing for one: it
        # resolves `master` or it fails. Qualifying it here is what makes
        # `ow switch -c fix master` work off a remote-only branch.
        start = _qualify(f, target)
        args = ("switch", "-c", create) + ((start,) if start else ())
        return SwitchPlan(alias=f.alias, args=args, action="create", resolved_target=start)

    assert target is not None  # cmd_switch refuses a run with neither
    if detach:
        # `git switch --detach master` on a branch that is only remote does
        # not detach: git's own guess turns it into a branch creation and
        # then refuses itself with "'--detach' cannot be used with -b". The
        # remote-tracking ref is what the user meant, so name it outright.
        ref = _qualify(f, target)
        assert ref is not None
        return SwitchPlan(alias=f.alias, args=("switch", "--detach", ref), action="detach", resolved_target=ref)
    if target == f.current_branch:
        # Nothing to run: git would answer "Already on 'x'", and a repo
        # that never moved must keep the spec it already has — an
        # upstream ow wrote once is not worth re-deriving.
        return SwitchPlan(alias=f.alias, action="noop")
    if f.dwim_remote is not None:
        return SwitchPlan(
            alias=f.alias,
            args=("switch", "-c", target, f"{f.dwim_remote}/{target}"),
            action="track",
            upstream=(f.dwim_remote, target),
        )
    return SwitchPlan(alias=f.alias, args=("switch", target))
