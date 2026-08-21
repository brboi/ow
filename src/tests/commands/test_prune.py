import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from ow.commands import cmd_prune
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


def test_cmd_prune_reports_nothing_for_a_duplicated_live_entry(tmp_path, capsys, xdg):
    """A duplicate raw line is internal hygiene, not a death.

    Two concurrent remember() calls are a read-modify-write race, so the raw
    index file can end up listing the same live, still-existing workspace
    twice. Deduplicating that is not "dropping a dead entry" and must not be
    reported as one.
    """
    config = _make_config()
    live = _make_indexed_workspace(tmp_path, "live")

    target = paths.index_file()
    target.write_text(target.read_text() + f"{live.resolve()}\n")

    cmd_prune(config)

    captured = capsys.readouterr()
    assert "index" not in captured.out
    assert index.known_workspaces() == [live.resolve()]


def test_cmd_prune_counts_only_workspaces_that_vanished(tmp_path, capsys, xdg):
    """A duplicate of a live entry must not inflate the death count.

    Guards against a naive fix that just subtracts "lines removed by
    dedup" from the old line-count subtraction: with two genuinely dead
    entries and one duplicate of a live entry, the correct count is 2, not
    3 (line-count subtraction) and not 1 (subtracting only one duplicate).
    """
    config = _make_config()
    live = _make_indexed_workspace(tmp_path, "live")
    dead1 = _make_indexed_workspace(tmp_path, "dead1")
    dead2 = _make_indexed_workspace(tmp_path, "dead2")
    shutil.rmtree(dead1)
    shutil.rmtree(dead2)

    target = paths.index_file()
    target.write_text(target.read_text() + f"{live.resolve()}\n")

    cmd_prune(config)

    captured = capsys.readouterr()
    assert "Dropped 2 dead index entries" in captured.out
    assert index.known_workspaces() == [live.resolve()]



# ---------------------------------------------------------------------------
# Real bare repos
#
# prune deletes refs. Mocking subprocess proves nothing about which commits
# survive, and both of the bugs these tests pin — a colorized branch listing
# and an unchecked delete exit code — are invisible to a mock that answers
# every call with returncode=0 and an empty stdout.
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _bare_repo(tmp_path: Path, alias: str = "community") -> Path:
    """A real bare repo where ow keeps them, mirroring what `ow init` leaves behind.

    Requires the xdg fixture: repos_dir() must already point into tmp_path.
    """
    src = tmp_path / "origin" / alias
    src.mkdir(parents=True)
    _git_init(src)
    (src / "a.txt").write_text("a")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "A")

    repos = paths.repos_dir()
    repos.mkdir(parents=True, exist_ok=True)
    bare = repos / f"{alias}.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(src), str(bare)],
        capture_output=True, text=True, check=True,
    )
    _git(bare, "config", "user.email", "t@t")
    _git(bare, "config", "user.name", "T")
    # ow fetches base refs into refs/remotes/<remote>/<branch> with explicit
    # refspecs; `clone --bare` alone leaves none behind.
    _git(bare, "update-ref", "refs/remotes/origin/master", "refs/heads/master")
    return bare


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "master"], check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "T")


def _branches(bare: Path) -> list[str]:
    out = _git(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    return out.splitlines() if out else []


def test_prune_reads_branch_names_uncolorized(tmp_path, capsys, xdg):
    """`git branch --list` honours color.ui=always; a name is not a display string.

    With colour on, the listing yields "\x1b[32morphanbr\x1b[m", which no
    amount of stripping "*+ " repairs — git is then asked to delete a branch
    that does not exist, and the user is told it was deleted.
    """
    bare = _bare_repo(tmp_path)
    _git(bare, "config", "color.ui", "always")
    _git(bare, "branch", "orphanbr", "master")

    cmd_prune(_make_config())

    out = capsys.readouterr().out
    assert "\x1b[" not in out
    assert "orphanbr" not in _branches(bare)


def test_prune_does_not_delete_a_branch_held_by_a_live_worktree(tmp_path, capsys, xdg):
    """The colour bug's near-miss: a used branch landed in the delete set too.

    Nothing but the mangled name failing to resolve saved it. Read the names
    properly and the branch must be excluded on its merits.
    """
    bare = _bare_repo(tmp_path)
    _git(bare, "config", "color.ui", "always")
    _git(bare, "worktree", "add", "-q", str(tmp_path / "ws" / "community"), "-b", "featA", "master")

    cmd_prune(_make_config())

    assert "featA" in _branches(bare)
    assert "featA" not in capsys.readouterr().out


def test_prune_reports_only_the_branches_it_actually_deleted(tmp_path, capsys, xdg):
    """A refused delete must not be reported as a deletion.

    Making refs/heads read-only is the cheapest real refusal: git cannot
    take the ref lock, exits non-zero, and the branch survives. Reporting it
    as gone sends the user looking for work that is still there.
    """
    bare = _bare_repo(tmp_path)
    _git(bare, "branch", "orphanbr", "master")

    refs_heads = bare / "refs" / "heads"
    refs_heads.chmod(0o555)
    try:
        cmd_prune(_make_config())
    finally:
        refs_heads.chmod(0o755)

    out = capsys.readouterr().out
    assert "orphanbr" in _branches(bare)
    assert "deleted orphaned branches" not in out
