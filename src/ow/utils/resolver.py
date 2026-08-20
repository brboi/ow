"""Locating the workspace a command should act on.

Configuration is global, so there is no longer a project that owns a set of
workspaces. What is left is a single question — which directory? — answered
by exactly one of four forms, each with its own failure. None of them falls
back to another: a bare name that the index does not know is an error, not
an invitation to try the same string as a relative path.
"""

import os
import sys
from pathlib import Path
from typing import NoReturn

from ow.utils import index
from ow.utils.config import Config, WorkspaceConfig, load_workspace_config

MARKER = Path(".ow") / "config.toml"


def _fail(*lines: str) -> NoReturn:
    for line in lines:
        print(line, file=sys.stderr)
    sys.exit(1)


def _looks_like_path(value: str) -> bool:
    """Tell `ow status quattromori` (a name) from `ow status ./quattromori`."""
    seps = [os.sep] + ([os.altsep] if os.altsep else [])
    return value.startswith("~") or value in (".", "..") or any(s in value for s in seps)


def _load(ws_dir: Path) -> tuple[Path, WorkspaceConfig]:
    """Read a directory known to be a workspace, and remember it."""
    ws = load_workspace_config(ws_dir / MARKER)
    index.remember(ws_dir)
    return ws_dir, ws


def _from_path(value: str) -> tuple[Path, WorkspaceConfig]:
    ws_dir = Path(value).expanduser().resolve()
    if not (ws_dir / MARKER).exists():
        _fail(
            f"Error: {ws_dir} is not a workspace",
            f"       missing {ws_dir / MARKER}",
        )
    return _load(ws_dir)


def _from_name(name: str) -> tuple[Path, WorkspaceConfig]:
    matches = index.find_by_name(name)
    if not matches:
        _fail(
            f"Error: no workspace named {name!r}",
            "       run `ow ls` to see the workspaces ow knows about,",
            f"       or pass a path, e.g. `./{name}`",
        )
    if len(matches) > 1:
        _fail(
            f"Error: {name!r} matches {len(matches)} workspaces:",
            *(f"         {candidate}" for candidate in matches),
            "       pass the path of the one you mean",
        )
    return _load(matches[0])


def _from_env(value: str) -> tuple[Path, WorkspaceConfig]:
    # One form only. Accepting a name here would resurrect the guess this
    # rewrite removes, and accepting "~" or a relative path would make the
    # variable's meaning depend on the shell that exported it and on cwd.
    if not Path(value).is_absolute():
        _fail(
            f"Error: OW_WORKSPACE={value!r} is not an absolute path",
            "       OW_WORKSPACE takes one form: an absolute path to a workspace",
        )
    return _from_path(value)


def _from_cwd() -> tuple[Path, WorkspaceConfig]:
    start = Path.cwd().resolve()
    for candidate in [start, *start.parents]:
        if (candidate / MARKER).exists():
            return _load(candidate)
    _fail(
        "Error: no workspace found here",
        "       run from inside a workspace, pass a path, or see `ow ls`",
    )


def resolve_workspace(config: Config, name: str | None = None) -> tuple[Path, WorkspaceConfig]:
    """Locate a workspace. One rule, four branches, no fallbacks between them.

    `config` is global and no longer derived from the workspace; it stays in
    the signature so callers keep one entry point for "which workspace am I
    acting on".
    """
    if name is not None:
        return _from_path(name) if _looks_like_path(name) else _from_name(name)

    env_val = os.environ.get("OW_WORKSPACE")
    if env_val:
        return _from_env(env_val)

    return _from_cwd()
