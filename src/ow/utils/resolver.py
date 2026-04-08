import os
import sys
from pathlib import Path

from ow.utils.config import Config, WorkspaceConfig, load_workspace_config


def _find_ow_config(start: Path) -> Path | None:
    """Walk up from start looking for .ow/config."""
    for parent in [start] + list(start.parents):
        candidate = parent / ".ow" / "config"
        if candidate.exists():
            return candidate
    return None


def resolve_workspace(config: Config, name: str | None = None) -> tuple[Path, WorkspaceConfig]:
    """Resolve workspace from name, env var, or cwd walk-up.

    Returns (workspace_dir_path, WorkspaceConfig).
    """
    config_file = None
    if name is not None:
        ws_dir = config.root_dir / "workspaces" / name
        if not ws_dir.exists():
            print(f"Workspace '{name}' not found", file=sys.stderr)
            sys.exit(1)
        config_file = ws_dir / ".ow" / "config"
        if not config_file.exists():
            print(f"Workspace '{name}' is not a valid workspace (missing .ow/config)", file=sys.stderr)
            sys.exit(1)
    elif os.environ.get("OW_WORKSPACE"):
        env_val = os.environ["OW_WORKSPACE"]
        ws_dir = config.root_dir / "workspaces" / env_val
        if (ws_dir / ".ow" / "config").exists():
            config_file = ws_dir / ".ow" / "config"
        else:
            config_file = Path(env_val) / ".ow" / "config"
    else:
        config_file = _find_ow_config(Path.cwd())

    if not config_file or not config_file.exists():
        print("No workspace found. Run from a workspace or pass a path.", file=sys.stderr)
        sys.exit(1)

    ws_dir = config_file.parent.parent.resolve()
    return ws_dir, load_workspace_config(config_file)
