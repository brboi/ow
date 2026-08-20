import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ow.utils.config import (
    BranchSpec,
    Config,
    parse_branch_spec,
    RemoteConfig,
    WorkspaceConfig,
    write_workspace_config,
)


def _make_config(
    vars: dict[str, Any] | None = None,
    remotes: dict[str, dict[str, RemoteConfig]] | None = None,
) -> Config:
    return Config(
        vars=vars if vars is not None else {"http_port": 8069, "db_host": "localhost", "db_port": 5432},
        remotes=remotes or {},
    )


@pytest.fixture
def config(xdg: Path) -> Config:
    return _make_config()


@pytest.fixture
def config_with_remotes(xdg: Path) -> Config:
    remotes = {
        "community": {
            "origin": MagicMock(url="git@github.com:odoo/odoo.git"),
        },
    }
    return _make_config(remotes=remotes)


@pytest.fixture
def config_full(xdg: Path) -> Config:
    remotes = {
        "community": {
            "origin": MagicMock(url="git@github.com:odoo/odoo.git"),
        },
    }
    return _make_config(
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
    """Create a temporary workspace with a .ow/config.toml file."""
    def _make(
        templates: list[str] | None = None,
        repos: dict[str, str] | None = None,
        vars: dict[str, Any] | None = None,
        name: str = "test",
    ) -> Path:
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
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        return ws_dir
    return _make


class GitLab:
    """A real git repository, for tests that must observe git's actual behaviour.

    Mocking subprocess proves nothing about commit reachability or patch
    identity, which is exactly what the rebase logic turns on.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    def commit(self, name: str, *, content: str | None = None) -> str:
        """Create a commit touching <name>.txt. Returns its SHA."""
        (self.path / f"{name}.txt").write_text(content if content is not None else name)
        self.git("add", "-A")
        self.git("commit", "-m", name)
        return self.sha("HEAD")

    def sha(self, ref: str) -> str:
        return self.git("rev-parse", ref)

    def set_remote_ref(self, ref: str, target: str) -> None:
        """Point refs/remotes/<ref> at <target>, e.g. set_remote_ref('origin/master', 'HEAD')."""
        self.git("update-ref", f"refs/remotes/{ref}", self.sha(target))

    def branch(self, name: str, start: str = "HEAD") -> None:
        self.git("branch", name, start)

    def checkout(self, ref: str) -> None:
        self.git("checkout", ref)


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every XDG base directory into tmp_path.

    Any test that touches configuration, bare repos, the workspace index or
    the template baseline must request this. Without it a test writes into
    the developer's real home.
    """
    for var, name in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_STATE_HOME", "state"),
    ):
        target = tmp_path / name
        target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(var, str(target))
    return tmp_path


@pytest.fixture
def git_lab(tmp_path: Path) -> GitLab:
    repo = tmp_path / "lab"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "master"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    lab = GitLab(repo)
    lab.commit("A")
    return lab
