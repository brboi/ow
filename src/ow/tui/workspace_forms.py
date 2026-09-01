"""Modal screens for workspace creation and editing.

These screens gather user input and dismiss with data objects; the
dashboard's key handlers consume the results and run the actual
operations.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Label, SelectionList, Static

from ow.utils.config import (
    BranchSpec,
    Config,
    WorkspaceConfig,
    parse_branch_spec,
)
from ow.utils.templates import available_templates
from ow.tui.widgets import LabeledInput


# ---------------------------------------------------------------------------
# Shared CSS for modal form screens (NewWorkspaceScreen, WorkspaceConfigScreen)
# ---------------------------------------------------------------------------

_FORM_SCREEN_CSS = """
{cls} {{
    align: center middle;
}}
{cls} > Vertical {{
    width: 80;
    max-height: 90%;
    padding: 1 2;
    border: round $primary;
    background: $surface;
}}
{cls} .section-heading {{
    text-style: bold;
    margin: 1 0 0 0;
}}
{cls} > Vertical > Horizontal {{
    align: center middle;
    height: auto;
    margin-top: 1;
}}
{cls} Button {{
    margin: 0 1;
}}
"""


# ---------------------------------------------------------------------------
# PromptScreen — single input + OK/Cancel
# ---------------------------------------------------------------------------


class PromptScreen(ModalScreen[str | None]):
    """A single `LabeledInput` + OK/Cancel.

    Dismisses with the entered string on OK, None on Cancel/Escape.
    """

    DEFAULT_CSS = """
    PromptScreen {
        align: center middle;
    }
    PromptScreen > Vertical {
        width: 60;
        height: auto;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    PromptScreen > Vertical > Horizontal {
        align: center middle;
        height: auto;
        margin-top: 1;
    }
    PromptScreen Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss(None)", "", show=False),
    ]

    def __init__(self, title: str, default: str = "") -> None:
        super().__init__()
        self._title = title
        self._default = default

    def compose(self) -> ComposeResult:
        yield Vertical(
            LabeledInput(self._title, value=self._default, id="prompt_input"),
            Horizontal(
                Button("OK", id="btn_ok", variant="success"),
                Button("Cancel", id="btn_cancel", variant="error"),
            ),
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_ok":
            inp = self.query_one("#prompt_input", LabeledInput)
            self.dismiss(inp.value)
        else:
            self.dismiss(None)

    def on_input_submitted(self, event) -> None:
        inp = self.query_one("#prompt_input", LabeledInput)
        self.dismiss(inp.value)


# ---------------------------------------------------------------------------
# NewWorkspaceRequest — the data NewWorkspaceScreen dismisses with
# ---------------------------------------------------------------------------


@dataclass
class NewWorkspaceRequest:
    """What the user filled in on the new-workspace form."""

    parent: Path
    name: str
    templates: list[str]
    repos: dict[str, BranchSpec]
    configuration: str | None


# ---------------------------------------------------------------------------
# NewWorkspaceScreen — create a workspace
# ---------------------------------------------------------------------------


_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


class NewWorkspaceScreen(ModalScreen[NewWorkspaceRequest | None]):
    """Form for creating a new workspace.

    Dismisses with a `NewWorkspaceRequest` on Create, None on Cancel.
    """

    DEFAULT_CSS = _FORM_SCREEN_CSS.format(cls="NewWorkspaceScreen")

    BINDINGS = [
        Binding("escape", "dismiss(None)", "", show=False),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._config = config
        self._templates = available_templates()
        self._aliases = list(config.remotes.keys())

    def compose(self) -> ComposeResult:
        with Vertical():
            yield LabeledInput(
                "parent", value=str(Path.cwd()), id="nw_parent"
            )
            yield LabeledInput("name", id="nw_name")

            yield Static("Templates", classes="section-heading")
            sel = SelectionList[str](id="nw_templates")
            for t in self._templates:
                sel.add_option((t, t, t == "common"))
            yield sel

            yield Static("Repos", classes="section-heading")
            for alias in self._aliases:
                # Pre-fill "master" for the community alias (matches the old
                # preselection); leave others empty so they are not included.
                value = "master" if alias == "community" else ""
                yield LabeledInput(alias, value=value, id=f"nw_spec_{alias}")

            yield LabeledInput("copy config from", id="nw_copy_config")

            yield Horizontal(
                Button("Create", id="btn_create", variant="success"),
                Button("Cancel", id="btn_cancel", variant="error"),
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_create":
            self._try_create()
        else:
            self.dismiss(None)

    def _try_create(self) -> None:
        parent_str = self.query_one("#nw_parent", LabeledInput).value.strip()
        name = self.query_one("#nw_name", LabeledInput).value.strip()

        # Validate name
        name_input = self.query_one("#nw_name", LabeledInput)
        if not name:
            name_input.set_error("name is required")
            return
        if not _NAME_RE.match(name):
            name_input.set_error("only letters, digits, _ and -")
            return
        name_input.set_error(None)

        parent = Path(parent_str).expanduser() if parent_str else Path.cwd()
        ws_dir = parent / name
        config_path = ws_dir / ".ow" / "config.toml"
        if config_path.exists():
            name_input.set_error("workspace already exists")
            return

        # Templates
        tpl_sel = self.query_one("#nw_templates", SelectionList)
        templates = list(tpl_sel.selected)

        # Repos — iterate aliases; empty spec means not included.
        repos: dict[str, BranchSpec] = {}
        for alias in self._aliases:
            inp = self.query_one(f"#nw_spec_{alias}", LabeledInput)
            spec_str = inp.value.strip()
            if not spec_str:
                continue
            try:
                repos[alias] = parse_branch_spec(spec_str)
            except ValueError as exc:
                inp.set_error(str(exc))
                return
            inp.set_error(None)

        # Copy config from
        copy_config = self.query_one("#nw_copy_config", LabeledInput).value.strip() or None

        self.dismiss(NewWorkspaceRequest(
            parent=parent,
            name=name,
            templates=templates,
            repos=repos,
            configuration=copy_config,
        ))


# ---------------------------------------------------------------------------
# WorkspaceConfigScreen — edit a workspace's config
# ---------------------------------------------------------------------------


class WorkspaceConfigScreen(ModalScreen[WorkspaceConfig | None]):
    """Edit a workspace's templates, repos and vars.

    Dismisses with a new `WorkspaceConfig` on Save, None on Cancel.
    """

    DEFAULT_CSS = _FORM_SCREEN_CSS.format(cls="WorkspaceConfigScreen")

    BINDINGS = [
        Binding("escape", "dismiss(None)", "", show=False),
    ]

    def __init__(self, config: Config, ws_dir: Path, ws: WorkspaceConfig) -> None:
        super().__init__()
        self._config = config
        self._ws_dir = ws_dir
        self._ws = ws
        self._aliases = list(config.remotes.keys())

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Templates", classes="section-heading")
            sel = SelectionList[str](id="wc_templates")
            for t in available_templates():
                sel.add_option((t, t, t in self._ws.templates))
            yield sel

            yield Static("Repos", classes="section-heading")
            for alias in self._aliases:
                current = self._ws.repos.get(alias)
                value = current.to_spec_str() if current else ""
                yield LabeledInput(alias, value=value, id=f"wc_spec_{alias}")

            yield Static("Vars", classes="section-heading")
            yield VarsEditor(self._ws.vars, id="wc_vars")

            yield Horizontal(
                Button("Save", id="btn_save", variant="success"),
                Button("Cancel", id="btn_cancel", variant="error"),
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_save":
            self._try_save()
        else:
            self.dismiss(None)

    def _try_save(self) -> None:
        # Templates
        tpl_sel = self.query_one("#wc_templates", SelectionList)
        templates = list(tpl_sel.selected)

        # Repos
        repos: dict[str, BranchSpec] = {}
        for alias in self._aliases:
            inp = self.query_one(f"#wc_spec_{alias}", LabeledInput)
            spec_str = inp.value.strip()
            if not spec_str:
                continue
            try:
                repos[alias] = parse_branch_spec(spec_str)
            except ValueError as exc:
                inp.set_error(str(exc))
                return
            inp.set_error(None)

        # Vars
        vars_editor = self.query_one("#wc_vars", VarsEditor)
        vars_dict = vars_editor.get_vars()

        self.dismiss(WorkspaceConfig(
            repos=repos,
            templates=templates,
            vars=vars_dict,
            version=self._ws.version,
        ))


# ---------------------------------------------------------------------------
# VarsEditor — shared key/value editor used by both config screens
# ---------------------------------------------------------------------------


class VarsEditor(Vertical):
    """A DataTable of key/value pairs with add/remove/edit.

    Value typing: parse the text as a TOML scalar; if that raises, store
    as a string. This keeps `http_port = 8069` an int and
    `db_host = "localhost"` a string without asking the user to quote.
    """

    DEFAULT_CSS = """
    VarsEditor {
        height: auto;
        max-height: 12;
    }
    VarsEditor DataTable {
        height: auto;
        max-height: 8;
    }
    VarsEditor Horizontal {
        height: auto;
    }
    VarsEditor Button {
        margin: 0 1;
        min-width: 4;
    }
    """

    BINDINGS = [
        Binding("enter", "edit_cell", "Edit", show=True),
    ]

    def __init__(self, initial: dict[str, Any], **kwargs) -> None:
        super().__init__(**kwargs)
        self._initial = initial

    def compose(self) -> ComposeResult:
        table = DataTable(id="vars_table")
        table.add_columns("key", "value")
        for k, v in self._initial.items():
            table.add_row(str(k), _format_value(v))
        yield table
        yield Horizontal(
            Button("+", id="vars_add"),
            Button("-", id="vars_remove"),
        )

    def get_vars(self) -> dict[str, Any]:
        """Collect the current table contents as a typed dict."""
        table = self.query_one("#vars_table", DataTable)
        result: dict[str, Any] = {}
        for row_idx in range(table.row_count):
            key = table.get_cell_at(row_idx, 0)
            val_text = table.get_cell_at(row_idx, 1)
            if key is None:
                continue
            key = str(key)
            result[key] = _parse_value(str(val_text))
        return result

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "vars_add":
            self._add_row()
        elif event.button.id == "vars_remove":
            self._remove_row()

    def _add_row(self) -> None:
        async def _prompt() -> None:
            result = await self.app.push_screen_wait(PromptScreen("key"))
            if result is None:
                return
            key = result.strip()
            if not key:
                return
            table = self.query_one("#vars_table", DataTable)
            table.add_row(key, "")
        self.app.call_later(_prompt)

    def _remove_row(self) -> None:
        table = self.query_one("#vars_table", DataTable)
        cursor = table.cursor_coordinate
        if cursor is not None and cursor.row is not None:
            try:
                row_key, _ = table.get_row_at(cursor.row)
                table.remove_row(row_key)
            except Exception:
                pass

    def action_edit_cell(self) -> None:
        table = self.query_one("#vars_table", DataTable)
        cursor = table.cursor_coordinate
        if cursor is None:
            return
        row_idx = cursor.row
        col_idx = cursor.column
        try:
            current = str(table.get_cell_at(row_idx, col_idx))
        except Exception:
            return

        async def _prompt() -> None:
            label = "value" if col_idx == 1 else "key"
            result = await self.app.push_screen_wait(PromptScreen(label, default=current))
            if result is None:
                return
            table = self.query_one("#vars_table", DataTable)
            try:
                row_key, _ = table.get_row_at(row_idx)
                # Rebuild the row with the new value
                other_col = 1 - col_idx
                other_val = str(table.get_cell_at(row_idx, other_col))
                table.remove_row(row_key)
                if col_idx == 0:
                    table.add_row(result, other_val)
                else:
                    table.add_row(other_val, result)
            except Exception:
                pass
        self.app.call_later(_prompt)


def _format_value(v: Any) -> str:
    """Render a Python value as a user-editable string."""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _parse_value(text: str) -> Any:
    """Parse a user-edited string as a TOML scalar; fall back to string."""
    text = text.strip()
    if not text:
        return ""
    try:
        return tomllib.loads(f"_ = {text}")["_"]
    except Exception:
        return text
