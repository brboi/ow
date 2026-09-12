"""End-to-end test for the dashboard.

Verifies the new-workspace form can be filled and the request is generated.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from ow.utils.config import Config, RemoteConfig, load_global_config, write_global_config
from ow.tui.workspace_forms import NewWorkspaceRequest
from textual.widgets import OptionList


def test_new_workspace_form_fills_and_dismisses(dashboard_pilot, tmp_path: Path):
    """Drive n → fill form → verify NewWorkspaceRequest is generated."""
    captured_request = []

    async def _run():
        async with dashboard_pilot() as (pilot, screen):
            # Press n to create new workspace
            await pilot.press("n")
            await pilot.pause()

            new_screen = pilot.app.screen

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


def test_operation_with_task_progress_completes_through_pushed_main_screen(dashboard_pilot):
    """A real DashboardApp always runs with `MainScreen` pushed on top of
    the App's default screen. `apply`/`status`/`reset` all report progress
    through `sink.task(...)` (via `ow.utils.display.task_progress`) while
    running — this drives that exact path through the real, pushed
    `MainScreen`, not a hand-rolled stand-in, and asserts the operation
    completes successfully instead of failing with `NoMatches`.
    """
    from ow.utils.display import task_progress

    completed: list[str] = []

    def _op() -> str:
        with task_progress("units", 2) as advance:
            advance()
            advance()
        return "ok"

    async def _run():
        async with dashboard_pilot() as (pilot, screen):
            screen.run_operation("test-op", _op, then=completed.append)
            for _ in range(100):
                if not screen._busy:
                    break
                await pilot.pause()
            await pilot.pause()

    asyncio.run(_run())

    assert completed == ["ok"], "operation did not complete successfully"


def _theme_test_config() -> Config:
    return Config(
        vars={"http_port": 8069},
        remotes={
            "community": {"origin": RemoteConfig(url="git@github.com:odoo/odoo.git")},
        },
    )




def test_setting_app_theme_directly_persists_to_disk(dashboard_pilot):
    """The decisive regression test: setting `app.theme` directly — exactly
    what Textual's built-in command palette and header menu do — must persist
    the theme to the real global config file. This is the test whose absence
    let the bug ship twice.
    """
    write_global_config(_theme_test_config())
    config = load_global_config()
    assert config.theme == "textual-dark"

    async def _run():
        async with dashboard_pilot(config) as (pilot, screen):
            # Set theme directly, as the built-in palette does
            pilot.app.theme = "dracula"
            await pilot.pause()
            assert pilot.app.theme == "dracula"

    asyncio.run(_run())

    reloaded = load_global_config()
    assert reloaded.theme == "dracula", (
        "setting app.theme directly did not persist to disk — "
        "this is the exact bug: the built-in palette sets theme but doesn't save it"
    )


def test_startup_with_theme_in_config_does_not_rewrite_file(dashboard_pilot):
    """When the config already has theme = "dracula", launching the app must
    apply it without writing the file again. A write that just re-states what
    is already on disk is churn.
    """
    # Write a config with theme = "dracula"
    cfg = _theme_test_config()
    cfg.theme = "dracula"
    write_global_config(cfg)

    # Record the file's mtime before launch
    from ow.utils import paths
    config_path = paths.config_file()
    mtime_before = config_path.stat().st_mtime

    async def _run():
        config = load_global_config()
        async with dashboard_pilot(config) as (pilot, screen):
            await pilot.pause()
            assert pilot.app.theme == "dracula"

    asyncio.run(_run())

    # The file should not have been rewritten
    mtime_after = config_path.stat().st_mtime
    assert mtime_after == mtime_before, (
        "startup rewrote the config file even though the theme was already saved"
    )


def test_theme_previews_on_highlight_in_command_palette(dashboard_pilot):
    """Regression test: Ctrl+P → "theme" → arrow down must change app.theme
    live (preview on highlight), but must NOT write to disk. Enter commits
    and writes to disk.

    Verifies the fix for the live-preview bug where every highlight wrote
    the config to disk.
    """
    from ow.utils import paths

    write_global_config(_theme_test_config())
    config = load_global_config()
    assert config.theme == "textual-dark"

    async def _run():
        async with dashboard_pilot(config) as (pilot, screen):
            assert pilot.app.theme == "textual-dark"

            # Open the theme-specific command palette
            pilot.app.search_themes()
            await pilot.pause()

            # It opens a CommandPalette as the active screen; arrow down
            # to highlight "nord" (index 2 in sorted order — after
            # "ansi-dark" and "ansi-light").
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()

            # The preview handler should have run: app.theme should now
            # match the highlighted theme.
            assert pilot.app.theme != "textual-dark", (
                "theme did not change on highlight — preview is broken"
            )

            # CRITICAL: preview must NOT write to disk
            config_path = paths.config_file()
            mtime_before_enter = config_path.stat().st_mtime
            reloaded_during_preview = load_global_config()
            assert reloaded_during_preview.theme == "textual-dark", (
                "preview wrote to disk — theme should only change visually, not persist"
            )

            # Press Enter to commit the selection
            await pilot.press("enter")
            await pilot.pause()

            # The final committed theme is whatever the user picked; verify
            # it persisted to disk.
            committed = pilot.app.theme
            reloaded = load_global_config()
            assert reloaded.theme == committed, (
                f"theme '{committed}' did not persist to disk after commit"
            )

    asyncio.run(_run())


def test_theme_escape_reverts_without_writing(dashboard_pilot):
    """Regression test: Ctrl+P → "theme" → arrow down → Escape must revert
    the display to the original theme and must NOT write to disk.

    Verifies the fix for the bug where Escape left the last-previewed theme
    in the display (and had written it to disk on every highlight).
    """
    from ow.utils import paths

    write_global_config(_theme_test_config())
    config = load_global_config()
    assert config.theme == "textual-dark"

    async def _run():
        async with dashboard_pilot(config) as (pilot, screen):
            assert pilot.app.theme == "textual-dark"

            # Record mtime before opening palette
            config_path = paths.config_file()
            mtime_before = config_path.stat().st_mtime

            # Open the theme-specific command palette
            pilot.app.search_themes()
            await pilot.pause()

            # Arrow down to highlight a different theme
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()

            # Preview should have changed the display
            assert pilot.app.theme != "textual-dark", (
                "theme did not change on highlight — preview is broken"
            )

            # Press Escape to cancel
            await pilot.press("escape")
            await pilot.pause()

            # The display should revert to the original theme
            assert pilot.app.theme == "textual-dark", (
                f"escape did not revert theme — expected 'textual-dark', got '{pilot.app.theme}'"
            )

            # The config file should NOT have been written
            mtime_after = config_path.stat().st_mtime
            assert mtime_after == mtime_before, (
                "escape wrote to disk — config should not change on cancel"
            )
            reloaded = load_global_config()
            assert reloaded.theme == "textual-dark", (
                "escape changed the config on disk — should not persist on cancel"
            )

    asyncio.run(_run())
