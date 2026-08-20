import shutil
from unittest.mock import MagicMock, patch

from ow.commands import cmd_prune
from ow.commands.prune import _prune_bare_repo
from ow.utils.config import Config
from ow.utils import index, paths


def _make_config(vars=None, remotes=None) -> Config:
    return Config(
        vars=vars if vars is not None else {"http_port": 8069, "db_host": "localhost", "db_port": 5432},
        remotes=remotes or {},
    )


# ---------------------------------------------------------------------------
# cmd_prune
# ---------------------------------------------------------------------------

def test_cmd_prune_no_bare_repos(tmp_path, capsys, xdg):
    config = _make_config()
    cmd_prune(config)
    captured = capsys.readouterr()
    assert "No bare repos found" in captured.out


def test_cmd_prune_cleans_repos(tmp_path, capsys, xdg):
    config = _make_config()
    bare_dir = paths.repos_dir()
    bare_dir.mkdir(parents=True)
    (bare_dir / "community.git").mkdir()
    (bare_dir / "enterprise.git").mkdir()

    with patch("ow.commands.prune._run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        cmd_prune(config)

    assert mock_run.call_count >= 6
    calls = mock_run.call_args_list
    all_args = " ".join(str(c) for c in calls)
    assert "community" in all_args
    assert "enterprise" in all_args
    prune_calls = [c for c in calls if c[0][0][3:5] == ["worktree", "prune"]]
    assert len(prune_calls) == 2


def _make_indexed_workspace(tmp_path, name: str):
    """A workspace directory with a .ow/config.toml marker, remembered in the index."""
    ws = tmp_path / "workspaces" / name
    (ws / ".ow").mkdir(parents=True)
    (ws / ".ow" / "config.toml").write_text("")
    index.remember(ws)
    return ws


def test_cmd_prune_drops_dead_index_entries(tmp_path, capsys, xdg):
    config = _make_config()

    live = _make_indexed_workspace(tmp_path, "live")
    dead1 = _make_indexed_workspace(tmp_path, "dead1")
    dead2 = _make_indexed_workspace(tmp_path, "dead2")

    # The workspaces vanish (e.g. the directory was deleted by hand) after
    # being remembered, but before the index is ever re-read — so the raw
    # file still holds all three entries when cmd_prune runs.
    shutil.rmtree(dead1)
    shutil.rmtree(dead2)

    cmd_prune(config)

    captured = capsys.readouterr()
    assert "Dropped 2 dead index entries" in captured.out
    assert index.known_workspaces() == [live.resolve()]


def test_cmd_prune_reports_nothing_when_index_is_clean(tmp_path, capsys, xdg):
    config = _make_config()
    _make_indexed_workspace(tmp_path, "live")

    cmd_prune(config)

    captured = capsys.readouterr()
    assert "index" not in captured.out


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

    with patch("ow.commands.prune._run", side_effect=[MagicMock(returncode=0), wt_result, branch_result, MagicMock(returncode=0)]):
        result = _prune_bare_repo(bare_repo)

    assert "main-parrot" not in result.deleted_branches
    assert "other-branch" in result.deleted_branches
