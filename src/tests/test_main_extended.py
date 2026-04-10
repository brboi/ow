import sys
from unittest.mock import MagicMock, patch

import pytest

from ow.__main__ import find_root, _available_repo_aliases


class TestFindRoot:

    def test_find_root_returns_path_with_ow_toml(self, tmp_path, monkeypatch):
        (tmp_path / "ow.toml").write_text("[vars]")
        monkeypatch.chdir(tmp_path)
        result = find_root()
        assert result == tmp_path

    def test_find_root_walks_up(self, tmp_path, monkeypatch):
        (tmp_path / "ow.toml").write_text("[vars]")
        subdir = tmp_path / "deep" / "nested"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)
        result = find_root()
        assert result == tmp_path

    def test_find_root_raises_when_not_found(self, tmp_path, monkeypatch):
        subdir = tmp_path / "noconfig"
        subdir.mkdir()
        monkeypatch.chdir(subdir)
        with pytest.raises(FileNotFoundError, match="ow.toml not found"):
            find_root()


class TestAvailableRepoAliases:

    def test_returns_aliases(self, tmp_path):
        (tmp_path / "ow.toml").write_text('[remotes.community]\norigin.url = "git@github.com:odoo/odoo.git"\n')
        with patch("ow.__main__.find_root", return_value=tmp_path):
            aliases = _available_repo_aliases()
        assert "community" in aliases
