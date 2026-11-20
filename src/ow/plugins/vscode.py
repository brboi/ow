"""VSCode editor plugin for ow."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from ow.core.fs import json_text
from ow.core.plugins import GeneratedFile, hookimpl
from ow.specs.editors import BASE_EXCLUDES, debugpy_fields, default_python, merge as merge_editors

if TYPE_CHECKING:
    from ow.core.workspace import Workspace

# Baseline search excludes, minus *.po and *.pot
_SEARCH_EXCLUDE_GLOBS: dict[str, bool] = dict.fromkeys(
    ("**/node_modules", "**/bower_components", "**/*.code-search", *BASE_EXCLUDES),
    True,
)


@hookimpl
def ow_generated_files(ws: Workspace, answers: Callable[[str], list]) -> list[GeneratedFile]:
    """Generate VSCode settings and launch files from editor contributions."""
    contributions = answers("editors_contribution")
    merged = merge_editors(contributions)

    files = []

    # Generate settings.json if there are language servers, excludes, or a shared python interpreter
    has_shared_python = (
        merged.python is not None
        and merged.python != default_python(ws)
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
        interpreter = str(merged.python or default_python(ws))
        configurations = []
        for launch in merged.launches:
            config = {
                "name": launch.name,
                "type": "debugpy",
                "request": "launch",
                "python": interpreter,
                **debugpy_fields(launch),
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
def ow_multi_workspace_files(multi) -> list[GeneratedFile]:
    """Generate `<workspace>.code-workspace` multi-root file for a multi-stack workspace."""
    if not multi.stacks:
        return []

    folders = [
        {"name": stack.name, "path": f"./{stack.root.name}"}
        for stack in multi.stacks
    ]
    payload = {"folders": folders}
    return [
        GeneratedFile(
            path=PurePosixPath(f"{multi.name}.code-workspace"),
            data=json_text(payload).encode("utf-8"),
        )
    ]
