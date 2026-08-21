import shutil
import subprocess
from pathlib import Path

from ow.commands import cmd_prune
from ow.utils import index, paths


# ---------------------------------------------------------------------------
# cmd_prune
# ---------------------------------------------------------------------------

def test_cmd_prune_no_bare_repos(tmp_path, capsys, xdg):
    cmd_prune()
    captured = capsys.readouterr()
    assert "No bare repos found" in captured.out


def test_cmd_prune_visits_every_bare_repo(tmp_path, capsys, xdg):
    bare_a = _bare_repo(tmp_path, "community")
    bare_b = _bare_repo(tmp_path, "enterprise")
    _git(bare_a, "branch", "spent-a", "refs/remotes/origin/master")
    _git(bare_b, "branch", "spent-b", "refs/remotes/origin/master")

    cmd_prune(yes=True)

    assert "spent-a" not in _branches(bare_a)
    assert "spent-b" not in _branches(bare_b)
    out = capsys.readouterr().out
    assert "[community]" in out
    assert "[enterprise]" in out


def _make_indexed_workspace(tmp_path, name: str):
    """A workspace directory with a .ow/config.toml marker, remembered in the index."""
    ws = tmp_path / "workspaces" / name
    (ws / ".ow").mkdir(parents=True)
    (ws / ".ow" / "config.toml").write_text("")
    index.remember(ws)
    return ws


def test_cmd_prune_drops_dead_index_entries(tmp_path, capsys, xdg):

    live = _make_indexed_workspace(tmp_path, "live")
    dead1 = _make_indexed_workspace(tmp_path, "dead1")
    dead2 = _make_indexed_workspace(tmp_path, "dead2")

    # The workspaces vanish (e.g. the directory was deleted by hand) after
    # being remembered, but before the index is ever re-read — so the raw
    # file still holds all three entries when cmd_prune runs.
    shutil.rmtree(dead1)
    shutil.rmtree(dead2)

    cmd_prune()

    captured = capsys.readouterr()
    assert "Dropped 2 dead index entries" in captured.out
    assert index.known_workspaces() == [live.resolve()]


def test_cmd_prune_reports_nothing_when_index_is_clean(tmp_path, capsys, xdg):
    _make_indexed_workspace(tmp_path, "live")

    cmd_prune()

    captured = capsys.readouterr()
    assert "index" not in captured.out


def test_cmd_prune_reports_nothing_for_a_duplicated_live_entry(tmp_path, capsys, xdg):
    """A duplicate raw line is internal hygiene, not a death.

    Two concurrent remember() calls are a read-modify-write race, so the raw
    index file can end up listing the same live, still-existing workspace
    twice. Deduplicating that is not "dropping a dead entry" and must not be
    reported as one.
    """
    live = _make_indexed_workspace(tmp_path, "live")

    target = paths.index_file()
    target.write_text(target.read_text() + f"{live.resolve()}\n")

    cmd_prune()

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
    live = _make_indexed_workspace(tmp_path, "live")
    dead1 = _make_indexed_workspace(tmp_path, "dead1")
    dead2 = _make_indexed_workspace(tmp_path, "dead2")
    shutil.rmtree(dead1)
    shutil.rmtree(dead2)

    target = paths.index_file()
    target.write_text(target.read_text() + f"{live.resolve()}\n")

    cmd_prune()

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

    cmd_prune(yes=True)

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

    cmd_prune(yes=True)

    assert "featA" in _branches(bare)
    assert "featA" not in capsys.readouterr().out


def test_prune_reports_only_the_branches_it_actually_deleted(tmp_path, capsys, xdg):
    """A refused delete must not be reported as a deletion.

    A stale `.lock` beside the ref is the cheapest real refusal: git checks
    for it before every ref update, exits non-zero, and the branch survives.
    Reporting it as gone sends the user looking for work that is still there.
    A read-only refs/heads would be simpler still, but permissions do not
    stop root — and CI containers routinely run as root.
    """
    bare = _bare_repo(tmp_path)
    _git(bare, "branch", "orphanbr", "master")
    _git(bare, "branch", "-D", "master")

    lock = bare / "refs" / "heads" / "orphanbr.lock"
    lock.write_text("")
    try:
        cmd_prune(yes=True)
    finally:
        lock.unlink()

    captured = capsys.readouterr()
    assert "orphanbr" in _branches(bare)
    assert "could not delete: orphanbr" in captured.err
    assert "could not delete" not in captured.out


def _commit_on_branch(bare: Path, tmp_path: Path, branch: str, name: str) -> str:
    """Create <branch> off master with one commit on it, reachable from nowhere else."""
    scratch = tmp_path / "scratch"
    _git(bare, "worktree", "add", "-q", str(scratch), "-b", branch, "master")
    (scratch / f"{name}.txt").write_text(name)
    _git(scratch, "add", "-A")
    _git(scratch, "commit", "-qm", name)
    sha = _git(scratch, "rev-parse", "HEAD")
    _git(bare, "worktree", "remove", "--force", str(scratch))
    return sha


def test_prune_keeps_a_branch_whose_commits_exist_nowhere_else(tmp_path, capsys, xdg):
    """The whole point. A ref is the only thing holding an unpushed commit alive.

    Delete it and the commit has no ref and, in a bare repo, no reflog
    either — it survives until the next gc, findable only through
    `git fsck --lost-found`, which nothing tells the user about. "Orphaned"
    describes the worktree that is gone, not the work that is still there.
    """
    bare = _bare_repo(tmp_path)
    sha = _commit_on_branch(bare, tmp_path, "featA", "work")

    cmd_prune(yes=True)

    assert "featA" in _branches(bare)
    assert _git(bare, "rev-parse", "featA") == sha


def test_prune_deletes_a_branch_already_contained_in_a_remote_ref(tmp_path, capsys, xdg):
    """Refusing everything would just be a broken command.

    A branch whose tip a remote already has holds nothing that deleting it
    can lose, so it still goes.
    """
    bare = _bare_repo(tmp_path)
    _git(bare, "branch", "spent", "refs/remotes/origin/master")
    _git(bare, "branch", "-D", "master")

    cmd_prune(yes=True)

    assert "spent" not in _branches(bare)
    out = capsys.readouterr().out
    assert "delete 1 orphaned branch: spent" in out


def test_prune_says_which_branches_it_refused_and_why(tmp_path, capsys, xdg):
    """Silently keeping them is better than deleting, but still not honest.

    The user asked for a cleanup and must be told what the cleanup left
    behind, by name, and what would make it go.
    """
    bare = _bare_repo(tmp_path)
    _commit_on_branch(bare, tmp_path, "featA", "work")

    cmd_prune(yes=True)

    out = capsys.readouterr().out
    assert "featA" in out
    assert "unpushed" in out
    assert "All bare repos are clean." not in out


def test_moving_a_workspace_directory_does_not_destroy_its_unpushed_commits(tmp_path, capsys, xdg):
    """The reported reproduction, end to end.

    `git worktree prune` unregisters the worktree whose directory moved, so
    its branch stops being "used" and becomes indistinguishable from an
    abandoned one. The same thing happens for a workspace on an unmounted
    disk or a dead fuse mount, where the directory is coming back.
    """
    bare = _bare_repo(tmp_path)
    ws = tmp_path / "wsA" / "community"
    _git(bare, "worktree", "add", "-q", str(ws), "-b", "featA", "master")
    (ws / "work.txt").write_text("work")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "work")
    sha = _git(ws, "rev-parse", "HEAD")

    (tmp_path / "wsA").rename(tmp_path / "wsA-moved")

    cmd_prune(yes=True)

    assert "featA" in _branches(bare)
    assert _git(bare, "rev-parse", "featA") == sha


def _registered_worktrees(bare: Path) -> list[str]:
    out = _git(bare, "worktree", "list", "--porcelain")
    return [
        line.split(" ", 1)[1]
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]


def test_prune_reports_the_stale_worktrees_it_unregistered(tmp_path, capsys, xdg):
    """`git worktree prune` writes nothing to stdout without --verbose.

    So the old check — "did prune print anything?" — was never true and the
    line could not be reached: prune would unregister a stale worktree and
    then say nothing at all about having done it.
    """
    bare = _bare_repo(tmp_path)
    ws = tmp_path / "wsA" / "community"
    _git(bare, "worktree", "add", "-q", str(ws), "-b", "featA", "master")
    shutil.rmtree(tmp_path / "wsA")

    cmd_prune(yes=True)

    out = capsys.readouterr().out
    assert "stale worktree" in out
    assert str(ws) in out
    assert str(ws) not in _registered_worktrees(bare)


def test_prune_says_nothing_about_worktrees_when_none_are_stale(tmp_path, capsys, xdg):
    """A live worktree is not something prune removed, and must not be claimed."""
    bare = _bare_repo(tmp_path)
    _git(bare, "worktree", "add", "-q", str(tmp_path / "wsA" / "community"), "-b", "featA", "master")
    _git(bare, "branch", "-D", "master")

    cmd_prune()

    out = capsys.readouterr().out
    assert "stale worktree" not in out
    assert "All bare repos are clean." in out


# ---------------------------------------------------------------------------
# --dry-run and the confirmation
#
# Wired at the cmd_prune level; the Typer options belong to __main__.py.
# ---------------------------------------------------------------------------

def _refuse_input(monkeypatch, reason: str = "prune must not prompt here"):
    def _boom(prompt: str = "") -> str:
        raise AssertionError(reason)
    monkeypatch.setattr("builtins.input", _boom)


def _answer(monkeypatch, reply: str):
    monkeypatch.setattr("builtins.input", lambda prompt="": reply)


def _dirty_repo(tmp_path) -> Path:
    """A bare repo with all three kinds of work pending: stale worktree, dead branch, live one."""
    bare = _bare_repo(tmp_path)
    ws = tmp_path / "wsA" / "community"
    _git(bare, "worktree", "add", "-q", str(ws), "-b", "featA", "master")
    shutil.rmtree(tmp_path / "wsA")
    _git(bare, "branch", "spent", "refs/remotes/origin/master")
    return bare


def test_prune_dry_run_changes_nothing(tmp_path, capsys, xdg, monkeypatch):
    """Seeing what would go must not be a way to make it go."""
    _refuse_input(monkeypatch, "--dry-run must not prompt")
    bare = _dirty_repo(tmp_path)
    dead = _make_indexed_workspace(tmp_path, "dead")
    shutil.rmtree(dead)
    before = paths.index_file().read_text()

    cmd_prune(dry_run=True)

    assert "spent" in _branches(bare)
    assert str(tmp_path / "wsA" / "community") in _registered_worktrees(bare)
    assert paths.index_file().read_text() == before


def test_prune_dry_run_names_the_commands_it_would_run(tmp_path, capsys, xdg, monkeypatch):
    _refuse_input(monkeypatch, "--dry-run must not prompt")
    _dirty_repo(tmp_path)
    dead = _make_indexed_workspace(tmp_path, "dead")
    shutil.rmtree(dead)

    cmd_prune(dry_run=True)

    out = capsys.readouterr().out
    assert "Would run:" in out
    assert "git branch -D spent" in out
    assert "git worktree prune" in out
    assert "Would drop 1 dead index entry" in out


def test_prune_asks_before_deleting_and_a_no_changes_nothing(tmp_path, capsys, xdg, monkeypatch):
    """`ow rebase` already defaults to no. The command that deletes refs cannot ask for less."""
    _answer(monkeypatch, "n")
    bare = _dirty_repo(tmp_path)
    dead = _make_indexed_workspace(tmp_path, "dead")
    shutil.rmtree(dead)
    before = paths.index_file().read_text()

    cmd_prune()

    out = capsys.readouterr().out
    assert "Aborted." in out
    assert "spent" in _branches(bare)
    assert str(tmp_path / "wsA" / "community") in _registered_worktrees(bare)
    assert paths.index_file().read_text() == before


def test_prune_treats_eof_as_no(tmp_path, capsys, xdg, monkeypatch):
    """A pipe with nothing on it is not consent."""
    def _eof(prompt: str = "") -> str:
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)
    bare = _dirty_repo(tmp_path)

    cmd_prune()

    assert "Aborted." in capsys.readouterr().out
    assert "spent" in _branches(bare)


def test_prune_proceeds_when_the_answer_is_yes(tmp_path, capsys, xdg, monkeypatch):
    _answer(monkeypatch, "y")
    bare = _dirty_repo(tmp_path)

    cmd_prune()

    assert "spent" not in _branches(bare)
    assert "Aborted." not in capsys.readouterr().out


def test_prune_shows_what_is_at_stake_before_asking(tmp_path, capsys, xdg, monkeypatch):
    """The prompt is worthless if the branch names come after the answer."""
    seen: list[str] = []

    def _record(prompt: str = "") -> str:
        seen.append(capsys.readouterr().out)
        return "n"

    monkeypatch.setattr("builtins.input", _record)
    _dirty_repo(tmp_path)

    cmd_prune()

    assert seen and "spent" in seen[0]


def test_prune_yes_skips_the_prompt(tmp_path, capsys, xdg, monkeypatch):
    _refuse_input(monkeypatch, "yes=True must not prompt")
    bare = _dirty_repo(tmp_path)

    cmd_prune(yes=True)

    assert "spent" not in _branches(bare)


def test_prune_does_not_ask_when_no_branch_would_be_deleted(tmp_path, capsys, xdg, monkeypatch):
    """Unregistering a worktree whose directory is already gone destroys nothing.

    The prompt guards ref deletion, which is the only step that can lose
    work; making the harmless half interactive would just teach the habit of
    answering y without reading.
    """
    _refuse_input(monkeypatch, "nothing deletable — prune must not prompt")
    bare = _bare_repo(tmp_path)
    ws = tmp_path / "wsA" / "community"
    _git(bare, "worktree", "add", "-q", str(ws), "-b", "featA", "master")
    (ws / "work.txt").write_text("work")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "work")
    _git(bare, "branch", "-D", "master")
    shutil.rmtree(tmp_path / "wsA")

    cmd_prune()

    assert str(ws) not in _registered_worktrees(bare)
    assert "featA" in _branches(bare)


# ---------------------------------------------------------------------------
# Mutation holes: assertions that could not distinguish a working prune from
# a deleted one.
# ---------------------------------------------------------------------------

def test_prune_rewrites_the_index_file_itself(tmp_path, capsys, xdg):
    """Assert on the file, not on known_workspaces().

    known_workspaces() prunes as it reads. Calling it to check prune's work
    performs that work, so the assertion passes whether or not prune ever
    rewrote anything — deleting the call from prune left the suite green.
    """
    live = _make_indexed_workspace(tmp_path, "live")
    dead = _make_indexed_workspace(tmp_path, "dead")
    shutil.rmtree(dead)

    cmd_prune()

    assert paths.index_file().read_text().splitlines() == [str(live.resolve())]


def test_prune_counts_a_duplicated_dead_entry_once(tmp_path, capsys, xdg):
    """The dedup has to be doing the deduplicating.

    Duplicating a *live* entry counts zero either way, so it cannot tell a
    working `if candidate in seen: continue` from a missing one. A
    duplicated dead entry can: without the skip it is counted twice.
    """
    dead = _make_indexed_workspace(tmp_path, "dead")
    shutil.rmtree(dead)
    paths.index_file().write_text(f"{dead.resolve()}\n{dead.resolve()}\n")

    cmd_prune()

    assert "Dropped 1 dead index entry" in capsys.readouterr().out


def test_prune_is_not_clean_when_it_deleted_something(tmp_path, capsys, xdg):
    """"All bare repos are clean." after a deletion contradicts the line above it."""
    bare = _bare_repo(tmp_path)
    _git(bare, "branch", "spent", "refs/remotes/origin/master")

    cmd_prune(yes=True)

    assert "All bare repos are clean." not in capsys.readouterr().out


def test_prune_ignores_directories_that_are_not_bare_repos(tmp_path, capsys, xdg):
    """repos_dir() is ow's, but it is a directory on someone's disk.

    A stray file or checkout beside the bare repos must not be handed to
    git as one — glob("*") would, and every test in this file would still
    have passed.
    """
    bare = _bare_repo(tmp_path)
    _git(bare, "branch", "-D", "master")

    # A real repo, so glob("*") would not merely name it — it would survey it
    # and delete a branch out of a checkout that is none of ow's business.
    stray = paths.repos_dir() / "notes"
    stray.mkdir()
    _git_init(stray)
    (stray / "n.txt").write_text("n")
    _git(stray, "add", "-A")
    _git(stray, "commit", "-qm", "N")
    _git(stray, "update-ref", "refs/remotes/origin/master", "refs/heads/master")
    _git(stray, "branch", "scratch", "master")
    (paths.repos_dir() / "README").write_text("not a repo")

    cmd_prune(yes=True)

    out = capsys.readouterr().out
    assert "notes" not in out
    assert "README" not in out
    assert "scratch" in _branches(stray)
    assert "All bare repos are clean." in out


def test_prune_lists_branches_in_a_stable_order(tmp_path, capsys, xdg):
    """The orphan set is a set; reporting it unsorted reorders run to run.

    Eight names make an accidentally-sorted iteration order a 1-in-40320
    coincidence, and PYTHONHASHSEED randomisation redraws it every run.
    """
    bare = _bare_repo(tmp_path)
    _git(bare, "branch", "-D", "master")
    names = ["delta", "alpha", "golf", "charlie", "echo", "bravo", "hotel", "foxtrot"]
    for name in names:
        _git(bare, "branch", name, "refs/remotes/origin/master")

    cmd_prune(dry_run=True)

    out = capsys.readouterr().out
    listed = out.split("orphaned branches: ", 1)[1].split("\n", 1)[0].split(", ")
    assert listed == sorted(names)


def test_prune_says_when_it_has_finished(tmp_path, capsys, xdg, monkeypatch):
    """The plan is written in the imperative, so silence afterwards is ambiguous.

    "delete 1 orphaned branch: spent" followed by nothing at all reads the
    same whether the delete happened, was skipped, or fell over.
    """
    _answer(monkeypatch, "y")
    bare = _bare_repo(tmp_path)
    _git(bare, "branch", "spent", "refs/remotes/origin/master")

    cmd_prune()

    assert "Done." in capsys.readouterr().out


def test_prune_does_not_claim_to_be_done_when_it_did_nothing(tmp_path, capsys, xdg):
    bare = _bare_repo(tmp_path)
    _git(bare, "branch", "-D", "master")

    cmd_prune()

    out = capsys.readouterr().out
    assert "All bare repos are clean." in out
    assert "Done." not in out


def test_prune_does_not_claim_to_be_done_when_it_only_refused(tmp_path, capsys, xdg):
    """Keeping every branch it looked at is not work performed."""
    bare = _bare_repo(tmp_path)
    _commit_on_branch(bare, tmp_path, "featA", "work")
    _git(bare, "branch", "-D", "master")

    cmd_prune()

    out = capsys.readouterr().out
    assert "keep 1 branch with unpushed commits: featA" in out
    assert "Done." not in out
