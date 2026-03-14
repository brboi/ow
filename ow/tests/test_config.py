import tempfile
from pathlib import Path

import pytest

from ow.config import (
    BranchSpec,
    WorkspaceConfig,
    format_workspace,
    load_config,
    parse_branch_spec,
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

[[workspace]]
name = "test-ws"
repo.community = "master"
repo.enterprise = "master..master-feature"
vars.http_port = 8070

[[workspace]]
name = "detached-ws"
repo.community = "18.0"
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

    assert len(config.workspaces) == 2

    ws = config.workspaces[0]
    assert ws.name == "test-ws"
    assert ws.repos["community"] == BranchSpec("origin/master")
    assert ws.repos["enterprise"] == BranchSpec("origin/master", "master-feature")
    assert ws.vars == {"http_port": 8070}

    ws2 = config.workspaces[1]
    assert ws2.name == "detached-ws"
    assert ws2.repos["community"].is_detached


# ---------------------------------------------------------------------------
# format_workspace
# ---------------------------------------------------------------------------

def test_format_workspace_simple():
    ws = WorkspaceConfig(
        name="test",
        repos={"community": BranchSpec("origin/master")},
    )
    result = format_workspace(ws)
    assert "[[workspace]]" in result
    assert 'name = "test"' in result
    assert 'repo.community = "master"' in result


def test_format_workspace_with_local_branch():
    ws = WorkspaceConfig(
        name="test",
        repos={
            "community": BranchSpec("origin/master"),
            "enterprise": BranchSpec("origin/master", "master-test"),
        },
    )
    result = format_workspace(ws)
    assert 'repo.community = "master"' in result
    assert 'repo.enterprise = "master..master-test"' in result


def test_format_workspace_with_vars():
    ws = WorkspaceConfig(
        name="test",
        repos={"community": BranchSpec("origin/master")},
        vars={"http_port": 8070},
    )
    result = format_workspace(ws)
    assert "vars.http_port = 8070" in result


def test_format_workspace_non_origin_remote():
    ws = WorkspaceConfig(
        name="test",
        repos={"community": BranchSpec("dev/master-phoenix", "fix")},
    )
    result = format_workspace(ws)
    assert 'repo.community = "dev/master-phoenix..fix"' in result
