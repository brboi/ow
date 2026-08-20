import sys
from dataclasses import replace

from ow.utils.config import Config, WorkspaceConfig, select_aliases, write_workspace_config
from ow.utils.git import run_cmd
from ow.utils.resolver import resolve_workspace
from ow.utils.templates import apply_templates, ensure_workspace_materialized


def cmd_apply(config: Config, workspace: str | None = None, *, only: str | None = None) -> None:
    """Make the tree match .ow/config.toml: materialize worktrees, render templates."""
    ws_dir, ws = resolve_workspace(name=workspace)

    # --only narrows the git work, never the rendering. A template sees the
    # whole workspace by construction — odoo.conf's addons_path is built from
    # every repo — so rendering a narrowed config would quietly drop the
    # addons of the repos left out.
    aliases = select_aliases(list(ws.repos), only)
    materializing: WorkspaceConfig = replace(
        ws, repos={alias: ws.repos[alias] for alias in aliases}
    )

    _, successful, errors = ensure_workspace_materialized(materializing, config, ws_dir)
    apply_templates(ws, config, ws_dir)

    if errors:
        print("\nWarning: repo(s) failed to set up:", file=sys.stderr)
        for alias, err in errors.items():
            print(f"  {alias}: {err}", file=sys.stderr)

    missing_vars = {k: v for k, v in config.vars.items() if k not in ws.vars}
    if missing_vars:
        ws.vars = {**ws.vars, **missing_vars}
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)

    mise_toml = ws_dir / "mise.toml"
    if mise_toml.exists():
        run_cmd(["mise", "trust", str(mise_toml)], check=True)

    print(f"\nWorkspace '{ws_dir.name}' applied.")
