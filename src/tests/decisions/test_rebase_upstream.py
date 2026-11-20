"""The rebase upstream step as a pure decision table.

`plan_rebase` takes the reads' results as `RebaseInputs`, so every row of the
guard — plus the base step, `--base` and `--autostash` — can be expressed
without git or a filesystem.
"""

from __future__ import annotations

from pathlib import Path

from ow.core.facts import RepoFacts
from ow.recipes.rebase import AUTOSTASH_MESSAGE, RebaseInputs, plan_rebase

BRANCH = "18.0-voip-boi"
UPSTREAM = f"origin/{BRANCH}"
BASE = "origin/18.0"
WORKTREE = Path("/ws/community")
TAKEN = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"


def _facts(**overrides) -> RepoFacts:
    """A branch with an upstream and a base, on a clean worktree, unless overridden."""
    values: dict = dict(
        name="community",
        path=WORKTREE,
        bare=Path("/data/repos/community.git"),
        remotes=("origin",),
        head="c" * 40,
        branch=BRANCH,
        upstream=UPSTREAM,
        upstream_ab=(0, 0),
        base=BASE,
        default_base=BASE,
        explicit_base=BASE,
        operation=None,
        modified=(),
        untracked=(),
        problem=None,
    )
    values.update(overrides)
    return RepoFacts(**values)


def _plan(facts=None, inputs=None, *, new_base=None, autostash=False):
    """The plan for hand-built facts and inputs; the base is already taken in by default."""
    return plan_rebase(
        facts if facts is not None else _facts(),
        inputs if inputs is not None else RebaseInputs(upstream_in_branch_past=True),
        new_base=new_base,
        autostash=autostash,
    )


def _argvs(plan):
    return [step.argv for step in plan.steps]


class TestUpstreamStep:
    """The three questions of the guard, in order."""

    def test_upstream_already_taken_in_runs_nothing(self):
        """An ancestor of a past branch value is nothing new to take in."""
        plan = _plan(
            inputs=RebaseInputs(
                upstream_in_branch_past=True,
                last_taken=TAKEN,
                last_taken_fast_forward=True,
                new_upstream_commits=3,
            )
        )
        assert plan.steps == ()
        assert plan.refusal is None

    def test_upstream_equal_to_the_base_has_no_upstream_step(self):
        """An @{u} that is the base does not count, whatever the reads said."""
        plan = _plan(
            facts=_facts(upstream=BASE),
            inputs=RebaseInputs(
                last_taken=TAKEN,
                last_taken_fast_forward=True,
                new_upstream_commits=3,
            ),
        )
        assert plan.steps == ()

    def test_fast_forward_with_only_patch_equivalent_commits_runs_nothing(self):
        """The branch already carries every new upstream patch."""
        plan = _plan(
            inputs=RebaseInputs(
                last_taken=TAKEN, last_taken_fast_forward=True, new_upstream_commits=0
            )
        )
        assert plan.steps == ()

    def test_fast_forward_with_new_commits_fast_forwards_the_range(self):
        """The colleague's commits land as they are, named by the SHA they follow."""
        plan = _plan(
            inputs=RebaseInputs(
                last_taken=TAKEN, last_taken_fast_forward=True, new_upstream_commits=2
            )
        )
        assert _argvs(plan) == [
            (
                "git",
                "-C",
                str(WORKTREE),
                "cherry-pick",
                "--ff",
                "--empty=drop",
                f"{TAKEN}..{UPSTREAM}",
            )
        ]
        step = plan.steps[0]
        assert step.comment == f"{TAKEN[:10]} = {UPSTREAM} as this branch last took it in"

    def test_a_force_pushed_upstream_falls_back_to_fork_point(self):
        """The last taken-in value is not an ancestor of the upstream: a rewrite."""
        plan = _plan(
            inputs=RebaseInputs(
                last_taken=TAKEN, last_taken_fast_forward=False, new_upstream_commits=2
            )
        )
        assert _argvs(plan) == [
            ("git", "-C", str(WORKTREE), "rebase", "--fork-point", UPSTREAM)
        ]

    def test_no_usable_reflog_falls_back_to_fork_point(self):
        """No upstream reflog entry has all its patches in HEAD."""
        plan = _plan(inputs=RebaseInputs(last_taken=None))
        assert _argvs(plan) == [
            ("git", "-C", str(WORKTREE), "rebase", "--fork-point", UPSTREAM)
        ]

    def test_a_merge_in_the_range_falls_back_to_fork_point(self):
        """A cherry-pick cannot replay a merge, so the fork-point path runs instead."""
        plan = _plan(
            inputs=RebaseInputs(
                last_taken=TAKEN,
                last_taken_fast_forward=True,
                new_upstream_commits=2,
                has_merge_in_range=True,
            )
        )
        assert _argvs(plan) == [
            ("git", "-C", str(WORKTREE), "rebase", "--fork-point", UPSTREAM)
        ]


class TestBaseStep:
    """`rebase <base>`, unless the branch holds the base, or the upstream lands it."""

    def test_runs_when_the_base_is_not_an_ancestor(self):
        plan = _plan(
            inputs=RebaseInputs(upstream_in_branch_past=True, base_is_ancestor=False)
        )
        assert _argvs(plan) == [("git", "-C", str(WORKTREE), "rebase", BASE)]

    def test_is_skipped_when_the_base_is_an_ancestor(self):
        plan = _plan(inputs=RebaseInputs(upstream_in_branch_past=True, base_is_ancestor=True))
        assert plan.steps == ()

    def test_is_skipped_when_the_upstream_step_lands_the_base(self):
        """The base is an ancestor of the upstream, so the step onto it brings it in."""
        plan = _plan(
            inputs=RebaseInputs(
                base_is_ancestor=False,
                base_in_upstream=True,
                last_taken=TAKEN,
                last_taken_fast_forward=True,
                new_upstream_commits=2,
            )
        )
        assert _argvs(plan) == [
            (
                "git",
                "-C",
                str(WORKTREE),
                "cherry-pick",
                "--ff",
                "--empty=drop",
                f"{TAKEN}..{UPSTREAM}",
            )
        ]

    def test_is_kept_when_no_upstream_step_lands_the_base(self):
        """An upstream already taken in leaves the base its own step."""
        plan = _plan(
            inputs=RebaseInputs(
                upstream_in_branch_past=True,
                base_is_ancestor=False,
                base_in_upstream=True,
            )
        )
        assert _argvs(plan) == [("git", "-C", str(WORKTREE), "rebase", BASE)]

    def test_a_detached_head_runs_only_the_base_step(self):
        """Detached: no upstream step, the base step against the default base."""
        plan = _plan(
            facts=_facts(branch=None, upstream=None, explicit_base=None, head=None),
            inputs=RebaseInputs(base_is_ancestor=False),
        )
        assert _argvs(plan) == [("git", "-C", str(WORKTREE), "rebase", BASE)]


class TestBaseReplacement:
    """`--base <new>`: `rebase --onto <new> <old>`, then the recorded new base."""

    def test_rebases_onto_the_new_base_and_records_it(self):
        plan = _plan(new_base="origin/18.1")
        assert _argvs(plan) == [
            ("git", "-C", str(WORKTREE), "rebase", "--onto", "origin/18.1", BASE),
            ("git", "-C", str(WORKTREE), "config", f"branch.{BRANCH}.ow-base", "origin/18.1"),
        ]

    def test_a_new_base_equal_to_the_recorded_one_records_nothing(self):
        """The re-run after a successful `--base` falls back to the plain base step."""
        plan = _plan(new_base=BASE)
        assert plan.steps == ()

    def test_a_new_base_already_in_head_only_records_it(self):
        """A re-run must not replay the new base's own history onto itself."""
        plan = _plan(
            inputs=RebaseInputs(
                upstream_in_branch_past=True, new_base_is_ancestor=True
            ),
            new_base="origin/18.1",
        )
        assert _argvs(plan) == [
            ("git", "-C", str(WORKTREE), "config", f"branch.{BRANCH}.ow-base", "origin/18.1"),
        ]

    def test_a_detached_head_rebases_but_records_no_base(self):
        plan = _plan(
            facts=_facts(branch=None, upstream=None, explicit_base=None, head=None),
            new_base="origin/18.1",
        )
        assert _argvs(plan) == [
            ("git", "-C", str(WORKTREE), "rebase", "--onto", "origin/18.1", BASE)
        ]


class TestAutostash:
    """`--autostash` wraps the steps in a stash push and pop, both shown."""

    def _cherry_pick_inputs(self):
        return RebaseInputs(
            last_taken=TAKEN, last_taken_fast_forward=True, new_upstream_commits=1
        )

    def test_wraps_the_steps_when_a_tracked_file_is_modified(self):
        plan = _plan(
            facts=_facts(modified=("odoo-bin",)),
            inputs=self._cherry_pick_inputs(),
            autostash=True,
        )
        assert _argvs(plan) == [
            ("git", "-C", str(WORKTREE), "stash", "push", "-m", AUTOSTASH_MESSAGE),
            (
                "git",
                "-C",
                str(WORKTREE),
                "cherry-pick",
                "--ff",
                "--empty=drop",
                f"{TAKEN}..{UPSTREAM}",
            ),
            ("git", "-C", str(WORKTREE), "stash", "pop"),
        ]
        # The stash is not a branch move: it must not trigger the post-batch refresh.
        assert plan.steps[0].mutating is False
        assert plan.steps[-1].mutating is False

    def test_adds_nothing_when_no_step_would_run(self):
        plan = _plan(
            facts=_facts(modified=("odoo-bin",)),
            inputs=RebaseInputs(upstream_in_branch_past=True),
            autostash=True,
        )
        assert plan.steps == ()

    def test_adds_nothing_without_a_modification(self):
        plan = _plan(inputs=self._cherry_pick_inputs(), autostash=True)
        assert _argvs(plan) == [
            (
                "git",
                "-C",
                str(WORKTREE),
                "cherry-pick",
                "--ff",
                "--empty=drop",
                f"{TAKEN}..{UPSTREAM}",
            )
        ]
