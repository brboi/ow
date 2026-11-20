"""End-to-end fleet scenarios on multi-stack workspaces: status, refresh, ls, cd, rm, prune, disambiguation."""

from __future__ import annotations

from ow.core.paths import repos_dir
from tests.conftest import add_repo


def test_disambiguate_stacks_across_multiple_multi_stack_workspaces(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "voip", {"master": {"README.md": "voip\n"}}, base="master")

    ws1 = lab.home / "task-alpha"
    ws2 = lab.home / "task-beta"
    ws1.mkdir(parents=True)
    ws2.mkdir(parents=True)
    assert lab.ow("init", "odoo=community@18.0", "voip=voip", cwd=ws1).exit_code == 0
    assert lab.ow("init", "odoo=community@18.0", cwd=ws2).exit_code == 0

    # `ow cd task-alpha` prints the multi-stack root; `ow cd task-alpha/odoo` prints the stack
    assert lab.ow("cd", "task-alpha").stdout.strip() == str(ws1.resolve())
    assert lab.ow("cd", "task-alpha/odoo").stdout.strip() == str((ws1 / "odoo").resolve())
    assert lab.ow("cd", "task-beta/odoo").stdout.strip() == str((ws2 / "odoo").resolve())

    # Unique stack name (`voip`) resolves from anywhere; ambiguous (`odoo`) hints `<ws>/<stack>`
    assert lab.ow("cd", "voip").stdout.strip() == str((ws1 / "voip").resolve())
    ambig = lab.ow("cd", "odoo", cwd=lab.home)
    assert ambig.exit_code == 2
    assert "task-alpha/odoo" in ambig.stderr
    assert "task-beta/odoo" in ambig.stderr

    # Inside `task-alpha`, `-w odoo` resolves local `./odoo` unambiguously!
    st_local = lab.ow("status", "-w", "odoo", cwd=ws1)
    assert st_local.exit_code == 0, st_local.stderr
    assert "community" in st_local.stdout

    # `ow ls` lists both workspaces cleanly
    ls = lab.ow("ls")
    assert ls.exit_code == 0
    lines = ls.stdout.splitlines()
    assert any("task-alpha" in line and "stacks: odoo, voip" in line for line in lines)
    assert any("task-beta" in line and "stacks: odoo" in line for line in lines)

    # `ow refresh` at multi-stack root refreshes all stacks & regenerates `.code-workspace`
    (ws1 / "task-alpha.code-workspace").unlink()
    ref = lab.ow("refresh", cwd=ws1)
    assert ref.exit_code == 0, ref.stderr + ref.stdout
    assert (ws1 / "task-alpha.code-workspace").is_file()


def test_prune_removes_merged_stack_inside_multi_stack_workspace(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    remote = add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    collab = lab.collaborator("dev", of=remote)
    collab.commit({"ent/m.py": "m = 1\n"}, message="merged", branch="18.0-merged-stack")
    collab.push("18.0-merged-stack")
    lab.publish_pr(remote, 505, "18.0-merged-stack")
    lab.git(repos_dir() / "enterprise.git", "config", "ow.github-repo", "odoo/enterprise")
    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 505,
                "title": "Merged PR",
                "url": "https://github.com/odoo/enterprise/pull/505",
                "state": "MERGED",
                "isDraft": False,
                "headRefName": "18.0-merged-stack",
                "baseRefName": "18.0",
            }
        ]
    )

    ws = lab.home / "mixed-multi"
    ws.mkdir(parents=True)
    assert lab.ow("init", "merged=enterprise#505", "keep=community@18.0", cwd=ws).exit_code == 0

    pruned = lab.ow("prune", "-y")
    assert pruned.exit_code == 0, pruned.stderr + pruned.stdout
    assert not (ws / "merged").exists()
    assert (ws / "keep").is_dir()
    # Multi-stack workspace stays indexed (not considered a dead entry) and code-workspace updated
    assert (ws / "mixed-multi.code-workspace").is_file()
    assert "merged" not in (ws / "mixed-multi.code-workspace").read_text()
    assert "keep" in (ws / "mixed-multi.code-workspace").read_text()
