"""Tests for the global config screen: modal-opening buttons, confirm-before-
removal for vars and remotes, and header geometry.

`push_screen_wait` requires an active Textual worker context. Called
directly from a plain button-press handler — which is never a worker —
it used to raise `NoActiveWorker` and crash. Every one of these buttons
now opens its modal with `push_screen(screen, callback=...)` instead.

Removing a var or a remote used to happen immediately, with no
confirmation and no indication whether anything still depended on it.
Both now go through `ConfirmDialog`, annotated with which known
workspaces actually use the item.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.widgets import Button, DataTable, Static

from ow.utils.config import Config, RemoteConfig
from ow.tui.global_config import AddRemoteScreen, GlobalConfigScreen
from ow.tui.widgets import ConfirmDialog
from ow.tui.workspace_forms import PromptScreen


def _single_var_config() -> Config:
    return Config(
        vars={"http_port": 8069},
        remotes={"community": {"origin": RemoteConfig(url="git@github.com:odoo/odoo.git")}},
    )


def test_vars_add_and_remote_add_open_prompt_without_crash(dashboard_pilot):
    """'+' in Vars and 'Add' in Remotes open their prompt screens instead
    of raising NoActiveWorker."""

    async def _run():
        async with dashboard_pilot(size=(120, 40)) as (pilot, screen):
            await pilot.press("E")
            await pilot.pause()
            gc_screen = pilot.app.screen

            # --- Vars: "+" must open PromptScreen, not crash ---
            gc_screen._show_section(1)
            await pilot.pause()
            await pilot.click("#vars_add")
            await pilot.pause()
            assert isinstance(pilot.app.screen, PromptScreen), (
                f"expected PromptScreen after '+', got {pilot.app.screen!r}"
            )
            await pilot.press("escape")
            await pilot.pause()

            # --- Remotes: "Add" must open AddRemoteScreen, not crash ---
            gc_screen._show_section(2)
            await pilot.pause()
            await pilot.click("#gc_remote_add")
            await pilot.pause()
            assert isinstance(pilot.app.screen, AddRemoteScreen), (
                f"expected AddRemoteScreen after 'Add', got {pilot.app.screen!r}"
            )
            await pilot.press("escape")
            await pilot.pause()

    asyncio.run(_run())


async def _open_vars_and_select_row(pilot) -> DataTable:
    await pilot.press("E")
    await pilot.pause()
    gc_screen = pilot.app.screen
    gc_screen._show_section(1)
    await pilot.pause()
    table = gc_screen.query_one("#gc_vars").query_one("#vars_table", DataTable)
    table.cursor_coordinate = table.cursor_coordinate.__class__(0, 0)
    await pilot.pause()
    return table


def test_removing_var_asks_for_confirmation_and_names_the_workspace(
    dashboard_pilot, seed_workspace, tmp_path: Path
):
    """Clicking '-' on a var used by a known workspace must show a
    ConfirmDialog naming that workspace, and cancelling must not remove
    the row."""
    seed_workspace(tmp_path, "voip", repos={"community": "origin/master"}, vars={"http_port": 8069})

    async def _run():
        async with dashboard_pilot(_single_var_config(), size=(120, 40)) as (pilot, screen):
            table = await _open_vars_and_select_row(pilot)
            await pilot.click("#vars_remove")
            await pilot.pause()
            assert isinstance(pilot.app.screen, ConfirmDialog)
            details = str(pilot.app.screen.query_one("#confirm_details").render())
            assert "voip" in details, f"workspace not named: {details!r}"
            # Cancel — the row must survive.
            await pilot.click("#btn_no")
            await pilot.pause()
            assert table.row_count == 1

    asyncio.run(_run())


def test_confirming_var_removal_removes_the_row(dashboard_pilot):
    """Confirming the dialog with Yes actually removes the row."""

    async def _run():
        async with dashboard_pilot(_single_var_config(), size=(120, 40)) as (pilot, screen):
            table = await _open_vars_and_select_row(pilot)
            await pilot.click("#vars_remove")
            await pilot.pause()
            assert isinstance(pilot.app.screen, ConfirmDialog)
            await pilot.click("#btn_yes")
            await pilot.pause()
            assert table.row_count == 0

    asyncio.run(_run())


def test_removing_remote_asks_for_confirmation_and_names_the_workspace(
    dashboard_pilot, seed_workspace, tmp_path: Path
):
    """Clicking '- Remove' on a remote alias used by a known workspace
    must show a ConfirmDialog naming that workspace."""
    seed_workspace(tmp_path, "voip", repos={"community": "origin/master"}, vars={"http_port": 8069})

    async def _run():
        async with dashboard_pilot(size=(120, 40)) as (pilot, screen):
            await pilot.press("E")
            await pilot.pause()
            gc_screen = pilot.app.screen
            gc_screen._show_section(2)
            await pilot.pause()
            await pilot.click("#gc_remote_remove")
            await pilot.pause()
            assert isinstance(pilot.app.screen, ConfirmDialog)
            details = str(pilot.app.screen.query_one("#confirm_details").render())
            assert "voip" in details, f"workspace not named: {details!r}"
            await pilot.click("#btn_no")
            await pilot.pause()

    asyncio.run(_run())


def test_removing_unused_var_states_it_is_not_used(dashboard_pilot):
    """A var no workspace overrides must say so, not name anyone."""

    async def _run():
        async with dashboard_pilot(size=(120, 40)) as (pilot, screen):
            await _open_vars_and_select_row(pilot)
            await pilot.click("#vars_remove")
            await pilot.pause()
            assert isinstance(pilot.app.screen, ConfirmDialog)
            details = str(pilot.app.screen.query_one("#confirm_details").render())
            assert "Not used by any workspace" in details
            await pilot.click("#btn_no")
            await pilot.pause()

    asyncio.run(_run())


def test_gc_header_save_button_not_clipped(dashboard_pilot):
    """The header bar must be tall enough for the Save button to render
    fully — a Button is 3 rows (border + label + border); `#gc_header`
    used to be `height: 3` with its own `border-bottom` on top, leaving
    only 2 usable rows and slicing the top and bottom off Save/Cancel.
    """

    async def _run():
        async with dashboard_pilot() as (pilot, screen):
            await pilot.press("E")
            await pilot.pause()
            gc_screen = pilot.app.screen
            assert isinstance(gc_screen, GlobalConfigScreen)

            header = gc_screen.query_one("#gc_header")
            save_button = gc_screen.query_one("#btn_save", Button)

            assert header.content_region.height >= save_button.region.height, (
                f"header content region ({header.content_region.height}) is "
                f"shorter than the Save button ({save_button.region.height}) "
                "— it will be clipped"
            )

    asyncio.run(_run())


def test_global_config_vars_section_explains_they_are_copied_not_linked(dashboard_pilot):
    """The vars panel must say these are initial values copied into new
    workspaces, and that editing them here does not touch existing ones —
    not that the workspace stays linked to the global config."""

    async def _run():
        async with dashboard_pilot(size=(120, 40)) as (pilot, screen):
            await pilot.press("E")
            await pilot.pause()
            gc_screen = pilot.app.screen
            gc_screen._show_section(1)
            await pilot.pause()
            panel = gc_screen.query_one("#gc_panel_vars")
            rendered = "\n".join(str(s.render()) for s in panel.query(Static)).lower()
            assert "copied" in rendered
            assert "new workspace" in rendered
            assert "does not affect" in rendered

    asyncio.run(_run())
