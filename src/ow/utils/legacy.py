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

# Named by URL, not by repo path: most people meeting this message installed ow
# from PyPI and have no checkout to open docs/ in.
_GUIDE = (
    "see https://github.com/brboi/ow/blob/main/docs/migrating-to-2.0.md to migrate"
)

# Shared with the resolver so the same hint is given from every entry point.
HINT_RENAME = "expected .ow/config.toml instead: mv .ow/config .ow/config.toml"


def check_legacy_layout(*, fatal: bool = True) -> None:
    """Detect the pre-2.0 layout and point at the migration guide.

    Must run before load_global_config(): that function bootstraps a
    default config.toml on first use, which would erase the "no global
    config yet" condition the first form below depends on before this
    check ever ran.

    fatal=False reports and returns instead of exiting. It is for `ow ls`
    alone: ls reads only the discovery index — not the global config, not
    the cwd — and writes nothing, so there is nothing here to protect it
    from, and stopping it would take away the one command that shows a
    migrating user what ow has picked up so far, from inside the very
    workspaces they are migrating.
    """
    prefix = "Error:" if fatal else "Warning:"
    if not paths.config_file().exists():
        old_root = find_project_root(Path.cwd())
        if old_root is not None:
            marker = "ow.toml" if (old_root / "ow.toml").exists() else "ow.toml.example"
            err_console.print(
                f"{prefix} found an old project layout at {old_root} ({marker})", markup=False
            )
            err_console.print(f"       {marker} is no longer used — {_GUIDE}")
            if fatal:
                raise typer.Exit(1)
            return

    current = Path.cwd().resolve()
    for candidate in [current, *current.parents]:
        old_config = candidate / ".ow" / "config"
        if old_config.exists() and not (candidate / ".ow" / "config.toml").exists():
            err_console.print(
                f"{prefix} found an old workspace config at {old_config}", markup=False
            )
            err_console.print(f"       {HINT_RENAME}")
            err_console.print(f"       {_GUIDE}")
            if fatal:
                raise typer.Exit(1)
            return
