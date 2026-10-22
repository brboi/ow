"""XDG directory paths for ow."""

import os
from pathlib import Path


def _base(var: str, default: str) -> str:
    """The XDG base directory: the environment variable when it is a non-empty absolute
    path, else `default`."""
    value = os.environ.get(var, "").strip()
    if value and Path(value).is_absolute():
        return value
    return default


def repos_dir() -> Path:
    """Return the directory where bare repos are stored.
    
    $XDG_DATA_HOME/ow/repos, or ~/.local/share/ow/repos if unset/empty.
    """
    base = _base("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    return Path(base) / "ow" / "repos"


def index_file() -> Path:
    """Return the path to the workspace index file.
    
    $XDG_STATE_HOME/ow/workspaces, or ~/.local/state/ow/workspaces if unset/empty.
    """
    base = _base("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
    return Path(base) / "ow" / "workspaces"


def global_config() -> Path:
    """Return the path to the global config file.
    
    $XDG_CONFIG_HOME/ow/config.toml, or ~/.config/ow/config.toml if unset/empty.
    """
    base = _base("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "ow" / "config.toml"


def local_seed_dir() -> Path:
    """Return the directory holding the seed files for .local/.
    
    $XDG_CONFIG_HOME/ow/local, or ~/.config/ow/local if unset/empty.
    """
    base = _base("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "ow" / "local"
