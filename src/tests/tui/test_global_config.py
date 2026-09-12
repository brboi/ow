"""Tests for the global config screen: modal-opening buttons and
confirm-before-removal for vars and remotes.

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

from textual.widgets import DataTable

from ow.utils import index
from ow.utils.config import (
    BranchSpec,
    Config,
    RemoteConfig,
    WorkspaceConfig,
    write_workspace_config,
)
from ow.tui.dashboard import DashboardApp
from ow.tui.global_config import AddRemoteScreen
from ow.tui.widgets import ConfirmDialog
from ow.tui.workspace_forms import PromptScreen


def _make_config() -> Config:
    return Config(
        vars={"http_port": 8069},
        remotes={
            "community": {
                "origin": RemoteConfig(url="git@github.com:odoo/odoo.git"),
            },
        },
    )


def test_vars_add_and_remote_add_open_prompt_without_crash(xdg):
    """'+' in Vars and 'Add' in Remotes open their prompt screens instead
    of raising NoActiveWorker."""
    app = DashboardApp(_make_config())

    async def _run():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen = app.main_screen
            for _ in range(100):
                if not screen._busy:
                    break
                await pilot.pause()

            await pilot.press("E")
            await pilot.pause()
            gc_screen = app.screen

            # --- Vars: "+" must open PromptScreen, not crash ---
            gc_screen._show_section(1)
            await pilot.pause()
            await pilot.click("#vars_add")
            await pilot.pause()
            assert isinstance(app.screen, PromptScreen), (
                f"expected PromptScreen after '+', got {app.screen!r}"
            )
            await pilot.press("escape")
            await pilot.pause()

            # --- Remotes: "Add" must open AddRemoteScreen, not crash ---
            gc_screen._show_section(2)
            await pilot.pause()
            await pilot.click("#gc_remote_add")
            await pilot.pause()
            assert isinstance(app.screen, AddRemoteScreen), (
                f"expected AddRemoteScreen after 'Add', got {app.screen!r}"
            )
            await pilot.press("escape")
            await pilot.pause()

    asyncio.run(_run())


def _register_workspace(base: Path, name: str, *, alias: str, var_key: str) -> None:
    """Register a workspace that declares `alias` as a repo and overrides
    `var_key` in its own vars — i.e. a real "user" of both."""
    ws_dir = base / name
    ws = WorkspaceConfig(
        repos={alias: BranchSpec("origin/master")},
        templates=["common"],
        vars={var_key: "override"},
    )
    write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
    index.remember(ws_dir)


async def _open_vars_and_select_row(app: DashboardApp, pilot) -> DataTable:
    screen = app.main_screen
    for _ in range(100):
        if not screen._busy:
            break
        await pilot.pause()
    await pilot.press("E")
    await pilot.pause()
    gc_screen = app.screen
    gc_screen._show_section(1)
    await pilot.pause()
    table = gc_screen.query_one("#gc_vars").query_one("#vars_table", DataTable)
    table.cursor_coordinate = table.cursor_coordinate.__class__(0, 0)
    await pilot.pause()
    return table


def test_removing_var_asks_for_confirmation_and_names_the_workspace(xdg, tmp_path):
    """Clicking '-' on a var used by a known workspace must show a
    ConfirmDialog naming that workspace, and cancelling must not remove
    the row."""
    _register_workspace(tmp_path, "voip", alias="community", var_key="http_port")
    app = DashboardApp(_make_config())

    async def _run():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = await _open_vars_and_select_row(app, pilot)
            await pilot.click("#vars_remove")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmDialog)
            details = str(app.screen.query_one("#confirm_details").render())
            assert "voip" in details, f"workspace not named: {details!r}"
            # Cancel — the row must survive.
            await pilot.click("#btn_no")
            await pilot.pause()
            assert table.row_count == 1

    asyncio.run(_run())


def test_confirming_var_removal_removes_the_row(xdg, tmp_path):
    """Confirming the dialog with Yes actually removes the row."""
    app = DashboardApp(_make_config())

    async def _run():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = await _open_vars_and_select_row(app, pilot)
            await pilot.click("#vars_remove")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmDialog)
            await pilot.click("#btn_yes")
            await pilot.pause()
            assert table.row_count == 0

    asyncio.run(_run())


def test_removing_remote_asks_for_confirmation_and_names_the_workspace(xdg, tmp_path):
    """Clicking '- Remove' on a remote alias used by a known workspace
    must show a ConfirmDialog naming that workspace."""
    _register_workspace(tmp_path, "voip", alias="community", var_key="http_port")
    app = DashboardApp(_make_config())

    async def _run():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen = app.main_screen
            for _ in range(100):
                if not screen._busy:
                    break
                await pilot.pause()
            await pilot.press("E")
            await pilot.pause()
            gc_screen = app.screen
            gc_screen._show_section(2)
            await pilot.pause()
            await pilot.click("#gc_remote_remove")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmDialog)
            details = str(app.screen.query_one("#confirm_details").render())
            assert "voip" in details, f"workspace not named: {details!r}"
            await pilot.click("#btn_no")
            await pilot.pause()

    asyncio.run(_run())


def test_removing_unused_var_states_it_is_not_used(xdg, tmp_path):
    """A var no workspace overrides must say so, not name anyone."""
    app = DashboardApp(_make_config())

    async def _run():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _open_vars_and_select_row(app, pilot)
            await pilot.click("#vars_remove")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmDialog)
            details = str(app.screen.query_one("#confirm_details").render())
            assert "Not used by any workspace" in details
            await pilot.click("#btn_no")
            await pilot.pause()

    asyncio.run(_run())
