"""VSCode editor plugin for ow."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from ow.core.plugins import GeneratedFile, hookimpl
from ow.specs.editors import EditorsSpec, merge as merge_editors

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


def json_text(value: object) -> str:
    """Serialize to JSON with stable indentation and trailing newline."""
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


@hookimpl
def ow_generated_files(ws: Workspace, answers) -> list[GeneratedFile]:
    """Generate VSCode settings and launch files from editor contributions."""
    contributions = answers("editors_contribution")
    merged = merge_editors(contributions)
    
    files = []
    
    # Generate settings.json only if there are language servers or excludes
    if merged.language_servers or merged.excludes:
        settings = {}
        
        # Merge language servers at top level, ignoring server_id
        for _server_id, server_settings in merged.language_servers.items():
            settings.update(server_settings)
        
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
        interpreter = str(ws.root / ".venv/bin/python")
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
