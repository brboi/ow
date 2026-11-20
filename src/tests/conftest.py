"""Pytest configuration, fixtures, and the helpers the test modules share."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

import pytest
from pytest import MonkeyPatch




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


