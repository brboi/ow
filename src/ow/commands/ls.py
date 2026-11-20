"""`ow ls` — list known workspaces (plain tab-separated or `--json`, filterable by `--task`)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ow import term
from ow.core import index
from ow.core.json_schema import envelope
from ow.core.session import open_session

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


def run(
    plugins: "Plugins",
    *,
    task: str | None = None,
    json_output: bool = False,
) -> int:
    """List the index's workspaces, in index order; dead entries degrade to `missing`."""
    entries: list[dict[str, Any]] = []
    for path in index.read():
        item = _inspect(path, plugins)
        if task is not None and item["task"] != task:
            continue
        entries.append(item)

    if json_output:
        term.emit_json(envelope({"workspaces": entries}))
        return 0

    for item in entries:
        repos_col = item["status"] if item["status"] != "ok" else ", ".join(
            f"{r['name']}:{r['state']}" for r in item["repos"]
        )
        line = f"{item['name']}\t{_display(Path(item['path']))}\t{repos_col}"
        if item["task"]:
            line += f"\ttask:{item['task']}"
        sys.stdout.write(line + "\n")
    return 0


def _inspect(path: Path, plugins: "Plugins") -> dict[str, Any]:
    if not path.exists():
        return {
            "name": path.name,
            "path": str(path),
            "status": "missing",
            "task": None,
            "http_port": None,
            "repos": [],
        }
    try:
        session = open_session(str(path), need_workspace=False, plugins=plugins)
    except Exception:
        return {
            "name": path.name,
            "path": str(path),
            "status": "unreadable",
            "task": None,
            "http_port": None,
            "repos": [],
        }
    if session.workspace is None:
        return {
            "name": path.name,
            "path": str(path),
            "status": "unreadable",
            "task": None,
            "http_port": None,
            "repos": [],
        }
    ws = session.workspace
    port = ws.settings.get("odoo.http_port")
    repos = [
        {
            "name": loc.name,
            "branch": loc.branch,
            "base": loc.explicit_base or loc.default_base,
            "state": loc.branch or loc.default_base or "(unset)",
        }
        for loc in session.locations
    ]
    return {
        "name": ws.name,
        "path": str(path),
        "status": "ok",
        "task": ws.task,
        "http_port": port if isinstance(port, int) else None,
        "repos": repos,
    }


def _display(path: Path) -> str:
    """`~/…` under `$HOME`, else absolute: a listing column, so no shell quoting."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)
