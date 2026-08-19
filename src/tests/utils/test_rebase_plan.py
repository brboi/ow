from ow.utils.rebase_plan import GitStep, RepoFacts, RebasePlan, plan_for


def facts(**kwargs) -> RepoFacts:
    base = dict(alias="community", base="origin/master", bound="BOUND", base_merged=True)
    base.update(kwargs)
    return RepoFacts(**base)


class TestSkips:
    def test_busy_repo_is_skipped_with_its_resume_commands(self):
        plan = plan_for(facts(busy=("rebase", "git rebase --continue", "git rebase --abort")))
        assert plan.is_skipped
        assert "rebase in progress" in plan.skip_reason
        assert plan.resume == ("git rebase --continue", "git rebase --abort")
        assert plan.steps == ()

    def test_busy_wins_over_dirty(self):
        plan = plan_for(facts(
            busy=("merge", "git merge --continue", "git merge --abort"),
            dirty_files=("a.py",),
        ))
        assert "merge in progress" in plan.skip_reason

    def test_dirty_repo_is_skipped_and_names_its_files(self):
        plan = plan_for(facts(dirty_files=("a.py", "b.py")))
        assert plan.is_skipped
        assert "a.py" in plan.skip_reason and "b.py" in plan.skip_reason
        assert plan.steps == ()

    def test_dirty_file_list_is_truncated(self):
        plan = plan_for(facts(dirty_files=("a", "b", "c", "d", "e")))
        assert "+2 more" in plan.skip_reason

    def test_autostash_lets_a_dirty_repo_through(self):
        plan = plan_for(facts(dirty_files=("a.py",), base_merged=False), autostash=True)
        assert not plan.is_skipped
        assert plan.steps == (GitStep(("rebase", "--autostash", "origin/master"), "origin/master"),)

    def test_autostash_does_not_apply_to_a_detached_worktree(self):
        """git switch has no --autostash, so a dirty detached repo is still skipped."""
        plan = plan_for(facts(dirty_files=("a.py",), is_detached=True), autostash=True)
        assert plan.is_skipped
        assert "--autostash does not apply" in plan.skip_reason
        assert plan.steps == ()

    def test_clean_detached_worktree_still_switches_under_autostash(self):
        plan = plan_for(facts(is_detached=True), autostash=True)
        assert not plan.is_skipped
        assert plan.steps == (
            GitStep(("switch", "--detach", "origin/master"), "origin/master"),
        )


class TestDetached:
    def test_switches_without_rebasing(self):
        plan = plan_for(facts(is_detached=True))
        assert plan.steps == (
            GitStep(("switch", "--detach", "origin/master"), "origin/master"),
        )

    def test_detaches_is_true_for_a_detached_plan(self):
        plan = plan_for(facts(is_detached=True))
        assert plan.detaches is True

    def test_detaches_is_false_for_a_rebase_plan(self):
        plan = plan_for(facts(base_merged=False))
        assert plan.detaches is False


class TestSingleRebase:
    def test_no_upstream_rebases_onto_the_base(self):
        plan = plan_for(facts(base_merged=False))
        assert plan.steps == (GitStep(("rebase", "origin/master"), "origin/master"),)

    def test_upstream_with_nothing_new_rebases_onto_the_base_only(self):
        """The idempotence fix: step 1 must not fire just because an upstream exists."""
        plan = plan_for(facts(up="dev/work", new_patches=0, base_merged=False))
        assert plan.steps == (GitStep(("rebase", "origin/master"), "origin/master"),)
        assert plan.step1_target is None

    def test_nothing_to_do_when_already_on_the_base(self):
        plan = plan_for(facts(up="dev/work", new_patches=0, base_merged=True))
        assert plan.steps == ()
        assert plan.is_noop
        assert not plan.is_skipped


class TestTwoStepRebase:
    def test_new_patches_upstream_trigger_an_onto_step(self):
        plan = plan_for(facts(up="dev/work", new_patches=1, base_merged=True))
        assert plan.steps == (
            GitStep(("rebase", "--onto", "dev/work", "BOUND"), "dev/work"),
            GitStep(("rebase", "origin/master"), "origin/master"),
        )
        assert plan.step1_target == "dev/work"

    def test_force_push_triggers_an_onto_step_even_with_no_new_patches(self):
        plan = plan_for(facts(up="dev/work", force_pushed=True, new_patches=0, base_merged=True))
        assert plan.steps[0] == GitStep(("rebase", "--onto", "dev/work", "BOUND"), "dev/work")

    def test_step_two_always_follows_step_one(self):
        """After step 1 HEAD sits on the upstream, so it must be moved to the base."""
        plan = plan_for(facts(up="dev/work", new_patches=1, base_merged=True))
        assert len(plan.steps) == 2
        assert plan.steps[-1].onto == "origin/master"

    def test_autostash_applies_to_both_steps(self):
        plan = plan_for(facts(up="dev/work", new_patches=1, base_merged=True), autostash=True)
        assert plan.steps == (
            GitStep(("rebase", "--autostash", "--onto", "dev/work", "BOUND"), "dev/work"),
            GitStep(("rebase", "--autostash", "origin/master"), "origin/master"),
        )

    def test_no_onto_step_without_a_bound(self):
        """A missing merge-base means we cannot bound the replay safely."""
        plan = plan_for(facts(up="dev/work", new_patches=1, bound=None, base_merged=False))
        assert plan.steps == (GitStep(("rebase", "origin/master"), "origin/master"),)


class TestCarriedFields:
    def test_display_fields_survive(self):
        plan = plan_for(facts(replay_count=7, unpushed=3, force_pushed=True, up="dev/work"))
        assert plan.replay_count == 7
        assert plan.unpushed == 3
        assert plan.force_pushed is True
        assert plan.alias == "community"
        assert plan.base == "origin/master"
