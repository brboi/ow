import json
from pathlib import Path
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader

from ow.config import BranchSpec, Config, WorkspaceConfig
from ow.workspace import (
    _check_source_drift,
    _compute_hash,
    _load_cache,
    _record_hash,
    _save_cache,
    build_template_context,
    cmd_create,
    find_addon_paths,
)

TEMPLATE_DIR = Path(__file__).parent.parent.parent / "workspaces" / ".template.init"


def make_config(
    workspaces=None,
    root_dir=None,
    vars=None,
    remotes=None,
) -> Config:
    return Config(
        vars=vars if vars is not None else {"http_port": 8069, "db_host": "localhost", "db_port": 5432},
        remotes=remotes or {},
        workspaces=workspaces or [],
        root_dir=root_dir or Path("/root"),
    )


# ---------------------------------------------------------------------------
# Filesystem fixture helpers
# ---------------------------------------------------------------------------

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
    """Repo where the root is directly an addons_path."""
    repo = ws_dir / alias
    (repo / "account").mkdir(parents=True)
    (repo / "account" / "__manifest__.py").touch()
    (repo / "sale").mkdir(parents=True)
    (repo / "sale" / "__manifest__.py").touch()
    return repo


def setup_categorized_repo(ws_dir: Path, alias: str) -> Path:
    """Repo whose immediate subdirs are each addons_paths (one level of nesting)."""
    repo = ws_dir / alias
    (repo / "telephony" / "phone_validation").mkdir(parents=True)
    (repo / "telephony" / "phone_validation" / "__manifest__.py").touch()
    (repo / "messaging" / "sms_gateway").mkdir(parents=True)
    (repo / "messaging" / "sms_gateway" / "__manifest__.py").touch()
    return repo


def make_ws_config(name: str, aliases: list[str]) -> WorkspaceConfig:
    return WorkspaceConfig(
        name=name,
        repos={alias: BranchSpec("origin/master") for alias in aliases},
    )


def render_template(name: str, context: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
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
    assert result == sorted([
        repo / "categories" / "crm",
        repo / "external" / "vendor" / "payments",
        repo / "helpers",
    ])


# ---------------------------------------------------------------------------
# build_template_context
# ---------------------------------------------------------------------------

def test_build_template_context_community_only(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config("test", ["community"])
    config = make_config(root_dir=tmp_path)
    ctx = build_template_context(ws, config, ws_dir)

    assert ctx["ws_name"] == "test"
    assert ctx["main_repo_alias"] == "community"
    assert ctx["repos"] == ["community"]
    assert str(ws_dir / "community" / "addons") in ctx["addons_paths"]
    assert str(ws_dir / "community" / "odoo" / "addons") in ctx["addons_paths"]
    assert "community/addons" in ctx["odools_path_items"]
    assert "community/odoo/addons" in ctx["odools_path_items"]


def test_build_template_context_addons_order(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    setup_flat_repo(ws_dir, "enterprise")
    ws = make_ws_config("test", ["community", "enterprise"])
    config = make_config(root_dir=tmp_path)
    ctx = build_template_context(ws, config, ws_dir)

    # enterprise before community in addons_paths
    ent_idx = next(i for i, p in enumerate(ctx["addons_paths"]) if "enterprise" in p)
    comm_idx = next(i for i, p in enumerate(ctx["addons_paths"]) if "community/addons" in p)
    assert ent_idx < comm_idx

    # enterprise before community in odools_path_items
    ent_idx = next(i for i, p in enumerate(ctx["odools_path_items"]) if "enterprise" in p)
    comm_idx = next(i for i, p in enumerate(ctx["odools_path_items"]) if "community/addons" in p)
    assert ent_idx < comm_idx


def test_build_template_context_vars_merge(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = WorkspaceConfig(
        name="test",
        repos={"community": BranchSpec("origin/master")},
        vars={"http_port": 8070},
    )
    config = make_config(root_dir=tmp_path)
    ctx = build_template_context(ws, config, ws_dir)

    assert ctx["vars"]["http_port"] == 8070
    assert "db_host" in ctx["vars"]


def test_build_template_context_full_workspace(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    setup_flat_repo(ws_dir, "enterprise")
    setup_flat_repo(ws_dir, "brboi-addons")
    ws = make_ws_config("test", ["community", "enterprise", "brboi-addons"])
    config = make_config(root_dir=tmp_path)
    ctx = build_template_context(ws, config, ws_dir)

    assert ctx["repos"] == ["community", "enterprise", "brboi-addons"]
    assert len([p for p in ctx["addons_paths"] if "community" in p]) == 2
    assert len([p for p in ctx["addons_paths"] if "enterprise" in p or "brboi-addons" in p]) == 2


def test_build_template_context_no_main_repo(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_flat_repo(ws_dir, "enterprise")
    ws = make_ws_config("test", ["enterprise"])
    config = make_config(root_dir=tmp_path)
    ctx = build_template_context(ws, config, ws_dir)

    assert ctx["main_repo_alias"] is None


# ---------------------------------------------------------------------------
# Template rendering — odoorc
# ---------------------------------------------------------------------------

def test_render_odoorc_community_only(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config("test", ["community"])
    config = make_config(root_dir=tmp_path)
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template("odoorc.j2", ctx)

    assert "[options]" in result
    assert "http_port = 8069" in result
    assert "db_host = localhost" in result
    assert "community/addons" in result
    assert "community/odoo/addons" in result
    assert "db_name = test" in result
    assert "dbfilter = ^test$" in result


def test_render_odoorc_enterprise_before_community(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    setup_flat_repo(ws_dir, "enterprise")
    ws = make_ws_config("test", ["community", "enterprise"])
    config = make_config(root_dir=tmp_path)
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template("odoorc.j2", ctx)

    lines = result.split("\n")
    addons_line = next(l for l in lines if l.startswith("addons_path"))
    paths = addons_line.split("=", 1)[1].strip().split(",")
    assert "enterprise" in paths[0]
    assert "community/addons" in paths[1]
    assert "community/odoo/addons" in paths[2]


def test_render_odoorc_workspace_overrides_global(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = WorkspaceConfig(
        name="test",
        repos={"community": BranchSpec("origin/master")},
        vars={"http_port": 8070},
    )
    config = make_config(root_dir=tmp_path)
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template("odoorc.j2", ctx)

    assert "http_port = 8070" in result
    assert "http_port = 8069" not in result


def test_render_odoorc_no_quotes_on_string_values(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config("test", ["community"])
    config = make_config(root_dir=tmp_path)
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template("odoorc.j2", ctx)

    assert 'db_host = "localhost"' not in result
    assert "db_host = localhost" in result


# ---------------------------------------------------------------------------
# Template rendering — odools.toml
# ---------------------------------------------------------------------------

def test_render_odools_community_only(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config("test", ["community"])
    config = make_config(root_dir=tmp_path)
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template("odools.toml.j2", ctx)

    assert "[[config]]" in result
    assert "[Odoo Workspace] test" in result
    assert 'python_path = ".venv/bin/python"' in result
    assert 'odoo_path = "./community"' in result
    assert "./community/addons" in result
    assert "./community/odoo/addons" in result
    assert "./enterprise" not in result


def test_render_odools_enterprise_before_community(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    setup_flat_repo(ws_dir, "enterprise")
    ws = make_ws_config("test", ["community", "enterprise"])
    config = make_config(root_dir=tmp_path)
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template("odools.toml.j2", ctx)

    assert "./enterprise" in result
    ent_idx = result.index("./enterprise")
    com_idx = result.index("./community/addons")
    assert ent_idx < com_idx


def test_render_odools_categorized_repo(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    setup_categorized_repo(ws_dir, "partner-addons")
    ws = make_ws_config("test", ["community", "partner-addons"])
    config = make_config(root_dir=tmp_path)
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

def test_render_mise_toml(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config("test", ["community"])
    config = make_config(root_dir=tmp_path)
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

def test_render_pyrightconfig(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config("test", ["community"])
    config = make_config(root_dir=tmp_path)
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

def test_render_vscode_settings(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config("test", ["community"])
    config = make_config(root_dir=tmp_path)
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template(".vscode/settings.json.j2", ctx)

    assert "[Odoo Workspace] test" in result


def test_render_vscode_launch(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config("test", ["community"])
    config = make_config(root_dir=tmp_path)
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template(".vscode/launch.json.j2", ctx)

    assert "debugpy" in result
    assert "${workspaceFolder}/community" in result
    assert "odoo-bin" in result
    assert "odoorc" in result


# ---------------------------------------------------------------------------
# Template rendering — .zed
# ---------------------------------------------------------------------------

def test_render_zed_settings(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    setup_flat_repo(ws_dir, "enterprise")
    ws = make_ws_config("test", ["community", "enterprise"])
    config = make_config(root_dir=tmp_path)
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template(".zed/settings.json.j2", ctx)

    assert "community/**" in result
    assert "enterprise/**" in result
    assert "[Odoo Workspace] test" in result
    assert '"mise.toml"' in result
    assert '"odools.toml"' in result
    assert '"pyrightconfig.json"' in result
    assert '"**/.venv"' in result


def test_render_zed_settings_full_workspace(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    setup_flat_repo(ws_dir, "enterprise")
    setup_flat_repo(ws_dir, "brboi-addons")
    ws = make_ws_config("test", ["community", "enterprise", "brboi-addons"])
    config = make_config(root_dir=tmp_path)
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template(".zed/settings.json.j2", ctx)

    assert "community/**" in result
    assert "enterprise/**" in result
    assert "brboi-addons/**" in result


def test_render_zed_debug(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    setup_odoo_main_repo(ws_dir, "community")
    ws = make_ws_config("test", ["community"])
    config = make_config(root_dir=tmp_path)
    ctx = build_template_context(ws, config, ws_dir)
    result = render_template(".zed/debug.json.j2", ctx)

    assert "Debugpy" in result
    assert "${ZED_WORKTREE_ROOT}/community" in result
    assert "odoo-bin" in result
    assert "${ZED_WORKTREE_ROOT}/.venv/bin/python" in result
    assert "odoorc" in result


# ---------------------------------------------------------------------------
# Hash / cache / drift
# ---------------------------------------------------------------------------

def test_compute_hash_file(tmp_path):
    f = tmp_path / "test.txt"
    f.write_bytes(b"hello")
    h1 = _compute_hash(f)
    assert len(h1) == 64  # sha256 hex
    f.write_bytes(b"world")
    h2 = _compute_hash(f)
    assert h1 != h2


def test_compute_hash_dir_stable(tmp_path):
    d = tmp_path / "dir"
    d.mkdir()
    (d / "a.txt").write_bytes(b"aaa")
    (d / "b.txt").write_bytes(b"bbb")
    h1 = _compute_hash(d)
    h2 = _compute_hash(d)
    assert h1 == h2


def test_compute_hash_dir_changes_on_content(tmp_path):
    d = tmp_path / "dir"
    d.mkdir()
    (d / "a.txt").write_bytes(b"aaa")
    h1 = _compute_hash(d)
    (d / "a.txt").write_bytes(b"bbb")
    h2 = _compute_hash(d)
    assert h1 != h2


def test_load_save_cache(tmp_path):
    assert _load_cache(tmp_path) == {}
    _save_cache(tmp_path, {"key": {"hash": "abc", "ignore": False}})
    cache = _load_cache(tmp_path)
    assert cache["key"]["hash"] == "abc"


def test_record_hash(tmp_path):
    f = tmp_path / "example.txt"
    f.write_bytes(b"content")
    _record_hash(tmp_path, "example.txt", f)
    cache = _load_cache(tmp_path)
    assert cache["example.txt"]["hash"] == _compute_hash(f)
    assert cache["example.txt"]["ignore"] is False


def test_check_source_drift_no_cache(tmp_path):
    f = tmp_path / "example.txt"
    f.write_bytes(b"content")
    # Non-TTY: should print warning but not abort
    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = False
        result = _check_source_drift(tmp_path, "example.txt", f)
    assert result is False


def test_check_source_drift_no_drift(tmp_path):
    f = tmp_path / "example.txt"
    f.write_bytes(b"content")
    _record_hash(tmp_path, "example.txt", f)
    # Same content: no drift
    result = _check_source_drift(tmp_path, "example.txt", f)
    assert result is False


def test_check_source_drift_ignore(tmp_path):
    f = tmp_path / "example.txt"
    f.write_bytes(b"v1")
    _save_cache(tmp_path, {"example.txt": {"hash": "old", "ignore": True}})
    # ignore=True: never drift
    result = _check_source_drift(tmp_path, "example.txt", f)
    assert result is False


def test_check_source_drift_abort(tmp_path):
    f = tmp_path / "example.txt"
    f.write_bytes(b"v2")
    _save_cache(tmp_path, {"example.txt": {"hash": "old_hash", "ignore": False}})
    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = True
        with patch("builtins.input", return_value="a"):
            result = _check_source_drift(tmp_path, "example.txt", f)
    assert result is True


def test_check_source_drift_skip(tmp_path):
    f = tmp_path / "example.txt"
    f.write_bytes(b"v2")
    _save_cache(tmp_path, {"example.txt": {"hash": "old_hash", "ignore": False}})
    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = True
        with patch("builtins.input", return_value="s"):
            result = _check_source_drift(tmp_path, "example.txt", f)
    assert result is False
    cache = _load_cache(tmp_path)
    assert cache["example.txt"]["hash"] == _compute_hash(f)


def test_check_source_drift_continue_no_save(tmp_path):
    f = tmp_path / "example.txt"
    f.write_bytes(b"v2")
    _save_cache(tmp_path, {"example.txt": {"hash": "old_hash"}})
    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = True
        with patch("builtins.input", return_value="c"):
            result = _check_source_drift(tmp_path, "example.txt", f)
    assert result is False
    # [c] does not update the cache — will warn again next time
    cache = _load_cache(tmp_path)
    assert cache["example.txt"]["hash"] == "old_hash"


# ---------------------------------------------------------------------------
# cmd_create — vars parsing
# ---------------------------------------------------------------------------

def test_cmd_create_vars_parsing(tmp_path):
    # Set up a minimal ow.toml and template dir so cmd_create doesn't fail on I/O
    (tmp_path / "ow.toml").write_text("[vars]\n")
    template_init = tmp_path / "workspaces" / ".template.init"
    template_init.mkdir(parents=True)
    config = make_config(root_dir=tmp_path, vars={}, workspaces=[])

    captured_ws = []
    original_cmd_apply = __import__("ow.workspace", fromlist=["cmd_apply"]).cmd_apply

    def fake_apply(cfg, name=None):
        pass

    with patch("ow.workspace.cmd_apply", fake_apply):
        cmd_create(config, "my-ws", ["community:master", "vars.http_port=8080", "vars.db_user=myuser"])

    ws = config.workspaces[0]
    assert ws.name == "my-ws"
    assert "community" in ws.repos
    assert ws.vars == {"http_port": "8080", "db_user": "myuser"}
