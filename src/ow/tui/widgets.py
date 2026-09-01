from __future__ import annotations

import re
from typing import Any

from textual.widget import Widget
from textual.widgets import Button, Input, Label, RichLog
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.binding import Binding

from ow.utils.config import WorkspaceConfig
from ow.utils.status import WorkspaceStatus


def sanitize_id(text: str) -> str:
    """Turn arbitrary text (a repo alias, a template name) into a valid
    Textual widget-id fragment.

    Textual ids must match ``[a-zA-Z_-][a-zA-Z0-9_-]*``. Aliases and template
    names come from user-controlled sources (TOML keys, directory names) and
    are not restricted the same way — a dotted alias like ``odoo.web`` is
    valid TOML but not a valid id, and previously crashed compose().
    """
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", text)
    return slug or "_"


def unique_slugs(names: list[str]) -> dict[str, str]:
    """Map each name to a sanitized, collision-free id fragment.

    Two distinct names can sanitize to the same slug (``odoo.web`` and
    ``odoo_web``); disambiguate with a numeric suffix so widget ids stay
    unique.
    """
    slugs: dict[str, str] = {}
    used: set[str] = set()
    for name in names:
        base = sanitize_id(name)
        candidate = base
        n = 2
        while candidate in used:
            candidate = f"{base}_{n}"
            n += 1
        used.add(candidate)
        slugs[name] = candidate
    return slugs


class LabeledInput(Horizontal):
    """A labeled text input that can be enabled/disabled.

    Used for branch specs: enabled when the corresponding repo checkbox
    is checked, disabled (greyed out) otherwise.
    """

    DEFAULT_CSS = """
    LabeledInput {
        height: auto;
        padding: 0 1 0 4;
    }
    LabeledInput > Label {
        width: 12;
        color: $text;
    }
    LabeledInput.-disabled > Label {
        color: $text-disabled;
    }
    LabeledInput > Vertical {
        width: 1fr;
        height: auto;
    }
    LabeledInput Input {
        width: 1fr;
    }
    LabeledInput #li_error {
        height: auto;
        color: $error;
        display: none;
    }
    LabeledInput.-invalid #li_error {
        display: block;
    }
    """

    def __init__(
        self,
        label: str,
        value: str = "",
        placeholder: str = "",
        enabled: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._label_text = label
        self._value = value
        self._placeholder = placeholder
        self._enabled = enabled
        self._error_message: str | None = None

    def compose(self):
        yield Label(self._label_text)
        with Vertical():
            yield Input(
                value=self._value,
                placeholder=self._placeholder,
                id="li_input",
                disabled=not self._enabled,
            )
            yield Label("", id="li_error")

    def on_mount(self) -> None:
        """Set initial disabled state."""
        if not self._enabled:
            self.add_class("-disabled")

    @property
    def value(self) -> str:
        """Current input value. Works before and after mounting."""
        if not self.is_mounted:
            return self._value
        return self.query_one("#li_input", Input).value

    @property
    def error_message(self) -> str | None:
        """The current validation error, or None if the field is valid."""
        return self._error_message

    def set_error(self, message: str | None) -> None:
        """Mark this field invalid and show `message` beneath the input, or
        clear the error when `message` is None/empty.

        Used when a branch spec fails to parse: the field stays visible and
        focused with its own error instead of the repo silently vanishing
        from the form (finding 5).
        """
        self._error_message = message or None
        error_label = self.query_one("#li_error", Label)
        if message:
            error_label.update(message)
            self.add_class("-invalid")
        else:
            error_label.update("")
            self.remove_class("-invalid")

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable the input."""
        self.query_one("#li_input", Input).disabled = not enabled
        if enabled:
            self.remove_class("-disabled")
        else:
            self.add_class("-disabled")


class ConfirmDialog(ModalScreen[bool]):
    """A yes/no confirmation modal.

    Tab/arrow keys navigate between buttons; y/n and Enter activate the
    focused button. Yes dismisses with True, No with False. No is focused
    by default and Escape dismisses with False: a destructive command must
    not proceed unasked.
    """

    DEFAULT_CSS = """
    ConfirmDialog {
        align: center middle;
    }
    ConfirmDialog > Vertical {
        width: 70;
        max-height: 80%;
        height: auto;
        padding: 1 3;
        border: thick $primary;
        background: $surface;
    }
    ConfirmDialog > Vertical > Label {
        text-align: center;
        text-style: bold;
        margin: 0 0 1 0;
    }
    ConfirmDialog #confirm_details {
        max-height: 50%;
        height: auto;
        margin: 0 0 1 0;
    }
    ConfirmDialog > Vertical > Horizontal {
        align: center middle;
        height: auto;
        margin-top: 1;
    }
    ConfirmDialog Button {
        margin: 0 2;
        min-width: 12;
    }
    """

    AUTO_FOCUS = "#btn_no"

    BINDINGS = [
        Binding("left", "app.focus_previous", "", show=False),
        Binding("right", "app.focus_next", "", show=False),
        Binding("escape", "dismiss(False)", "", show=False),
        Binding("y", "dismiss(True)", "", show=False),
        Binding("n", "dismiss(False)", "", show=False),
    ]

    def __init__(
        self,
        message: str = "Proceed?",
        details: Any | None = None,
    ) -> None:
        super().__init__()
        self._message = message
        self._details = details

    def compose(self):
        from rich.text import Text
        from textual.widgets import Static
        children: list = [Label(self._message)]
        if self._details is not None:
            if isinstance(self._details, str):
                children.append(Static(Text(self._details), id="confirm_details"))
            else:
                children.append(Static(self._details, id="confirm_details"))
        children.append(Horizontal(
            Button("Yes", id="btn_yes", variant="success"),
            Button("No", id="btn_no", variant="error"),
        ))
        yield Vertical(*children)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_yes":
            self.dismiss(True)
        else:
            self.dismiss(False)


class OperationLog(RichLog):
    """The persistent log pane at the bottom of the dashboard.

    Receives Rich renderables from the output sink — styles survive the
    trip, so the log pane shows what the terminal would have shown.
    """

    BINDINGS = [
        Binding("j", "scroll_down", "Down", show=False),
        Binding("k", "scroll_up", "Up", show=False),
        Binding("g", "scroll_home", "Top", show=False),
        Binding("G", "scroll_end", "Bottom", show=False),
    ]

    DEFAULT_CSS = """
    OperationLog {
        width: 1fr;
        height: 1fr;
        border: tall $surface;
        padding: 0 1;
    }
    OperationLog:focus {
        border: tall $accent;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(
            markup=False,
            wrap=True,
            auto_scroll=True,
            min_width=20,
            **kwargs,
        )

    def action_scroll_down(self) -> None:
        self.auto_scroll = False
        super().action_scroll_down()

    def action_scroll_up(self) -> None:
        self.auto_scroll = False
        super().action_scroll_up()

    def action_scroll_home(self) -> None:
        self.auto_scroll = False
        super().action_scroll_home()

    def action_scroll_end(self) -> None:
        self.auto_scroll = True  # Re-enable auto_scroll at bottom
        super().action_scroll_end()


class WorkspaceDetail(VerticalScroll):
    """The right-hand detail pane.

    Renders workspace config (path, repos, templates, vars) immediately
    on highlight, then upgrades to a full status table once the gather
    worker finishes.
    """

    DEFAULT_CSS = """
    WorkspaceDetail {
        width: 1fr;
        height: 1fr;
        padding: 1 2;
    }
    WorkspaceDetail .detail-heading {
        text-style: bold;
        color: $text;
        margin: 1 0 0 0;
        padding: 0 0 0 0;
    }
    WorkspaceDetail .detail-section {
        margin: 0 0 1 2;
        height: auto;
        color: $text-muted;
    }
    """

    def show_config_only(self, ws_dir: Path, ws: WorkspaceConfig) -> None:
        """Render path, repos (alias/spec), templates, vars. No git."""
        self.remove_children()
        self.mount(self._build_config_view(ws_dir, ws))

    def show_status(
        self,
        status: WorkspaceStatus,
        ws_dir: Path,
        ws: WorkspaceConfig,
    ) -> None:
        """Replace the repos block with a status table and add links."""
        self.remove_children()
        self.mount(self._build_status_view(status, ws_dir, ws))

    def _build_config_view(self, ws_dir: Path, ws: WorkspaceConfig) -> Widget:
        from textual.widgets import Static
        from rich.text import Text
        from ow.utils.display import display_path

        children: list[Widget] = []
        path_text = Text(display_path(ws_dir), style="bold cyan")
        children.append(Static(path_text, classes="detail-section"))

        # Repos
        children.append(Static("Repos", classes="detail-heading"))
        if ws.repos:
            lines = Text()
            for alias, spec in ws.repos.items():
                lines.append(f"  {alias}  ")
                lines.append(f"{spec.to_spec_str()}\n", style="dim")
            children.append(Static(lines, classes="detail-section"))
        else:
            children.append(Static("  (none)", classes="detail-section"))

        # Templates
        children.append(Static("Templates", classes="detail-heading"))
        children.append(
            Static(f"  {', '.join(ws.templates) or '(none)'}", classes="detail-section")
        )

        # Vars
        if ws.vars:
            children.append(Static("Vars", classes="detail-heading"))
            lines = Text()
            for k, v in ws.vars.items():
                lines.append(f"  {k} = {v}\n")
            children.append(Static(lines, classes="detail-section"))

        from textual.containers import Vertical
        return Vertical(*children)

    def _build_status_view(
        self,
        status: WorkspaceStatus,
        ws_dir: Path,
        ws: WorkspaceConfig,
    ) -> Widget:
        from textual.widgets import Static
        from rich.text import Text
        from ow.utils.display import display_path, counts

        children: list[Widget] = []
        path_text = Text(display_path(ws_dir), style="bold cyan")
        children.append(Static(path_text, classes="detail-section"))

        # Build drift lookup
        drift_map: dict[str, str] = {}
        for d in status.drift:
            if d.is_drifted and d.actual_branch is not None:
                drift_map[d.alias] = d.actual_branch

        # Repos status table
        children.append(Static("Repos", classes="detail-heading"))
        table = _render_repos_table(status.repos, drift_map)
        children.append(Static(table, classes="detail-section"))

        # Templates
        children.append(Static("Templates", classes="detail-heading"))
        children.append(
            Static(f"  {', '.join(ws.templates) or '(none)'}", classes="detail-section")
        )

        # Vars
        if ws.vars:
            children.append(Static("Vars", classes="detail-heading"))
            lines = Text()
            for k, v in ws.vars.items():
                lines.append(f"  {k} = {v}\n")
            children.append(Static(lines, classes="detail-section"))

        # Links
        links = self._build_links(status)
        if links is not None:
            children.append(Static("Links", classes="detail-heading"))
            children.append(Static(links, classes="detail-section"))

        from textual.containers import Vertical
        return Vertical(*children)

    def _build_links(self, status: WorkspaceStatus) -> Text | None:
        from rich.text import Text
        parts: list[Text] = []
        if status.runbot_branch:
            t = Text()
            t.append("  runbot: ", style="dim")
            t.append(status.runbot_branch)
            parts.append(t)
        for rs in status.repos:
            if rs.github_url:
                t = Text()
                t.append(f"  {rs.alias}: ", style="dim")
                t.append(rs.github_url, style=f"link {rs.github_url}")
                parts.append(t)
        if not parts:
            return None
        result = Text()
        for i, p in enumerate(parts):
            if i > 0:
                result.append("\n")
            result.append_text(p)
        return result


def _render_repos_table(
    repos: list,
    drift_map: dict[str, str],
) -> Text:
    """Build a Rich Text block rendering repo status lines.

    Columns: alias / base / counts / head / flags.
    """
    from rich.text import Text
    from ow.utils.display import counts

    if not repos:
        return Text("  (no repos)")

    max_alias = max(len(rs.alias) for rs in repos)
    lines = Text()
    for rs in repos:
        alias = rs.alias.ljust(max_alias)
        line = Text(f"  {alias}  ")

        # base
        base = rs.base_ref or "?"
        line.append(f"{base:<24} ")

        # state
        if rs.state == "not_applied":
            line.append("⊘ not applied", style="yellow")
        elif rs.state == "unresolved":
            line.append("✗ unresolved", style="red")
        elif rs.state == "error":
            line.append(f"✗ error: {rs.error}", style="red")
        else:
            # counts
            if rs.primary is not None:
                behind, ahead = rs.primary
                line.append_text(Text.from_markup(counts(behind, ahead)))
                if rs.secondary is not None:
                    sb, sa = rs.secondary
                    line.append_text(Text.from_markup(f" ({counts(sb, sa)})"))
                line.append(" ")

            # head
            if rs.kind == "detached":
                line.append(f"⊘ DETACHED {rs.short_hash or ''}", style="yellow")
            elif rs.head_label:
                line.append(rs.head_label)
            else:
                line.append("?", style="dim")

        # flags
        flags: list[str] = []
        if rs.alias in drift_map:
            flags.append(f"⚠ drift: on {drift_map[rs.alias]}")
        if rs.fetch_failed:
            flags.append("fetch failed")
        if flags:
            line.append(f"  {' | '.join(flags)}", style="yellow")

        lines.append_text(line)
        lines.append("\n")
    return lines
