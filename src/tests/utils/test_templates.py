import pytest
import json
import re
import subprocess
import tomllib
from pathlib import Path
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader

from ow.utils.templates import (
    ABSENT,
    NOT_RENDERED,
    OUTDATED,
    RENDERED_LOCK,
    UP_TO_DATE,
    YOURS,
    apply_templates,
    bundle_source_files,
    build_template_context,
    effective_bundles,
    ensure_workspace_materialized,
    find_addon_paths,
    legacy_mise_toml,
    rendered_states,
    selectable_templates,
)
from ow.utils.config import BranchSpec, Config, WorkspaceConfig

TEMPLATE_DIR = Path(__file__).parent.parent.parent / "ow" / "_static" / "templates" / "common"
ODOO_TEMPLATE_DIR = Path(__file__).parent.parent.parent / "ow" / "_static" / "templates" / "odoo"
VSCODE_TEMPLATE_DIR = Path(__file__).parent.parent.parent / "ow" / "_static" / "templates" / "vscode"
ZED_TEMPLATE_DIR = Path(__file__).parent.parent.parent / "ow" / "_static" / "templates" / "zed"


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


def make_ws_config(
    aliases: list[str],
    templates: list[str] | None = None,
    vars: dict | None = None,
) -> WorkspaceConfig:
    return WorkspaceConfig(
        repos={alias: BranchSpec("origin/master") for alias in aliases},
        templates=templates if templates is not None else [],
        vars=vars if vars is not None else {"http_port": 8069, "db_host": "localhost", "db_port": 5432},
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
    # helpers/utils/__manifest__.py  -> helpers is addons_path
    (repo / "helpers" / "utils").mkdir(parents=True)
    (repo / "helpers" / "utils" / "__manifest__.py").touch()
    # categories/crm/sale_crm/__manifest__.py  -> crm is addons_path
    (repo / "categories" / "crm" / "sale_crm").mkdir(parents=True)
    (repo / "categories" / "crm" / "sale_crm" / "__manifest__.py").touch()
    # external/vendor/payments/stripe/__manifest__.py -> payments is addons_path
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

def test_find_addon_paths_handles_symlink_cycle(tmp_path):
    """A symlink cycle must not raise RecursionError."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "addons").mkdir()
    (root / "addons" / "cycle").symlink_to(root / "addons")
    # Should not raise RecursionError
    result = find_addon_paths(root)
    assert isinstance(result, list)


def test_find_addon_paths_prunes_noise_dirs(tmp_path):
    """.git, node_modules, __pycache__ are skipped even if they contain addons."""
    root = tmp_path / "repo"
    root.mkdir()
    # Create noise directories with addon structures inside
    for noise in (".git", "node_modules", "__pycache__"):
        d = root / noise / "addons" / "fake_addon"
        d.mkdir(parents=True)
        (d / "__manifest__.py").touch()
    # Create a real addons directory
    (root / "real_addons" / "real_addon").mkdir(parents=True)
    (root / "real_addons" / "real_addon" / "__manifest__.py").touch()
    result = find_addon_paths(root)
    # Should find root/real_addons but not root/.git/addons, etc.
    assert result == [root / "real_addons"]


def test_find_addon_paths_ignores_python_packages(tmp_path):
    """A bare __init__.py is a Python package marker, not an addon marker.

    Reproduces #44: an Ansible repo whose role trees hold ordinary Python
    packages had every one of their parents rendered into addons_path.
    """
    repo = tmp_path / "repo"
    # A plain Python package, nested the way an Ansible role tree nests them.
    pkg = repo / "roles" / "prom_node" / "files" / "etc" / "prometheus"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").touch()
    # A real addon, so the walk has something legitimate to find.
    addon = repo / "custom" / "my_addon"
    addon.mkdir(parents=True)
    (addon / "__manifest__.py").touch()

    assert find_addon_paths(repo) == [repo / "custom"]


def test_find_addon_paths_prunes_hidden_dirs(tmp_path):
    """An addon never lives in .venv, .odoo or .ow — and a vendored Odoo does."""
    root = tmp_path / "repo"
    root.mkdir()
    for hidden in (".venv", ".odoo", ".ow"):
        d = root / hidden / "addons" / "vendored_addon"
        d.mkdir(parents=True)
        (d / "__manifest__.py").touch()
    (root / "real_addons" / "real_addon").mkdir(parents=True)
    (root / "real_addons" / "real_addon" / "__manifest__.py").touch()

    assert find_addon_paths(root) == [root / "real_addons"]


def test_find_addon_paths_honours_exclude(tmp_path):
    """Excluded directories are not entered at all."""
    root = tmp_path / "repo"
    for name in ("mine", "theirs"):
        d = root / name / "an_addon"
        d.mkdir(parents=True)
        (d / "__manifest__.py").touch()

    assert find_addon_paths(root, exclude=[root / "theirs"]) == [root / "mine"]


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


def test_build_template_context_vars_are_ws_vars_only(tmp_path, config):
    """#40: global vars are only seeds at init; rendering sees ws.vars alone."""
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = WorkspaceConfig(
        repos={"community": BranchSpec("origin/master")},
        templates=["common"],
        vars={"http_port": 8070},
    )
    ctx = build_template_context(ws, config, ws_dir)

    assert ctx["vars"] == {"http_port": 8070}
    assert "db_host" not in ctx["vars"]


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


def test_build_template_context_has_services_keys(tmp_path, config, xdg):
    """build_template_context exposes ws_dir, services_compose, volumes_dir."""
    from ow.utils import paths

    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config(["community"])
    ctx = build_template_context(ws, config, ws_dir)

    assert "ws_dir" in ctx
    assert ctx["ws_dir"] == str(ws_dir)
    assert "services_compose" in ctx
    assert ctx["services_compose"] == str(paths.services_dir() / "compose.yml")
    assert "volumes_dir" in ctx
    assert ctx["volumes_dir"] == str(paths.volumes_dir())

# ---------------------------------------------------------------------------
# Template rendering - odoorc
# ---------------------------------------------------------------------------

def test_render_odoorc_community_only(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config(["community"])
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template("odoorc.j2", ctx, ODOO_TEMPLATE_DIR)

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
    result = render_template("odoorc.j2", ctx, ODOO_TEMPLATE_DIR)

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
    result = render_template("odoorc.j2", ctx, ODOO_TEMPLATE_DIR)

    assert "http_port = 8070" in result
    assert "http_port = 8069" not in result


def test_render_odoorc_no_quotes_on_string_values(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config(["community"])
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template("odoorc.j2", ctx, ODOO_TEMPLATE_DIR)

    assert 'db_host = "localhost"' not in result
    assert "db_host = localhost" in result


def test_render_odoorc_with_data_dir(tmp_path, config):
    """odoorc sets data_dir to <ws_dir>/.odoo."""
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config(["community"])
    apply_templates(ws, config, ws_dir)
    content = (ws_dir / "odoorc").read_text()
    assert "data_dir" in content
    assert str(ws_dir) + "/.odoo" in content


# ---------------------------------------------------------------------------
# Template rendering - odools.toml
# ---------------------------------------------------------------------------

def test_render_odools_community_only(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config(["community"])
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template("odools.toml.j2", ctx, ODOO_TEMPLATE_DIR)

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
    result = render_template("odools.toml.j2", ctx, ODOO_TEMPLATE_DIR)

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
    result = render_template("odools.toml.j2", ctx, ODOO_TEMPLATE_DIR)

    assert "./partner-addons/messaging" in result
    assert "./partner-addons/telephony" in result
    assert "./community/addons" in result
    msg_idx = result.index("./partner-addons/messaging")
    com_idx = result.index("./community/addons")
    assert msg_idx < com_idx


# ---------------------------------------------------------------------------
# Template rendering - mise.toml
# ---------------------------------------------------------------------------

def test_render_mise_toml(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config(["community"])
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template("mise/conf.d/00-ow.toml.j2", ctx)

    assert "[tools]" in result
    assert "python" in result
    assert "[hooks]" in result
    assert "community/requirements.txt" in result
    assert ".venv" in result
    assert "{{config_root}}/community" in result


def test_render_mise_toml_exports_an_absolute_ow_workspace(tmp_path, config):
    """OW_WORKSPACE takes one form — an absolute path. A name is rejected.

    mise expands {{config_root}} to the directory holding mise.toml, which is
    the workspace itself. Exporting the bare name here made every ow command
    run under mise fail on the variable the workspace generated for itself.
    """
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config(["community"])
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template("mise/conf.d/00-ow.toml.j2", ctx)

    exported = re.search(r'^OW_WORKSPACE = "(.*)"$', result, re.M)
    assert exported, "the common bundle must export OW_WORKSPACE"
    assert exported.group(1) == "{{config_root}}"


def test_render_mise_toml_with_compose_file(tmp_path, config, xdg):
    """mise.toml exports COMPOSE_FILE pointing at the services compose path."""
    from ow.utils import paths
    from ow.utils.templates import ensure_services_compose
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config(["community"])
    ensure_services_compose()
    apply_templates(ws, config, ws_dir)
    content = (ws_dir / "mise" / "conf.d" / "00-ow.toml").read_text()
    assert "COMPOSE_FILE" in content
    assert str(paths.services_dir() / "compose.yml") in content


# ---------------------------------------------------------------------------
# Template rendering - pyrightconfig.json
# ---------------------------------------------------------------------------

def test_render_pyrightconfig(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config(["community"])
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template("pyrightconfig.json.j2", ctx, ODOO_TEMPLATE_DIR)
    data = json.loads(result)

    assert data["venvPath"] == "."
    assert data["venv"] == ".venv"
    assert data["pythonVersion"] == "3.12"
    assert "./community" in data["extraPaths"]
    assert data["typeCheckingMode"] == "off"


# ---------------------------------------------------------------------------
# Template rendering - .vscode
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


# ---------------------------------------------------------------------------
# Template rendering - .zed
# ---------------------------------------------------------------------------

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
# A workspace with no Odoo core repo — issue #45's acceptance test
#
# `main_repo_alias` is None whenever nothing in the workspace ships odoo-bin —
# an enterprise-only or addons-only workspace, or a core worktree that is not
# materialised yet. The `odoo` bundle is never declared by hand: it renders
# only once a repo turns out to have odoo-bin, so a workspace that never runs
# Odoo receives none of its files at all.
# ---------------------------------------------------------------------------

PACKAGED_BUNDLES = ["bwrap", "vscode", "zed"]

ODOO_ONLY_OUTPUTS = (
    "odoorc",
    "odools.toml",
    "pyrightconfig.json",
    "requirements-dev.txt",
    Path(".vscode") / "launch.json",
    Path(".zed") / "debug.json",
)

ALWAYS_RENDERED_OUTPUTS = (
    Path("mise") / "conf.d" / "00-ow.toml",
    Path(".vscode") / "settings.json",
    Path(".zed") / "settings.json",
)


def strip_jsonc(text: str) -> str:
    """Zed writes JSONC: // comments and trailing commas."""
    lines = [l for l in text.splitlines() if not l.strip().startswith("//")]
    return re.sub(r",(\s*[}\]])", r"\1", "\n".join(lines))


def render_every_bundle(tmp_path: Path, config: Config, with_core: bool) -> Path:
    """Apply every packaged bundle to a workspace with, or without, an Odoo core repo."""
    ws_dir = tmp_path / "workspaces" / ("odoows" if with_core else "plainws")
    ws_dir.mkdir(parents=True)
    if with_core:
        setup_odoo_main_repo(ws_dir, "community")
        ws = make_ws_config(["community"], templates=PACKAGED_BUNDLES)
    else:
        setup_flat_repo(ws_dir, "plain")
        ws = make_ws_config(["plain"], templates=PACKAGED_BUNDLES)
    ctx = build_template_context(ws, config, ws_dir)
    assert (ctx["main_repo_alias"] == "community") is with_core
    apply_templates(ws, config, ws_dir)
    return ws_dir


def rendered_files(ws_dir: Path) -> list[Path]:
    """Everything the templates wrote, excluding the repo worktrees."""
    return [
        p
        for p in sorted(ws_dir.rglob("*"))
        if p.is_file() and p.relative_to(ws_dir).parts[0] not in ("plain", "community")
    ]


def test_no_odoo_file_exists_without_a_core_repo(tmp_path, config):
    ws_dir = render_every_bundle(tmp_path, config, with_core=False)
    for rel in ODOO_ONLY_OUTPUTS:
        assert not (ws_dir / rel).exists(), f"{rel} must not exist without an Odoo core repo"


def test_common_and_editor_files_still_render_without_a_core_repo(tmp_path, config):
    ws_dir = render_every_bundle(tmp_path, config, with_core=False)
    for rel in ALWAYS_RENDERED_OUTPUTS:
        assert (ws_dir / rel).is_file(), f"{rel} must be rendered for every workspace"


def test_odoo_files_appear_once_a_core_repo_is_present(tmp_path, config):
    ws_dir = render_every_bundle(tmp_path, config, with_core=True)
    for rel in ODOO_ONLY_OUTPUTS:
        assert (ws_dir / rel).is_file(), f"{rel} must exist once an Odoo core repo is present"


def test_no_packaged_template_renders_a_literal_none(tmp_path, config):
    ws_dir = render_every_bundle(tmp_path, config, with_core=False)
    written = rendered_files(ws_dir)
    assert written, "the bundles must write something"
    for path in written:
        assert not re.search(r"\bNone\b", path.read_text()), (
            f"{path.relative_to(ws_dir)} names a directory called None"
        )


def test_mise_fragment_parses_and_carries_no_odoo_wiring_without_core(tmp_path, config):
    ws_dir = render_every_bundle(tmp_path, config, with_core=False)
    content = (ws_dir / "mise" / "conf.d" / "00-ow.toml").read_text()
    data = tomllib.loads(content)

    assert "ODOO_RC" not in content
    assert "osh" not in content
    assert data["env"]["_"]["path"] == ["{{config_root}}"]


def test_mise_fragment_wires_odoo_once_a_core_repo_is_present(tmp_path, config):
    ws_dir = render_every_bundle(tmp_path, config, with_core=True)
    content = (ws_dir / "mise" / "conf.d" / "00-ow.toml").read_text()
    data = tomllib.loads(content)

    assert "ODOO_RC" in content
    assert data["shell_alias"]["osh"]


def test_vscode_settings_parses_strict_json_and_never_mentions_odoo_without_core(tmp_path, config):
    ws_dir = render_every_bundle(tmp_path, config, with_core=False)
    content = (ws_dir / ".vscode" / "settings.json").read_text()

    json.loads(content)  # strict JSON: no trailing commas, no comments
    assert "Odoo" not in content


def test_zed_settings_parses_after_stripping_jsonc_and_never_mentions_odoo_without_core(tmp_path, config):
    ws_dir = render_every_bundle(tmp_path, config, with_core=False)
    content = (ws_dir / ".zed" / "settings.json").read_text()

    json.loads(strip_jsonc(content))
    assert "Odoo" not in content


# ---------------------------------------------------------------------------
# ensure_workspace_materialized — reconcile loop error handling
# ---------------------------------------------------------------------------

class TestEnsureWorkspaceMaterializedReconcileErrors:
    """A failure in the reconcile loop must land in errors like any other repo failure."""

    def test_attach_failure_is_caught_and_reported(self, tmp_path):
        """Currently detached + resolved attached → attach_worktree; failure caught."""
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        config = Config(remotes={}, vars={})
        ws = WorkspaceConfig(
            repos={
                "community": BranchSpec("origin/master", "my-feature"),
                "enterprise": BranchSpec("origin/master", "ent-feature"),
            },
            templates=[],
        )

        with patch("ow.utils.templates.ensure_bare_repo"):
            with patch("ow.utils.templates.resolve_spec") as mr:
                mr.side_effect = [
                    BranchSpec("origin/master", "my-feature"),
                    BranchSpec("origin/master", "ent-feature"),
                ]
                with patch(
                    "ow.utils.templates.parallel_per_repo",
                    return_value={
                        "community": BranchSpec("origin/master", "my-feature"),
                        "enterprise": BranchSpec("origin/master", "ent-feature"),
                    },
                ):
                    with patch("ow.utils.templates.worktree_exists", return_value=True):
                        with patch(
                            "ow.utils.templates.worktree_is_detached",
                            side_effect=[True, False],
                        ):
                            with patch(
                                "ow.utils.templates.get_worktree_branch",
                                return_value="ent-feature",
                            ):
                                with patch(
                                    "ow.utils.templates.attach_worktree"
                                ) as mock_attach:
                                    mock_attach.side_effect = subprocess.CalledProcessError(
                                        128, ["git", "switch"], stderr=b"fatal: bla"
                                    )
                                    with patch(
                                        "ow.utils.templates.set_branch_upstream"
                                    ):
                                        with patch("ow.utils.templates.run_cmd"):
                                            _, successful, errors = (
                                            ensure_workspace_materialized(
                                                ws, config, ws_dir
                                            )
                                        )

        assert "community" in errors
        assert "community" not in successful
        assert "enterprise" in successful

    def test_detach_failure_is_caught_and_reported(self, tmp_path):
        """Currently attached + resolved detached → detach_worktree; failure caught."""
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        config = Config(remotes={}, vars={})
        ws = WorkspaceConfig(
            repos={
                "community": BranchSpec("origin/master"),
                "enterprise": BranchSpec("origin/master"),
            },
            templates=[],
        )

        with patch("ow.utils.templates.ensure_bare_repo"):
            with patch("ow.utils.templates.resolve_spec") as mr:
                mr.side_effect = [
                    BranchSpec("origin/master"),
                    BranchSpec("origin/master"),
                ]
                with patch(
                    "ow.utils.templates.parallel_per_repo",
                    return_value={
                        "community": BranchSpec("origin/master"),
                        "enterprise": BranchSpec("origin/master"),
                    },
                ):
                    with patch("ow.utils.templates.worktree_exists", return_value=True):
                        with patch(
                            "ow.utils.templates.worktree_is_detached",
                            side_effect=[False, True],
                        ):
                            with patch(
                                "ow.utils.templates.detach_worktree"
                            ) as mock_detach:
                                mock_detach.side_effect = subprocess.CalledProcessError(
                                    128, ["git", "switch", "--detach"], stderr=b"fatal: bla"
                                )
                                with patch("ow.utils.templates.run_cmd"):
                                    _, successful, errors = (
                                        ensure_workspace_materialized(
                                            ws, config, ws_dir
                                        )
                                    )

        assert "community" in errors
        assert "community" not in successful
        assert "enterprise" in successful

    def test_create_worktree_failure_is_caught_and_reported(self, tmp_path):
        """Missing worktree + resolved attached → create_worktree; failure caught."""
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        config = Config(remotes={}, vars={})
        ws = WorkspaceConfig(
            repos={
                "community": BranchSpec("origin/master", "my-feature"),
                "enterprise": BranchSpec("origin/master", "ent-feature"),
            },
            templates=[],
        )

        with patch("ow.utils.templates.ensure_bare_repo"):
            with patch("ow.utils.templates.resolve_spec") as mr:
                mr.side_effect = [
                    BranchSpec("origin/master", "my-feature"),
                    BranchSpec("origin/master", "ent-feature"),
                ]
                with patch(
                    "ow.utils.templates.parallel_per_repo",
                    return_value={
                        "community": BranchSpec("origin/master", "my-feature"),
                        "enterprise": BranchSpec("origin/master", "ent-feature"),
                    },
                ):
                    with patch(
                        "ow.utils.templates.worktree_exists",
                        side_effect=[False, True],
                    ):
                        with patch(
                            "ow.utils.templates.create_worktree"
                        ) as mock_create:
                            mock_create.side_effect = subprocess.CalledProcessError(
                                128, ["git", "worktree", "add"], stderr=b"fatal: bla"
                            )
                            with patch(
                                "ow.utils.templates.set_branch_upstream"
                            ):
                                with patch("ow.utils.templates.run_cmd"):
                                    with patch(
                                        "ow.utils.templates.worktree_is_detached",
                                        return_value=False,
                                    ):
                                        with patch(
                                            "ow.utils.templates.get_worktree_branch",
                                            return_value="ent-feature",
                                        ):
                                            _, successful, errors = (
                                            ensure_workspace_materialized(
                                                ws, config, ws_dir
                                            )
                                        )

        assert "community" in errors
        assert "community" not in successful
        assert "enterprise" in successful

    def test_in_progress_operation_is_named_in_error(self, tmp_path):
        """A mid-rebase worktree is named and told how to finish or abort."""
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        config = Config(remotes={}, vars={})
        ws = WorkspaceConfig(
            repos={
                "community": BranchSpec("origin/master", "my-feature"),
                "enterprise": BranchSpec("origin/master", "ent-feature"),
            },
            templates=[],
        )

        with patch("ow.utils.templates.ensure_bare_repo"):
            with patch("ow.utils.templates.resolve_spec") as mr:
                mr.side_effect = [
                    BranchSpec("origin/master", "my-feature"),
                    BranchSpec("origin/master", "ent-feature"),
                ]
                with patch(
                    "ow.utils.templates.parallel_per_repo",
                    return_value={
                        "community": BranchSpec("origin/master", "my-feature"),
                        "enterprise": BranchSpec("origin/master", "ent-feature"),
                    },
                ):
                    with patch("ow.utils.templates.worktree_exists", return_value=True):
                        with patch(
                            "ow.utils.templates.worktree_is_detached",
                            side_effect=[True, False],
                        ):
                            with patch(
                                "ow.utils.templates.get_worktree_branch",
                                return_value="ent-feature",
                            ):
                                with patch(
                                    "ow.utils.templates.attach_worktree"
                                ) as mock_attach:
                                    mock_attach.side_effect = subprocess.CalledProcessError(
                                        128, ["git", "switch"], stderr=b"fatal: bla"
                                    )
                                    with patch(
                                        "ow.utils.templates.set_branch_upstream"
                                    ):
                                        with patch("ow.utils.templates.run_cmd"):
                                            with patch(
                                                "ow.utils.templates.in_progress_operation",
                                                return_value=(
                                                    "rebase",
                                                    "git rebase --continue",
                                                    "git rebase --abort",
                                                ),
                                            ):
                                                _, successful, errors = (
                                                    ensure_workspace_materialized(
                                                        ws, config, ws_dir
                                                    )
                                                )

        assert "community" in errors
        assert "rebase" in errors["community"]
        assert "git rebase --continue" in errors["community"]
        assert "git rebase --abort" in errors["community"]
        assert "enterprise" in successful

    def test_broken_git_file_error_is_caught_and_reported(self, tmp_path):
        """A .git file pointing at a moved bare repo raises CalledProcessError."""
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        config = Config(remotes={}, vars={})
        ws = WorkspaceConfig(
            repos={
                "community": BranchSpec("origin/master", "my-feature"),
                "enterprise": BranchSpec("origin/master", "ent-feature"),
            },
            templates=[],
        )

        with patch("ow.utils.templates.ensure_bare_repo"):
            with patch("ow.utils.templates.resolve_spec") as mr:
                mr.side_effect = [
                    BranchSpec("origin/master", "my-feature"),
                    BranchSpec("origin/master", "ent-feature"),
                ]
                with patch(
                    "ow.utils.templates.parallel_per_repo",
                    return_value={
                        "community": BranchSpec("origin/master", "my-feature"),
                        "enterprise": BranchSpec("origin/master", "ent-feature"),
                    },
                ):
                    with patch("ow.utils.templates.worktree_exists", return_value=True):
                        with patch(
                            "ow.utils.templates.worktree_is_detached",
                            side_effect=[True, False],
                        ):
                            with patch(
                                "ow.utils.templates.get_worktree_branch",
                                return_value="ent-feature",
                            ):
                                with patch(
                                    "ow.utils.templates.attach_worktree"
                                ) as mock_attach:
                                    mock_attach.side_effect = subprocess.CalledProcessError(
                                        128, ["git", "switch"], stderr=b"fatal: not a git repository"
                                    )
                                    with patch(
                                        "ow.utils.templates.set_branch_upstream"
                                    ):
                                        with patch("ow.utils.templates.run_cmd"):
                                            with patch(
                                                "ow.utils.templates.in_progress_operation",
                                                return_value=None,
                                            ):
                                                _, successful, errors = (
                                                    ensure_workspace_materialized(
                                                        ws, config, ws_dir
                                                    )
                                                )

        assert "community" in errors
        assert "community" not in successful
        assert "enterprise" in successful


# ---------------------------------------------------------------------------
# ensure_services_compose
# ---------------------------------------------------------------------------

def test_ensure_services_compose_materializes_file(xdg):
    from ow.utils import paths
    from ow.utils.templates import ensure_services_compose
    path = ensure_services_compose()
    assert path == paths.services_dir() / "compose.yml"
    assert path.exists()
    content = path.read_text()
    # volumes_dir should be resolved (not a Jinja template)
    assert "{{" not in content
    assert str(paths.volumes_dir()) in content


def test_ensure_services_compose_is_idempotent(xdg):
    from ow.utils import paths
    from ow.utils.templates import ensure_services_compose
    path = ensure_services_compose()
    first_mtime = path.stat().st_mtime_ns
    path2 = ensure_services_compose()
    assert path == path2
    # Should not rewrite if unchanged
    assert path2.stat().st_mtime_ns == first_mtime


# ---------------------------------------------------------------------------
# StrictUndefined — missing variables must raise, not render empty
# ---------------------------------------------------------------------------

def test_undefined_var_raises_at_render(xdg, tmp_path, config):
    """A local template referencing an unknown variable must raise UndefinedError."""
    from jinja2 import UndefinedError
    from ow.utils import paths

    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)

    # Create a local "common" template that references an undefined variable.
    local_dir = paths.templates_dir() / "common"
    local_dir.mkdir(parents=True)
    (local_dir / "odoorc.j2").write_text("data_dir = {{ undefined_var }}\n")

    ws = WorkspaceConfig(repos={}, templates=["common"])
    with pytest.raises(UndefinedError):
        apply_templates(ws, config, ws_dir)


# ---------------------------------------------------------------------------
# Two-pass rendering — an addon shipped by a template must reach addons_path
# ---------------------------------------------------------------------------

def test_apply_templates_sees_an_addon_a_template_created(xdg, tmp_path, config):
    """#42: build_template_context reads the filesystem, so an addon that a
    bundle materialises does not exist yet on the first pass."""
    from ow.utils import paths

    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")

    # A local bundle that ships an addon of its own, plus the odoorc that
    # has to name it.
    local_dir = paths.templates_dir() / "local"
    (local_dir / "custom" / "my_addon").mkdir(parents=True)
    (local_dir / "custom" / "my_addon" / "__manifest__.py").write_text("{}\n")
    (local_dir / "odoorc.j2").write_text("addons_path = {{ addons_paths | join(',') }}\n")

    ws = WorkspaceConfig(
        repos={"community": BranchSpec("origin/master")},
        templates=["local"],
    )
    apply_templates(ws, config, ws_dir)

    addons_path = (ws_dir / "odoorc").read_text()
    assert str(ws_dir / "custom") in addons_path


def test_apply_templates_is_idempotent(xdg, tmp_path, config):
    """A second run must produce the same bytes — the two-pass driver relies on it."""
    from ow.utils import paths

    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")

    local_dir = paths.templates_dir() / "local"
    (local_dir / "custom" / "my_addon").mkdir(parents=True)
    (local_dir / "custom" / "my_addon" / "__manifest__.py").write_text("{}\n")
    (local_dir / "odoorc.j2").write_text("addons_path = {{ addons_paths | join(',') }}\n")

    ws = WorkspaceConfig(
        repos={"community": BranchSpec("origin/master")},
        templates=["local"],
    )
    apply_templates(ws, config, ws_dir)
    first = (ws_dir / "odoorc").read_bytes()
    apply_templates(ws, config, ws_dir)

    assert (ws_dir / "odoorc").read_bytes() == first


def test_apply_templates_renders_once_when_nothing_appears(xdg, tmp_path, config):
    """The second pass is a fallback, not a tax on every apply."""
    from ow.utils import paths
    from ow.utils import templates as templates_mod

    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")

    local_dir = paths.templates_dir() / "local"
    local_dir.mkdir(parents=True)
    (local_dir / "odoorc.j2").write_text("db_name = {{ ws_name }}\n")

    ws = WorkspaceConfig(
        repos={"community": BranchSpec("origin/master")},
        templates=["local"],
    )
    with patch.object(
        templates_mod, "_render_outputs", wraps=templates_mod._render_outputs,
    ) as render:
        apply_templates(ws, config, ws_dir)

    assert render.call_count == 1


def make_addon_bundle() -> Path:
    """A local bundle whose output only the second pass can render.

    It ships an addon of its own — which the first pass writes, so the first
    pass's addon scan cannot see it — and a template that names what the scan
    found. The template renders empty on the first pass and real on the
    second.
    """
    from ow.utils import paths

    local = paths.templates_dir() / "local"
    (local / "custom" / "my_addon").mkdir(parents=True, exist_ok=True)
    (local / "custom" / "my_addon" / "__manifest__.py").write_text("{}\n")
    (local / "addons.txt.j2").write_text(
        "{% if addons_paths %}addons = {{ addons_paths | join(',') }}\n{% endif %}"
    )
    return local


def test_apply_templates_reports_an_addon_dependent_output_it_wrote(xdg, tmp_path, config):
    """The first pass renders nothing at a path whose content depends on the
    addon the same pass is about to write; the second pass writes it. The
    output is reported as written — "not rendered" would name a file that is
    sitting right there."""
    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)
    make_addon_bundle()

    ws = WorkspaceConfig(repos={}, templates=["local"])
    result = apply_templates(ws, config, ws_dir)

    assert str(ws_dir / "custom") in (ws_dir / "addons.txt").read_text()
    assert "addons.txt" in result.wrote
    assert "addons.txt" not in result.skipped


def test_preexisting_divergent_output_survives_the_first_apply(xdg, tmp_path, config):
    """A file that was already there, differing from the render, with no lock
    to speak for it: it is the user's from the first apply on, and the second
    pass must not quietly adopt it by writing what the first pass could not
    render."""
    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)
    local_dir = make_addon_bundle()
    (ws_dir / "addons.txt").write_text("my own words\n")

    ws = WorkspaceConfig(repos={}, templates=["local"])
    result = apply_templates(ws, config, ws_dir)

    assert (ws_dir / "addons.txt").read_text() == "my own words\n"
    assert "addons.txt" in result.yours
    assert "addons.txt" not in result.wrote
    assert "addons.txt" not in result.skipped

    # Not adopted into the lock: a render that moves still leaves it alone.
    (local_dir / "addons.txt.j2").write_text("addons = gone\n")
    again = apply_templates(ws, config, ws_dir)

    assert (ws_dir / "addons.txt").read_text() == "my own words\n"
    assert "addons.txt" in again.yours


def test_an_output_the_second_pass_only_adopts_is_not_reported_as_skipped(
    xdg, tmp_path, config
):
    """The second pass finds the file already equal to what it renders: it
    adopts it and writes nothing, which is not a skip either — the file the
    first pass wanted nothing at is on disk and correct."""
    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)
    make_addon_bundle()
    (ws_dir / "addons.txt").write_text(f"addons = {ws_dir / 'custom'}\n")

    ws = WorkspaceConfig(repos={}, templates=["local"])
    result = apply_templates(ws, config, ws_dir)

    assert "addons.txt" not in result.skipped
    assert "addons.txt" in result.managed
    assert "addons.txt" not in result.wrote
    assert "addons.txt" not in result.yours


# ---------------------------------------------------------------------------
# bundle_source_files — hybrid source resolution (local overrides packaged
# per file, never per bundle)
# ---------------------------------------------------------------------------

class TestResolveBundleFiles:
    def test_partial_local_bundle_does_not_hide_packaged_siblings(self, xdg):
        """A local bundle holding one file must not hide its packaged siblings."""
        from ow.utils import paths

        local = paths.templates_dir() / "odoo"
        local.mkdir(parents=True)
        (local / "odoorc.j2").write_text("local override")

        result = bundle_source_files("odoo")

        # The customised file resolves to the local copy.
        assert result[Path("odoorc.j2")] == local / "odoorc.j2"
        assert result[Path("odoorc.j2")].read_text() == "local override"

        # Its siblings still resolve to the packaged versions, by path.
        packaged_dir = (
            Path(__file__).parent.parent.parent / "ow" / "_static" / "templates" / "odoo"
        )
        assert result[Path("pyrightconfig.json.j2")] == packaged_dir / "pyrightconfig.json.j2"
        assert result[Path("requirements-dev.txt")] == packaged_dir / "requirements-dev.txt"
        assert result[Path("odools.toml.j2")] == packaged_dir / "odools.toml.j2"

    def test_purely_local_bundle_resolves(self, xdg):
        from ow.utils import paths

        local = paths.templates_dir() / "my-custom"
        local.mkdir(parents=True)
        (local / "only.txt").write_text("only file")

        result = bundle_source_files("my-custom")

        assert result == {Path("only.txt"): local / "only.txt"}

    def test_purely_packaged_bundle_resolves(self, xdg):
        result = bundle_source_files("vscode")

        packaged_dir = (
            Path(__file__).parent.parent.parent / "ow" / "_static" / "templates" / "vscode"
        )
        expected_rel = Path(".vscode") / "settings.json.j2"
        assert result[expected_rel] == packaged_dir / ".vscode" / "settings.json.j2"

    def test_unknown_bundle_resolves_to_empty(self, xdg):
        assert bundle_source_files("does-not-exist") == {}


# ---------------------------------------------------------------------------
# The rendered lock (#45) — apply_templates locks *what ow wrote*, not the
# template it wrote it from. A local bundle stands in for a packaged one:
# the rule under test is the lock's four-way split on an output path,
# independent of which bundle produced it.
# ---------------------------------------------------------------------------


class TestRenderedLock:
    def _local_bundle(self, content: str = "hello\n") -> Path:
        from ow.utils import paths

        local = paths.templates_dir() / "mine"
        local.mkdir(parents=True)
        (local / "greeting.txt").write_text(content)
        return local

    def _ws(self, tmp_path: Path) -> tuple[WorkspaceConfig, Path]:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        return WorkspaceConfig(repos={}, templates=["mine"]), ws_dir

    def _own(self, names: list[str]) -> list[str]:
        """This bundle's own output, filtered from the common bundle's mise
        fragment — every workspace renders it too, first-apply included."""
        return [n for n in names if n == "greeting.txt"]

    def test_first_apply_writes_and_locks(self, xdg, tmp_path, config):
        self._local_bundle()
        ws, ws_dir = self._ws(tmp_path)

        result = apply_templates(ws, config, ws_dir)

        assert (ws_dir / "greeting.txt").read_text() == "hello\n"
        assert self._own(result.wrote) == ["greeting.txt"]
        assert (ws_dir / RENDERED_LOCK).is_file()

    def test_untouched_output_follows_a_moved_source(self, xdg, tmp_path, config):
        local = self._local_bundle()
        ws, ws_dir = self._ws(tmp_path)
        apply_templates(ws, config, ws_dir)

        (local / "greeting.txt").write_text("goodbye\n")
        result = apply_templates(ws, config, ws_dir)

        assert (ws_dir / "greeting.txt").read_text() == "goodbye\n"
        assert result.updated == ["greeting.txt"]

    def test_hand_edited_output_survives_a_moved_source(self, xdg, tmp_path, config):
        local = self._local_bundle()
        ws, ws_dir = self._ws(tmp_path)
        apply_templates(ws, config, ws_dir)

        (ws_dir / "greeting.txt").write_text("my own words\n")
        (local / "greeting.txt").write_text("goodbye\n")
        result = apply_templates(ws, config, ws_dir)

        assert (ws_dir / "greeting.txt").read_text() == "my own words\n"
        assert result.yours == ["greeting.txt"]

        # A second apply still leaves it alone.
        again = apply_templates(ws, config, ws_dir)
        assert (ws_dir / "greeting.txt").read_text() == "my own words\n"
        assert again.yours == ["greeting.txt"]

    def test_preexisting_identical_file_is_adopted_silently(self, xdg, tmp_path, config):
        local = self._local_bundle()
        ws, ws_dir = self._ws(tmp_path)
        (ws_dir / "greeting.txt").write_text("hello\n")  # a file from before the lock existed

        result = apply_templates(ws, config, ws_dir)

        assert self._own(result.wrote) == []
        assert self._own(result.updated) == []
        assert self._own(result.yours) == []
        assert self._own(result.skipped) == []
        assert (ws_dir / "greeting.txt").read_text() == "hello\n"

        # The lock now protects it exactly as if ow had written it itself:
        # moving the source updates it on the next apply.
        (local / "greeting.txt").write_text("goodbye\n")
        second = apply_templates(ws, config, ws_dir)
        assert (ws_dir / "greeting.txt").read_text() == "goodbye\n"
        assert second.updated == ["greeting.txt"]

    def test_deleted_output_is_rewritten_on_next_apply(self, xdg, tmp_path, config):
        self._local_bundle()
        ws, ws_dir = self._ws(tmp_path)
        apply_templates(ws, config, ws_dir)
        (ws_dir / "greeting.txt").unlink()

        result = apply_templates(ws, config, ws_dir)

        assert (ws_dir / "greeting.txt").read_text() == "hello\n"
        assert result.wrote == ["greeting.txt"]

    def test_whitespace_only_render_writes_nothing(self, xdg, tmp_path, config):
        from ow.utils import paths

        local = paths.templates_dir() / "mine"
        local.mkdir(parents=True)
        (local / "greeting.txt.j2").write_text("   \n\n")

        ws, ws_dir = self._ws(tmp_path)
        result = apply_templates(ws, config, ws_dir)

        assert not (ws_dir / "greeting.txt").exists()
        assert result.skipped == ["greeting.txt"]


# ---------------------------------------------------------------------------
# rendered_states — the read-only view `ow templates` shows, never writing.
# ---------------------------------------------------------------------------


class TestRenderedStates:
    def _local_bundle(self) -> Path:
        from ow.utils import paths

        local = paths.templates_dir() / "mine"
        local.mkdir(parents=True)
        (local / "up_to_date.txt").write_text("keep me\n")
        (local / "yours.txt").write_text("original\n")
        (local / "outdated.txt").write_text("original\n")
        (local / "absent.txt").write_text("original\n")
        (local / "blank.txt.j2").write_text("   \n")
        return local

    def _ws(self, tmp_path: Path) -> tuple[WorkspaceConfig, Path]:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        return WorkspaceConfig(repos={}, templates=["mine"]), ws_dir

    def _states_by_path(self, ws, config, ws_dir) -> dict[str, str]:
        return {f.path: f.state for f in rendered_states(ws, config, ws_dir)}

    def test_every_state_is_produced(self, xdg, tmp_path, config):
        local = self._local_bundle()
        ws, ws_dir = self._ws(tmp_path)
        apply_templates(ws, config, ws_dir)

        (ws_dir / "yours.txt").write_text("hand edited\n")
        (local / "outdated.txt").write_text("moved\n")
        (ws_dir / "absent.txt").unlink()

        states = self._states_by_path(ws, config, ws_dir)

        assert states["up_to_date.txt"] == UP_TO_DATE
        assert states["yours.txt"] == YOURS
        assert states["outdated.txt"] == OUTDATED
        assert states["absent.txt"] == ABSENT
        assert states["blank.txt"] == NOT_RENDERED

    def test_writes_nothing(self, xdg, tmp_path, config):
        self._local_bundle()
        ws, ws_dir = self._ws(tmp_path)
        apply_templates(ws, config, ws_dir)

        file_before = (ws_dir / "up_to_date.txt").read_bytes()
        file_mtime_before = (ws_dir / "up_to_date.txt").stat().st_mtime_ns
        lock_before = (ws_dir / RENDERED_LOCK).read_bytes()
        lock_mtime_before = (ws_dir / RENDERED_LOCK).stat().st_mtime_ns

        rendered_states(ws, config, ws_dir)

        assert (ws_dir / "up_to_date.txt").read_bytes() == file_before
        assert (ws_dir / "up_to_date.txt").stat().st_mtime_ns == file_mtime_before
        assert (ws_dir / RENDERED_LOCK).read_bytes() == lock_before
        assert (ws_dir / RENDERED_LOCK).stat().st_mtime_ns == lock_mtime_before


# ---------------------------------------------------------------------------
# effective_bundles / selectable_templates / legacy_mise_toml
# ---------------------------------------------------------------------------


def test_effective_bundles_orders_common_then_odoo_then_declared(tmp_path):
    ws_dir = tmp_path / "ws"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config(["community"], templates=["zed", "vscode"])

    assert effective_bundles(ws, ws_dir) == ["common", "odoo", "zed", "vscode"]


def test_effective_bundles_has_no_odoo_bundle_without_a_core_repo(tmp_path):
    ws_dir = tmp_path / "ws"
    setup_flat_repo(ws_dir, "plain")
    ws = make_ws_config(["plain"], templates=["zed"])

    assert effective_bundles(ws, ws_dir) == ["common", "zed"]


def test_effective_bundles_does_not_duplicate_a_still_declared_common(tmp_path):
    ws_dir = tmp_path / "ws"
    ws = make_ws_config([], templates=["common", "zed"])

    assert effective_bundles(ws, ws_dir) == ["common", "zed"]


def test_selectable_templates_excludes_common_and_odoo_but_offers_zed(xdg):
    names = selectable_templates()

    assert "common" not in names
    assert "odoo" not in names
    assert "zed" in names


def test_legacy_mise_toml_recognises_ows_own_marker(tmp_path):
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    (ws_dir / "mise.toml").write_text('OW_WORKSPACE = "/some/path"\n')

    assert legacy_mise_toml(ws_dir) == ws_dir / "mise.toml"


def test_legacy_mise_toml_ignores_a_users_own_file(tmp_path):
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    (ws_dir / "mise.toml").write_text('[tools]\npython = "3.12"\n')

    assert legacy_mise_toml(ws_dir) is None


def test_legacy_mise_toml_absent_when_there_is_no_file(tmp_path):
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()

    assert legacy_mise_toml(ws_dir) is None


# ---------------------------------------------------------------------------
# .local — a workspace-owned addon precedes every repo in the context (#45's
# regression: without this, an addon under development had to be named by
# hand in a manually edited odoorc).
# ---------------------------------------------------------------------------


def test_dot_local_addon_precedes_repo_addons_in_context(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    (ws_dir / ".local" / "odoo_addons" / "dev_module").mkdir(parents=True)
    (ws_dir / ".local" / "odoo_addons" / "dev_module" / "__manifest__.py").write_text("{}\n")

    ws = make_ws_config(["community"])
    ctx = build_template_context(ws, config, ws_dir)

    local_path = str(ws_dir / ".local" / "odoo_addons")
    community_path = str(ws_dir / "community" / "addons")
    assert ctx["addons_paths"].index(local_path) < ctx["addons_paths"].index(community_path)

    local_item = ".local/odoo_addons"
    assert local_item in ctx["odools_path_items"]
    assert ctx["odools_path_items"].index(local_item) < ctx["odools_path_items"].index("community/addons")


def test_dot_local_addon_scan_ignores_venv_and_dot_odoo(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    (ws_dir / ".venv" / "x" / "y").mkdir(parents=True)
    (ws_dir / ".venv" / "x" / "y" / "__manifest__.py").write_text("{}\n")
    (ws_dir / ".odoo" / "a" / "b").mkdir(parents=True)
    (ws_dir / ".odoo" / "a" / "b" / "__manifest__.py").write_text("{}\n")

    ws = make_ws_config(["community"])
    ctx = build_template_context(ws, config, ws_dir)

    assert not any(".venv" in p for p in ctx["addons_paths"])
    assert not any(".odoo" in p for p in ctx["addons_paths"])
