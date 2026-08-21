import subprocess

from ow.utils.git import (
    _git_dir,
    count_commits,
    count_new_patches,
    count_unpushed,
    dirty_files,
    in_progress_operation,
    is_ancestor,
    merge_base,
    merge_base_fork_point,
    rev_parse,
)


class TestRevParse:
    def test_resolves_an_existing_ref(self, git_lab):
        assert rev_parse(git_lab.path, "HEAD") == git_lab.sha("HEAD")

    def test_returns_none_for_a_missing_ref(self, git_lab):
        assert rev_parse(git_lab.path, "refs/remotes/nope/nope") is None


class TestIsAncestor:
    def test_true_for_a_parent(self, git_lab):
        a = git_lab.sha("HEAD")
        b = git_lab.commit("B")
        assert is_ancestor(git_lab.path, a, b) is True

    def test_false_for_a_descendant(self, git_lab):
        a = git_lab.sha("HEAD")
        b = git_lab.commit("B")
        assert is_ancestor(git_lab.path, b, a) is False

    def test_true_for_the_same_commit(self, git_lab):
        a = git_lab.sha("HEAD")
        assert is_ancestor(git_lab.path, a, a) is True


class TestMergeBase:
    def test_finds_the_fork(self, git_lab):
        fork = git_lab.sha("HEAD")
        git_lab.branch("side")
        git_lab.commit("B")
        git_lab.checkout("side")
        git_lab.commit("C")
        assert merge_base(git_lab.path, "master", "side") == fork


class TestMergeBaseForkPoint:
    """The primitive that recovers a force-push after some other command
    (ow status, a plain git fetch) already absorbed it into the ref."""

    def test_none_when_the_ref_has_no_reflog(self, git_lab):
        # 'other' diverges from HEAD, so its tip is not an ancestor of HEAD.
        # Tags never get a reflog, so this is a ref with nothing to walk —
        # the fallback single-entry check (the ref's current value) also
        # fails, which is what drives returncode != 0.
        git_lab.branch("other")
        git_lab.checkout("other")
        git_lab.commit("Z")
        git_lab.checkout("master")
        git_lab.git("tag", "notrail", "other")

        assert merge_base_fork_point(git_lab.path, "notrail") is None

    def test_returns_the_pre_force_value_when_reflog_is_enabled(self, git_lab):
        # X - Y            <- upstream, before the force-push
        #      \\
        #       W          <- HEAD, built on Y
        #
        # X - Z            <- upstream, after the force-push (unrelated to Y)
        x = git_lab.sha("HEAD")
        git_lab.branch("upstream", x)
        y = git_lab.commit("Y")
        git_lab.git("update-ref", "refs/heads/upstream", y)  # fast-forward
        git_lab.commit("W")  # HEAD built on top of Y

        git_lab.git("checkout", "-q", "-b", "colleague", x)
        git_lab.commit("Z")
        git_lab.git("checkout", "-q", "master")
        git_lab.git("update-ref", "refs/heads/upstream", git_lab.sha("colleague"))  # force-push

        # Sanity: plain merge-base only sees the old, pre-Y fork.
        assert merge_base(git_lab.path, "upstream", "HEAD") == x
        assert merge_base_fork_point(git_lab.path, "upstream") == y

    def test_a_fast_forward_only_history_returns_an_ancestor_of_head(self, git_lab):
        git_lab.branch("upstream")
        git_lab.commit("B")
        git_lab.git("update-ref", "refs/heads/upstream", "HEAD")

        point = merge_base_fork_point(git_lab.path, "upstream")

        assert point is not None
        assert is_ancestor(git_lab.path, point, git_lab.sha("HEAD"))


class TestCountCommits:
    def test_counts_a_range(self, git_lab):
        base = git_lab.sha("HEAD")
        git_lab.commit("B")
        git_lab.commit("C")
        assert count_commits(git_lab.path, f"{base}..HEAD") == 2

    def test_zero_for_an_empty_range(self, git_lab):
        assert count_commits(git_lab.path, "HEAD..HEAD") == 0


class TestCountNewPatches:
    """The primitive that keeps the idempotence fix honest."""

    def test_counts_commits_absent_from_head(self, git_lab):
        git_lab.branch("other")
        git_lab.checkout("other")
        git_lab.commit("Z")
        git_lab.set_remote_ref("dev/work", "other")
        git_lab.checkout("master")
        assert count_new_patches(git_lab.path, "dev/work") == 1

    def test_ignores_commits_whose_patch_head_already_carries(self, git_lab):
        # X exists on the remote copy, and again on HEAD under a different SHA
        # (as after a rebase). A plain commit count would say 1; patch identity says 0.
        git_lab.branch("remote_copy")
        git_lab.checkout("remote_copy")
        git_lab.commit("X")
        git_lab.set_remote_ref("dev/work", "remote_copy")
        git_lab.checkout("master")
        git_lab.commit("base_moved")
        git_lab.git("cherry-pick", git_lab.sha("remote_copy"))

        assert count_commits(git_lab.path, "HEAD..dev/work") == 1
        assert count_new_patches(git_lab.path, "dev/work") == 0


class TestCountUnpushed:
    """The primitive that keeps 'unpushed' from double-counting the base."""

    def test_zero_when_head_is_at_the_bound(self, git_lab):
        bound = git_lab.sha("HEAD")
        assert count_unpushed(git_lab.path, bound, "HEAD") == 0

    def test_ignores_a_commit_whose_patch_is_already_upstream(self, git_lab):
        # X exists on "upstream_copy", and again on HEAD under a different
        # SHA (as after a cherry-pick/rebase). Only Y is genuinely unpushed.
        bound = git_lab.sha("HEAD")
        git_lab.branch("upstream_copy")
        git_lab.checkout("upstream_copy")
        x_sha = git_lab.commit("X")
        git_lab.checkout("master")
        git_lab.git("cherry-pick", x_sha)
        git_lab.commit("Y")
        assert count_unpushed(git_lab.path, bound, "upstream_copy") == 1

    def test_commits_before_the_bound_are_not_counted(self, git_lab):
        """A plain count of HEAD's commits missing from `other` would also
        catch commits older than the bound — e.g. the base branch's own
        history — which is exactly the double-counting bug this guards
        against."""
        git_lab.branch("other")  # published branch shares no history beyond this point
        git_lab.commit("C")  # base-side commit, predates the bound
        bound = git_lab.sha("HEAD")
        git_lab.commit("W")
        assert count_unpushed(git_lab.path, bound, "other") == 1


class TestInProgressOperation:
    def test_none_when_idle(self, git_lab):
        assert in_progress_operation(git_lab.path) is None

    def test_detects_a_stopped_cherry_pick(self, git_lab):
        git_lab.branch("side")
        git_lab.checkout("side")
        git_lab.commit("conflicting", content="side")
        git_lab.checkout("master")
        git_lab.commit("conflicting", content="master")
        subprocess_result = subprocess.run(
            ["git", "-C", str(git_lab.path), "cherry-pick", git_lab.sha("side")],
            capture_output=True,
        )
        assert subprocess_result.returncode != 0

        op = in_progress_operation(git_lab.path)
        assert op is not None
        name, cont, abort = op
        assert name == "cherry-pick"
        assert cont == "git cherry-pick --continue"
        assert abort == "git cherry-pick --abort"

    def test_detects_a_stopped_rebase(self, git_lab):
        git_lab.branch("side")
        git_lab.checkout("side")
        git_lab.commit("f", content="side")
        git_lab.checkout("master")
        git_lab.commit("f", content="master")
        git_lab.checkout("side")
        subprocess_result = subprocess.run(
            ["git", "-C", str(git_lab.path), "rebase", "master"],
            capture_output=True,
        )
        assert subprocess_result.returncode != 0

        op = in_progress_operation(git_lab.path)
        assert op is not None
        assert op[0] == "rebase"
        assert op[1] == "git rebase --continue"

    def test_a_rebase_wins_over_a_sequencer_directory(self, git_lab):
        """Marker order is load-bearing, not cosmetic: git has written a
        top-level `sequencer` directory alongside `rebase-merge` for an
        interactive rebase, and reporting that as a cherry-pick sends the user
        `git cherry-pick --abort`, which cannot end a rebase.

        The two markers are created directly rather than by driving git: the
        git in this environment keeps interactive-rebase state entirely inside
        rebase-merge, so the collision cannot be provoked here — while the
        ordering that guards against it still has to hold.
        """
        git_dir = _git_dir(git_lab.path)
        assert git_dir is not None
        (git_dir / "rebase-merge").mkdir()
        (git_dir / "sequencer").mkdir()

        op = in_progress_operation(git_lab.path)

        assert op is not None
        assert op[0] == "rebase"
        assert op[2] == "git rebase --abort"


class TestDirtyFiles:
    def test_empty_when_clean(self, git_lab):
        assert dirty_files(git_lab.path) == []

    def test_lists_modified_tracked_files(self, git_lab):
        (git_lab.path / "A.txt").write_text("changed")
        assert dirty_files(git_lab.path) == ["A.txt"]

    def test_ignores_untracked_files(self, git_lab):
        """git rebase only refuses on modified tracked files."""
        (git_lab.path / "scratch.txt").write_text("junk")
        assert dirty_files(git_lab.path) == []
