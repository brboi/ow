"""`ow archive` / `ow unarchive` — park a workspace, then bring it back.

Archiving is relocation to a canonical place plus dropping the index entry.
The worktrees stay registered — repaired at the archive path — and the local
branches stay, which is the whole point: an archived workspace comes back
exactly as it left. Unarchiving is the same move in reverse, plus a re-render
so the absolute paths in `odoorc` name wherever it landed.

One subject, two directions, one module.
"""

import sys
import tomllib
from pathlib import Path

from ow.utils import index, paths
from ow.utils.config import Config, WorkspaceConfig, load_workspace_config
from ow.utils.display import confirm, display_path, err_console
from ow.utils.relocate import relocate_workspace, validate_target
from ow.utils.templates import apply_templates

MARKER = Path(".ow") / "config.toml"


def _resolve_by_name(name: str) -> tuple[Path, WorkspaceConfig]:
    """Find an active workspace by name, the way `ow rm` does."""
    matches = index.find_by_name(name)
    if not matches:
        print(
            f"No workspace named '{name}' found. "
            "Run `ow ls` to see known workspaces.",
            file=sys.stderr,
        )
        sys.exit(1)
    if len(matches) > 1:
        print(f"Multiple workspaces named '{name}':", file=sys.stderr)
        for m in matches:
            print(f"  {m}", file=sys.stderr)
        print("Remove or move one of them first.", file=sys.stderr)
        sys.exit(1)

    ws_dir = matches[0]
    try:
        return ws_dir, load_workspace_config(ws_dir / MARKER)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        print(f"Could not read workspace config: {exc}", file=sys.stderr)
        sys.exit(1)


def _report_unrepaired(unrepaired: list[str]) -> None:
    for alias in unrepaired:
        err_console.print(
            f"  [{alias}] worktree not repaired — run `ow apply`",
            markup=False,
        )


def cmd_archive(name: str, *, yes: bool = False) -> None:
    """Move a workspace into the archive, keeping its worktrees and branches."""
    ws_dir, ws = _resolve_by_name(name)
    # Created up front, not at move time: the archive root is ow's own, and
    # validate_target rightly refuses a target whose parent is missing.
    try:
        paths.archives_dir().mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"Error: could not create {paths.archives_dir()}: {exc}", file=sys.stderr)
        sys.exit(1)
    target = paths.archives_dir() / name

    err = validate_target(ws_dir, target)
    if err is not None:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)

    print(f"Archiving workspace '{name}'")
    print(f"  from {display_path(ws_dir)}")
    print(f"  to   {display_path(target)}")
    print()
    print("Worktrees and local branches are kept.")
    print(f"Restore with `ow unarchive {name}`.")

    if not yes and not confirm():
        print("Aborted.")
        sys.exit(2)

    sys.stdout.flush()

    unrepaired = relocate_workspace(ws_dir, target, ws.repos)
    index.forget(ws_dir)

    # No apply_templates: an archive is not meant to be used in place.
    if unrepaired:
        _report_unrepaired(unrepaired)
        sys.exit(1)

    print("Done.")


def _resolve_unarchive_dest(name: str, dest: str | None) -> Path:
    """Where an archive lands, with the same mv(1) semantics as `ow mv`."""
    if dest is None:
        return Path.cwd() / name
    d = Path(dest).expanduser()
    if d.is_dir():
        return (d / name).resolve()
    return d.resolve()


def cmd_unarchive(
    config: Config, name: str, dest: str | None = None, *, yes: bool = False,
) -> None:
    """Restore an archived workspace."""
    source = paths.archives_dir() / name
    if not (source / MARKER).exists():
        print(
            f"Error: no archived workspace named '{name}'. "
            "Run `ow ls --archived` to list them.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        ws = load_workspace_config(source / MARKER)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        print(f"Could not read workspace config: {exc}", file=sys.stderr)
        sys.exit(1)

    target = _resolve_unarchive_dest(name, dest)
    err = validate_target(source, target)
    if err is not None:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)

    print(f"Restoring workspace '{name}'")
    print(f"  from {display_path(source)}")
    print(f"  to   {display_path(target)}")
    print()
    print("Will re-render: odoorc (absolute addons_path and data_dir), and every")
    print("                other template file of this workspace")

    if not yes and not confirm():
        print("Aborted.")
        sys.exit(2)

    sys.stdout.flush()

    unrepaired = relocate_workspace(source, target, ws.repos)
    index.remember(target)
    apply_templates(ws, config, target)

    if unrepaired:
        _report_unrepaired(unrepaired)
        sys.exit(1)

    print("Done.")


def archived_workspaces() -> list[Path]:
    """Every directory in the archive that still looks like a workspace.

    Also used by shell completion, so a missing archive directory is an
    empty list, never an error.
    """
    root = paths.archives_dir()
    if not root.is_dir():
        return []
    try:
        return sorted(d for d in root.iterdir() if (d / MARKER).exists())
    except OSError:
        return []
