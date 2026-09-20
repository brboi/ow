"""Global config editor screen for the dashboard.

Edits `editor`, typed odoo/mise overrides, the ignore pattern list and
remotes; dismisses with a fresh `Config` that the dashboard writes via
`write_global_config`.

Layout: full-screen modal with a sidebar for section navigation
(Editor / Odoo / Mise / Ignore / Remotes) and a right panel showing the
selected section's content.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Label, OptionList, Static

from ow.utils.config import Config, RemoteConfig, WorkspaceConfig, load_workspace_config
from ow.tui.widgets import ConfirmDialog, LabeledInput
from ow.tui.workspace_forms import OptionsEditor, PromptScreen

# The per-workspace config marker, relative to a workspace directory.
_WS_MARKER = Path(".ow") / "config.toml"


def _known_workspace_configs() -> list[tuple[str, WorkspaceConfig]]:
    """Every known workspace's name + config, skipping ones that fail to load.

    Pure index/config reads: no git, no probing, nothing that touches a
    worktree. Used to describe what a remote or a typed option override is
    actually used for, before letting the user remove or change it.
    """
    from ow.utils import index

    out: list[tuple[str, WorkspaceConfig]] = []
    for ws_dir in index.known_workspaces():
        try:
            ws = load_workspace_config(ws_dir / _WS_MARKER)
        except (OSError, ValueError):
            continue
        out.append((ws_dir.name, ws))
    return out


def _describe_usage(names: list[str]) -> str:
    if not names:
        return "Not used by any workspace."
    return f"Used by {len(names)} workspace(s): {', '.join(sorted(names))}"


def _describe_remote_usage(alias: str) -> str:
    """Which known workspaces declare a repo under this remote alias."""
    names = [name for name, ws in _known_workspace_configs() if alias in ws.repos]
    return _describe_usage(names)


def _describe_option_override_usage(table: str) -> str:
    """How many known workspaces set at least one local override in `table`
    ('odoo' or 'mise'), replacing the old free-form-vars usage describer
    with a typed count over the fixed option model."""
    def _has_override(ws: WorkspaceConfig) -> bool:
        record = ws.odoo if table == "odoo" else ws.mise
        return any(getattr(record, f.name) is not None for f in fields(record))

    names = [name for name, ws in _known_workspace_configs() if _has_override(ws)]
    if not names:
        return f"No known workspace overrides {table} options locally."
    return f"{len(names)} known workspace(s) override {table} options locally: {', '.join(sorted(names))}"


# ---------------------------------------------------------------------------
# AddRemoteRequest — data the AddRemoteScreen dismisses with
# ---------------------------------------------------------------------------


@dataclass
class AddRemoteRequest:
    """A new remote entry from the single-form add dialog."""

    alias: str
    name: str
    url: str
    pushurl: str | None
    fetch: str | None


# ---------------------------------------------------------------------------
# AddRemoteScreen — single form to add a remote
# ---------------------------------------------------------------------------


class AddRemoteScreen(ModalScreen[AddRemoteRequest | None]):
    """Single form to add a remote: alias + name + url + pushurl + fetch.

    Replaces the old 3-step flow (alias → name → fields).
    """

    DEFAULT_CSS = """
    AddRemoteScreen {
        align: center middle;
    }
    AddRemoteScreen > Vertical {
        width: 70;
        height: auto;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    AddRemoteScreen .form-title {
        text-style: bold;
        content-align: center middle;
        width: 1fr;
        margin-bottom: 1;
    }
    AddRemoteScreen > Vertical > Horizontal {
        align: center middle;
        height: auto;
        margin-top: 1;
    }
    AddRemoteScreen Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss(None)", "", show=False),
    ]

    def __init__(
        self,
        existing_aliases: list[str],
        default_alias: str = "",
    ) -> None:
        super().__init__()
        self._existing_aliases = existing_aliases
        self._default_alias = default_alias

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Add Remote", classes="form-title")
            yield LabeledInput(
                "Alias",
                value=self._default_alias,
                placeholder="e.g. community",
                id="ar_alias",
            )
            yield LabeledInput(
                "Name",
                value="origin",
                placeholder="e.g. origin",
                id="ar_name",
            )
            yield LabeledInput(
                "URL",
                placeholder="git@github.com:user/repo.git",
                id="ar_url",
            )
            yield LabeledInput(
                "Push URL",
                placeholder="(optional)",
                id="ar_pushurl",
            )
            yield LabeledInput(
                "Fetch",
                placeholder="(optional)",
                id="ar_fetch",
            )
            yield Horizontal(
                Button("Cancel", id="btn_cancel", variant="error"),
                Button("Add", id="btn_add", variant="success"),
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_cancel":
            self.dismiss(None)
        elif event.button.id == "btn_add":
            self._try_add()

    def on_input_submitted(self, event) -> None:
        # Enter in any input submits the form
        self._try_add()

    def _try_add(self) -> None:
        alias = self.query_one("#ar_alias", LabeledInput).value.strip()
        name = self.query_one("#ar_name", LabeledInput).value.strip()
        url = self.query_one("#ar_url", LabeledInput).value.strip()
        pushurl = self.query_one("#ar_pushurl", LabeledInput).value.strip()
        fetch = self.query_one("#ar_fetch", LabeledInput).value.strip()

        # Validation
        alias_inp = self.query_one("#ar_alias", LabeledInput)
        name_inp = self.query_one("#ar_name", LabeledInput)
        url_inp = self.query_one("#ar_url", LabeledInput)

        alias_inp.set_error(None)
        name_inp.set_error(None)
        url_inp.set_error(None)

        if not alias:
            alias_inp.set_error("alias is required")
            return
        if not name:
            name_inp.set_error("name is required")
            return
        if not url:
            url_inp.set_error("url is required")
            return

        self.dismiss(AddRemoteRequest(
            alias=alias,
            name=name,
            url=url,
            pushurl=pushurl or None,
            fetch=fetch or None,
        ))


# ---------------------------------------------------------------------------
# _IgnoreEditor — ordered global ignore-pattern list
# ---------------------------------------------------------------------------


class _IgnoreEditor(Vertical):
    """An ordered `DataTable` of ignore patterns, with add/remove/edit.

    `owignore` is a global, ordered pattern list applied to every
    workspace's generated files — not a per-workspace setting — so this
    editor has no per-row "used by" describer the way remotes/options do.
    """

    DEFAULT_CSS = """
    _IgnoreEditor {
        height: auto;
        max-height: 12;
    }
    _IgnoreEditor DataTable {
        height: auto;
        max-height: 8;
    }
    _IgnoreEditor Horizontal {
        height: auto;
    }
    _IgnoreEditor Button {
        margin: 0 1;
        min-width: 4;
    }
    """

    BINDINGS = [
        Binding("enter", "edit_cell", "Edit", show=True),
    ]

    def __init__(self, initial: tuple[str, ...], *, disabled: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self._initial = initial
        self._locked = disabled

    def compose(self) -> ComposeResult:
        table = DataTable(id="ignore_table")
        table.add_column("pattern")
        for pattern in self._initial:
            table.add_row(pattern)
        yield table
        yield Horizontal(
            Button("+", id="ignore_add", disabled=self._locked),
            Button("-", id="ignore_remove", disabled=self._locked),
        )

    def get_patterns(self) -> tuple[str, ...]:
        table = self.query_one("#ignore_table", DataTable)
        return tuple(
            str(table.get_cell_at(Coordinate(i, 0))) for i in range(table.row_count)
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ignore_add":
            self._add_row()
        elif event.button.id == "ignore_remove":
            self._remove_row()

    def _add_row(self) -> None:
        def _on_result(result: str | None) -> None:
            if result is None:
                return
            pattern = result.strip()
            if not pattern:
                return
            self.query_one("#ignore_table", DataTable).add_row(pattern)
        self.app.push_screen(PromptScreen("pattern"), callback=_on_result)

    def _remove_row(self) -> None:
        table = self.query_one("#ignore_table", DataTable)
        cursor = table.cursor_coordinate
        if cursor is None:
            self.app.notify("No row selected", severity="warning")
            return
        try:
            row_key = table.coordinate_to_cell_key(cursor)[0]
        except Exception:
            self.app.notify("Could not remove row", severity="warning")
            return
        table.remove_row(row_key)

    def action_edit_cell(self) -> None:
        if self._locked:
            return
        table = self.query_one("#ignore_table", DataTable)
        cursor = table.cursor_coordinate
        if cursor is None:
            self.app.notify("No row selected", severity="warning")
            return
        try:
            current = str(table.get_cell_at(cursor))
            row_key = table.coordinate_to_cell_key(cursor)[0]
        except Exception:
            self.app.notify("Could not read cell", severity="warning")
            return

        def _on_result(result: str | None) -> None:
            if result is None:
                return
            table = self.query_one("#ignore_table", DataTable)
            try:
                table.update_cell(row_key, table.columns[0].key, result)
            except Exception:
                pass
        self.app.push_screen(PromptScreen("pattern", default=current), callback=_on_result)


# ---------------------------------------------------------------------------
# GlobalConfigScreen — sidebar layout
# ---------------------------------------------------------------------------


class GlobalConfigScreen(ModalScreen[Config | None]):
    """Edit the global config: editor, typed odoo/mise defaults, the
    ignore pattern list, and remotes.

    Full-screen modal with a sidebar for section navigation and a right
    panel showing the selected section's content.

    Dismisses with a new `Config` on Save, None on Cancel.
    """

    DEFAULT_CSS = """
    GlobalConfigScreen {
        /* full-screen modal — no centering */
    }

    /* ---- outer frame ---- */
    GlobalConfigScreen #gc_frame {
        width: 1fr;
        height: 1fr;
        background: $surface;
    }

    /* ---- header bar ---- */
    GlobalConfigScreen #gc_header {
        height: 4;
        padding: 0 2;
        background: $primary-background;
        border-bottom: solid $primary;
    }
    GlobalConfigScreen #gc_title {
        text-style: bold;
        width: 1fr;
        content-align: left middle;
        padding: 0 1;
    }
    GlobalConfigScreen #gc_header Button {
        margin: 0 1;
    }

    /* ---- sidebar ---- */
    GlobalConfigScreen #gc_sidebar {
        width: 20;
        min-width: 20;
        height: 1fr;
        border-right: solid $primary-background;
        background: $surface;
        padding: 1 0;
    }
    GlobalConfigScreen #gc_sidebar OptionList {
        height: 1fr;
        border: none;
        background: transparent;
    }
    GlobalConfigScreen #gc_sidebar > Label {
        text-style: bold;
        color: $text-muted;
        padding: 0 1;
        margin-bottom: 1;
    }

    /* ---- content panel ---- */
    GlobalConfigScreen #gc_content {
        width: 1fr;
        height: 1fr;
        padding: 1 2;
    }
    GlobalConfigScreen .section-heading {
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }
    GlobalConfigScreen .section-container {
        height: 1fr;
    }
    GlobalConfigScreen .section-hint {
        color: $text-muted;
        margin-bottom: 1;
    }

    /* ---- remotes section ---- */
    GlobalConfigScreen #gc_remotes_list {
        height: auto;
        max-height: 10;
        border: tall $secondary-background;
        margin-bottom: 1;
    }
    GlobalConfigScreen #gc_remote_fields {
        height: auto;
        margin-top: 1;
    }
    GlobalConfigScreen #gc_remote_buttons {
        height: auto;
        margin-top: 1;
    }
    GlobalConfigScreen #gc_remote_buttons Button {
        margin: 0 1;
        min-width: 6;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss(None)", "", show=False),
        Binding("ctrl+s", "save", "Save", show=True),
    ]

    _SECTIONS = ("Editor", "Odoo", "Mise", "Ignore", "Remotes")
    _PANEL_IDS = (
        "gc_panel_editor",
        "gc_panel_odoo",
        "gc_panel_mise",
        "gc_panel_ignore",
        "gc_panel_remotes",
    )

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._config = config
        self._schema1 = config.version == 1
        # Working copy of remotes that we mutate as the user edits
        self._remotes: dict[str, dict[str, RemoteConfig]] = {}
        for alias, names in config.remotes.items():
            self._remotes[alias] = {}
            for name, rc in names.items():
                self._remotes[alias][name] = RemoteConfig(
                    url=rc.url, pushurl=rc.pushurl, fetch=rc.fetch,
                )

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def _migration_hint(self, what: str) -> Static:
        return Static(
            f"This config is still schema 1 — typed {what} can't be saved here "
            "until it is migrated. Run `ow render` in any workspace, or press "
            "I (Repair) in the dashboard.",
            classes="section-hint",
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="gc_frame"):
            # Header bar with title + Save/Cancel
            yield Horizontal(
                Static("Global Config", id="gc_title"),
                Button("Save", id="btn_save", variant="success"),
                Button("Cancel", id="btn_cancel", variant="error"),
                id="gc_header",
            )
            # Sidebar + content
            with Horizontal(id="gc_body"):
                with Vertical(id="gc_sidebar"):
                    yield Label("Settings")
                    yield OptionList(*self._SECTIONS, id="gc_sections")
                with VerticalScroll(id="gc_content"):
                    # Editor section
                    yield Vertical(
                        Static("Editor", classes="section-heading"),
                        LabeledInput(
                            "editor",
                            value=self._config.editor,
                            id="gc_editor",
                        ),
                        id="gc_panel_editor",
                        classes="section-container",
                    )
                    # Odoo section
                    odoo_children: list = [Static("Odoo", classes="section-heading")]
                    if self._schema1:
                        odoo_children.append(self._migration_hint("odoo options"))
                    odoo_children.append(
                        Static(_describe_option_override_usage("odoo"), classes="section-hint")
                    )
                    odoo_children.append(
                        OptionsEditor(
                            odoo=self._config.odoo,
                            include_mise=False,
                            disabled=self._schema1,
                            id="gc_odoo",
                        )
                    )
                    yield Vertical(*odoo_children, id="gc_panel_odoo", classes="section-container")

                    # Mise section
                    mise_children: list = [Static("Mise", classes="section-heading")]
                    if self._schema1:
                        mise_children.append(self._migration_hint("mise options"))
                    mise_children.append(
                        Static(_describe_option_override_usage("mise"), classes="section-hint")
                    )
                    mise_children.append(
                        OptionsEditor(
                            mise=self._config.mise,
                            include_odoo=False,
                            disabled=self._schema1,
                            id="gc_mise",
                        )
                    )
                    yield Vertical(*mise_children, id="gc_panel_mise", classes="section-container")

                    # Ignore section
                    ignore_children: list = [Static("Ignore", classes="section-heading")]
                    if self._schema1:
                        ignore_children.append(self._migration_hint("the ignore list"))
                    ignore_children.append(
                        Static(
                            "An ordered list of patterns applied to every workspace's "
                            "generated files.",
                            classes="section-hint",
                        )
                    )
                    ignore_children.append(
                        _IgnoreEditor(self._config.owignore, disabled=self._schema1, id="gc_ignore")
                    )
                    yield Vertical(*ignore_children, id="gc_panel_ignore", classes="section-container")

                    # Remotes section
                    yield Vertical(
                        Static("Remotes", classes="section-heading"),
                        OptionList(id="gc_remotes_list"),
                        Vertical(
                            LabeledInput("url", id="gc_remote_url"),
                            LabeledInput("pushurl", id="gc_remote_pushurl"),
                            LabeledInput("fetch", id="gc_remote_fetch"),
                            id="gc_remote_fields",
                        ),
                        Horizontal(
                            Button("+ Add", id="gc_remote_add", variant="success"),
                            Button("− Remove", id="gc_remote_remove", variant="error"),
                            id="gc_remote_buttons",
                        ),
                        id="gc_panel_remotes",
                        classes="section-container",
                    )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        # Show only the editor section initially
        self._show_section(0)
        # Populate the remotes list
        self._refresh_remotes_list()

    # ------------------------------------------------------------------
    # Section navigation
    # ------------------------------------------------------------------

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        if event.option_list.id == "gc_sections":
            idx = event.option_index
            if idx is not None:
                self._show_section(idx)
        elif event.option_list.id == "gc_remotes_list":
            self._apply_field_edits()
            self._refresh_remote_fields()

    def _show_section(self, index: int) -> None:
        """Show the section at `index` and hide the others."""
        for i, pid in enumerate(self._PANEL_IDS):
            panel = self.query_one(f"#{pid}", Vertical)
            panel.display = (i == index)

    # ------------------------------------------------------------------
    # Remotes list management
    # ------------------------------------------------------------------

    def _refresh_remotes_list(self) -> None:
        """Rebuild the remotes OptionList from self._remotes."""
        ol = self.query_one("#gc_remotes_list", OptionList)
        ol.clear_options()
        first_alias = True
        for alias in sorted(self._remotes):
            if not first_alias:
                ol.add_option(None)
            first_alias = False
            names = self._remotes.get(alias, {})
            if not names:
                ol.add_option(f"  {alias}  (no remotes)")
            else:
                for rname, rc in names.items():
                    ol.add_option(f"  {alias}/{rname}  {rc.url}")
        # Highlight first real option
        if ol.option_count > 0:
            ol.highlighted = 0
            self._refresh_remote_fields()
        else:
            self._clear_remote_fields()

    def _selected_remote(self) -> tuple[str, str] | None:
        """Return (alias, name) for the highlighted remote, or None."""
        ol = self.query_one("#gc_remotes_list", OptionList)
        if ol.highlighted is None:
            return None
        option = ol.get_option_at_index(ol.highlighted)
        if option is None:
            return None
        prompt = str(option.prompt).strip()
        # Parse "alias/name  url" back out
        if "  " in prompt:
            left = prompt.split("  ")[0].strip()
        else:
            left = prompt
        if "/" not in left:
            return None
        alias, name = left.split("/", 1)
        return alias.strip(), name.strip()

    def _refresh_remote_fields(self) -> None:
        """Fill the url/pushurl/fetch inputs from the selected remote."""
        sel = self._selected_remote()
        if sel is None:
            self._clear_remote_fields()
            return
        alias, name = sel
        rc = self._remotes.get(alias, {}).get(name)
        if rc is None:
            self._clear_remote_fields()
            return
        url_li = self.query_one("#gc_remote_url", LabeledInput)
        url_li.query_one("#li_input").value = rc.url
        url_li.set_error(None)
        pushurl_li = self.query_one("#gc_remote_pushurl", LabeledInput)
        pushurl_li.query_one("#li_input").value = rc.pushurl or ""
        pushurl_li.set_error(None)
        fetch_li = self.query_one("#gc_remote_fetch", LabeledInput)
        fetch_li.query_one("#li_input").value = rc.fetch or ""
        fetch_li.set_error(None)

    def _clear_remote_fields(self) -> None:
        for wid in ("#gc_remote_url", "#gc_remote_pushurl", "#gc_remote_fetch"):
            li = self.query_one(wid, LabeledInput)
            li.query_one("#li_input").value = ""
            li.set_error(None)

    def _apply_field_edits(self) -> bool:
        """Write back any edits in the url/pushurl/fetch fields to the
        currently selected remote in self._remotes.
        Returns False if validation failed (caller should abort save)."""
        sel = self._selected_remote()
        if sel is None:
            return True
        alias, name = sel
        url = self.query_one("#gc_remote_url", LabeledInput).value.strip()
        if not url:
            self.query_one("#gc_remote_url", LabeledInput).set_error("URL is required")
            return False
        pushurl = self.query_one("#gc_remote_pushurl", LabeledInput).value.strip()
        fetch = self.query_one("#gc_remote_fetch", LabeledInput).value.strip()
        if alias not in self._remotes:
            self._remotes[alias] = {}
        self._remotes[alias][name] = RemoteConfig(
            url=url,
            pushurl=pushurl or None,
            fetch=fetch or None,
        )
        return True


    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn_save":
            self._try_save()
        elif bid == "btn_cancel":
            self.dismiss(None)
        elif bid == "gc_remote_add":
            self._add_remote()
        elif bid == "gc_remote_remove":
            self._remove_remote()

    def _add_remote(self) -> None:
        """Open the single-form AddRemoteScreen."""
        # Apply any pending field edits before opening the form
        self._apply_field_edits()
        sel = self._selected_remote()
        default_alias = sel[0] if sel else ""
        self.app.push_screen(
            AddRemoteScreen(
                existing_aliases=list(self._remotes),
                default_alias=default_alias,
            ),
            callback=self._on_remote_added,
        )

    def _on_remote_added(self, result: AddRemoteRequest | None) -> None:
        if result is None:
            return
        # Add the remote to our working copy
        if result.alias not in self._remotes:
            self._remotes[result.alias] = {}
        self._remotes[result.alias][result.name] = RemoteConfig(
            url=result.url,
            pushurl=result.pushurl,
            fetch=result.fetch,
        )
        self._refresh_remotes_list()
        # Highlight the newly added remote
        ol = self.query_one("#gc_remotes_list", OptionList)
        # Find the option that matches
        target = f"{result.alias}/{result.name}"
        for idx in range(ol.option_count):
            opt = ol.get_option_at_index(idx)
            if opt is not None and target in str(opt.prompt):
                ol.highlighted = idx
                break

    def _remove_remote(self) -> None:
        """Confirm, then remove the selected remote from the working copy."""
        # Apply field edits first so we don't lose unsaved changes
        self._apply_field_edits()
        sel = self._selected_remote()
        if sel is None:
            return
        alias, name = sel
        usage = _describe_remote_usage(alias)

        def _on_confirmed(ok: bool) -> None:
            if not ok:
                return
            if alias in self._remotes:
                self._remotes[alias].pop(name, None)
                if not self._remotes[alias]:
                    del self._remotes[alias]
            self._refresh_remotes_list()

        self.app.push_screen(
            ConfirmDialog(f"Remove remote '{alias}/{name}'?", details=usage),
            callback=_on_confirmed,
        )

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _try_save(self) -> None:
        # Apply any pending remote field edits
        if not self._apply_field_edits():
            return

        # Editor
        editor = self.query_one("#gc_editor", LabeledInput).value.strip() or "code"

        # Typed odoo/mise/ignore — a schema-1 config keeps its current
        # sparse overrides unchanged; its controls are disabled.
        if self._schema1:
            odoo, mise, owignore = self._config.odoo, self._config.mise, self._config.owignore
        else:
            odoo = self.query_one("#gc_odoo", OptionsEditor).build_odoo()
            if odoo is None:
                self._show_section(1)
                return
            mise = self.query_one("#gc_mise", OptionsEditor).build_mise()
            if mise is None:
                self._show_section(2)
                return
            owignore = self.query_one("#gc_ignore", _IgnoreEditor).get_patterns()

        self.dismiss(replace(
            self._config,
            remotes=dict(self._remotes),
            editor=editor,
            odoo=odoo,
            mise=mise,
            owignore=owignore,
        ))

    def action_save(self) -> None:
        """Ctrl+S binding."""
        self._try_save()
