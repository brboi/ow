"""Global config editor screen for the dashboard.

Edits `editor`, `vars` and `remotes`; dismisses with a fresh `Config`
that the dashboard writes via `write_global_config`.

Layout: full-screen modal with a sidebar for section navigation
(Editor / Vars / Remotes) and a right panel showing the selected
section's content.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, OptionList, Static

from ow.utils.config import Config, RemoteConfig
from ow.tui.widgets import LabeledInput
from ow.tui.workspace_forms import VarsEditor


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
# GlobalConfigScreen — sidebar layout
# ---------------------------------------------------------------------------



class GlobalConfigScreen(ModalScreen[Config | None]):
    """Edit the global config: editor, vars, remotes.

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
        height: 3;
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

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._config = config
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
                    yield OptionList(
                        "Editor",
                        "Vars",
                        "Remotes",
                        id="gc_sections",
                    )
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
                    # Vars section
                    yield Vertical(
                        Static("Variables", classes="section-heading"),
                        VarsEditor(self._config.vars, id="gc_vars"),
                        id="gc_panel_vars",
                        classes="section-container",
                    )
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

    def _show_section(self, index: int) -> None:
        """Show the section at `index` and hide the others."""
        panel_ids = ("gc_panel_editor", "gc_panel_vars", "gc_panel_remotes")
        for i, pid in enumerate(panel_ids):
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
        self.query_one("#gc_remote_url", LabeledInput).query_one(
            "#li_input"
        ).value = rc.url
        self.query_one("#gc_remote_pushurl", LabeledInput).query_one(
            "#li_input"
        ).value = rc.pushurl or ""
        self.query_one("#gc_remote_fetch", LabeledInput).query_one(
            "#li_input"
        ).value = rc.fetch or ""

    def _clear_remote_fields(self) -> None:
        for wid in ("#gc_remote_url", "#gc_remote_pushurl", "#gc_remote_fetch"):
            self.query_one(wid, LabeledInput).query_one("#li_input").value = ""

    def _apply_field_edits(self) -> None:
        """Write back any edits in the url/pushurl/fetch fields to the
        currently selected remote in self._remotes."""
        sel = self._selected_remote()
        if sel is None:
            return
        alias, name = sel
        url = self.query_one("#gc_remote_url", LabeledInput).value.strip()
        if not url:
            return
        pushurl = self.query_one("#gc_remote_pushurl", LabeledInput).value.strip()
        fetch = self.query_one("#gc_remote_fetch", LabeledInput).value.strip()
        if alias not in self._remotes:
            self._remotes[alias] = {}
        self._remotes[alias][name] = RemoteConfig(
            url=url,
            pushurl=pushurl or None,
            fetch=fetch or None,
        )

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

    async def _add_remote(self) -> None:
        """Open the single-form AddRemoteScreen."""
        # Apply any pending field edits before opening the form
        self._apply_field_edits()
        sel = self._selected_remote()
        default_alias = sel[0] if sel else ""
        result = await self.app.push_screen_wait(
            AddRemoteScreen(
                existing_aliases=list(self._remotes),
                default_alias=default_alias,
            )
        )
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
        """Remove the selected remote from the working copy."""
        # Apply field edits first so we don't lose unsaved changes
        self._apply_field_edits()
        sel = self._selected_remote()
        if sel is None:
            return
        alias, name = sel
        if alias in self._remotes:
            self._remotes[alias].pop(name, None)
            if not self._remotes[alias]:
                del self._remotes[alias]
        self._refresh_remotes_list()

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _try_save(self) -> None:
        # Apply any pending remote field edits
        self._apply_field_edits()

        # Editor
        editor = self.query_one("#gc_editor", LabeledInput).value.strip() or "code"

        # Vars
        vars_editor = self.query_one("#gc_vars", VarsEditor)
        vars_dict = vars_editor.get_vars()

        self.dismiss(Config(
            vars=vars_dict,
            remotes=dict(self._remotes),
            version=self._config.version,
            editor=editor,
        ))

    def action_save(self) -> None:
        """Ctrl+S binding."""
        self._try_save()
