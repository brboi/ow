"""The rebase decision table, as a pure function.

Kept free of subprocess and of ow.utils.git on purpose: this is the part
where a mistake costs data, and a pure function is exhaustively testable
without a git repository.
"""

from dataclasses import dataclass

_MAX_LISTED_DIRTY = 3


@dataclass(frozen=True)
class RepoFacts:
    """Everything observed about one repo, before any decision is made."""

    alias: str
    base: str
    up: str | None = None
    is_detached: bool = False  # observed from the worktree, not the config
    detached_drift: bool = False  # observation disagrees with the config
    busy: tuple[str, str, str] | None = None  # (operation, continue, abort)
    dirty_files: tuple[str, ...] = ()
    force_pushed: bool = False
    new_patches: int = 0
    bound: str | None = None
    base_merged: bool = False
    replay_count: int = 0
    unpushed: int = 0

    @property
    def needs_upstream_step(self) -> bool:
        return (
            self.up is not None
            and self.bound is not None
            and (self.force_pushed or self.new_patches > 0)
        )


@dataclass(frozen=True)
class GitStep:
    """One git invocation, plus the ref it lands on.

    Carrying `onto` is what lets a conflict name the ref that actually
    failed instead of always naming the upstream.
    """

    args: tuple[str, ...]
    onto: str


@dataclass(frozen=True)
class RebasePlan:
    alias: str
    base: str
    steps: tuple[GitStep, ...] = ()
    skip_reason: str | None = None
    resume: tuple[str, str] | None = None
    step1_target: str | None = None
    replay_count: int = 0
    unpushed: int = 0
    force_pushed: bool = False

    @property
    def is_skipped(self) -> bool:
        return self.skip_reason is not None

    @property
    def is_noop(self) -> bool:
        return not self.steps and not self.is_skipped

    @property
    def detaches(self) -> bool:
        return bool(self.steps) and self.steps[0].args[0] == "switch"


def _dirty_summary(files: tuple[str, ...]) -> str:
    listed = ", ".join(files[:_MAX_LISTED_DIRTY])
    extra = len(files) - _MAX_LISTED_DIRTY
    suffix = f" (+{extra} more)" if extra > 0 else ""
    return f"uncommitted changes: {listed}{suffix}"


def plan_for(f: RepoFacts, *, autostash: bool = False) -> RebasePlan:
    """Turn observed facts into the exact git steps to run."""
    carried = dict(
        alias=f.alias,
        base=f.base,
        replay_count=f.replay_count,
        unpushed=f.unpushed,
        force_pushed=f.force_pushed,
    )

    if f.busy is not None:
        operation, cont, abort = f.busy
        return RebasePlan(
            skip_reason=f"{operation} in progress",
            resume=(cont, abort),
            **carried,
        )

    # Planning against the config's shape while the worktree has drifted to
    # the other one rebases something nobody will look at again: the result
    # lives on a detached HEAD the next `ow apply` throws away, or on a
    # branch the config does not name.
    if f.detached_drift:
        found = "a detached HEAD" if f.is_detached else "a branch"
        expected = "a branch" if f.is_detached else "a detached HEAD"
        return RebasePlan(
            skip_reason=(
                f"worktree is on {found}, config expects {expected}"
                " — run `ow apply` to realign"
            ),
            **carried,
        )

    # --autostash is a git rebase flag; a detached worktree gets a switch,
    # which has no equivalent, so the flag cannot apply on that path.
    autostash_applies = autostash and not f.is_detached
    if f.dirty_files and not autostash_applies:
        reason = _dirty_summary(f.dirty_files)
        if autostash and f.is_detached:
            reason += " (--autostash does not apply to a detached switch)"
        return RebasePlan(skip_reason=reason, **carried)

    if f.is_detached:
        return RebasePlan(
            steps=(GitStep(("switch", "--detach", f.base), f.base),),
            **carried,
        )

    stash: tuple[str, ...] = ("--autostash",) if autostash else ()
    steps: list[GitStep] = []
    step1_target: str | None = None

    if f.needs_upstream_step:
        step1_target = f.up
        steps.append(GitStep(("rebase", *stash, "--onto", f.up, f.bound), f.up))

    # Step 2 always follows step 1 (HEAD now sits on the upstream), and runs
    # on its own whenever HEAD does not already descend from the base.
    if steps or not f.base_merged:
        steps.append(GitStep(("rebase", *stash, f.base), f.base))

    return RebasePlan(steps=tuple(steps), step1_target=step1_target, **carried)
