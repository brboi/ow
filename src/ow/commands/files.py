"""`ow files` — the generated files' states, or the diff of the ones that differ (§8).

Read-only: it projects, it never writes. A hook that raises propagates as
`PluginError` (exit 1), and an `error` diagnostic blocks the refresh, so the
command exits 1 for the same reason `ow refresh` would refuse to write.

The listing is tab-separated, so its lines go to stdout verbatim: `term.out`
renders through Rich, which expands every tab to the next tab stop.
"""

from __future__ import annotations

import difflib
import sys

from ow.core import refresh
from ow.core.refresh import Projection
from ow.core.session import Session
from ow.errors import UsageError


def run(session: Session, *, diff: bool) -> int:
    """List the files and their states, or diff the ones that differ; 1 when blocked."""
    if session.workspace is None:
        raise UsageError("not inside an ow workspace; pass -w <name|path> or run `ow init`")

    projection = refresh.project(session.workspace, session.plugins)
    if diff:
        _print_diffs(projection)
    else:
        _print_states(projection)
    return 1 if projection.blocked else 0


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

    `fromfile` is ow's proposal, `tofile` what is there now, so the diff reads
    as the change that would be made. A file ow has no text for — a binary
    proposal, or a destination that cannot be read — gets one line instead.
    """
    for status in projection.files:
        if status.state not in ("absent", "outdated", "yours"):
            continue
        path = status.path.as_posix()
        proposal = _text(status.proposal)
        # An absent file diffs against nothing, so the whole proposal is an addition.
        current = "" if status.state == "absent" else _text(status.current)
        if proposal is None or current is None:
            _line(f"{path}: binary")
            continue
        for line in difflib.unified_diff(
            proposal.splitlines(),
            current.splitlines(),
            fromfile=f"ow's {path}",
            tofile=path,
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
