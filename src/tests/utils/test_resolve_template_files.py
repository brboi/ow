from pathlib import Path

from ow.utils import paths
from ow.utils.templates import resolve_template_files


class TestResolveTemplateFiles:
    def test_partial_local_bundle_does_not_hide_packaged_siblings(self, xdg):
        """A local bundle holding one file must not hide its packaged siblings."""
        local = paths.templates_dir() / "common"
        local.mkdir(parents=True)
        (local / "odoorc.j2").write_text("local override")

        result = resolve_template_files("common")

        # The customised file resolves to the local copy.
        assert result[Path("odoorc.j2")] == local / "odoorc.j2"
        assert result[Path("odoorc.j2")].read_text() == "local override"

        # Its siblings still resolve to the packaged versions, by path.
        packaged_dir = (
            Path(__file__).parent.parent.parent / "ow" / "_static" / "templates" / "common"
        )
        assert result[Path("pyrightconfig.json.j2")] == packaged_dir / "pyrightconfig.json.j2"
        assert result[Path("requirements-dev.txt")] == packaged_dir / "requirements-dev.txt"
        assert result[Path("mise.toml.j2")] == packaged_dir / "mise.toml.j2"
        assert result[Path("odools.toml.j2")] == packaged_dir / "odools.toml.j2"

    def test_purely_local_bundle_resolves(self, xdg):
        local = paths.templates_dir() / "my-custom"
        local.mkdir(parents=True)
        (local / "only.txt").write_text("only file")

        result = resolve_template_files("my-custom")

        assert result == {Path("only.txt"): local / "only.txt"}

    def test_purely_packaged_bundle_resolves(self, xdg):
        result = resolve_template_files("vscode")

        packaged_dir = (
            Path(__file__).parent.parent.parent / "ow" / "_static" / "templates" / "vscode"
        )
        expected_rel = Path(".vscode") / "settings.json.j2"
        assert result[expected_rel] == packaged_dir / ".vscode" / "settings.json.j2"

    def test_unknown_bundle_resolves_to_empty(self, xdg):
        assert resolve_template_files("does-not-exist") == {}
