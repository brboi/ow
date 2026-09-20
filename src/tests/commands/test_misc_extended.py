from unittest.mock import patch

from ow.utils.status import _gather_one_repo
from ow.utils.config import BranchSpec


# ---------------------------------------------------------------------------
# status — github_link for attached worktree, no attached branch link
# ---------------------------------------------------------------------------

class TestStatusExtended:
    def test_gather_attached_no_github_no_link(self, tmp_path):
        worktree = tmp_path / "community"
        worktree.mkdir()
        bare_repo = tmp_path / "community.git"
        bare_repo.mkdir()
        spec = BranchSpec("origin/master", "feature")
        resolved = BranchSpec("origin/master", "feature")

        with (
            patch("ow.utils.status.get_upstream", return_value=None),
            patch("ow.utils.status.get_rev_list_count", return_value=(0, 0)),
            patch("ow.utils.status.get_remote_url", return_value="https://gitlab.example.com/odoo.git"),
            patch("ow.utils.status.get_worktree_branch", return_value="feature"),
        ):
            result = _gather_one_repo(
                "community", spec, resolved, worktree, bare_repo, False,
            )

        assert result.github_url is None

    def test_gather_attached_branch_shows_tree_link(self, tmp_path):
        worktree = tmp_path / "community"
        worktree.mkdir()
        bare_repo = tmp_path / "community.git"
        bare_repo.mkdir()
        spec = BranchSpec("origin/master", "feature")
        resolved = BranchSpec("origin/master", "feature")

        with (
            patch("ow.utils.status.get_upstream", return_value=None),
            patch("ow.utils.status.get_rev_list_count", return_value=(0, 0)),
            patch("ow.utils.status.get_remote_url", return_value="git@github.com:odoo/odoo.git"),
            patch("ow.utils.status.get_worktree_branch", return_value="feature"),
        ):
            result = _gather_one_repo(
                "community", spec, resolved, worktree, bare_repo, False,
            )

        assert result.github_url is not None
        assert "tree/feature" in result.github_url