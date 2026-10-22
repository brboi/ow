"""`ow config` scenarios: listing, get, set and unset through the real CLI.

The workspace is built here with plain git, `write_workspace` and the index —
never with `ow init` — so these scenarios stand alone.
"""

from __future__ import annotations

import fcntl
import threading
import tomllib
from pathlib import Path

import pytest
import tomli_w

from ow.core.config import _atomic_write_toml, update_global
from ow.core.paths import global_config, repos_dir
from ow.core.plugins import Setting
from tests.conftest import workspace

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


def test_config_lists_keys_inside_and_outside_a_workspace(lab):
    root = workspace(lab, branches={BASE: {"README.md": "hi\n"}})

    inside = lab.ow("config", cwd=root)
    assert inside.exit_code == 0
    assert inside.stdout.splitlines() == LISTING

    outside = lab.ow("config", cwd=lab.home)
    assert outside.exit_code == 0
    assert outside.stdout.splitlines() == LISTING_OUTSIDE


def test_config_get_value_and_unknown_key(lab):
    root = workspace(lab, branches={BASE: {"README.md": "hi\n"}})

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
    root = workspace(lab, branches={BASE: lab.odoo_files(with_demo=False)})
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
    root = workspace(lab, branches={BASE: lab.odoo_files()})

    result = lab.ow("config", "set", "--global", "mise.python", "3.12", cwd=root)
    assert result.exit_code == 0
    assert tomllib.loads(global_config().read_text()) == {"mise": {"python": "3.12"}}

    # The resolved workspace is refreshed even for a global write.
    fragment = root / "mise" / "conf.d" / "00-ow.toml"
    assert tomllib.loads(fragment.read_text())["tools"]["python"] == "3.12"

    listing = lab.ow("config", cwd=root)
    assert listing.stdout.splitlines()[1] == (
        "mise.python = \"3.12\" (global) # Python version for mise (e.g. '3.11', '3.12')"
    )

    # `--global` shows the global layer alone: built-in defaults still fill the
    # keys it does not set, exactly as they do outside a workspace.
    expected = list(LISTING_OUTSIDE)
    expected[1] = "mise.python = \"3.12\" (global) # Python version for mise (e.g. '3.11', '3.12')"
    global_listing = lab.ow("config", "--global", cwd=root)
    assert global_listing.stdout.splitlines() == expected


def test_config_unset_global_key(lab):
    root = workspace(lab, branches={BASE: lab.odoo_files()})
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
    root = workspace(lab, branches={BASE: {"README.md": "hi\n"}})
    config_path = root / ".ow" / "config.toml"
    config_path.write_text(config_path.read_text() + "[theme]\ndark = true\n")

    result = lab.ow("config", "unset", "theme.dark", cwd=root)
    assert result.exit_code == 0
    assert "warning: dropped theme.dark = true" in result.stderr
    assert tomllib.loads(config_path.read_text()) == {"repos": {REPO: f"origin/{BASE}"}}


def test_config_workspace_overrides_global(lab):
    root = workspace(lab, branches={BASE: {"README.md": "hi\n"}})
    assert lab.ow("config", "set", "--global", "odoo.http_port", "8070", cwd=root).exit_code == 0
    assert lab.ow("config", "set", "odoo.http_port", "8071", cwd=root).exit_code == 0

    # The workspace layer wins over the global one, and the listing names it.
    assert lab.ow("config", "get", "odoo.http_port", cwd=root).stdout == "8071\n"
    listing = lab.ow("config", cwd=root)
    assert "odoo.http_port = 8071 (workspace) # HTTP port for Odoo" in listing.stdout.splitlines()

    # `--global` reads the global layer alone: the workspace value is invisible.
    assert lab.ow("config", "get", "--global", "odoo.http_port", cwd=root).stdout == "8070\n"
    # A key the global file does not set prints nothing there, default or not.
    assert lab.ow("config", "get", "--global", "odoo.db_port", cwd=root).stdout == ""


def test_config_get_prints_a_string_raw(lab):
    root = workspace(lab, branches={BASE: {"README.md": "hi\n"}})
    result = lab.ow("config", "get", "odoo.db_host", cwd=root)
    assert result.exit_code == 0
    assert result.stdout == "localhost\n"


def test_config_set_parses_bools_and_empty_lists(lab):
    root = workspace(lab, branches={BASE: {"README.md": "hi\n"}})
    config_path = root / ".ow" / "config.toml"

    assert lab.ow("config", "set", "vscode.enabled", "false", cwd=root).exit_code == 0
    assert lab.ow("config", "set", "odoo.debug_args", cwd=root).exit_code == 0

    written = tomllib.loads(config_path.read_text())
    assert written["vscode"]["enabled"] is False
    assert written["odoo"]["debug_args"] == []

    # A bool and a list keep a parseable spelling.
    assert lab.ow("config", "get", "vscode.enabled", cwd=root).stdout == "false\n"
    assert lab.ow("config", "get", "odoo.debug_args", cwd=root).stdout == "[]\n"

    bad = lab.ow("config", "set", "vscode.enabled", "1", cwd=root)
    assert bad.exit_code == 2
    assert "error: Expected 'true' or 'false' for vscode.enabled, got '1'" in bad.stderr


def test_config_set_drops_a_wrong_type_key(lab):
    root = workspace(lab, branches={BASE: {"README.md": "hi\n"}})
    config_path = root / ".ow" / "config.toml"
    config_path.write_text(config_path.read_text() + '[odoo]\nhttp_port = "not-an-int"\n')

    result = lab.ow("config", "set", "mise.python", "3.12", cwd=root)
    assert result.exit_code == 0
    assert 'warning: dropped odoo.http_port = "not-an-int"' in result.stderr
    assert "http_port" not in tomllib.loads(config_path.read_text()).get("odoo", {})


def test_config_set_drops_a_rejected_repo_base(lab):
    root = workspace(lab, branches={BASE: {"README.md": "hi\n"}})
    config_path = root / ".ow" / "config.toml"
    config_path.write_text(config_path.read_text().replace(f"origin/{BASE}", "not a base"))

    result = lab.ow("config", "set", "mise.python", "3.12", cwd=root)
    assert result.exit_code == 0
    assert f'warning: dropped repos.{REPO} = "not a base"' in result.stderr
    assert "repos" not in tomllib.loads(config_path.read_text())


def test_config_unset_repos_is_refused(lab):
    root = workspace(lab, branches={BASE: {"README.md": "hi\n"}})
    config_path = root / ".ow" / "config.toml"

    result = lab.ow("config", "unset", f"repos.{REPO}", cwd=root)
    assert result.exit_code == 2
    assert (
        "error: `[repos]` is not a setting; remove a repo with `ow repo rm <name>`"
        in result.stderr
    )
    assert tomllib.loads(config_path.read_text()) == {"repos": {REPO: f"origin/{BASE}"}}


def test_config_file_that_is_not_utf8_is_a_usage_error(lab):
    root = workspace(lab, branches={BASE: {"README.md": "hi\n"}})
    config_path = root / ".ow" / "config.toml"
    config_path.write_bytes(b'[mise]\npython = "\xff"\n')

    result = lab.ow("config", cwd=root)
    assert result.exit_code == 2
    assert f"error: cannot read {config_path}" in result.stderr


def test_config_write_waits_for_the_lock(tmp_path):
    path = tmp_path / "config.toml"
    declared = {"mise.python": Setting(key="mise.python", type=str, default=None, help="")}
    written = threading.Event()

    def writer() -> None:
        update_global(path, lambda current: current | {"mise.python": "3.12"}, declared=declared)
        written.set()

    with open(tmp_path / ".config.toml.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        thread = threading.Thread(target=writer)
        thread.start()
        # The writer needs the same sidecar lock, so it cannot have written yet.
        assert not written.wait(0.5)
        assert not path.exists()
    thread.join(5)
    assert written.is_set()
    assert tomllib.loads(path.read_text()) == {"mise": {"python": "3.12"}}


def test_atomic_write_removes_its_temp_file_on_interrupt(tmp_path, monkeypatch):
    target = tmp_path / "cfg"
    target.mkdir()

    def interrupted(data, handle):
        raise KeyboardInterrupt

    monkeypatch.setattr(tomli_w, "dump", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _atomic_write_toml(target / "config.toml", {"mise": {"python": "3.12"}})
    assert list(target.iterdir()) == []


def test_paths_ignore_a_relative_xdg_value(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    monkeypatch.setenv("XDG_DATA_HOME", "~/unexpanded")
    assert global_config() == Path.home() / ".config" / "ow" / "config.toml"
    assert repos_dir() == Path.home() / ".local" / "share" / "ow" / "repos"

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert global_config() == tmp_path / "cfg" / "ow" / "config.toml"
