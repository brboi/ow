import json
import re
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

from jinja2 import Environment, FileSystemLoader

from ow.utils.templates import apply_templates, build_template_context, find_addon_paths
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
