from ow.__main__ import _available_repo_aliases
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
