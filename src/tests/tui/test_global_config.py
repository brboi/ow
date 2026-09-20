"""Tests for the global config screen: its five sections, modal-opening
buttons, confirm-before-removal for remotes, the typed override summary,
the ignore pattern list, and header geometry.

`push_screen_wait` requires an active Textual worker context. Called
directly from a plain button-press handler — which is never a worker —
it used to raise `NoActiveWorker` and crash. Every one of these buttons
now opens its modal with `push_screen(screen, callback=...)` instead.

Removing a remote used to happen immediately, with no confirmation and
no indication whether anything still depended on it; it now goes through
`ConfirmDialog`, annotated with which known workspaces use it.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from textual.widgets import Button, Checkbox, Static

from ow.utils.config import (
    Config,
    RemoteConfig,
    load_global_config,
    write_global_config,
)
from ow.utils.options import OdooOverrides
from ow.tui.global_config import AddRemoteScreen, GlobalConfigScreen
from ow.tui.widgets import ConfirmDialog, LabeledInput
from ow.tui.workspace_forms import PromptScreen


# Section indexes of GlobalConfigScreen's sidebar.
_EDITOR, _ODOO, _MISE, _IGNORE, _REMOTES = range(5)

# The option panels are tall: a taller pilot keeps the widgets the test
# clicks on inside the visible region.
_SIZE = (120, 60)


def _global_config() -> Config:
    return Config(
        remotes={"community": {"origin": RemoteConfig(url="git@github.com:odoo/odoo.git")}},
    )


async def _open_section(pilot, index: int):
    await pilot.press("E")
    await pilot.pause()
    gc_screen = pilot.app.screen
    gc_screen._show_section(index)
    await pilot.pause()
    return gc_screen


def test_remote_add_opens_form_without_crash(dashboard_pilot):
    """'+ Add' in Remotes opens AddRemoteScreen instead of raising
    NoActiveWorker."""

    async def _run():
        async with dashboard_pilot(size=_SIZE) as (pilot, screen):
            gc_screen = await _open_section(pilot, _REMOTES)
            await pilot.click("#gc_remote_add")
            await pilot.pause()
            assert isinstance(pilot.app.screen, AddRemoteScreen), (
                f"expected AddRemoteScreen after 'Add', got {pilot.app.screen!r}"
            )
            await pilot.press("escape")
            await pilot.pause()

    asyncio.run(_run())


def test_removing_remote_asks_for_confirmation_and_names_the_workspace(
    dashboard_pilot, seed_workspace, tmp_path: Path
):
    """Clicking '- Remove' on a remote alias used by a known workspace
    must show a ConfirmDialog naming that workspace."""
    seed_workspace(tmp_path, "voip", repos={"community": "origin/master"})

    async def _run():
        async with dashboard_pilot(size=_SIZE) as (pilot, screen):
            gc_screen = await _open_section(pilot, _REMOTES)
            await pilot.click("#gc_remote_remove")
            await pilot.pause()
            assert isinstance(pilot.app.screen, ConfirmDialog)
            details = str(pilot.app.screen.query_one("#confirm_details").render())
            assert "voip" in details, f"workspace not named: {details!r}"
            await pilot.click("#btn_no")
            await pilot.pause()

    asyncio.run(_run())


def test_odoo_section_reports_typed_override_usage(
    dashboard_pilot, seed_workspace, tmp_path: Path
):
    """The Odoo panel replaces the old per-var usage describer with a typed
    count: it names the workspaces that override an odoo option locally,
    and says so plainly when none do."""
    seed_workspace(
        tmp_path, "voip",
        repos={"community": "origin/master"},
        odoo=OdooOverrides(http_port=8169),
    )

    async def _run():
        async with dashboard_pilot(size=_SIZE) as (pilot, screen):
            gc_screen = await _open_section(pilot, _ODOO)
            panel = gc_screen.query_one("#gc_panel_odoo")
            rendered = "\n".join(str(s.render()) for s in panel.query(Static))
            assert "voip" in rendered, rendered
            assert "override odoo options locally" in rendered, rendered

            gc_screen._show_section(_MISE)
            await pilot.pause()
            mise_rendered = "\n".join(
                str(s.render()) for s in gc_screen.query_one("#gc_panel_mise").query(Static)
            )
            assert "No known workspace overrides mise options locally." in mise_rendered

    asyncio.run(_run())


def test_ignore_pattern_list_is_ordered_and_saved(dashboard_pilot):
    """Ignore is an ordered pattern list: a pattern added through the prompt
    lands at the end, and Ctrl+S persists the whole list in that order."""
    seeded = Config(
        remotes={"community": {"origin": RemoteConfig(url="git@github.com:odoo/odoo.git")}},
        owignore=("*.log",),
    )

    async def _run():
        async with dashboard_pilot(seeded, size=_SIZE) as (pilot, screen):
            gc_screen = await _open_section(pilot, _IGNORE)
            await pilot.click("#ignore_add")
            await pilot.pause()
            assert isinstance(pilot.app.screen, PromptScreen)
            pilot.app.screen.query_one("#prompt_input").query_one("#li_input").value = "*.tmp"
            await pilot.click("#btn_ok")
            await pilot.pause()

            await pilot.click("#btn_save")
            await pilot.pause()

    asyncio.run(_run())

    assert load_global_config().owignore == ("*.log", "*.tmp")


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


def test_editing_the_global_port_leaves_a_local_override_alone(
    dashboard_pilot, seed_workspace, tmp_path: Path
):
    """Changing the global http_port saves the global file only: a
    workspace whose own config overrides that key keeps its value, and
    keeps it on disk untouched — inheritance is resolved at render time,
    never copied into anyone's file by a global save."""
    ws_dir = seed_workspace(
        tmp_path, "voip",
        repos={"community": "origin/master"},
        odoo=OdooOverrides(http_port=8169),
    )
    ws_config_path = ws_dir / ".ow" / "config.toml"
    before = ws_config_path.read_bytes()

    async def _run():
        async with dashboard_pilot(size=_SIZE) as (pilot, screen):
            gc_screen = await _open_section(pilot, _ODOO)
            checkbox = gc_screen.query_one("#oe_odoo_http_port_override", Checkbox)
            assert checkbox.value is True, "the global config's own port override"
            port_input = gc_screen.query_one("#oe_odoo_http_port_input", LabeledInput)
            assert port_input.value == "8069"

            port_input.query_one("#li_input").value = "8180"
            await pilot.click("#btn_save")
            await pilot.pause()
            assert pilot.app.screen is screen, "save should dismiss the global screen"

    asyncio.run(_run())

    assert load_global_config().odoo.http_port == 8180
    assert ws_config_path.read_bytes() == before, (
        "a global save rewrote a workspace's config file"
    )

def test_schema1_global_disables_typed_options_and_keeps_its_file(
    xdg, dashboard_pilot
):
    """A schema-1 global config keeps its editor/remotes editable, disables
    the typed odoo/mise/ignore controls with migration guidance, and its
    unmodelled keys survive a save."""
    from ow.utils import paths
    from ow.utils.config import load_config

    path = paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# my notes\nversion = 1\n\n[vars]\nhttp_port = 8071\nmystery = \"kept\"\n\n"
        "[remotes.community]\norigin.url = \"git@github.com:odoo/odoo.git\"\n"
    )
    config = load_config(path)
    assert config.version == 1

    async def _run():
        async with dashboard_pilot(config, size=_SIZE) as (pilot, screen):
            gc_screen = await _open_section(pilot, _ODOO)
            assert gc_screen.query_one(
                "#oe_odoo_http_port_override", Checkbox
            ).disabled is True
            panels = [
                "\n".join(
                    str(s.render())
                    for s in gc_screen.query_one(pid).query(Static)
                )
                for pid in ("#gc_panel_odoo", "#gc_panel_mise", "#gc_panel_ignore")
            ]
            for rendered in panels:
                assert "ow render" in rendered, rendered

            # Editor is still editable — only the typed tables are frozen.
            gc_screen._show_section(_EDITOR)
            await pilot.pause()
            gc_screen.query_one("#gc_editor").query_one("#li_input").value = "nvim"
            await pilot.click("#btn_save")
            await pilot.pause()
            assert pilot.app.screen is screen, "save should dismiss the global screen"

    asyncio.run(_run())

    text = path.read_text()
    assert 'editor = "nvim"' in text
    assert "# my notes" in text
    assert "version = 1" in text, "the TUI save migrated the file: only init/render may"
    assert 'mystery = "kept"' in text, "a save dropped legacy data it has no model for"


def test_saving_defaults_keeps_a_theme_committed_while_the_modal_was_open(
    xdg, dashboard_pilot
):
    """The modal is opened with a snapshot; the theme is not its business.

    A palette theme commit writes the file and swaps the holder while the
    global screen sits open on top. Saving the screen must apply only the
    sections it owns onto the record the holder has *now* — writing the
    open-time snapshot back would silently revert the theme the user just
    chose.
    """
    write_global_config(replace(_global_config(), theme="textual-dark"))
    config = load_global_config()

    async def _run():
        async with dashboard_pilot(config, size=_SIZE) as (pilot, screen):
            gc_screen = await _open_section(pilot, _EDITOR)
            assert pilot.app.theme == "textual-dark"

            # The palette's commit path: set the reactive, let the watcher
            # persist it, exactly as a theme selection does.
            pilot.app.theme = "nord"
            await pilot.pause()
            assert load_global_config().theme == "nord", "watcher did not persist"

            gc_screen.query_one("#gc_editor").query_one("#li_input").value = "vim"
            await pilot.click("#btn_save")
            await pilot.pause()

            assert pilot.app._config_holder.value.theme == "nord", (
                "the holder was left holding the modal's stale theme"
            )

    asyncio.run(_run())

    reloaded = load_global_config()
    assert reloaded.theme == "nord", (
        "saving the modal reverted the theme committed while it was open"
    )
    assert reloaded.editor == "vim"


def test_ignore_edit_in_place_opens_on_enter(dashboard_pilot):
    """Enter on the focused ignore table opens the edit prompt for the
    highlighted row (the table's own Enter binding wins, or the affordance
    is dead), and the edit is what gets saved."""
    seeded = Config(
        remotes={"community": {"origin": RemoteConfig(url="git@github.com:odoo/odoo.git")}},
        owignore=("*.log",),
    )

    async def _run():
        async with dashboard_pilot(seeded, size=_SIZE) as (pilot, screen):
            gc_screen = await _open_section(pilot, _IGNORE)
            table = gc_screen.query_one("#ignore_table")

            # Tab to the table the way a user does — Save, Cancel, the section
            # list, the panel scroller, then the table — and assert it really
            # is the focused widget before pressing the key.
            for _ in range(4):
                await pilot.press("tab")
                await pilot.pause()
            assert pilot.app.focused is table, (
                f"tabbing never reaches the ignore table, focus is {pilot.app.focused!r}"
            )

            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(pilot.app.screen, PromptScreen), (
                f"Enter did not open the edit prompt, got {pilot.app.screen!r}"
            )
            prompt_input = pilot.app.screen.query_one("#prompt_input")
            assert prompt_input.value == "*.log", "the prompt must carry the row's value"
            prompt_input.query_one("#li_input").value = "*.bak"
            await pilot.click("#btn_ok")
            await pilot.pause()

            await pilot.click("#btn_save")
            await pilot.pause()

    asyncio.run(_run())

    assert load_global_config().owignore == ("*.bak",)
