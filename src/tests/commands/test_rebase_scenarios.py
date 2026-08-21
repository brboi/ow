"""The scenarios from the design doc, on real git repositories.

Mocking subprocess cannot express commit reachability or patch identity,
which is precisely what every decision here turns on.
"""

from ow.commands.rebase import _bound, gather_facts
from ow.utils.rebase_plan import plan_for


def build_pushed_branch(lab):
    """origin/master at D; work branch X, Y also published as dev/work.

    A - B - C - D            <- origin/master
         \\
          X - Y              <- work, dev/work
    """
    lab.commit("B")
    lab.set_remote_ref("origin/master", "HEAD")
    lab.git("checkout", "-q", "-b", "work")
    lab.commit("X")
    lab.commit("Y")
    lab.set_remote_ref("dev/work", "HEAD")
    lab.git("checkout", "-q", "master")
    lab.commit("C")
    lab.commit("D")
    lab.set_remote_ref("origin/master", "HEAD")
    lab.git("checkout", "-q", "work")


def run(lab, plan):
    for step in plan.steps:
        lab.git(*step.args)


class TestIdempotence:
    """Scenario 1 — the heart of issue #11."""

    def test_first_run_moves_the_work_onto_the_base(self, git_lab):
        build_pushed_branch(git_lab)
        facts = gather_facts(
            git_lab.path, "community", "origin/master", "dev/work",
            git_lab.sha("refs/remotes/dev/work"), is_detached=False,
        )
        assert facts.unpushed == 0
        run(git_lab, plan_for(facts))

        assert git_lab.git("rev-list", "--count", "origin/master..HEAD") == "2"
        assert "D" in git_lab.git("log", "--format=%s", "HEAD")

    def test_second_run_changes_nothing(self, git_lab):
        """The test the old mock-based suite could never have written."""
        build_pushed_branch(git_lab)
        first = gather_facts(
            git_lab.path, "community", "origin/master", "dev/work",
            git_lab.sha("refs/remotes/dev/work"), is_detached=False,
        )
        run(git_lab, plan_for(first))
        after_first = git_lab.sha("HEAD")

        second = gather_facts(
            git_lab.path, "community", "origin/master", "dev/work",
            git_lab.sha("refs/remotes/dev/work"), is_detached=False,
        )
        second_plan = plan_for(second)
        run(git_lab, second_plan)

        assert git_lab.sha("HEAD") == after_first
        assert second_plan.is_noop

    def test_the_stale_upstream_does_not_trigger_step_one(self, git_lab):
        build_pushed_branch(git_lab)
        first = gather_facts(
            git_lab.path, "community", "origin/master", "dev/work",
            git_lab.sha("refs/remotes/dev/work"), is_detached=False,
        )
        run(git_lab, plan_for(first))

        second = gather_facts(
            git_lab.path, "community", "origin/master", "dev/work",
            git_lab.sha("refs/remotes/dev/work"), is_detached=False,
        )
        # A plain commit count would say 2 here; patch identity says 0.
        assert second.new_patches == 0
        assert plan_for(second).step1_target is None


class TestColleaguePushed:
    """Scenario 2 — the only case where step 1 is warranted."""

    def test_the_new_commit_is_integrated_without_duplicating_the_old_ones(self, git_lab):
        build_pushed_branch(git_lab)
        first = gather_facts(
            git_lab.path, "community", "origin/master", "dev/work",
            git_lab.sha("refs/remotes/dev/work"), is_detached=False,
        )
        run(git_lab, plan_for(first))
        git_lab.commit("W")

        # A colleague pushes Z on top of the published branch.
        up_before = git_lab.sha("refs/remotes/dev/work")
        git_lab.git("checkout", "-q", "-b", "colleague", up_before)
        git_lab.commit("Z")
        git_lab.set_remote_ref("dev/work", "colleague")
        git_lab.git("checkout", "-q", "work")

        facts = gather_facts(
            git_lab.path, "community", "origin/master", "dev/work",
            up_before, is_detached=False,
        )
        assert facts.new_patches == 1
        assert facts.force_pushed is False
        assert facts.bound == git_lab.git("merge-base", "HEAD", "origin/master")
        assert facts.unpushed == 1
        run(git_lab, plan_for(facts))

        subjects = git_lab.git("log", "--format=%s", "origin/master..HEAD").split("\n")
        assert sorted(subjects) == ["W", "X", "Y", "Z"]


class TestForcePush:
    """Scenario 3 — no reflog consulted, no reset --hard."""

    def test_local_commit_survives_a_squash(self, git_lab):
        build_pushed_branch(git_lab)
        up_before = git_lab.sha("refs/remotes/dev/work")
        git_lab.commit("W")

        # The colleague squashes X and Y into one commit and force-pushes.
        fork = git_lab.git("merge-base", "HEAD", "origin/master")
        git_lab.git("checkout", "-q", "-b", "squashed", fork)
        git_lab.commit("XY")
        git_lab.set_remote_ref("dev/work", "squashed")
        git_lab.git("checkout", "-q", "work")

        facts = gather_facts(
            git_lab.path, "community", "origin/master", "dev/work",
            up_before, is_detached=False,
        )
        assert facts.force_pushed is True
        assert facts.bound == up_before
        assert facts.unpushed == 1
        # `base..HEAD` would say 3 here (X, Y, W); only W actually moves.
        # This is the one scenario where the two candidate ranges disagree,
        # so it is the only place the choice of range is observable.
        assert facts.replay_count == 1

        run(git_lab, plan_for(facts))
        subjects = git_lab.git("log", "--format=%s", "origin/master..HEAD").split("\n")
        assert sorted(subjects) == ["W", "XY"]


class TestBoundInvariant:
    """The bound is never older than merge-base(HEAD, base), which is what
    keeps base commits from ever being replayed."""

    def test_bound_is_the_base_merge_base_when_no_upstream_history(self, git_lab):
        build_pushed_branch(git_lab)
        expected = git_lab.git("merge-base", "HEAD", "origin/master")
        assert _bound(git_lab.path, "origin/master", None) == expected

    def test_bound_prefers_the_upstream_when_it_is_more_recent(self, git_lab):
        build_pushed_branch(git_lab)
        up_before = git_lab.sha("refs/remotes/dev/work")
        git_lab.commit("W")
        assert _bound(git_lab.path, "origin/master", up_before) == up_before

    def test_bound_rejects_an_upstream_older_than_the_base_fork(self, git_lab):
        """After a local rebase the old upstream sits behind the base fork;
        using it would replay the base commits."""
        build_pushed_branch(git_lab)
        up_before = git_lab.sha("refs/remotes/dev/work")
        first = gather_facts(
            git_lab.path, "community", "origin/master", "dev/work",
            up_before, is_detached=False,
        )
        run(git_lab, plan_for(first))

        base_fork = git_lab.git("merge-base", "HEAD", "origin/master")
        assert _bound(git_lab.path, "origin/master", up_before) == base_fork

    def test_no_base_commit_is_ever_inside_the_replay_range(self, git_lab):
        build_pushed_branch(git_lab)
        up_before = git_lab.sha("refs/remotes/dev/work")
        git_lab.commit("W")
        bound = _bound(git_lab.path, "origin/master", up_before)

        replayed = git_lab.git("rev-list", f"{bound}..HEAD").split("\n")
        base_commits = set(git_lab.git("rev-list", "origin/master").split("\n"))
        assert not (set(replayed) & base_commits)


class TestForcePushAlreadyFetched:
    """Task 8 — the force-push already absorbed by an earlier command
    (ow status, or a plain git fetch) before ow rebase ever runs. The
    pre-fetch capture then sees up_before == up_now — the value handed to
    gather_facts is already the post-force SHA — and only the reflog-backed
    fork-point can still recover the pre-force value."""

    def test_bound_recovers_the_pre_force_value_from_the_reflog(self, git_lab):
        build_pushed_branch(git_lab)
        git_lab.commit("W")  # local work to preserve

        # Reflogs are on by default for a non-bare repo, but set it
        # explicitly so the test does not depend on that default.
        git_lab.git("config", "core.logAllRefUpdates", "true")

        up_before = git_lab.sha("refs/remotes/dev/work")  # the pre-force value (Y)

        # The colleague squashes X and Y into one commit and force-pushes.
        fork = git_lab.git("merge-base", "HEAD", "origin/master")
        git_lab.git("checkout", "-q", "-b", "squashed", fork)
        git_lab.commit("XY")
        git_lab.set_remote_ref("dev/work", "squashed")  # update-ref -> reflog entry written
        git_lab.git("checkout", "-q", "work")

        # Simulate `ow status` having already absorbed the force-push:
        # gather_facts is handed up_before == the post-force value, exactly
        # what fetch_workspace_refs would report after the ref already moved.
        post_force = git_lab.sha("refs/remotes/dev/work")
        facts = gather_facts(
            git_lab.path, "community", "origin/master", "dev/work",
            post_force, is_detached=False,
        )

        assert facts.force_pushed is False  # detection genuinely lost, as expected
        assert facts.bound == up_before  # but the fork-point recovers it anyway
        run(git_lab, plan_for(facts))

        subjects = git_lab.git("log", "--format=%s", "origin/master..HEAD").split("\n")
        assert sorted(subjects) == ["W", "XY"]

    def test_degrades_gracefully_with_no_reflog_available(self, git_lab):
        """No reflog to fall back on: the bound widens all the way back to
        the base fork. gather_facts and plan_for must not raise, and the
        plan must still run — it just replays more than necessary
        (duplication, not destruction: the same safe-failure class as the
        mixed force-push case already documented in the spec)."""
        build_pushed_branch(git_lab)
        git_lab.commit("W")

        fork = git_lab.git("merge-base", "HEAD", "origin/master")
        git_lab.git("checkout", "-q", "-b", "squashed", fork)
        git_lab.commit("XY")
        git_lab.set_remote_ref("dev/work", "squashed")
        # Delete the ref's reflog entirely: fork-point has nothing to walk.
        reflog_path = git_lab.path / ".git" / "logs" / "refs" / "remotes" / "dev" / "work"
        assert reflog_path.exists()
        reflog_path.unlink()
        git_lab.git("checkout", "-q", "work")

        post_force = git_lab.sha("refs/remotes/dev/work")
        facts = gather_facts(
            git_lab.path, "community", "origin/master", "dev/work",
            post_force, is_detached=False,
        )

        assert facts.force_pushed is False
        assert facts.bound == fork
        plan = plan_for(facts)
        run(git_lab, plan)  # must not raise

        subjects = git_lab.git("log", "--format=%s", "origin/master..HEAD").split("\n")
        assert sorted(subjects) == ["W", "X", "XY", "Y"]


class TestFactsFromState:
    def test_busy_repo_is_reported_before_anything_else(self, git_lab):
        build_pushed_branch(git_lab)
        git_lab.git("checkout", "-q", "master")
        git_lab.commit("conflict", content="master")
        git_lab.git("checkout", "-q", "work")
        git_lab.commit("conflict", content="work")
        import subprocess
        subprocess.run(
            ["git", "-C", str(git_lab.path), "rebase", "master"], capture_output=True,
        )

        facts = gather_facts(
            git_lab.path, "community", "origin/master", None, None, is_detached=False,
        )
        assert facts.busy is not None
        assert facts.busy[0] == "rebase"

    def test_dirty_files_are_reported(self, git_lab):
        build_pushed_branch(git_lab)
        (git_lab.path / "X.txt").write_text("edited")
        facts = gather_facts(
            git_lab.path, "community", "origin/master", None, None, is_detached=False,
        )
        assert facts.dirty_files == ("X.txt",)

    def test_replay_count_matches_what_will_move(self, git_lab):
        build_pushed_branch(git_lab)
        facts = gather_facts(
            git_lab.path, "community", "origin/master", "dev/work",
            git_lab.sha("refs/remotes/dev/work"), is_detached=False,
        )
        assert facts.replay_count == 2

    def test_an_unresolvable_upstream_does_not_raise(self, git_lab):
        """get_rev_list_count's check=True would otherwise blow up here."""
        build_pushed_branch(git_lab)
        facts = gather_facts(
            git_lab.path, "community", "origin/master", "refs/remotes/dev/gone",
            None, is_detached=False,
        )
        assert facts.force_pushed is False
        assert facts.new_patches == 0
        assert facts.unpushed == 0


class TestObservedShape:
    """D1 — the config's `is_detached` is an intent, not an observation.

    Planning against the config while the worktree has drifted rebases a
    detached HEAD and reports success; the result is abandoned to the reflog
    the moment anything switches back to the configured branch.
    """

    def test_a_detached_worktree_under_an_attached_config_is_skipped(self, git_lab):
        build_pushed_branch(git_lab)
        git_lab.git("checkout", "-q", "--detach", "HEAD")

        facts = gather_facts(
            git_lab.path, "community", "origin/master", None, None, is_detached=False,
        )

        assert facts.is_detached is True  # observed, not the config's False
        assert facts.detached_drift is True
        plan = plan_for(facts)
        assert plan.is_skipped
        assert plan.steps == ()
        assert "ow apply" in plan.skip_reason

    def test_an_attached_worktree_under_a_detached_config_is_skipped(self, git_lab):
        build_pushed_branch(git_lab)

        facts = gather_facts(
            git_lab.path, "community", "origin/master", None, None, is_detached=True,
        )

        assert facts.is_detached is False
        assert facts.detached_drift is True
        assert plan_for(facts).is_skipped

    def test_an_aligned_detached_worktree_still_plans_its_switch(self, git_lab):
        build_pushed_branch(git_lab)
        git_lab.git("checkout", "-q", "--detach", "HEAD")

        facts = gather_facts(
            git_lab.path, "community", "origin/master", None, None, is_detached=True,
        )

        assert facts.detached_drift is False
        plan = plan_for(facts)
        assert not plan.is_skipped
        assert plan.detaches

    def test_an_aligned_attached_worktree_is_unaffected(self, git_lab):
        build_pushed_branch(git_lab)

        facts = gather_facts(
            git_lab.path, "community", "origin/master", "dev/work",
            git_lab.sha("refs/remotes/dev/work"), is_detached=False,
        )

        assert facts.detached_drift is False
        assert not plan_for(facts).is_skipped
