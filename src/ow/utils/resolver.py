import os
import sys
from pathlib import Path

from ow.utils.config import (
    Config,
    WorkspaceConfig,
    find_project_root,
    load_config,
    load_workspace_config,
)
from ow.utils.display import err_console


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


def _project_for(ws_dir: Path, config: Config, source: str) -> Config:
    """Return the config of the project owning ws_dir.

    A workspace inside the current project is already covered by `config`.
    Only when it lies outside do we go looking for its own ow.toml — mixing
    one project's remotes and bare repos with another's workspace silently
    produces nonsense.
    """
    if ws_dir.is_relative_to(config.root_dir.resolve()):
        return config

    root = find_project_root(ws_dir)
    toml_path = root / "ow.toml" if root else None
    if toml_path is None or not toml_path.exists():
        _fail(
            f"Error: no ow.toml above {ws_dir}",
            "       a workspace must live inside an ow project",
        )
    err_console.print(f"Using project {root} (from {source})")
    return load_config(toml_path)


def _by_name(config: Config, name: str) -> tuple[Path, WorkspaceConfig]:
    """Positional `ow status <name>` — always relative to the current project."""
    ws_dir = config.root_dir / "workspaces" / name
    if not ws_dir.exists():
        _fail(f"Workspace '{name}' not found")
    config_file = ws_dir / ".ow" / "config.toml"
    if not config_file.exists():
        _fail(f"Workspace '{name}' is not a valid workspace (missing .ow/config.toml)")
    return ws_dir.resolve(), load_workspace_config(config_file)


def resolve_workspace(
    config: Config, name: str | None = None
) -> tuple[Config, Path, WorkspaceConfig]:
    """Resolve the workspace *and* the project that owns it.

    Returns (project_config, workspace_dir, workspace_config). The config
    returned is the one passed in, unless the workspace lives in another
    project — then it is that project's, so remotes and bare repos come from
    the same place as the workspace.

    Each form has exactly one meaning and one failure; none falls back to
    another:
      - `name`                  -> <current project>/workspaces/<name>
      - OW_WORKSPACE=<name>     -> <current project>/workspaces/<name>
      - OW_WORKSPACE=<path>     -> that path, project found by walking up
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
            return (
                _project_for(ws_dir, config, "OW_WORKSPACE"),
                ws_dir,
                load_workspace_config(config_file),
            )

        ws_dir = (config.root_dir / "workspaces" / env_val).resolve()
        config_file = ws_dir / ".ow" / "config.toml"
        if not config_file.exists():
            _fail(
                f"Error: OW_WORKSPACE={env_val!r} not found",
                f"       looked in {config.root_dir / 'workspaces'}",
            )
        return config, ws_dir, load_workspace_config(config_file)

    config_file = _find_ow_config(Path.cwd())
    if config_file is None:
        _fail("No workspace found. Run from a workspace or pass a path.")
    ws_dir = config_file.parent.parent.resolve()
    return (
        _project_for(ws_dir, config, "workspace path"),
        ws_dir,
        load_workspace_config(config_file),
    )
