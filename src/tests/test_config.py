import tempfile
import textwrap
from pathlib import Path

import pytest

from ow.utils.config import (
    BranchSpec,
    WorkspaceConfig,
    load_config,
    load_workspace_config,
    parse_branch_spec,
    write_workspace_config,
)

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
    assert config.root_dir == Path(tmpdir)

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
        config_path = Path(tmpdir) / ".ow" / "config"
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
        config_path = Path(tmpdir) / ".ow" / "config"
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
        config_path = Path(tmpdir) / ".ow" / "config"
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
        config_path = Path(tmpdir) / ".ow" / "config"
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
        config_path = Path(tmpdir) / ".ow" / "config"
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
        config_path = Path(tmpdir) / ".ow" / "config"
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
        config_path = Path(tmpdir) / ".ow" / "config"
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
        config_path = Path(tmpdir) / ".ow" / "config"
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
        config_path = Path(tmpdir) / ".ow" / "config"
        write_workspace_config(config_path, ws)
        ws2 = load_workspace_config(config_path)

    assert ws2.repos["community"] == BranchSpec("dev/master-phoenix", "fix")
