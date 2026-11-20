"""Universal `--json` contract tests: strict valid JSON on stdout, progress on stderr."""

from __future__ import annotations

import json

from ow.core.paths import repos_dir
from tests.conftest import add_repo


def test_json_across_lifecycle_and_coding_harness_task_status(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    lab.git(repos_dir() / "enterprise.git", "config", "ow.github-repo", "odoo/enterprise")
    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 777,
                "title": "Harness test",
                "url": "https://github.com/odoo/enterprise/pull/777",
                "state": "OPEN",
                "isDraft": False,
                "reviewDecision": "APPROVED",
                "headRefName": "18.0-harness",
                "baseRefName": "18.0",
            }
        ]
    )

    parent = lab.home / "odoo"
    parent.mkdir(parents=True)

    # 1. ow add --json (multi-workspace task: ws-a on branch + ws-b detached)
    a_res = lab.ow(
        "add", "-t", "my-task", "ws-a", "--base", "18.0", "--branch", "18.0-harness", "--json",
        cwd=parent,
    )
    assert a_res.exit_code == 0, a_res.stderr
    a_json = json.loads(a_res.stdout)
    assert a_json["schema_version"] == 1
    assert a_json["result"]["workspaces"][0]["name"] == "ws-a"
    assert a_json["result"]["workspaces"][0]["http_port"] == 8069

    b_res = lab.ow("add", "-t", "my-task", "ws-b", "--base", "18.0", "--json", cwd=parent)
    assert b_res.exit_code == 0, b_res.stderr
    b_json = json.loads(b_res.stdout)
    assert b_json["result"]["workspaces"][0]["http_port"] == 8070

    # 2. ow ls --json & --task filter
    ls_res = lab.ow("ls", "-t", "my-task", "--json")
    assert ls_res.exit_code == 0
    ls_json = json.loads(ls_res.stdout)
    assert ls_json["schema_version"] == 1
    assert [w["name"] for w in ls_json["workspaces"]] == ["ws-a", "ws-b"]

    # 3. ow status --json inside ws-a exposes sibling ws-b + GitHub PR payload for coding harness
    st_res = lab.ow("status", "--json", cwd=parent / "ws-a")
    assert st_res.exit_code == 0, st_res.stderr
    st_json = json.loads(st_res.stdout)
    assert st_json["schema_version"] == 1
    assert st_json["workspace"]["name"] == "ws-a"
    assert st_json["task"]["name"] == "my-task"
    assert st_json["task"]["code_workspace"] == str(parent / "my-task.code-workspace")
    assert [s["name"] for s in st_json["task"]["siblings"]] == ["ws-b"]
    assert st_json["task"]["siblings"][0]["http_port"] == 8070
    ent_repo = next(r for r in st_json["workspace"]["repos"] if r["name"] == "enterprise")
    assert ent_repo["plugins"]["github"]["data"]["number"] == 777

    # 4. ow status -t my-task --json
    task_st = lab.ow("status", "-t", "my-task", "--offline", "--json")
    assert task_st.exit_code == 0
    task_st_json = json.loads(task_st.stdout)
    assert len(task_st_json["task"]["workspaces"]) == 2

    # 5. ow files --json
    files_res = lab.ow("files", "--json", cwd=parent / "ws-a")
    assert files_res.exit_code == 0
    files_json = json.loads(files_res.stdout)
    assert files_json["schema_version"] == 1
    assert any(f["path"] == "odoorc" for f in files_json["files"])

    # 6. ow config --json & ow config get --json
    cfg_res = lab.ow("config", "--json", cwd=parent / "ws-a")
    assert cfg_res.exit_code == 0
    cfg_json = json.loads(cfg_res.stdout)
    assert cfg_json["settings"]["retarget.enabled"]["value"] is False
    get_res = lab.ow("config", "get", "odoo.http_port", "--json", cwd=parent / "ws-a")
    assert get_res.exit_code == 0
    assert json.loads(get_res.stdout)["value"] == 8069

    # 7. ow prune -n --offline --json
    prune_res = lab.ow("prune", "-n", "--offline", "--json")
    assert prune_res.exit_code == 0
    assert json.loads(prune_res.stdout)["schema_version"] == 1

    # 8. ow rm -t my-task -y --json
    rm_res = lab.ow("rm", "-t", "my-task", "-y", "--json")
    assert rm_res.exit_code == 0, rm_res.stderr
    rm_json = json.loads(rm_res.stdout)
    assert rm_json["task"] == "my-task"
    assert all(w["removed"] for w in rm_json["workspaces"])
    assert not (parent / "my-task.code-workspace").exists()
