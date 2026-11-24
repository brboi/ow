"""Policies of the shared command helpers: stack removal, combined plugin sets, running and reset hooks."""

from __future__ import annotations

import subprocess
import tomllib
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from ow import term
from ow.commands import _runtime
from ow.commands.run import _confirm_reset
from ow.core import stacks, trust
from ow.core.plugins import Answers, GeneratedFile, hookimpl, load_plugins
from ow.core.workspace import Repo, Workspace
from ow.errors import UsageError


def _ws(root: Path, name: str = "ws", repos: tuple[Repo, ...] = (), *, is_stack: bool = False) -> Workspace:
    return Workspace(root=root, name=name, repos=repos, settings={}, is_stack=is_stack)


def _module(name: str, **hooks: object) -> types.ModuleType:
    mod = types.ModuleType(name)
    for hook, fn in hooks.items():
        setattr(mod, hook, hookimpl(fn))
    return mod


def _multi_root(tmp_path: Path, *names: str) -> Path:
    root = tmp_path / "multi"
    (root / ".ow").mkdir(parents=True)
    (root / ".ow" / "config.toml").write_text("".join(f"[stacks.{name}]\n" for name in names))
    for name in names:
        (root / name).mkdir()
    return root


def test_removing_a_stack_keeps_the_root_config_when_another_stack_fails_to_open(tmp_path, monkeypatch):
    root = _multi_root(tmp_path, "a", "b")
    monkeypatch.setattr(stacks, "load_multi_workspace", lambda *args, **kwargs: None)

    _runtime.remove_workspace_root(_ws(root / "a", "a", is_stack=True), load_plugins())

    assert not (root / "a").exists()
    assert (root / "b").is_dir()
    config = tomllib.loads((root / ".ow" / "config.toml").read_text())
    assert set(config["stacks"]) == {"b"}


def test_removing_the_last_stack_removes_the_root_config(tmp_path):
    root = _multi_root(tmp_path, "a")

    _runtime.remove_workspace_root(_ws(root / "a", "a", is_stack=True), load_plugins())

    assert not root.exists()


def test_failing_ow_running_is_warned_about_and_counts_as_not_running(tmp_path, capsys):
    def ow_running(ws):
        raise RuntimeError("socket gone")

    plugins = load_plugins((("flaky", _module("flaky", ow_running=ow_running)),))

    assert _runtime.plugin_running(_ws(tmp_path), plugins) == []
    assert "warning: flaky: socket gone" in capsys.readouterr().err
    assert _runtime.plugin_running(_ws(tmp_path), plugins, report=False) == []
    assert capsys.readouterr().err == ""


def test_reset_asks_about_a_plugin_that_resets_without_a_preview(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(term, "interactive", lambda: False)
    plugins = load_plugins((("quiet", _module("quiet", ow_reset_run=lambda ws: ["did it"])),))

    with pytest.raises(UsageError, match="re-run with -y"):
        _confirm_reset([_ws(tmp_path)], plugins, yes=False)
    assert "#   quiet: local state" in capsys.readouterr().out

    _confirm_reset([_ws(tmp_path)], plugins, yes=True)


def test_reset_does_not_ask_when_a_previewing_plugin_has_nothing_to_drop(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(term, "interactive", lambda: False)
    mod = _module("odoo_like", ow_reset_run=lambda ws: None, ow_reset_preview=lambda ws: None)
    plugins = load_plugins((("odoo_like", mod),))

    _confirm_reset([_ws(tmp_path)], plugins, yes=False)

    assert capsys.readouterr().out == ""


def test_multi_workspace_files_get_answers_bound_to_the_first_stack(tmp_path):
    seen: list[Answers] = []

    def ow_multi_workspace_files(multi, answers):
        seen.append(answers)
        return [GeneratedFile(path="m.txt", data=b"x")]

    plugins = load_plugins((("rec", _module("rec", ow_multi_workspace_files=ow_multi_workspace_files)),))
    first, second = _ws(tmp_path / "a", "a", is_stack=True), _ws(tmp_path / "b", "b", is_stack=True)

    stacks.project_multi_workspace_files(stacks.MultiWorkspace(root=tmp_path, name="m", stacks=(first, second)), plugins)

    assert len(seen) == 1 and isinstance(seen[0], Answers)
    assert seen[0]._ws is first


def test_multi_workspace_files_are_not_asked_for_without_stacks(tmp_path):
    calls: list[object] = []
    plugins = load_plugins(
        (("rec", _module("rec", ow_multi_workspace_files=lambda multi, answers: calls.append(answers) or [])),)
    )

    stacks.project_multi_workspace_files(stacks.MultiWorkspace(root=tmp_path, name="m", stacks=()), plugins)

    assert calls == []


def _plugin_repo(parent: Path, name: str, plugin_name: str) -> Path:
    repo = parent / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "ow_plugin.py").write_text(f"OW_API_VERSION = 1\nPLUGIN_NAME = {plugin_name!r}\n")
    return repo


def test_combined_plugins_warns_about_a_plugin_another_target_already_brought(tmp_path, capsys):
    one = _plugin_repo(tmp_path / "src", "one", "voipdemo")
    two = _plugin_repo(tmp_path / "src", "two", "voipdemo")
    trust.trust_path(tmp_path / "src")
    base = load_plugins()

    def target(repo: Path, name: str) -> _runtime.Target:
        ws = _ws(tmp_path / name, name, (Repo(name=repo.name, path=repo, branch=None, base=None),))
        plugins, warnings = base.with_repo_plugins([(repo.name, repo)])
        assert warnings == ()
        return _runtime.Target(plugins, (SimpleNamespace(workspace=ws, warnings=()),))

    combined = _runtime.combined_plugins(base, [target(one, "ws1"), target(two, "ws2")])

    assert [path for _name, path in combined.external_plugins] == [(one / "ow_plugin.py").resolve()]
    err = capsys.readouterr().err
    assert "warning: skipping plugin" in err and "two" in err and "already registered" in err
