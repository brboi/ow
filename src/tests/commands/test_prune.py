from pathlib import Path
from unittest.mock import MagicMock, patch

from ow.commands import cmd_prune
from ow.commands.prune import _prune_bare_repo
from ow.utils.config import Config


def _make_config(root_dir=None, vars=None, remotes=None) -> Config:
    return Config(
        vars=vars if vars is not None else {"http_port": 8069, "db_host": "localhost", "db_port": 5432},
        remotes=remotes or {},
        root_dir=root_dir or Path("/root"),
    )


# ---------------------------------------------------------------------------
# cmd_prune
# ---------------------------------------------------------------------------

def test_cmd_prune_no_bare_repos(tmp_path, capsys):
    config = _make_config(root_dir=tmp_path)
    cmd_prune(config)
    captured = capsys.readouterr()
    assert "No bare repos found" in captured.out


def test_cmd_prune_cleans_repos(tmp_path, capsys):
    config = _make_config(root_dir=tmp_path)
    bare_dir = tmp_path / ".bare-git-repos"
    bare_dir.mkdir()
    (bare_dir / "community.git").mkdir()
    (bare_dir / "enterprise.git").mkdir()

    with patch("ow.commands.prune.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        cmd_prune(config)

    assert mock_run.call_count >= 6
    calls = mock_run.call_args_list
    all_args = " ".join(str(c) for c in calls)
    assert "community" in all_args
    assert "enterprise" in all_args
    prune_calls = [c for c in calls if c[0][0][3:5] == ["worktree", "prune"]]
    assert len(prune_calls) == 2


# ---------------------------------------------------------------------------
# _prune_bare_repo
# ---------------------------------------------------------------------------

def test_prune_bare_repo_strips_plus_prefix(tmp_path):
    """Branch names with + prefix (worktree branches) are correctly parsed."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()

    wt_result = MagicMock(returncode=0)
    wt_result.stdout = "worktree /path/to/ws/community\nHEAD abc123\nbranch refs/heads/main-parrot\n"

    branch_result = MagicMock(returncode=0)
    branch_result.stdout = "+ main-parrot\n  other-branch\n"

    with patch("ow.commands.prune.subprocess.run", side_effect=[MagicMock(returncode=0), wt_result, branch_result, MagicMock(returncode=0)]):
        result = _prune_bare_repo(bare_repo)

    assert "main-parrot" not in result.deleted_branches
    assert "other-branch" in result.deleted_branches
