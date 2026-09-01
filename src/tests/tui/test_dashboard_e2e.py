"""End-to-end test for the dashboard.

Verifies the new-workspace form can be filled and the request is generated.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ow.utils.config import (
    BranchSpec,
    Config,
    WorkspaceConfig,
    write_workspace_config,
)
from ow.tui.dashboard import DashboardApp, MainScreen
from ow.tui.workspace_forms import NewWorkspaceScreen, NewWorkspaceRequest


def _make_config() -> Config:
    return Config(
        vars={"http_port": 8069, "db_host": "localhost", "db_port": 5432},
        remotes={
            "community": {
                "origin": MagicMock(url="git@github.com:odoo/odoo.git"),
            },
        },
    )


def test_new_workspace_form_fills_and_dismisses(xdg: Path, tmp_path: Path):
    """Drive n → fill form → verify NewWorkspaceRequest is generated."""
    config = _make_config()

    app = DashboardApp(config)
    captured_request = []

    async def _run():
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, MainScreen)

            # Press n to create new workspace
            await pilot.press("n")
            await pilot.pause()

            # NewWorkspaceScreen should be pushed
            assert isinstance(app.screen, NewWorkspaceScreen)
            new_screen = app.screen

            # Fill in the form
            # Set parent
            parent_input = new_screen.query_one("#nw_parent")
            parent_inner = parent_input.query_one("#li_input")
            parent_inner.value = str(tmp_path)

            # Set name
            name_input = new_screen.query_one("#nw_name")
            name_inner = name_input.query_one("#li_input")
            name_inner.value = "e2e-test-ws"

            # Set branch spec for community (already pre-filled with "master")
            spec_input = new_screen.query_one("#nw_spec_community")
            spec_inner = spec_input.query_one("#li_input")
            spec_inner.value = "origin/master"

            # Patch cmd_init to capture the request instead of running it
            with patch("ow.commands.init.cmd_init") as mock_init:
                def capture_init(*args, **kwargs):
                    # Extract the request from kwargs
                    req = NewWorkspaceRequest(
                        parent=Path(kwargs.get("parent", tmp_path)),
                        name=kwargs.get("name", "test"),
                        templates=kwargs.get("templates", []),
                        repos=kwargs.get("repos", {}),
                        configuration=kwargs.get("configuration"),
                    )
                    captured_request.append(req)
                mock_init.side_effect = capture_init

                # Press Create
                create_btn = new_screen.query_one("#btn_create")
                create_btn.press()
                await pilot.pause()

                # Wait for the operation to complete
                for _ in range(100):
                    if not screen._busy:
                        break
                    await pilot.pause()

                await pilot.pause()
                await pilot.pause()

    asyncio.run(_run())

    # Verify the request was captured
    assert len(captured_request) == 1, "cmd_init was not called"
    req = captured_request[0]
    assert req.name == "e2e-test-ws"
    assert req.parent == tmp_path
    assert "community" in req.repos
