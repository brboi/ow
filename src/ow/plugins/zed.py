"""Zed editor plugin for ow."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from ow.core.fs import json_text
from ow.core.plugins import GeneratedFile, hookimpl
from ow.specs.editors import BASE_EXCLUDES, debugpy_fields, default_python
from ow.specs.editors import merge as merge_editors

if TYPE_CHECKING:
    from ow.core.workspace import Workspace

# Baseline file scan exclusions
_ZED_FILE_SCAN_EXCLUSIONS: tuple[str, ...] = (*BASE_EXCLUDES, "**/node_modules")


@hookimpl
def ow_generated_files(ws: Workspace, answers: Callable[[str], list]) -> list[GeneratedFile]:
    """Generate Zed settings and debug files from editor contributions."""
    contributions = answers("editors_contribution")
    merged = merge_editors(contributions)

    files = []

    # Generate settings.json only if there are language servers or excludes
    if merged.language_servers or merged.excludes:
        settings = {}

        # Add language servers under lsp.<server_id>.settings
        if merged.language_servers:
            lsp = {}
            for server_id, server_settings in merged.language_servers.items():
                lsp[server_id] = {"settings": server_settings}
            settings["lsp"] = lsp

        # Add file_scan_exclusions with baseline + contributions, deduplicated
        excludes = list(_ZED_FILE_SCAN_EXCLUSIONS)
        seen = set(excludes)
        for exclude in merged.excludes:
            if exclude not in seen:
                excludes.append(exclude)
                seen.add(exclude)

        settings["file_scan_exclusions"] = excludes

        files.append(
            GeneratedFile(
                path=PurePosixPath(".zed/settings.json"),
                data=json_text(settings).encode("utf-8"),
            )
        )

    # Generate debug.json only if there are launches
    if merged.launches:
        interpreter = str(merged.python or default_python(ws))
        configurations = []
        for launch in merged.launches:
            config = {
                "adapter": "Debugpy",
                "label": launch.name,
                "request": "launch",
                "console": "integratedTerminal",
                **debugpy_fields(launch),
                "python": interpreter,
            }
            configurations.append(config)

        files.append(
            GeneratedFile(
                path=PurePosixPath(".zed/debug.json"),
                data=json_text(configurations).encode("utf-8"),
            )
        )

    return files
