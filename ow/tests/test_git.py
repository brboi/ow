from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from ow.config import BranchSpec, RemoteConfig
from ow.git import (
    create_worktree,
    ensure_bare_repo,
    ensure_ref,
    get_rev_list_count,
    get_upstream,
    get_worktree_head,
    remove_worktree,
    rebase_worktree,
    resolve_spec,
    resolve_spec_local,
    worktree_exists,
)


# ---------------------------------------------------------------------------
# ensure_bare_repo
# ---------------------------------------------------------------------------

def test_ensure_bare_repo_clones_when_missing(tmp_path):
    bare_repos_dir = tmp_path / "bare-repos"
    bare_repos_dir.mkdir()
    # bare_repo doesn't exist yet

    remotes = {"origin": RemoteConfig(url="git@github.com:odoo/odoo.git")}

    with patch("ow.git.subprocess.run") as mock_run:
        ensure_bare_repo("community", remotes, bare_repos_dir)

    mock_run.assert_called_once_with(
        ["git", "clone", "--bare", "--filter=blob:none", "--single-branch",
         "git@github.com:odoo/odoo.git", str(bare_repos_dir / "community.git")],
        check=True,
    )


def test_ensure_bare_repo_skips_clone_when_exists(tmp_path):
    bare_repos_dir = tmp_path / "bare-repos"
    bare_repo = bare_repos_dir / "community.git"
    bare_repo.mkdir(parents=True)

    remotes = {"origin": RemoteConfig(url="git@github.com:odoo/odoo.git")}

    with patch("ow.git.subprocess.run") as mock_run:
        ensure_bare_repo("community", remotes, bare_repos_dir)

    mock_run.assert_not_called()


def test_ensure_bare_repo_configures_extra_remotes(tmp_path):
    bare_repos_dir = tmp_path / "bare-repos"
    bare_repo = bare_repos_dir / "community.git"
    bare_repo.mkdir(parents=True)

    remotes = {
        "origin": RemoteConfig(url="git@github.com:odoo/odoo.git"),
        "dev": RemoteConfig(
            url="git@github.com:odoo-dev/odoo.git",
            pushurl="git@github.com:odoo-dev/odoo.git",
            fetch="+refs/heads/*:refs/remotes/dev/*",
        ),
    }

    with patch("ow.git.subprocess.run") as mock_run:
        ensure_bare_repo("community", remotes, bare_repos_dir)

    calls = mock_run.call_args_list
    assert len(calls) == 3
    assert calls[0] == call(
        ["git", "-C", str(bare_repo), "config", "remote.dev.url", "git@github.com:odoo-dev/odoo.git"],
        check=True,
    )
    assert calls[1] == call(
        ["git", "-C", str(bare_repo), "config", "remote.dev.pushurl", "git@github.com:odoo-dev/odoo.git"],
        check=True,
    )
    assert calls[2] == call(
        ["git", "-C", str(bare_repo), "config", "remote.dev.fetch", "+refs/heads/*:refs/remotes/dev/*"],
        check=True,
    )


# ---------------------------------------------------------------------------
# ensure_ref
# ---------------------------------------------------------------------------

def test_ensure_ref_fetches_when_missing(tmp_path):
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()

    mock_check = MagicMock(returncode=1)

    with patch("ow.git.subprocess.run", side_effect=[mock_check, MagicMock()]) as mock_run:
        ensure_ref(bare_repo, "origin", "master")

    assert mock_run.call_count == 2
    assert mock_run.call_args_list[1] == call(
        ["git", "-C", str(bare_repo), "fetch", "origin", "master:refs/remotes/origin/master"],
        check=True,
    )


def test_ensure_ref_skips_fetch_when_exists(tmp_path):
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()

    mock_check = MagicMock(returncode=0)

    with patch("ow.git.subprocess.run", return_value=mock_check) as mock_run:
        ensure_ref(bare_repo, "origin", "master")

    assert mock_run.call_count == 1  # only the rev-parse check


# ---------------------------------------------------------------------------
# worktree_exists
# ---------------------------------------------------------------------------

def test_worktree_exists_true(tmp_path):
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    worktree_path = tmp_path / "workspaces" / "test" / "community"
    worktree_path.mkdir(parents=True)

    mock_result = MagicMock()
    mock_result.stdout = f"{worktree_path} abc1234 [main]\n"

    with patch("ow.git.subprocess.run", return_value=mock_result):
        assert worktree_exists(bare_repo, worktree_path) is True


def test_worktree_exists_false(tmp_path):
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    worktree_path = tmp_path / "workspaces" / "test" / "community"
    worktree_path.mkdir(parents=True)

    mock_result = MagicMock()
    mock_result.stdout = "/other/path abc1234 [main]\n"

    with patch("ow.git.subprocess.run", return_value=mock_result):
        assert worktree_exists(bare_repo, worktree_path) is False


def test_worktree_exists_false_when_dir_missing_but_in_git_output(tmp_path):
    """Prunable worktree: git still lists the path but directory no longer exists."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    worktree_path = tmp_path / "workspaces" / "test" / "community"
    # worktree_path is NOT created on disk

    mock_result = MagicMock()
    mock_result.stdout = f"{worktree_path} abc1234 [main]\n"

    with patch("ow.git.subprocess.run", return_value=mock_result):
        assert worktree_exists(bare_repo, worktree_path) is False


# ---------------------------------------------------------------------------
# create_worktree
# ---------------------------------------------------------------------------

def test_create_worktree_detached(tmp_path):
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    worktree_path = Path("/fake/workspaces/test/community")
    spec = BranchSpec("origin/master", None)

    with patch("ow.git.subprocess.run") as mock_run:
        create_worktree(bare_repo, worktree_path, spec)

    mock_run.assert_called_once_with(
        ["git", "-C", str(bare_repo), "worktree", "add", "--detach", str(worktree_path), "origin/master"],
        check=True,
    )


def test_create_worktree_attached_new_branch(tmp_path):
    """Branch doesn't exist yet — uses -b to create it."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    worktree_path = Path("/fake/workspaces/test/community")
    spec = BranchSpec("origin/master", "master-feature")

    branch_missing = MagicMock(returncode=1)

    with patch("ow.git.subprocess.run", side_effect=[branch_missing, MagicMock()]) as mock_run:
        create_worktree(bare_repo, worktree_path, spec)

    assert mock_run.call_args_list[1] == call(
        ["git", "-C", str(bare_repo), "worktree", "add", "-b", "master-feature",
         str(worktree_path), "origin/master"],
        check=True,
    )


def test_create_worktree_attached_existing_branch(tmp_path):
    """Branch already exists (prunable worktree re-created) — omits -b."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    worktree_path = Path("/fake/workspaces/test/community")
    spec = BranchSpec("origin/master", "master-feature")

    branch_exists = MagicMock(returncode=0)

    with patch("ow.git.subprocess.run", side_effect=[branch_exists, MagicMock()]) as mock_run:
        create_worktree(bare_repo, worktree_path, spec)

    assert mock_run.call_args_list[1] == call(
        ["git", "-C", str(bare_repo), "worktree", "add", str(worktree_path), "master-feature"],
        check=True,
    )


# ---------------------------------------------------------------------------
# remove_worktree
# ---------------------------------------------------------------------------

def test_remove_worktree_detached(tmp_path):
    bare_repo = tmp_path / "community.git"
    worktree_path = Path("/fake/workspaces/test/community")

    with patch("ow.git.subprocess.run") as mock_run:
        remove_worktree(bare_repo, worktree_path, None)

    mock_run.assert_called_once_with(
        ["git", "-C", str(bare_repo), "worktree", "remove", "--force", str(worktree_path)],
        check=True,
    )


def test_remove_worktree_attached(tmp_path):
    bare_repo = tmp_path / "community.git"
    worktree_path = Path("/fake/workspaces/test/community")

    with patch("ow.git.subprocess.run") as mock_run:
        remove_worktree(bare_repo, worktree_path, "master-feature")

    calls = mock_run.call_args_list
    assert len(calls) == 2
    assert calls[0] == call(
        ["git", "-C", str(bare_repo), "worktree", "remove", "--force", str(worktree_path)],
        check=True,
    )
    assert calls[1] == call(
        ["git", "-C", str(bare_repo), "branch", "-D", "master-feature"],
        check=True,
    )


# ---------------------------------------------------------------------------
# rebase_worktree
# ---------------------------------------------------------------------------

def test_rebase_worktree_detached(tmp_path):
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    worktree_path = Path("/fake/workspaces/test/community")
    spec = BranchSpec("origin/master", None)

    with patch("ow.git.subprocess.run") as mock_run:
        rebase_worktree(bare_repo, worktree_path, spec)

    calls = mock_run.call_args_list
    assert len(calls) == 2
    assert calls[0] == call(
        ["git", "-C", str(bare_repo), "fetch", "origin", "master:refs/remotes/origin/master"],
        check=True,
    )
    assert calls[1] == call(
        ["git", "-C", str(worktree_path), "switch", "--detach", "origin/master"],
        check=True,
    )


def test_rebase_worktree_attached(tmp_path):
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    worktree_path = Path("/fake/workspaces/test/community")
    spec = BranchSpec("origin/master", "master-feature")

    with patch("ow.git.subprocess.run") as mock_run:
        rebase_worktree(bare_repo, worktree_path, spec)

    calls = mock_run.call_args_list
    assert len(calls) == 2
    assert calls[0] == call(
        ["git", "-C", str(bare_repo), "fetch", "origin", "master:refs/remotes/origin/master"],
        check=True,
    )
    assert calls[1] == call(
        ["git", "-C", str(worktree_path), "rebase", "origin/master"],
        check=True,
    )


def test_rebase_worktree_non_origin(tmp_path):
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    worktree_path = Path("/fake/workspaces/test/community")
    spec = BranchSpec("dev/master-phoenix", "fix")

    with patch("ow.git.subprocess.run") as mock_run:
        rebase_worktree(bare_repo, worktree_path, spec)

    calls = mock_run.call_args_list
    assert calls[0] == call(
        ["git", "-C", str(bare_repo), "fetch", "dev", "master-phoenix:refs/remotes/dev/master-phoenix"],
        check=True,
    )
    assert calls[1] == call(
        ["git", "-C", str(worktree_path), "rebase", "dev/master-phoenix"],
        check=True,
    )


# ---------------------------------------------------------------------------
# get_rev_list_count
# ---------------------------------------------------------------------------

def test_get_rev_list_count(tmp_path):
    mock_result = MagicMock()
    mock_result.stdout = "3\t5\n"

    with patch("ow.git.subprocess.run", return_value=mock_result):
        ahead, behind = get_rev_list_count(tmp_path, "HEAD", "origin/master")

    assert ahead == 3
    assert behind == 5


def test_get_rev_list_count_zero(tmp_path):
    mock_result = MagicMock()
    mock_result.stdout = "0\t0\n"

    with patch("ow.git.subprocess.run", return_value=mock_result):
        ahead, behind = get_rev_list_count(tmp_path, "HEAD", "origin/master")

    assert ahead == 0
    assert behind == 0


# ---------------------------------------------------------------------------
# get_worktree_head
# ---------------------------------------------------------------------------

def test_get_worktree_head(tmp_path):
    full_hash = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
    mock_result = MagicMock()
    mock_result.stdout = full_hash + "\n"

    with patch("ow.git.subprocess.run", return_value=mock_result):
        short, full = get_worktree_head(tmp_path)

    assert short == "a1b2c3d"
    assert full == full_hash


# ---------------------------------------------------------------------------
# get_upstream
# ---------------------------------------------------------------------------

def test_get_upstream_returns_ref(tmp_path):
    mock_result = MagicMock(returncode=0)
    mock_result.stdout = "dev/master-canary\n"

    with patch("ow.git.subprocess.run", return_value=mock_result):
        result = get_upstream(tmp_path)

    assert result == "dev/master-canary"


def test_get_upstream_returns_none_when_no_upstream(tmp_path):
    mock_result = MagicMock(returncode=128)
    mock_result.stdout = ""

    with patch("ow.git.subprocess.run", return_value=mock_result):
        result = get_upstream(tmp_path)

    assert result is None


# ---------------------------------------------------------------------------
# resolve_spec
# ---------------------------------------------------------------------------

def test_resolve_spec_branch_found_on_spec_remote(tmp_path):
    """Branch already exists as a remote ref on spec.remote — no fetch needed."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    spec = BranchSpec("origin/master", None)
    remotes = {"origin": RemoteConfig(url="git@github.com:odoo/odoo.git")}

    rev_parse_ok = MagicMock(returncode=0)

    with patch("ow.git.subprocess.run", return_value=rev_parse_ok) as mock_run:
        result = resolve_spec(bare_repo, spec, remotes)

    assert result.remote == "origin"
    assert result.branch == "master"
    assert result.local_branch is None
    # Only the rev-parse check, no fetch
    mock_run.assert_called_once_with(
        ["git", "-C", str(bare_repo), "rev-parse", "--verify", "refs/remotes/origin/master"],
        capture_output=True,
    )


def test_resolve_spec_branch_not_on_spec_remote_found_on_fallback(tmp_path):
    """Branch not on origin but found on dev fallback remote after fetch."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    spec = BranchSpec("origin/master-parrot", None)
    remotes = {
        "origin": RemoteConfig(url="git@github.com:odoo/odoo.git"),
        "dev": RemoteConfig(url="git@github.com:odoo-dev/odoo.git"),
    }

    rev_parse_fail = MagicMock(returncode=1)
    fetch_fail = MagicMock(returncode=1)
    rev_parse_fail2 = MagicMock(returncode=1)
    fetch_ok = MagicMock(returncode=0)

    with patch("ow.git.subprocess.run", side_effect=[
        rev_parse_fail,   # rev-parse origin/master-parrot → miss
        fetch_fail,       # fetch origin master-parrot → fail
        rev_parse_fail2,  # rev-parse dev/master-parrot → miss
        fetch_ok,         # fetch dev master-parrot → success
    ]) as mock_run:
        result = resolve_spec(bare_repo, spec, remotes)

    assert result.remote == "dev"
    assert result.branch == "master-parrot"
    assert mock_run.call_count == 4


def test_resolve_spec_branch_found_in_existing_local_refs(tmp_path):
    """Branch already fetched under a non-spec remote ref — no new fetch needed."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    spec = BranchSpec("origin/master-parrot", None)
    remotes = {
        "origin": RemoteConfig(url="git@github.com:odoo/odoo.git"),
        "dev": RemoteConfig(url="git@github.com:odoo-dev/odoo.git"),
    }

    rev_parse_fail = MagicMock(returncode=1)
    fetch_fail = MagicMock(returncode=1)
    rev_parse_ok = MagicMock(returncode=0)

    with patch("ow.git.subprocess.run", side_effect=[
        rev_parse_fail,   # rev-parse origin/master-parrot → miss
        fetch_fail,       # fetch origin → fail
        rev_parse_ok,     # rev-parse dev/master-parrot → hit (already fetched before)
    ]) as mock_run:
        result = resolve_spec(bare_repo, spec, remotes)

    assert result.remote == "dev"
    assert result.branch == "master-parrot"
    assert mock_run.call_count == 3


def test_resolve_spec_raises_when_branch_not_found_anywhere(tmp_path):
    """RuntimeError raised when branch not found on any remote."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    spec = BranchSpec("origin/nonexistent", None)
    remotes = {"origin": RemoteConfig(url="git@github.com:odoo/odoo.git")}

    always_fail = MagicMock(returncode=1)

    with patch("ow.git.subprocess.run", return_value=always_fail):
        with pytest.raises(RuntimeError, match="nonexistent"):
            resolve_spec(bare_repo, spec, remotes)


# ---------------------------------------------------------------------------
# resolve_spec_local
# ---------------------------------------------------------------------------

def test_resolve_spec_local_found_on_spec_remote(tmp_path):
    """Branch already in local refs on spec.remote — returns immediately."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    spec = BranchSpec("origin/master", None)
    remotes = {"origin": RemoteConfig(url="git@github.com:odoo/odoo.git")}

    rev_parse_ok = MagicMock(returncode=0)

    with patch("ow.git.subprocess.run", return_value=rev_parse_ok) as mock_run:
        result = resolve_spec_local(bare_repo, spec, remotes)

    assert result.remote == "origin"
    assert result.branch == "master"
    assert result.local_branch is None
    mock_run.assert_called_once_with(
        ["git", "-C", str(bare_repo), "rev-parse", "--verify", "refs/remotes/origin/master"],
        capture_output=True,
    )


def test_resolve_spec_local_found_on_fallback_remote(tmp_path):
    """Branch not on spec.remote but found in local refs on fallback remote."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    spec = BranchSpec("origin/master-parrot", None)
    remotes = {
        "origin": RemoteConfig(url="git@github.com:odoo/odoo.git"),
        "dev": RemoteConfig(url="git@github.com:odoo-dev/odoo.git"),
    }

    rev_parse_fail = MagicMock(returncode=1)
    rev_parse_ok = MagicMock(returncode=0)

    with patch("ow.git.subprocess.run", side_effect=[rev_parse_fail, rev_parse_ok]) as mock_run:
        result = resolve_spec_local(bare_repo, spec, remotes)

    assert result.remote == "dev"
    assert result.branch == "master-parrot"
    assert mock_run.call_count == 2


def test_resolve_spec_local_raises_when_not_found(tmp_path):
    """RuntimeError raised when branch not found in any local refs (no fetch attempted)."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    spec = BranchSpec("origin/nonexistent", None)
    remotes = {"origin": RemoteConfig(url="git@github.com:odoo/odoo.git")}

    always_fail = MagicMock(returncode=1)

    with patch("ow.git.subprocess.run", return_value=always_fail):
        with pytest.raises(RuntimeError, match="nonexistent"):
            resolve_spec_local(bare_repo, spec, remotes)
