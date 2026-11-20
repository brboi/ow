"""Pytest configuration, fixtures, and the helpers the test modules share."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Iterator

import pytest
from pytest import MonkeyPatch

from ow.core.config import Layer, resolve_settings, write_workspace
from ow.core.index import add as index_add
from ow.core.paths import index_file, repos_dir
from ow.core.plugins import GeneratedFile, load_plugins
from ow.core.workspace import Repo, Workspace
from tests.lab import file_url, git, git_ok

if TYPE_CHECKING:
    from tests.lab import Lab


@pytest.fixture(autouse=True)
def hermetic(tmp_path: Path, monkeypatch: MonkeyPatch) -> Iterator[None]:
    """Isolate git: a private HOME and XDG tree, a global config outside HOME, PATH."""
    home = tmp_path / "home"
    home.mkdir()

    xdg_config = home / ".config"
    xdg_config.mkdir(parents=True)

    xdg_data = home / ".local" / "share"
    xdg_data.mkdir(parents=True)

    xdg_state = home / ".local" / "state"
    xdg_state.mkdir(parents=True)

    xdg_cache = home / ".cache"
    xdg_cache.mkdir(parents=True)

    # Outside HOME, so nothing under test can rewrite the config it reads.
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        "[init]\n"
        "    defaultBranch = master\n"
        "[protocol \"file\"]\n"
        "    allow = always\n"
    )

    # Only the commands the tests and ow are allowed to find.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    for cmd in ("git", "sh", "bash", "true"):
        real_path = shutil.which(cmd)
        if real_path:
            link_path = bin_dir / cmd
            link_path.symlink_to(real_path)

    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_cache))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test Author")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2024-01-01T00:00:00Z")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test Committer")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2024-01-01T00:00:00Z")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    # `git rebase --continue` and friends must never open an editor in tests.
    monkeypatch.setenv("GIT_EDITOR", "true")
    monkeypatch.setenv("EDITOR", "true")
    monkeypatch.setenv("PATH", str(bin_dir))

    for var in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
        "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    ):
        monkeypatch.delenv(var, raising=False)
    inherited = [
        var
        for var in os.environ
        if var.startswith(("MISE_", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    ]
    for var in inherited:
        monkeypatch.delenv(var, raising=False)

    monkeypatch.chdir(home)

    yield


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: MonkeyPatch) -> Lab:
    """Lab fixture for creating test repositories and workspaces."""
    # Import here to avoid circular dependency
    from tests.lab import Lab

    return Lab(tmp_path, monkeypatch)


def index_bytes() -> bytes:
    """The workspace index file's bytes."""
    path = index_file()
    return path.read_bytes() if path.exists() else b""


def index_lines() -> list[str]:
    """The workspace index file's entries."""
    return [line for line in index_bytes().decode().splitlines() if line]


def workspace_tree(root: Path) -> dict[str, bytes]:
    """Every file under `root`, by relative path — the workspace tree and the lock."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def worktree_refs(lab: Lab, worktree: Path, *, remote_tracking: bool = True) -> dict[str, str]:
    """The worktree's refs, name -> sha; `remote_tracking` keeps the ones a fetch may move."""
    output = lab.git(worktree, "for-each-ref", "--format=%(refname) %(objectname)")
    return {
        name: sha
        for name, sha in (line.split(" ", 1) for line in output.splitlines())
        if remote_tracking or not name.startswith("refs/remotes/")
    }


def make_bare(
    lab: Lab,
    name: str = "community",
    branches: dict[str, dict[str, str | bytes]] | None = None,
    *,
    base: str = "18.0",
) -> Path:
    """A bare in repos_dir(), fed from a fresh `name` remote holding `branches`.

    `branches` is `lab.remote`'s mapping; the default is one `base` branch with a README.
    """
    if branches is None:
        branches = {base: {"README.md": "c\n"}}
    origin = lab.remote(name, branches, head=base)
    path = repos_dir() / f"{name}.git"
    path.parent.mkdir(parents=True, exist_ok=True)
    lab.git(path.parent, "init", "--bare", str(path))
    lab.git(path, "remote", "add", "origin", str(origin))
    lab.git(path, "fetch", "origin")
    return path


def add_worktree(
    lab: Lab,
    bare: Path,
    path: Path,
    *,
    branch: str | None = None,
    base: str = "18.0",
) -> None:
    """`git worktree add` at `path`: on `branch` (created here), or detached at `origin/<base>`."""
    if branch is None:
        lab.git(bare, "worktree", "add", "--detach", str(path), f"origin/{base}")
    else:
        lab.git(bare, "worktree", "add", "-b", branch, str(path), f"origin/{base}")


def workspace(
    lab: Lab,
    *,
    name: str = "voip",
    repo: str = "community",
    branches: dict[str, dict[str, str | bytes]] | None = None,
    base: str = "18.0",
    branch: str | None = None,
    bare: Path | None = None,
    entry: bool = True,
    local_commit: bool = False,
) -> Path:
    """A one-repo workspace at `~/ws/<name>`: a bare, a worktree, a config and an index entry.

    `bare` reuses a bare an earlier workspace already made; `entry` records the repo's
    default base in the config; `local_commit` adds a commit no remote has.
    """
    if bare is None:
        bare = make_bare(lab, repo, branches, base=base)
    root = lab.ws(name)
    root.mkdir(parents=True, exist_ok=True)
    add_worktree(lab, bare, root / repo, branch=branch, base=base)
    if local_commit:
        (root / repo / "local.txt").write_text("x\n")
        lab.git(root / repo, "add", "local.txt")
        lab.git(root / repo, "commit", "-m", "local work")
    declared_repos = {repo: f"origin/{base}"} if entry else {}
    write_workspace(root / ".ow" / "config.toml", declared_repos, {}, declared={})
    index_add(root)
    return root


def add_repo(
    lab: Lab,
    name: str = "community",
    branches: dict[str, dict[str, str | bytes]] | None = None,
    *,
    base: str = "18.0",
) -> Path:
    """The `name` remote, registered with `ow repo add`; returns the remote's path.

    The default branches are one `base` branch holding `lab.odoo_files()`.
    """
    if branches is None:
        branches = {base: lab.odoo_files()}
    remote = lab.remote(name, branches, head=base)
    assert lab.ow("repo", "add", name, file_url(remote)).exit_code == 0
    return remote


def init_workspace(
    lab: Lab,
    *names: str,
    root: Path | None = None,
    name: str = "voip",
    base: str = "18.0",
    branch: str | None = None,
) -> Path:
    """`ow init` a workspace of the already-registered `names`; returns its root.

    `root` defaults to `~/ws/<name>`.
    """
    root = lab.ws(name) if root is None else root
    args = ["init", str(root), *names, "--base", base]
    if branch is not None:
        args += ["--branch", branch]
    assert lab.ow(*args).exit_code == 0
    return root


def resolved_workspace(
    root: Path,
    repos: tuple[Repo, ...] = (),
    overrides: dict[str, object] | None = None,
) -> Workspace:
    """A workspace whose settings come from `resolve_settings` over empty layers."""
    declared = {setting.key: setting for setting in load_plugins().settings()}
    empty = Workspace(root=root, name=root.name, repos=repos, settings=MappingProxyType({}))
    values, _ = resolve_settings(
        declared,
        Layer(values={}, unknown={}, wrong_type=()),
        Layer(values=dict(overrides or {}), unknown={}, wrong_type=()),
        empty,
    )
    return Workspace(root=root, name=root.name, repos=repos, settings=MappingProxyType(values))


def files_of(result: list) -> list[GeneratedFile]:
    """The `GeneratedFile`s of a hook's results, in plugin order."""
    return [item for item in result if isinstance(item, GeneratedFile)]


def diagnostics_of(result: list) -> list:
    """Everything a hook returned that is not a `GeneratedFile`, in plugin order."""
    return [item for item in result if not isinstance(item, GeneratedFile)]
