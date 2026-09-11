"""The reset decision table.

Pure facts in, git steps out. The command exists to destroy things, so
every guard that stops it destroying the wrong thing is pinned here.
"""

from ow.utils.reset_plan import ResetFacts, plan_reset


def _facts(**kwargs) -> ResetFacts:
    base = dict(
        alias="community",
        target="origin/master",
        head="a" * 40,
        target_sha="b" * 40,
    )
    return ResetFacts(**{**base, **kwargs})


def test_an_operation_in_progress_is_skipped_with_a_way_out():
    plan = plan_reset(_facts(busy=("rebase", "git rebase --continue", "git rebase --abort")))

    assert plan.is_skipped
    assert plan.skip_reason == "rebase in progress"
    assert plan.resume == ("git rebase --continue", "git rebase --abort")
    assert plan.steps == ()


def test_a_worktree_on_another_branch_is_never_reset():
    """Resetting it would throw away work ow was never told about."""
    plan = plan_reset(_facts(drift="on branch hotfix, config says featA"))

    assert plan.is_skipped
    assert "hotfix" in plan.skip_reason
    assert "ow apply" in plan.skip_reason
    assert plan.steps == ()


def test_drift_outranks_being_dirty():
    plan = plan_reset(_facts(drift="detached, config says branch featA", dirty_files=("a.py",)), hard=True)

    assert plan.is_skipped
    assert plan.steps == ()


def test_an_unresolvable_target_is_skipped_rather_than_guessed_at():
    plan = plan_reset(_facts(target_sha=None))

    assert plan.is_skipped
    assert plan.skip_reason == "could not resolve origin/master"


def test_an_unresolvable_head_is_skipped():
    plan = plan_reset(_facts(head=None))

    assert plan.is_skipped
    assert plan.skip_reason == "could not resolve HEAD"


def test_already_on_the_target_with_a_clean_tree_is_nothing_to_do():
    plan = plan_reset(_facts(head="c" * 40, target_sha="c" * 40))

    assert plan.is_noop
    assert plan.steps == ()


def test_a_dirty_tree_alone_is_nothing_to_do_without_hard():
    """The plain form does not touch the working tree, so there is no work
    in a repo that is already on its target."""
    plan = plan_reset(_facts(head="c" * 40, target_sha="c" * 40, dirty_files=("a.py",)))

    assert plan.is_noop


def test_a_dirty_tree_alone_is_work_for_hard():
    plan = plan_reset(_facts(head="c" * 40, target_sha="c" * 40, dirty_files=("a.py",)), hard=True)

    assert not plan.is_noop
    assert [s.args for s in plan.steps] == [("reset", "--hard", "origin/master")]
    assert plan.discarded == 1
    assert plan.moves is False


def test_the_plain_form_never_discards_the_working_tree():
    """Its whole safety story: the content of dropped commits is still on
    disk afterwards, as unstaged changes."""
    plan = plan_reset(_facts(drop_commits=3, dirty_files=("a.py", "b.py")))

    assert [s.args for s in plan.steps] == [("reset", "origin/master")]
    assert plan.discarded == 0
    assert plan.drop_commits == 3


def test_hard_discards_the_working_tree_too():
    plan = plan_reset(_facts(drop_commits=3, dirty_files=("a.py", "b.py")), hard=True)

    assert [s.args for s in plan.steps] == [("reset", "--hard", "origin/master")]
    assert plan.discarded == 2


def test_commits_no_remote_carries_are_carried_into_the_plan():
    """The one number that says whether this is recoverable."""
    plan = plan_reset(_facts(drop_commits=5, unbacked=2))

    assert plan.unbacked == 2
    assert plan.loses_work


def test_dropping_only_pushed_commits_loses_nothing():
    plan = plan_reset(_facts(drop_commits=5, unbacked=0))

    assert not plan.loses_work


def test_a_repo_merely_behind_its_target_moves_without_dropping_anything():
    plan = plan_reset(_facts(drop_commits=0))

    assert plan.moves
    assert plan.drop_commits == 0
    assert not plan.loses_work
    assert [s.args for s in plan.steps] == [("reset", "origin/master")]


def test_the_step_carries_the_ref_it_lands_on():
    plan = plan_reset(_facts(target="dev/master-feature", target_sha="d" * 40))

    assert plan.steps[0].onto == "dev/master-feature"
