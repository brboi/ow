"""`ow fetch` scenarios: the force-pushed fetch, the deleted upstream, and the
adopted bare without fetch config.

All fetch lines print first, in workspace repo order, before any of them runs;
the repos are then fetched in parallel. The table is gathered after the fetch,
so it shows the remote-tracking refs the fetch just moved.
"""

from __future__ import annotations

from ow.core.paths import repos_dir
from tests.conftest import add_repo, init_workspace

BARE = "~/.local/share/ow/repos"


def _ws_with_voip_branch(lab):
    community_remote = add_repo(lab)
    add_repo(lab, "enterprise", {"18.0": {"README.md": "enterprise\n"}})

    collaborator = lab.collaborator("dev", of=community_remote)
    collaborator.commit({"feature.py": "v1\n"}, message="feature v1", branch="18.0-voip")
    collaborator.push("18.0-voip")

    ws = init_workspace(lab, "community", "enterprise", branch="18.0-voip")
    return community_remote, collaborator, ws


def test_force_pushed_fetch_moves_the_ref_and_reports_it(lab):
    community_remote, collaborator, ws = _ws_with_voip_branch(lab)

    bare = repos_dir() / "community.git"
    published = lab.sha(bare, "refs/remotes/origin/18.0-voip")
    # The collaborator rewinds the published branch to 18.0's tip: a force-push.
    collaborator.force_push("18.0-voip", lab.sha(bare, "refs/remotes/origin/18.0"))
    rewound = collaborator.git("rev-parse", "18.0-voip")
    assert rewound != published

    result = lab.ow("fetch", cwd=ws)

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0] == (
        f"git -C {BARE}/community.git fetch origin +refs/heads/18.0:refs/remotes/origin/18.0"
    )
    assert lines[1] == (
        f"git -C {BARE}/community.git fetch origin"
        " +refs/heads/18.0-voip:refs/remotes/origin/18.0-voip"
    )
    assert lines[2] == (
        f"git -C {BARE}/enterprise.git fetch origin +refs/heads/18.0:refs/remotes/origin/18.0"
    )
    assert lines[3] == "# community: origin/18.0-voip was force-pushed"
    assert lines[4].startswith("community\t")
    assert lines[5].startswith("enterprise\t")
    assert lab.sha(bare, "refs/remotes/origin/18.0-voip") == rewound


def test_deleted_upstream_is_a_note_and_the_base_is_still_fetched(lab):
    community_remote, collaborator, ws = _ws_with_voip_branch(lab)

    # The clone already has a local 18.0 (the remote's HEAD), so switch to it
    # rather than creating a new branch.
    collaborator.git("checkout", "18.0")
    collaborator.commit({"later.py": "v1\n"}, message="base moved on")
    collaborator.push("18.0")
    collaborator.git("push", "origin", "--delete", "18.0-voip")
    moved = collaborator.git("rev-parse", "18.0")

    result = lab.ow("fetch", cwd=ws)

    assert result.exit_code == 0
    assert "# community: upstream origin/18.0-voip is gone; `git branch --unset-upstream`" in (
        result.stdout.splitlines()
    )
    bare = repos_dir() / "community.git"
    assert lab.sha(bare, "refs/remotes/origin/18.0") == moved


def test_bare_without_fetch_config_still_updates_the_remote_ref(lab):
    community_remote, collaborator, ws = _ws_with_voip_branch(lab)

    bare = repos_dir() / "community.git"

    # The clone already has a local 18.0 (the remote's HEAD), so switch to it
    # rather than creating a new branch.
    collaborator.git("checkout", "18.0")
    collaborator.commit({"later.py": "v1\n"}, message="base moved on")
    collaborator.push("18.0")
    moved = collaborator.git("rev-parse", "18.0")

    result = lab.ow("fetch", cwd=ws)

    assert result.exit_code == 0
    assert lab.sha(bare, "refs/remotes/origin/18.0") == moved
