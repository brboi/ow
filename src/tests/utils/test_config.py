import tempfile
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from ow.utils import paths
from ow.utils.config import (
    _DEFAULT_CONFIG,
    BranchSpec,
    Config,
    LegacyConfig,
    RemoteConfig,
    WorkspaceConfig,
    _legacy_options,
    _legacy_templates,
    dumps_global_config,
    dumps_workspace_config,
    find_project_root,
    load_config,
    load_global_config,
    load_workspace_config,
    parse_branch_spec,
    pending_migration,
    report_pending_migration,
    select_aliases,
    write_global_config,
    write_workspace_config,
)
from ow.utils.options import MiseOverrides, OdooOverrides

# ---------------------------------------------------------------------------
# parse_branch_spec
# ---------------------------------------------------------------------------

def test_parse_simple():
    spec = parse_branch_spec("master")
    assert spec == BranchSpec("origin/master")
    assert spec.remote == "origin"
    assert spec.branch == "master"
    assert spec.is_detached


def test_parse_with_local_branch():
    spec = parse_branch_spec("master..master-feature")
    assert spec == BranchSpec("origin/master", "master-feature")
    assert not spec.is_detached
    assert spec.remote == "origin"
    assert spec.branch == "master"


def test_parse_with_non_origin_remote():
    spec = parse_branch_spec("dev/master-phoenix..fix")
    assert spec == BranchSpec("dev/master-phoenix", "fix")
    assert spec.remote == "dev"
    assert spec.branch == "master-phoenix"
    assert not spec.is_detached


def test_parse_full_ref():
    spec = parse_branch_spec("origin/master")
    assert spec == BranchSpec("origin/master")
    assert spec.is_detached


def test_parse_18_0():
    spec = parse_branch_spec("18.0")
    assert spec == BranchSpec("origin/18.0")
    assert spec.branch == "18.0"


@pytest.mark.parametrize("spec", ["", "master..", "..feat", "a..b..c", "master ..feat"])
def test_parse_branch_spec_rejects_degenerate(spec):
    with pytest.raises(ValueError, match="invalid branch spec"):
        parse_branch_spec(spec)


# ---------------------------------------------------------------------------
# to_spec_str round-trips
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("s", [
    "master",
    "18.0",
    "master..master-feature",
    "dev/master-phoenix..fix",
    # An origin branch whose own name has a slash keeps its prefix: written
    # bare, `dev/18.0-fix` would re-read as the `dev` remote's `18.0-fix`.
    "origin/dev/18.0-fix",
    "origin/dev/18.0-fix..fix",
])
def test_to_spec_str_round_trip(s):
    assert parse_branch_spec(s).to_spec_str() == s


def test_to_spec_str_origin_prefix_stripped():
    spec = BranchSpec("origin/master")
    assert spec.to_spec_str() == "master"


def test_to_spec_str_non_origin_kept():
    spec = BranchSpec("dev/master-phoenix", "fix")
    assert spec.to_spec_str() == "dev/master-phoenix..fix"


# ---------------------------------------------------------------------------
# The schema-2 global config
# ---------------------------------------------------------------------------

SAMPLE_TOML = """\
version = 2
editor = "nvim"
theme = "dracula"
owignore = [".zed/**", "*.lock"]

[odoo]
http_port = 8069
db_host = "localhost"

[mise]
python = "3.12"

[remotes]
community.origin.url = "git@github.com:odoo/odoo.git"
community.dev.url = "git@github.com:odoo-dev/odoo.git"
community.dev.pushurl = "git@github.com:odoo-dev/odoo.git"
community.dev.fetch = "+refs/heads/*:refs/remotes/dev/*"
"""


def _write(tmp_path: Path, text: str, name: str = "config.toml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def _write_ws(tmp_path: Path, text: str) -> Path:
    """A workspace config at its real location, `<ws>/.ow/config.toml`."""
    path = tmp_path / ".ow" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def test_load_config(tmp_path):
    config = load_config(_write(tmp_path, SAMPLE_TOML))

    assert config.version == 2
    assert config.editor == "nvim"
    assert config.theme == "dracula"
    assert config.owignore == (".zed/**", "*.lock")
    assert config.odoo == OdooOverrides(http_port=8069, db_host="localhost")
    assert config.mise == MiseOverrides(python="3.12")
    assert not hasattr(config, "vars")

    assert "community" in config.remotes
    assert config.remotes["community"]["origin"].url == "git@github.com:odoo/odoo.git"
    assert config.remotes["community"]["dev"].pushurl == "git@github.com:odoo-dev/odoo.git"
    assert config.remotes["community"]["dev"].fetch == "+refs/heads/*:refs/remotes/dev/*"


def test_load_config_options_are_sparse(tmp_path):
    config = load_config(_write(tmp_path, 'version = 2\n[remotes]\n'))

    assert config.odoo == OdooOverrides()
    assert config.mise == MiseOverrides()
    assert config.owignore == ()
    assert config.editor == "code"
    assert config.theme == "textual-dark"


def test_load_config_remotes_missing_url_raises_valueerror(tmp_path):
    toml = textwrap.dedent("""\
    version = 2
    [remotes.community]
    origin.pushurl = "git@github.com:odoo-dev/odoo.git"
    """)
    with pytest.raises(ValueError, match="community.*origin"):
        load_config(_write(tmp_path, toml))


def test_load_config_remotes_non_table_raises_valueerror(tmp_path):
    toml = textwrap.dedent("""\
    version = 2
    [remotes.community]
    origin = "not-a-table"
    """)
    with pytest.raises(ValueError, match="community.*origin"):
        load_config(_write(tmp_path, toml))


def test_load_config_remotes_scalar_alias_raises_valueerror(tmp_path):
    toml = textwrap.dedent("""\
    version = 2
    [remotes]
    community = "not-a-table"
    """)
    with pytest.raises(ValueError, match=r"remotes\.community.*table"):
        load_config(_write(tmp_path, toml))


@pytest.mark.parametrize("key,value", [
    ("editor", "42"),
    ("theme", "[1]"),
    ("owignore", '"one-pattern"'),
])
def test_load_config_rejects_wrong_scalar_types(tmp_path, key, value):
    path = _write(tmp_path, f'version = 2\n{key} = {value}\n[remotes]\n')
    with pytest.raises(ValueError, match=key):
        load_config(path)


def test_load_config_rejects_an_unknown_key_by_name(tmp_path):
    """Unknown owned keys are configuration errors, never silently ignored.

    A typo'd `owignored` that ow shrugged at would look exactly like a rule
    the user wrote and ow refuses to honour."""
    path = _write(tmp_path, 'version = 2\nowignored = [".zed/**"]\n[remotes]\n')
    with pytest.raises(ValueError, match="owignored"):
        load_config(path)


def test_load_config_rejects_unknown_option_keys(tmp_path):
    path = _write(tmp_path, 'version = 2\n[remotes]\n[odoo]\nhttp_portt = 8069\n')
    with pytest.raises(ValueError, match="http_portt"):
        load_config(path)


def test_load_config_rejects_a_bool_port(tmp_path):
    path = _write(tmp_path, 'version = 2\n[remotes]\n[odoo]\nhttp_port = true\n')
    with pytest.raises(ValueError, match="http_port"):
        load_config(path)


# ---------------------------------------------------------------------------
# Version markers
# ---------------------------------------------------------------------------

def test_load_config_version_absent_means_1(tmp_path):
    config = load_config(_write(tmp_path, '[remotes.community]\norigin.url = "x"\n'))

    assert config.version == 1
    assert config.legacy is not None


def test_load_config_version_unknown_raises(tmp_path):
    path = _write(tmp_path, 'version = 99\n[remotes.community]\norigin.url = "x"\n')
    with pytest.raises(ValueError, match="schema version 99"):
        load_config(path)


def test_load_config_non_integer_version_raises(tmp_path):
    path = _write(tmp_path, 'version = "2"\n[remotes]\n')
    with pytest.raises(ValueError, match="'version' must be an integer"):
        load_config(path)


def test_load_workspace_config_version_absent_means_1(tmp_path):
    ws = load_workspace_config(_write(tmp_path, 'templates = ["common"]\n[repos]\ncommunity = "master"'))

    assert ws.version == 1
    assert ws.legacy is not None


def test_load_workspace_config_version_unknown_raises(tmp_path):
    toml = 'version = 99\ntemplates = ["common"]\n[repos]\ncommunity = "master"'
    with pytest.raises(ValueError, match="schema version 99"):
        load_workspace_config(_write(tmp_path, toml))


# ---------------------------------------------------------------------------
# The schema-2 workspace config
# ---------------------------------------------------------------------------

SAMPLE_WS_CONFIG = """\
version = 2

[repos]
community = "master..master-parrot"
enterprise = "master..master-parrot"

[odoo]
http_port = 8067
debug_args = ["--dev=all"]

[mise]
python = "3.11"
"""


def test_load_workspace_config(tmp_path):
    ws = load_workspace_config(_write(tmp_path, SAMPLE_WS_CONFIG))

    assert ws.version == 2
    assert ws.legacy is None
    assert ws.repos["community"] == BranchSpec("origin/master", "master-parrot")
    assert ws.repos["enterprise"] == BranchSpec("origin/master", "master-parrot")
    assert ws.odoo == OdooOverrides(http_port=8067, debug_args=("--dev=all",))
    assert ws.mise == MiseOverrides(python="3.11")
    assert not hasattr(ws, "vars")
    assert not hasattr(ws, "templates")


def test_load_workspace_config_without_repos_is_empty(tmp_path):
    """A generic workspace is a complete workspace: no repo is required."""
    ws = load_workspace_config(_write(tmp_path, "version = 2\n"))

    assert ws.repos == {}
    assert ws.odoo == OdooOverrides()


def test_load_workspace_config_rejects_unknown_keys(tmp_path):
    toml = 'version = 2\ntemplates = ["common"]\n[repos]\ncommunity = "master"\n'
    with pytest.raises(ValueError, match="templates"):
        load_workspace_config(_write(tmp_path, toml))


def test_load_workspace_config_rejects_owignore(tmp_path):
    """The ignore list is global: a workspace has no output list of its own."""
    with pytest.raises(ValueError, match="owignore.*global"):
        load_workspace_config(_write(tmp_path, 'version = 2\nowignore = [".zed/**"]\n[repos]\n'))


def test_load_workspace_config_rejects_unknown_option_key(tmp_path):
    toml = 'version = 2\n[repos]\n[odoo]\npython = "3.11"\n'
    with pytest.raises(ValueError, match="python"):
        load_workspace_config(_write(tmp_path, toml))


def test_load_workspace_config_rejects_a_malformed_repo_spec(tmp_path):
    with pytest.raises(ValueError, match="invalid branch spec"):
        load_workspace_config(_write(tmp_path, 'version = 2\n[repos]\ncommunity = "a..b..c"\n'))


# ---------------------------------------------------------------------------
# Schema 1: the legacy translation
# ---------------------------------------------------------------------------

LEGACY_WS = """\
templates = ["common", "vscode"]

[repos]
community = "master..master-parrot"

[vars]
http_port = 8067
db_host = "db"
db_port = 5433
db_user = "user"
db_password = "secret"
admin_passwd = "admin"
smtp_server = "mail"
smtp_port = 1025
debug_args = ["--dev=all"]
debug_test_args = ["--test-tags=legacy"]
python = "3.11"
"""


def test_legacy_workspace_translates_every_known_key(tmp_path):
    ws = load_workspace_config(_write(tmp_path, LEGACY_WS))

    assert ws.version == 1
    assert ws.legacy is not None
    assert ws.legacy.original == LEGACY_WS.encode()
    assert ws.legacy.source == tmp_path / "config.toml"
    assert ws.legacy.issues == ()
    assert ws.odoo == OdooOverrides(
        http_port=8067,
        db_host="db",
        db_port=5433,
        db_user="user",
        db_password="secret",
        admin_passwd="admin",
        smtp_server="mail",
        smtp_port=1025,
        debug_args=("--dev=all",),
        debug_test_args=("--test-tags=legacy",),
    )
    assert ws.mise == MiseOverrides(python="3.11")
    assert ws.repos["community"] == BranchSpec("origin/master", "master-parrot")


def test_legacy_typed_keys_win_only_on_their_own_key(tmp_path):
    """A transitional `[odoo]`/`[mise]` key beats `vars` for that field alone."""
    toml = """\
version = 1

[repos]
community = "master"

[vars]
http_port = 8067
db_host = "from-vars"
python = "3.10"

[odoo]
http_port = 8070
debug_args = ["--dev=all"]

[mise]
python = "3.12"
"""
    ws = load_workspace_config(_write(tmp_path, toml))

    assert ws.odoo.http_port == 8070
    assert ws.odoo.db_host == "from-vars"
    assert ws.odoo.debug_args == ("--dev=all",)
    assert ws.mise.python == "3.12"


def test_legacy_unknown_var_is_an_issue_not_a_failure(tmp_path):
    toml = '[repos]\ncommunity = "master"\n\n[vars]\nmystery = "kept"\nhttp_port = 8067\n'
    ws = load_workspace_config(_write(tmp_path, toml))

    assert ws.odoo.http_port == 8067
    assert any("mystery" in issue for issue in ws.legacy.issues)


def test_legacy_unrepresentable_value_is_an_issue(tmp_path):
    toml = '[repos]\ncommunity = "master"\n\n[vars]\nhttp_port = "not-a-port"\n'
    ws = load_workspace_config(_write(tmp_path, toml))

    assert ws.odoo.http_port is None
    assert any("http_port" in issue for issue in ws.legacy.issues)


def test_legacy_unknown_top_level_key_is_an_issue(tmp_path):
    toml = 'repos = { community = "master" }\neditor = "nvim"\n'
    ws = load_workspace_config(_write(tmp_path, toml))

    assert any("editor" in issue for issue in ws.legacy.issues)


def test_legacy_templates_must_be_a_list(tmp_path):
    toml = 'templates = "common"\n[repos]\ncommunity = "master"\n'
    ws = load_workspace_config(_write(tmp_path, toml))

    assert any("templates" in issue for issue in ws.legacy.issues)
    with pytest.raises(ValueError, match="templates"):
        _legacy_templates({"templates": "common"})


def test_legacy_workspace_without_templates_is_not_an_issue(tmp_path):
    """`common` was always implicit; a manifest that never named it is normal."""
    ws = load_workspace_config(_write(tmp_path, '[repos]\ncommunity = "master"\n'))

    assert ws.legacy.issues == ()
    assert _legacy_templates({}) == ()


def test_legacy_workspace_with_owignore_is_an_issue(tmp_path):
    toml = 'owignore = [".zed/**"]\n[repos]\ncommunity = "master"\n'
    ws = load_workspace_config(_write(tmp_path, toml))

    assert any("owignore" in issue and "global" in issue for issue in ws.legacy.issues)


def test_legacy_global_config_translates_and_keeps_its_bytes(tmp_path):
    toml = """\
version = 1
editor = "nvim"

[vars]
http_port = 8067
python = "3.11"

[remotes.community]
origin.url = "git@github.com:odoo/odoo.git"
"""
    config = load_config(_write(tmp_path, toml))

    assert config.version == 1
    assert config.legacy.original == toml.encode()
    assert config.editor == "nvim"
    assert config.odoo == OdooOverrides(http_port=8067)
    assert config.mise == MiseOverrides(python="3.11")


def test_legacy_malformed_repos_are_a_load_error(tmp_path):
    """Repos are intent, not evidence: an unreadable one is not translatable."""
    with pytest.raises(ValueError, match="repos.community"):
        load_workspace_config(_write(tmp_path, '[repos]\ncommunity = 18.0\n'))


def test_invalid_toml_is_a_load_error(tmp_path):
    import tomllib

    with pytest.raises(tomllib.TOMLDecodeError):
        load_workspace_config(_write(tmp_path, 'templates = ["common\n'))


def test_legacy_options_reports_the_key_it_cannot_translate():
    translation = _legacy_options({"vars": {"python": "3.12.1", "port": 1}})

    assert translation.mise == MiseOverrides()
    assert len(translation.issues) == 2


# ---------------------------------------------------------------------------
# The default global configuration
# ---------------------------------------------------------------------------

def test_default_bootstrap_is_a_valid_schema_2_document(tmp_path):
    """`_DEFAULT_CONFIG` is parsed by the same strict reader as any user file.

    That is what keeps the documented bootstrap and the in-memory default
    from drifting apart — and why the typed examples must stay commented:
    an uncommented `[mise] python` would hand Python tooling to every
    generic workspace that never asked for it."""
    config = load_config(_write(tmp_path, _DEFAULT_CONFIG))

    assert config.version == 2
    assert config.odoo == OdooOverrides()
    assert config.mise == MiseOverrides()
    assert config.owignore == ()
    assert "community" in config.remotes
    uncommented = [line.strip() for line in _DEFAULT_CONFIG.splitlines()]
    assert "[odoo]" not in uncommented
    assert "[mise]" not in uncommented


def test_load_global_config_reads_the_config_file(xdg):
    paths.config_home().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(SAMPLE_TOML)

    config = load_global_config()

    assert config.editor == "nvim"
    assert config.odoo == OdooOverrides(http_port=8069, db_host="localhost")
    assert config.remotes["community"]["origin"].url == "git@github.com:odoo/odoo.git"


def test_load_global_config_returns_defaults_without_creating_anything(xdg):
    """A read creates nothing: no file, no directory, no bootstrap."""
    assert not paths.config_file().exists()

    config = load_global_config()

    assert not paths.config_file().exists()
    assert not paths.config_home().exists()
    assert config.version == 2
    assert config.editor == "code"
    assert config.theme == "textual-dark"
    assert config.odoo == OdooOverrides()
    assert config.mise == MiseOverrides()
    assert config.remotes["community"]["origin"].url == "git@github.com:odoo/odoo.git"


def test_global_config_default_is_not_python_for_generic_workspaces(xdg):
    """The in-memory default must not opt every workspace into Python."""
    assert load_global_config().mise.python is None


# ---------------------------------------------------------------------------
# write_workspace_config
# ---------------------------------------------------------------------------

def test_write_workspace_config_round_trip(tmp_path):
    ws = WorkspaceConfig(
        repos={
            "community": BranchSpec("origin/master", "master-parrot"),
            "enterprise": BranchSpec("origin/master", "master-parrot"),
        },
        odoo=OdooOverrides(http_port=8067, debug_args=("--dev=all",)),
        mise=MiseOverrides(python="3.11"),
    )
    config_path = tmp_path / ".ow" / "config.toml"
    write_workspace_config(config_path, ws)
    ws2 = load_workspace_config(config_path)

    assert ws2.repos == ws.repos
    assert ws2.odoo == ws.odoo
    assert ws2.mise == ws.mise
    assert ws2.version == 2


def test_write_workspace_config_is_sparse(tmp_path):
    """An unset option is absent: a workspace never pins an inherited default."""
    ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")})
    config_path = tmp_path / ".ow" / "config.toml"
    write_workspace_config(config_path, ws)

    content = config_path.read_text()
    assert "http_port" not in content
    assert "[odoo]" not in content
    assert "[mise]" not in content


def test_write_workspace_config_keeps_an_explicit_empty_list(tmp_path):
    """An empty debug list is a decision, not an absence."""
    ws = WorkspaceConfig(
        repos={}, odoo=OdooOverrides(debug_args=())
    )
    config_path = tmp_path / ".ow" / "config.toml"
    write_workspace_config(config_path, ws)

    assert load_workspace_config(config_path).odoo.debug_args == ()
    assert "debug_args = []" in config_path.read_text()


def test_write_workspace_config_includes_version(tmp_path):
    ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")})
    config_path = tmp_path / "config.toml"
    write_workspace_config(config_path, ws)

    lines = config_path.read_text().splitlines()
    assert lines[0].startswith("# Managed by ow")
    assert "Do not edit `version`" in lines[0]
    assert lines[1] == "version = 2"


def test_write_workspace_config_is_private(tmp_path):
    """Both configs can hold a db_password: every writer replaces them 0600."""
    config_path = tmp_path / ".ow" / "config.toml"
    write_workspace_config(config_path, WorkspaceConfig(repos={}))

    assert (config_path.stat().st_mode & 0o777) == 0o600
    assert list(tmp_path.glob("**/*.tmp")) == []


def test_write_workspace_config_keeps_a_legacy_file_legacy(tmp_path):
    """A v1 record is written back as v1: repos only, everything else intact.

    `ow switch` is the caller this exists for: it rewrites the specs git was
    actually given, and it has no business converting a manifest — or
    dropping the unknown sections a user hand-wrote."""
    original = textwrap.dedent("""\
        # hand-written note
        templates = ["common", "vscode"]

        [repos]
        community = "master..feat"

        [vars]
        http_port = 8067
        mystery = "kept"
        """)
    config_path = _write_ws(tmp_path, original)
    ws = load_workspace_config(config_path)

    write_workspace_config(config_path, replace(ws, repos={"community": BranchSpec("origin/master", "other")}))

    content = config_path.read_text()
    assert "# hand-written note" in content
    assert 'mystery = "kept"' in content
    assert 'templates = ["common", "vscode"]' in content
    assert "version" not in content
    assert 'community = "master..other"' in content
    assert (config_path.stat().st_mode & 0o777) == 0o600


def test_write_workspace_config_returns_refreshed_legacy_evidence(tmp_path):
    """The return value is the record to keep using, not a `None` a caller
    has to discard: `legacy.original` comes back as exactly the bytes just
    written, ready for the caller's *next* save."""
    config_path = _write_ws(tmp_path, 'templates = ["common"]\n[repos]\ncommunity = "master"\n')
    ws = load_workspace_config(config_path)

    saved = write_workspace_config(
        config_path, replace(ws, repos={"community": BranchSpec("origin/master", "feat")})
    )

    assert saved.legacy is not None
    assert saved.legacy.original == config_path.read_bytes()
    assert saved.legacy.issues == ws.legacy.issues


def test_saving_from_the_returned_workspace_record_does_not_revert_an_earlier_edit(tmp_path):
    """The same hazard `write_global_config` has, on the repos this writer owns:
    chaining the second save from what the first one returned — never from the
    object that was loaded — is what keeps it from undoing the first edit."""
    config_path = _write_ws(
        tmp_path,
        'templates = ["common"]\n[repos]\ncommunity = "master"\nenterprise = "master"\n',
    )
    ws = load_workspace_config(config_path)

    saved = write_workspace_config(
        config_path,
        replace(ws, repos={**ws.repos, "community": BranchSpec("origin/master", "feat")}),
    )
    write_workspace_config(
        config_path,
        replace(saved, repos={**saved.repos, "enterprise": BranchSpec("origin/master", "other")}),
    )

    reloaded = load_workspace_config(config_path)
    assert reloaded.repos["community"] == BranchSpec("origin/master", "feat")
    assert reloaded.repos["enterprise"] == BranchSpec("origin/master", "other")


def test_write_workspace_config_refuses_a_migrated_destination(tmp_path):
    """A stale schema-1 record must not revert a file `ow render` migrated.

    The TUI holds a Config across screens: between opening it and saving it,
    an explicit migration can replace the file. Re-reading the destination
    and checking its schema is what makes that save refuse instead of
    writing schema-1 text over schema-2 content."""
    config_path = _write_ws(tmp_path, 'templates = ["common"]\n[repos]\ncommunity = "master"\n')
    ws = load_workspace_config(config_path)
    migrated = dumps_workspace_config(replace(ws, version=2, legacy=None))
    config_path.write_bytes(migrated)

    with pytest.raises(ValueError, match="schema 2, not schema 1"):
        write_workspace_config(config_path, ws)

    assert config_path.read_bytes() == migrated


def test_write_workspace_config_refuses_a_vanished_destination(tmp_path):
    config_path = _write_ws(tmp_path, '[repos]\ncommunity = "master"\n')
    ws = load_workspace_config(config_path)
    config_path.unlink()

    with pytest.raises(ValueError, match="no longer exists"):
        write_workspace_config(config_path, ws)


def test_write_workspace_config_refuses_a_typed_change_on_a_legacy_record(tmp_path):
    """Schema 1 has nowhere to put typed options: name `ow render`, never convert."""
    config_path = _write_ws(tmp_path, '[repos]\ncommunity = "master"\n')
    ws = load_workspace_config(config_path)

    with pytest.raises(ValueError, match="ow render"):
        write_workspace_config(config_path, replace(ws, odoo=OdooOverrides(http_port=9999)))

    assert "http_port" not in config_path.read_text()


def test_dumps_workspace_config_is_pure(tmp_path):
    """Serialization reads no destination: it works with nothing on disk."""
    ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")})
    data = dumps_workspace_config(ws)

    assert b"version = 2" in data
    assert not (tmp_path / ".ow").exists()


def test_dumps_and_write_agree_for_a_legacy_record(tmp_path):
    """One writer per format: migration's replacement bytes are the save's.

    Migration builds `MigrationWrite.replacement` from `dumps_*`, so a
    divergence between what a pure dump says and what a save writes would be
    a migration writing a different file than the one it previewed."""
    config_path = _write_ws(tmp_path, 'templates = ["common"]\n[repos]\ncommunity = "master"\n')
    ws = load_workspace_config(config_path)
    updated = replace(ws, repos={"community": BranchSpec("origin/master", "feat")})

    expected = dumps_workspace_config(updated)
    write_workspace_config(config_path, updated)

    assert config_path.read_bytes() == expected


# ---------------------------------------------------------------------------
# Schema 2 accepts nothing positional
# ---------------------------------------------------------------------------

def test_config_constructors_are_keyword_only():
    """A positional `Config(vars, remotes)` must fail loudly.

    The old model's first two positional arguments were vars and remotes.
    If a new field could silently bind where one of them used to, a stale
    caller would write the wrong file instead of crashing."""
    with pytest.raises(TypeError):
        Config({}, {})
    with pytest.raises(TypeError):
        WorkspaceConfig({}, ["common"])


def test_legacy_config_is_immutable():
    import dataclasses

    legacy = LegacyConfig(source=Path("/x"), original=b"")
    with pytest.raises(dataclasses.FrozenInstanceError):
        legacy.issues = ()


# ---------------------------------------------------------------------------
# Pending migration reporting
# ---------------------------------------------------------------------------

def test_pending_migration_names_every_schema_1_source(xdg, tmp_path):
    config = Config(remotes={}, version=1, legacy=LegacyConfig(tmp_path / "global.toml", b""))
    ws = WorkspaceConfig(repos={}, version=1, legacy=LegacyConfig(tmp_path / "ws.toml", b""))

    [message] = pending_migration(config, ws, tmp_path / "ws")

    assert str(tmp_path / "global.toml") in message
    assert str(tmp_path / "ws.toml") in message
    assert f"ow render -w {tmp_path / 'ws'}" in message


def test_pending_migration_is_silent_for_schema_2(xdg, tmp_path):
    assert pending_migration(Config(remotes={}), WorkspaceConfig(repos={}), tmp_path) == ()


def test_report_pending_migration_warns_once_per_source(xdg, tmp_path, capsys):
    """A command that loads config twice warns once; the loader never warns."""
    source = tmp_path / "ws.toml"
    ws = WorkspaceConfig(repos={}, version=1, legacy=LegacyConfig(source, b""))

    report_pending_migration(Config(remotes={}), ws, tmp_path)
    report_pending_migration(Config(remotes={}), ws, tmp_path)

    err = capsys.readouterr().err
    assert err.count("Pending migration") == 1
    assert str(source) in err
    assert "ow render" in err


# ---------------------------------------------------------------------------
# Global writer
# ---------------------------------------------------------------------------

def test_write_global_config_keeps_the_documented_comments(xdg):
    config = load_global_config()

    write_global_config(config)

    written = paths.config_file().read_text()
    for line in _DEFAULT_CONFIG.splitlines():
        if line.strip().startswith("#"):
            assert line in written
    assert (paths.config_file().stat().st_mode & 0o777) == 0o600


def test_write_global_config_round_trips_every_field(xdg):
    config = Config(
        remotes={
            "community": {
                "origin": RemoteConfig(url="git@github.com:odoo/odoo.git"),
                "dev": RemoteConfig(
                    url="git@github.com:odoo-dev/odoo.git",
                    pushurl="git@github.com:odoo-dev/odoo.git",
                    fetch="+refs/heads/*:refs/remotes/dev/*",
                ),
            },
        },
        odoo=OdooOverrides(http_port=8070, debug_args=()),
        mise=MiseOverrides(python="3.12"),
        owignore=(".zed/**",),
        editor="nvim",
        theme="dracula",
    )
    write_global_config(config)
    reloaded = load_global_config()

    assert reloaded.editor == "nvim"
    assert reloaded.theme == "dracula"
    assert reloaded.owignore == (".zed/**",)
    assert reloaded.odoo == OdooOverrides(http_port=8070, debug_args=())
    assert reloaded.mise == MiseOverrides(python="3.12")
    assert reloaded.remotes["community"]["dev"].fetch == "+refs/heads/*:refs/remotes/dev/*"


def test_dumps_global_config_is_pure_and_schema_2(xdg):
    data = dumps_global_config(Config(remotes={}))

    assert b"version = 2" in data
    assert not paths.config_file().exists()


# ---------------------------------------------------------------------------
# find_project_root
# ---------------------------------------------------------------------------

class TestFindProjectRoot:
    """find_project_root locates the ow project owning a path."""

    def test_finds_root_at_start(self, tmp_path):
        (tmp_path / "ow.toml").write_text("[remotes]\n")
        assert find_project_root(tmp_path) == tmp_path

    def test_walks_up_from_nested_path(self, tmp_path):
        (tmp_path / "ow.toml").write_text("[remotes]\n")
        nested = tmp_path / "workspaces" / "ws" / "community"
        nested.mkdir(parents=True)
        assert find_project_root(nested) == tmp_path

    def test_accepts_example_marker(self, tmp_path):
        (tmp_path / "ow.toml.example").write_text("[remotes]\n")
        assert find_project_root(tmp_path) == tmp_path

    def test_returns_none_when_no_project_above(self, tmp_path):
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert find_project_root(nested) is None

    def test_stops_at_nearest_root(self, tmp_path):
        (tmp_path / "ow.toml").write_text("[remotes]\n")
        inner = tmp_path / "inner"
        inner.mkdir()
        (inner / "ow.toml").write_text("[remotes]\n")
        assert find_project_root(inner) == inner


class TestSelectAliases:
    """Shared by every --only flag; lives in config.py beside the repo aliases it filters."""
    def test_none_selects_everything(self):
        assert select_aliases(["a", "b"], None) == ["a", "b"]

    def test_only_filters_and_preserves_config_order(self):
        assert select_aliases(["a", "b", "c"], "c,a") == ["a", "c"]

    def test_only_tolerates_spaces(self):
        assert select_aliases(["a", "b"], " a , b ") == ["a", "b"]

    def test_unknown_alias_raises_and_lists_the_valid_ones(self):
        import typer
        with pytest.raises(typer.BadParameter) as exc:
            select_aliases(["a", "b"], "nope")
        assert "nope" in str(exc.value)
        assert "a, b" in str(exc.value)

    @pytest.mark.parametrize("only", ["", ",", " ", " , "])
    def test_an_only_that_names_nothing_is_a_user_error(self, only):
        """`ow rebase --only ''` used to rebase nothing and report success —
        an explicit --only that selects no repo is a mistake, not a request
        to do nothing."""
        import typer
        with pytest.raises(typer.BadParameter) as exc:
            select_aliases(["a", "b"], only)
        assert repr(only) in str(exc.value)
        assert "a, b" in str(exc.value)
