"""`ow files` — the generated files' states, or the diff of the ones that differ.

Read-only: it projects, it never writes. An `error` diagnostic exits 1 for the same reason `ow refresh` would refuse to write; the listing is tab-separated and written to stdout verbatim.
"""

from __future__ import annotations

import difflib
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ow import term
from ow.core import refresh, stacks
from ow.core.json_schema import envelope
from ow.core.refresh import Projection
from ow.core.session import Session
from ow.errors import UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


def _emit_projection(
    *,
    name: str,
    projection: Projection,
    diff: bool,
    json_output: bool,
    multi_stack: bool = False,
) -> int:
    if json_output:
        payload: dict[str, Any] = {
            "workspace": name,
            "blocked": projection.blocked,
            "files": [
                {
                    "path": st.path.as_posix(),
                    "owner": st.owner,
                    "state": st.state,
                    "reason": st.reason,
                }
                for st in projection.files
            ],
            "stale_lock": list(projection.stale_lock),
            "diagnostics": [
                {"plugin": p, "level": d.level, "text": d.text}
                for p, d in projection.diagnostics
            ],
        }
        if multi_stack:
            payload["multi_stack"] = True
        term.emit_json(envelope(payload))
        return 1 if projection.blocked else 0

    if diff:
        _print_diffs(projection)
    else:
        _print_states(projection)

    if projection.blocked:
        for plugin_name, diag in projection.diagnostics:
            if diag.level == "error":
                term.error(f"{plugin_name}: {diag.text}")
        return 1
    return 0


def run(session: Session, *, diff: bool, json_output: bool = False) -> int:
    """List the files and their states, or diff the ones that differ; 1 when blocked."""
    if session.workspace is None:
        raise UsageError("not inside an ow workspace; pass -w <name|path> or run `ow init`")

    with term.json_mode(json_output):
        multi = None
        if session.workspace.is_stack:
            parent = stacks.parent_multi_workspace(session.workspace.root)
            if parent is not None:
                loaded = stacks.load_multi_workspace(
                    parent, session.plugins, discover_repo_plugins=False
                )
                multi = loaded[0] if loaded is not None else None
        projection = (
            refresh.project(session.workspace, session.plugins, multi=multi)
            if multi is not None
            else refresh.project(session.workspace, session.plugins)
        )
        return _emit_projection(
            name=session.workspace.name,
            projection=projection,
            diff=diff,
            json_output=json_output,
        )


def run_multi(
    multi_root: Path,
    plugins: Plugins,
    *,
    diff: bool,
    json_output: bool = False,
) -> int:
    """Inspect multi-stack workspace-level generated files (e.g. `<ws>.code-workspace`)."""
    loaded = stacks.load_multi_workspace(multi_root, plugins)
    if loaded is None:
        raise UsageError(f"no valid stacks found in {multi_root}")
    multi, sessions = loaded
    eff_plugins = sessions[0].plugins if sessions else plugins
    with term.json_mode(json_output):
        projection = stacks.project_multi_workspace_files(multi, eff_plugins)
        return _emit_projection(
            name=multi.name,
            projection=projection,
            diff=diff,
            json_output=json_output,
            multi_stack=True,
        )


def _print_states(projection: Projection) -> None:
    """One `<path>\\t<state>` line per file, with the reason for yours/skipped."""
    for status in projection.files:
        line = f"{status.path.as_posix()}\t{status.state}"
        if status.state in ("yours", "skipped") and status.reason:
            line += f"\t{status.reason}"
        _line(line)
    _print_stale(projection)


def _print_diffs(projection: Projection) -> None:
    """A unified diff per file ow would write, plus the stale lock entries.

    Each diff reads current → ow's proposal, so `+` lines are what ow would add; a file ow has no text for gets one line instead.
    """
    for status in projection.files:
        if status.state not in ("absent", "outdated", "yours"):
            continue
        path = status.path.as_posix()
        proposal = _text(status.proposal)
        # An absent file has no current bytes, so every proposal line is an addition.
        current = "" if status.state == "absent" else _text(status.current)
        if proposal is None or current is None:
            _line(f"{path}: binary")
            continue
        for line in difflib.unified_diff(
            current.splitlines(),
            proposal.splitlines(),
            fromfile=path,
            tofile=f"ow's {path}",
            lineterm="",
        ):
            _line(line)
    _print_stale(projection)


def _print_stale(projection: Projection) -> None:
    """The lock entries no plugin produces any more; their files stay put."""
    for path in projection.stale_lock:
        _line(f"{path}\tstale (no longer produced)")


def _line(text: str) -> None:
    """Write one line to stdout verbatim."""
    sys.stdout.write(f"{text}\n")


def _text(data: bytes | None) -> str | None:
    """The bytes as text, or None when no line diff exists for them."""
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
