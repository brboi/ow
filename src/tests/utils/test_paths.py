"""Tests for ow.utils.paths.

The seven path functions fall into three families, one per XDG base
directory: config (config_file, templates_dir, services_dir), data
(repos_dir, volumes_dir) and state (index_file, template_base_dir). Each
family is exercised once rather than writing the same test seven times.
"""

from pathlib import Path

import pytest

from ow.utils import paths

CONFIG_FUNCS = [paths.config_file, paths.templates_dir, paths.services_dir]
DATA_FUNCS = [paths.repos_dir, paths.volumes_dir]
STATE_FUNCS = [paths.index_file, paths.template_base_dir]

ALL_FUNCS = CONFIG_FUNCS + DATA_FUNCS + STATE_FUNCS


@pytest.mark.parametrize("func", CONFIG_FUNCS)
def test_config_follows_xdg_config_home(xdg: Path, func) -> None:
    result = func()
    assert result.is_relative_to(xdg / "config" / "ow")


@pytest.mark.parametrize("func", DATA_FUNCS)
def test_data_follows_xdg_data_home(xdg: Path, func) -> None:
    result = func()
    assert result.is_relative_to(xdg / "data" / "ow")


@pytest.mark.parametrize("func", STATE_FUNCS)
def test_state_follows_xdg_state_home(xdg: Path, func) -> None:
    result = func()
    assert result.is_relative_to(xdg / "state" / "ow")


@pytest.mark.parametrize("func", CONFIG_FUNCS)
def test_config_falls_back_to_dot_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, func
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    result = func()
    assert result.is_relative_to(tmp_path / ".config" / "ow")


@pytest.mark.parametrize("func", DATA_FUNCS)
def test_data_falls_back_to_dot_local_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, func
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    result = func()
    assert result.is_relative_to(tmp_path / ".local" / "share" / "ow")


@pytest.mark.parametrize("func", STATE_FUNCS)
def test_state_falls_back_to_dot_local_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, func
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    result = func()
    assert result.is_relative_to(tmp_path / ".local" / "state" / "ow")


@pytest.mark.parametrize(
    "func,var,fallback",
    [(f, "XDG_CONFIG_HOME", ".config") for f in CONFIG_FUNCS]
    + [(f, "XDG_DATA_HOME", ".local/share") for f in DATA_FUNCS]
    + [(f, "XDG_STATE_HOME", ".local/state") for f in STATE_FUNCS],
)
def test_empty_xdg_var_counts_as_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, func, var, fallback
) -> None:
    """An XDG_* variable set to the empty string must be treated as unset.

    Otherwise Path("") would produce a relative path instead of falling back
    to the documented default — a subtle way to end up writing files into
    whatever directory the process happened to be started from.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(var, "")
    result = func()
    assert result.is_relative_to(tmp_path / fallback / "ow")


@pytest.mark.parametrize("func", ALL_FUNCS)
def test_no_function_creates_anything(xdg: Path, func) -> None:
    """Calling a path function must never create a file or directory."""
    result = func()
    assert not result.exists()
