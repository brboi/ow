"""VSCode editor plugin for ow."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from ow.core.plugins import GeneratedFile, hookimpl
from ow.specs.editors import EditorsSpec, json_text, merge as merge_editors

if TYPE_CHECKING:
    from ow.core.workspace import Workspace

HOOKSPECS = (EditorsSpec,)

# VSCode baseline excludes from v3.0.0a1, minus *.po and *.pot
_SEARCH_EXCLUDE_GLOBS: dict[str, bool] = {
    "**/node_modules": True,
    "**/bower_components": True,
    "**/*.code-search": True,
    "**/.git": True,
    "**/.svn": True,
    "**/.hg": True,
    "**/.jj": True,
    "**/CVS": True,
    "**/.DS_Store": True,
    "**/Thumbs.db": True,
    "**/.classpath": True,
    "**/.settings": True,
    "**/.venv": True,
}


@hookimpl
def ow_generated_files(ws: Workspace, answers) -> list[GeneratedFile]:
    """Generate VSCode settings and launch files from editor contributions."""
    contributions = answers("editors_contribution")
    merged = merge_editors(contributions)
    
    files = []
    
    # Generate settings.json if there are language servers, excludes, or a shared python interpreter
    has_shared_python = (
        merged.python is not None
        and merged.python != (ws.root / ".venv/bin/python")
    )
    if merged.language_servers or merged.excludes or has_shared_python:
        settings = {}
        
        # Merge language servers at top level, ignoring server_id
        for _server_id, server_settings in merged.language_servers.items():
            settings.update(server_settings)

        if has_shared_python:
            settings["python.defaultInterpreterPath"] = str(merged.python)
        
        # Add search.exclude with baseline + contributions
        excludes = dict(_SEARCH_EXCLUDE_GLOBS)
        # Add contribution excludes
        for exclude in merged.excludes:
            excludes[exclude] = True
        
        settings["search.exclude"] = excludes
        
        files.append(
            GeneratedFile(
                path=PurePosixPath(".vscode/settings.json"),
                data=json_text(settings).encode("utf-8"),
            )
        )
    
    # Generate launch.json only if there are launches
    if merged.launches:
        interpreter = str(merged.python or (ws.root / ".venv/bin/python"))
        configurations = []
        for launch in merged.launches:
            config = {
                "name": launch.name,
                "type": "debugpy",
                "request": "launch",
                "python": interpreter,
                "program": str(launch.program),
                "args": list(launch.args),
                "cwd": str(launch.cwd),
                "justMyCode": False,
                "pythonArgs": ["-Xfrozen_modules=off"],
                "env": dict(launch.env),
            }
            configurations.append(config)

        launch_data = {
            "version": "0.2.0",
            "configurations": configurations,
        }

        files.append(
            GeneratedFile(
                path=PurePosixPath(".vscode/launch.json"),
                data=json_text(launch_data).encode("utf-8"),
            )
        )

    return files


@hookimpl
def ow_task_files(task) -> list[GeneratedFile]:
    """Generate `<task>.code-workspace` multi-root workspace file for tasks."""
    if not task.workspaces:
        return []

    name_counts: dict[str, int] = {}
    for ws in task.workspaces:
        name_counts[ws.name] = name_counts.get(ws.name, 0) + 1

    folders = []
    for ws in task.workspaces:
        if task.common_parent is not None:
            path_str = f"./{ws.root.name}"
        else:
            path_str = str(ws.root)
        label = (
            f"{ws.root.parent.name}/{ws.name}"
            if name_counts[ws.name] > 1
            else ws.name
        )
        folders.append({"name": label, "path": path_str})

    payload = {"folders": folders}
    return [
        GeneratedFile(
            path=PurePosixPath(f"{task.name}.code-workspace"),
            data=json_text(payload).encode("utf-8"),
        )
    ]
