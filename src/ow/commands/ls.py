"""`ow ls` — list indexed workspaces (mono-stack and multi-stack, plain tab-separated or `--json`)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ow import term
from ow.commands._workspace import stack_info
from ow.core import index, stacks
from ow.core.json_schema import envelope
from ow.core.session import open_session

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


def run(
    plugins: "Plugins",
    *,
    json_output: bool = False,
) -> int:
    """List the index's workspaces, in index order; dead entries degrade to `missing`."""
    with term.json_mode(json_output):
        entries: list[dict[str, Any]] = []
        for path in index.read():
            entries.append(_inspect(path, plugins))

        if json_output:
            term.emit_json(envelope({"workspaces": entries}))
            return 0

        for item in entries:
            if item["status"] != "ok":
                detail_col = item["status"]
            elif item.get("multi_stack"):
                stack_labels = ", ".join(s["name"] for s in item.get("stacks", []))
                detail_col = f"stacks: {stack_labels}"
            else:
                detail_col = ", ".join(f"{r['name']}:{r['state']}" for r in item["repos"])
            line = f"{item['name']}\t{_display(Path(item['path']))}\t{detail_col}"
            sys.stdout.write(line + "\n")
        return 0


def _inspect_single(path: Path, plugins: "Plugins") -> dict[str, Any]:
    try:
        session = open_session(str(path), need_workspace=False, plugins=plugins)
    except Exception:
        return {
            "name": path.name,
            "path": str(path),
            "status": "unreadable",
            "multi_stack": False,
            "ports": {},
            "repos": [],
        }
    if session.workspace is None:
        return {
            "name": path.name,
            "path": str(path),
            "status": "unreadable",
            "multi_stack": False,
            "ports": {},
            "repos": [],
        }
    ws = session.workspace
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
        "multi_stack": False,
        "ports": dict(stack_info(ws, plugins).ports),
        "repos": repos,
    }


def _inspect(path: Path, plugins: "Plugins") -> dict[str, Any]:
    if not path.exists():
        return {
            "name": path.name,
            "path": str(path),
            "status": "missing",
            "multi_stack": False,
            "ports": {},
            "repos": [],
        }
    stack_paths = stacks.detect_multi_workspace(path)
    if stack_paths:
        stack_items = [_inspect_single(sp, plugins) for sp in stack_paths]
        return {
            "name": path.name,
            "path": str(path),
            "status": "ok",
            "multi_stack": True,
            "stacks": stack_items,
            "repos": [],
        }

    if (path / ".ow" / "config.toml").exists():
        return _inspect_single(path, plugins)

    return {
        "name": path.name,
        "path": str(path),
        "status": "unreadable",
        "multi_stack": False,
        "ports": {},
        "repos": [],
    }


def _display(path: Path) -> str:
    """`~/…` under `$HOME`, else absolute: a listing column, so no shell quoting."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)
