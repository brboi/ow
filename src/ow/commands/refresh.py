"""`ow refresh` — recompute and write generated files (workspace + task files if member of a task)."""

from __future__ import annotations

from ow.commands._shared import refresh_workspace
from ow.core import tasks
from ow.core.session import Session


def run(session: Session) -> int:
    """Refresh the session's workspace (and its task's files if applicable); 1 when blocked."""
    code = refresh_workspace(session)
    if session.workspace is not None and session.workspace.task:
        task = tasks.find_task(session.workspace.task, session.plugins)
        if task is not None:
            tasks.sync_task_files(task, session.plugins)
    return code
