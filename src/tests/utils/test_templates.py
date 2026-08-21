import pytest
import json
import re
import subprocess
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

from jinja2 import Environment, FileSystemLoader

from ow.utils.templates import apply_templates, build_template_context, ensure_workspace_materialized, find_addon_paths
from ow.utils.config import BranchSpec, Config, WorkspaceConfig, write_workspace_config

TEMPLATE_DIR = Path(__file__).parent.parent.parent / "ow" / "_static" / "templates" / "common"
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
# Template rendering - mise.toml
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
    result = render_template("mise.toml.j2", ctx)

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
    content = (ws_dir / "mise.toml").read_text()
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
    result = render_template("pyrightconfig.json.j2", ctx)
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
# A workspace with no Odoo core repo
#
# `main_repo_alias` is None whenever nothing in the workspace ships odoo-bin —
# an enterprise-only or addons-only workspace, or a core worktree that is not
# materialised yet. Every packaged template must degrade to something valid in
# its own format instead of naming a directory called "None".
# ---------------------------------------------------------------------------

PACKAGED_BUNDLES = ["bwrap", "common", "vscode", "zed"]


def strip_jsonc(text: str) -> str:
    """Zed writes JSONC: // comments and trailing commas."""
    lines = [l for l in text.splitlines() if not l.strip().startswith("//")]
    return re.sub(r",(\s*[}\]])", r"\1", "\n".join(lines))


def render_every_bundle_without_core(tmp_path: Path, config: Config) -> Path:
    """Apply every packaged bundle to a workspace holding no Odoo core repo."""
    ws_dir = tmp_path / "workspaces" / "plainws"
    ws_dir.mkdir(parents=True)
    setup_flat_repo(ws_dir, "plain")
    ws = make_ws_config(["plain"], templates=PACKAGED_BUNDLES)
    assert build_template_context(ws, config, ws_dir)["main_repo_alias"] is None
    apply_templates(ws, config, ws_dir)
    return ws_dir


def rendered_files(ws_dir: Path) -> list[Path]:
    """Everything the templates wrote, excluding the repo worktrees."""
    return [
        p
        for p in sorted(ws_dir.rglob("*"))
        if p.is_file() and p.relative_to(ws_dir).parts[0] != "plain"
    ]


def test_no_packaged_template_renders_a_literal_none(tmp_path, config):
    ws_dir = render_every_bundle_without_core(tmp_path, config)
    written = rendered_files(ws_dir)
    assert written, "the bundles must write something"
    for path in written:
        assert not re.search(r"\bNone\b", path.read_text()), (
            f"{path.relative_to(ws_dir)} names a directory called None"
        )


def test_mise_toml_parses_and_installs_only_what_exists(tmp_path, config):
    ws_dir = render_every_bundle_without_core(tmp_path, config)
    data = tomllib.loads((ws_dir / "mise.toml").read_text())

    assert "requirements-dev.txt" in data["hooks"]["postinstall"]
    assert "requirements.txt" not in data["hooks"]["postinstall"].replace(
        "requirements-dev.txt", ""
    )
    assert data["env"]["_"]["path"] == ["{{config_root}}"]
    # odoo-bin is never on PATH without a core repo, so an `osh` alias could
    # only ever fail.
    assert "shell_alias" not in data


def test_odools_toml_parses_and_omits_the_odoo_path(tmp_path, config):
    ws_dir = render_every_bundle_without_core(tmp_path, config)
    data = tomllib.loads((ws_dir / "odools.toml").read_text())

    entry = data["config"][0]
    assert "odoo_path" not in entry
    assert entry["addons_paths"] == ["./plain"]


def test_pyrightconfig_parses_with_no_core_extra_path(tmp_path, config):
    ws_dir = render_every_bundle_without_core(tmp_path, config)
    data = json.loads((ws_dir / "pyrightconfig.json").read_text())

    assert data["extraPaths"] == []
    assert data["venv"] == ".venv"


def test_vscode_launch_parses_with_no_configurations(tmp_path, config):
    ws_dir = render_every_bundle_without_core(tmp_path, config)
    data = json.loads((ws_dir / ".vscode" / "launch.json").read_text())

    # Both configurations run odoo-bin out of the core repo. Without one there
    # is nothing to launch, and a half-filled entry would only fail on use.
    assert data["configurations"] == []


def test_zed_debug_parses_with_no_configurations(tmp_path, config):
    ws_dir = render_every_bundle_without_core(tmp_path, config)
    data = json.loads(strip_jsonc((ws_dir / ".zed" / "debug.json").read_text()))

    assert data == []


def test_odoorc_still_lists_the_non_core_addons(tmp_path, config):
    """Degrading is not blanking: what does not need core is still written."""
    ws_dir = render_every_bundle_without_core(tmp_path, config)
    rendered = (ws_dir / "odoorc").read_text()

    assert f"addons_path = {ws_dir / 'plain'}" in rendered
    assert "db_name = plainws" in rendered


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
