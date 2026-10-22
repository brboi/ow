"""`ow ls` and `ow cd`: the listing's columns, `missing`/`unreadable`, and the roots."""

from __future__ import annotations

from ow.core.index import add as index_add
from tests.conftest import make_bare, workspace

REPO = "community"
BASE = "18.0"


def test_ls_lists_every_workspace_and_cd_prints_their_roots(lab):
    bare = make_bare(lab)
    voip = workspace(lab, name="voip", branch="18.0-voip", bare=bare)
    det = workspace(lab, name="det", bare=bare)
    unset = workspace(lab, name="unset", bare=bare, entry=False)
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
    assert "unknown workspace 'bogus'" in missing.stderr


def test_ls_with_no_known_workspace_prints_nothing(lab):
    result = lab.ow("ls")

    assert result.exit_code == 0
    assert result.stdout == ""


def test_ls_marks_a_live_but_unreadable_workspace(lab):
    root = lab.ws("bad")
    (root / ".ow").mkdir(parents=True)
    (root / ".ow" / "config.toml").write_text("repos = [\n")  # invalid TOML
    index_add(root)

    result = lab.ow("ls")

    assert result.exit_code == 0
    assert result.stdout == "bad\t~/ws/bad\tunreadable\n"


def test_cd_on_a_dead_index_entry_refuses_with_a_prune_hint(lab):
    index_add(lab.ws("gone"))

    result = lab.ow("cd", "gone")

    assert result.exit_code == 2
    assert "workspace 'gone' is missing" in result.stderr
    assert "ow prune" in result.stderr
