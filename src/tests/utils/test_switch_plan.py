"""Table-driven tests of the switch decision table.

No git, no filesystem: `plan_switch` is a pure function from observed
facts to either the exact `git switch` invocation or the reason this
repo cannot run it.
"""

from ow.utils.switch_plan import SwitchFacts, SwitchPlan, plan_switch

_ARGS = ("switch", "--guess", "feature-x")


def test_a_clean_repo_gets_the_given_args_unchanged():
    facts = SwitchFacts(alias="community")

    plan = plan_switch(facts, _ARGS)

    assert plan == SwitchPlan(alias="community", args=_ARGS)
    assert not plan.is_skipped


def test_a_missing_worktree_points_at_ow_apply():
    facts = SwitchFacts(alias="community", worktree_missing=True)

    plan = plan_switch(facts, _ARGS)

    assert plan.is_skipped
    assert plan.args == ()
    assert "ow apply" in plan.skip_reason


def test_a_busy_repo_is_skipped_with_its_resume_commands():
    busy = ("rebase", "git rebase --continue", "git rebase --abort")
    facts = SwitchFacts(alias="community", busy=busy)

    plan = plan_switch(facts, _ARGS)

    assert plan.is_skipped
    assert "rebase in progress" in plan.skip_reason
    assert plan.resume == ("git rebase --continue", "git rebase --abort")


def test_an_unresolvable_target_is_skipped():
    facts = SwitchFacts(alias="community", target_resolvable=False)

    plan = plan_switch(facts, _ARGS)

    assert plan.is_skipped
    assert "not found" in plan.skip_reason


def test_worktree_missing_takes_priority_over_a_busy_check_that_never_happened():
    """Facts are gathered in this order for a reason: an absent worktree
    cannot be probed for a rebase-in-progress marker at all."""
    facts = SwitchFacts(alias="community", worktree_missing=True, busy=None)

    plan = plan_switch(facts, _ARGS)

    assert "ow apply" in plan.skip_reason


def test_create_without_a_start_point_needs_no_resolution():
    """`-c NEW` alone leaves target_resolvable at its default True — there
    was nothing to resolve."""
    facts = SwitchFacts(alias="community")
    args = ("switch", "-c", "new-branch")

    plan = plan_switch(facts, args)

    assert not plan.is_skipped
    assert plan.args == args


def test_a_busy_repo_beats_an_unresolvable_target():
    busy = ("cherry-pick", "git cherry-pick --continue", "git cherry-pick --abort")
    facts = SwitchFacts(alias="community", busy=busy, target_resolvable=False)

    plan = plan_switch(facts, _ARGS)

    assert "cherry-pick in progress" in plan.skip_reason
