"""`ow ls` and `ow cd` scenarios (§4): the listing's columns, `missing`, and the roots.

The workspaces are built with plain git, `write_workspace` and the index, like
`test_cli.py`: the fleet commands read the same state however it came to be.
"""

from __future__ import annotations

from pathlib import Path

from ow.core.config import write_workspace
from ow.core.index import add as index_add
from ow.core.paths import repos_dir

REPO = "community"
BASE = "18.0"


def _bare(lab) -> Path:
    """The shared bare repo, fetched from its origin."""
    origin = lab.remote(REPO, {BASE: {"README.md": "c\n"}}, head=BASE)
    bare = repos_dir() / f"{REPO}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    lab.git(bare.parent, "init", "--bare", str(bare))
    lab.git(bare, "remote", "add", "origin", str(origin))
    lab.git(bare, "fetch", "origin")
    return bare


def _workspace(lab, bare: Path, name: str, *, branch: str | None, entry: bool) -> Path:
    """One workspace of the shared bare: on `branch`, or detached; `entry` records the default base."""
    root = lab.ws(name)
    root.mkdir(parents=True, exist_ok=True)
    if branch is None:
        lab.git(bare, "worktree", "add", "--detach", str(root / REPO), f"origin/{BASE}")
    else:
        lab.git(bare, "worktree", "add", "-b", branch, str(root / REPO), f"origin/{BASE}")
    repos = {REPO: f"origin/{BASE}"} if entry else {}
    write_workspace(root / ".ow" / "config.toml", repos, {}, declared={})
    index_add(root)
    return root


def test_ls_lists_every_workspace_and_cd_prints_their_roots(lab):
    bare = _bare(lab)
    voip = _workspace(lab, bare, "voip", branch="18.0-voip", entry=True)
    det = _workspace(lab, bare, "det", branch=None, entry=True)
    unset = _workspace(lab, bare, "unset", branch=None, entry=False)
    index_add(lab.ws("gone"))  # a dead index entry: the path never held a workspace

    result = lab.ow("ls")

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        f"voip\t~/ws/voip\t{REPO}:18.0-voip",
        f"det\t~/ws/det\t{REPO}:origin/{BASE}",
        f"unset\t~/ws/unset\t{REPO}:(unset)",
        "gone\t~/ws/gone\tmissing",
    ]

    # `ow cd` inside a workspace prints that workspace's root; `<ws>` resolves by name.
    inside = lab.ow("cd", cwd=voip)
    assert inside.exit_code == 0
    assert inside.stdout == f"{voip.resolve()}\n"

    by_name = lab.ow("cd", "det")
    assert by_name.exit_code == 0
    assert by_name.stdout == f"{det.resolve()}\n"

    assert lab.ow("cd", "unset").stdout == f"{unset.resolve()}\n"

    missing = lab.ow("cd", "bogus")
    assert missing.exit_code == 2
    assert "workspace 'bogus' not found" in missing.stderr


def test_ls_with_no_known_workspace_prints_nothing(lab):
    result = lab.ow("ls")

    assert result.exit_code == 0
    assert result.stdout == ""
