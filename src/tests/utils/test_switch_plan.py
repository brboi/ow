"""Table-driven tests of the switch decision table.

No git, no filesystem: `plan_switch` is a pure function from observed
facts plus the command's flags to either the exact `git switch`
invocation or the reason this repo cannot run it.
"""

from ow.utils.switch_plan import SwitchFacts, SwitchPlan, plan_switch


def plan(facts, *, target="feature-x", create=None, detach=False):
    return plan_switch(facts, target=target, create=create, detach=detach)


def test_an_existing_local_branch_is_switched_to_plainly():
    result = plan(SwitchFacts(alias="community"))

    assert result == SwitchPlan(alias="community", args=("switch", "feature-x"))
    assert not result.is_skipped


def test_a_remote_only_branch_is_created_and_tracked():
    """ow does git's DWIM itself: the bare repos are --single-branch, so
    git refuses to guess from a ref outside the remote's refspec."""
    result = plan(SwitchFacts(alias="community", dwim_remote="origin"))

    assert result.args == ("switch", "-c", "feature-x", "origin/feature-x")
    assert result.upstream == ("origin", "feature-x")


def test_detaching_at_a_remote_only_branch_names_the_remote_ref():
    """`git switch --detach feature-x` on a branch that exists only on a
    remote does not detach: git guesses a branch creation and then refuses
    "'--detach' cannot be used with -b/-B/--orphan". The remote-tracking
    ref is what the user meant, and naming it is the only way to say it."""
    result = plan(SwitchFacts(alias="community", dwim_remote="origin"), detach=True)

    assert result.args == ("switch", "--detach", "origin/feature-x")
    assert result.resolved_target == "origin/feature-x"
    # A detached HEAD tracks nothing; the ref is a pin, not an upstream.
    assert result.upstream is None


def test_detaching_at_a_ref_that_resolves_here_passes_it_through():
    """No DWIM, so the ref reaches git as typed — and `resolved_target`
    still carries it, because the caller writes the config from that field
    and a repo pinned at `origin/18.0` must not be recorded as anything
    else."""
    result = plan(SwitchFacts(alias="community"), target="origin/18.0", detach=True)

    assert result.args == ("switch", "--detach", "origin/18.0")
    assert result.resolved_target == "origin/18.0"


def test_a_remote_only_start_point_is_named_by_its_remote_ref():
    """git guesses nothing for a start point: `switch -c fix feature-x`
    fails outright unless feature-x resolves."""
    result = plan(SwitchFacts(alias="community", dwim_remote="upstream"), create="fix")

    assert result.args == ("switch", "-c", "fix", "upstream/feature-x")
    assert result.resolved_target == "upstream/feature-x"


def test_create_from_a_start_point():
    result = plan(SwitchFacts(alias="community"), target="origin/18.0", create="new-branch")

    assert result.args == ("switch", "-c", "new-branch", "origin/18.0")
    assert result.upstream is None


def test_create_without_a_start_point_needs_no_resolution():
    """`-c NEW` alone leaves target_resolvable at its default True — there
    was nothing to resolve."""
    result = plan(SwitchFacts(alias="community"), target=None, create="new-branch")

    assert not result.is_skipped
    assert result.args == ("switch", "-c", "new-branch")


def test_a_missing_worktree_points_at_ow_init():
    result = plan(SwitchFacts(alias="community", worktree_missing=True))

    assert result.is_skipped
    assert result.args == ()
    assert "ow init" in result.hint


def test_a_busy_repo_is_skipped_with_its_resume_commands():
    busy = ("rebase", "git rebase --continue", "git rebase --abort")
    result = plan(SwitchFacts(alias="community", busy=busy))

    assert result.is_skipped
    assert "rebase in progress" in result.skip_reason
    assert result.resume == ("git rebase --continue", "git rebase --abort")


def test_an_unresolvable_target_says_how_to_create_it():
    """The usual reason a branch is nowhere to be found is that it does not
    exist yet, so the refusal names `-c` rather than just saying no."""
    result = plan(SwitchFacts(alias="community", target_resolvable=False))

    assert result.is_skipped
    assert "feature-x" in result.skip_reason
    assert result.hint == "create it with `ow switch -c feature-x`"


def test_detach_does_not_suggest_creating_a_branch():
    """`--detach` asks for a ref that exists; `-c` would answer another question."""
    result = plan(SwitchFacts(alias="community", target_resolvable=False), detach=True)

    assert "feature-x" in result.skip_reason
    assert result.hint is None


def test_creating_a_branch_that_already_exists_is_refused_before_anything_moves():
    """git refuses `switch -c` on an existing branch, so a repo that already
    has it would fail mid-run — after its siblings had already moved."""
    result = plan(SwitchFacts(alias="community", create_exists=True), create="feature-x")

    assert result.is_skipped
    assert result.args == ()
    assert result.hint == "switch to it with `ow switch feature-x`"


def test_a_repo_already_on_the_target_runs_nothing():
    """`git switch x` on a repo already on x only answers "Already on 'x'",
    and rewriting its spec would churn the config of a repo that never moved."""
    result = plan(SwitchFacts(alias="community", current_branch="feature-x"))

    assert result.is_noop
    assert not result.is_skipped
    assert result.args == ()


def test_detaching_is_never_a_noop_even_on_the_target_branch():
    """Being on branch x is not being detached at x: the run has work to do."""
    result = plan(SwitchFacts(alias="community", current_branch="feature-x"), detach=True)

    assert not result.is_noop
    assert result.args == ("switch", "--detach", "feature-x")


def test_worktree_missing_takes_priority_over_a_busy_check_that_never_happened():
    """Facts are gathered in this order for a reason: an absent worktree
    cannot be probed for a rebase-in-progress marker at all."""
    result = plan(SwitchFacts(alias="community", worktree_missing=True, busy=None))

    assert "worktree not found" in result.skip_reason


def test_a_busy_repo_beats_an_unresolvable_target():
    busy = ("cherry-pick", "git cherry-pick --continue", "git cherry-pick --abort")
    result = plan(SwitchFacts(alias="community", busy=busy, target_resolvable=False))

    assert "cherry-pick in progress" in result.skip_reason
