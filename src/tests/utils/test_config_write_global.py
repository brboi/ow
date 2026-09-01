"""Tests for write_global_config — round-tripping the hand-commented config."""

import pytest

from ow.utils import paths
from ow.utils.config import (
    Config,
    RemoteConfig,
    _DEFAULT_CONFIG,
    load_config,
    load_global_config,
    write_global_config,
)


@pytest.fixture
def default_config_path(xdg):
    """Bootstrap the default config and return its path."""
    path = paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_DEFAULT_CONFIG, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Comment preservation
# ---------------------------------------------------------------------------


class TestCommentPreservation:
    def test_writing_unchanged_config_keeps_every_comment_line(self, default_config_path):
        """Writing an unchanged Config leaves every comment line of _DEFAULT_CONFIG present."""
        cfg = load_config(default_config_path)
        write_global_config(cfg)

        original_comments = [
            line for line in _DEFAULT_CONFIG.splitlines()
            if line.strip().startswith("#")
        ]
        result = default_config_path.read_text(encoding="utf-8")
        for comment in original_comments:
            assert comment in result, f"Comment lost: {comment!r}"


# ---------------------------------------------------------------------------
# Editor key
# ---------------------------------------------------------------------------


class TestEditorKey:
    def test_setting_editor_lands_before_vars(self, default_config_path):
        """Setting editor='nvim' on the default file lands the key before [vars]."""
        cfg = load_config(default_config_path)
        cfg.editor = "nvim"
        write_global_config(cfg)

        content = default_config_path.read_text(encoding="utf-8")
        editor_pos = content.index('editor = "nvim"')
        vars_pos = content.index("[vars]")
        assert editor_pos < vars_pos

    def test_setting_editor_reparses_with_new_value(self, default_config_path):
        cfg = load_config(default_config_path)
        cfg.editor = "nvim"
        write_global_config(cfg)

        reloaded = load_config(default_config_path)
        assert reloaded.editor == "nvim"

    def test_default_editor_not_written_when_absent(self, default_config_path):
        """When editor is 'code' (default) and the key is absent, it stays absent."""
        cfg = load_config(default_config_path)
        assert cfg.editor == "code"
        write_global_config(cfg)

        content = default_config_path.read_text(encoding="utf-8")
        # The default config has `# editor = "code"` (commented out)
        # but no uncommented `editor = "code"` line
        lines = [
            line for line in content.splitlines()
            if line.strip().startswith("editor") and not line.strip().startswith("#")
        ]
        assert len(lines) == 0


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
        # The default config has community; remove it
        del cfg.remotes["community"]
        write_global_config(cfg)

        reloaded = load_config(default_config_path)
        assert "community" not in reloaded.remotes


# ---------------------------------------------------------------------------
# Vars
# ---------------------------------------------------------------------------


class TestVars:
    def test_deleting_var_removes_only_that_key(self, default_config_path):
        cfg = load_config(default_config_path)
        original_vars = dict(cfg.vars)
        del cfg.vars["http_port"]
        write_global_config(cfg)

        reloaded = load_config(default_config_path)
        assert "http_port" not in reloaded.vars
        # Other vars survive
        for k, v in original_vars.items():
            if k != "http_port":
                assert reloaded.vars.get(k) == v


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_load_after_write_equals_original(self, default_config_path):
        """Round-trip: load_config(path) after write_global_config(cfg) equals cfg."""
        cfg = Config(
            vars={"http_port": 8069, "db_host": "localhost"},
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
            version=1,
            editor="nvim",
        )
        write_global_config(cfg)

        reloaded = load_config(default_config_path)
        assert reloaded.vars == cfg.vars
        assert reloaded.editor == cfg.editor
        assert reloaded.version == cfg.version
        assert set(reloaded.remotes.keys()) == set(cfg.remotes.keys())
        for alias in cfg.remotes:
            assert set(reloaded.remotes[alias].keys()) == set(cfg.remotes[alias].keys())
            for rname in cfg.remotes[alias]:
                orig = cfg.remotes[alias][rname]
                got = reloaded.remotes[alias][rname]
                assert got.url == orig.url
                assert got.pushurl == orig.pushurl
                assert got.fetch == orig.fetch
