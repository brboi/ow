"""Detecting the pre-2.0 project layout.

ow used to be project-scoped: an `ow.toml` at a project root, and a
per-workspace config file named `.ow/config` with no extension. Now
configuration is global and the per-workspace file is `.ow/config.toml`.
Someone upgrading has none of the new layout — this points them at the
migration guide instead of leaving them reading "no workspace found" while
their workspaces sit right there.
"""

from pathlib import Path

import typer

from ow.utils import paths
from ow.utils.config import find_project_root
from ow.utils.display import err_console

_GUIDE = "see docs/migrating-to-2.0.md to migrate"


def check_legacy_layout() -> None:
    """Detect the pre-2.0 layout and point at the migration guide.

    Must run before load_global_config(): that function bootstraps a
    default config.toml on first use, which would erase the "no global
    config yet" condition the first form below depends on before this
    check ever ran.
    """
    if not paths.config_file().exists():
        old_root = find_project_root(Path.cwd())
        if old_root is not None:
            marker = "ow.toml" if (old_root / "ow.toml").exists() else "ow.toml.example"
            err_console.print(f"Error: found an old project layout at {old_root} ({marker})")
            err_console.print(f"       {marker} is no longer used — {_GUIDE}")
            raise typer.Exit(1)

    current = Path.cwd().resolve()
    for candidate in [current, *current.parents]:
        old_config = candidate / ".ow" / "config"
        if old_config.exists() and not (candidate / ".ow" / "config.toml").exists():
            err_console.print(f"Error: found an old workspace config at {old_config}")
            err_console.print(f"       expected .ow/config.toml instead — {_GUIDE}")
            raise typer.Exit(1)
