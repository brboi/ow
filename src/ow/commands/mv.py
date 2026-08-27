"""`ow mv` — move a workspace to a new path.

Moving a workspace is not `mv`: the bare repos still point at the old
worktree paths, the index still names the old directory, and `odoorc` holds
absolute paths that only `ow apply` can regenerate. All three are fixed here,
in that order — the worktrees have to work again before the addon scan that
re-renders `odoorc` can see anything.
"""

import sys
from pathlib import Path

from ow.utils import index, paths
from ow.utils.config import Config
from ow.utils.display import confirm, display_path, err_console
from ow.utils.relocate import relocate_workspace, validate_target
from ow.utils.resolver import resolve_workspace
from ow.utils.templates import apply_templates


def _resolve_dest(ws_dir: Path, dest: str) -> Path:
    """Where the workspace lands, with mv(1) semantics.

    An existing directory means "move into it"; anything else is the new
    path itself. `ow mv parrot ~/odoo/parrot` renames, `ow mv ./parrot ..`
    moves into the parent.
    """
    d = Path(dest).expanduser()
    if d.is_dir():
        return (d / ws_dir.name).resolve()
    return d.resolve()


def _display_summary(ws_dir: Path, target: Path, aliases: list[str], repairable: list[str]) -> None:
    """The whole report, in the imperative: nothing here has happened yet."""
    print(f"Moving workspace '{ws_dir.name}'")
    print(f"  from {display_path(ws_dir)}")
    print(f"  to   {display_path(target)}")
    print()
    print("Will repair:")
    for alias in aliases:
        if alias in repairable:
            print(f"  [{alias}] worktree registration")
        else:
            print(f"  [{alias}] bare repo missing — worktree will not be repaired")
    print()
    print("Will re-render: odoorc (absolute addons_path and data_dir), and every")
    print("                other template file of this workspace")

    if target.name != ws_dir.name:
        print()
        print(f"  ⚠ renaming changes db_name and dbfilter in odoorc to "
              f"'{target.name}' — the existing Odoo database is not renamed")
    if (ws_dir / ".venv").exists():
        print()
        print("  ⚠ .venv holds absolute paths — run `mise install` in the new location")


def cmd_mv(config: Config, source: str, dest: str, *, yes: bool = False) -> None:
    """Move a workspace to a new path, repairing its worktrees.

    `source` takes every form `resolve_workspace` accepts — a name, a path,
    `$OW_WORKSPACE`, or the workspace holding the current directory.
    """
    ws_dir, ws = resolve_workspace(name=source)
    target = _resolve_dest(ws_dir, dest)

    err = validate_target(ws_dir, target)
    if err is not None:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)

    repos_dir = paths.repos_dir()
    aliases = list(ws.repos)
    repairable = [a for a in aliases if (repos_dir / f"{a}.git").exists()]

    _display_summary(ws_dir, target, aliases, repairable)

    if not yes and not confirm():
        print("Aborted.")
        sys.exit(2)

    sys.stdout.flush()

    unrepaired = relocate_workspace(ws_dir, target, aliases)

    index.forget(ws_dir)
    index.remember(target)

    apply_templates(ws, config, target)

    if unrepaired:
        for alias in unrepaired:
            err_console.print(
                f"  [{alias}] worktree not repaired — run `ow apply`",
                markup=False,
            )
        sys.exit(1)

    print("Done.")
