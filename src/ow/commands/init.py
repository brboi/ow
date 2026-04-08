import shutil
import sys
from pathlib import Path


def _copy_packaged_templates(dest: Path) -> None:
    """Copy all packaged templates to destination directory."""
    from importlib.resources import files

    pkg_templates = files("ow") / "_static" / "templates"
    dest.mkdir(parents=True, exist_ok=True)

    for template_dir in pkg_templates.iterdir():
        if not template_dir.is_dir():
            continue

        src_dir = pkg_templates / template_dir.name
        dst_dir = dest / template_dir.name
        dst_dir.mkdir(exist_ok=True)

        for src_file in src_dir.rglob("*"):
            if src_file.is_file():
                rel = src_file.relative_to(src_dir)
                dst_file = dst_dir / rel
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst_file)


def _copy_ow_services(dest: Path) -> None:
    """Copy ow-scoped services to destination directory."""
    from importlib.resources import files

    pkg_services = files("ow") / "_static" / "services"
    dest.mkdir(parents=True, exist_ok=True)

    for src_file in pkg_services.iterdir():
        if src_file.is_file():
            shutil.copy2(src_file, dest / src_file.name)


def cmd_init(path: Path | None = None, *, force: bool = False, with_backup: bool = False) -> None:
    """Initialize a new ow project in the current directory.

    Creates:
    - ow.toml (minimal config with odoo/odoo repo)
    - workspaces/ (empty directory)
    - templates/ (copy of packaged templates)
    - mise.toml (ow-scoped tools: Python, Node, rtlcss)
    - services/compose.yml (Docker Compose example)

    Args:
        path: Target directory (default: current directory)
        force: Overwrite existing files without backup
        with_backup: Backup existing files before overwrite
    """
    target = path or Path.cwd()

    ow_toml = target / "ow.toml"
    templates_dir = target / "templates"
    workspaces_dir = target / "workspaces"
    mise_toml = target / "mise.toml"
    services_dir = target / "services"

    # Check if files already exist
    exists = []
    if ow_toml.exists():
        exists.append("ow.toml")
    if templates_dir.exists() and any(templates_dir.iterdir()):
        exists.append("templates/")
    if mise_toml.exists():
        exists.append("mise.toml")
    if services_dir.exists():
        exists.append("services/")

    if exists and not force and not with_backup:
        print(f"Error: existing files found: {', '.join(exists)}", file=sys.stderr)
        print("Use --force to overwrite without backup, or --force-with-backup to backup first.", file=sys.stderr)
        sys.exit(1)

    # Backup if requested
    if with_backup and exists:
        if ow_toml.exists():
            backup_path = target / "ow.toml.bak"
            shutil.copy2(ow_toml, backup_path)
            print(f"Backed up: ow.toml → ow.toml.bak")

        if templates_dir.exists() and any(templates_dir.iterdir()):
            backup_path = target / "templates.bak"
            if backup_path.exists():
                shutil.rmtree(backup_path)
            shutil.copytree(templates_dir, backup_path)
            print(f"Backed up: templates/ → templates.bak/")

        if mise_toml.exists():
            backup_path = target / "mise.toml.bak"
            shutil.copy2(mise_toml, backup_path)
            print(f"Backed up: mise.toml → mise.toml.bak")

        if services_dir.exists():
            backup_path = target / "services.bak"
            if backup_path.exists():
                shutil.rmtree(backup_path)
            shutil.copytree(services_dir, backup_path)
            print(f"Backed up: services/ → services.bak/")

    # Create directories
    workspaces_dir.mkdir(parents=True, exist_ok=True)
    templates_dir.mkdir(parents=True, exist_ok=True)
    services_dir.mkdir(parents=True, exist_ok=True)

    # Copy packaged templates (overwrite if exists)
    _copy_packaged_templates(templates_dir)
    print(f"Copied packaged templates to templates/")

    # Copy ow-scoped services
    _copy_ow_services(services_dir)
    print(f"Copied services to services/")

    # Create ow.toml (minimal config)
    ow_toml_content = '''[vars]
http_port = 8069
db_host = "localhost"
db_port = 5432
db_user = "odoo"
db_password = "odoo"
admin_passwd = "Password"
# smtp_server = "mailpit"
# smtp_port = 1025

[remotes.community]
origin.url = "git@github.com:odoo/odoo.git"
# dev.url = "git@github.com:odoo-dev/odoo.git"
# dev.pushurl = "git@github.com:odoo-dev/odoo.git"
# dev.fetch = "+refs/heads/*:refs/remotes/dev/*"

# [remotes.enterprise]
# origin.url = "git@github.com:odoo/enterprise.git"
# dev.url = "git@github.com:odoo-dev/enterprise.git"
# dev.pushurl = "git@github.com:odoo-dev/enterprise.git"
# dev.fetch = "+refs/heads/*:refs/remotes/dev/*"
'''
    ow_toml.write_text(ow_toml_content)
    print(f"Created: ow.toml")

    # Create mise.toml (ow-scoped)
    mise_toml_content = '''[tools]
python = "3.12"
node = { version = "latest", postinstall = "npm install -g rtlcss" }

[settings]
experimental = true

[env]
COMPOSE_FILE = "{{config_root}}/services/compose.yml"
'''
    mise_toml.write_text(mise_toml_content)
    print(f"Created: mise.toml")

    print("\nProject initialized successfully!")
    print("\nNext steps:")
    print("  1. Edit ow.toml to add more remotes if needed")
    print("  2. Run: mise install")
    print("  3. Run: ow create  (to create your first workspace)")
