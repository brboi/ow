"""`Setting.unique` only collides with workspaces that really hold the setting."""

from __future__ import annotations

from tests.conftest import add_repo

_PLUGIN = """
from ow.plugin_api import Setting, hookimpl

OW_API_VERSION = 1
PLUGIN_NAME = "slot"

@hookimpl
def ow_settings():
    return [Setting(key="slot.name", type=str, default="shared", help="Must differ per workspace", unique=True)]
"""


def _add_plugin_repo(lab) -> None:
    add_repo(
        lab,
        "slot",
        {"main": {"pyproject.toml": '[tool.ow]\nplugin = "ow_plugin.py"\n', "ow_plugin.py": _PLUGIN}},
        base="main",
    )


def test_a_repo_plugin_setting_does_not_collide_with_a_workspace_lacking_the_plugin(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    _add_plugin_repo(lab)
    assert lab.ow("trust", str(lab.home), cwd=lab.home).exit_code == 0
    plain = lab.home / "plain" / "w"
    with_plugin = lab.home / "with" / "w"
    for path in (plain, with_plugin):
        path.mkdir(parents=True)

    assert lab.ow("init", "community@18.0", cwd=plain).exit_code == 0
    res = lab.ow("init", "community@18.0,slot@main", cwd=with_plugin)
    assert res.exit_code == 0, res.stderr + res.stdout

    assert lab.ow("config", "get", "slot.name", cwd=with_plugin).stdout.strip() == "shared"
    assert "[slot]" not in (with_plugin / ".ow" / "config.toml").read_text()


def test_a_repo_plugin_setting_still_collides_between_workspaces_that_both_load_the_plugin(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    _add_plugin_repo(lab)
    assert lab.ow("trust", str(lab.home), cwd=lab.home).exit_code == 0
    first = lab.home / "one" / "w"
    second = lab.home / "two" / "w"
    for path in (first, second):
        path.mkdir(parents=True)

    assert lab.ow("init", "community@18.0,slot@main", cwd=first).exit_code == 0
    assert lab.ow("init", "community@18.0,slot@main", cwd=second).exit_code == 0

    assert lab.ow("config", "get", "slot.name", cwd=first).stdout.strip() == "shared"
    assert lab.ow("config", "get", "slot.name", cwd=second).stdout.strip() == "shared-2"
