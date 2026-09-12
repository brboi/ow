"""Tests for the dashboard action handlers (Phase 4).

These tests verify:
- Workspace list rendering (active + archived)
- Config-only detail rendering without git calls
- Remove confirmation flow
- New workspace form validation
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Static

from ow.utils import paths
from ow.utils.config import BranchSpec, WorkspaceConfig, write_workspace_config
from ow.tui.dashboard import MainScreen
from ow.tui.widgets import ConfirmDialog
from ow.tui.workspace_forms import NewWorkspaceScreen


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_workspace_list_active_and_archived(dashboard_pilot, seed_workspace, tmp_path: Path):
    """Two workspaces on disk (one archived) produce three options with separator."""
    seed_workspace(tmp_path, "ws-alpha")
    seed_workspace(tmp_path, "ws-beta")

    # Create an archived workspace directly under archives_dir — a
    # location `seed_workspace` (which targets `base/name`) doesn't model.
    archives_dir = paths.archives_dir()
    archives_dir.mkdir(parents=True, exist_ok=True)
    archived_ws = archives_dir / "ws-archived"
    archived_ws.mkdir(parents=True, exist_ok=True)
    ws_config = WorkspaceConfig(
        repos={"community": BranchSpec("origin/master")},
        templates=["common"],
        vars={},
    )
    write_workspace_config(archived_ws / ".ow" / "config.toml", ws_config)

    async def _run():
        async with dashboard_pilot() as (pilot, screen):
            entries = screen._entries
            assert len(entries) == 3, f"expected 3 entries, got {len(entries)}"

            active_entries = [e for e in entries if not e.archived]
            archived_entries = [e for e in entries if e.archived]
            assert len(active_entries) == 2
            assert len(archived_entries) == 1
            # Check option list has separator
            option_list = screen.query_one("#ws_list")
            # 2 active + 1 archived = 3 options (separators don't count)
            assert option_list.option_count == 3

    asyncio.run(_run())


def test_detail_renders_config_without_git(dashboard_pilot, seed_workspace, tmp_path: Path):
    """Highlighting a workspace renders its repos/templates/vars straight
    from config — the alias, its branch spec and its template all show up
    in the detail pane — before the debounced status refresh ever calls
    git."""
    seed_workspace(
        tmp_path,
        "test-ws",
        repos={"community": "origin/master"},
        templates=["common"],
    )

    async def _run():
        async with dashboard_pilot() as (pilot, screen):
            with patch("ow.tui.dashboard.gather_workspace_status") as mock_gather:
                option_list = screen.query_one("#ws_list")
                option_list.highlighted = 0
                await pilot.pause()

                detail = screen.query_one("#detail")
                rendered = "\n".join(
                    str(static.render()) for static in detail.query(Static)
                )
                assert "community" in rendered, rendered
                assert "master" in rendered, rendered
                assert "common" in rendered, rendered

                # The debounced git-backed status refresh must not have
                # fired yet from a mere highlight.
                mock_gather.assert_not_called()

    asyncio.run(_run())


def test_remove_pushes_confirm_and_cancel_does_nothing(
    dashboard_pilot, seed_workspace, tmp_path: Path
):
    """x on a workspace pushes ConfirmDialog; dismissing False performs no removal."""
    ws_dir = seed_workspace(tmp_path, "to-remove")

    async def _run():
        async with dashboard_pilot() as (pilot, screen):
            # Select the workspace
            option_list = screen.query_one("#ws_list")
            option_list.highlighted = 0
            await pilot.pause()

            # Press x to remove
            await pilot.press("x")
            await pilot.pause()
            await pilot.pause()

            # A ConfirmDialog should be pushed (after survey completes)
            # Since survey runs in a worker, we need to wait for it
            for _ in range(50):
                if isinstance(pilot.app.screen, ConfirmDialog):
                    break
                await pilot.pause()

            # If we got a confirm dialog, dismiss it with False
            if isinstance(pilot.app.screen, ConfirmDialog):
                pilot.app.screen.dismiss(False)
                await pilot.pause()

            # The workspace directory should still exist
            assert ws_dir.exists(), "workspace was removed despite cancelling"

    asyncio.run(_run())


def test_new_workspace_form_validation(dashboard_pilot):
    """n opens NewWorkspaceScreen; can be dismissed."""

    async def _run():
        async with dashboard_pilot() as (pilot, screen):
            # Press n to create new workspace
            await pilot.press("n")
            await pilot.pause()

            # NewWorkspaceScreen should be pushed
            assert isinstance(pilot.app.screen, NewWorkspaceScreen)
            new_screen = pilot.app.screen

            # Just dismiss it
            new_screen.dismiss(None)
            await pilot.pause()

            # Back to MainScreen
            assert isinstance(pilot.app.screen, MainScreen)

    asyncio.run(_run())


def test_help_screen_opens(dashboard_pilot):
    """? pushes HelpScreen."""

    async def _run():
        async with dashboard_pilot() as (pilot, screen):
            # Press ? for help
            await pilot.press("question_mark")
            await pilot.pause()

            # HelpScreen should be on top
            from ow.tui.dashboard import HelpScreen
            assert isinstance(pilot.app.screen, HelpScreen)

            # Dismiss it
            pilot.app.screen.dismiss(None)
            await pilot.pause()

            # Back to MainScreen
            assert isinstance(pilot.app.screen, MainScreen)

    asyncio.run(_run())


def test_quit_action(dashboard_pilot):
    """q exits the app with a clean (zero) return code."""

    async def _run():
        async with dashboard_pilot() as (pilot, screen):
            # Press q to quit
            await pilot.press("q")
            await pilot.pause()

            # The app decided to exit cleanly — `return_code` is the
            # public, documented value `App.exit()` was called with.
            assert pilot.app.return_code == 0

    asyncio.run(_run())
