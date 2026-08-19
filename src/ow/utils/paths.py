"""Where ow keeps things, per the XDG Base Directory specification.

These are functions rather than constants because tests redirect them by
setting XDG_* — which exercises the real resolution instead of bypassing it.
None of them create anything; callers make directories when they write.
"""

import os
from pathlib import Path

_APP = "ow"


def _base(var: str, default: str) -> Path:
    # XDG treats an unset variable and an empty one identically; an empty one
    # would otherwise yield a relative path.
    value = os.environ.get(var)
    if not value:
        return Path.home() / default
    return Path(value)


def config_home() -> Path:
    return _base("XDG_CONFIG_HOME", ".config") / _APP


def data_home() -> Path:
    return _base("XDG_DATA_HOME", ".local/share") / _APP


def state_home() -> Path:
    return _base("XDG_STATE_HOME", ".local/state") / _APP


def config_file() -> Path:
    return config_home() / "config.toml"


def templates_dir() -> Path:
    return config_home() / "templates"


def services_dir() -> Path:
    return config_home() / "services"


def repos_dir() -> Path:
    return data_home() / "repos"


def volumes_dir() -> Path:
    return data_home() / "volumes"


def index_file() -> Path:
    return state_home() / "workspaces"


def template_base_dir() -> Path:
    return state_home() / "template-base"
