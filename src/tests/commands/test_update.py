import os
from pathlib import Path
from unittest.mock import patch

from ow.commands import cmd_update
from ow.utils.config import BranchSpec, Config, WorkspaceConfig, load_workspace_config, parse_branch_spec, write_workspace_config


def write_ow_config(ws_dir: Path, templates: list[str], repos: dict[str, str], vars: dict | None = None) -> None:
    ws = WorkspaceConfig(
        repos={alias: parse_branch_spec(spec) for alias, spec in repos.items()},
        templates=templates,
        vars=vars or {},
    )
    write_workspace_config(ws_dir / ".ow" / "config", ws)


# ---------------------------------------------------------------------------
# cmd_update
# ---------------------------------------------------------------------------

def test_cmd_update_renders_templates_and_materializes(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master"})

    with (
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
        patch("ow.commands.update.ensure_workspace_materialized", return_value=(ws_dir, {"community"}, {})) as mock_mat,
        patch("ow.commands.update.apply_templates") as mock_apply,
    ):
        cmd_update(config)

    mock_mat.assert_called_once()
    mock_apply.assert_called_once()


def test_cmd_update_merges_missing_vars(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master"}, vars={"http_port": 9090})
    config.vars = {"http_port": 8069, "db_host": "localhost"}

    with (
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
        patch("ow.commands.update.ensure_workspace_materialized", return_value=(ws_dir, {"community"}, {})),
        patch("ow.commands.update.apply_templates"),
    ):
        cmd_update(config)

    updated = load_workspace_config(ws_dir / ".ow" / "config")
    assert updated.vars["http_port"] == 9090
    assert updated.vars["db_host"] == "localhost"


def test_cmd_update_preserves_existing_vars(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master"}, vars={"http_port": 9090})
    config.vars = {"http_port": 8069}

    with (
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
        patch("ow.commands.update.ensure_workspace_materialized", return_value=(ws_dir, {"community"}, {})),
        patch("ow.commands.update.apply_templates"),
    ):
        cmd_update(config)

    updated = load_workspace_config(ws_dir / ".ow" / "config")
    assert updated.vars["http_port"] == 9090
