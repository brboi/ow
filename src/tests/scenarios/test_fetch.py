"""`ow fetch` scenarios (§7/§16): the force-pushed fetch, and the table behind it.

The collaborator rewinds the branch it published: the fetch must move the bare's
remote-tracking ref and write its reflog. The plan lines print first, in workspace repo
order, before the status table.
"""

from __future__ import annotations

from ow.core.paths import repos_dir

BARE = "~/.local/share/ow/repos"


def test_force_pushed_fetch_moves_the_ref_and_its_reflog(lab):
    community_remote = lab.remote("community", {"18.0": lab.odoo_files()}, head="18.0")
    enterprise_remote = lab.remote(
        "enterprise", {"18.0": {"README.md": "enterprise\n"}}, head="18.0"
    )
    assert lab.ow("repo", "add", "community", f"file://{community_remote}").exit_code == 0
    assert lab.ow("repo", "add", "enterprise", f"file://{enterprise_remote}").exit_code == 0

    collaborator = lab.collaborator("dev", of=community_remote)
    collaborator.commit({"feature.py": "v1\n"}, message="feature v1", branch="18.0-voip")
    collaborator.push("18.0-voip")

    ws = lab.ws("voip")
    assert (
        lab.ow(
            "init", str(ws), "community", "enterprise", "--base", "18.0", "--branch", "18.0-voip"
        ).exit_code
        == 0
    )

    bare = repos_dir() / "community.git"
    published = lab.sha(bare, "refs/remotes/origin/18.0-voip")
    # The collaborator rewinds the published branch to 18.0's tip: a force-push.
    collaborator.force_push("18.0-voip", lab.sha(bare, "refs/remotes/origin/18.0"))
    rewound = collaborator.git("rev-parse", "18.0-voip")
    assert rewound != published

    result = lab.ow("fetch", cwd=ws)

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0] == f"git -C {BARE}/community.git fetch origin 18.0 18.0-voip"
    assert lines[1] == f"git -C {BARE}/enterprise.git fetch origin 18.0"
    assert lines[2].startswith("community\t")
    assert lines[3].startswith("enterprise\t")

    # The remote-tracking ref moved, and its reflog kept both tips.
    assert lab.sha(bare, "refs/remotes/origin/18.0-voip") == rewound
    reflog = lab.git(
        bare, "reflog", "show", "--format=%H", "refs/remotes/origin/18.0-voip"
    ).splitlines()
    assert reflog[0] == rewound
    assert published in reflog[1:]
    assert (
        lab.git(
            bare, "reflog", "show", "-1", "--format=%H", "refs/remotes/origin/18.0-voip"
        )
        == rewound
    )