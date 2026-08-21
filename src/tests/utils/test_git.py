import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from ow.utils.config import BranchSpec, RemoteConfig
from ow.utils import git as git_mod
from ow.utils import paths
from ow.utils.git import (
    _get_bare_config,
    attach_worktree,
    create_worktree,
    detach_worktree,
    ensure_bare_repo,
    ensure_ref,
    get_all_remote_refs,
    get_remote_ref_for_branch,
    get_remote_url,
    get_rev_list_count,
    get_upstream,
    get_worktree_branch,
    get_worktree_head,
    git,
    git_fetch,
    ordered_remotes,
    parallel_per_repo,
    resolve_spec,
    resolve_spec_local,
    run_cmd,
    set_branch_upstream,
    worktree_exists,
    worktree_is_detached,
)

def make_bare_repo(path: Path) -> Path:
    """A real bare repository at `path`.

    ensure_bare_repo asks git whether the directory is a repository, so a
    mkdir() no longer stands in for one — which is the whole point of the
    check: an empty or half-written directory is not a clone that worked.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", "-q", str(path)], check=True)
    return path


# ---------------------------------------------------------------------------
# run_cmd
# ---------------------------------------------------------------------------

def test_run_cmd_prints_to_stderr(capsys):
    with patch("ow.utils.git._run") as mock_run:
        run_cmd(["git", "status"], check=True)

    captured = capsys.readouterr()
    assert "$ git status" in captured.err
    mock_run.assert_called_once_with(["git", "status"], check=True)


def test_run_cmd_quiet_no_stderr(capsys):
    with patch("ow.utils.git._run") as mock_run:
        run_cmd(["git", "config", "foo", "bar"], quiet=True, check=True)

    captured = capsys.readouterr()
    assert captured.err == ""
    mock_run.assert_called_once_with(["git", "config", "foo", "bar"], check=True)


def test_run_cmd_returns_completed_process():
    mock_result = MagicMock(returncode=0)
    with patch("ow.utils.git._run", return_value=mock_result):
        result = run_cmd(["git", "status"], quiet=True)
    assert result.returncode == 0


def test_run_cmd_hides_C_path(capsys):
    """When git command has -C path, display strips it for cleaner output."""
    with patch("ow.utils.git._run") as mock_run:
        run_cmd(["git", "-C", "/path/to/repo", "fetch", "origin"], quiet=False, label="community", check=True)

    captured = capsys.readouterr()
    assert "[community] git fetch origin" in captured.err
    assert "-C /path/to/repo" not in captured.err
    mock_run.assert_called_once_with(
        ["git", "-C", "/path/to/repo", "fetch", "origin"], check=True
    )


# ---------------------------------------------------------------------------
# git() — bare-repo alias labelling
# ---------------------------------------------------------------------------

def test_git_labels_by_alias_under_ows_repos_dir(xdg):
    """A repo under paths.repos_dir() is one of ow's bare repos: label by its
    alias (the stem), not its full directory name."""
    repo = paths.repos_dir() / "community.git"
    with patch("ow.utils.git.run_cmd") as mock_run_cmd:
        git(repo, "fetch", "origin")
    assert mock_run_cmd.call_args.kwargs["label"] == "community"


def test_git_does_not_label_by_alias_under_an_unrelated_repos_dir(xdg, tmp_path):
    """A repo merely sitting under *some* directory named "repos" — e.g.
    ~/repos/foo.git, or a workspace's own repos/ subdir — is not one of ow's
    bare repos just because its parent happens to be named "repos"."""
    repo = tmp_path / "repos" / "community.git"
    assert repo.parent != paths.repos_dir()
    with patch("ow.utils.git.run_cmd") as mock_run_cmd:
        git(repo, "fetch", "origin")
    assert mock_run_cmd.call_args.kwargs["label"] == "community.git"


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
    with patch("ow.utils.git._run", return_value=mock_result):
        assert get_worktree_branch(Path("/fake")) == "master-feature"


def test_get_worktree_branch_returns_none_when_detached():
    mock_result = MagicMock(returncode=0)
    mock_result.stdout = "HEAD\n"
    with patch("ow.utils.git._run", return_value=mock_result):
        assert get_worktree_branch(Path("/fake")) is None


def test_get_worktree_branch_returns_none_on_error():
    mock_result = MagicMock(returncode=128)
    mock_result.stdout = ""
    with patch("ow.utils.git._run", return_value=mock_result):
        assert get_worktree_branch(Path("/fake")) is None


# ---------------------------------------------------------------------------
# get_all_remote_refs
# ---------------------------------------------------------------------------

def test_get_all_remote_refs_parses_output(tmp_path):
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()

    mock_result = MagicMock(returncode=0)
    mock_result.stdout = "origin/master\norigin/18.0\ndev/master-feature\n"

    with patch("ow.utils.git._run", return_value=mock_result):
        refs = get_all_remote_refs(bare_repo)

    assert refs == {"origin/master", "origin/18.0", "dev/master-feature"}


def test_get_all_remote_refs_returns_empty_on_failure(tmp_path):
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()

    mock_result = MagicMock(returncode=1)

    with patch("ow.utils.git._run", return_value=mock_result):
        refs = get_all_remote_refs(bare_repo)

    assert refs == set()


def test_get_all_remote_refs_handles_empty_output(tmp_path):
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()

    mock_result = MagicMock(returncode=0)
    mock_result.stdout = ""

    with patch("ow.utils.git._run", return_value=mock_result):
        refs = get_all_remote_refs(bare_repo)

    assert refs == set()


# ---------------------------------------------------------------------------
# _get_bare_config
# ---------------------------------------------------------------------------

def test_get_bare_config_parses_key_value(tmp_path):
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()

    mock_result = MagicMock(returncode=0)
    mock_result.stdout = "remote.origin.url=git@github.com:odoo/odoo.git\nremote.dev.url=git@github.com:dev/odoo.git\n"

    with patch("ow.utils.git._run", return_value=mock_result):
        config = _get_bare_config(bare_repo)

    assert config == {
        "remote.origin.url": "git@github.com:odoo/odoo.git",
        "remote.dev.url": "git@github.com:dev/odoo.git",
    }


def test_get_bare_config_returns_empty_on_failure(tmp_path):
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()

    mock_result = MagicMock(returncode=1)

    with patch("ow.utils.git._run", return_value=mock_result):
        config = _get_bare_config(bare_repo)

    assert config == {}


def test_ensure_bare_repo_skips_writes_when_config_matches(tmp_path):
    """When config values already match, no git config writes should occur."""
    bare_repos_dir = tmp_path / "bare-repos"
    bare_repo = make_bare_repo(bare_repos_dir / "community.git")

    remotes = {
        "origin": RemoteConfig(url="git@github.com:odoo/odoo.git"),
        "dev": RemoteConfig(
            url="git@github.com:odoo-dev/odoo.git",
            pushurl="git@github.com:odoo-dev/odoo.git",
            fetch="+refs/heads/*:refs/remotes/dev/*",
        ),
    }

    existing_config = {
        "remote.origin.url": "git@github.com:odoo/odoo.git",
        "remote.dev.url": "git@github.com:odoo-dev/odoo.git",
        "remote.dev.pushurl": "git@github.com:odoo-dev/odoo.git",
        "remote.dev.fetch": "+refs/heads/*:refs/remotes/dev/*",
        "core.logallrefupdates": "true",
    }

    with patch("ow.utils.git.run_cmd") as mock_run_cmd, \
         patch("ow.utils.git._get_bare_config", return_value=existing_config):
        ensure_bare_repo("community", remotes, bare_repos_dir)

    mock_run_cmd.assert_not_called()


def test_ensure_bare_repo_writes_only_changed_values(tmp_path):
    """Only writes config values that differ from current config."""
    bare_repos_dir = tmp_path / "bare-repos"
    bare_repo = make_bare_repo(bare_repos_dir / "community.git")

    remotes = {
        "origin": RemoteConfig(url="git@github.com:odoo/odoo.git"),
        "dev": RemoteConfig(
            url="git@github.com:odoo-dev/odoo.git",
            pushurl="git@github.com:NEW-pushurl/odoo.git",
            fetch="+refs/heads/*:refs/remotes/dev/*",
        ),
    }

    existing_config = {
        "remote.origin.url": "git@github.com:odoo/odoo.git",
        "remote.dev.url": "git@github.com:odoo-dev/odoo.git",
        "remote.dev.pushurl": "git@github.com:OLD-pushurl/odoo.git",
        "remote.dev.fetch": "+refs/heads/*:refs/remotes/dev/*",
        "core.logallrefupdates": "true",
    }

    with patch("ow.utils.git.run_cmd") as mock_run_cmd, \
         patch("ow.utils.git._get_bare_config", return_value=existing_config):
        ensure_bare_repo("community", remotes, bare_repos_dir)

    # Only pushurl should be written (url and fetch already match)
    assert mock_run_cmd.call_count == 1
    assert "remote.dev.pushurl" in mock_run_cmd.call_args_list[0].args[0]



def test_ensure_bare_repo_updates_origin_url_when_it_drifts(tmp_path):
    """When the user changes origin.url in their config, the bare repo must be
    updated to match — not left pointing at the old URL."""
    bare_repos_dir = tmp_path / "bare-repos"
    bare_repo = make_bare_repo(bare_repos_dir / "community.git")

    remotes = {
        "origin": RemoteConfig(url="git@github.com:NEW-fork/odoo.git"),
    }

    existing_config = {
        "remote.origin.url": "git@github.com:OLD-fork/odoo.git",
        "core.logallrefupdates": "true",
    }

    with patch("ow.utils.git.run_cmd") as mock_run_cmd, \
         patch("ow.utils.git._get_bare_config", return_value=existing_config):
        ensure_bare_repo("community", remotes, bare_repos_dir)

    origin_url_writes = [
        c for c in mock_run_cmd.call_args_list
        if "remote.origin.url" in str(c)
    ]
    assert len(origin_url_writes) == 1
    assert "git@github.com:NEW-fork/odoo.git" in origin_url_writes[0].args[0]

# ---------------------------------------------------------------------------
# ensure_bare_repo
# ---------------------------------------------------------------------------

def test_clone_bare_asks_for_the_cheapest_clone_git_will_give(tmp_path):
    """--filter=blob:none --single-branch is the difference between a few MB and
    the whole of odoo/odoo. Nothing else in ow re-applies these, so a repair
    path that skips this function silently pulls full history."""
    destination = tmp_path / "community.git"

    with patch("ow.utils.git.run_cmd") as mock_run_cmd:
        git_mod._clone_bare("community", "git@github.com:odoo/odoo.git", destination)

    mock_run_cmd.assert_called_once_with(
        ["git", "clone", "--bare", "--filter=blob:none", "--single-branch",
         "git@github.com:odoo/odoo.git", str(destination)],
        label="community",
        check=True,
        capture_output=True,
        text=True,
    )


def test_ensure_bare_repo_clones_when_missing(tmp_path):
    bare_repos_dir = tmp_path / "bare-repos"
    bare_repos_dir.mkdir()
    bare_repo = bare_repos_dir / "community.git"
    # bare_repo doesn't exist yet

    remotes = {"origin": RemoteConfig(url="git@github.com:odoo/odoo.git")}

    def fake_clone(alias, url, destination):
        make_bare_repo(destination)

    with patch.object(git_mod, "_clone_bare", autospec=True, side_effect=fake_clone) as mock_clone, \
         patch("ow.utils.git.run_cmd") as mock_run_cmd, \
         patch("ow.utils.git._get_bare_config", return_value={}):
        ensure_bare_repo("community", remotes, bare_repos_dir)

    # Cloned to one side, then moved in: the final path only ever holds a
    # repository that finished cloning.
    mock_clone.assert_called_once_with(
        "community", "git@github.com:odoo/odoo.git", bare_repos_dir / "community.git.incoming",
    )
    assert (bare_repo / "HEAD").exists()
    assert not (bare_repos_dir / "community.git.incoming").exists()

    # Also turns on reflogs for the bare repo (needed for --fork-point).
    assert mock_run_cmd.call_args_list[0] == call(
        ["git", "-C", str(bare_repo), "config", "core.logAllRefUpdates", "true"],
        quiet=True, check=True, label="community",
    )


def test_ensure_bare_repo_repairs_a_directory_that_is_not_a_repository(tmp_path, xdg, git_lab):
    """A directory at the path is not proof that a clone ever succeeded.

    Trusting bare_repo.exists() means a SIGKILLed clone, or anything else that
    left a directory there, poisons that repo for good: every later run dies
    with "fatal: not in a git directory" and no ow command repairs it.

    The origin is a repository in tmp_path, so this never leaves the machine.
    """
    bare_repos_dir = tmp_path / "bare-repos"
    bare_repo = bare_repos_dir / "community.git"
    bare_repo.mkdir(parents=True)
    (bare_repo / "half-a-clone").write_text("junk")
    remotes = {"origin": RemoteConfig(url=str(git_lab.path))}

    ensure_bare_repo("community", remotes, bare_repos_dir)

    assert subprocess.run(
        ["git", "-C", str(bare_repo), "rev-parse", "--absolute-git-dir"],
        capture_output=True, text=True,
    ).stdout.strip() == str(bare_repo)
    # The repair is a real clone, so it keeps the flags that make the clone
    # cheap — the old self-heal-by-fetch silently pulled full history.
    assert subprocess.run(
        ["git", "-C", str(bare_repo), "config", "--get", "remote.origin.partialclonefilter"],
        capture_output=True, text=True,
    ).stdout.strip() == "blob:none"
    # The remnant is moved aside, not deleted: ow did not put it there and
    # cannot know it holds nothing of the user's.
    assert (bare_repos_dir / "community.git.broken" / "half-a-clone").read_text() == "junk"


def test_ensure_bare_repo_clears_a_leftover_staging_directory(tmp_path, xdg, git_lab):
    """The run that was killed mid-clone left its staging directory behind, and
    git refuses to clone into a non-empty one. Nothing else would ever remove
    it, so the alias would be stuck exactly as it was before staging existed."""
    bare_repos_dir = tmp_path / "bare-repos"
    leftover = bare_repos_dir / "community.git.incoming"
    leftover.mkdir(parents=True)
    (leftover / "objects").mkdir()
    remotes = {"origin": RemoteConfig(url=str(git_lab.path))}

    ensure_bare_repo("community", remotes, bare_repos_dir)

    assert (bare_repos_dir / "community.git" / "HEAD").exists()
    assert not leftover.exists()


def test_ensure_bare_repo_is_not_fooled_by_an_enclosing_repository(tmp_path, xdg, git_lab):
    """git resolves a repository by walking upwards, so "is this a repo?" asked
    of a plain directory answers yes for anyone who keeps their home directory
    in git — a common dotfiles habit, and ow's repos live under $HOME."""
    enclosing = tmp_path / "home"
    enclosing.mkdir()
    subprocess.run(["git", "init", "-q", str(enclosing)], check=True)
    bare_repos_dir = enclosing / "repos"
    bare_repo = bare_repos_dir / "community.git"
    bare_repo.mkdir(parents=True)
    remotes = {"origin": RemoteConfig(url=str(git_lab.path))}

    ensure_bare_repo("community", remotes, bare_repos_dir)

    assert subprocess.run(
        ["git", "-C", str(bare_repo), "rev-parse", "--absolute-git-dir"],
        capture_output=True, text=True,
    ).stdout.strip() == str(bare_repo)


def test_ensure_bare_repo_keeps_a_repository_reached_through_a_symlink(tmp_path, xdg, git_lab):
    """The repos directory is built from XDG_DATA_HOME, which is not resolved,
    while git answers with symlinks resolved. On any machine whose home
    traverses a symlink (/home -> /var/home) a healthy repository would be
    called "not a repository", re-cloned over, and the copy holding the user's
    unpushed commits renamed to .broken — then deleted by the run after that.
    """
    real = tmp_path / "real"
    bare_repos_dir = real / "bare-repos"
    bare_repo = bare_repos_dir / "community.git"
    make_bare_repo(bare_repo)
    subprocess.run(
        ["git", "-C", str(bare_repo), "fetch", "-q", str(git_lab.path), "master:refs/heads/work"],
        check=True,
    )
    unpushed = git_lab.sha("master")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    remotes = {"origin": RemoteConfig(url=str(git_lab.path))}

    with patch.object(git_mod, "_clone_bare", autospec=True) as mock_clone:
        ensure_bare_repo("community", remotes, link / "bare-repos")

    mock_clone.assert_not_called()
    assert not (bare_repos_dir / "community.git.broken").exists()
    assert subprocess.run(
        ["git", "-C", str(bare_repo), "rev-parse", "refs/heads/work"],
        capture_output=True, text=True,
    ).stdout.strip() == unpushed


def test_clone_bare_into_place_refuses_to_displace_a_real_repository(tmp_path, xdg, git_lab):
    """Defence in depth for the rename above: _clone_bare_into_place is only
    reached because a predicate said "not a repository", and one wrong answer
    from it must not be enough to lose work. Ask git again, by a route that
    does not compare paths at all, and stop rather than displace what it
    recognises. A false refusal costs a message; a false repair costs commits.
    """
    bare_repos_dir = tmp_path / "bare-repos"
    bare_repo = bare_repos_dir / "community.git"
    make_bare_repo(bare_repo)

    with patch.object(git_mod, "_clone_bare", autospec=True) as mock_clone, \
         pytest.raises(RuntimeError, match="is a git repository"):
        git_mod._clone_bare_into_place("community", str(git_lab.path), bare_repo)

    mock_clone.assert_not_called()
    assert (bare_repo / "HEAD").exists()
    assert not (bare_repos_dir / "community.git.broken").exists()


def test_clone_bare_into_place_refuses_to_delete_a_repository_left_at_broken(tmp_path, xdg, git_lab):
    """The .broken slot holds one directory and the next repair rmtree()s it.
    A user repaired by an ow that had the symlink bug has their real repository
    sitting there right now, so that rmtree is a delete of the user's only
    copy. Refuse and say so; the user can move it out of the way in a second.
    """
    bare_repos_dir = tmp_path / "bare-repos"
    bare_repo = bare_repos_dir / "community.git"
    bare_repo.mkdir(parents=True)
    (bare_repo / "half-a-clone").write_text("junk")
    make_bare_repo(bare_repos_dir / "community.git.broken")

    with patch.object(git_mod, "_clone_bare", autospec=True) as mock_clone, \
         pytest.raises(RuntimeError, match="community.git.broken"):
        git_mod._clone_bare_into_place("community", str(git_lab.path), bare_repo)

    mock_clone.assert_not_called()
    assert (bare_repos_dir / "community.git.broken" / "HEAD").exists()
    assert (bare_repo / "half-a-clone").read_text() == "junk"


def test_ensure_bare_repo_leaves_nothing_at_the_final_path_when_a_clone_dies(tmp_path, xdg):
    """A clone that is killed partway must not leave a half-repo behind.

    Cloning straight to the final path means whatever git managed to write
    before the SIGKILL becomes the thing every later run trusts.
    """
    bare_repos_dir = tmp_path / "bare-repos"
    bare_repos_dir.mkdir()
    remotes = {"origin": RemoteConfig(url="git@github.com:odoo/odoo.git")}

    def half_a_clone(alias, url, destination):
        destination.mkdir(parents=True)
        (destination / "objects").mkdir()
        raise KeyboardInterrupt

    with patch.object(git_mod, "_clone_bare", autospec=True, side_effect=half_a_clone), \
         pytest.raises(KeyboardInterrupt):
        ensure_bare_repo("community", remotes, bare_repos_dir)

    assert not (bare_repos_dir / "community.git").exists()


def test_ensure_bare_repo_names_the_config_file_when_the_repo_is_undefined(tmp_path, xdg):
    """The user's mistake is a missing [remotes.<alias>] section, so say that,
    and say which file it is missing from. "No origin remote configured" names
    an internal notion of ow's and points at nothing the user can open."""
    bare_repos_dir = tmp_path / "bare-repos"
    bare_repos_dir.mkdir()

    with pytest.raises(ValueError) as excinfo:
        ensure_bare_repo("ghost", {}, bare_repos_dir)

    message = str(excinfo.value)
    assert "references repo 'ghost' but it's not defined in [remotes]" in message
    assert "[remotes.ghost]" in message
    assert str(paths.config_file()) in message


def test_ensure_bare_repo_distinguishes_a_section_that_lacks_an_origin(tmp_path, xdg):
    """A [remotes.ghost] that only defines a fork is a different mistake from
    no section at all, and telling the user to add the section they can already
    see would send them looking in the wrong place."""
    bare_repos_dir = tmp_path / "bare-repos"
    bare_repos_dir.mkdir()
    remotes = {"dev": RemoteConfig(url="git@github.com:odoo-dev/odoo.git")}

    with pytest.raises(ValueError) as excinfo:
        ensure_bare_repo("ghost", remotes, bare_repos_dir)

    message = str(excinfo.value)
    assert "has no origin remote" in message
    assert "not defined in [remotes]" not in message
    assert "[remotes.ghost]" in message
    assert str(paths.config_file()) in message


def test_ensure_bare_repo_reports_git_s_own_message_when_the_clone_fails(tmp_path, xdg, capsys):
    """A failed clone must surface git's diagnosis, not Python's.

    `check=True` alone raises CalledProcessError, whose str is
    "Command '[...]' returned non-zero exit status 128" — the one sentence
    that says nothing about what went wrong. git already said it.

    The url is a path that does not exist, so this never leaves the machine.
    """
    bare_repos_dir = tmp_path / "bare-repos"
    bare_repos_dir.mkdir()
    remotes = {"origin": RemoteConfig(url=str(tmp_path / "nowhere.git"))}

    with pytest.raises(RuntimeError) as excinfo:
        ensure_bare_repo("community", remotes, bare_repos_dir)

    message = str(excinfo.value)
    assert "does not exist" in message
    assert str(tmp_path / "nowhere.git") in message
    assert "returned non-zero exit status" not in message


def test_ensure_bare_repo_skips_clone_when_exists(tmp_path):
    bare_repos_dir = tmp_path / "bare-repos"
    bare_repo = make_bare_repo(bare_repos_dir / "community.git")

    remotes = {"origin": RemoteConfig(url="git@github.com:odoo/odoo.git")}

    with patch("ow.utils.git.run_cmd") as mock_run_cmd, \
         patch("ow.utils.git._get_bare_config", return_value={}):
        ensure_bare_repo("community", remotes, bare_repos_dir)
    # No clone, but origin.url and reflogs still get configured.
    assert mock_run_cmd.call_count == 2
    assert mock_run_cmd.call_args_list[0] == call(
        ["git", "-C", str(bare_repo), "config", "core.logAllRefUpdates", "true"],
        quiet=True, check=True, label="community",
    )
    assert mock_run_cmd.call_args_list[1] == call(
        ["git", "-C", str(bare_repo), "config", "remote.origin.url", "git@github.com:odoo/odoo.git"],
        quiet=True, check=True, label="community",
    )


def test_ensure_bare_repo_configures_extra_remotes(tmp_path):
    bare_repos_dir = tmp_path / "bare-repos"
    bare_repo = make_bare_repo(bare_repos_dir / "community.git")

    remotes = {
        "origin": RemoteConfig(url="git@github.com:odoo/odoo.git"),
        "dev": RemoteConfig(
            url="git@github.com:odoo-dev/odoo.git",
            pushurl="git@github.com:odoo-dev/odoo.git",
            fetch="+refs/heads/*:refs/remotes/dev/*",
        ),
    }

    with patch("ow.utils.git.run_cmd") as mock_run_cmd, \
         patch("ow.utils.git._get_bare_config", return_value={}):
        ensure_bare_repo("community", remotes, bare_repos_dir)

    calls = mock_run_cmd.call_args_list
    assert len(calls) == 5
    assert calls[0] == call(
        ["git", "-C", str(bare_repo), "config", "core.logAllRefUpdates", "true"],
        quiet=True, check=True, label="community",
    )
    assert calls[1] == call(
        ["git", "-C", str(bare_repo), "config", "remote.origin.url", "git@github.com:odoo/odoo.git"],
        quiet=True, check=True, label="community",
    )
    assert calls[2] == call(
        ["git", "-C", str(bare_repo), "config", "remote.dev.url", "git@github.com:odoo-dev/odoo.git"],
        quiet=True, check=True, label="community",
    )
    assert calls[3] == call(
        ["git", "-C", str(bare_repo), "config", "remote.dev.pushurl", "git@github.com:odoo-dev/odoo.git"],
        quiet=True, check=True, label="community",
    )
    assert calls[4] == call(
        ["git", "-C", str(bare_repo), "config", "remote.dev.fetch", "+refs/heads/*:refs/remotes/dev/*"],
        quiet=True, check=True, label="community",
    )


def test_ensure_bare_repo_configures_origin_pushurl_and_fetch(tmp_path):
    """Origin pushurl and fetch must be configured alongside the url."""
    bare_repos_dir = tmp_path / "bare-repos"
    bare_repo = make_bare_repo(bare_repos_dir / "community.git")

    remotes = {
        "origin": RemoteConfig(
            url="git@github.com:odoo/odoo.git",
            pushurl="git@github.com:my-fork/odoo.git",
            fetch="+refs/heads/*:refs/remotes/origin/*",
        ),
    }

    with patch("ow.utils.git.run_cmd") as mock_run_cmd, \
         patch("ow.utils.git._get_bare_config", return_value={}):
        ensure_bare_repo("community", remotes, bare_repos_dir)

    calls = mock_run_cmd.call_args_list
    # url, pushurl and fetch SHOULD be set, after the reflog config write
    assert calls[0] == call(
        ["git", "-C", str(bare_repo), "config", "core.logAllRefUpdates", "true"],
        quiet=True, check=True, label="community",
    )
    assert calls[1] == call(
        ["git", "-C", str(bare_repo), "config", "remote.origin.url", "git@github.com:odoo/odoo.git"],
        quiet=True, check=True, label="community",
    )
    assert calls[2] == call(
        ["git", "-C", str(bare_repo), "config", "remote.origin.pushurl", "git@github.com:my-fork/odoo.git"],
        quiet=True, check=True, label="community",
    )
    assert calls[3] == call(
        ["git", "-C", str(bare_repo), "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"],
        quiet=True, check=True, label="community",
    )


def test_ensure_bare_repo_ordered_remotes(tmp_path):
    """Remotes are configured in alphabetical order, origin first."""
    bare_repos_dir = tmp_path / "bare-repos"
    bare_repo = make_bare_repo(bare_repos_dir / "community.git")

    remotes = {
        "origin": RemoteConfig(url="origin-url"),
        "zebra": RemoteConfig(url="zebra-url"),
        "alpha": RemoteConfig(url="alpha-url"),
    }

    with patch("ow.utils.git.run_cmd") as mock_run_cmd, \
         patch("ow.utils.git._get_bare_config", return_value={}):
        ensure_bare_repo("community", remotes, bare_repos_dir)

    calls = mock_run_cmd.call_args_list
    assert len(calls) == 4
    # reflog config first, then origin, then alpha before zebra
    assert "core.logAllRefUpdates" in calls[0].args[0][-2]
    assert "remote.origin.url" in calls[1].args[0][-2]
    assert "remote.alpha.url" in calls[2].args[0][-2]
    assert "remote.zebra.url" in calls[3].args[0][-2]


# ---------------------------------------------------------------------------
# ensure_ref
# ---------------------------------------------------------------------------

def test_ensure_ref_fetches_when_missing(tmp_path):
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()

    mock_check = MagicMock(returncode=1)

    with patch("ow.utils.git._run", side_effect=[mock_check, MagicMock()]) as mock_run:
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

    with patch("ow.utils.git._run", return_value=mock_check) as mock_run:
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
    mock_result.returncode = 0
    mock_result.stdout = f"worktree {worktree_path}\nHEAD abc1234\nbranch refs/heads/master\n"

    with patch("ow.utils.git._run", return_value=mock_result):
        assert worktree_exists(bare_repo, worktree_path) is True


def test_worktree_exists_false(tmp_path):
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    worktree_path = tmp_path / "workspaces" / "test" / "community"
    worktree_path.mkdir(parents=True)

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "worktree /other/path\nHEAD abc1234\nbranch refs/heads/master\n"

    with patch("ow.utils.git._run", return_value=mock_result):
        assert worktree_exists(bare_repo, worktree_path) is False


def test_worktree_exists_false_when_dir_missing_but_in_git_output(tmp_path):
    """Prunable worktree: git still lists the path but directory no longer exists."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    worktree_path = tmp_path / "workspaces" / "test" / "community"
    # worktree_path is NOT created on disk

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = f"worktree {worktree_path}\nHEAD abc1234\nbranch refs/heads/master\n"

    with patch("ow.utils.git._run", return_value=mock_result):
        assert worktree_exists(bare_repo, worktree_path) is False


def test_worktree_exists_does_not_match_substring(tmp_path):
    """Regression: 'community' must not match a worktree registered as 'community-old'."""
    # Set up a real bare repo with one commit so worktree add works.
    src_repo = tmp_path / "src"
    src_repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(src_repo)], check=True)
    subprocess.run(["git", "-C", str(src_repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(src_repo), "config", "user.name", "T"], check=True)
    (src_repo / "init.txt").write_text("init")
    subprocess.run(["git", "-C", str(src_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(src_repo), "commit", "-q", "-m", "init"], check=True)

    bare_repo = tmp_path / "community.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(src_repo), str(bare_repo)], check=True)

    # Add a worktree at community-old
    wt_old = tmp_path / "community-old"
    subprocess.run(
        ["git", "-C", str(bare_repo), "worktree", "add", "--detach", str(wt_old), "master"],
        check=True,
    )

    # Check that worktree_exists for 'community' (substring of 'community-old') returns False
    wt_short = tmp_path / "community"
    wt_short.mkdir()  # exists on disk so the early-return path doesn't trigger
    assert worktree_exists(bare_repo, wt_short) is False

    # Sanity: the actual path DOES exist
    assert worktree_exists(bare_repo, wt_old) is True


# ---------------------------------------------------------------------------
# create_worktree
# ---------------------------------------------------------------------------

def test_create_worktree_detached(tmp_path):
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    worktree_path = Path("/fake/workspaces/test/community")
    spec = BranchSpec("origin/master", None)

    with patch("ow.utils.git._run") as mock_run:
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

    with patch("ow.utils.git._run", side_effect=[branch_missing, MagicMock(), MagicMock(), MagicMock()]) as mock_run:
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

    with patch("ow.utils.git._run", side_effect=[branch_missing, MagicMock(), MagicMock(), MagicMock()]) as mock_run:
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

    with patch("ow.utils.git._run", side_effect=[branch_exists, MagicMock(), MagicMock(), MagicMock()]) as mock_run:
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
# get_rev_list_count
# ---------------------------------------------------------------------------

def test_get_rev_list_count(tmp_path):
    mock_result = MagicMock()
    mock_result.stdout = "3\t5\n"

    with patch("ow.utils.git._run", return_value=mock_result):
        ahead, behind = get_rev_list_count(tmp_path, "HEAD", "origin/master")

    assert ahead == 3
    assert behind == 5


def test_get_rev_list_count_zero(tmp_path):
    mock_result = MagicMock()
    mock_result.stdout = "0\t0\n"

    with patch("ow.utils.git._run", return_value=mock_result):
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

    with patch("ow.utils.git._run", return_value=mock_result):
        short, full = get_worktree_head(tmp_path)

    assert short == "a1b2c3d"
    assert full == full_hash


# ---------------------------------------------------------------------------
# get_upstream
# ---------------------------------------------------------------------------

def test_get_upstream_returns_ref(tmp_path):
    mock_result = MagicMock(returncode=0)
    mock_result.stdout = "dev/master-canary\n"

    with patch("ow.utils.git._run", return_value=mock_result):
        result = get_upstream(tmp_path)

    assert result == "dev/master-canary"


def test_get_upstream_returns_none_when_no_upstream(tmp_path):
    mock_result = MagicMock(returncode=128)
    mock_result.stdout = ""

    with patch("ow.utils.git._run", return_value=mock_result):
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

    with patch("ow.utils.git._run", return_value=rev_parse_ok) as mock_run:
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

    with patch("ow.utils.git._run", side_effect=[
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

    with patch("ow.utils.git._run", side_effect=[
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

    with patch("ow.utils.git._run", side_effect=[
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

    with patch("ow.utils.git._run", side_effect=[
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

    with patch("ow.utils.git._run", return_value=always_fail):
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

    refs = {"origin/master"}
    result = resolve_spec_local(bare_repo, spec, remotes, refs=refs)

    assert result.remote == "origin"
    assert result.branch == "master"
    assert result.local_branch is None


def test_resolve_spec_local_found_on_fallback_remote(tmp_path):
    """Branch not on spec.remote but found in local refs on fallback remote."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    spec = BranchSpec("origin/master-parrot", None)
    remotes = {
        "origin": RemoteConfig(url="git@github.com:odoo/odoo.git"),
        "dev": RemoteConfig(url="git@github.com:odoo-dev/odoo.git"),
    }

    refs = {"dev/master-parrot"}
    result = resolve_spec_local(bare_repo, spec, remotes, refs=refs)

    assert result.remote == "dev"
    assert result.branch == "master-parrot"


def test_resolve_spec_local_prefers_the_spec_remote_over_the_configured_order(tmp_path):
    """When several remotes carry the branch, the one the spec names wins.

    ordered_remotes puts origin first, so a spec of "dev/master" resolved
    against a repo where both origin/master and dev/master exist would silently
    become origin/master if the spec's own remote were not tried first — the
    worktree would then track, and rebase onto, the wrong fork.
    """
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    spec = BranchSpec("dev/master", None)
    remotes = {
        "origin": RemoteConfig(url="git@github.com:odoo/odoo.git"),
        "dev": RemoteConfig(url="git@github.com:odoo-dev/odoo.git"),
    }

    refs = {"origin/master", "dev/master"}
    result = resolve_spec_local(bare_repo, spec, remotes, refs=refs)

    assert result.base_ref == "dev/master"
    assert result.remote == "dev"


def test_resolve_spec_local_raises_when_not_found(tmp_path):
    """RuntimeError raised when branch not found in any local refs (no fetch attempted)."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    spec = BranchSpec("origin/nonexistent", None)
    remotes = {"origin": RemoteConfig(url="git@github.com:odoo/odoo.git")}

    refs: set[str] = set()
    with pytest.raises(RuntimeError, match="nonexistent"):
        resolve_spec_local(bare_repo, spec, remotes, refs=refs)


# ---------------------------------------------------------------------------
# set_branch_upstream
# ---------------------------------------------------------------------------

def test_set_branch_upstream(tmp_path):
    """Writes branch.X.remote and branch.X.merge config keys."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()

    with patch("ow.utils.git._run") as mock_run:
        set_branch_upstream(bare_repo, "master-feature", "origin", "master")

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

    with patch("ow.utils.git._run") as mock_run:
        set_branch_upstream(bare_repo, "master-parrot-ring-the-phone", "dev", "master-parrot-ring-the-phone")

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

    with patch("ow.utils.git._run", return_value=mock_result):
        assert worktree_is_detached(worktree_path) is True


def test_worktree_is_detached_returns_false(tmp_path):
    """Returns False when symbolic-ref exits zero (HEAD is on a branch)."""
    worktree_path = tmp_path / "workspaces" / "test" / "community"
    worktree_path.mkdir(parents=True)

    mock_result = MagicMock(returncode=0)

    with patch("ow.utils.git._run", return_value=mock_result):
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

    with patch("ow.utils.git._run", side_effect=[branch_missing, MagicMock(), MagicMock(), MagicMock()]) as mock_run:
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

    with patch("ow.utils.git._run", side_effect=[branch_exists, MagicMock(), MagicMock(), MagicMock()]) as mock_run:
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

    with patch("ow.utils.git._run") as mock_run:
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

    with patch("ow.utils.git._run", side_effect=[
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

    with patch("ow.utils.git._run", side_effect=[
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

    refs = {"origin/18.0-add-voip-telnyx-service-basm", "iap-apps/18.0-add-voip-telnyx-service-basm"}
    result = get_remote_ref_for_branch(
        bare_repo, "18.0-add-voip-telnyx-service-basm", alias_remotes, refs=refs,
    )

    assert result == "origin/18.0-add-voip-telnyx-service-basm"


def test_get_remote_ref_for_branch_found_on_second_remote(tmp_path):
    """Skips first remote (miss) and returns match on second."""
    bare_repo = tmp_path / "enterprise.git"
    bare_repo.mkdir()
    alias_remotes = {
        "origin": RemoteConfig(url="git@github.com:odoo/enterprise.git"),
        "dev": RemoteConfig(url="git@github.com:odoo-dev/enterprise.git"),
    }

    refs = {"dev/master-parrot"}
    result = get_remote_ref_for_branch(bare_repo, "master-parrot", alias_remotes, refs=refs)

    assert result == "dev/master-parrot"


def test_get_remote_ref_for_branch_excludes_base_ref(tmp_path):
    """exclude_ref skips the candidate even if the ref exists."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    alias_remotes = {
        "origin": RemoteConfig(url="git@github.com:odoo/odoo.git"),
        "iap-apps": RemoteConfig(url="git@github.com:odoo-ps/ps-tech-iap-apps.git"),
    }

    refs = {"origin/18.0"}
    result = get_remote_ref_for_branch(
        bare_repo, "18.0", alias_remotes, exclude_ref="origin/18.0", refs=refs,
    )

    # origin/18.0 skipped (excluded); iap-apps/18.0 not found
    assert result is None


def test_get_remote_ref_for_branch_returns_none_when_not_found(tmp_path):
    """Returns None when no configured remote has the branch."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    alias_remotes = {
        "origin": RemoteConfig(url="git@github.com:odoo/odoo.git"),
        "dev": RemoteConfig(url="git@github.com:odoo-dev/odoo.git"),
    }

    refs: set[str] = set()
    result = get_remote_ref_for_branch(bare_repo, "18.0-nonexistent", alias_remotes, refs=refs)

    assert result is None


def test_get_remote_ref_for_branch_prefers_non_base_remote(tmp_path):
    """With base_remote set, fork remote is checked before base remote."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    alias_remotes = {
        "origin": RemoteConfig(url="git@github.com:odoo/odoo.git"),
        "dev": RemoteConfig(url="git@github.com:odoo-dev/odoo.git"),
    }

    refs = {"dev/master-parrot", "origin/master-parrot"}
    result = get_remote_ref_for_branch(
        bare_repo, "master-parrot", alias_remotes,
        exclude_ref="origin/master", base_remote="origin", refs=refs,
    )

    assert result == "dev/master-parrot"


def test_get_remote_ref_for_branch_falls_back_to_base_remote(tmp_path):
    """Falls back to base remote if no fork remote has the branch."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()
    alias_remotes = {
        "origin": RemoteConfig(url="git@github.com:odoo/iap-apps.git"),
    }

    refs = {"origin/18.0-my-feature", "origin/18.0"}
    result = get_remote_ref_for_branch(
        bare_repo, "18.0-my-feature", alias_remotes,
        exclude_ref="origin/18.0", base_remote="origin", refs=refs,
    )

    assert result == "origin/18.0-my-feature"


# ---------------------------------------------------------------------------
# get_remote_url
# ---------------------------------------------------------------------------

def test_get_remote_url_returns_url(tmp_path):
    """Returns the URL when git remote get-url succeeds."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()

    mock_result = MagicMock(returncode=0)
    mock_result.stdout = "git@github.com:odoo-dev/odoo.git\n"

    with patch("ow.utils.git._run", return_value=mock_result):
        result = get_remote_url(bare_repo, "dev")

    assert result == "git@github.com:odoo-dev/odoo.git"


def test_get_remote_url_returns_none_when_remote_missing(tmp_path):
    """Returns None when the remote is not configured."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()

    mock_result = MagicMock(returncode=128)
    mock_result.stdout = ""

    with patch("ow.utils.git._run", return_value=mock_result):
        result = get_remote_url(bare_repo, "nonexistent")

    assert result is None


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


def test_git_adds_c_flag(tmp_path):
    """git() automatically adds -C flag with repo path."""
    repo = tmp_path / "repo"
    repo.mkdir()

    with patch("ow.utils.git.run_cmd") as mock_run:
        git(repo, "status", check=True)

    mock_run.assert_called_once_with(
        ["git", "-C", str(repo), "status"], quiet=False, label="repo", check=True
    )


def test_git_passes_quiet_flag(tmp_path):
    """git() passes quiet flag to run_cmd."""
    repo = tmp_path / "repo"
    repo.mkdir()

    with patch("ow.utils.git.run_cmd") as mock_run:
        git(repo, "status", quiet=True, check=True)

    mock_run.assert_called_once_with(
        ["git", "-C", str(repo), "status"], quiet=True, label="repo", check=True
    )


# ---------------------------------------------------------------------------
# git_fetch
# ---------------------------------------------------------------------------


def test_git_fetch_basic(tmp_path):
    """git_fetch builds correct fetch command."""
    repo = tmp_path / "repo"
    repo.mkdir()

    with patch("ow.utils.git.git") as mock_git:
        git_fetch(repo, "origin", "master:refs/remotes/origin/master", check=True)

    mock_git.assert_called_once_with(
        repo, "fetch", "origin", "master:refs/remotes/origin/master", check=True
    )


def test_git_fetch_force(tmp_path):
    """git_fetch with force=True prepends + to refspec."""
    repo = tmp_path / "repo"
    repo.mkdir()

    with patch("ow.utils.git.git") as mock_git:
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
# parallel_per_repo
# ---------------------------------------------------------------------------

def test_parallel_per_repo_runs_all_tasks():
    results = parallel_per_repo(
        {"a": lambda: "result_a", "b": lambda: "result_b"},
    )
    assert results == {"a": "result_a", "b": "result_b"}


def test_parallel_per_repo_catches_exceptions():
    def fail():
        raise RuntimeError("boom")

    results = parallel_per_repo(
        {"ok": lambda: 42, "bad": fail},
    )
    assert results["ok"] == 42
    assert isinstance(results["bad"], Exception)
    assert "boom" in str(results["bad"])


def test_parallel_per_repo_keys_results_regardless_of_completion_order():
    """Results are now collected via as_completed (needed so an interrupt has
    somewhere to land — see test_interrupt.py), so a slower task's alias can
    land in the dict after a faster one's. Every caller looks results up by
    alias rather than relying on insertion order, so only the mapping matters."""
    import time

    def slow():
        time.sleep(0.05)
        return "slow"

    results = parallel_per_repo(
        {"first": slow, "second": lambda: "fast"},
    )
    assert results == {"first": "slow", "second": "fast"}


def test_parallel_per_repo_empty_tasks():
    results = parallel_per_repo({})
    assert results == {}
