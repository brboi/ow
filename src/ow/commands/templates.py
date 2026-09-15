"""`ow templates` — see what ow would write into a workspace, and diff it.

ow no longer copies template sources into the workspace. It renders
directly from the packaged bundles (`src/ow/_static/templates/<bundle>/`),
optionally overridden file-by-file from `$XDG_CONFIG_HOME/ow/templates/<bundle>/`,
and keeps the sha256 of what it wrote in `<ws>/.ow/rendered.lock.toml`. A
file on disk that no longer matches its lock entry is the user's: ow will
never overwrite it again, and this command is how you see that has
happened, and what ow would have written instead.
"""

import difflib

from ow.utils.config import Config
from ow.utils.legacy import check_legacy_layout
from ow.utils.resolver import resolve_workspace
from ow.utils.templates import OUTDATED, YOURS, RenderedFile, rendered_states


def _list(states: list[RenderedFile]) -> None:
    if not states:
        print("nothing to render.")
        return
    width = max(len(s.path) for s in states)
    for s in states:
        print(f"{s.path.ljust(width)}  {s.state}")


def _diff(states: list[RenderedFile]) -> None:
    diffable = [s for s in states if s.state in (YOURS, OUTDATED)]
    if not diffable:
        print("nothing differs from what ow would write.")
        return
    for s in diffable:
        assert s.your_text is not None and s.ow_text is not None
        lines = difflib.unified_diff(
            s.your_text.splitlines(keepends=True),
            s.ow_text.splitlines(keepends=True),
            fromfile=f"{s.path} (yours)",
            tofile=f"{s.path} (ow)",
        )
        for line in lines:
            print(line, end="" if line.endswith("\n") else "\n")


def cmd_templates(config: Config, workspace: str | None = None, *, show_diff: bool = False) -> None:
    """List the files ow manages in the workspace with their state, or diff the ones that differ."""
    check_legacy_layout()
    ws_dir, ws = resolve_workspace(name=workspace)

    states = rendered_states(ws, config, ws_dir)
    if show_diff:
        _diff(states)
        return
    _list(states)
