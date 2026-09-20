"""Tests for write_global_config — round-tripping the hand-commented config.

The schema-2 writer serializes the documented bootstrap (`_DEFAULT_CONFIG`)
with the record's values applied, so the comments that explain the keys
survive every save. A schema-1 file is not rebuilt: it is re-read and only
its remotes, editor and theme are updated, in place.
"""

from dataclasses import replace

import pytest

from ow.utils import paths
from ow.utils.config import (
    _DEFAULT_CONFIG,
    Config,
    LegacyConfig,
    RemoteConfig,
    dumps_global_config,
    load_config,
    load_global_config,
    write_global_config,
)
from ow.utils.options import MiseOverrides, OdooOverrides


def _uncommented(text: str) -> list[str]:
    """The file's real content: the bootstrap documents every key in a comment."""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


@pytest.fixture
def default_config_path(xdg):
    """Persist the documented bootstrap and return its path."""
    path = paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_DEFAULT_CONFIG, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Comment preservation
# ---------------------------------------------------------------------------


class TestCommentPreservation:
    def test_writing_unchanged_config_keeps_every_comment_line(self, default_config_path):
        """Writing an unchanged Config leaves every comment line present."""
        cfg = load_config(default_config_path)
        write_global_config(cfg)

        result = default_config_path.read_text(encoding="utf-8")
        for line in _DEFAULT_CONFIG.splitlines():
            if line.strip().startswith("#"):
                assert line in result, f"Comment lost: {line!r}"


# ---------------------------------------------------------------------------
# editor / theme
# ---------------------------------------------------------------------------


class TestEditorAndTheme:
    def test_setting_editor_lands_before_the_first_table(self, default_config_path):
        """A bare key written after a table header would not be valid TOML."""
        cfg = load_config(default_config_path)
        write_global_config(replace(cfg, editor="nvim"))

        content = default_config_path.read_text(encoding="utf-8")
        editor_pos = content.index('editor = "nvim"')
        remotes_pos = content.index("[remotes.community]")
        assert editor_pos < remotes_pos

    def test_setting_editor_reparses_with_new_value(self, default_config_path):
        cfg = load_config(default_config_path)
        write_global_config(replace(cfg, editor="nvim"))

        assert load_config(default_config_path).editor == "nvim"

    def test_default_editor_not_written_when_absent(self, default_config_path):
        """When editor is 'code' (default) and the key is absent, it stays absent."""
        cfg = load_config(default_config_path)
        assert cfg.editor == "code"
        write_global_config(cfg)

        lines = [
            line for line in default_config_path.read_text(encoding="utf-8").splitlines()
            if line.startswith("editor")
        ]
        assert lines == []

    def test_theme_round_trips(self, default_config_path):
        """The dashboard's theme picker writes the config the next launch reads."""
        cfg = load_global_config()
        assert cfg.theme == "textual-dark"

        write_global_config(replace(cfg, theme="dracula"))

        assert load_global_config().theme == "dracula"
        assert '\ntheme = "dracula"' in default_config_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# owignore
# ---------------------------------------------------------------------------


class TestOwignore:
    def test_ignore_patterns_round_trip(self, default_config_path):
        cfg = load_config(default_config_path)
        write_global_config(replace(cfg, owignore=(".zed/**", "*.lock")))

        assert load_config(default_config_path).owignore == (".zed/**", "*.lock")

    def test_removing_the_last_pattern_removes_the_key(self, default_config_path):
        cfg = load_config(default_config_path)
        write_global_config(replace(cfg, owignore=(".zed/**",)))
        write_global_config(load_config(default_config_path))
        write_global_config(replace(load_config(default_config_path), owignore=()))

        written = _uncommented(default_config_path.read_text(encoding="utf-8"))
        assert not any(line.startswith("owignore") for line in written)


# ---------------------------------------------------------------------------
# Remotes
# ---------------------------------------------------------------------------


class TestRemotes:
    def test_adding_remote_alias_produces_table(self, default_config_path):
        """Adding a remote alias produces [remotes.<alias>] with origin.url on one line."""
        cfg = load_config(default_config_path)
        cfg.remotes["enterprise"] = {
            "origin": RemoteConfig(url="git@github.com:odoo/enterprise.git"),
        }
        write_global_config(cfg)

        content = default_config_path.read_text(encoding="utf-8")
        assert "[remotes.enterprise]" in content
        assert 'url = "git@github.com:odoo/enterprise.git"' in content

    def test_deleting_remote_alias_removes_it(self, default_config_path):
        cfg = load_config(default_config_path)
        del cfg.remotes["community"]
        write_global_config(cfg)

        assert "community" not in load_config(default_config_path).remotes

    def test_an_unknown_subkey_inside_a_remote_entry_is_rejected(self, default_config_path):
        """Silently dropping a user's own key on the next save is the worst case."""
        default_config_path.write_text(
            'version = 2\n\n[remotes.community.origin]\n'
            'url = "git@github.com:odoo/odoo.git"\n'
            'note = "my fork"\n'
        )
        with pytest.raises(ValueError, match="note"):
            load_config(default_config_path)

    def test_pushurl_and_fetch_are_cleared_when_unset(self, default_config_path):
        path = default_config_path
        path.write_text(
            'version = 2\n\n[remotes.community.dev]\n'
            'url = "git@github.com:odoo-dev/odoo.git"\n'
            'pushurl = "git@github.com:odoo-dev/odoo.git"\n'
            'fetch = "+refs/heads/*:refs/remotes/dev/*"\n'
        )
        cfg = load_config(path)
        write_global_config(
            replace(cfg, remotes={"community": {"dev": RemoteConfig(url="git@github.com:odoo-dev/odoo.git")}})
        )

        reloaded = load_config(path)
        assert reloaded.remotes["community"]["dev"].pushurl is None
        assert reloaded.remotes["community"]["dev"].fetch is None


# ---------------------------------------------------------------------------
# Typed options
# ---------------------------------------------------------------------------


class TestTypedOptions:
    def test_typed_defaults_round_trip(self, default_config_path):
        cfg = load_config(default_config_path)
        write_global_config(
            replace(
                cfg,
                odoo=OdooOverrides(http_port=8070, db_password="s3cret", debug_args=()),
                mise=MiseOverrides(python="3.12"),
            )
        )

        reloaded = load_config(default_config_path)
        assert reloaded.odoo == OdooOverrides(http_port=8070, db_password="s3cret", debug_args=())
        assert reloaded.mise == MiseOverrides(python="3.12")

    def test_options_are_sparse(self, default_config_path):
        """A key the record does not set is absent, not a copy of a default."""
        cfg = load_config(default_config_path)
        write_global_config(replace(cfg, odoo=OdooOverrides(http_port=8070)))

        written = _uncommented(default_config_path.read_text(encoding="utf-8"))
        assert "http_port = 8070" in written
        assert not any(line.startswith("db_port") for line in written)

    def test_removing_every_typed_key_removes_the_table(self, default_config_path):
        cfg = load_config(default_config_path)
        write_global_config(replace(cfg, odoo=OdooOverrides(http_port=8070)))
        write_global_config(replace(load_config(default_config_path), odoo=OdooOverrides()))

        written = _uncommented(default_config_path.read_text(encoding="utf-8"))
        assert not any(line.startswith("http_port") for line in written)
        assert "[odoo]" not in written


# ---------------------------------------------------------------------------
# Round trip and file hygiene
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_load_after_write_equals_original(self, default_config_path):
        cfg = Config(
            remotes={
                "community": {
                    "origin": RemoteConfig(url="git@github.com:odoo/odoo.git"),
                    "dev": RemoteConfig(
                        url="git@github.com:odoo-dev/odoo.git",
                        pushurl="git@github.com:odoo-dev/odoo.git",
                        fetch="+refs/heads/*:refs/remotes/dev/*",
                    ),
                },
            },
            odoo=OdooOverrides(http_port=8070),
            mise=MiseOverrides(python="3.12"),
            owignore=(".zed/**",),
            editor="nvim",
            theme="dracula",
        )
        write_global_config(cfg)

        reloaded = load_config(default_config_path)
        assert reloaded.owignore == cfg.owignore
        assert reloaded.odoo == cfg.odoo
        assert reloaded.mise == cfg.mise
        assert reloaded.editor == cfg.editor
        assert reloaded.theme == cfg.theme
        assert set(reloaded.remotes.keys()) == set(cfg.remotes.keys())
        for alias in cfg.remotes:
            assert set(reloaded.remotes[alias].keys()) == set(cfg.remotes[alias].keys())
            for rname in cfg.remotes[alias]:
                orig = cfg.remotes[alias][rname]
                got = reloaded.remotes[alias][rname]
                assert got.url == orig.url
                assert got.pushurl == orig.pushurl
                assert got.fetch == orig.fetch

    def test_written_file_is_private_and_leaves_no_temp(self, default_config_path):
        write_global_config(load_config(default_config_path))

        assert (default_config_path.stat().st_mode & 0o777) == 0o600
        assert list(default_config_path.parent.glob("*.tmp")) == []

    def test_dumps_and_write_agree(self, default_config_path):
        """One writer per format: what a pure dump says is what a save writes."""
        cfg = replace(load_config(default_config_path), theme="dracula")

        expected = dumps_global_config(cfg)
        write_global_config(cfg)

        assert default_config_path.read_bytes() == expected


# ---------------------------------------------------------------------------
# Schema 1: in-place, remotes/editor/theme only
# ---------------------------------------------------------------------------

LEGACY_GLOBAL = """\
# my notes
version = 1

[vars]
http_port = 8071
mystery = "kept"

[remotes.community]
origin.url = "git@github.com:odoo/odoo.git"
"""


class TestLegacyGlobal:
    def test_remotes_editor_and_theme_update_in_place(self, xdg):
        path = paths.config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(LEGACY_GLOBAL)
        cfg = load_config(path)

        write_global_config(
            replace(
                cfg,
                editor="nvim",
                theme="dracula",
                remotes={**cfg.remotes, "enterprise": {"origin": RemoteConfig(url="https://example.test/e.git")}},
            )
        )

        content = path.read_text()
        assert "# my notes" in content
        assert "version = 1" in content
        assert 'mystery = "kept"' in content
        assert 'editor = "nvim"' in content
        assert 'theme = "dracula"' in content
        assert "enterprise" in content
        reloaded = load_config(path)
        assert reloaded.version == 1
        assert reloaded.editor == "nvim"
        assert reloaded.remotes["enterprise"]["origin"].url == "https://example.test/e.git"

    def test_a_remote_subkey_ow_does_not_model_survives(self, xdg):
        """A schema-1 entry is updated in place: a key ow never heard of stays."""
        path = paths.config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'version = 1\n\n[remotes.community.origin]\n'
            'url = "git@github.com:odoo/odoo.git"\n'
            'note = "my fork"\n'
        )
        cfg = load_config(path)

        write_global_config(replace(cfg, editor="nvim"))

        assert 'note = "my fork"' in path.read_text()

    def test_typed_options_cannot_be_saved_to_a_legacy_file(self, xdg):
        path = paths.config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(LEGACY_GLOBAL)
        cfg = load_config(path)

        with pytest.raises(ValueError, match="ow render"):
            write_global_config(replace(cfg, odoo=OdooOverrides(http_port=9999)))

        assert "http_port = 9999" not in path.read_text()

    def test_a_migrated_destination_is_refused(self, xdg):
        path = paths.config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(LEGACY_GLOBAL)
        cfg = load_config(path)
        migrated = dumps_global_config(replace(cfg, version=2, legacy=None))
        path.write_bytes(migrated)

        with pytest.raises(ValueError, match="schema 2, not schema 1"):
            write_global_config(cfg)

        assert path.read_bytes() == migrated


def test_write_global_config_returns_refreshed_legacy_evidence(xdg):
    """The return value is the record to keep using, not a `None` a caller
    has to discard. For a schema-1 save, `legacy.original` comes back as
    exactly the bytes just written — the evidence a caller holding the
    object needs for its *next* save, or for `_reject_typed_change` on one
    after that."""
    path = paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(LEGACY_GLOBAL)
    cfg = load_config(path)

    saved = write_global_config(replace(cfg, editor="nvim"))

    assert saved.legacy is not None
    assert saved.legacy.original == path.read_bytes()
    assert saved.legacy.issues == cfg.legacy.issues


def test_saving_from_the_returned_record_does_not_revert_an_earlier_edit(xdg):
    """Chaining from the returned record — never from the object that was
    loaded — is what keeps a second save from reverting the first one.

    `_set_optional_scalar` writes a record's own field whenever the on-disk
    key already exists, so building the second save on `cfg` (whose
    `editor` is still whatever it was before either save) would reapply
    that stale value over the one the first save just wrote — the "shared
    Config object" hazard the TUI's theme picker hits on a second save.
    Both saves below come only from what `write_global_config` returned;
    neither reloads from disk by hand."""
    path = paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(LEGACY_GLOBAL)
    cfg = load_config(path)

    saved = write_global_config(replace(cfg, editor="nvim"))
    write_global_config(replace(saved, theme="dracula"))

    reloaded = load_config(path)
    assert reloaded.editor == "nvim"
    assert reloaded.theme == "dracula"
