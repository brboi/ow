import tempfile
import textwrap
from pathlib import Path

import pytest

from ow.utils.config import (
    BranchSpec,
    WorkspaceConfig,
    find_project_root,
    load_config,
    load_global_config,
    select_aliases,
    load_workspace_config,
    parse_branch_spec,
    write_workspace_config,
)
from ow.utils import paths

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
# load_config
# ---------------------------------------------------------------------------

SAMPLE_TOML = """\
[vars]
http_port = 8069
db_host = "localhost"

[remotes]
community.origin.url = "git@github.com:odoo/odoo.git"
community.dev.url = "git@github.com:odoo-dev/odoo.git"
community.dev.pushurl = "git@github.com:odoo-dev/odoo.git"
community.dev.fetch = "+refs/heads/*:refs/remotes/dev/*"
"""


def test_load_config():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "ow.toml"
        path.write_text(SAMPLE_TOML)
        config = load_config(path)

    assert config.vars == {"http_port": 8069, "db_host": "localhost"}
    assert not hasattr(config, "root_dir")

    assert "community" in config.remotes
    assert config.remotes["community"]["origin"].url == "git@github.com:odoo/odoo.git"
    assert config.remotes["community"]["dev"].pushurl == "git@github.com:odoo-dev/odoo.git"
    assert config.remotes["community"]["dev"].fetch == "+refs/heads/*:refs/remotes/dev/*"


def test_load_config_vars_empty():
    toml = textwrap.dedent("""\
        [remotes]
        community.origin.url = "git@github.com:odoo/odoo.git"
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "ow.toml"
        path.write_text(toml)
        config = load_config(path)

    assert config.vars == {}

def test_load_config_remotes_missing_url_raises_valueerror():
    toml = textwrap.dedent("""\
    [remotes.community]
    origin.pushurl = "git@github.com:odoo-dev/odoo.git"
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "config.toml"
        path.write_text(toml)
        with pytest.raises(ValueError, match="community.*origin"):
            load_config(path)


def test_load_config_remotes_non_table_raises_valueerror():
    toml = textwrap.dedent("""\
    [remotes.community]
    origin = "not-a-table"
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "config.toml"
        path.write_text(toml)
        with pytest.raises(ValueError, match="community.*origin"):
            load_config(path)


# ---------------------------------------------------------------------------
# version field
# ---------------------------------------------------------------------------

def test_load_config_with_version():
    toml = "version = 1\n[remotes.community]\norigin.url = \"x\""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "config.toml"
        path.write_text(toml)
        config = load_config(path)
        assert config.version == 1

def test_load_config_version_absent_means_1():
    toml = "[remotes.community]\norigin.url = \"x\""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "config.toml"
        path.write_text(toml)
        config = load_config(path)
        assert config.version == 1

def test_load_config_version_unknown_raises():
    toml = "version = 99\n[remotes.community]\norigin.url = \"x\""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "config.toml"
        path.write_text(toml)
        with pytest.raises(ValueError, match="schema version 99"):
            load_config(path)

def test_load_workspace_config_version_absent_means_1():
    toml = 'templates = ["common"]\n[repos]\ncommunity = "master"'
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "config.toml"
        path.write_text(toml)
        ws = load_workspace_config(path)
        assert ws.version == 1

def test_write_workspace_config_includes_version():
    ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=["common"])
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "config.toml"
        write_workspace_config(path, ws)
        content = path.read_text()
        assert "version = 1" in content


# ---------------------------------------------------------------------------
# load_global_config
# ---------------------------------------------------------------------------

def test_load_global_config_reads_the_config_file(xdg):
    paths.config_home().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(SAMPLE_TOML)

    config = load_global_config()

    assert config.vars == {"http_port": 8069, "db_host": "localhost"}
    assert config.remotes["community"]["origin"].url == "git@github.com:odoo/odoo.git"


def test_load_global_config_bootstraps_when_missing(xdg, capsys):
    path = paths.config_file()
    assert not path.exists()

    config = load_global_config()

    assert path.exists()
    content = path.read_text()
    assert content.startswith("# ow configuration")
    assert "community" in config.remotes

    err = capsys.readouterr().err
    assert "Created" in err
    assert str(path) in err


def test_load_global_config_bootstraps_only_once(xdg):
    load_global_config()
    path = paths.config_file()
    first_mtime = path.stat().st_mtime_ns

    load_global_config()

    assert path.stat().st_mtime_ns == first_mtime


def test_load_global_config_creates_the_parent_directory(xdg):
    assert not paths.config_home().exists()

    load_global_config()

    assert paths.config_home().exists()


# ---------------------------------------------------------------------------
# load_workspace_config
# ---------------------------------------------------------------------------

SAMPLE_WS_CONFIG = """\
templates = ["common", "vscode"]

[repos]
community = "master..master-parrot"
enterprise = "master..master-parrot"

[vars]
http_port = 8067
"""


def test_load_workspace_config():
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / ".ow" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(SAMPLE_WS_CONFIG)
        ws = load_workspace_config(config_path)

    assert ws.templates == ["common", "vscode"]
    assert ws.repos["community"] == BranchSpec("origin/master", "master-parrot")
    assert ws.repos["enterprise"] == BranchSpec("origin/master", "master-parrot")
    assert ws.vars == {"http_port": 8067}


def test_load_workspace_config_no_vars():
    toml = textwrap.dedent("""\
        templates = ["common"]

        [repos]
        community = "master"
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / ".ow" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(toml)
        ws = load_workspace_config(config_path)

    assert ws.vars == {}


def test_load_workspace_config_missing_templates():
    toml = textwrap.dedent("""\
        [repos]
        community = "master"
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / ".ow" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(toml)
        with pytest.raises(ValueError, match="missing required 'templates'"):
            load_workspace_config(config_path)


def test_load_workspace_config_empty_templates():
    """Empty templates list is allowed — workspace with no template files."""
    toml = textwrap.dedent("""\
        templates = []

        [repos]
        community = "master"
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / ".ow" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(toml)
        ws = load_workspace_config(config_path)
        assert ws.templates == []


def test_load_workspace_config_templates_not_list():
    toml = textwrap.dedent("""\
        templates = "common"

        [repos]
        community = "master"
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / ".ow" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(toml)
        with pytest.raises(ValueError, match="must be a list"):
            load_workspace_config(config_path)


# ---------------------------------------------------------------------------
# write_workspace_config
# ---------------------------------------------------------------------------

def test_write_workspace_config_round_trip():
    ws = WorkspaceConfig(
        repos={
            "community": BranchSpec("origin/master", "master-parrot"),
            "enterprise": BranchSpec("origin/master", "master-parrot"),
        },
        templates=["common", "vscode"],
        vars={"http_port": 8067},
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / ".ow" / "config.toml"
        write_workspace_config(config_path, ws)
        ws2 = load_workspace_config(config_path)

    assert ws2.templates == ws.templates
    assert ws2.repos == ws.repos
    assert ws2.vars == ws.vars


def test_write_workspace_config_no_vars():
    ws = WorkspaceConfig(
        repos={"community": BranchSpec("origin/master")},
        templates=["common"],
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / ".ow" / "config.toml"
        write_workspace_config(config_path, ws)
        content = config_path.read_text()
        ws2 = load_workspace_config(config_path)

    assert ws2.templates == ws.templates
    assert ws2.repos == ws.repos
    assert ws2.vars == {}
    assert "vars" not in content


def test_write_workspace_config_detached():
    ws = WorkspaceConfig(
        repos={"community": BranchSpec("origin/18.0")},
        templates=["common"],
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / ".ow" / "config.toml"
        write_workspace_config(config_path, ws)
        ws2 = load_workspace_config(config_path)

    assert ws2.repos["community"].is_detached
    assert ws2.repos["community"].base_ref == "origin/18.0"


def test_write_workspace_config_non_origin_remote():
    ws = WorkspaceConfig(
        repos={"community": BranchSpec("dev/master-phoenix", "fix")},
        templates=["common"],
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / ".ow" / "config.toml"
        write_workspace_config(config_path, ws)
        ws2 = load_workspace_config(config_path)

    assert ws2.repos["community"] == BranchSpec("dev/master-phoenix", "fix")


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
        """`ow apply --only ''` used to materialize nothing and print
        "applied." — an explicit --only that selects no repo is a mistake,
        not a request to do nothing."""
        import typer
        with pytest.raises(typer.BadParameter) as exc:
            select_aliases(["a", "b"], only)
        assert repr(only) in str(exc.value)
        assert "a, b" in str(exc.value)
