from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ow.config import BranchSpec, Config, RemoteConfig, WorkspaceConfig, write_workspace_config


def _make_config(
    root_dir: Path | str | None = None,
    vars: dict[str, Any] | None = None,
    remotes: dict[str, dict[str, RemoteConfig]] | None = None,
) -> Config:
    return Config(
        vars=vars if vars is not None else {"http_port": 8069, "db_host": "localhost", "db_port": 5432},
        remotes=remotes or {},
        root_dir=Path(root_dir) if root_dir is not None else Path("/root"),
    )


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return _make_config(root_dir=tmp_path)


@pytest.fixture
def config_with_remotes(tmp_path: Path) -> Config:
    remotes = {
        "community": {
            "origin": MagicMock(url="git@github.com:odoo/odoo.git"),
        },
    }
    return _make_config(root_dir=tmp_path, remotes=remotes)


@pytest.fixture
def config_full(tmp_path: Path) -> Config:
    remotes = {
        "community": {
            "origin": MagicMock(url="git@github.com:odoo/odoo.git"),
        },
    }
    return _make_config(
        root_dir=tmp_path,
        vars={"http_port": 8069, "db_host": "localhost", "db_port": 5432},
        remotes=remotes,
    )


@pytest.fixture
def ws_config() -> WorkspaceConfig:
    """Helper factory — call ws_config(repos=..., templates=..., vars=...)."""
    def _make(
        repos: dict[str, str] | dict[str, BranchSpec] | None = None,
        templates: list[str] | None = None,
        vars: dict[str, Any] | None = None,
    ) -> WorkspaceConfig:
        from ow.config import parse_branch_spec
        if repos is None:
            repos = {"community": BranchSpec("origin/master")}
        parsed = {}
        for alias, spec in repos.items():
            parsed[alias] = spec if isinstance(spec, BranchSpec) else parse_branch_spec(spec)
        return WorkspaceConfig(
            repos=parsed,
            templates=templates or ["common"],
            vars=vars or {},
        )
    return _make


@pytest.fixture
def workspace_dir(tmp_path: Path) -> Path:
    """Create a temporary workspace with a .ow/config file."""
    def _make(
        templates: list[str] | None = None,
        repos: dict[str, str] | None = None,
        vars: dict[str, Any] | None = None,
        name: str = "test",
    ) -> Path:
        from ow.config import parse_branch_spec
        ws_dir = tmp_path / "workspaces" / name
        ws_dir.mkdir(parents=True)
        parsed_repos = {}
        if repos:
            for alias, spec in repos.items():
                parsed_repos[alias] = spec if isinstance(spec, BranchSpec) else parse_branch_spec(spec)
        else:
            parsed_repos = {"community": BranchSpec("origin/master")}
        ws = WorkspaceConfig(
            repos=parsed_repos,
            templates=templates or ["common"],
            vars=vars or {},
        )
        write_workspace_config(ws_dir / ".ow" / "config", ws)
        return ws_dir
    return _make
