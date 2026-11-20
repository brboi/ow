"""Tests for plugin loader and specs."""

from __future__ import annotations

import os
import tempfile
import types
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

import pytest
import typer
from ow.core.plugins import (
    Diagnostic,
    GeneratedFile,
    KnownRepo,
    PluginError,
    Plugins,
    Setting,
    hookimpl,
    load_plugins,
)
from ow.specs.editors import DebugLaunch, EditorContribution, merge as merge_editors
from ow.specs.mise import MiseEnvironment, MiseSpec, merge as merge_mise


@dataclass(frozen=True)
class FakeWorkspace:
    """Minimal workspace for testing."""

    root: Path
    settings: Mapping[str, object]


def test_generated_file_coerces_string_path():
    """GeneratedFile normalizes a str path to PurePosixPath."""
    gf = GeneratedFile(path="another/file.txt", data=b"content")  # type: ignore[arg-type]
    assert isinstance(gf.path, PurePosixPath)
    assert gf.path == PurePosixPath("another/file.txt")


def test_load_plugins_basic():
    """Test loading plugins with minimal stub modules."""
    plugin_a = types.ModuleType("plugin_a")
    plugin_b = types.ModuleType("plugin_b")

    @hookimpl
    def ow_settings():
        return []

    @hookimpl
    def ow_settings_b():
        return [Setting(key="b.setting", type=str, default="b", help="B plugin")]

    # Rename for pluggy to recognize
    ow_settings_b.__name__ = "ow_settings"

    plugin_a.ow_settings = ow_settings
    plugin_b.ow_settings = ow_settings_b
    plugin_a.HOOKSPECS = ()
    plugin_b.HOOKSPECS = ()

    plugins = load_plugins(builtins=(("a", plugin_a), ("b", plugin_b)))
    assert plugins.names == ("a", "b")


def test_plugins_settings_includes_enabled_and_plugin_settings():
    """Test that settings() includes synthesized enabled setting and plugin settings."""
    plugin_a = types.ModuleType("plugin_a")

    @hookimpl
    def ow_settings():
        return [
            Setting(key="a.custom", type=str, default="value", help="Custom setting"),
        ]

    plugin_a.ow_settings = ow_settings
    plugin_a.HOOKSPECS = ()

    plugins = load_plugins(builtins=(("a", plugin_a),))
    settings = plugins.settings()

    # Should have both synthesized enabled and plugin's custom setting
    setting_keys = [s.key for s in settings]
    assert "a.enabled" in setting_keys
    assert "a.custom" in setting_keys

    # Check the enabled setting
    enabled_setting = next(s for s in settings if s.key == "a.enabled")
    assert enabled_setting.type == bool
    assert enabled_setting.default is True


def test_plugins_call_respects_tuple_order():
    """Test that call() returns results in tuple order."""
    plugin_a = types.ModuleType("plugin_a")
    plugin_b = types.ModuleType("plugin_b")

    @hookimpl
    def ow_settings():
        return []

    @hookimpl
    def ow_diagnostics(ws):
        return [Diagnostic(level="info", text="from a")]

    @hookimpl
    def ow_settings_b():
        return []

    ow_settings_b.__name__ = "ow_settings"

    @hookimpl
    def ow_diagnostics_b(ws):
        return [Diagnostic(level="info", text="from b")]

    ow_diagnostics_b.__name__ = "ow_diagnostics"

    plugin_a.ow_settings = ow_settings
    plugin_a.ow_diagnostics = ow_diagnostics
    plugin_a.HOOKSPECS = ()

    plugin_b.ow_settings = ow_settings_b
    plugin_b.ow_diagnostics = ow_diagnostics_b
    plugin_b.HOOKSPECS = ()

    plugins = load_plugins(builtins=(("a", plugin_a), ("b", plugin_b)))
    fake_ws = FakeWorkspace(root=Path("/tmp"), settings={})

    results = plugins.call("ow_diagnostics", ws=fake_ws)

    # Results should be in tuple order: a, then b
    assert len(results) == 2
    assert results[0][0] == "a"
    assert results[1][0] == "b"


def test_plugins_call_skips_disabled_plugin():
    """Test that call() skips disabled plugins when ws is given."""
    plugin_a = types.ModuleType("plugin_a")
    plugin_b = types.ModuleType("plugin_b")

    @hookimpl
    def ow_settings():
        return []

    @hookimpl
    def ow_diagnostics(ws):
        return [Diagnostic(level="info", text="from a")]

    @hookimpl
    def ow_settings_b():
        return []

    ow_settings_b.__name__ = "ow_settings"

    @hookimpl
    def ow_diagnostics_b(ws):
        return [Diagnostic(level="info", text="from b")]

    ow_diagnostics_b.__name__ = "ow_diagnostics"

    plugin_a.ow_settings = ow_settings
    plugin_a.ow_diagnostics = ow_diagnostics
    plugin_a.HOOKSPECS = ()

    plugin_b.ow_settings = ow_settings_b
    plugin_b.ow_diagnostics = ow_diagnostics_b
    plugin_b.HOOKSPECS = ()

    plugins = load_plugins(builtins=(("a", plugin_a), ("b", plugin_b)))

    # Create workspace where plugin b is disabled
    fake_ws = FakeWorkspace(root=Path("/tmp"), settings={"b.enabled": False})

    results = plugins.call("ow_diagnostics", ws=fake_ws)

    # Only a should be in results
    assert len(results) == 1
    assert results[0][0] == "a"


def test_plugins_settings_lists_every_plugin_in_order():
    """settings() lists each plugin's enabled flag before its own settings, in plugin order."""
    plugin_a = types.ModuleType("plugin_a")
    plugin_b = types.ModuleType("plugin_b")

    @hookimpl
    def ow_settings():
        return []

    @hookimpl
    def ow_settings_b():
        return [Setting(key="b.setting", type=str, default="b", help="B")]

    ow_settings_b.__name__ = "ow_settings"

    plugin_a.ow_settings = ow_settings
    plugin_a.HOOKSPECS = ()

    plugin_b.ow_settings = ow_settings_b
    plugin_b.HOOKSPECS = ()

    plugins = load_plugins(builtins=(("a", plugin_a), ("b", plugin_b)))

    assert [s.key for s in plugins.settings()] == ["a.enabled", "b.enabled", "b.setting"]


def test_plugins_call_raises_plugin_error_on_exception():
    """Test that call() converts exceptions to PluginError naming the plugin."""
    plugin_a = types.ModuleType("plugin_a")

    @hookimpl
    def ow_settings():
        return []

    @hookimpl
    def ow_diagnostics(ws):
        raise ValueError("Something broke")

    plugin_a.ow_settings = ow_settings
    plugin_a.ow_diagnostics = ow_diagnostics
    plugin_a.HOOKSPECS = ()

    plugins = load_plugins(builtins=(("a", plugin_a),))
    fake_ws = FakeWorkspace(root=Path("/tmp"), settings={})

    with pytest.raises(PluginError) as exc_info:
        plugins.call("ow_diagnostics", ws=fake_ws)

    error = exc_info.value
    assert error.plugin == "a"
    assert error.hook == "ow_diagnostics"
    assert "ValueError" in error.cause.__class__.__name__


def test_plugins_call_reraises_existing_plugin_error():
    """Test that call() re-raises PluginError unchanged to keep original plugin name."""
    plugin_a = types.ModuleType("plugin_a")
    plugin_b = types.ModuleType("plugin_b")

    @hookimpl
    def ow_settings():
        return []

    @hookimpl
    def ow_diagnostics(ws):
        # Plugin a raises a PluginError claiming to be from plugin b
        # This tests whether call() re-raises it unchanged (preserving "b")
        # vs wrapping it (which would change it to "a")
        raise PluginError("b", "ow_diagnostics", "something broke")

    @hookimpl
    def ow_settings_b():
        return []

    ow_settings_b.__name__ = "ow_settings"

    @hookimpl
    def ow_diagnostics_b(ws):
        return []

    ow_diagnostics_b.__name__ = "ow_diagnostics"

    plugin_a.ow_settings = ow_settings
    plugin_a.ow_diagnostics = ow_diagnostics
    plugin_a.HOOKSPECS = ()

    plugin_b.ow_settings = ow_settings_b
    plugin_b.ow_diagnostics = ow_diagnostics_b
    plugin_b.HOOKSPECS = ()

    plugins = load_plugins(builtins=(("a", plugin_a), ("b", plugin_b)))
    fake_ws = FakeWorkspace(root=Path("/tmp"), settings={})

    with pytest.raises(PluginError) as exc_info:
        plugins.call("ow_diagnostics", ws=fake_ws)

    # The error should be re-raised unchanged, still blaming plugin b
    error = exc_info.value
    assert error.plugin == "b"


def test_plugins_answers_returns_callable():
    """Test that answers() returns a callable that collects results in tuple order."""
    plugin_a = types.ModuleType("plugin_a")

    @hookimpl
    def ow_settings():
        return []

    @hookimpl
    def mise_environment(ws):
        return MiseEnvironment(env={"FROM": "a"})

    plugin_a.ow_settings = ow_settings
    plugin_a.mise_environment = mise_environment
    plugin_a.HOOKSPECS = (MiseSpec,)

    plugins = load_plugins(builtins=(("a", plugin_a),))
    fake_ws = FakeWorkspace(root=Path("/tmp"), settings={})

    answers_fn = plugins.answers(fake_ws)
    results = answers_fn("mise_environment")

    # Should return list of MiseEnvironment results without the plugin names
    assert len(results) == 1
    assert isinstance(results[0], MiseEnvironment)
    assert results[0].env == {"FROM": "a"}

def test_plugins_answers_skips_disabled_plugin():
    """Test that answers() skips disabled plugins."""
    plugin_a = types.ModuleType("plugin_a")
    plugin_b = types.ModuleType("plugin_b")

    @hookimpl
    def ow_settings():
        return []

    @hookimpl
    def mise_environment_a(ws):
        return MiseEnvironment(env={"FROM": "a"})

    @hookimpl
    def ow_settings_b():
        return []

    @hookimpl
    def mise_environment_b(ws):
        return MiseEnvironment(env={"FROM": "b"})

    # Set function names properly for hookimpl
    mise_environment_a.__name__ = "mise_environment"
    mise_environment_b.__name__ = "mise_environment"

    plugin_a.ow_settings = ow_settings
    plugin_a.mise_environment = mise_environment_a
    plugin_a.HOOKSPECS = (MiseSpec,)

    plugin_b.ow_settings = ow_settings_b
    plugin_b.mise_environment = mise_environment_b
    plugin_b.HOOKSPECS = (MiseSpec,)

    plugins = load_plugins(builtins=(("a", plugin_a), ("b", plugin_b)))
    fake_ws = FakeWorkspace(root=Path("/tmp"), settings={"b.enabled": False})

    answers_fn = plugins.answers(fake_ws)
    results = answers_fn("mise_environment")

    # Should skip disabled plugin b
    assert len(results) == 1
    assert results[0].env == {"FROM": "a"}

def test_plugins_add_commands_creates_subapps():
    """Test that add_commands() creates and registers subapps for plugins."""
    plugin_a = types.ModuleType("plugin_a")

    @hookimpl
    def ow_settings():
        return []

    @hookimpl
    def ow_commands(app):
        @app.command()
        def hello():
            """Say hello."""
            typer.echo("Hello")

    plugin_a.ow_settings = ow_settings
    plugin_a.ow_commands = ow_commands
    plugin_a.HOOKSPECS = ()

    plugins = load_plugins(builtins=(("a", plugin_a),))
    root_app = typer.Typer()
    plugins.add_commands(root_app)

    # Verify the subapp group was added under the plugin name
    assert [g.name for g in root_app.registered_groups] == ["a"]


def test_plugins_add_commands_skips_plugins_with_no_commands():
    """Test that add_commands() doesn't add a group if plugin registered no commands."""
    plugin_a = types.ModuleType("plugin_a")
    plugin_b = types.ModuleType("plugin_b")

    @hookimpl
    def ow_settings():
        return []

    @hookimpl
    def ow_commands(app):
        pass  # No commands registered

    @hookimpl
    def ow_settings_b():
        return []

    ow_settings_b.__name__ = "ow_settings"

    @hookimpl
    def ow_commands_b(app):
        @app.command()
        def hello():
            """Say hello."""
            typer.echo("Hello")

    ow_commands_b.__name__ = "ow_commands"

    plugin_a.ow_settings = ow_settings
    plugin_a.ow_commands = ow_commands
    plugin_a.HOOKSPECS = ()

    plugin_b.ow_settings = ow_settings_b
    plugin_b.ow_commands = ow_commands_b
    plugin_b.HOOKSPECS = ()

    plugins = load_plugins(builtins=(("a", plugin_a), ("b", plugin_b)))
    root_app = typer.Typer()
    plugins.add_commands(root_app)

    # Plugin b should be added (has command), plugin a should not
    assert [g.name for g in root_app.registered_groups] == ["b"]


def test_hookspecs_declared_once():
    """Test that HOOKSPECS declared by multiple modules are added only once."""
    plugin_a = types.ModuleType("plugin_a")
    plugin_b = types.ModuleType("plugin_b")

    @hookimpl
    def ow_settings():
        return []

    @hookimpl
    def mise_environment(ws):
        return None

    @hookimpl
    def ow_settings_b():
        return []

    ow_settings_b.__name__ = "ow_settings"

    @hookimpl
    def mise_environment_b(ws):
        return None

    mise_environment_b.__name__ = "mise_environment"

    plugin_a.ow_settings = ow_settings
    plugin_a.mise_environment = mise_environment
    plugin_a.HOOKSPECS = (MiseSpec,)

    plugin_b.ow_settings = ow_settings_b
    plugin_b.mise_environment = mise_environment_b
    plugin_b.HOOKSPECS = (MiseSpec,)  # Same spec as plugin_a

    # Should not raise ValueError about spec being added twice
    plugins = load_plugins(builtins=(("a", plugin_a), ("b", plugin_b)))
    assert plugins.names == ("a", "b")


def test_check_pending_rejects_misspelled_hookimpl():
    """Test that load_plugins raises PluginError for misspelled hookimpl."""
    plugin_a = types.ModuleType("plugin_a")

    @hookimpl
    def ow_settings():
        return []

    @hookimpl
    def ow_diagnostics_typo(ws):  # Misspelled
        return []

    plugin_a.ow_settings = ow_settings
    plugin_a.ow_diagnostics_typo = ow_diagnostics_typo
    plugin_a.HOOKSPECS = ()

    with pytest.raises(PluginError) as exc_info:
        load_plugins(builtins=(("a", plugin_a),))

    error = exc_info.value
    assert error.hook == "check_pending"


def test_load_plugins_rejects_non_tuple_hookspecs():
    """A non-tuple HOOKSPECS is a PluginError labelled HOOKSPECS, not check_pending."""
    plugin_a = types.ModuleType("plugin_a")
    plugin_a.HOOKSPECS = MiseSpec  # not a tuple

    with pytest.raises(PluginError) as exc_info:
        load_plugins(builtins=(("a", plugin_a),))

    error = exc_info.value
    assert error.plugin == "a"
    assert error.hook == "HOOKSPECS"


def test_load_plugins_wraps_hookimpl_signature_mismatch():
    """A hookimpl whose signature does not match its spec becomes a PluginError."""
    plugin_a = types.ModuleType("plugin_a")

    @hookimpl
    def ow_diagnostics(ws, unexpected):  # 'unexpected' is not in the spec
        return []

    plugin_a.ow_diagnostics = ow_diagnostics
    plugin_a.HOOKSPECS = ()

    with pytest.raises(PluginError) as exc_info:
        load_plugins(builtins=(("a", plugin_a),))

    error = exc_info.value
    assert error.plugin == "a"
    assert error.hook == "register"


def test_merge_mise_environment_empty():
    """Test merge of empty mise environments."""
    result = merge_mise([])
    assert result is None


def test_merge_mise_environment_single():
    """Test merge of single mise environment."""
    env = MiseEnvironment(
        env={"KEY": "value"},
        path=("path1",),
        python=((3, 9), (3, 12)),
        postinstall=("cmd1",),
        aliases={"alias1": "cmd1"},
    )
    result = merge_mise([env])
    assert result == env


def test_merge_mise_environment_concatenates_path_postinstall():
    """Test that merge concatenates path and postinstall."""
    env1 = MiseEnvironment(path=("p1", "p2"), postinstall=("c1",))
    env2 = MiseEnvironment(path=("p3",), postinstall=("c2", "c3"))
    result = merge_mise([env1, env2])

    assert result.path == ("p1", "p2", "p3")
    assert result.postinstall == ("c1", "c2", "c3")


def test_merge_mise_environment_later_wins_for_env():
    """Test that later env wins for environment variables."""
    env1 = MiseEnvironment(env={"KEY": "value1", "OTHER": "val"})
    env2 = MiseEnvironment(env={"KEY": "value2"})
    result = merge_mise([env1, env2])

    assert result.env["KEY"] == "value2"
    assert result.env["OTHER"] == "val"


def test_merge_mise_environment_python_later_non_none_wins():
    """Test that later non-None python wins (P14)."""
    env1 = MiseEnvironment(python=((3, 9), (3, 11)))
    env2 = MiseEnvironment(python=None)
    env3 = MiseEnvironment(python=((3, 10), (3, 12)))

    result = merge_mise([env1, env2, env3])
    assert result.python == ((3, 10), (3, 12))


def test_merge_editors_contribution_empty():
    """Test merge of empty editor contributions."""
    result = merge_editors([])

    assert result.launches == ()
    assert result.language_servers == {}
    assert result.excludes == ()


def test_merge_editors_contribution_concatenates_launches():
    """Test that merge concatenates launches."""
    launch1 = DebugLaunch(
        name="Launch1",
        program=Path("/path/to/prog1"),
        args=("arg1",),
        cwd=Path("/cwd1"),
        env={},
    )
    launch2 = DebugLaunch(
        name="Launch2",
        program=Path("/path/to/prog2"),
        args=("arg2",),
        cwd=Path("/cwd2"),
        env={},
    )

    contrib1 = EditorContribution(launches=(launch1,))
    contrib2 = EditorContribution(launches=(launch2,))

    result = merge_editors([contrib1, contrib2])

    assert len(result.launches) == 2
    assert result.launches[0].name == "Launch1"
    assert result.launches[1].name == "Launch2"


def test_merge_editors_contribution_deduplicates_excludes():
    """Test that merge deduplicates excludes."""
    contrib1 = EditorContribution(excludes=("*.po", "*.pot", "*.po"))
    contrib2 = EditorContribution(excludes=("*.pot", "node_modules"))

    result = merge_editors([contrib1, contrib2])

    assert result.excludes == ("*.po", "*.pot", "node_modules")


def test_merge_editors_contribution_language_servers_later_wins():
    """Test that merge of language_servers has later answer win per key."""
    contrib1 = EditorContribution(
        language_servers={"odoo": {"setting1": "value1", "setting2": "value2"}}
    )
    contrib2 = EditorContribution(
        language_servers={"odoo": {"setting2": "new_value2", "setting3": "value3"}}
    )

    result = merge_editors([contrib1, contrib2])

    assert result.language_servers["odoo"]["setting1"] == "value1"
    assert result.language_servers["odoo"]["setting2"] == "new_value2"
    assert result.language_servers["odoo"]["setting3"] == "value3"


def test_merge_editors_contribution_multiple_servers():
    """Test merge with multiple language servers."""
    contrib1 = EditorContribution(language_servers={"pyright": {"key1": "v1"}})
    contrib2 = EditorContribution(language_servers={"pylance": {"key2": "v2"}})

    result = merge_editors([contrib1, contrib2])

    assert "pyright" in result.language_servers
    assert "pylance" in result.language_servers
    assert result.language_servers["pyright"]["key1"] == "v1"
    assert result.language_servers["pylance"]["key2"] == "v2"


def test_plugins_settings_rejects_invalid_key_prefix():
    """Test that settings() raises PluginError for keys not starting with <plugin>."""
    plugin_a = types.ModuleType("plugin_a")

    @hookimpl
    def ow_settings():
        # Return a setting with invalid key prefix
        return [Setting(key="invalid.key", type=str, default="value", help="Bad key")]

    plugin_a.ow_settings = ow_settings
    plugin_a.HOOKSPECS = ()

    plugins = load_plugins(builtins=(("a", plugin_a),))

    with pytest.raises(PluginError) as exc_info:
        plugins.settings()

    error = exc_info.value
    assert error.plugin == "a"
    assert error.hook == "ow_settings"
    assert "invalid.key" in str(error.cause)


def test_plugins_call_with_undeclared_hook_returns_empty_list():
    """Test that call() returns [] for undeclared hooks."""
    plugin_a = types.ModuleType("plugin_a")

    @hookimpl
    def ow_settings():
        return []

    plugin_a.ow_settings = ow_settings
    plugin_a.HOOKSPECS = ()

    plugins = load_plugins(builtins=(("a", plugin_a),))

    # Call with a hook that doesn't exist and has no implementations
    results = plugins.call("nonexistent_hook")

    assert results == []


def test_plugins_call_with_errors_collect():
    """Test that call(errors='collect') collects PluginErrors instead of raising."""
    plugin_a = types.ModuleType("plugin_a")
    plugin_b = types.ModuleType("plugin_b")
    plugin_c = types.ModuleType("plugin_c")

    @hookimpl
    def ow_diagnostics_a(ws):
        return [Diagnostic("info", "diagnostic from a")]

    @hookimpl
    def ow_diagnostics_b(ws):
        raise ValueError("Something went wrong in b")

    @hookimpl
    def ow_diagnostics_c(ws):
        return [Diagnostic("warning", "diagnostic from c")]

    plugin_a.ow_diagnostics = ow_diagnostics_a
    plugin_a.HOOKSPECS = ()

    plugin_b.ow_diagnostics = ow_diagnostics_b
    plugin_b.HOOKSPECS = ()

    plugin_c.ow_diagnostics = ow_diagnostics_c
    plugin_c.HOOKSPECS = ()

    plugins = load_plugins(builtins=(("a", plugin_a), ("b", plugin_b), ("c", plugin_c)))
    fake_ws = FakeWorkspace(root=Path("/tmp"), settings={})

    # With errors="collect", all results including errors should be returned
    results = plugins.call("ow_diagnostics", ws=fake_ws, errors="collect")

    # Should have 3 results: a, b (error), c
    assert len(results) == 3

    # First result should be from plugin a
    assert results[0][0] == "a"
    assert isinstance(results[0][1], list)
    assert results[0][1][0].text == "diagnostic from a"

    # Second result should be the error from plugin b as a PluginError
    assert results[1][0] == "b"
    assert isinstance(results[1][1], PluginError)
    assert "Something went wrong in b" in str(results[1][1].cause)

    # Third result should be from plugin c
    assert results[2][0] == "c"
    assert isinstance(results[2][1], list)
    assert results[2][1][0].text == "diagnostic from c"


def test_plugins_call_skips_disabled_plugin_without_ws():
    """Test that call() skips disabled plugins even without ws by reading global config."""
    plugin_a = types.ModuleType("plugin_a")
    plugin_b = types.ModuleType("plugin_b")

    @hookimpl
    def ow_settings():
        return []

    @hookimpl
    def ow_known_repos():
        return [KnownRepo(name="a-repo", url="git@github.com:a/repo.git")]

    @hookimpl
    def ow_settings_b():
        return []

    ow_settings_b.__name__ = "ow_settings"

    @hookimpl
    def ow_known_repos_b():
        return [KnownRepo(name="b-repo", url="git@github.com:b/repo.git")]

    ow_known_repos_b.__name__ = "ow_known_repos"

    plugin_a.ow_settings = ow_settings
    plugin_a.ow_known_repos = ow_known_repos
    plugin_a.HOOKSPECS = ()

    plugin_b.ow_settings = ow_settings_b
    plugin_b.ow_known_repos = ow_known_repos_b
    plugin_b.HOOKSPECS = ()

    # Create a temporary global config file with b.enabled=false
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write("b.enabled = false\n")
        config_file = f.name

    import ow.core.paths

    original = ow.core.paths.global_config
    ow.core.paths.global_config = lambda: Path(config_file)
    try:
        plugins = load_plugins(builtins=(("a", plugin_a), ("b", plugin_b)))
        assert [name for name, _ in plugins.call("ow_known_repos")] == ["a"]
    finally:
        ow.core.paths.global_config = original
        os.unlink(config_file)

