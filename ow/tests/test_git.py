from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from ow.config import BranchSpec, RemoteConfig
from ow.git import (
    _set_branch_upstream,
    attach_worktree,
    create_worktree,
    detach_worktree,
    ensure_bare_repo,
    ensure_ref,
    get_remote_ref_for_branch,
    get_remote_url,
    get_rev_list_count,
    get_upstream,
    get_worktree_branch,
    get_worktree_head,
    git,
    git_fetch,
    git_merge_base_fork_point,
    git_rebase,
    git_switch,
    ordered_remotes,
    remove_worktree,
    resolve_spec,
    resolve_spec_local,
    run_cmd,
    worktree_exists,
    worktree_is_detached,
)


# ---------------------------------------------------------------------------
# run_cmd
# ---------------------------------------------------------------------------

def test_run_cmd_prints_to_stderr(capsys):
    with patch("ow.git.subprocess.run") as mock_run:
        run_cmd(["git", "status"], check=True)

    captured = capsys.readouterr()
    assert "$ git status" in captured.err
    mock_run.assert_called_once_with(["git", "status"], check=True)


def test_run_cmd_quiet_no_stderr(capsys):
    with patch("ow.git.subprocess.run") as mock_run:
        run_cmd(["git", "config", "foo", "bar"], quiet=True, check=True)

    captured = capsys.readouterr()
    assert captured.err == ""
    mock_run.assert_called_once_with(["git", "config", "foo", "bar"], check=True)


def test_run_cmd_returns_completed_process():
    mock_result = MagicMock(returncode=0)
    with patch("ow.git.subprocess.run", return_value=mock_result):
        result = run_cmd(["git", "status"], quiet=True)
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# ordered_remotes
# ---------------------------------------------------------------------------

def test_ordered_remotes_origin_first():
    remotes = {
        "dev": RemoteConfig(url="dev-url"),
        "origin": RemoteConfig(url="origin-url"),
        "abc": RemoteConfig(url="abc-url"),
    }
    assert ordered_remotes(remotes) == ["origin", "abc", "dev"]


def test_ordered_remotes_no_origin():
    remotes = {
        "dev": RemoteConfig(url="dev-url"),
        "abc": RemoteConfig(url="abc-url"),
    }
    assert ordered_remotes(remotes) == ["abc", "dev"]


def test_ordered_remotes_only_origin():
    remotes = {"origin": RemoteConfig(url="origin-url")}
    assert ordered_remotes(remotes) == ["origin"]


def test_ordered_remotes_empty():
    assert ordered_remotes({}) == []


# ---------------------------------------------------------------------------
# get_worktree_branch
# ---------------------------------------------------------------------------

def test_get_worktree_branch_returns_name():
    mock_result = MagicMock(returncode=0)
    mock_result.stdout = "master-feature\n"
    with patch("ow.git.subprocess.run", return_value=mock_result):
        assert get_worktree_branch(Path("/fake")) == "master-feature"


def test_get_worktree_branch_returns_none_when_detached():
    mock_result = MagicMock(returncode=0)
    mock_result.stdout = "HEAD\n"
    with patch("ow.git.subprocess.run", return_value=mock_result):
        assert get_worktree_branch(Path("/fake")) is None


def test_get_worktree_branch_returns_none_on_error():
    mock_result = MagicMock(returncode=128)
    mock_result.stdout = ""
    with patch("ow.git.subprocess.run", return_value=mock_result):
        assert get_worktree_branch(Path("/fake")) is None


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


def test_ensure_bare_repo_ordered_remotes(tmp_path):
    """Non-origin remotes are configured in alphabetical order."""
    bare_repos_dir = tmp_path / "bare-repos"
    bare_repo = bare_repos_dir / "community.git"
    bare_repo.mkdir(parents=True)

    remotes = {
        "origin": RemoteConfig(url="origin-url"),
        "zebra": RemoteConfig(url="zebra-url"),
        "alpha": RemoteConfig(url="alpha-url"),
    }

    with patch("ow.git.subprocess.run") as mock_run:
        ensure_bare_repo("community", remotes, bare_repos_dir)

    calls = mock_run.call_args_list
    assert len(calls) == 2
    # alpha before zebra
    assert "remote.alpha.url" in calls[0].args[0][-2]
    assert "remote.zebra.url" in calls[1].args[0][-2]


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

    with patch("ow.git.subprocess.run", side_effect=[branch_missing, MagicMock(), MagicMock(), MagicMock()]) as mock_run:
        create_worktree(bare_repo, worktree_path, spec)

    assert mock_run.call_args_list[1] == call(
        ["git", "-C", str(bare_repo), "worktree", "add", "-b", "master-feature",
         str(worktree_path), "origin/master"],
        check=True,
    )


def test_create_worktree_attached_new_branch_sets_upstream(tmp_path):
    """New branch creation also sets upstream tracking via two git config calls."""
    bare_repo = tmp_path / "enterprise.git"
    bare_repo.mkdir()
    worktree_path = Path("/fake/workspaces/test/enterprise")
    spec = BranchSpec("dev/master-parrot-ring-the-phone", "master-parrot-ring-the-phone")

    branch_missing = MagicMock(returncode=1)

    with patch("ow.git.subprocess.run", side_effect=[branch_missing, MagicMock(), MagicMock(), MagicMock()]) as mock_run:
        create_worktree(bare_repo, worktree_path, spec)

    assert mock_run.call_args_list[1] == call(
        ["git", "-C", str(bare_repo), "worktree", "add", "-b", "master-parrot-ring-the-phone",
         str(worktree_path), "dev/master-parrot-ring-the-phone"],
        check=True,
    )
    assert mock_run.call_args_list[2] == call(
        ["git", "-C", str(bare_repo), "config",
         "branch.master-parrot-ring-the-phone.remote", "dev"],
        check=True,
    )
    assert mock_run.call_args_list[3] == call(
        ["git", "-C", str(bare_repo), "config",
         "branch.master-parrot-ring-the-phone.merge", "refs/heads/master-parrot-ring-the-phone"],
        check=True,
    )


def test_create_worktree_attached_existing_branch(tmp_path):
    """Branch already exists (prunable worktree re-created) — omits -b, still sets upstream."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    worktree_path = Path("/fake/workspaces/test/community")
    spec = BranchSpec("origin/master", "master-feature")

    branch_exists = MagicMock(returncode=0)

    with patch("ow.git.subprocess.run", side_effect=[branch_exists, MagicMock(), MagicMock(), MagicMock()]) as mock_run:
        create_worktree(bare_repo, worktree_path, spec)

    assert mock_run.call_count == 4
    assert mock_run.call_args_list[1] == call(
        ["git", "-C", str(bare_repo), "worktree", "add", str(worktree_path), "master-feature"],
        check=True,
    )
    assert mock_run.call_args_list[2] == call(
        ["git", "-C", str(bare_repo), "config", "branch.master-feature.remote", "origin"],
        check=True,
    )
    assert mock_run.call_args_list[3] == call(
        ["git", "-C", str(bare_repo), "config", "branch.master-feature.merge", "refs/heads/master"],
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


def test_resolve_spec_local_branch_found_on_remote(tmp_path):
    """local_branch already exists on a remote — use it as base_ref, then ensure base branch ref."""
    bare_repo = tmp_path / "enterprise.git"
    bare_repo.mkdir()
    spec = BranchSpec("origin/master-parrot", "master-parrot-ring-the-phone")
    remotes = {
        "origin": RemoteConfig(url="git@github.com:odoo/enterprise.git"),
        "dev": RemoteConfig(url="git@github.com:odoo-dev/enterprise.git"),
    }

    rev_parse_fail_origin = MagicMock(returncode=1)  # origin/master-parrot-ring-the-phone: miss
    fetch_fail_origin = MagicMock(returncode=1)       # fetch origin master-parrot-ring-the-phone: fail
    rev_parse_ok_dev = MagicMock(returncode=0)         # dev/master-parrot-ring-the-phone: hit
    rev_parse_ok_base = MagicMock(returncode=0)        # refs/remotes/origin/master-parrot: already present

    with patch("ow.git.subprocess.run", side_effect=[
        rev_parse_fail_origin,
        fetch_fail_origin,
        rev_parse_ok_dev,
        rev_parse_ok_base,  # _ensure_base_ref_non_fatal: base ref already present
    ]) as mock_run:
        result = resolve_spec(bare_repo, spec, remotes)

    assert result.base_ref == "dev/master-parrot-ring-the-phone"
    assert result.local_branch == "master-parrot-ring-the-phone"
    assert mock_run.call_count == 4


def test_resolve_spec_local_branch_not_on_remote_falls_back_to_base(tmp_path):
    """local_branch not on any remote — falls through to base branch lookup as normal."""
    bare_repo = tmp_path / "enterprise.git"
    bare_repo.mkdir()
    spec = BranchSpec("origin/master-parrot", "master-parrot-ring-the-phone")
    remotes = {
        "origin": RemoteConfig(url="git@github.com:odoo/enterprise.git"),
        "dev": RemoteConfig(url="git@github.com:odoo-dev/enterprise.git"),
    }

    # All local_branch lookups fail
    lp_fail_o = MagicMock(returncode=1)  # rev-parse origin/master-parrot-ring-the-phone
    lf_fail_o = MagicMock(returncode=1)  # fetch origin master-parrot-ring-the-phone
    lp_fail_d = MagicMock(returncode=1)  # rev-parse dev/master-parrot-ring-the-phone
    lf_fail_d = MagicMock(returncode=1)  # fetch dev master-parrot-ring-the-phone
    # Base branch: origin/master-parrot found locally
    bp_ok = MagicMock(returncode=0)      # rev-parse origin/master-parrot

    with patch("ow.git.subprocess.run", side_effect=[
        lp_fail_o, lf_fail_o, lp_fail_d, lf_fail_d,
        bp_ok,
    ]) as mock_run:
        result = resolve_spec(bare_repo, spec, remotes)

    assert result.base_ref == "origin/master-parrot"
    assert result.local_branch == "master-parrot-ring-the-phone"
    assert mock_run.call_count == 5


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


# ---------------------------------------------------------------------------
# _set_branch_upstream
# ---------------------------------------------------------------------------

def test_set_branch_upstream(tmp_path):
    """Writes branch.X.remote and branch.X.merge config keys."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()

    with patch("ow.git.subprocess.run") as mock_run:
        _set_branch_upstream(bare_repo, "master-feature", "origin", "master")

    assert mock_run.call_count == 2
    assert mock_run.call_args_list[0] == call(
        ["git", "-C", str(bare_repo), "config", "branch.master-feature.remote", "origin"],
        check=True,
    )
    assert mock_run.call_args_list[1] == call(
        ["git", "-C", str(bare_repo), "config", "branch.master-feature.merge", "refs/heads/master"],
        check=True,
    )


def test_set_branch_upstream_non_origin(tmp_path):
    """remote arg is forwarded correctly for non-origin remotes."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()

    with patch("ow.git.subprocess.run") as mock_run:
        _set_branch_upstream(bare_repo, "master-parrot-ring-the-phone", "dev", "master-parrot-ring-the-phone")

    assert mock_run.call_args_list[0] == call(
        ["git", "-C", str(bare_repo), "config",
         "branch.master-parrot-ring-the-phone.remote", "dev"],
        check=True,
    )
    assert mock_run.call_args_list[1] == call(
        ["git", "-C", str(bare_repo), "config",
         "branch.master-parrot-ring-the-phone.merge", "refs/heads/master-parrot-ring-the-phone"],
        check=True,
    )


# ---------------------------------------------------------------------------
# worktree_is_detached
# ---------------------------------------------------------------------------

def test_worktree_is_detached_returns_true(tmp_path):
    """Returns True when symbolic-ref exits non-zero (HEAD is detached)."""
    worktree_path = tmp_path / "workspaces" / "test" / "community"
    worktree_path.mkdir(parents=True)

    mock_result = MagicMock(returncode=1)

    with patch("ow.git.subprocess.run", return_value=mock_result):
        assert worktree_is_detached(worktree_path) is True


def test_worktree_is_detached_returns_false(tmp_path):
    """Returns False when symbolic-ref exits zero (HEAD is on a branch)."""
    worktree_path = tmp_path / "workspaces" / "test" / "community"
    worktree_path.mkdir(parents=True)

    mock_result = MagicMock(returncode=0)

    with patch("ow.git.subprocess.run", return_value=mock_result):
        assert worktree_is_detached(worktree_path) is False


# ---------------------------------------------------------------------------
# attach_worktree
# ---------------------------------------------------------------------------

def test_attach_worktree_creates_new_branch(tmp_path):
    """When local branch doesn't exist: switch -c, then set upstream."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    worktree_path = Path("/fake/workspaces/test/community")
    spec = BranchSpec("origin/master", "master-feature")

    branch_missing = MagicMock(returncode=1)

    with patch("ow.git.subprocess.run", side_effect=[branch_missing, MagicMock(), MagicMock(), MagicMock()]) as mock_run:
        attach_worktree(bare_repo, worktree_path, spec)

    assert mock_run.call_count == 4
    assert mock_run.call_args_list[1] == call(
        ["git", "-C", str(worktree_path), "switch", "-c", "master-feature"],
        check=True,
    )
    assert mock_run.call_args_list[2] == call(
        ["git", "-C", str(bare_repo), "config", "branch.master-feature.remote", "origin"],
        check=True,
    )
    assert mock_run.call_args_list[3] == call(
        ["git", "-C", str(bare_repo), "config", "branch.master-feature.merge", "refs/heads/master"],
        check=True,
    )


def test_attach_worktree_existing_branch(tmp_path):
    """When local branch exists: switch (no -c), then set upstream."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    worktree_path = Path("/fake/workspaces/test/community")
    spec = BranchSpec("origin/master", "master-feature")

    branch_exists = MagicMock(returncode=0)

    with patch("ow.git.subprocess.run", side_effect=[branch_exists, MagicMock(), MagicMock(), MagicMock()]) as mock_run:
        attach_worktree(bare_repo, worktree_path, spec)

    assert mock_run.call_count == 4
    assert mock_run.call_args_list[1] == call(
        ["git", "-C", str(worktree_path), "switch", "master-feature"],
        check=True,
    )
    assert mock_run.call_args_list[2] == call(
        ["git", "-C", str(bare_repo), "config", "branch.master-feature.remote", "origin"],
        check=True,
    )
    assert mock_run.call_args_list[3] == call(
        ["git", "-C", str(bare_repo), "config", "branch.master-feature.merge", "refs/heads/master"],
        check=True,
    )


# ---------------------------------------------------------------------------
# detach_worktree
# ---------------------------------------------------------------------------

def test_detach_worktree(tmp_path):
    """Switches worktree to detached HEAD at base_ref."""
    worktree_path = Path("/fake/workspaces/test/community")

    with patch("ow.git.subprocess.run") as mock_run:
        detach_worktree(worktree_path, "origin/master")

    mock_run.assert_called_once_with(
        ["git", "-C", str(worktree_path), "switch", "--detach", "origin/master"],
        check=True,
    )


# ---------------------------------------------------------------------------
# resolve_spec Fix 1 — base ref fetch on early-return path
# ---------------------------------------------------------------------------

def test_resolve_spec_local_branch_found_fetches_base_ref_when_missing(tmp_path):
    """Early-return path: base ref not in local store — rev-parse miss + fetch issued."""
    bare_repo = tmp_path / "enterprise.git"
    bare_repo.mkdir()
    spec = BranchSpec("origin/18.0", "18.0-my-feature")
    remotes = {"origin": RemoteConfig(url="git@github.com:odoo/enterprise.git")}

    rev_parse_ok_local = MagicMock(returncode=0)   # origin/18.0-my-feature already fetched
    rev_parse_miss_base = MagicMock(returncode=1)  # refs/remotes/origin/18.0: missing
    fetch_base_ok = MagicMock(returncode=0)         # fetch origin 18.0: success

    with patch("ow.git.subprocess.run", side_effect=[
        rev_parse_ok_local,
        rev_parse_miss_base,
        fetch_base_ok,
    ]) as mock_run:
        result = resolve_spec(bare_repo, spec, remotes)

    assert result.base_ref == "origin/18.0-my-feature"
    assert result.local_branch == "18.0-my-feature"
    assert mock_run.call_count == 3
    assert mock_run.call_args_list[1] == call(
        ["git", "-C", str(bare_repo), "rev-parse", "--verify", "refs/remotes/origin/18.0"],
        capture_output=True,
    )
    assert mock_run.call_args_list[2] == call(
        ["git", "-C", str(bare_repo), "fetch", "origin", "18.0:refs/remotes/origin/18.0"],
        capture_output=True,
    )


def test_resolve_spec_local_branch_found_skips_base_ref_fetch_when_present(tmp_path):
    """Early-return path: base ref already in local store — no fetch issued."""
    bare_repo = tmp_path / "enterprise.git"
    bare_repo.mkdir()
    spec = BranchSpec("origin/18.0", "18.0-my-feature")
    remotes = {"origin": RemoteConfig(url="git@github.com:odoo/enterprise.git")}

    rev_parse_ok_local = MagicMock(returncode=0)  # origin/18.0-my-feature already fetched
    rev_parse_ok_base = MagicMock(returncode=0)   # refs/remotes/origin/18.0: already present

    with patch("ow.git.subprocess.run", side_effect=[
        rev_parse_ok_local,
        rev_parse_ok_base,
    ]) as mock_run:
        result = resolve_spec(bare_repo, spec, remotes)

    assert result.base_ref == "origin/18.0-my-feature"
    assert result.local_branch == "18.0-my-feature"
    assert mock_run.call_count == 2


# ---------------------------------------------------------------------------
# get_remote_ref_for_branch
# ---------------------------------------------------------------------------

def test_get_remote_ref_for_branch_found_on_first_remote(tmp_path):
    """With ordered_remotes, origin is checked first."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    alias_remotes = {
        "iap-apps": RemoteConfig(url="git@github.com:odoo-ps/ps-tech-iap-apps.git"),
        "origin": RemoteConfig(url="git@github.com:odoo/odoo.git"),
    }

    rev_parse_ok = MagicMock(returncode=0)

    with patch("ow.git.subprocess.run", return_value=rev_parse_ok) as mock_run:
        result = get_remote_ref_for_branch(
            bare_repo, "18.0-add-voip-telnyx-service-basm", alias_remotes
        )

    assert result == "origin/18.0-add-voip-telnyx-service-basm"
    assert mock_run.call_count == 1
    mock_run.assert_called_once_with(
        ["git", "-C", str(bare_repo), "rev-parse", "--verify",
         "refs/remotes/origin/18.0-add-voip-telnyx-service-basm"],
        capture_output=True,
    )


def test_get_remote_ref_for_branch_found_on_second_remote(tmp_path):
    """Skips first remote (miss) and returns match on second."""
    bare_repo = tmp_path / "enterprise.git"
    bare_repo.mkdir()
    alias_remotes = {
        "origin": RemoteConfig(url="git@github.com:odoo/enterprise.git"),
        "dev": RemoteConfig(url="git@github.com:odoo-dev/enterprise.git"),
    }

    miss = MagicMock(returncode=1)
    hit = MagicMock(returncode=0)

    with patch("ow.git.subprocess.run", side_effect=[miss, hit]) as mock_run:
        result = get_remote_ref_for_branch(bare_repo, "master-parrot", alias_remotes)

    assert result == "dev/master-parrot"
    assert mock_run.call_count == 2


def test_get_remote_ref_for_branch_excludes_base_ref(tmp_path):
    """exclude_ref skips the candidate even if the ref exists."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    alias_remotes = {
        "origin": RemoteConfig(url="git@github.com:odoo/odoo.git"),
        "iap-apps": RemoteConfig(url="git@github.com:odoo-ps/ps-tech-iap-apps.git"),
    }

    miss_iap = MagicMock(returncode=1)

    with patch("ow.git.subprocess.run", side_effect=[miss_iap]) as mock_run:
        result = get_remote_ref_for_branch(
            bare_repo, "18.0", alias_remotes, exclude_ref="origin/18.0"
        )

    # origin/18.0 skipped (excluded); iap-apps/18.0 not found
    assert result is None
    assert mock_run.call_count == 1


def test_get_remote_ref_for_branch_returns_none_when_not_found(tmp_path):
    """Returns None when no configured remote has the branch."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    alias_remotes = {
        "origin": RemoteConfig(url="git@github.com:odoo/odoo.git"),
        "dev": RemoteConfig(url="git@github.com:odoo-dev/odoo.git"),
    }

    always_miss = MagicMock(returncode=1)

    with patch("ow.git.subprocess.run", return_value=always_miss):
        result = get_remote_ref_for_branch(bare_repo, "18.0-nonexistent", alias_remotes)

    assert result is None


def test_get_remote_ref_for_branch_prefers_non_base_remote(tmp_path):
    """With base_remote set, fork remote is checked before base remote."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    alias_remotes = {
        "origin": RemoteConfig(url="git@github.com:odoo/odoo.git"),
        "dev": RemoteConfig(url="git@github.com:odoo-dev/odoo.git"),
    }

    # dev is checked first (non-base), hits immediately
    hit_dev = MagicMock(returncode=0)

    with patch("ow.git.subprocess.run", side_effect=[hit_dev]) as mock_run:
        result = get_remote_ref_for_branch(
            bare_repo, "master-parrot", alias_remotes,
            exclude_ref="origin/master", base_remote="origin",
        )

    assert result == "dev/master-parrot"
    assert mock_run.call_count == 1
    mock_run.assert_called_once_with(
        ["git", "-C", str(bare_repo), "rev-parse", "--verify",
         "refs/remotes/dev/master-parrot"],
        capture_output=True,
    )


def test_get_remote_ref_for_branch_falls_back_to_base_remote(tmp_path):
    """Falls back to base remote if no fork remote has the branch."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    alias_remotes = {
        "origin": RemoteConfig(url="git@github.com:odoo/iap-apps.git"),
    }

    hit_origin = MagicMock(returncode=0)

    with patch("ow.git.subprocess.run", side_effect=[hit_origin]) as mock_run:
        result = get_remote_ref_for_branch(
            bare_repo, "18.0-my-feature", alias_remotes,
            exclude_ref="origin/18.0", base_remote="origin",
        )

    assert result == "origin/18.0-my-feature"
    assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# get_remote_url
# ---------------------------------------------------------------------------

def test_get_remote_url_returns_url(tmp_path):
    """Returns the URL when git remote get-url succeeds."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()

    mock_result = MagicMock(returncode=0)
    mock_result.stdout = "git@github.com:odoo-dev/odoo.git\n"

    with patch("ow.git.subprocess.run", return_value=mock_result):
        result = get_remote_url(bare_repo, "dev")

    assert result == "git@github.com:odoo-dev/odoo.git"


def test_get_remote_url_returns_none_when_remote_missing(tmp_path):
    """Returns None when the remote is not configured."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()

    mock_result = MagicMock(returncode=128)
    mock_result.stdout = ""

    with patch("ow.git.subprocess.run", return_value=mock_result):
        result = get_remote_url(bare_repo, "nonexistent")

    assert result is None


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


def test_git_adds_c_flag(tmp_path):
    """git() automatically adds -C flag with repo path."""
    repo = tmp_path / "repo"
    repo.mkdir()

    with patch("ow.git.run_cmd") as mock_run:
        git(repo, "status", check=True)

    mock_run.assert_called_once_with(
        ["git", "-C", str(repo), "status"], quiet=False, check=True
    )


def test_git_passes_quiet_flag(tmp_path):
    """git() passes quiet flag to run_cmd."""
    repo = tmp_path / "repo"
    repo.mkdir()

    with patch("ow.git.run_cmd") as mock_run:
        git(repo, "status", quiet=True, check=True)

    mock_run.assert_called_once_with(
        ["git", "-C", str(repo), "status"], quiet=True, check=True
    )


# ---------------------------------------------------------------------------
# git_fetch
# ---------------------------------------------------------------------------


def test_git_fetch_basic(tmp_path):
    """git_fetch builds correct fetch command."""
    repo = tmp_path / "repo"
    repo.mkdir()

    with patch("ow.git.git") as mock_git:
        git_fetch(repo, "origin", "master:refs/remotes/origin/master", check=True)

    mock_git.assert_called_once_with(
        repo, "fetch", "origin", "master:refs/remotes/origin/master", check=True
    )


def test_git_fetch_force(tmp_path):
    """git_fetch with force=True prepends + to refspec."""
    repo = tmp_path / "repo"
    repo.mkdir()

    with patch("ow.git.git") as mock_git:
        git_fetch(
            repo,
            "origin",
            "master:refs/remotes/origin/master",
            force=True,
            check=True,
        )

    mock_git.assert_called_once_with(
        repo, "fetch", "origin", "+master:refs/remotes/origin/master", check=True
    )


# ---------------------------------------------------------------------------
# git_switch
# ---------------------------------------------------------------------------


def test_git_switch_basic(tmp_path):
    """git_switch switches to a branch."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    with patch("ow.git.git") as mock_git:
        git_switch(worktree, "master", check=True)

    mock_git.assert_called_once_with(worktree, "switch", "master", check=True)


def test_git_switch_detach(tmp_path):
    """git_switch with detach=True adds --detach flag."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    with patch("ow.git.git") as mock_git:
        git_switch(worktree, "origin/master", detach=True, check=True)

    mock_git.assert_called_once_with(
        worktree, "switch", "--detach", "origin/master", check=True
    )


def test_git_switch_create(tmp_path):
    """git_switch with create=True adds -c flag."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    with patch("ow.git.git") as mock_git:
        git_switch(worktree, "new-branch", create=True, check=True)

    mock_git.assert_called_once_with(
        worktree, "switch", "-c", "new-branch", check=True
    )


# ---------------------------------------------------------------------------
# git_rebase
# ---------------------------------------------------------------------------


def test_git_rebase_returns_completed_process(tmp_path):
    """git_rebase returns CompletedProcess for caller to check."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    mock_result = MagicMock(returncode=0)

    with patch("ow.git.git", return_value=mock_result) as mock_git:
        result = git_rebase(worktree, "origin/master")

    mock_git.assert_called_once_with(worktree, "rebase", "origin/master")
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# git_merge_base_fork_point
# ---------------------------------------------------------------------------


def test_git_merge_base_fork_point_returns_hash(tmp_path):
    """Returns the fork-point hash when found."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    mock_result = MagicMock(returncode=0)
    mock_result.stdout = "abc123def456\n"

    with patch("ow.git.git", return_value=mock_result) as mock_git:
        result = git_merge_base_fork_point(worktree, "origin/master", "feature")

    mock_git.assert_called_once_with(
        worktree,
        "merge-base",
        "--fork-point",
        "origin/master",
        "feature",
        quiet=True,
        capture_output=True,
        text=True,
    )
    assert result == "abc123def456"


def test_git_merge_base_fork_point_returns_none_on_failure(tmp_path):
    """Returns None when fork-point cannot be found (upstream rewritten)."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    mock_result = MagicMock(returncode=1)
    mock_result.stdout = ""

    with patch("ow.git.git", return_value=mock_result):
        result = git_merge_base_fork_point(worktree, "origin/master", "feature")

    assert result is None


def test_git_merge_base_fork_point_returns_none_on_empty_output(tmp_path):
    """Returns None when output is empty."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    mock_result = MagicMock(returncode=0)
    mock_result.stdout = "\n"

    with patch("ow.git.git", return_value=mock_result):
        result = git_merge_base_fork_point(worktree, "origin/master", "feature")

    assert result is None
