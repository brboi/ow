import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from jinja2 import Environment, FileSystemLoader
from ow.commands import cmd_create
from ow.commands.create import _check_duplicate_branches, _cleanup_failed_workspace
from ow.utils.display import Spinner
from ow.utils.drift import DriftResult, check_drift, warn_if_drifted
from ow.commands.prune import _prune_bare_repo
from ow.commands.rebase import _analyze_repo_for_rebase, _recover_with_cherry_pick
from ow.utils.templates import build_template_context, find_addon_paths
from ow.utils.config import BranchSpec, WorkspaceConfig, write_workspace_config

TEMPLATE_DIR = Path(__file__).parent.parent / "ow" / "_static" / "templates" / "common"
VSCODE_TEMPLATE_DIR = Path(__file__).parent.parent / "ow" / "_static" / "templates" / "vscode"
ZED_TEMPLATE_DIR = Path(__file__).parent.parent / "ow" / "_static" / "templates" / "zed"


def setup_odoo_main_repo(ws_dir: Path, alias: str = "community") -> Path:
    repo = ws_dir / alias
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "odoo-bin").touch()
    (repo / "addons" / "sale").mkdir(parents=True)
    (repo / "addons" / "sale" / "__manifest__.py").touch()
    (repo / "odoo" / "addons" / "base").mkdir(parents=True)
    (repo / "odoo" / "addons" / "base" / "__manifest__.py").touch()
    return repo


def setup_flat_repo(ws_dir: Path, alias: str) -> Path:
    repo = ws_dir / alias
    (repo / "account").mkdir(parents=True)
    (repo / "account" / "__manifest__.py").touch()
    (repo / "sale").mkdir(parents=True)
    (repo / "sale" / "__manifest__.py").touch()
    return repo


def setup_categorized_repo(ws_dir: Path, alias: str) -> Path:
    repo = ws_dir / alias
    (repo / "telephony" / "phone_validation").mkdir(parents=True)
    (repo / "telephony" / "phone_validation" / "__manifest__.py").touch()
    (repo / "messaging" / "sms_gateway").mkdir(parents=True)
    (repo / "messaging" / "sms_gateway" / "__manifest__.py").touch()
    return repo


def make_ws_config(aliases: list[str], templates: list[str] | None = None) -> WorkspaceConfig:
    return WorkspaceConfig(
        repos={alias: BranchSpec("origin/master") for alias in aliases},
        templates=templates or ["common"],
    )


def render_template(name: str, context: dict, template_dir: Path = TEMPLATE_DIR) -> str:
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env.get_template(name).render(context)


# ---------------------------------------------------------------------------
# find_addon_paths
# ---------------------------------------------------------------------------


def test_find_addon_paths_on_file(tmp_path):
    f = tmp_path / "somefile.txt"
    f.touch()
    assert find_addon_paths(f) == []


def test_find_addon_paths_nonexistent(tmp_path):
    assert find_addon_paths(tmp_path / "nonexistent") == []


def test_find_addon_paths_empty_dir(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    assert find_addon_paths(d) == []


def test_find_addon_paths_flat_repo(tmp_path):
    repo = setup_flat_repo(tmp_path, "myaddon")
    assert find_addon_paths(repo) == [repo]


def test_find_addon_paths_categorized_repo(tmp_path):
    repo = setup_categorized_repo(tmp_path, "myaddon")
    result = find_addon_paths(repo)
    assert result == sorted([repo / "messaging", repo / "telephony"])


def test_find_addon_paths_mixed_depths(tmp_path):
    repo = tmp_path / "repo"
    # helpers/utils/__manifest__.py  → helpers is addons_path
    (repo / "helpers" / "utils").mkdir(parents=True)
    (repo / "helpers" / "utils" / "__manifest__.py").touch()
    # categories/crm/sale_crm/__manifest__.py  → crm is addons_path
    (repo / "categories" / "crm" / "sale_crm").mkdir(parents=True)
    (repo / "categories" / "crm" / "sale_crm" / "__manifest__.py").touch()
    # external/vendor/payments/stripe/__manifest__.py → payments is addons_path
    (repo / "external" / "vendor" / "payments" / "stripe").mkdir(parents=True)
    (repo / "external" / "vendor" / "payments" / "stripe" / "__manifest__.py").touch()

    result = find_addon_paths(repo)
    assert result == sorted(
        [
            repo / "categories" / "crm",
            repo / "external" / "vendor" / "payments",
            repo / "helpers",
        ]
    )


# ---------------------------------------------------------------------------
# build_template_context
# ---------------------------------------------------------------------------


def test_build_template_context_community_only(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config(["community"])
    ctx = build_template_context(ws, config, ws_dir)

    assert ctx["ws_name"] == "test"
    assert ctx["main_repo_alias"] == "community"
    assert ctx["repos"] == ["community"]
    assert str(ws_dir / "community" / "addons") in ctx["addons_paths"]
    assert str(ws_dir / "community" / "odoo" / "addons") in ctx["addons_paths"]
    assert "community/addons" in ctx["odools_path_items"]
    assert "community/odoo/addons" in ctx["odools_path_items"]


def test_build_template_context_addons_order(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    setup_flat_repo(ws_dir, "enterprise")
    ws = make_ws_config(["community", "enterprise"])
    ctx = build_template_context(ws, config, ws_dir)

    # enterprise before community in addons_paths
    ent_idx = next(i for i, p in enumerate(ctx["addons_paths"]) if "enterprise" in p)
    comm_idx = next(
        i for i, p in enumerate(ctx["addons_paths"]) if "community/addons" in p
    )
    assert ent_idx < comm_idx

    # enterprise before community in odools_path_items
    ent_idx = next(
        i for i, p in enumerate(ctx["odools_path_items"]) if "enterprise" in p
    )
    comm_idx = next(
        i for i, p in enumerate(ctx["odools_path_items"]) if "community/addons" in p
    )
    assert ent_idx < comm_idx


def test_build_template_context_vars_merge(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = WorkspaceConfig(
        repos={"community": BranchSpec("origin/master")},
        templates=["common"],
        vars={"http_port": 8070},
    )
    ctx = build_template_context(ws, config, ws_dir)

    assert ctx["vars"]["http_port"] == 8070
    assert "db_host" in ctx["vars"]


def test_build_template_context_full_workspace(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    setup_flat_repo(ws_dir, "enterprise")
    setup_flat_repo(ws_dir, "brboi-addons")
    ws = make_ws_config(["community", "enterprise", "brboi-addons"])
    ctx = build_template_context(ws, config, ws_dir)

    assert ctx["repos"] == ["community", "enterprise", "brboi-addons"]
    assert len([p for p in ctx["addons_paths"] if "community" in p]) == 2
    assert (
        len(
            [p for p in ctx["addons_paths"] if "enterprise" in p or "brboi-addons" in p]
        )
        == 2
    )


def test_build_template_context_no_main_repo(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_flat_repo(ws_dir, "enterprise")
    ws = make_ws_config(["enterprise"])
    ctx = build_template_context(ws, config, ws_dir)

    assert ctx["main_repo_alias"] is None


# ---------------------------------------------------------------------------
# Template rendering — odoorc
# ---------------------------------------------------------------------------


def test_render_odoorc_community_only(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config(["community"])
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template("odoorc.j2", ctx)

    assert "[options]" in result
    assert "http_port = 8069" in result
    assert "db_host = localhost" in result
    assert "community/addons" in result
    assert "community/odoo/addons" in result
    assert "db_name = test" in result
    assert "dbfilter = ^test$" in result


def test_render_odoorc_enterprise_before_community(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    setup_flat_repo(ws_dir, "enterprise")
    ws = make_ws_config(["community", "enterprise"])
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template("odoorc.j2", ctx)

    lines = result.split("\n")
    addons_line = next(l for l in lines if l.startswith("addons_path"))
    paths = addons_line.split("=", 1)[1].strip().split(",")
    assert "enterprise" in paths[0]
    assert "community/addons" in paths[1]
    assert "community/odoo/addons" in paths[2]


def test_render_odoorc_workspace_overrides_global(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = WorkspaceConfig(
        repos={"community": BranchSpec("origin/master")},
        templates=["common"],
        vars={"http_port": 8070},
    )
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template("odoorc.j2", ctx)

    assert "http_port = 8070" in result
    assert "http_port = 8069" not in result


def test_render_odoorc_no_quotes_on_string_values(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config(["community"])
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template("odoorc.j2", ctx)

    assert 'db_host = "localhost"' not in result
    assert "db_host = localhost" in result


# ---------------------------------------------------------------------------
# Template rendering — odools.toml
# ---------------------------------------------------------------------------


def test_render_odools_community_only(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config(["community"])
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template("odools.toml.j2", ctx)

    assert "[[config]]" in result
    assert "[Odoo Workspace] test" in result
    assert 'python_path = ".venv/bin/python"' in result
    assert 'odoo_path = "./community"' in result
    assert "./community/addons" in result
    assert "./community/odoo/addons" in result
    assert "./enterprise" not in result


def test_render_odools_enterprise_before_community(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    setup_flat_repo(ws_dir, "enterprise")
    ws = make_ws_config(["community", "enterprise"])
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template("odools.toml.j2", ctx)

    assert "./enterprise" in result
    ent_idx = result.index("./enterprise")
    com_idx = result.index("./community/addons")
    assert ent_idx < com_idx


def test_render_odools_categorized_repo(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    setup_categorized_repo(ws_dir, "partner-addons")
    ws = make_ws_config(["community", "partner-addons"])
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template("odools.toml.j2", ctx)

    assert "./partner-addons/messaging" in result
    assert "./partner-addons/telephony" in result
    assert "./community/addons" in result
    msg_idx = result.index("./partner-addons/messaging")
    com_idx = result.index("./community/addons")
    assert msg_idx < com_idx


# ---------------------------------------------------------------------------
# Template rendering — mise.toml
# ---------------------------------------------------------------------------


def test_render_mise_toml(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config(["community"])
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template("mise.toml.j2", ctx)

    assert "[tools]" in result
    assert "python" in result
    assert "[hooks]" in result
    assert "community/requirements.txt" in result
    assert "[env]" in result
    assert ".venv" in result
    assert "{{config_root}}/community" in result


# ---------------------------------------------------------------------------
# Template rendering — pyrightconfig.json
# ---------------------------------------------------------------------------


def test_render_pyrightconfig(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config(["community"])
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template("pyrightconfig.json.j2", ctx)
    data = json.loads(result)

    assert data["venvPath"] == "."
    assert data["venv"] == ".venv"
    assert data["pythonVersion"] == "3.12"
    assert "./community" in data["extraPaths"]
    assert data["typeCheckingMode"] == "off"


# ---------------------------------------------------------------------------
# Template rendering — .vscode
# ---------------------------------------------------------------------------


def test_render_vscode_settings(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config(["community"])
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template(".vscode/settings.json.j2", ctx, VSCODE_TEMPLATE_DIR)

    assert "[Odoo Workspace] test" in result


def test_render_vscode_launch(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config(["community"])
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template(".vscode/launch.json.j2", ctx, VSCODE_TEMPLATE_DIR)

    assert "debugpy" in result
    assert "${workspaceFolder}/community" in result
    assert "odoo-bin" in result
    assert "odoorc" in result


def test_render_vscode_launch_default_args(tmp_path, config):
    """Default debug_args includes --dev=all and --with-demo."""
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config(["community"])
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template(".vscode/launch.json.j2", ctx, VSCODE_TEMPLATE_DIR)
    parsed = json.loads(result)

    run_config = parsed["configurations"][0]
    assert run_config["args"] == ["--dev=all", "--with-demo"]

    test_config = parsed["configurations"][1]
    assert test_config["args"] == ["--test-tags=test"]
    assert test_config["name"] == "Debug Tests (test)"


def test_render_vscode_launch_custom_args(tmp_path, config):
    """Custom debug_args and debug_test_args override defaults."""
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = WorkspaceConfig(
        repos={"community": BranchSpec("origin/18.0")},
        templates=["common", "vscode"],
        vars={
            "debug_args": ["--dev=all"],
            "debug_test_args": ["--test-tags=/phone_service"],
        },
    )
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template(".vscode/launch.json.j2", ctx, VSCODE_TEMPLATE_DIR)
    parsed = json.loads(result)

    run_config = parsed["configurations"][0]
    assert run_config["args"] == ["--dev=all"]

    test_config = parsed["configurations"][1]
    assert test_config["args"] == ["--test-tags=/phone_service"]


def test_render_zed_settings(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    setup_flat_repo(ws_dir, "enterprise")
    ws = make_ws_config(["community", "enterprise"])
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template(".zed/settings.json.j2", ctx, ZED_TEMPLATE_DIR)

    assert "community/**" in result
    assert "enterprise/**" in result
    assert "[Odoo Workspace] test" in result
    assert '"mise.toml"' in result
    assert '"odools.toml"' in result
    assert '"pyrightconfig.json"' in result
    assert '"**/.venv"' in result


def test_render_zed_settings_full_workspace(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    setup_flat_repo(ws_dir, "enterprise")
    setup_flat_repo(ws_dir, "brboi-addons")
    ws = make_ws_config(["community", "enterprise", "brboi-addons"])
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template(".zed/settings.json.j2", ctx, ZED_TEMPLATE_DIR)

    assert "community/**" in result
    assert "enterprise/**" in result
    assert "brboi-addons/**" in result


def test_render_zed_debug(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config(["community"])
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template(".zed/debug.json.j2", ctx, ZED_TEMPLATE_DIR)

    assert "Debugpy" in result
    assert "${ZED_WORKTREE_ROOT}/community" in result
    assert "odoo-bin" in result
    assert "${ZED_WORKTREE_ROOT}/.venv/bin/python" in result
    assert "odoorc" in result


def test_render_zed_debug_default_args(tmp_path, config):
    """Default debug_args includes --dev=all and --with-demo."""
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config(["community"])
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template(".zed/debug.json.j2", ctx, ZED_TEMPLATE_DIR)
    lines = [l for l in result.splitlines() if not l.strip().startswith("//")]
    clean = re.sub(r',(\s*[}\]])', r'\1', "\n".join(lines))
    parsed = json.loads(clean)

    run_config = parsed[0]
    assert run_config["args"] == ["--dev=all", "--with-demo"]

    test_config = parsed[1]
    assert test_config["args"] == ["--test-tags=test"]
    assert test_config["label"] == "Debug Tests (test)"


def test_render_zed_debug_custom_args(tmp_path, config):
    """Custom debug_args and debug_test_args override defaults."""
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = WorkspaceConfig(
        repos={"community": BranchSpec("origin/18.0")},
        templates=["common", "zed"],
        vars={
            "debug_args": ["--dev=all"],
            "debug_test_args": ["--test-tags=/voip_pbx"],
        },
    )
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template(".zed/debug.json.j2", ctx, ZED_TEMPLATE_DIR)
    lines = [l for l in result.splitlines() if not l.strip().startswith("//")]
    clean = re.sub(r',(\s*[}\]])', r'\1', "\n".join(lines))
    parsed = json.loads(clean)

    run_config = parsed[0]
    assert run_config["args"] == ["--dev=all"]

    test_config = parsed[1]
    assert test_config["args"] == ["--test-tags=/voip_pbx"]


# ---------------------------------------------------------------------------
# DriftResult
# ---------------------------------------------------------------------------


def test_drift_result_detached_config_detached_worktree():
    """Config says detached, worktree is detached — no drift."""
    dr = DriftResult(
        alias="community", spec=BranchSpec("origin/master"), actual_branch=None
    )
    assert dr.is_drifted is False


def test_drift_result_detached_config_worktree_on_branch():
    """Config says detached, worktree is on a branch — drift."""
    dr = DriftResult(
        alias="community", spec=BranchSpec("origin/master"), actual_branch="some-branch"
    )
    assert dr.is_drifted is True


def test_drift_result_attached_config_correct_branch():
    """Config says branch X, worktree is on branch X — no drift."""
    dr = DriftResult(
        alias="community",
        spec=BranchSpec("origin/master", "my-feature"),
        actual_branch="my-feature",
    )
    assert dr.is_drifted is False


def test_drift_result_attached_config_worktree_detached():
    """Config says branch X, worktree is detached — drift."""
    dr = DriftResult(
        alias="community",
        spec=BranchSpec("origin/master", "my-feature"),
        actual_branch=None,
    )
    assert dr.is_drifted is True


def test_drift_result_attached_config_wrong_branch():
    """Config says branch X, worktree is on branch Y — drift."""
    dr = DriftResult(
        alias="community",
        spec=BranchSpec("origin/master", "my-feature"),
        actual_branch="other-branch",
    )
    assert dr.is_drifted is True


def test_drift_result_message_detached_drift():
    dr = DriftResult(
        alias="community",
        spec=BranchSpec("origin/master"),
        actual_branch="rogue-branch",
    )
    msg = dr.message
    assert "community" in msg
    assert "detached" in msg
    assert "rogue-branch" in msg


def test_drift_result_message_attached_drift():
    dr = DriftResult(
        alias="enterprise",
        spec=BranchSpec("origin/master", "my-feature"),
        actual_branch=None,
    )
    msg = dr.message
    assert "enterprise" in msg
    assert "my-feature" in msg
    assert "detached HEAD" in msg


# ---------------------------------------------------------------------------
# check_drift
# ---------------------------------------------------------------------------


def test_check_drift_uses_get_worktree_branch(tmp_path):
    worktree_path = tmp_path / "community"
    worktree_path.mkdir()
    spec = BranchSpec("origin/master", "my-feature")

    with patch("ow.utils.drift.get_worktree_branch", return_value="my-feature"):
        result = check_drift(worktree_path, spec, "community")

    assert result.alias == "community"
    assert result.actual_branch == "my-feature"
    assert result.is_drifted is False


def test_check_drift_detects_wrong_branch(tmp_path):
    worktree_path = tmp_path / "community"
    worktree_path.mkdir()
    spec = BranchSpec("origin/master", "my-feature")

    with patch("ow.utils.drift.get_worktree_branch", return_value="other-branch"):
        result = check_drift(worktree_path, spec, "community")

    assert result.is_drifted is True


# ---------------------------------------------------------------------------
# warn_if_drifted
# ---------------------------------------------------------------------------


def test_warn_if_drifted_passes_when_aligned(tmp_path, capsys):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    ws = WorkspaceConfig(
        repos={"community": BranchSpec("origin/master", "my-feature")},
        templates=["common"],
    )

    with patch("ow.utils.drift.get_worktree_branch", return_value="my-feature"):
        warn_if_drifted(ws, ws_dir)

    captured = capsys.readouterr()
    assert "Warning" not in captured.err


def test_warn_if_drifted_warns_on_drift(tmp_path, capsys):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    ws = WorkspaceConfig(
        repos={"community": BranchSpec("origin/master", "my-feature")},
        templates=["common"],
    )

    with patch("ow.utils.drift.get_worktree_branch", return_value="wrong-branch"):
        warn_if_drifted(ws, ws_dir)

    captured = capsys.readouterr()
    assert "Warning" in captured.err


def test_warn_if_drifted_skips_unapplied_repos(tmp_path, capsys):
    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)
    ws = WorkspaceConfig(
        repos={"community": BranchSpec("origin/master", "my-feature")},
        templates=["common"],
    )

    warn_if_drifted(ws, ws_dir)

    captured = capsys.readouterr()
    assert "Warning" not in captured.err


# ---------------------------------------------------------------------------
# cmd_create — checkbox uses Choice objects
# ---------------------------------------------------------------------------


def test_cmd_create_checkbox_uses_choice_objects(tmp_path, config):
    """cmd_create must use questionary.Choice objects, none selected by default."""
    (tmp_path / "templates" / "zed").mkdir(parents=True)
    (tmp_path / "templates" / "common").mkdir(parents=True)
    (tmp_path / "templates" / "vscode").mkdir(parents=True)
    config.remotes = {
        "brboi-addons": {"origin": MagicMock(url="git@github.com:brboi/addons.git")},
        "community": {"origin": MagicMock(url="git@github.com:odoo/odoo.git")},
    }

    checkbox_calls = []

    def mock_checkbox(message, choices=None, **kwargs):
        checkbox_calls.append({"message": message, "choices": choices})
        mock = MagicMock()
        if "Templates" in message:
            mock.ask.return_value = ["common", "vscode"]
        else:
            mock.ask.return_value = ["brboi-addons", "community"]
        return mock

    def mock_text(message):
        mock = MagicMock()
        if "Workspace name" in message:
            mock.ask.return_value = "test"
        elif "branch spec" in message:
            mock.ask.return_value = "master"
        else:
            mock.ask.return_value = ""
        return mock

    def mock_confirm(message):
        mock = MagicMock()
        mock.ask.return_value = True
        return mock

    with (
        patch("questionary.checkbox", side_effect=mock_checkbox),
        patch("questionary.text", side_effect=mock_text),
        patch("questionary.confirm", side_effect=mock_confirm),
        patch("ow.commands.create.ensure_workspace_materialized", return_value=(tmp_path / "workspaces" / "test", {"brboi-addons", "community"}, {})),
        patch("ow.commands.create.apply_templates"),
        patch("ow.commands.create.write_workspace_config"),
        patch("ow.commands.create.run_cmd"),
    ):
        cmd_create(config)

    # Verify templates are alphabetical and unchecked
    template_checkbox = checkbox_calls[0]
    assert "Templates" in template_checkbox["message"]
    template_names = [c.title for c in template_checkbox["choices"]]
    assert template_names == ["common", "vscode", "zed"]  # alphabetical
    for choice in template_checkbox["choices"]:
        assert not choice.checked  # none selected by default

    # Verify remotes are in declaration order and unchecked
    repo_checkbox = checkbox_calls[1]
    assert "Repos" in repo_checkbox["message"]
    repo_names = [c.title for c in repo_checkbox["choices"]]
    assert repo_names == ["brboi-addons", "community"]  # declaration order, not sorted
    for choice in repo_checkbox["choices"]:
        assert not choice.checked  # none selected by default


def test_cmd_create_rejects_existing_workspace(tmp_path, config):
    """cmd_create loops when workspace name already exists."""
    (tmp_path / "templates" / "common").mkdir(parents=True)
    (tmp_path / "workspaces" / "parrot").mkdir(parents=True)
    config.remotes = {"community": {"origin": MagicMock(url="git@github.com:odoo/odoo.git")}}

    call_count = [0]

    def mock_checkbox(message, choices=None, **kwargs):
        mock = MagicMock()
        if "Templates" in message:
            mock.ask.return_value = ["common"]
        else:
            mock.ask.return_value = ["community"]
        return mock

    def mock_text(message):
        if "Workspace name" in message:
            call_count[0] += 1
            mock = MagicMock()
            if call_count[0] == 1:
                mock.ask.return_value = "parrot"  # already exists
            else:
                mock.ask.return_value = "new-ws"  # valid
        elif "branch spec" in message:
            mock = MagicMock()
            mock.ask.return_value = "master"
        else:
            mock = MagicMock()
            mock.ask.return_value = ""
        return mock

    def mock_confirm(message):
        mock = MagicMock()
        mock.ask.return_value = True
        return mock

    with (
        patch("questionary.checkbox", side_effect=mock_checkbox),
        patch("questionary.text", side_effect=mock_text),
        patch("questionary.confirm", side_effect=mock_confirm),
        patch("ow.commands.create.ensure_workspace_materialized", return_value=(tmp_path / "workspaces" / "new-ws", {"community"}, {})),
        patch("ow.commands.create.apply_templates"),
        patch("ow.commands.create.write_workspace_config"),
        patch("ow.commands.create.run_cmd"),
    ):
        cmd_create(config)

    assert call_count[0] == 2  # asked twice: first name rejected, second accepted


class TestSpinner:
    def test_spinner_context_manager(self, capsys):
        with Spinner("Testing"):
            pass
        captured = capsys.readouterr()
        assert "\r" in captured.out
        assert "Testing" in captured.out

    def test_spinner_clears_on_exit(self, capsys):
        with Spinner("Prefix"):
            pass
        captured = capsys.readouterr()
        assert captured.out.endswith("\r")

    def test_spinner_animates(self, capsys):
        import time
        with Spinner("Anim"):
            time.sleep(0.25)
        captured = capsys.readouterr()
        assert captured.out.count("\r") >= 2


# ---------------------------------------------------------------------------
# _cleanup_failed_workspace
# ---------------------------------------------------------------------------


def test_cleanup_failed_workspace_removes_if_empty(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)
    _cleanup_failed_workspace(ws_dir)
    assert not ws_dir.exists()


def test_cleanup_failed_workspace_removes_if_only_ow_dir(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / ".ow").mkdir(parents=True)
    _cleanup_failed_workspace(ws_dir)
    assert not ws_dir.exists()


def test_cleanup_failed_workspace_keeps_if_has_files(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)
    (ws_dir / "somefile.txt").touch()
    _cleanup_failed_workspace(ws_dir)
    assert ws_dir.exists()
    assert (ws_dir / "somefile.txt").exists()


def test_cleanup_failed_workspace_does_nothing_if_not_exists(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    _cleanup_failed_workspace(ws_dir)  # should not raise
    assert not ws_dir.exists()


# ---------------------------------------------------------------------------
# _check_duplicate_branches
# ---------------------------------------------------------------------------


def test_check_duplicate_branches_detects_same_local_branch(tmp_path, capsys, config):
    """Abort if new repo shares local_branch with existing workspace on same alias."""
    ws_root = tmp_path / "workspaces"
    existing_ws = ws_root / "existing"
    (existing_ws / ".ow").mkdir(parents=True)
    existing_config = WorkspaceConfig(
        repos={"community": BranchSpec("origin/master", "shared-branch")},
        templates=["common"],
    )
    write_workspace_config(existing_ws / ".ow" / "config", existing_config)

    new_repos = {"community": BranchSpec("origin/master", "shared-branch")}

    with pytest.raises(SystemExit) as exc_info:
        _check_duplicate_branches(new_repos, config)
    assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert "existing" in captured.err
    assert "shared-branch" in captured.err


def test_check_duplicate_branches_no_duplicate_if_different_local_branch(tmp_path, capsys, config):
    """No abort if local_branch differs."""
    ws_root = tmp_path / "workspaces"
    existing_ws = ws_root / "existing"
    (existing_ws / ".ow").mkdir(parents=True)
    existing_config = WorkspaceConfig(
        repos={"community": BranchSpec("origin/master", "other-branch")},
        templates=["common"],
    )
    write_workspace_config(existing_ws / ".ow" / "config", existing_config)

    new_repos = {"community": BranchSpec("origin/master", "my-branch")}

    _check_duplicate_branches(new_repos, config)  # should not raise

    captured = capsys.readouterr()
    assert "Error" not in captured.err


def test_check_duplicate_branches_no_duplicate_if_different_alias(tmp_path, capsys, config):
    """No abort if alias differs — git allows same branch on different aliases."""
    ws_root = tmp_path / "workspaces"
    existing_ws = ws_root / "existing"
    (existing_ws / ".ow").mkdir(parents=True)
    existing_config = WorkspaceConfig(
        repos={"enterprise": BranchSpec("origin/master", "shared-branch")},
        templates=["common"],
    )
    write_workspace_config(existing_ws / ".ow" / "config", existing_config)

    new_repos = {"community": BranchSpec("origin/master", "shared-branch")}

    _check_duplicate_branches(new_repos, config)  # should not raise

    captured = capsys.readouterr()
    assert "Error" not in captured.err


def test_check_duplicate_branches_ignores_workspaces_without_ow_config(tmp_path, capsys, config):
    """Skip workspaces that have no .ow/config file."""
    ws_root = tmp_path / "workspaces"
    existing_ws = ws_root / "existing"
    existing_ws.mkdir(parents=True)
    # No .ow/config created

    new_repos = {"community": BranchSpec("origin/master", "some-branch")}

    _check_duplicate_branches(new_repos, config)  # should not raise

    captured = capsys.readouterr()
    assert "Error" not in captured.err


def test_check_duplicate_branches_silent_if_no_existing_workspaces(tmp_path, capsys, config):
    """Return silently when no workspaces exist yet."""
    new_repos = {"community": BranchSpec("origin/master", "some-branch")}

    _check_duplicate_branches(new_repos, config)  # should not raise

    captured = capsys.readouterr()
    assert captured.err == ""


# ---------------------------------------------------------------------------
# _recover_with_cherry_pick
# ---------------------------------------------------------------------------


def test_recover_with_cherry_pick_success_returns_none(tmp_path):
    """All cherry-picks succeed → returns None."""
    worktree = tmp_path / "repo"
    worktree.mkdir()
    commits = ["aaa111", "bbb222", "ccc333"]

    mock_reset = MagicMock()
    mock_cp = MagicMock()
    mock_cp.return_value = MagicMock(returncode=0)
    mock_log = MagicMock(return_value="hash some message")

    with patch("ow.commands.rebase.git_reset_hard", mock_reset), \
         patch("ow.commands.rebase.git_cherry_pick", mock_cp), \
         patch("ow.commands.rebase.git_log_oneline", mock_log):
        result = _recover_with_cherry_pick(worktree, "origin/master", commits)

    assert result is None
    mock_reset.assert_called_once_with(worktree, "origin/master")
    assert mock_cp.call_count == 3
    mock_cp.assert_any_call(worktree, "aaa111")
    mock_cp.assert_any_call(worktree, "bbb222")
    mock_cp.assert_any_call(worktree, "ccc333")


def test_recover_with_cherry_pick_conflict_on_second_commit_returns_hash(tmp_path):
    """Conflict on 2nd cherry-pick → returns the failing commit hash."""
    worktree = tmp_path / "repo"
    worktree.mkdir()
    commits = ["aaa111", "bbb222", "ccc333"]

    call_count = [0]

    def mock_cp_side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 2:
            return MagicMock(returncode=1)
        return MagicMock(returncode=0)

    mock_reset = MagicMock()
    mock_cp = MagicMock(side_effect=mock_cp_side_effect)
    mock_log = MagicMock(return_value="hash some message")

    with patch("ow.commands.rebase.git_reset_hard", mock_reset), \
         patch("ow.commands.rebase.git_cherry_pick", mock_cp), \
         patch("ow.commands.rebase.git_log_oneline", mock_log):
        result = _recover_with_cherry_pick(worktree, "origin/master", commits)

    assert result == "bbb222"
    assert mock_cp.call_count == 2  # stops after the conflict


# ---------------------------------------------------------------------------
# _analyze_repo_for_rebase
# ---------------------------------------------------------------------------


def test_analyze_repo_normal_rebase_no_rewrite(tmp_path):
    """Normal rebase: no upstream rewrite, no conflicts."""
    worktree = tmp_path / "repo"
    worktree.mkdir()

    with patch("ow.commands.rebase.get_rev_list_count") as mock_rev_count, \
         patch("ow.commands.rebase.git_merge_base_fork_point", return_value=None), \
         patch("ow.commands.rebase.git_rev_list", return_value=[]), \
         patch("ow.utils.drift.get_worktree_branch", return_value="my-feature"):
        mock_rev_count.side_effect = [(3, True), (0, True)]  # local=3, unpushed=0

        plan = _analyze_repo_for_rebase(worktree, "origin/master", "origin/master", "community", False)

    assert plan.alias == "community"
    assert plan.track_ref == "origin/master"
    assert plan.upstream == "origin/master"
    assert plan.is_detached is False
    assert plan.local_commits == 3
    assert plan.unpushed_commits == 0
    assert plan.fork_point is None
    assert plan.commits_to_reapply == []
    assert plan.upstream_rewritten is False
    assert plan.has_conflicts is False


def test_analyze_repo_upstream_rewritten_with_fork_point(tmp_path):
    """Upstream rewritten but fork-point exists → recovery possible."""
    worktree = tmp_path / "repo"
    worktree.mkdir()
    fork = "abc123"
    commits_list = ["def456", "ghi789"]

    with patch("ow.commands.rebase.get_rev_list_count") as mock_rev_count, \
         patch("ow.commands.rebase.git_merge_base_fork_point", return_value=fork), \
         patch("ow.commands.rebase.git_rev_list", return_value=commits_list), \
         patch("ow.commands.rebase.get_worktree_branch", return_value="my-feature"):
        mock_rev_count.side_effect = [(2, True), (2, True)]  # local=2, unpushed=2

        plan = _analyze_repo_for_rebase(worktree, "origin/master", "origin/master", "community", False)

    assert plan.fork_point == fork
    assert plan.commits_to_reapply == commits_list
    assert plan.upstream_rewritten is False  # fork_point found, so not "rewritten"
    assert plan.unpushed_commits == 2


def test_analyze_repo_upstream_rewritten_without_fork_point(tmp_path):
    """Upstream rewritten and no fork-point → no recovery."""
    worktree = tmp_path / "repo"
    worktree.mkdir()

    with patch("ow.commands.rebase.get_rev_list_count") as mock_rev_count, \
         patch("ow.commands.rebase.git_merge_base_fork_point", return_value=None), \
         patch("ow.commands.rebase.git_rev_list", return_value=[]), \
         patch("ow.utils.drift.get_worktree_branch", return_value="my-feature"):
        mock_rev_count.side_effect = [(2, True), (2, True)]  # local=2, unpushed=2

        plan = _analyze_repo_for_rebase(worktree, "origin/master", "origin/master", "community", False)

    assert plan.fork_point is None
    assert plan.commits_to_reapply == []
    assert plan.upstream_rewritten is True  # no fork_point AND unpushed > 0
    assert plan.unpushed_commits == 2


def test_analyze_repo_rebase_in_progress(tmp_path):
    """rebase-merge directory exists → has_conflicts."""
    worktree = tmp_path / "repo"
    (worktree / ".git").mkdir(parents=True)
    (worktree / ".git" / "rebase-merge").mkdir()

    with patch("ow.commands.rebase.get_rev_list_count", return_value=(1, True)), \
         patch("ow.commands.rebase.git_merge_base_fork_point", return_value=None), \
         patch("ow.commands.rebase.git_rev_list", return_value=[]), \
         patch("ow.utils.drift.get_worktree_branch", return_value="my-feature"):
        plan = _analyze_repo_for_rebase(worktree, "origin/master", "origin/master", "community", False)

    assert plan.has_conflicts is True


def test_analyze_repo_detached_worktree(tmp_path):
    """Detached worktree → is_detached True, no fork-point lookup."""
    worktree = tmp_path / "repo"
    worktree.mkdir()

    with patch("ow.commands.rebase.get_rev_list_count", return_value=(0, True)), \
         patch("ow.commands.rebase.git_merge_base_fork_point", return_value=None) as mock_fork, \
         patch("ow.commands.rebase.git_rev_list", return_value=[]), \
         patch("ow.utils.drift.get_worktree_branch", return_value=None):
        plan = _analyze_repo_for_rebase(worktree, "origin/master", "origin/master", "community", True)

    assert plan.is_detached is True
    assert mock_fork.call_count == 0


def test_prune_bare_repo_strips_plus_prefix(tmp_path):
    """Branch names with + prefix (worktree branches) are correctly parsed."""
    bare_repo = tmp_path / "community.git"
    bare_repo.mkdir()

    wt_result = MagicMock(returncode=0)
    wt_result.stdout = "worktree /path/to/ws/community\nHEAD abc123\nbranch refs/heads/main-parrot\n"

    branch_result = MagicMock(returncode=0)
    branch_result.stdout = "+ main-parrot\n  other-branch\n"

    with patch("ow.commands.prune.subprocess.run", side_effect=[MagicMock(returncode=0), wt_result, branch_result, MagicMock(returncode=0)]):
        result = _prune_bare_repo(bare_repo)

    assert "main-parrot" not in result.deleted_branches
    assert "other-branch" in result.deleted_branches
