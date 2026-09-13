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


def test_detach_passes_the_ref_through_untouched():
    result = plan(SwitchFacts(alias="community", dwim_remote="origin"), detach=True)

    assert result.args == ("switch", "--detach", "feature-x")
    assert result.upstream is None


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


def test_a_missing_worktree_points_at_ow_apply():
    result = plan(SwitchFacts(alias="community", worktree_missing=True))

    assert result.is_skipped
    assert result.args == ()
    assert "ow apply" in result.skip_reason


def test_a_busy_repo_is_skipped_with_its_resume_commands():
    busy = ("rebase", "git rebase --continue", "git rebase --abort")
    result = plan(SwitchFacts(alias="community", busy=busy))

    assert result.is_skipped
    assert "rebase in progress" in result.skip_reason
    assert result.resume == ("git rebase --continue", "git rebase --abort")


def test_an_unresolvable_target_is_skipped():
    result = plan(SwitchFacts(alias="community", target_resolvable=False))

    assert result.is_skipped
    assert "not found" in result.skip_reason


def test_worktree_missing_takes_priority_over_a_busy_check_that_never_happened():
    """Facts are gathered in this order for a reason: an absent worktree
    cannot be probed for a rebase-in-progress marker at all."""
    result = plan(SwitchFacts(alias="community", worktree_missing=True, busy=None))

    assert "ow apply" in result.skip_reason


def test_a_busy_repo_beats_an_unresolvable_target():
    busy = ("cherry-pick", "git cherry-pick --continue", "git cherry-pick --abort")
    result = plan(SwitchFacts(alias="community", busy=busy, target_resolvable=False))

    assert "cherry-pick in progress" in result.skip_reason
