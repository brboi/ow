"""`ow config` scenarios: listing, get, set and unset through the real CLI.

The workspace is built here with plain git, `write_workspace` and the index —
never with `ow init` — so these scenarios stand alone.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from ow.core.config import write_workspace
from ow.core.index import add as index_add
from ow.core.paths import global_config, repos_dir

REPO = "community"
BASE = "18.0"

# The bare listing of a workspace whose only repo is not an Odoo core: every
# value is the built-in default, and `odoo.db_name` takes the workspace's name.
LISTING = [
    "mise.enabled = true (default) # Enable or disable the mise plugin",
    "mise.python = (unset) (default) # Python version for mise (e.g. '3.11', '3.12')",
    "odoo.enabled = true (default) # Enable or disable the odoo plugin",
    "odoo.http_port = 8069 (default) # HTTP port for Odoo",
    'odoo.db_name = "voip" (default) # Database name (defaults to workspace name)',
    'odoo.db_host = "localhost" (default) # Database host',
    "odoo.db_port = 5432 (default) # Database port",
    'odoo.db_user = "odoo" (default) # Database user',
    'odoo.db_password = "odoo" (default) # Database password',
    'odoo.admin_passwd = "Password" (default) # Odoo admin password',
    'odoo.smtp_server = "localhost" (default) # SMTP server',
    "odoo.smtp_port = 25 (default) # SMTP port",
    'odoo.debug_args = ["--dev=all"] (default) # Debug arguments for Odoo',
    'odoo.debug_test_args = ["--test-tags=voip"] (default) # Debug test arguments',
    "vscode.enabled = true (default) # Enable or disable the vscode plugin",
    "zed.enabled = true (default) # Enable or disable the zed plugin",
    "local.enabled = true (default) # Enable or disable the local plugin",
]

# The same listing outside a workspace: no workspace, so no callable default
# has anything to resolve against and every one of them is unset.
LISTING_OUTSIDE = [
    "mise.enabled = true (default) # Enable or disable the mise plugin",
    "mise.python = (unset) (default) # Python version for mise (e.g. '3.11', '3.12')",
    "odoo.enabled = true (default) # Enable or disable the odoo plugin",
    "odoo.http_port = 8069 (default) # HTTP port for Odoo",
    "odoo.db_name = (unset) (default) # Database name (defaults to workspace name)",
    'odoo.db_host = "localhost" (default) # Database host',
    "odoo.db_port = 5432 (default) # Database port",
    'odoo.db_user = "odoo" (default) # Database user',
    'odoo.db_password = "odoo" (default) # Database password',
    'odoo.admin_passwd = "Password" (default) # Odoo admin password',
    'odoo.smtp_server = "localhost" (default) # SMTP server',
    "odoo.smtp_port = 25 (default) # SMTP port",
    "odoo.debug_args = (unset) (default) # Debug arguments for Odoo",
    "odoo.debug_test_args = (unset) (default) # Debug test arguments",
    "vscode.enabled = true (default) # Enable or disable the vscode plugin",
    "zed.enabled = true (default) # Enable or disable the zed plugin",
    "local.enabled = true (default) # Enable or disable the local plugin",
]


def workspace(lab, files: dict[str, str], *, name: str = "voip") -> Path:
    """A real workspace: a bare repo, a worktree, a config file and an index entry."""
    origin = lab.remote(REPO, {BASE: files}, head=BASE)
    bare = repos_dir() / f"{REPO}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    lab.git(bare.parent, "init", "--bare", str(bare))
    lab.git(bare, "remote", "add", "origin", str(origin))
    lab.git(bare, "fetch", "origin")

    root = lab.ws(name)
    root.mkdir(parents=True, exist_ok=True)
    lab.git(bare, "worktree", "add", "--detach", str(root / REPO), f"origin/{BASE}")

    write_workspace(root / ".ow" / "config.toml", {REPO: f"origin/{BASE}"}, {}, declared={})
    index_add(root)
    return root


def test_config_lists_keys_inside_and_outside_a_workspace(lab):
    root = workspace(lab, {"README.md": "hi\n"})

    inside = lab.ow("config", cwd=root)
    assert inside.exit_code == 0
    assert inside.stdout.splitlines() == LISTING

    outside = lab.ow("config", cwd=lab.home)
    assert outside.exit_code == 0
    assert outside.stdout.splitlines() == LISTING_OUTSIDE


def test_config_get_value_and_unknown_key(lab):
    root = workspace(lab, {"README.md": "hi\n"})

    got = lab.ow("config", "get", "odoo.http_port", cwd=root)
    assert got.exit_code == 0
    assert got.stdout == "8069\n"
    assert got.stderr == ""

    # An unset value prints nothing, and that is not an error.
    unset = lab.ow("config", "get", "mise.python", cwd=root)
    assert unset.exit_code == 0
    assert unset.stdout == ""

    unknown = lab.ow("config", "get", "odoo.http_prt", cwd=root)
    assert unknown.exit_code == 2
    assert unknown.stdout == ""
    assert (
        "error: unknown setting 'odoo.http_prt'; did you mean 'odoo.http_port'?"
        in unknown.stderr
    )


def test_config_set_workspace_key_refreshes(lab):
    root = workspace(lab, lab.odoo_files(with_demo=False))
    config_path = root / ".ow" / "config.toml"

    result = lab.ow("config", "set", "odoo.debug_args", "--", "--dev=all", cwd=root)
    assert result.exit_code == 0
    assert "wrote odoorc" in result.stdout.splitlines()

    written = tomllib.loads(config_path.read_text())
    assert written["odoo"]["debug_args"] == ["--dev=all"]
    assert written["repos"] == {REPO: f"origin/{BASE}"}

    # The automatic refresh used the new value: the editor launches dropped the
    # `--without-demo=all` that the pre-19 default had put there.
    launch = (root / ".vscode" / "launch.json").read_text()
    assert '"--dev=all"' in launch
    assert "--without-demo=all" not in launch
    assert (root / "odoorc").exists()

    # The listing now names the workspace layer as the value's origin.
    listing = lab.ow("config", cwd=root)
    assert (
        'odoo.debug_args = ["--dev=all"] (workspace) # Debug arguments for Odoo'
        in listing.stdout.splitlines()
    )

    # A type error is a usage error and writes nothing.
    before = config_path.read_bytes()
    bad = lab.ow("config", "set", "odoo.http_port", "notanint", cwd=root)
    assert bad.exit_code == 2
    assert "error: Expected an integer for odoo.http_port, got 'notanint'" in bad.stderr
    assert config_path.read_bytes() == before

    # An unknown key is rejected with the closest declared one.
    typo = lab.ow("config", "set", "odoo.http_prt", "8070", cwd=root)
    assert typo.exit_code == 2
    assert "error: unknown setting 'odoo.http_prt'; did you mean 'odoo.http_port'?" in typo.stderr


def test_config_set_needs_a_workspace_without_global(lab):
    result = lab.ow("config", "set", "odoo.http_port", "8070", cwd=lab.home)
    assert result.exit_code == 2
    assert "error: not inside an ow workspace; pass --global or use -w" in result.stderr

    unset = lab.ow("config", "unset", "odoo.http_port", cwd=lab.home)
    assert unset.exit_code == 2
    assert "error: not inside an ow workspace; pass --global or use -w" in unset.stderr


def test_config_set_global_refreshes_the_current_workspace(lab):
    root = workspace(lab, lab.odoo_files())

    result = lab.ow("config", "set", "--global", "mise.python", "3.12", cwd=root)
    assert result.exit_code == 0
    assert tomllib.loads(global_config().read_text()) == {"mise": {"python": "3.12"}}

    # §17.5: the resolved workspace is refreshed even for a global write.
    fragment = root / "mise" / "conf.d" / "00-ow.toml"
    assert tomllib.loads(fragment.read_text())["tools"]["python"] == "3.12"

    listing = lab.ow("config", cwd=root)
    assert listing.stdout.splitlines()[1] == (
        "mise.python = \"3.12\" (global) # Python version for mise (e.g. '3.11', '3.12')"
    )

    # `--global` shows the global layer alone: every key it does not set is
    # unset there, whatever the built-in default or the workspace says.
    global_listing = lab.ow("config", "--global", cwd=root)
    assert global_listing.stdout.splitlines() == [
        "mise.enabled = (unset) (global) # Enable or disable the mise plugin",
        "mise.python = \"3.12\" (global) # Python version for mise (e.g. '3.11', '3.12')",
        "odoo.enabled = (unset) (global) # Enable or disable the odoo plugin",
        "odoo.http_port = (unset) (global) # HTTP port for Odoo",
        "odoo.db_name = (unset) (global) # Database name (defaults to workspace name)",
        "odoo.db_host = (unset) (global) # Database host",
        "odoo.db_port = (unset) (global) # Database port",
        "odoo.db_user = (unset) (global) # Database user",
        "odoo.db_password = (unset) (global) # Database password",
        "odoo.admin_passwd = (unset) (global) # Odoo admin password",
        "odoo.smtp_server = (unset) (global) # SMTP server",
        "odoo.smtp_port = (unset) (global) # SMTP port",
        "odoo.debug_args = (unset) (global) # Debug arguments for Odoo",
        "odoo.debug_test_args = (unset) (global) # Debug test arguments",
        "vscode.enabled = (unset) (global) # Enable or disable the vscode plugin",
        "zed.enabled = (unset) (global) # Enable or disable the zed plugin",
        "local.enabled = (unset) (global) # Enable or disable the local plugin",
    ]


def test_config_unset_global_key(lab):
    root = workspace(lab, lab.odoo_files())
    assert lab.ow("config", "set", "--global", "mise.python", "3.12", cwd=root).exit_code == 0

    result = lab.ow("config", "unset", "--global", "mise.python", cwd=root)
    assert result.exit_code == 0
    assert "python" not in global_config().read_text()

    # Nothing is left to remove, so the second run is a no-op.
    again = lab.ow("config", "unset", "--global", "mise.python", cwd=root)
    assert again.exit_code == 0
    assert again.stdout == ""
    assert again.stderr == ""


def test_config_unset_an_unknown_key(lab):
    root = workspace(lab, {"README.md": "hi\n"})
    config_path = root / ".ow" / "config.toml"
    config_path.write_text(config_path.read_text() + "[theme]\ndark = true\n")

    result = lab.ow("config", "unset", "theme.dark", cwd=root)
    assert result.exit_code == 0
    assert "warning: dropped theme.dark = true" in result.stderr
    assert tomllib.loads(config_path.read_text()) == {"repos": {REPO: f"origin/{BASE}"}}
