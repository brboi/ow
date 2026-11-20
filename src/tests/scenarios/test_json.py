"""Universal `--json` contract tests: strict valid JSON on stdout, progress on stderr."""

from __future__ import annotations

import json

from ow.core.paths import repos_dir
from tests.conftest import add_repo


def test_json_across_lifecycle_and_multi_stack_status(lab):
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

    ws_dir = lab.home / "my-project"
    ws_dir.mkdir(parents=True)

    # 1. ow init --json (multi-stack workspace: stack-a on branch + stack-b added via ow add)
    a_res = lab.ow(
        "init", "stack-a=@18.0", "--branch", "18.0-harness", "--json",
        cwd=ws_dir,
    )
    assert a_res.exit_code == 0, a_res.stderr
    a_json = json.loads(a_res.stdout)
    assert a_json["schema_version"] == 1
    assert a_json["result"]["multi_stack"] is True
    assert a_json["result"]["workspaces"][0]["name"] == "stack-a"
    assert a_json["result"]["workspaces"][0]["ports"] == {"http": 8069, "gevent": 8072}

    b_res = lab.ow("add", "stack-b=@18.0", "--json", cwd=ws_dir)
    assert b_res.exit_code == 0, b_res.stderr
    b_json = json.loads(b_res.stdout)
    assert b_json["result"]["workspaces"][0]["ports"] == {"http": 8070, "gevent": 8073}

    # 2. ow ls --json lists the multi-stack workspace and its stacks
    ls_res = lab.ow("ls", "--json")
    assert ls_res.exit_code == 0
    ls_json = json.loads(ls_res.stdout)
    assert ls_json["schema_version"] == 1
    assert len(ls_json["workspaces"]) == 1
    assert ls_json["workspaces"][0]["name"] == "my-project"
    assert ls_json["workspaces"][0]["multi_stack"] is True
    assert [s["name"] for s in ls_json["workspaces"][0]["stacks"]] == ["stack-a", "stack-b"]
    assert [s["ports"]["http"] for s in ls_json["workspaces"][0]["stacks"]] == [8069, 8070]

    # 3. ow status --json inside stack-a exposes sibling stack-b + GitHub PR payload
    st_res = lab.ow("status", "--json", cwd=ws_dir / "stack-a")
    assert st_res.exit_code == 0, st_res.stderr
    st_json = json.loads(st_res.stdout)
    assert st_json["schema_version"] == 1
    assert st_json["workspace"]["name"] == "stack-a"
    assert st_json["workspace"]["ports"] == {"http": 8069, "gevent": 8072}
    assert st_json["parent_workspace"]["name"] == "my-project"
    assert st_json["parent_workspace"]["code_workspace"] == str(ws_dir / "my-project.code-workspace")
    assert [s["name"] for s in st_json["parent_workspace"]["siblings"]] == ["stack-b"]
    assert st_json["parent_workspace"]["siblings"][0]["ports"] == {"http": 8070, "gevent": 8073}
    ent_repo = next(r for r in st_json["workspace"]["repos"] if r["name"] == "enterprise")
    assert ent_repo["plugins"]["github"]["data"]["number"] == 777

    # 4. ow status --json at multi-stack root inspects all stacks
    multi_st = lab.ow("status", "--offline", "--json", cwd=ws_dir)
    assert multi_st.exit_code == 0
    multi_st_json = json.loads(multi_st.stdout)
    assert multi_st_json["workspace"]["multi_stack"] is True
    assert len(multi_st_json["workspace"]["stacks"]) == 2

    # 5. ow files --json
    files_res = lab.ow("files", "--json", cwd=ws_dir / "stack-a")
    assert files_res.exit_code == 0
    files_json = json.loads(files_res.stdout)
    assert files_json["schema_version"] == 1
    assert any(f["path"] == "odoorc" for f in files_json["files"])

    # 6. ow config --json & ow config get --json
    cfg_res = lab.ow("config", "--json", cwd=ws_dir / "stack-a")
    assert cfg_res.exit_code == 0
    cfg_json = json.loads(cfg_res.stdout)
    assert cfg_json["settings"]["runbot.enabled"]["value"] is True
    get_res = lab.ow("config", "get", "odoo.http_port", "--json", cwd=ws_dir / "stack-a")
    assert get_res.exit_code == 0
    assert json.loads(get_res.stdout)["value"] == 8069

    # 7. ow prune -n --offline --json
    prune_res = lab.ow("prune", "-n", "--offline", "--json")
    assert prune_res.exit_code == 0
    assert json.loads(prune_res.stdout)["schema_version"] == 1

    # 8. ow rm my-project -y --json
    rm_res = lab.ow("rm", "my-project", "-y", "--json")
    assert rm_res.exit_code == 0, rm_res.stderr
    rm_json = json.loads(rm_res.stdout)
    assert rm_json["workspace"] == "my-project"
    assert rm_json["multi_stack"] is True
    assert all(s["removed"] for s in rm_json["stacks"])
    assert not ws_dir.exists()
