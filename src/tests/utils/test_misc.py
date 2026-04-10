from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ow.utils.display import counts, osc8
from ow.commands.prune import _PruneResult, _prune_bare_repo


class TestDisplayHelpers:

    def test_counts_nothing(self):
        result = counts(0, 0)
        assert "0" in result or result in [""]

    def test_counts_behind_only(self):
        result = counts(3, 0)
        assert "3" in result

    def test_counts_ahead_only(self):
        result = counts(0, 5)
        assert "5" in result

    def test_counts_both(self):
        result = counts(2, 3)
        assert "2" in result
        assert "3" in result

    def test_osc8(self):
        result = osc8("https://example.com", "link text")
        assert "]8;;" in result
        assert "link text" in result


class TestPruneBareRepoExtended:

    def test_prune_bare_repo_no_worktrees(self, tmp_path):
        bare_repo = tmp_path / "community.git"
        bare_repo.mkdir()
        with patch("ow.commands.prune.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = _prune_bare_repo(bare_repo)
        assert result.deleted_branches == []
        assert len(result.deleted_branches) == 0

    def test_prune_bare_repo_worktree_branches(self, tmp_path):
        bare_repo = tmp_path / "community.git"
        bare_repo.mkdir()
        wt_result = MagicMock(returncode=0)
        wt_result.stdout = "worktree /path/to/ws/community\nHEAD abc123\nbranch refs/heads/main-parrot\n"
        branch_result = MagicMock(returncode=0)
        branch_result.stdout = "+ main-parrot\n  old-branch\n"
        prune_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("ow.commands.prune.subprocess.run") as mock_run:
            mock_run.side_effect = [MagicMock(returncode=0), wt_result, branch_result, prune_result]
            result = _prune_bare_repo(bare_repo)
        assert "main-parrot" not in result.deleted_branches
        assert "old-branch" in result.deleted_branches
