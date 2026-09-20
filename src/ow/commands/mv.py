"""`ow mv` — move a workspace to a new path.

Moving a workspace is not `mv`: the bare repos still point at the old
worktree paths, the index still names the old directory, and `odoorc` holds
an absolute `data_dir` that only a refresh can regenerate. All three are
fixed here, in that order — the worktrees have to work again before the
addon scan that re-renders `odoorc` can see anything.
"""

import sys
from pathlib import Path

from ow.utils import index, paths
from ow.utils.config import Config, WorkspaceConfig
from ow.utils.display import confirm, display_path, err_console
from ow.utils.relocate import refresh_after_relocation, relocate_workspace, validate_target
from ow.utils.render import RenderResult
from ow.utils.resolver import resolve_workspace


def resolve_dest(ws_dir: Path, dest: str) -> Path:
    """Where the workspace lands, with mv(1) semantics.

    An existing directory means "move into it"; anything else is the new
    path itself. `ow mv parrot ~/odoo/parrot` renames, `ow mv ./parrot ..`
    moves into the parent.
    """
    d = Path(dest).expanduser()
    if d.is_dir():
        return (d / ws_dir.name).resolve()
    return d.resolve()


# Keep the old private name as an alias for any external callers.
_resolve_dest = resolve_dest


def _display_summary(
    ws_dir: Path, target: Path, aliases: list[str], repairable: list[str], *, schema1: bool,
) -> None:
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
    if schema1:
        print("Will not re-render: the workspace config is still schema 1 — run")
        print(f"                    `ow render -w {target}` afterwards")
    else:
        print("Will re-render: odoorc (absolute data_dir), and every other generated")
        print("                file of this workspace")

    if target.name != ws_dir.name:
        print()
        print(f"  ⚠ renaming changes db_name and dbfilter in odoorc to "
              f"'{target.name}' — the existing Odoo database is not renamed")
    if (ws_dir / ".venv").exists():
        print()
        print("  ⚠ .venv holds absolute paths — run `mise install` in the new location")


def execute_move(
    config: Config, ws_dir: Path, ws: WorkspaceConfig, target: Path,
) -> tuple[list[str], RenderResult | None]:
    """Relocate + reindex + refresh. Returns (aliases left unrepaired, the refresh outcome).

    `None` for the refresh outcome means the workspace is still schema 1:
    the manifest and lock at `target` are exactly what they were before the
    move. Relocation and the index are already done by the time refresh
    runs — a prerequisite or render failure leaves the workspace at
    `target`, never rolled back.
    """
    unrepaired = relocate_workspace(ws_dir, target, ws.repos)
    index.forget(ws_dir)
    index.remember(target)
    render_result = refresh_after_relocation(config, ws, target)
    return unrepaired, render_result


def cmd_mv(config: Config, source: str, dest: str, *, yes: bool = False) -> None:
    """Move a workspace to a new path, repairing its worktrees.

    `source` takes every form `resolve_workspace` accepts — a name, a path,
    `$OW_WORKSPACE`, or the workspace holding the current directory.
    """
    ws_dir, ws = resolve_workspace(name=source)
    target = resolve_dest(ws_dir, dest)

    err = validate_target(ws_dir, target)
    if err is not None:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)

    repos_dir = paths.repos_dir()
    aliases = list(ws.repos)
    repairable = [a for a in aliases if (repos_dir / f"{a}.git").exists()]
    schema1 = config.version == 1 or ws.version == 1

    _display_summary(ws_dir, target, aliases, repairable, schema1=schema1)

    if not yes and not confirm():
        print("Aborted.")
        sys.exit(2)

    sys.stdout.flush()

    unrepaired, render_result = execute_move(config, ws_dir, ws, target)

    failed = bool(unrepaired)
    for alias in unrepaired:
        err_console.print(
            f"  [{alias}] worktree not repaired — run `ow init` in {target}",
            markup=False,
        )

    if render_result is None:
        print(f"Schema 1 workspace: run `ow render -w {target}` to re-render odoorc and the rest.")
    else:
        for path in render_result.wrote:
            print(f"wrote {path}")
        for path in render_result.updated:
            print(f"updated {path}")
        for path in render_result.adopted:
            print(f"adopted {path}")
        if render_result.yours:
            print("yours, left alone: " + ", ".join(render_result.yours))
        for warning in render_result.warnings:
            err_console.print(warning, markup=False)
        if render_result.failed:
            for line in render_result.errors:
                err_console.print(f"Error: {line}", markup=False)
            failed = True

    if failed:
        sys.exit(1)

    print("Done.")
