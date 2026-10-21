"""Decision table tests for pre-flight refusal checks.

Pure tests with no git or filesystem access.
"""

from pathlib import Path

import pytest

from ow.core.facts import RepoFacts
from ow.recipes.preflight import MUTATING, refusal


class TestRefusal:
    """Test cases for refusal()."""

    def test_problem_repo_always_refused(self):
        """A problem repo is refused on any command."""
        facts = RepoFacts(
            name="test",
            path=Path("/ws/test"),
            bare=Path("/data/repos/test.git"),
            remotes=("origin",),
            head=None,
            branch="main",
            upstream=None,
            upstream_ab=None,
            base="origin/18.0",
            default_base="origin/18.0",
            explicit_base=None,
            operation=None,
            modified=(),
            untracked=(),
            problem="prunable",
        )

        for command in ("status", "fetch", "pull", "rebase", "reset", "switch"):
            msg = refusal(facts, command)
            assert msg is not None
            assert "worktree is broken" in msg
            assert "run `ow init`" in msg

    def test_operation_in_progress_on_mutating_command(self):
        """Operation in progress refuses mutating commands."""
        facts = RepoFacts(
            name="test",
            path=Path("/ws/test"),
            bare=Path("/data/repos/test.git"),
            remotes=("origin",),
            head=None,
            branch="main",
            upstream=None,
            upstream_ab=None,
            base="origin/18.0",
            default_base="origin/18.0",
            explicit_base=None,
            operation="rebase",
            modified=(),
            untracked=(),
            problem=None,
        )

        # Mutating commands should be refused
        for command in ("pull", "rebase", "reset", "switch", "repo rm", "rm"):
            msg = refusal(facts, command)
            assert msg is not None
            assert "rebase in progress" in msg

    def test_operation_in_progress_not_refused_for_status_fetch(self):
        """Operation in progress does not refuse status or fetch."""
        facts = RepoFacts(
            name="test",
            path=Path("/ws/test"),
            bare=Path("/data/repos/test.git"),
            remotes=("origin",),
            head=None,
            branch="main",
            upstream=None,
            upstream_ab=None,
            base="origin/18.0",
            default_base="origin/18.0",
            explicit_base=None,
            operation="rebase",
            modified=(),
            untracked=(),
            problem=None,
        )

        # Non-mutating commands should not be refused
        assert refusal(facts, "status") is None
        assert refusal(facts, "fetch") is None

    def test_modified_files_on_rebase_without_autostash(self):
        """Modified tracked files on rebase without autostash."""
        facts = RepoFacts(
            name="test",
            path=Path("/ws/test"),
            bare=Path("/data/repos/test.git"),
            remotes=("origin",),
            head=None,
            branch="main",
            upstream=None,
            upstream_ab=None,
            base="origin/18.0",
            default_base="origin/18.0",
            explicit_base=None,
            operation=None,
            modified=("src/file.py", "README.md"),
            untracked=(),
            problem=None,
        )

        # Without autostash, should be refused
        msg = refusal(facts, "rebase", autostash=False)
        assert msg is not None
        assert "modified files" in msg

        # With autostash, should not be refused
        msg = refusal(facts, "rebase", autostash=True)
        assert msg is None

    def test_missing_default_base_on_switch_detach(self):
        """Missing default base on switch --detach."""
        facts = RepoFacts(
            name="test",
            path=Path("/ws/test"),
            bare=Path("/data/repos/test.git"),
            remotes=("origin",),
            head="abc123",
            branch=None,
            upstream=None,
            upstream_ab=None,
            base=None,
            default_base=None,
            explicit_base=None,
            operation=None,
            modified=(),
            untracked=(),
            problem=None,
        )

        msg = refusal(facts, "switch --detach")
        assert msg is not None
        assert "no default base" in msg

    def test_missing_default_base_on_pull_when_detached(self):
        """Missing default base on pull when detached."""
        facts = RepoFacts(
            name="test",
            path=Path("/ws/test"),
            bare=Path("/data/repos/test.git"),
            remotes=("origin",),
            head="abc123",
            branch=None,
            upstream=None,
            upstream_ab=None,
            base=None,
            default_base=None,
            explicit_base=None,
            operation=None,
            modified=(),
            untracked=(),
            problem=None,
        )

        msg = refusal(facts, "pull")
        assert msg is not None
        assert "no default base" in msg

    def test_missing_default_base_on_rebase_when_detached(self):
        """Missing default base on rebase when detached."""
        facts = RepoFacts(
            name="test",
            path=Path("/ws/test"),
            bare=Path("/data/repos/test.git"),
            remotes=("origin",),
            head="abc123",
            branch=None,
            upstream=None,
            upstream_ab=None,
            base=None,
            default_base=None,
            explicit_base=None,
            operation=None,
            modified=(),
            untracked=(),
            problem=None,
        )

        msg = refusal(facts, "rebase")
        assert msg is not None
        assert "no default base" in msg

    def test_missing_default_base_on_reset_when_detached(self):
        """Missing default base on reset when detached."""
        facts = RepoFacts(
            name="test",
            path=Path("/ws/test"),
            bare=Path("/data/repos/test.git"),
            remotes=("origin",),
            head="abc123",
            branch=None,
            upstream=None,
            upstream_ab=None,
            base=None,
            default_base=None,
            explicit_base=None,
            operation=None,
            modified=(),
            untracked=(),
            problem=None,
        )

        msg = refusal(facts, "reset")
        assert msg is not None
        assert "no default base" in msg

    def test_default_base_exists_on_detached_no_refusal(self):
        """Default base exists when detached: no refusal."""
        facts = RepoFacts(
            name="test",
            path=Path("/ws/test"),
            bare=Path("/data/repos/test.git"),
            remotes=("origin",),
            head="abc123",
            branch=None,
            upstream=None,
            upstream_ab=None,
            base="origin/18.0",
            default_base="origin/18.0",
            explicit_base=None,
            operation=None,
            modified=(),
            untracked=(),
            problem=None,
        )

        # Should not be refused even when detached
        assert refusal(facts, "switch --detach") is None
        assert refusal(facts, "pull") is None
        assert refusal(facts, "rebase") is None
        assert refusal(facts, "reset") is None

    def test_branch_with_base_no_refusal(self):
        """On a branch with a base: no refusal for most commands."""
        facts = RepoFacts(
            name="test",
            path=Path("/ws/test"),
            bare=Path("/data/repos/test.git"),
            remotes=("origin",),
            head=None,
            branch="main",
            upstream=None,
            upstream_ab=None,
            base="origin/18.0",
            default_base="origin/18.0",
            explicit_base=None,
            operation=None,
            modified=(),
            untracked=(),
            problem=None,
        )

        assert refusal(facts, "status") is None
        assert refusal(facts, "fetch") is None
        assert refusal(facts, "pull") is None
        assert refusal(facts, "rebase") is None
        assert refusal(facts, "reset") is None
        assert refusal(facts, "switch") is None

    def test_mutating_constant(self):
        """MUTATING constant contains the right commands."""
        assert "status" not in MUTATING
        assert "fetch" not in MUTATING
        assert "pull" in MUTATING
        assert "rebase" in MUTATING
        assert "reset" in MUTATING
        assert "switch" in MUTATING
        assert "switch --detach" in MUTATING
        assert "repo rm" in MUTATING
        assert "rm" in MUTATING

    def test_different_operations(self):
        """Different operations produce appropriate messages."""
        for operation, name in [
            ("rebase", "rebase"),
            ("cherry-pick", "cherry-pick"),
            ("merge", "merge"),
            ("revert", "revert"),
            ("bisect", "bisect"),
        ]:
            facts = RepoFacts(
                name="test",
                path=Path("/ws/test"),
                bare=Path("/data/repos/test.git"),
                remotes=("origin",),
                head=None,
                branch="main",
                upstream=None,
                upstream_ab=None,
                base="origin/18.0",
                default_base="origin/18.0",
                explicit_base=None,
                operation=operation,
                modified=(),
                untracked=(),
                problem=None,
            )

            msg = refusal(facts, "pull")
            assert msg is not None
            assert f"{name} in progress" in msg
