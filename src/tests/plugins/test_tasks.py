"""Tests for multi-workspace tasks (issue #84): derivation, port allocation, .code-workspace & cleanup."""

from __future__ import annotations

import json
import tomllib

from ow.core import tasks
from ow.core.paths import venvs_dir
from ow.core.plugins import load_plugins
from tests.conftest import add_repo


def test_task_port_allocation_code_workspace_and_cleanup(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    parent = lab.home / "odoo"
    parent.mkdir(parents=True)

    r1 = lab.ow("add", "-t", "voip-task", "ws-one", "--base", "18.0", cwd=parent)
    assert r1.exit_code == 0, r1.stderr + r1.stdout
    r2 = lab.ow("add", "-t", "voip-task", "ws-two", "--base", "18.0", cwd=parent)
    assert r2.exit_code == 0, r2.stderr + r2.stdout

    ws1 = parent / "ws-one"
    ws2 = parent / "ws-two"
    assert (ws1 / ".ow" / "config.toml").is_file()
    assert (ws2 / ".ow" / "config.toml").is_file()

    # Distinct auto-allocated HTTP ports
    assert "http_port = 8069" in (ws1 / "odoorc").read_text()
    assert "http_port = 8070" in (ws2 / "odoorc").read_text()

    # Multi-root .code-workspace generated directly in common parent
    code_ws = parent / "voip-task.code-workspace"
    assert code_ws.is_file()
    payload = json.loads(code_ws.read_text())
    assert payload == {
        "folders": [
            {"name": "ws-one", "path": "./ws-one"},
            {"name": "ws-two", "path": "./ws-two"},
        ]
    }

    # Shared virtualenv path by Python version + Odoo series (py3.12-18.0)
    expected_venv = venvs_dir() / "py3.12-18.0"
    for ws in (ws1, ws2):
        frag = tomllib.loads((ws / "mise" / "conf.d" / "00-ow.toml").read_text())
        assert frag["env"]["_"]["python"]["venv"]["path"] == str(expected_venv)
        odools = tomllib.loads((ws / "odools.toml").read_text())
        assert odools["config"][0]["python_path"] == str(expected_venv / "bin" / "python")
        pyright = json.loads((ws / "pyrightconfig.json").read_text())
        assert pyright == {"venvPath": str(expected_venv.parent), "venv": expected_venv.name}
        launch = json.loads((ws / ".vscode" / "launch.json").read_text())
        assert launch["configurations"][0]["python"] == str(expected_venv / "bin" / "python")
        zed_dbg = json.loads((ws / ".zed" / "debug.json").read_text())
        assert zed_dbg[0]["python"] == str(expected_venv / "bin" / "python")

    # Removing ws-one updates voip-task.code-workspace to only ws-two
    rm1 = lab.ow("rm", "ws-one", "-y")
    assert rm1.exit_code == 0, rm1.stderr
    assert not ws1.exists()
    assert json.loads(code_ws.read_text()) == {
        "folders": [{"name": "ws-two", "path": "./ws-two"}]
    }

    # Removing the last member deletes voip-task.code-workspace cleanly
    rm2 = lab.ow("rm", "ws-two", "-y")
    assert rm2.exit_code == 0, rm2.stderr
    assert not ws2.exists()
    assert not code_ws.exists()


def test_task_code_workspace_respects_user_edits(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files()})
    parent = lab.home / "odoo"
    parent.mkdir(parents=True)

    assert lab.ow("add", "-t", "custom-task", "a", "-r", "community", "--base", "18.0", cwd=parent).exit_code == 0
    code_ws = parent / "custom-task.code-workspace"
    assert code_ws.is_file()

    # User customizes the .code-workspace file
    code_ws.write_text('{"folders": [], "custom": true}\n')

    # Adding a second workspace must NOT overwrite user-edited file
    assert lab.ow("add", "-t", "custom-task", "b", "-r", "community", "--base", "18.0", cwd=parent).exit_code == 0
    assert '"custom": true' in code_ws.read_text()

    # Removing the task must also preserve the user-edited file
    assert lab.ow("rm", "-t", "custom-task", "-y").exit_code == 0
    assert code_ws.is_file()
    assert '"custom": true' in code_ws.read_text()
