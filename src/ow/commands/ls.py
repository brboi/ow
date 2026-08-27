"""`ow ls` — list every known workspace, its path, and its repos.

Reads the index and the workspace configs it points at. No git: that is
what `ow status` is for, and a listing that takes ten seconds because it
hit the network is not a listing.
"""

import tomllib
from pathlib import Path

from rich.table import Table
from rich.text import Text

from ow.utils import index
from ow.utils.config import load_workspace_config
from ow.utils.display import console, display_path
from ow.utils.legacy import check_legacy_layout

MARKER = Path(".ow") / "config.toml"


def _repos_cell(ws_dir: Path) -> str | Text:
    """The repo column for one workspace: 'alias:spec' pairs, or an error mark.

    A workspace whose config.toml fails to parse must not abort the whole
    listing — that is precisely the moment a broken entry is most annoying
    to have hidden. The error text is built as a Text object rather than
    markup: `exc` is an interpolated exception message, which is data, not
    something safe to hand to Rich's markup parser.
    """
    try:
        ws = load_workspace_config(ws_dir / MARKER)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        return Text(f"(error: {exc})", style="red")
    return ", ".join(f"{alias}:{spec.to_spec_str()}" for alias, spec in ws.repos.items())


def cmd_ls(*, archived: bool = False) -> None:
    """List every known workspace: its name, path, and repos.

    With `archived`, list the archive instead. The archive is not in the
    index by design — that is what being archived means — so it is read
    straight off the filesystem.
    """
    # A warning, not a stop: see check_legacy_layout's own docstring. ls is
    # read-only and index-only, and it is where the other commands' error
    # messages send a lost user.
    check_legacy_layout(fatal=False)

    if archived:
        from ow.commands.archive import archived_workspaces

        workspaces = archived_workspaces()
        if not workspaces:
            console.print("No archived workspaces.")
            return
    else:
        workspaces = index.known_workspaces()
        if not workspaces:
            console.print("No known workspaces. Run `ow init` to create one.")
            return

    table = Table(box=None)
    table.add_column("NAME")
    # Folded, not truncated: on a narrow terminal the path is the one thing
    # the reader came for, and "~/dev/very/long/pa…" is not it.
    table.add_column("PATH", overflow="fold")
    table.add_column("REPOS")

    # Index order is the order workspaces happened to be first resolved,
    # which is arbitrary to everyone but the index. Path breaks the tie so
    # two workspaces sharing a name still come out in a stable order.
    for ws_dir in sorted(workspaces, key=lambda p: (p.name, str(p))):
        # NAME and PATH come straight from the filesystem: a directory named
        # something like "ws[/bad]" is data, not Rich markup, and must not
        # be parsed as such — Text() renders it literally.
        table.add_row(Text(ws_dir.name), Text(display_path(ws_dir)), _repos_cell(ws_dir))

    console.print(table)
