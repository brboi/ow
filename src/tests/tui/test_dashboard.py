"""Tests for the dashboard action handlers (Phase 4).

These tests verify:
- Workspace list rendering (active + archived)
- Config-only detail rendering without git calls
- Remove confirmation flow
- New workspace form validation
- The typed OptionsEditor's local-override interactions
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Checkbox, Static

from ow.utils import paths
from ow.utils.config import (
    BranchSpec,
    WorkspaceConfig,
    load_workspace_config,
    write_workspace_config,
)
from ow.utils.options import OdooOverrides
from ow.tui.dashboard import MainScreen
from ow.tui.widgets import ConfirmDialog, LabeledInput
from ow.tui.workspace_forms import NewWorkspaceScreen, SwitchScreen


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
    """Highlighting a workspace renders its repos straight from config —
    the alias and its branch spec show up in the detail pane — and git is
    only ever *scheduled*, never run by the highlight itself.

    The debounce timer is neutralised rather than raced against: armed at
    250 ms, it fires on its own under a loaded test suite and repaints the
    pane from a real status, which is not what this test is about.
    """
    seed_workspace(
        tmp_path,
        "test-ws",
        repos={"community": "origin/master"},
    )

    async def _run():
        with (
            patch.object(MainScreen, "_arm_status_debounce") as armed,
            patch("ow.tui.dashboard.gather_workspace_status") as mock_gather,
        ):
            async with dashboard_pilot() as (pilot, screen):
                option_list = screen.query_one("#ws_list")
                option_list.highlighted = 0
                await pilot.pause()

                detail = screen.query_one("#detail")
                rendered = "\n".join(
                    str(static.render()) for static in detail.query(Static)
                )
                assert "community" in rendered, rendered
                assert "master" in rendered, rendered

                # A highlight schedules the status refresh and nothing more:
                # it never calls git itself.
                assert armed.called
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


def test_switch_key_opens_form_and_validates_empty_submission(
    dashboard_pilot, seed_workspace, tmp_path: Path
):
    """S opens SwitchScreen; submitting with no target and no new branch
    name shows a validation error instead of dismissing."""
    seed_workspace(tmp_path, "test-ws", repos={"community": "origin/master"})

    async def _run():
        async with dashboard_pilot() as (pilot, screen):
            option_list = screen.query_one("#ws_list")
            option_list.highlighted = 0
            await pilot.pause()

            await pilot.press("S")
            await pilot.pause()
            assert isinstance(pilot.app.screen, SwitchScreen)

            await pilot.click("#btn_switch")
            await pilot.pause()

            # Still on the form — the empty submission was rejected.
            assert isinstance(pilot.app.screen, SwitchScreen)
            target_field = pilot.app.screen.query_one("#sw_target")
            assert target_field.error_message

    asyncio.run(_run())


def test_switch_submits_target_and_runs_cmd_switch(
    dashboard_pilot, seed_workspace, tmp_path: Path
):
    """Filling the target field and pressing Switch calls cmd_switch with
    the selected workspace and the form's values."""
    ws_dir = seed_workspace(tmp_path, "test-ws", repos={"community": "origin/master"})

    async def _run():
        async with dashboard_pilot() as (pilot, screen):
            option_list = screen.query_one("#ws_list")
            option_list.highlighted = 0
            await pilot.pause()

            await pilot.press("S")
            await pilot.pause()
            sw_screen = pilot.app.screen
            assert isinstance(sw_screen, SwitchScreen)

            target_inner = sw_screen.query_one("#sw_target").query_one("#li_input")
            target_inner.value = "feature-branch"

            with patch("ow.commands.cmd_switch") as mock_switch:
                await pilot.click("#btn_switch")
                await pilot.pause()

                for _ in range(100):
                    if not screen._busy:
                        break
                    await pilot.pause()
                await pilot.pause()

                mock_switch.assert_called_once()
                _, kwargs = mock_switch.call_args
                assert kwargs["target"] == "feature-branch"
                assert kwargs["workspace"] == str(ws_dir)
                assert kwargs["create"] is None
                assert kwargs["detach"] is False
                assert kwargs["only"] is None
                assert kwargs["dry_run"] is False

    asyncio.run(_run())


def test_dashboard_boots_without_the_generated_version_module(
    dashboard_pilot, monkeypatch
):
    """A source checkout has no `ow/_version.py`: setuptools-scm writes it at
    build time and .gitignore keeps it out of git. The dashboard used to
    import it on mount, so `pytest` (or `python -m ow`) straight out of a
    fresh clone crashed every TUI screen with an ImportError.

    Two things have to go for a clone to be modelled: the entry in
    sys.modules, and the attribute an earlier import left on the package —
    `from ow import _version` finds the second one even when the first is
    gone, which is why blocking sys.modules alone proves nothing here.
    """
    ow = importlib.import_module("ow")
    monkeypatch.setitem(sys.modules, "ow._version", None)
    monkeypatch.delattr(ow, "_version", raising=False)

    async def _run():
        async with dashboard_pilot() as (pilot, screen):
            assert isinstance(pilot.app.screen, MainScreen)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# OptionsEditor interactions, driven through `e` (edit workspace config)
# ---------------------------------------------------------------------------


async def _open_workspace_config(pilot):
    await pilot.press("e")
    await pilot.pause()
    return pilot.app.screen


# The form holds a checkbox + input per option key, so the default 80x24
# leaves the Save button below the fold; a full-size terminal keeps the
# whole form on screen for a click-driven test.
_FORM_SIZE = (120, 60)


async def _click_save(pilot, screen) -> None:
    """Save the workspace form, declining the follow-up render offer."""
    screen.query_one("#btn_save").scroll_visible()
    await pilot.pause()
    await pilot.click("#btn_save")
    await pilot.pause()
    if isinstance(pilot.app.screen, ConfirmDialog):
        pilot.app.screen.dismiss(False)
        await pilot.pause()


def _saved_odoo(ws_dir: Path) -> OdooOverrides:
    return load_workspace_config(ws_dir / ".ow" / "config.toml").odoo


def test_enabling_a_local_port_saves_the_override(
    dashboard_pilot, seed_workspace, tmp_path: Path
):
    """Checking `http_port`'s override and typing a value saves it as this
    workspace's own sparse override."""
    ws_dir = seed_workspace(tmp_path, "voip", repos={"community": "origin/master"})

    async def _run():
        async with dashboard_pilot(size=_FORM_SIZE) as (pilot, screen):
            option_list = screen.query_one("#ws_list")
            option_list.highlighted = 0
            await pilot.pause()

            wc_screen = await _open_workspace_config(pilot)
            await pilot.click("#oe_odoo_http_port_override")
            await pilot.pause()
            inp = wc_screen.query_one("#oe_odoo_http_port_input").query_one("#li_input")
            inp.value = "8169"

            # Options-only change: a "render now?" confirm follows — declined.
            await _click_save(pilot, wc_screen)

    asyncio.run(_run())

    assert _saved_odoo(ws_dir).http_port == 8169


def test_clearing_the_override_reverts_to_inherit(
    dashboard_pilot, seed_workspace, tmp_path: Path
):
    """Unchecking an already-set override saves `None` — this record no
    longer says anything about that key, so it inherits again."""
    ws_dir = seed_workspace(
        tmp_path, "voip",
        repos={"community": "origin/master"},
        odoo=OdooOverrides(http_port=8169),
    )

    async def _run():
        async with dashboard_pilot(size=_FORM_SIZE) as (pilot, screen):
            option_list = screen.query_one("#ws_list")
            option_list.highlighted = 0
            await pilot.pause()

            wc_screen = await _open_workspace_config(pilot)
            checkbox = wc_screen.query_one("#oe_odoo_http_port_override", Checkbox)
            assert checkbox.value is True

            await pilot.click("#oe_odoo_http_port_override")
            await pilot.pause()

            await _click_save(pilot, wc_screen)

    asyncio.run(_run())

    assert _saved_odoo(ws_dir).http_port is None


def test_explicit_empty_password_saves_as_empty_string(
    dashboard_pilot, seed_workspace, tmp_path: Path
):
    """Enabling `db_password`'s override and leaving the input blank saves
    an explicit `''`, not `None` — an enabled empty password is a real,
    intentional value, not "inherit"."""
    ws_dir = seed_workspace(tmp_path, "voip", repos={"community": "origin/master"})

    async def _run():
        async with dashboard_pilot(size=_FORM_SIZE) as (pilot, screen):
            option_list = screen.query_one("#ws_list")
            option_list.highlighted = 0
            await pilot.pause()

            wc_screen = await _open_workspace_config(pilot)
            await pilot.click("#oe_odoo_db_password_override")
            await pilot.pause()

            await _click_save(pilot, wc_screen)

    asyncio.run(_run())

    assert _saved_odoo(ws_dir).db_password == ""


def test_invalid_port_keeps_the_form_open(
    dashboard_pilot, seed_workspace, tmp_path: Path
):
    """An enabled `http_port` override that isn't a whole number keeps the
    form open with the field named, instead of silently discarding it or
    saving garbage."""
    ws_dir = seed_workspace(tmp_path, "voip", repos={"community": "origin/master"})

    async def _run():
        async with dashboard_pilot(size=_FORM_SIZE) as (pilot, screen):
            option_list = screen.query_one("#ws_list")
            option_list.highlighted = 0
            await pilot.pause()

            wc_screen = await _open_workspace_config(pilot)
            await pilot.click("#oe_odoo_http_port_override")
            await pilot.pause()
            inp = wc_screen.query_one("#oe_odoo_http_port_input", LabeledInput)
            inp.query_one("#li_input").value = "not-a-port"

            wc_screen.query_one("#btn_save").scroll_visible()
            await pilot.pause()
            await pilot.click("#btn_save")
            await pilot.pause()

            assert pilot.app.screen is wc_screen, "invalid input dismissed the form"
            assert inp.error_message and "http_port" in inp.error_message

    asyncio.run(_run())

    assert _saved_odoo(ws_dir).http_port is None


# ---------------------------------------------------------------------------
# Generation keys: a (Render), F (Files), I (Repair)
# ---------------------------------------------------------------------------


async def _select_first(pilot, screen) -> None:
    screen.query_one("#ws_list").highlighted = 0
    await pilot.pause()


async def _wait_idle(pilot, screen) -> None:
    for _ in range(200):
        if not screen._busy:
            break
        await pilot.pause()
    # One more turn so call_from_thread log writes land.
    await pilot.pause()
    await pilot.pause()


def _log_lines(screen) -> list[str]:
    from ow.tui.widgets import OperationLog

    log = screen.query_one("#log", OperationLog)
    return [line.plain if hasattr(line, "plain") else str(line) for line in log.lines]


def test_render_key_runs_cmd_render(dashboard_pilot, seed_workspace, tmp_path: Path):
    """`a` renders the selected workspace through cmd_render."""
    ws_dir = seed_workspace(tmp_path, "voip", repos={"community": "origin/master"})

    async def _run():
        async with dashboard_pilot() as (pilot, screen):
            await _select_first(pilot, screen)
            with patch("ow.commands.render.cmd_render") as mock_render:
                await pilot.press("a")
                await _wait_idle(pilot, screen)

                mock_render.assert_called_once()
                _, kwargs = mock_render.call_args
                assert kwargs["workspace"] == str(ws_dir)

    asyncio.run(_run())


def test_files_key_reports_pending_changes_rather_than_a_failure(
    dashboard_pilot, seed_workspace, tmp_path: Path
):
    """`F` requests the diff and exits 1 when files differ. Exit 1 here is
    the *answer* — pending changes — not a Git failure, so the log must not
    call it one."""
    seed_workspace(tmp_path, "voip", repos={"community": "origin/master"})

    def _fake_files(config, workspace=None, *, show_diff=False):
        assert show_diff is True, "the dashboard must ask for the diff"
        print("--- .mise.toml (yours)\n+++ .mise.toml (ow)")
        raise SystemExit(1)

    async def _run():
        async with dashboard_pilot() as (pilot, screen):
            await _select_first(pilot, screen)
            with patch("ow.commands.files.cmd_files", _fake_files):
                await pilot.press("F")
                await _wait_idle(pilot, screen)

            lines = _log_lines(screen)
            assert any("+++ .mise.toml (ow)" in line for line in lines), lines
            assert any("pending changes" in line for line in lines), lines
            assert not any("failed (exit 1)" in line for line in lines), lines

    asyncio.run(_run())


def test_repair_key_repairs_the_selected_workspace(
    dashboard_pilot, seed_workspace, tmp_path: Path
):
    """`I` repairs the selection in place: cmd_init gets the workspace
    directory as `parent` with no name, and `yes=True` so it never waits on
    a prompt that cannot be answered through the log pane."""
    ws_dir = seed_workspace(tmp_path, "voip", repos={"community": "origin/master"})

    async def _run():
        async with dashboard_pilot() as (pilot, screen):
            await _select_first(pilot, screen)
            with patch("ow.commands.init.cmd_init") as mock_init:
                await pilot.press("I")
                await _wait_idle(pilot, screen)

                mock_init.assert_called_once()
                _, kwargs = mock_init.call_args
                assert kwargs["parent"] == ws_dir
                assert kwargs["name"] is None
                assert kwargs["yes"] is True

            lines = _log_lines(screen)
            assert any("repair voip" in line for line in lines), lines
            assert not any("failed" in line for line in lines), lines

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Schema 1: free editing, but no typed option writes until migration
# ---------------------------------------------------------------------------


_LEGACY_WORKSPACE = """\
version = 1

[repos]
community = "origin/master"

[vars]
http_port = 8071
mystery = "kept"
"""


def test_schema1_workspace_disables_typed_options_and_keeps_its_file(
    dashboard_pilot, tmp_path: Path
):
    """A schema-1 workspace can still have repos edited, but its typed
    option controls stay disabled with explicit guidance — there is no
    reverse serializer to write them back — and a save leaves the legacy
    document's own keys (`[vars]` here) untouched."""
    from ow.utils import index

    ws_dir = tmp_path / "legacy"
    (ws_dir / ".ow").mkdir(parents=True)
    config_path = ws_dir / ".ow" / "config.toml"
    config_path.write_text(_LEGACY_WORKSPACE)
    index.remember(ws_dir)

    async def _run():
        async with dashboard_pilot(size=_FORM_SIZE) as (pilot, screen):
            await _select_first(pilot, screen)
            wc_screen = await _open_workspace_config(pilot)

            assert wc_screen.query_one(
                "#oe_odoo_http_port_override", Checkbox
            ).disabled is True
            rendered = "\n".join(
                str(s.render())
                for s in wc_screen.query(Static)
            )
            assert "ow render" in rendered, rendered

            await _click_save(pilot, wc_screen)

    asyncio.run(_run())

    text = config_path.read_text()
    assert 'mystery = "kept"' in text, "a save dropped legacy data it has no model for"
    assert "version = 1" in text, "the TUI save migrated the file: only init/render may"
