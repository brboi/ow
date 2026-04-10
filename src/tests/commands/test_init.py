import shutil
from pathlib import Path

import pytest

from ow.commands.init import _copy_ow_services, _copy_packaged_templates, cmd_init


class TestCopyPackagedTemplates:
    def test_copies_all_template_dirs(self, tmp_path):
        dest = tmp_path / "templates"
        _copy_packaged_templates(dest)
        assert dest.exists()
        assert (dest / "common").exists()

    def test_copies_nested_files(self, tmp_path):
        dest = tmp_path / "templates"
        _copy_packaged_templates(dest)
        common = dest / "common"
        assert any(common.rglob("*"))

    def test_overwrites_existing(self, tmp_path):
        dest = tmp_path / "templates"
        dest.mkdir(parents=True)
        (dest / "common").mkdir()
        (dest / "common" / "mise.toml.j2").write_text("# dummy")
        _copy_packaged_templates(dest)
        result = (dest / "common" / "mise.toml.j2").read_text()
        assert result != "# dummy"


class TestCopyOwServices:
    def test_copies_services(self, tmp_path):
        dest = tmp_path / "services"
        _copy_ow_services(dest)
        assert dest.exists()
        assert any(dest.iterdir())

    def test_idempotent(self, tmp_path):
        dest = tmp_path / "services"
        _copy_ow_services(dest)
        _copy_ow_services(dest)
        assert dest.exists()


class TestCmdInit:
    def test_init_creates_files(self, tmp_path, capsys):
        cmd_init(tmp_path)
        captured = capsys.readouterr()
        assert (tmp_path / "ow.toml").exists()
        assert (tmp_path / "workspaces").is_dir()
        assert (tmp_path / "templates").is_dir()
        assert (tmp_path / "mise.toml").exists()
        assert (tmp_path / "services").is_dir()
        assert "Project initialized successfully" in captured.out

    def test_init_default_path_is_cwd(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        cmd_init()
        captured = capsys.readouterr()
        assert (tmp_path / "ow.toml").exists()

    def test_init_copies_packaged_templates(self, tmp_path, capsys):
        cmd_init(tmp_path)
        captured = capsys.readouterr()
        tpl = tmp_path / "templates" / "common"
        assert tpl.exists()
        assert "Copied packaged templates" in captured.out

    def test_init_copies_services(self, tmp_path, capsys):
        cmd_init(tmp_path)
        captured = capsys.readouterr()
        assert (tmp_path / "services").exists()
        assert any((tmp_path / "services").iterdir())
        assert "Copied services" in captured.out

    def test_init_creates_ow_toml_content(self, tmp_path):
        cmd_init(tmp_path)
        content = (tmp_path / "ow.toml").read_text()
        assert "[vars]" in content
        assert "[remotes.community]" in content
        assert "git@github.com:odoo/odoo.git" in content

    def test_init_creates_mise_toml_content(self, tmp_path):
        cmd_init(tmp_path)
        content = (tmp_path / "mise.toml").read_text()
        assert "[tools]" in content

    def test_init_prints_next_steps(self, tmp_path, capsys):
        cmd_init(tmp_path)
        captured = capsys.readouterr()
        assert "Edit ow.toml" in captured.out
        assert "mise install" in captured.out
        assert "ow create" in captured.out

    def test_init_refuses_existing_ow_toml(self, tmp_path, capsys):
        (tmp_path / "ow.toml").write_text("[vars]")
        with pytest.raises(SystemExit) as exc:
            cmd_init(tmp_path)
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "existing files found" in captured.err.lower()
        assert "ow.toml" in captured.err

    def test_init_refuses_existing_templates(self, tmp_path, capsys):
        tpl = tmp_path / "templates" / "common"
        tpl.mkdir(parents=True)
        (tpl / "dummy.txt").write_text("x")
        with pytest.raises(SystemExit) as exc:
            cmd_init(tmp_path)
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "templates/" in captured.err

    def test_init_empty_dir_no_refuse(self, tmp_path, capsys):
        tpl = tmp_path / "templates"
        tpl.mkdir()
        (tmp_path / "ow.toml").write_text("[vars]")
        with pytest.raises(SystemExit) as exc:
            cmd_init(tmp_path)
        captured = capsys.readouterr()
        assert "ow.toml" in captured.err
        assert "templates/" not in captured.err

    def test_init_existing_hint_about_force(self, tmp_path, capsys):
        (tmp_path / "ow.toml").write_text("[vars]")
        with pytest.raises(SystemExit) as exc:
            cmd_init(tmp_path)
        captured = capsys.readouterr()
        assert "--force" in captured.err
        assert "backup" in captured.err

    def test_init_force_overwrites(self, tmp_path, capsys):
        ow_toml = tmp_path / "ow.toml"
        ow_toml.write_text("# old")
        cmd_init(tmp_path, force=True)
        captured = capsys.readouterr()
        assert ow_toml.read_text() != "# old"
        assert "Project initialized successfully" in captured.out

    def test_init_force_with_backup(self, tmp_path, capsys):
        ow_toml = tmp_path / "ow.toml"
        ow_toml.write_text("# old ow")
        mise_toml = tmp_path / "mise.toml"
        mise_toml.write_text("# old mise")
        tpl = tmp_path / "templates" / "common"
        tpl.mkdir(parents=True)
        (tpl / "dummy.txt").write_text("dummy")
        svc = tmp_path / "services"
        svc.mkdir()
        cmd_init(tmp_path, with_backup=True)
        captured = capsys.readouterr()
        assert (tmp_path / "ow.toml.bak").exists()
        assert (tmp_path / "mise.toml.bak").exists()
        assert (tmp_path / "templates.bak").exists()
        assert (tmp_path / "services.bak").exists()
        assert "Backed up: ow.toml" in captured.out
        assert "Backed up: mise.toml" in captured.out

    def test_init_backup_removes_old_dir_backup(self, tmp_path):
        ow_toml = tmp_path / "ow.toml"
        ow_toml.write_text("# ow")
        tpl = tmp_path / "templates" / "common"
        tpl.mkdir(parents=True)
        (tpl / "dummy.txt").write_text("dummy")
        old_backup = tmp_path / "templates.bak"
        old_backup.mkdir()
        (old_backup / "old.txt").write_text("stale")
        cmd_init(tmp_path, with_backup=True)
        assert (old_backup / "common").exists()
        assert not (old_backup / "old.txt").exists()

    def test_init_backup_no_existing_files(self, tmp_path, capsys):
        cmd_init(tmp_path, with_backup=True)
        captured = capsys.readouterr()
        assert "Backed up" not in captured.out
        assert "Project initialized successfully" in captured.out
