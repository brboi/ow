"""Tests for multi-stack workspaces: port coordination, .code-workspace ownership, and rm."""

from __future__ import annotations

import json
import tomllib

from ow.core.paths import venvs_dir
from ow.core.plugins import load_plugins
from ow.core.refresh import read_lock
from tests.conftest import add_repo


def test_multi_stack_port_allocation_code_workspace_and_cleanup(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    ws_dir = lab.home / "voip-review"
    ws_dir.mkdir(parents=True)

    r1 = lab.ow("init", "stack-one=@18.0", cwd=ws_dir)
    assert r1.exit_code == 0, r1.stderr + r1.stdout
    r2 = lab.ow("add", "stack-two=@18.0", cwd=ws_dir)
    assert r2.exit_code == 0, r2.stderr + r2.stdout

    s1 = ws_dir / "stack-one"
    s2 = ws_dir / "stack-two"
    assert (s1 / ".ow" / "config.toml").is_file()
    assert (s2 / ".ow" / "config.toml").is_file()

    # Distinct auto-allocated HTTP and gevent ports
    assert "http_port = 8069" in (s1 / "odoorc").read_text()
    assert "gevent_port = 8072" in (s1 / "odoorc").read_text()
    assert "http_port = 8070" in (s2 / "odoorc").read_text()
    assert "gevent_port = 8073" in (s2 / "odoorc").read_text()

    # Multi-root .code-workspace generated in workspace root
    code_ws = ws_dir / "voip-review.code-workspace"
    assert code_ws.is_file()
    payload = json.loads(code_ws.read_text())
    assert payload == {
        "folders": [
            {"name": "stack-one", "path": "./stack-one"},
            {"name": "stack-two", "path": "./stack-two"},
        ]
    }
    assert list(read_lock(ws_dir)) == ["voip-review.code-workspace"]

    # Shared virtualenv path by Python version + Odoo series (py3.12-18.0)
    expected_venv = venvs_dir() / "py3.12-18.0"
    for stack in (s1, s2):
        frag = tomllib.loads((stack / "mise" / "conf.d" / "00-ow.toml").read_text())
        assert frag["env"]["_"]["python"]["venv"]["path"] == str(expected_venv)
        odools = tomllib.loads((stack / "odools.toml").read_text())
        assert odools["config"][0]["python_path"] == str(expected_venv / "bin" / "python")
        pyright = json.loads((stack / "pyrightconfig.json").read_text())
        assert pyright == {"venvPath": str(expected_venv.parent), "venv": expected_venv.name}
        launch = json.loads((stack / ".vscode" / "launch.json").read_text())
        assert launch["configurations"][0]["python"] == str(expected_venv / "bin" / "python")
        zed_dbg = json.loads((stack / ".zed" / "debug.json").read_text())
        assert zed_dbg[0]["python"] == str(expected_venv / "bin" / "python")

    # Removing stack-one from workspace root updates voip-review.code-workspace to only stack-two
    rm1 = lab.ow("rm", "stack-one", "-y", cwd=ws_dir)
    assert rm1.exit_code == 0, rm1.stderr
    assert not s1.exists()
    assert json.loads(code_ws.read_text()) == {
        "folders": [{"name": "stack-two", "path": "./stack-two"}]
    }

    # Removing the last stack via <ws>/<stack> cleans up voip-review.code-workspace
    rm2 = lab.ow("rm", "voip-review/stack-two", "-y")
    assert rm2.exit_code == 0, rm2.stderr
    assert not s2.exists()
    assert not code_ws.exists()


def test_multi_stack_code_workspace_respects_user_edits(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files()})
    ws_dir = lab.home / "custom-ws"
    ws_dir.mkdir(parents=True)

    assert lab.ow("init", "a=community@18.0", cwd=ws_dir).exit_code == 0
    code_ws = ws_dir / "custom-ws.code-workspace"
    assert code_ws.is_file()

    # User customizes the .code-workspace file
    code_ws.write_text('{"folders": [], "custom": true}\n')

    # Adding a second stack must NOT overwrite user-edited file
    assert lab.ow("add", "b=community@18.0", cwd=ws_dir).exit_code == 0
    assert '"custom": true' in code_ws.read_text()

    # Removing the whole multi-stack workspace must also preserve the user-edited file
    assert lab.ow("rm", "custom-ws", "-y").exit_code == 0
    assert code_ws.is_file()
    assert '"custom": true' in code_ws.read_text()


def test_multi_stack_refresh_never_deletes_a_file_no_plugin_produces_any_more(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files()})
    ws_dir = lab.home / "dropped-ws"
    ws_dir.mkdir(parents=True)

    assert lab.ow("init", "a=community@18.0", cwd=ws_dir).exit_code == 0
    code_ws = ws_dir / "dropped-ws.code-workspace"
    assert list(read_lock(ws_dir)) == ["dropped-ws.code-workspace"]

    assert lab.ow("config", "set", "--global", "vscode.enabled", "false").exit_code == 0
    assert lab.ow("refresh", cwd=ws_dir).exit_code == 0

    assert code_ws.is_file()
    assert read_lock(ws_dir) == {}


def test_to_json_value_serializes_workspace_with_mapping_proxy(lab):
    from ow.core.json_schema import to_json_value
    from ow.core.session import open_session

    add_repo(lab, "community", {"18.0": lab.odoo_files()})
    ws_dir = lab.home / "ws-json"
    ws_dir.mkdir(parents=True)
    assert lab.ow("init", "community@18.0", cwd=ws_dir).exit_code == 0
    sess = open_session(str(ws_dir), need_workspace=True, plugins=load_plugins())
    serialized = to_json_value(sess.workspace)
    assert serialized["name"] == "ws-json"
    assert isinstance(serialized["settings"], dict)


def test_stack_values_avoid_siblings_and_pending_stacks_without_mutating_inputs():
    from types import SimpleNamespace

    from ow.plugins import odoo

    sibling = SimpleNamespace(settings={"odoo.http_port": 8069, "odoo.gevent_port": 8072})
    pending = ({"odoo.http_port": 8070, "odoo.gevent_port": 8073},)
    ws_values: dict[str, object] = {}

    got = odoo.ow_stack_values(None, ws_values, [sibling], pending, {})

    assert got == {"odoo.http_port": 8071, "odoo.gevent_port": 8074}
    assert ws_values == {}
    assert pending == ({"odoo.http_port": 8070, "odoo.gevent_port": 8073},)


def test_stack_values_keep_explicit_ports_and_honour_global_base_port():
    from ow.plugins import odoo

    got = odoo.ow_stack_values(
        None, {"odoo.http_port": 9000}, [], (), {"odoo.gevent_port": 9100}
    )

    assert got == {"odoo.gevent_port": 9100}
    assert odoo.ow_stack_values(None, {"odoo.enabled": False}, [], (), {}) is None


def test_stack_values_pin_db_name_and_current_ports_when_promoting():
    from types import SimpleNamespace

    from ow.plugins import odoo

    ws = SimpleNamespace(
        name="voip",
        settings={"odoo.db_name": "voip_db", "odoo.http_port": 8071, "odoo.gevent_port": 8074},
    )

    assert odoo.ow_stack_values(ws, {}, [], (), {}) == {
        "odoo.db_name": "voip_db",
        "odoo.http_port": 8071,
        "odoo.gevent_port": 8074,
    }
    assert odoo.ow_stack_values(ws, {"odoo.db_name": "mine"}, [], (), {}) == {
        "odoo.http_port": 8071,
        "odoo.gevent_port": 8074,
    }


def test_rm_multi_stack_prompts_once_for_entire_workspace(lab, monkeypatch):
    from ow import term

    monkeypatch.setattr(term, "interactive", lambda: True)
    add_repo(lab, "community", {"18.0": lab.odoo_files()})
    ws_dir = lab.home / "one-prompt"
    ws_dir.mkdir(parents=True)

    assert lab.ow("init", "ws-a=community@18.0", "ws-b=community@18.0", cwd=ws_dir).exit_code == 0

    # Declining once aborts the whole removal without deleting any stack
    declined = lab.ow("rm", "one-prompt", input="n\n")
    assert declined.exit_code == 2
    assert (ws_dir / "ws-a").is_dir()
    assert (ws_dir / "ws-b").is_dir()

    # Accepting once removes both stacks, .code-workspace, and the empty root directory
    accepted = lab.ow("rm", "one-prompt", input="y\n")
    assert accepted.exit_code == 0, accepted.stderr
    assert accepted.stdout.count("Proceed?") == 1
    assert not ws_dir.exists()


def test_files_command_inspects_multi_stack_root(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws_dir = lab.home / "files-multi"
    ws_dir.mkdir(parents=True)
    assert lab.ow("init", "stack-a=community@18.0", "stack-b=community@18.0", cwd=ws_dir).exit_code == 0

    ls_res = lab.ow("files", cwd=ws_dir)
    assert ls_res.exit_code == 0, ls_res.stderr
    assert ls_res.stdout.splitlines() == ["files-multi.code-workspace\tup to date"]

    json_res = lab.ow("files", "--json", cwd=ws_dir)
    assert json_res.exit_code == 0, json_res.stderr
    payload = json.loads(json_res.stdout)
    assert payload["multi_stack"] is True
    assert payload["files"][0]["path"] == "files-multi.code-workspace"
    assert payload["files"][0]["state"] == "up to date"

    code_ws = ws_dir / "files-multi.code-workspace"
    code_ws.write_text('{"folders": []}\n')
    yours_res = lab.ow("files", cwd=ws_dir)
    assert yours_res.stdout.splitlines() == ["files-multi.code-workspace\tyours"]

    diff_res = lab.ow("files", "--diff", cwd=ws_dir)
    assert diff_res.exit_code == 0
    assert "--- files-multi.code-workspace" in diff_res.stdout
    assert "+++ ow's files-multi.code-workspace" in diff_res.stdout
