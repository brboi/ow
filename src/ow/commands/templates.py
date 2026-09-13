"""`ow templates` — see what ow materialised, and hear when it drifted.

Every template file lives twice: the working copy under
`<ws>/.ow/templates/<bundle>/<relpath>` that rendering reads from and the
user is free to edit, and an entry in `<ws>/.ow/templates.lock.toml` that
records the sha256 of the source it was copied from. The lock is what makes
"did I edit this, and did ow's source move since?" answerable at all —
diffing the working copy against today's source only ever shows edits made
on purpose, appearing at every hop whether or not ow ever touched the file.
"""

import difflib
from pathlib import Path

from ow.utils.legacy import check_legacy_layout
from ow.utils.resolver import resolve_workspace
from ow.utils.templates import OUTDATED, TemplateState, template_states


def _list(states: list[TemplateState]) -> None:
    if not states:
        print("No templates materialised. Run `ow apply` first.")
        return
    width = max(len(s.name) for s in states)
    for s in states:
        print(f"{s.name.ljust(width)}  {s.state}")


def _diff(states: list[TemplateState]) -> None:
    outdated = [s for s in states if s.state == OUTDATED]
    if not outdated:
        print("No materialised template is outdated.")
        return
    for s in outdated:
        assert s.source is not None  # OUTDATED implies a source to compare to
        lines = difflib.unified_diff(
            s.copy.read_text().splitlines(keepends=True),
            s.source.read_text().splitlines(keepends=True),
            fromfile=f"{s.name} (yours)",
            tofile=f"{s.name} (ow)",
        )
        for line in lines:
            print(line, end="" if line.endswith("\n") else "\n")


def cmd_templates(workspace: str | None = None, *, show_diff: bool = False) -> None:
    """List materialised template files with their state, or diff the outdated ones."""
    check_legacy_layout()
    ws_dir: Path
    ws_dir, _ = resolve_workspace(name=workspace)

    states = template_states(ws_dir)
    if show_diff:
        _diff(states)
        return
    _list(states)
