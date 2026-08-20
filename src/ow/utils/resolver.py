import os
import sys
from pathlib import Path

from ow.utils.config import (
    Config,
    WorkspaceConfig,
    load_workspace_config,
)


def _find_ow_config(start: Path) -> Path | None:
    """Walk up from start looking for .ow/config.toml."""
    for parent in [start] + list(start.parents):
        candidate = parent / ".ow" / "config.toml"
        if candidate.exists():
            return candidate
    return None


def _fail(*lines: str) -> None:
    for line in lines:
        print(line, file=sys.stderr)
    sys.exit(1)


def _looks_like_path(value: str) -> bool:
    """Tell `OW_WORKSPACE=quattromori` (a name) from `OW_WORKSPACE=~/odoo/w/q` (a path)."""
    seps = [os.sep] + ([os.altsep] if os.altsep else [])
    return value.startswith("~") or value in (".", "..") or any(s in value for s in seps)


def _by_name(config: Config, name: str) -> tuple[Path, WorkspaceConfig]:
    """Positional `ow status <name>` — resolving a workspace by name.

    Provisional: without the discovery index (task 5) there is nowhere to
    look a name up. A loud, honest failure beats guessing a directory from
    an attribute Config no longer has.
    """
    _fail(
        f"Error: cannot resolve workspace {name!r} by name yet",
        "       pass a path instead, e.g. `ow status ./path/to/workspace`",
    )


def resolve_workspace(
    config: Config, name: str | None = None
) -> tuple[Config, Path, WorkspaceConfig]:
    """Resolve the workspace.

    Configuration is global now, so there is only ever one `config` — no more
    "the project owning this workspace" to look up.

    Each form has exactly one meaning and one failure; none falls back to
    another:
      - `name`                  -> not resolvable yet (needs task 5's index)
      - OW_WORKSPACE=<name>     -> not resolvable yet (needs task 5's index)
      - OW_WORKSPACE=<path>     -> that path
      - neither                 -> walk up from cwd for .ow/config.toml
    """
    if name is not None:
        return (config, *_by_name(config, name))

    env_val = os.environ.get("OW_WORKSPACE")
    if env_val:
        if _looks_like_path(env_val):
            ws_dir = Path(env_val).expanduser().resolve()
            config_file = ws_dir / ".ow" / "config.toml"
            if not config_file.exists():
                _fail(
                    f"Error: OW_WORKSPACE={env_val!r} is not a workspace",
                    f"       missing {config_file}",
                )
            return config, ws_dir, load_workspace_config(config_file)

        _fail(
            f"Error: cannot resolve OW_WORKSPACE={env_val!r} by name yet",
            "       pass an absolute path instead",
        )

    config_file = _find_ow_config(Path.cwd())
    if config_file is None:
        _fail("No workspace found. Run from a workspace or pass a path.")
    ws_dir = config_file.parent.parent.resolve()
    return config, ws_dir, load_workspace_config(config_file)
