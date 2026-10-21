"""Pytest configuration and fixtures for ow tests."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Generator

import pytest


@pytest.fixture(autouse=True)
def hermetic(tmp_path: Path, monkeypatch) -> Generator[None, None, None]:
    """Hermetic fixture: isolate git environment for testing."""
    # Create HOME directory
    home = tmp_path / "home"
    home.mkdir()
    
    # Create XDG directories under HOME
    xdg_config = home / ".config"
    xdg_config.mkdir(parents=True)
    
    xdg_data = home / ".local" / "share"
    xdg_data.mkdir(parents=True)
    
    xdg_state = home / ".local" / "state"
    xdg_state.mkdir(parents=True)
    
    xdg_cache = home / ".cache"
    xdg_cache.mkdir(parents=True)
    
    # Create git config file outside HOME (in tmp root)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        "[init]\n"
        "    defaultBranch = master\n"
        "[protocol \"file\"]\n"
        "    allow = always\n"
    )
    
    # Create PATH directory with symlinks to real commands
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    
    for cmd in ("git", "sh", "bash"):
        real_path = shutil.which(cmd)
        if real_path:
            link_path = bin_dir / cmd
            link_path.symlink_to(real_path)
    
    # Set environment variables
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
    monkeypatch.setenv("PATH", str(bin_dir))
    
    # Unset inherited git/mise variables
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)
    for var in [k for k in os.environ if k.startswith("MISE_")]:
        monkeypatch.delenv(var, raising=False)
    
    # Change to home directory
    monkeypatch.chdir(home)
    
    yield


@pytest.fixture
def lab(tmp_path: Path, monkeypatch) -> Generator:
    """Lab fixture for creating test repositories and workspaces."""
    # Import here to avoid circular dependency
    from tests.lab import Lab
    
    return Lab(tmp_path, monkeypatch)
