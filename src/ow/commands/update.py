from ow.utils.resolver import resolve_workspace
from ow.utils.templates import apply_templates, ensure_workspace_materialized
from ow.utils.config import Config, write_workspace_config
from ow.utils.git import run_cmd


def cmd_update(config: Config, workspace: str | None = None) -> None:
    """Re-render templates and materialize worktrees for the current workspace."""
    ws_dir, ws = resolve_workspace(config, name=workspace)
    _, successful, errors = ensure_workspace_materialized(ws, config, ws_dir)
    apply_templates(ws, config, ws_dir)

    if errors:
        print(f"\nWarning: repo(s) failed to set up:", file=sys.stderr)
        for alias, err in errors.items():
            print(f"  {alias}: {err}", file=sys.stderr)

    missing_vars = {k: v for k, v in config.vars.items() if k not in ws.vars}
    if missing_vars:
        ws.vars = {**ws.vars, **missing_vars}
        ow_config_path = ws_dir / ".ow" / "config"
        write_workspace_config(ow_config_path, ws)

    mise_toml = ws_dir / "mise.toml"
    if mise_toml.exists():
        run_cmd(["mise", "trust", str(mise_toml)], check=True)

    print(f"\nWorkspace '{ws_dir.name}' updated.")
