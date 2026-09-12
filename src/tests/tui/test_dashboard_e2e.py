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


def test_operation_with_task_progress_completes_through_pushed_main_screen(
    xdg: Path, tmp_path: Path
):
    """A real DashboardApp always runs with `MainScreen` pushed on top of
    the App's default screen. `apply`/`status`/`reset` all report progress
    through `sink.task(...)` (via `ow.utils.display.task_progress`) while
    running — this drives that exact path through the real, pushed
    `MainScreen`, not a hand-rolled stand-in, and asserts the operation
    completes successfully instead of failing with `NoMatches`.
    """
    from ow.utils.display import task_progress

    config = _make_config()
    app = DashboardApp(config)
    completed: list[str] = []

    def _op() -> str:
        with task_progress("units", 2) as advance:
            advance()
            advance()
        return "ok"

    async def _run():
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.main_screen
            assert isinstance(screen, MainScreen)
            # Let the on_mount legacy-layout check finish first.
            for _ in range(100):
                if not screen._busy:
                    break
                await pilot.pause()

            screen.run_operation("test-op", _op, then=completed.append)
            for _ in range(100):
                if not screen._busy:
                    break
                await pilot.pause()
            await pilot.pause()

    asyncio.run(_run())

    assert completed == ["ok"], "operation did not complete successfully"


def test_theme_picker_persists_selection_to_real_config_file(
    xdg: Path, tmp_path: Path
):
    """Driving 't' → pick a theme → Apply must write it to the real global
    config file, and a fresh `DashboardApp` built from a fresh
    `load_global_config()` read must re-apply it on mount."""
    from ow.utils.config import RemoteConfig, load_global_config, write_global_config

    write_global_config(Config(
        vars={"http_port": 8069},
        remotes={
            "community": {"origin": RemoteConfig(url="git@github.com:odoo/odoo.git")},
        },
    ))
    config = load_global_config()
    assert config.theme == "textual-dark"

    app = DashboardApp(config)

    async def _pick_dracula():
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.main_screen
            for _ in range(100):
                if not screen._busy:
                    break
                await pilot.pause()

            await pilot.press("t")
            await pilot.pause()
            theme_screen = app.screen
            option_list = theme_screen.query_one("#theme_list")
            for i, opt in enumerate(option_list.options):
                if opt.id == "dracula":
                    option_list.highlighted = i
                    break
            await pilot.pause()
            await pilot.click("#btn_apply")
            await pilot.pause()

    asyncio.run(_pick_dracula())

    assert app.theme == "dracula"
    reloaded = load_global_config()
    assert reloaded.theme == "dracula", "theme picker did not persist to disk"

    # A fresh app instance, built from a fresh config read, must reapply it.
    app2 = DashboardApp(reloaded)

    async def _relaunch():
        async with app2.run_test() as pilot:
            await pilot.pause()

    asyncio.run(_relaunch())
    assert app2.theme == "dracula", "theme was not reapplied on relaunch"


def test_theme_survives_an_unrelated_global_config_save(xdg: Path, tmp_path: Path):
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
    from ow.utils.config import RemoteConfig, load_global_config, write_global_config

    write_global_config(Config(
        vars={"http_port": 8069},
        remotes={
            "community": {"origin": RemoteConfig(url="git@github.com:odoo/odoo.git")},
        },
    ))
    config = load_global_config()
    assert config.theme == "textual-dark"

    app = DashboardApp(config)

    async def _run():
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.main_screen
            for _ in range(100):
                if not screen._busy:
                    break
                await pilot.pause()

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
            theme_screen = app.screen
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

    app2 = DashboardApp(reloaded)

    async def _relaunch():
        async with app2.run_test() as pilot:
            await pilot.pause()

    asyncio.run(_relaunch())
    assert app2.theme == "dracula", "theme was not reapplied on relaunch"


def test_theme_persists_synchronously_at_confirm_not_via_deferred_callback(
    xdg: Path, tmp_path: Path
):
    """`ThemeSelectorScreen` must apply and write the theme itself, at the
    moment of confirmation — not depend on `push_screen`'s result
    callback, which Textual defers via `call_next` to the *next* message
    the requester dispatches. A fast enough subsequent action (quitting,
    cancelling) can run ahead of that deferred callback and silently
    discard the selection. Calling the confirm handler directly, with no
    further message-pump processing at all, must already have written the
    theme to disk.
    """
    from ow.tui.dashboard import ThemeSelectorScreen
    from ow.utils.config import RemoteConfig, load_global_config, write_global_config
    from textual.widgets import OptionList

    write_global_config(Config(
        vars={"http_port": 8069},
        remotes={
            "community": {"origin": RemoteConfig(url="git@github.com:odoo/odoo.git")},
        },
    ))
    config = load_global_config()
    app = DashboardApp(config)

    async def _run():
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.main_screen
            for _ in range(100):
                if not screen._busy:
                    break
                await pilot.pause()
            theme_screen = ThemeSelectorScreen(config.theme)
            await app.push_screen(theme_screen)
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


def test_theme_write_failure_surfaces_as_error_notification(xdg: Path, tmp_path: Path):
    """A theme that fails to save must notify the user with an error, not
    fail silently — a raise from `write_global_config` must not vanish."""
    from ow.utils.config import RemoteConfig, load_global_config, write_global_config

    write_global_config(Config(
        vars={"http_port": 8069},
        remotes={
            "community": {"origin": RemoteConfig(url="git@github.com:odoo/odoo.git")},
        },
    ))
    config = load_global_config()
    app = DashboardApp(config)
    captured_calls = []

    async def _run():
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.main_screen
            for _ in range(100):
                if not screen._busy:
                    break
                await pilot.pause()
            with patch.object(app, "notify") as mock_notify, patch(
                "ow.tui.dashboard.write_global_config",
                side_effect=OSError("disk full"),
            ):
                app.apply_theme("nord")
            captured_calls.extend(mock_notify.call_args_list)

    asyncio.run(_run())

    assert captured_calls, "write failure was silently swallowed"
    (message,), kwargs = captured_calls[0]
    assert "disk full" in message
    assert kwargs.get("severity") == "error"
