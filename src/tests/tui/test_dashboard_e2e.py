"""End-to-end test for the dashboard.

Verifies the new-workspace form can be filled and the request is generated.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from ow.utils.config import Config, RemoteConfig, load_global_config, write_global_config
from ow.tui.dashboard import ThemeSelectorScreen
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


def test_theme_picker_persists_selection_to_real_config_file(dashboard_pilot):
    """Driving 't' → pick a theme → Apply must write it to the real global
    config file, and a fresh `DashboardApp` built from a fresh
    `load_global_config()` read must re-apply it on mount."""
    write_global_config(_theme_test_config())
    config = load_global_config()
    assert config.theme == "textual-dark"

    async def _pick_dracula():
        async with dashboard_pilot(config) as (pilot, screen):
            await pilot.press("t")
            await pilot.pause()
            theme_screen = pilot.app.screen
            option_list = theme_screen.query_one("#theme_list")
            for i, opt in enumerate(option_list.options):
                if opt.id == "dracula":
                    option_list.highlighted = i
                    break
            await pilot.pause()
            await pilot.click("#btn_apply")
            await pilot.pause()
            assert pilot.app.theme == "dracula"

    asyncio.run(_pick_dracula())

    reloaded = load_global_config()
    assert reloaded.theme == "dracula", "theme picker did not persist to disk"

    # A fresh app instance, built from a fresh config read, must reapply it.
    async def _relaunch():
        async with dashboard_pilot(reloaded) as (pilot, screen):
            assert pilot.app.theme == "dracula", "theme was not reapplied on relaunch"

    asyncio.run(_relaunch())


def test_theme_survives_an_unrelated_global_config_save(dashboard_pilot):
    """Root cause of the real bug: `MainScreen._config` used to be
    *replaced* with a freshly loaded `Config` object every time the Global
    Config screen was saved (`_do_save_global_config`), while the theme
    picker mutates `DashboardApp._config` — the *same* object as
    `MainScreen._config`, until the first Global Config save silently
    swaps that reference out for a different one. From that point on the
    two screens observe different objects: a *later* Global Config save
    carries MainScreen's now-stale (pre-pick) theme value back over
    whatever the picker had written, quietly reverting it to default —
    exactly what a real user hits by adding a remote (Save), picking a
    theme, then adding another remote (Save) in the same sitting.

    This drives that exact sequence: an unrelated Global Config save,
    then a theme pick, then another unrelated Global Config save — all in
    one continuous session — and asserts the theme is still there
    afterwards, on disk and reapplied on relaunch.
    """
    write_global_config(_theme_test_config())
    config = load_global_config()
    assert config.theme == "textual-dark"

    async def _run():
        async with dashboard_pilot(config) as (pilot, screen):
            # 1. An unrelated Global Config save, before ever touching the
            #    theme -- this is what used to swap MainScreen's config
            #    reference out.
            await pilot.press("E")
            await pilot.pause()
            await pilot.click("#btn_save")
            await pilot.pause()

            # 2. Pick a theme.
            await pilot.press("t")
            await pilot.pause()
            theme_screen = pilot.app.screen
            option_list = theme_screen.query_one("#theme_list")
            for i, opt in enumerate(option_list.options):
                if opt.id == "dracula":
                    option_list.highlighted = i
                    break
            await pilot.pause()
            await pilot.click("#btn_apply")
            await pilot.pause()

            # 3. Another unrelated Global Config save.
            await pilot.press("E")
            await pilot.pause()
            await pilot.click("#btn_save")
            await pilot.pause()

    asyncio.run(_run())

    reloaded = load_global_config()
    assert reloaded.theme == "dracula", (
        "theme reverted to default after an unrelated Global Config save"
    )

    async def _relaunch():
        async with dashboard_pilot(reloaded) as (pilot, screen):
            assert pilot.app.theme == "dracula", "theme was not reapplied on relaunch"

    asyncio.run(_relaunch())


def test_theme_persists_synchronously_at_confirm_not_via_deferred_callback(dashboard_pilot):
    """`ThemeSelectorScreen` must apply and write the theme itself, at the
    moment of confirmation — not depend on `push_screen`'s result
    callback, which Textual defers via `call_next` to the *next* message
    the requester dispatches. A fast enough subsequent action (quitting,
    cancelling) can run ahead of that deferred callback and silently
    discard the selection. Calling the confirm handler directly, with no
    further message-pump processing at all, must already have written the
    theme to disk.
    """
    write_global_config(_theme_test_config())
    config = load_global_config()

    async def _run():
        async with dashboard_pilot(config) as (pilot, screen):
            theme_screen = ThemeSelectorScreen(config.theme)
            await pilot.app.push_screen(theme_screen)
            await pilot.pause()
            option_list = theme_screen.query_one("#theme_list", OptionList)
            option = next(o for o in option_list.options if o.id == "nord")
            # Call the confirm handler directly and inspect the file
            # immediately after — no additional pilot.pause(), no
            # push_screen callback ever invoked.
            theme_screen.on_option_list_option_selected(
                OptionList.OptionSelected(option_list, option, 0)
            )
            reloaded = load_global_config()
            assert reloaded.theme == "nord", (
                "theme was not persisted synchronously at confirm"
            )

    asyncio.run(_run())


def test_theme_write_failure_surfaces_as_error_notification(dashboard_pilot):
    """A theme that fails to save must notify the user with an error, not
    fail silently — a raise from `write_global_config` must not vanish."""
    write_global_config(_theme_test_config())
    config = load_global_config()
    captured_calls = []

    async def _run():
        async with dashboard_pilot(config) as (pilot, screen):
            with patch.object(pilot.app, "notify") as mock_notify, patch(
                "ow.tui.dashboard.write_global_config",
                side_effect=OSError("disk full"),
            ):
                pilot.app.apply_theme("nord")
            captured_calls.extend(mock_notify.call_args_list)

    asyncio.run(_run())

    assert captured_calls, "write failure was silently swallowed"
    (message,), kwargs = captured_calls[0]
    assert "disk full" in message
    assert kwargs.get("severity") == "error"
