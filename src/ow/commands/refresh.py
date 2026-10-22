"""`ow refresh` — recompute and write the generated files."""

from __future__ import annotations

from ow.commands._shared import refresh_workspace
from ow.core.session import Session


def run(session: Session) -> int:
    """Refresh the session's workspace and print the summary; 1 when it was blocked."""
    return refresh_workspace(session)
