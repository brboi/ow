"""The pull decision table.

Pure facts in, git steps out. The one thing that must never happen is a
step that rewrites or discards history, so every branch is pinned.
"""

from ow.utils.pull_plan import PullFacts, plan_pull


def _facts(**kwargs) -> PullFacts:
    base = dict(
        alias="community",
        target="origin/master",
        head="a" * 40,
        target_sha="b" * 40,
        ff_possible=True,
    )
    return PullFacts(**{**base, **kwargs})


def test_an_operation_in_progress_is_skipped_with_a_way_out():
    plan = plan_pull(_facts(busy=("rebase", "git rebase --continue", "git rebase --abort")))

    assert plan.is_skipped
    assert "rebase in progress" == plan.skip_reason
    assert plan.resume == ("git rebase --continue", "git rebase --abort")
    assert plan.steps == ()


def test_a_worktree_that_disagrees_with_its_config_is_left_alone():
    plan = plan_pull(_facts(detached_drift=True))

    assert plan.is_skipped
    assert "ow apply" in plan.skip_reason


def test_an_unresolvable_target_is_skipped_rather_than_guessed_at():
    plan = plan_pull(_facts(target_sha=None, ff_possible=False))

    assert plan.is_skipped
    assert "could not resolve origin/master" == plan.skip_reason


def test_already_there_is_not_an_operation():
    plan = plan_pull(_facts(head="c" * 40, target_sha="c" * 40))

    assert plan.is_noop
    assert not plan.is_skipped
    assert plan.steps == ()


def test_being_strictly_ahead_is_up_to_date_not_a_divergence():
    """Unpushed local work is the normal state of a feature branch. Calling
    it a divergence sent every such repo to `ow rebase` for nothing."""
    plan = plan_pull(_facts(ff_possible=False, target_merged=True))

    assert plan.is_noop
    assert not plan.is_skipped


def test_being_ahead_outranks_having_an_upstream_to_replay_on():
    plan = plan_pull(_facts(ff_possible=False, target_merged=True, target_is_upstream=True))

    assert plan.is_noop
    assert plan.steps == ()



def test_diverging_from_the_base_ref_is_handed_to_rebase():
    """No upstream: the target is the base branch, and carrying work over to
    a moved base is rebase's job, force-push detection and all."""
    plan = plan_pull(_facts(ff_possible=False, target_is_upstream=False))

    assert plan.is_skipped
    assert "ow rebase" in plan.skip_reason
    assert plan.steps == ()


def test_diverging_from_its_own_upstream_is_replayed_like_git_pull_rebase():
    """Same branch, two copies. Replaying ours on theirs moves nothing off
    the base and invents no merge commit."""
    plan = plan_pull(
        _facts(ff_possible=False, target_is_upstream=True, target="dev/work")
    )

    assert plan.rebases
    assert [s.args for s in plan.steps] == [("rebase", "dev/work")]


def test_a_dirty_worktree_blocks_the_replay_with_the_files_named():
    plan = plan_pull(
        _facts(ff_possible=False, target_is_upstream=True, dirty_files=("a.py", "b.py"))
    )

    assert plan.is_skipped
    assert "a.py" in plan.skip_reason
    assert plan.steps == ()


def test_a_dirty_worktree_does_not_block_a_plain_fast_forward():
    """git refuses a fast-forward that would clobber a local change, and
    allows one that would not. That judgement is git's, not ours."""
    plan = plan_pull(_facts(dirty_files=("a.py",)))

    assert [s.args for s in plan.steps] == [("merge", "--ff-only", "origin/master")]


def test_a_detached_worktree_follows_the_ref():
    plan = plan_pull(_facts(is_detached=True))

    assert plan.detaches
    assert [s.args for s in plan.steps] == [("switch", "--detach", "origin/master")]


def test_an_attached_branch_is_fast_forwarded_and_never_plain_merged():
    plan = plan_pull(_facts())

    assert not plan.is_skipped and not plan.is_noop
    assert [s.args for s in plan.steps] == [("merge", "--ff-only", "origin/master")]


def test_being_busy_outranks_being_diverged():
    """Reporting the divergence would send the user to rebase, which would
    refuse the same way — the operation in progress is the actionable fact."""
    plan = plan_pull(
        _facts(ff_possible=False, busy=("merge", "git merge --continue", "git merge --abort"))
    )

    assert "merge in progress" == plan.skip_reason


def test_the_step_carries_the_ref_it_lands_on():
    plan = plan_pull(_facts(target="dev/master-feature"))

    assert plan.steps[0].onto == "dev/master-feature"
