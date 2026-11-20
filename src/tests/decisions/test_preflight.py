"""Decision table tests for pre-flight refusal checks.

Pure tests with no git or filesystem access.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from ow.core.facts import RepoFacts
from ow.recipes.preflight import refusal


@pytest.fixture
def facts():
    """A healthy repo on a branch; override fields with dataclasses.replace()."""
    return RepoFacts(
        name="test",
        path=Path("/ws/test"),
        bare=Path("/bare/test.git"),
        remotes=("origin",),
        head="abc123",
        branch="18.0-voip",
        upstream="origin/18.0-voip",
        upstream_ab=(0, 0),
        base="origin/18.0",
        default_base="origin/18.0",
        explicit_base=None,
        operation=None,
        modified=(),
        untracked=(),
        problem=None,
    )


class TestRefusal:
    def test_problem_repo_refused_on_any_command(self, facts):
        msg = refusal(replace(facts, problem="prunable"), "status")
        assert msg == "worktree is broken (prunable); run `ow init`"

    def test_operation_refuses_every_mutating_command(self, facts):
        dirty = replace(facts, operation="rebase")
        for command in ("pull", "rebase", "reset", "switch", "repo rm", "rm"):
            assert refusal(dirty, command) is not None

    def test_operation_allows_status_and_fetch(self, facts):
        dirty = replace(facts, operation="rebase")
        assert refusal(dirty, "status") is None
        assert refusal(dirty, "fetch") is None

    @pytest.mark.parametrize(
        "operation, command",
        [
            ("rebase", "rebase --abort"),
            ("cherry-pick", "cherry-pick --abort"),
            ("merge", "merge --abort"),
            ("revert", "revert --abort"),
            ("bisect", "bisect reset"),
        ],
    )
    def test_operation_hint_names_the_real_abort_command(self, facts, operation, command):
        msg = refusal(replace(facts, operation=operation), "pull")
        assert msg == f"{operation} in progress; finish it or run `git -C /ws/test {command}`"

    def test_operation_hint_prints_workspace_relative_with_root(self, facts):
        msg = refusal(replace(facts, operation="bisect"), "pull", root=Path("/ws"))
        assert msg == "bisect in progress; finish it or run `git -C test bisect reset`"

    def test_modified_files_refuse_rebase_without_autostash(self, facts):
        dirty = replace(facts, modified=("file.py",))
        assert refusal(dirty, "rebase") == "modified files; commit, stash, or pass --autostash"

    def test_modified_files_allowed_with_autostash(self, facts):
        dirty = replace(facts, modified=("file.py",))
        assert refusal(dirty, "rebase", autostash=True) is None

    @pytest.mark.parametrize("command", ["pull", "rebase", "reset"])
    def test_detached_without_base_refused(self, facts, command):
        detached = replace(facts, branch=None, base=None)
        assert refusal(detached, command) == "no default base; run `ow init`"

    @pytest.mark.parametrize("command", ["pull", "rebase", "reset"])
    def test_detached_with_base_allowed(self, facts, command):
        detached = replace(facts, branch=None, base="origin/18.0")
        assert refusal(detached, command) is None

    def test_healthy_branch_allowed_everywhere(self, facts):
        for command in ("status", "fetch", "pull", "rebase", "reset", "switch"):
            assert refusal(facts, command) is None
