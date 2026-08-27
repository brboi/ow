"""Where ow keeps things, per the XDG Base Directory specification.

These are functions rather than constants because tests redirect them by
setting XDG_* — which exercises the real resolution instead of bypassing it.
None of them create anything; callers make directories when they write.
"""

import os
from pathlib import Path

_APP = "ow"


def _base(var: str, default: str) -> Path:
    # The spec says a value that is not an absolute path must be ignored,
    # and that an unset variable and an empty one are the same thing. Both
    # rules exist for one reason: anything relative would make ow's
    # locations depend on the directory the command was run from. "~" is a
    # shell nicety, not a path — nothing expands it once it is in the
    # environment, so it is relative too.
    value = os.environ.get(var)
    if not value or not Path(value).is_absolute():
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


def archives_dir() -> Path:
    # data, not state: an archive holds a whole workspace, worktrees
    # included, not a disposable cache entry.
    return data_home() / "archives"


def backups_dir() -> Path:
    # state, not data: one small TOML per removal, regenerable from the
    # workspace it came from and safe to lose.
    return state_home() / "backups"


def index_file() -> Path:
    return state_home() / "workspaces"


def template_base_dir() -> Path:
    return state_home() / "template-base"
