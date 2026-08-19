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

        run(git_lab, plan_for(facts))
        subjects = git_lab.git("log", "--format=%s", "origin/master..HEAD").split("\n")
        assert "W" in subjects
        assert "XY" in subjects


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
