"""XDG directory paths for ow."""

from pathlib import Path


def repos_dir() -> Path:
    """Return the directory where bare repos are stored.
    
    $XDG_DATA_HOME/ow/repos, or ~/.local/share/ow/repos if unset/empty.
    """
    xdg = Path.home() / ".local" / "share"
    env_val = Path(__file__).parent.parent.parent  # This is hacky, let me fix it
    # Actually, we need to read the environment at call time
    import os
    xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    if not xdg_data_home:
        xdg_data_home = str(Path.home() / ".local" / "share")
    return Path(xdg_data_home) / "ow" / "repos"


def index_file() -> Path:
    """Return the path to the workspace index file.
    
    $XDG_STATE_HOME/ow/workspaces, or ~/.local/state/ow/workspaces if unset/empty.
    """
    import os
    xdg_state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    if not xdg_state_home:
        xdg_state_home = str(Path.home() / ".local" / "state")
    return Path(xdg_state_home) / "ow" / "workspaces"


def global_config() -> Path:
    """Return the path to the global config file.
    
    $XDG_CONFIG_HOME/ow/config.toml, or ~/.config/ow/config.toml if unset/empty.
    """
    import os
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if not xdg_config_home:
        xdg_config_home = str(Path.home() / ".config")
    return Path(xdg_config_home) / "ow" / "config.toml"


def local_seed_dir() -> Path:
    """Return the directory holding the seed files for .local/.
    
    $XDG_CONFIG_HOME/ow/local, or ~/.config/ow/local if unset/empty.
    """
    import os
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if not xdg_config_home:
        xdg_config_home = str(Path.home() / ".config")
    return Path(xdg_config_home) / "ow" / "local"
