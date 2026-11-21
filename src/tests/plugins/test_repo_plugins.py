"""Tests for in-repo plugins, `OW_API_VERSION` contract, `ow trust`, cross-stack hooks, and multi-stack config layers."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

from tests.conftest import add_repo


_VOIP_LIKE_PLUGIN = '''
from pathlib import PurePosixPath
from ow.plugin_api import (
    PLUGIN_API_VERSION,
    GeneratedFile,
    RunCommand,
    Setting,
    hookimpl,
    resolve_stack_python,
)

OW_API_VERSION = PLUGIN_API_VERSION
PLUGIN_NAME = "voipdemo"


@hookimpl
def ow_settings():
    return [
        Setting(
            key="voipdemo.client_stack",
            type=str,
            default=None,
            help="Override client stack name",
        ),
        Setting(
            key="voipdemo.db",
            type=str,
            default="voip-local",
            help="Client DB name",
        ),
    ]


@hookimpl
def ow_generated_files(ws, answers, multi):
    if not any(r.name == "voip" for r in ws.repos):
        return []
    if multi is None:
        return []
    target_name = ws.settings.get("voipdemo.client_stack")
    client = None
    for s in multi.stacks:
        if target_name:
            if s.name == target_name:
                client = s
                break
        elif any(r.name == "enterprise" for r in s.repos):
            client = s
            break
    if client is None:
        return []
    py = resolve_stack_python(client)
    db = ws.settings.get("voipdemo.db") or "voip-local"
    body = f"CLIENT={client.name}\\nPYTHON={py}\\nDB={db}\\n"
    return [GeneratedFile(path=PurePosixPath("voip/.dev/config"), data=body.encode("utf-8"))]


@hookimpl
def ow_multi_runner(multi, args):
    voip_stack = next((s for s in multi.stacks if any(r.name == "voip" for r in s.repos)), None)
    if voip_stack is None:
        return None
    marker = multi.root / "multi_runner.json"
    code = (
        "import json, sys; "
        f"open({str(marker)!r}, 'w').write(json.dumps({{'stack': {voip_stack.name!r}, 'args': sys.argv[1:]}}))"
    )
    return RunCommand(argv=(sys.executable, "-c", code, *args), cwd=voip_stack.root)
'''


def test_in_repo_plugin_trust_cross_stack_files_config_layers_and_multi_runner(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "enterprise", {"18.0": {"voip/__manifest__.py": "{}"}})
    add_repo(
        lab,
        "voip",
        {"master": {"ow_plugin.py": _VOIP_LIKE_PLUGIN.replace("sys.executable", repr(sys.executable))}},
        base="master",
    )

    ws = lab.home / "porting"
    ws.mkdir(parents=True)
    init_res = lab.ow("init", "client=@18.0", "voipstack=voip", cwd=ws)
    assert init_res.exit_code == 0, init_res.stderr + init_res.stdout

    # 1. Before trusting: warning emitted, plugin does NOT run, `.dev/config` not written
    assert "untrusted plugin" in init_res.stderr
    assert "ow trust" in init_res.stderr
    assert not (ws / "voipstack" / "voip" / ".dev" / "config").exists()

    # Non-Odoo stack `voipstack` has NO phantom Odoo ports pinned in root `.ow/config.toml`
    root_cfg = tomllib.loads((ws / ".ow" / "config.toml").read_text())
    assert "odoo" not in root_cfg["stacks"]["voipstack"]
    assert root_cfg["stacks"]["client"]["odoo"]["http_port"] == 8069

    # 2. `ow trust` trusts the plugin and refreshes immediately -> generates `voip/.dev/config`
    tr = lab.ow("trust", cwd=ws)
    assert tr.exit_code == 0, tr.stderr + tr.stdout
    assert "trusted " in tr.stdout

    dev_cfg = (ws / "voipstack" / "voip" / ".dev" / "config").read_text()
    assert "CLIENT=client" in dev_cfg
    assert "DB=voip-local" in dev_cfg
    assert "py3.12-18.0" in dev_cfg

    # 3. `ow trust --list` shows the canonical path
    ls_tr = lab.ow("trust", "--list", cwd=ws)
    assert ls_tr.exit_code == 0
    assert "voipstack/voip/ow_plugin.py" in ls_tr.stdout

    # 4. Workspace-wide `ow config set` at multi-stack root flows down to stacks and refreshes
    cfg_set = lab.ow("config", "set", "voipdemo.db", "custom-voip", cwd=ws)
    assert cfg_set.exit_code == 0, cfg_set.stderr + cfg_set.stdout
    root_cfg2 = tomllib.loads((ws / ".ow" / "config.toml").read_text())
    assert root_cfg2["voipdemo"]["db"] == "custom-voip"
    assert "DB=custom-voip" in (ws / "voipstack" / "voip" / ".dev" / "config").read_text()

    # Stack-level override wins over workspace-level setting
    stack_set = lab.ow("config", "set", "voipdemo.db", "stack-only-db", cwd=ws / "voipstack")
    assert stack_set.exit_code == 0, stack_set.stderr + stack_set.stdout
    assert "DB=stack-only-db" in (ws / "voipstack" / "voip" / ".dev" / "config").read_text()

    # 5. `ow run` at multi-stack root uses `ow_multi_runner` with direct args passthrough
    run_res = lab.ow("run", "--", "--flag", cwd=ws)
    assert run_res.exit_code == 0, run_res.stderr + run_res.stdout
    recorded = json.loads((ws / "multi_runner.json").read_text())
    assert recorded == {"stack": "voipstack", "args": ["--flag"]}

    # 6. `ow trust --untrust` revokes trust
    untr = lab.ow("trust", "--untrust", cwd=ws)
    assert untr.exit_code == 0
    assert "untrusted " in untr.stdout
    assert lab.ow("trust", "--list").stdout.strip() == ""


def test_in_repo_plugin_pyproject_discovery_and_api_version_contract(lab):
    bad_plugin = (
        "OW_API_VERSION = 99\n"
        "from ow.plugin_api import hookimpl\n"
        "@hookimpl\n"
        "def ow_settings(): return []\n"
    )
    good_plugin = (
        "OW_API_VERSION = 1\n"
        "PLUGIN_NAME = 'custompkg'\n"
        "from pathlib import PurePosixPath\n"
        "from ow.plugin_api import GeneratedFile, hookimpl\n"
        "@hookimpl\n"
        "def ow_generated_files(ws): return [GeneratedFile(PurePosixPath('hello.txt'), b'hi\\n')]\n"
    )

    add_repo(
        lab,
        "goodrepo",
        {
            "master": {
                "pyproject.toml": '[tool.ow]\nplugin = "tools/my_plugin.py"\n',
                "tools/my_plugin.py": good_plugin,
            }
        },
        base="master",
    )
    add_repo(
        lab,
        "badrepo",
        {"master": {"ow_plugin.py": bad_plugin}},
        base="master",
    )
    add_repo(
        lab,
        "escaperepo",
        {"master": {"pyproject.toml": '[tool.ow]\nplugin = "../outside.py"\n'}},
        base="master",
    )

    ws_good = lab.home / "ws-good"
    ws_good.mkdir(parents=True)
    assert lab.ow("init", "goodrepo", cwd=ws_good).exit_code == 0
    assert lab.ow("trust", cwd=ws_good).exit_code == 0
    assert (ws_good / "hello.txt").read_text() == "hi\n"

    ws_bad = lab.home / "ws-bad"
    ws_bad.mkdir(parents=True)
    assert lab.ow("init", "badrepo", cwd=ws_bad).exit_code == 0
    tr_bad = lab.ow("trust", cwd=ws_bad)
    assert tr_bad.exit_code == 0
    assert "incompatible OW_API_VERSION=99" in tr_bad.stderr

    # Path traversal in pyproject.toml is silently ignored
    ws_esc = lab.home / "ws-esc"
    ws_esc.mkdir(parents=True)
    esc_res = lab.ow("init", "escaperepo", cwd=ws_esc)
    assert esc_res.exit_code == 0
    assert "untrusted plugin" not in esc_res.stderr
