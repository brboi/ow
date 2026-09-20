"""`ow render` — repair a workspace's generated files, in place.

Re-runnable: a workspace already fully rendered gets a quiet no-op. A
still-schema-1 workspace is migrated explicitly here (the one thing this
command is allowed to convert), then the fixed generator's proposal is
written and its mise fragment trusted. Nothing here creates a missing
repo or seeds `.local` — that is `ow init`'s job, since only `ow init`
knows whether the caller wants a repair or a fresh workspace.
"""

import sys
from pathlib import Path

from ow.utils import index
from ow.utils.config import Config, load_global_config, load_workspace_config
from ow.utils.display import console, err_console
from ow.utils.migration import commit_migration, plan_migration
from ow.utils.resolver import resolve_workspace
from ow.utils.workspace import refresh_workspace, require_mise

MARKER = Path(".ow") / "config.toml"

def cmd_render(config: Config, workspace: str | None = None) -> None:
    """Migrate the config if needed, then write every generated file."""
    ws_dir, ws = resolve_workspace(name=workspace)

    try:
        require_mise()
    except ValueError as exc:
        err_console.print(f"Error: {exc}", markup=False)
        sys.exit(1)

    if config.version == 1 or ws.version == 1:
        migration_plan = plan_migration(config, ws, ws_dir)
        if migration_plan.errors:
            for line in migration_plan.errors:
                err_console.print(f"Error: {line}", markup=False)
            sys.exit(1)
        commit_migration(migration_plan)
        for line in migration_plan.warnings:
            err_console.print(line, markup=False)
        if config.version == 1:
            config = load_global_config()
        if ws.version == 1:
            ws = load_workspace_config(ws_dir / MARKER)

    result = refresh_workspace(config, ws, ws_dir, trust=True)

    for path in result.wrote:
        console.print(f"wrote {path}")
    for path in result.updated:
        console.print(f"updated {path}")
    for path in result.adopted:
        console.print(f"adopted {path}")
    if result.yours:
        console.print("yours, left alone: " + ", ".join(result.yours))
        console.print("run `ow files --diff` to see what ow would write instead.")
    if result.skipped:
        console.print("not rendered: " + ", ".join(result.skipped))
    for warning in result.warnings:
        err_console.print(warning, markup=False)

    if result.failed:
        for line in result.errors:
            err_console.print(f"Error: {line}", markup=False)
        console.print(f"\nWorkspace '{ws_dir.name}' not fully rendered.")
        sys.exit(1)

    index.remember(ws_dir)
    console.print(f"\nWorkspace '{ws_dir.name}' rendered.")
