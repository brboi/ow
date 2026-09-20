"""`ow archive` / `ow unarchive` — park a workspace, then bring it back.

Archiving is relocation to a canonical place plus dropping the index entry.
The worktrees stay registered — repaired at the archive path — and the local
branches stay, which is the whole point: an archived workspace comes back
exactly as it left. Unarchiving is the same move in reverse, plus a refresh
for a schema-2 workspace so the absolute `data_dir` in `odoorc` names
wherever it landed. A schema-1 archive comes back exactly as it left,
unmigrated too — restoring it is never the thing that converts it; only
`ow render` does that.

One subject, two directions, one module.
"""

import sys
import tomllib
from pathlib import Path

from ow.utils import index, paths
from ow.utils.config import Config, WorkspaceConfig, load_workspace_config
from ow.utils.display import confirm, display_path, err_console
from ow.utils.relocate import refresh_after_relocation, relocate_workspace, validate_target
from ow.utils.render import RenderResult

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


def _report_unrepaired(unrepaired: list[str], target: Path) -> None:
    for alias in unrepaired:
        err_console.print(
            f"  [{alias}] worktree not repaired — run `ow init` in {target}",
            markup=False,
        )


def execute_archive(ws_dir: Path, ws: WorkspaceConfig, target: Path) -> list[str]:
    """Relocate to archive + drop index entry. Returns aliases left unrepaired."""
    unrepaired = relocate_workspace(ws_dir, target, ws.repos)
    index.forget(ws_dir)
    return unrepaired


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

    unrepaired = execute_archive(ws_dir, ws, target)

    # No refresh: an archive is not meant to be used in place.
    if unrepaired:
        _report_unrepaired(unrepaired, target)
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


def execute_unarchive(
    config: Config, source: Path, ws: WorkspaceConfig, target: Path,
) -> tuple[list[str], RenderResult | None]:
    """Relocate from archive + reindex + refresh. Returns (aliases left unrepaired, the refresh outcome).

    `None` means the archived workspace is still schema 1: restoring it
    must never be the thing that migrates it, so unarchiving a legacy
    archive stays legacy — only `ow render` converts it.
    """
    unrepaired = relocate_workspace(source, target, ws.repos)
    index.remember(target)
    render_result = refresh_after_relocation(config, ws, target)
    return unrepaired, render_result


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
    if config.version == 1 or ws.version == 1:
        print("Will not re-render: the workspace config is still schema 1 — run")
        print(f"                    `ow render -w {target}` afterwards")
    else:
        print("Will re-render: odoorc (absolute data_dir), and every other generated")
        print("                file of this workspace")

    if not yes and not confirm():
        print("Aborted.")
        sys.exit(2)

    sys.stdout.flush()

    unrepaired, render_result = execute_unarchive(config, source, ws, target)

    failed = bool(unrepaired)
    if unrepaired:
        _report_unrepaired(unrepaired, target)

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
