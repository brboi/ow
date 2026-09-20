"""Modal screens for workspace creation and editing.

These screens gather user input and dismiss with data objects; the
dashboard's key handlers consume the results and run the actual
operations.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Static

from ow.utils.config import (
    BranchSpec,
    Config,
    WorkspaceConfig,
    parse_branch_spec,
)
from ow.utils.options import (
    MiseOverrides,
    OdooOverrides,
    parse_mise_options,
    parse_odoo_options,
)
from ow.tui.widgets import LabeledInput


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

    def on_input_submitted(self, event: Input.Submitted) -> None:
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
        self._aliases = list(config.remotes.keys())

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield LabeledInput(
                "parent", value=str(Path.cwd()), id="nw_parent"
            )
            yield LabeledInput("name", id="nw_name")

            yield Static("Repos", classes="section-heading")
            for alias in self._aliases:
                yield LabeledInput(alias, id=f"nw_spec_{alias}")

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
            repos=repos,
            configuration=copy_config,
        ))


# ---------------------------------------------------------------------------
# OptionsEditor — typed, sparse odoo/mise override editor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _OptionField:
    """UI metadata for one of the ten fixed odoo keys, or mise.python.

    This is display/parsing metadata for a *closed* set of keys — never a
    user-extensible schema. `kind` picks the parser: `port` and `python`
    convert the typed text before delegating to `options.py`; `str` and
    `password` pass the text through unchanged; `args` splits it on shell
    rules first. `default_display` is the built-in default shown as a hint
    when neither this record nor the layer above it overrides the key; a
    blank one marks a key whose default depends on the detected Odoo core
    rather than being a fixed constant.
    """

    key: str
    kind: str  # "port" | "str" | "password" | "args" | "python"
    default_display: str = ""


_ODOO_FIELDS: tuple[_OptionField, ...] = (
    _OptionField("http_port", "port", "8069"),
    _OptionField("db_host", "str", "localhost"),
    _OptionField("db_port", "port", "5432"),
    _OptionField("db_user", "str", "odoo"),
    _OptionField("db_password", "password"),
    _OptionField("admin_passwd", "password"),
    _OptionField("smtp_server", "str", "localhost"),
    _OptionField("smtp_port", "port", "25"),
    _OptionField("debug_args", "args"),
    _OptionField("debug_test_args", "args"),
)

_MISE_FIELDS: tuple[_OptionField, ...] = (
    _OptionField("python", "python", "3.12"),
)


def _format_field(field: _OptionField, value: Any) -> str:
    if field.kind == "args":
        return shlex.join(value)
    return str(value)


def _validate_odoo_field(field: _OptionField, text: str) -> object:
    if field.kind == "port":
        try:
            raw: object = int(text)
        except ValueError:
            raise ValueError(f"{field.key} must be a whole number") from None
    elif field.kind == "args":
        raw = shlex.split(text)
    else:
        raw = text
    validated = parse_odoo_options({field.key: raw})
    return getattr(validated, field.key)


def _validate_mise_field(field: _OptionField, text: str) -> object:
    validated = parse_mise_options({field.key: text})
    return getattr(validated, field.key)


class OptionsEditor(Vertical):
    """One checkbox + `LabeledInput` row per fixed odoo/mise key.

    Unchecked means "inherit" — the row's own override stays `None`
    regardless of what text sits in its (disabled) input. Checked reads
    whatever text is there, including blank: an enabled password commits
    as `''`, and enabled `debug_args`/`debug_test_args` commit as `()` —
    both explicit, neither `None`. `base_odoo`/`base_mise` is the layer
    immediately above this record (the global config, for a workspace's
    editor); its values show as the row's inherited hint. Left at their
    `None` default, every row hints the built-in default (or, for the two
    core-dependent debug-arg keys, that the default is automatic).

    `disabled=True` (a schema-1 record) freezes every row so nothing here
    can be changed until the record is migrated to schema 2.
    """

    DEFAULT_CSS = """
    OptionsEditor {
        height: auto;
    }
    OptionsEditor .option-row {
        height: auto;
    }
    OptionsEditor .option-row Checkbox {
        width: auto;
        padding: 0 1 0 0;
    }
    OptionsEditor .option-row LabeledInput {
        width: 1fr;
    }
    """

    def __init__(
        self,
        *,
        odoo: OdooOverrides = OdooOverrides(),
        mise: MiseOverrides = MiseOverrides(),
        base_odoo: OdooOverrides | None = None,
        base_mise: MiseOverrides | None = None,
        include_odoo: bool = True,
        include_mise: bool = True,
        disabled: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._odoo = odoo
        self._mise = mise
        self._base_odoo = base_odoo
        self._base_mise = base_mise
        self._include_odoo = include_odoo
        self._include_mise = include_mise
        self._disabled = disabled

    def _hint(self, table: str, field: _OptionField) -> tuple[str, str]:
        base_val = (
            self._base_mise.python
            if table == "mise" and self._base_mise is not None
            else getattr(self._base_odoo, field.key)
            if table == "odoo" and self._base_odoo is not None
            else None
        )
        if base_val is not None:
            if field.kind == "password":
                return "(set)", "global"
            return _format_field(field, base_val), "global"
        if field.kind == "password":
            return "(unset)", "built-in"
        return field.default_display or "auto", "built-in"

    def _row(self, table: str, field: _OptionField, local_value: Any) -> Widget:
        hint, source = self._hint(table, field)
        overridden = local_value is not None
        text_value = "" if local_value is None else _format_field(field, local_value)
        checkbox_id = f"oe_{table}_{field.key}_override"
        input_id = f"oe_{table}_{field.key}_input"
        return Horizontal(
            Checkbox(
                "override",
                value=overridden,
                disabled=self._disabled,
                id=checkbox_id,
            ),
            LabeledInput(
                field.key,
                value=text_value,
                placeholder=f"{hint} ({source})",
                enabled=overridden and not self._disabled,
                password=(field.kind == "password"),
                id=input_id,
            ),
            classes="option-row",
        )

    def compose(self) -> ComposeResult:
        if self._include_odoo:
            for field in _ODOO_FIELDS:
                yield self._row("odoo", field, getattr(self._odoo, field.key))
        if self._include_mise:
            for field in _MISE_FIELDS:
                yield self._row("mise", field, self._mise.python)

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        cid = event.checkbox.id or ""
        if not (cid.startswith("oe_") and cid.endswith("_override")):
            return
        input_id = cid[: -len("_override")] + "_input"
        try:
            li = self.query_one(f"#{input_id}", LabeledInput)
        except Exception:
            return
        li.set_enabled(event.value)
        if not event.value:
            li.set_error(None)

    def build_odoo(self) -> OdooOverrides | None:
        """The sparse `OdooOverrides` the checked rows describe, or `None`
        if a checked row's text failed validation (its own error is set)."""
        assert self._include_odoo, "build_odoo() called on a mise-only editor"
        kwargs: dict[str, object] = {}
        for field in _ODOO_FIELDS:
            checkbox = self.query_one(f"#oe_odoo_{field.key}_override", Checkbox)
            li = self.query_one(f"#oe_odoo_{field.key}_input", LabeledInput)
            li.set_error(None)
            if not checkbox.value:
                continue
            try:
                kwargs[field.key] = _validate_odoo_field(field, li.value)
            except ValueError as exc:
                li.set_error(str(exc))
                return None
        return OdooOverrides(**kwargs)

    def build_mise(self) -> MiseOverrides | None:
        """The sparse `MiseOverrides` the checked rows describe, or `None`
        if a checked row's text failed validation (its own error is set)."""
        assert self._include_mise, "build_mise() called on an odoo-only editor"
        kwargs: dict[str, object] = {}
        for field in _MISE_FIELDS:
            checkbox = self.query_one(f"#oe_mise_{field.key}_override", Checkbox)
            li = self.query_one(f"#oe_mise_{field.key}_input", LabeledInput)
            li.set_error(None)
            if not checkbox.value:
                continue
            try:
                kwargs[field.key] = _validate_mise_field(field, li.value)
            except ValueError as exc:
                li.set_error(str(exc))
                return None
        return MiseOverrides(**kwargs)


# ---------------------------------------------------------------------------
# WorkspaceConfigScreen — edit a workspace's config
# ---------------------------------------------------------------------------


class WorkspaceConfigScreen(ModalScreen[WorkspaceConfig | None]):
    """Edit a workspace's repos and typed odoo/mise overrides.

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
        self._schema1 = ws.version == 1

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("Repos", classes="section-heading")
            for alias in self._aliases:
                current = self._ws.repos.get(alias)
                value = current.to_spec_str() if current else ""
                yield LabeledInput(alias, value=value, id=f"wc_spec_{alias}")

            yield Static("Options", classes="section-heading")
            if self._schema1:
                yield Static(
                    "This workspace is still schema 1 — typed odoo/mise options "
                    "can't be saved here until it is migrated. Run `ow render`, "
                    "or press I (Repair) in the dashboard.",
                    classes="section-hint",
                )
            yield OptionsEditor(
                odoo=self._ws.odoo,
                mise=self._ws.mise,
                base_odoo=self._config.odoo,
                base_mise=self._config.mise,
                disabled=self._schema1,
                id="wc_options",
            )

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

        # Options — a schema-1 workspace keeps its current sparse overrides
        # unchanged; its controls are disabled and have nothing new to read.
        if self._schema1:
            odoo, mise = self._ws.odoo, self._ws.mise
        else:
            options = self.query_one("#wc_options", OptionsEditor)
            odoo = options.build_odoo()
            if odoo is None:
                return
            mise = options.build_mise()
            if mise is None:
                return

        self.dismiss(replace(self._ws, repos=repos, odoo=odoo, mise=mise))


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
