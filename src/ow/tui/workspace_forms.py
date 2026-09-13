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
from typing import Any, Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Label, SelectionList, Static

from ow.utils.config import (
    BranchSpec,
    Config,
    WorkspaceConfig,
    parse_branch_spec,
)
from ow.utils.templates import available_templates
from ow.tui.widgets import ConfirmDialog, LabeledInput


# ---------------------------------------------------------------------------
# Shared CSS for modal form screens (NewWorkspaceScreen, WorkspaceConfigScreen)
# ---------------------------------------------------------------------------

_FORM_SCREEN_CSS = """
{cls} {{
    align: center middle;
}}
{cls} > VerticalScroll {{
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
{cls} .section-hint {{
    color: $text-muted;
}}
{cls} > VerticalScroll > Horizontal {{
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
        with VerticalScroll():
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

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Submit form when Enter is pressed in any input field."""
        self._try_create()

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
        with VerticalScroll():
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
            yield Static(
                "Copied from the global config when this workspace was created; "
                "these values are now this workspace's own — editing global vars "
                "later won't change them.",
                classes="section-hint",
            )
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

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Submit form when Enter is pressed in any input field."""
        self._try_save()

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
# SwitchRequest / SwitchScreen — `ow switch` form
# ---------------------------------------------------------------------------


@dataclass
class SwitchRequest:
    """What the user filled in on the switch form."""

    target: str | None
    create: str | None
    detach: bool


class SwitchScreen(ModalScreen[SwitchRequest | None]):
    """Form for `ow switch`: target branch, optional new-branch name (-c),
    and a --detach checkbox.

    Dismisses with a `SwitchRequest` on Switch, None on Cancel.
    """

    DEFAULT_CSS = """
    SwitchScreen {
        align: center middle;
    }
    SwitchScreen > Vertical {
        width: 60;
        height: auto;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    SwitchScreen Checkbox {
        margin: 1 0 0 4;
    }
    SwitchScreen > Vertical > Horizontal {
        align: center middle;
        height: auto;
        margin-top: 1;
    }
    SwitchScreen Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss(None)", "", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            LabeledInput(
                "target branch",
                placeholder="e.g. main, or a start point with -c below",
                id="sw_target",
            ),
            LabeledInput(
                "-c new branch",
                placeholder="(optional) create this branch and switch to it",
                id="sw_create",
            ),
            Checkbox("detach", id="sw_detach"),
            Horizontal(
                Button("Switch", id="btn_switch", variant="success"),
                Button("Cancel", id="btn_cancel", variant="error"),
            ),
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_switch":
            self._try_submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._try_submit()

    def _try_submit(self) -> None:
        target_input = self.query_one("#sw_target", LabeledInput)
        target = target_input.value.strip() or None
        create = self.query_one("#sw_create", LabeledInput).value.strip() or None
        detach = self.query_one("#sw_detach", Checkbox).value

        if target is None and create is None:
            target_input.set_error("target branch or -c new branch is required")
            return
        target_input.set_error(None)

        self.dismiss(SwitchRequest(target=target, create=create, detach=detach))


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

    def __init__(
        self,
        initial: dict[str, Any],
        *,
        usage_describer: Callable[[str], str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._initial = initial
        self._usage_describer = usage_describer

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
            key = table.get_cell_at(Coordinate(row_idx, 0))
            val_text = table.get_cell_at(Coordinate(row_idx, 1))
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
        def _on_result(result: str | None) -> None:
            if result is None:
                return
            key = result.strip()
            if not key:
                return
            table = self.query_one("#vars_table", DataTable)
            table.add_row(key, "")
        self.app.push_screen(PromptScreen("key"), callback=_on_result)

    def _remove_row(self) -> None:
        table = self.query_one("#vars_table", DataTable)
        cursor = table.cursor_coordinate
        if cursor is None or cursor.row is None:
            self.app.notify("No row selected", severity="warning")
            return
        try:
            row_key = table.coordinate_to_cell_key(cursor)[0]
            key = str(table.get_cell_at(Coordinate(cursor.row, 0)))
        except Exception:
            self.app.notify("Could not remove row", severity="warning")
            return

        def _on_confirmed(ok: bool) -> None:
            if not ok:
                return
            try:
                table.remove_row(row_key)
            except Exception:
                self.app.notify("Could not remove row", severity="warning")

        details = self._usage_describer(key) if self._usage_describer else None
        self.app.push_screen(
            ConfirmDialog(f"Remove var {key!r}?", details=details),
            callback=_on_confirmed,
        )

    def action_edit_cell(self) -> None:
        table = self.query_one("#vars_table", DataTable)
        cursor = table.cursor_coordinate
        if cursor is None:
            self.app.notify("No row selected", severity="warning")
            return
        row_idx = cursor.row
        col_idx = cursor.column
        try:
            current = str(table.get_cell_at(cursor))
            row_key = table.coordinate_to_cell_key(cursor)[0]
        except Exception:
            self.app.notify("Could not read cell", severity="warning")
            return

        def _on_result(result: str | None) -> None:
            if result is None:
                return
            table = self.query_one("#vars_table", DataTable)
            try:
                # Update cell in-place — preserves row position and cursor
                table.update_cell(row_key, table.columns[col_idx].key, result)
            except Exception:
                pass
        label = "value" if col_idx == 1 else "key"
        self.app.push_screen(PromptScreen(label, default=current), callback=_on_result)


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
        value = tomllib.loads(f"_ = {text}")["_"]
        # Restrict to scalar types only — dates, arrays, tables corrupt config
        if isinstance(value, (str, int, float, bool)):
            return value
    except Exception:
        pass
    return text
