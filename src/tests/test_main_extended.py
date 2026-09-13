import pytest
import typer

from ow.__main__ import _available_repo_aliases, _pick_workspace
from ow.utils import paths


class TestAvailableRepoAliases:

    def test_returns_aliases(self, xdg):
        paths.config_home().mkdir(parents=True, exist_ok=True)
        paths.config_file().write_text(
            '[remotes.community]\norigin.url = "git@github.com:odoo/odoo.git"\n'
        )
        aliases = _available_repo_aliases()
        assert "community" in aliases

    def test_returns_empty_list_if_config_cannot_be_loaded(self, xdg, monkeypatch):
        """Completion must never crash the shell, whatever state the config is in."""
        def _boom():
            raise OSError("boom")

        monkeypatch.setattr("ow.__main__.load_global_config", _boom)
        assert _available_repo_aliases() == []


class TestPickWorkspace:
    def test_returns_positional_when_option_absent(self):
        assert _pick_workspace("myws", None) == "myws"

    def test_returns_option_when_positional_absent(self):
        assert _pick_workspace(None, "myws") == "myws"

    def test_returns_none_when_neither_given(self):
        assert _pick_workspace(None, None) is None

    def test_accepts_identical_values(self):
        assert _pick_workspace("myws", "myws") == "myws"

    def test_rejects_disagreeing_values(self):
        with pytest.raises(typer.BadParameter):
            _pick_workspace("myws", "otherws")
