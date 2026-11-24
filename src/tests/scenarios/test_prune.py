"""`ow prune` scenarios: prunable worktrees, pushed vs kept orphan branches, dead index
entries — all under one prompt.

The workspace comes from `ow repo add` + `ow init`; the orphans and the dead index
entry are then arranged by hand, the states prune is asked to clean up.
"""

from __future__ import annotations

from ow.core.index import add as index_add
from ow.core.paths import repos_dir
from tests.conftest import add_repo, git, git_ok, index_lines, init_workspace

BARE = "~/.local/share/ow/repos"
REPO = "community"
BASE = "18.0"
HINT = "directory gone"


def _machine(lab):
    """A machine with one bare: a prunable worktree on a pushed branch, a pushed orphan, an
    unpushed orphan, a stale `master` its HEAD points at, and a dead index entry."""
    add_repo(lab, REPO, {BASE: {"README.md": "c\n"}})
    ws = init_workspace(lab, REPO, branch="18.0-voip")
    bare = repos_dir() / f"{REPO}.git"

    # The worktree moves away: its registration becomes prunable, and its branch — whose
    # tip is the base's, so pushed — still counts as checked out.
    (ws / REPO).rename(ws / f"{REPO}-moved")
    # A pushed orphan: created at a remote tip, checked out in no worktree.
    lab.git(bare, "branch", "pushed", f"origin/{BASE}")
    # An unpushed orphan: a commit no remote has, held only by the bare.
    tree = lab.sha(bare, f"origin/{BASE}^{{tree}}")
    sha = git(bare, "commit-tree", tree, "-m", "local only")
    lab.git(bare, "branch", "kept", sha)
    # A stale default branch the bare's HEAD points at, as an adopted 2.x bare has.
    lab.git(bare, "branch", "master", f"origin/{BASE}")
    # A dead index entry: a path that never held a workspace.
    index_add(lab.ws("gone"))
    return ws, bare


PLAN = [
    f"# ~/ws/voip/{REPO}: prunable ({HINT})",
    f"# {REPO}: kept unpushed branch kept; "
    f"delete it with `git -C {BARE}/{REPO}.git branch -D kept`",
    f"git -C {BARE}/{REPO}.git worktree prune",
    f"git -C {BARE}/{REPO}.git branch -D pushed",
    "# drop ~/ws/gone from the index",
]


def test_prune_cleans_the_whole_machine_with_one_prompt(lab):
    ws, bare = _machine(lab)
    plan = PLAN

    dry = lab.ow("prune", "-n")

    assert dry.exit_code == 0
    assert dry.stdout.splitlines() == plan
    # `-n` cleans nothing.
    assert f"worktree {ws / REPO}" in lab.git(bare, "worktree", "list", "--porcelain")
    assert git_ok(bare, "rev-parse", "--verify", "refs/heads/pushed")
    assert git_ok(bare, "rev-parse", "--verify", "refs/heads/kept")
    assert str(lab.ws("gone")) in index_lines()

    result = lab.ow("prune", "-y")

    assert result.exit_code == 0
    assert result.stdout.splitlines() == plan
    assert f"worktree {ws / REPO}" not in lab.git(bare, "worktree", "list", "--porcelain")
    assert not git_ok(bare, "rev-parse", "--verify", "refs/heads/pushed")
    # The branch of a prunable worktree still counts as checked out, pushed or not.
    assert git_ok(bare, "rev-parse", "--verify", "refs/heads/18.0-voip")
    # Unpushed orphans stay.
    assert git_ok(bare, "rev-parse", "--verify", "refs/heads/kept")
    assert str(lab.ws("gone")) not in index_lines()
    assert str(ws) in index_lines()


def test_prune_keeps_the_branch_the_bare_head_points_at(lab):
    _, bare = _machine(lab)
    assert git_ok(bare, "rev-parse", "--verify", "refs/heads/master")

    result = lab.ow("prune", "-y")

    assert result.exit_code == 0
    assert "branch -D master" not in result.stdout
    assert git_ok(bare, "rev-parse", "--verify", "refs/heads/master")


def test_prune_deletes_a_pushed_branch_that_shares_its_name_with_a_tag(lab):
    _, bare = _machine(lab)
    lab.git(bare, "tag", "pushed", f"origin/{BASE}")

    result = lab.ow("prune", "-y")

    assert result.exit_code == 0
    assert f"git -C {BARE}/{REPO}.git branch -D pushed" in result.stdout.splitlines()
    assert not git_ok(bare, "rev-parse", "--verify", "refs/heads/pushed")
    assert git_ok(bare, "rev-parse", "--verify", "refs/tags/pushed")


def test_a_declined_prompt_prunes_nothing(lab, monkeypatch):
    ws, bare = _machine(lab)
    monkeypatch.setattr("ow.term.interactive", lambda: True)

    result = lab.ow("prune", input="n\n")

    assert result.exit_code == 2
    assert result.stdout.count("Proceed?") == 1
    assert f"worktree {ws / REPO}" in lab.git(bare, "worktree", "list", "--porcelain")
    assert git_ok(bare, "rev-parse", "--verify", "refs/heads/pushed")
    assert str(lab.ws("gone")) in index_lines()


def test_prune_on_a_clean_machine_prints_nothing_and_asks_nothing(lab):
    result = lab.ow("prune", "-y")

    assert result.exit_code == 0
    assert result.stdout == ""


DEMO_PLUGIN = '''
from ow.plugin_api import PruneCandidate, hookimpl

OW_API_VERSION = 1
PLUGIN_NAME = "demo_prune"


@hookimpl
def ow_prune_candidates(workspaces, offline):
    return [PruneCandidate(workspace=ws.root, reason="demo says so") for ws in workspaces]


@hookimpl
def ow_running(ws):
    return ["demo-svc"] if (ws.root / ".up").exists() else None
'''


def _demo_workspace(lab, name: str):
    """A workspace whose own repo carries a trusted in-repo plugin proposing itself for pruning."""
    add_repo(
        lab,
        "demo",
        {
            "main": {
                "pyproject.toml": '[tool.ow]\nplugin = "tools/ow_plugin.py"\n',
                "tools/ow_plugin.py": DEMO_PLUGIN,
            }
        },
        base="main",
    )
    ws = init_workspace(lab, "demo", base="main", name=name)
    assert lab.ow("trust", cwd=ws).exit_code == 0
    return ws


def test_prune_honours_an_in_repo_plugins_candidates(lab):
    ws = _demo_workspace(lab, "old")

    dry = lab.ow("prune", "-n")

    assert dry.exit_code == 0, dry.stderr + dry.stdout
    assert "# prune workspace old: demo says so" in dry.stdout.splitlines()

    done = lab.ow("prune", "-y")

    assert done.exit_code == 0, done.stderr + done.stdout
    assert not ws.exists()
    assert str(ws) not in index_lines()


def test_prune_skips_a_candidate_workspace_with_something_running(lab):
    ws = _demo_workspace(lab, "busy")
    (ws / ".up").write_text("up")

    result = lab.ow("prune", "-y")

    assert result.exit_code == 0, result.stderr + result.stdout
    assert (
        "# skip pruning busy (demo says so): runtime: running (demo-svc); run `ow stop` first"
        in result.stdout.splitlines()
    )
    assert ws.exists()
    assert str(ws) in index_lines()
