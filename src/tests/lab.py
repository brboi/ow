"""Lab fixture for creating test repositories and workspaces."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from ow.cli import build_app
from ow.core.plugins import load_plugins


class Collaborator:
    """A working clone of a remote repository."""

    def __init__(self, path: Path):
        """Initialize collaborator with a clone path."""
        self.path = path

    def commit(self, files: dict[str, str | bytes], message: str = "Test commit", branch: str | None = None) -> str:
        """Commit files to the repository."""
        if branch:
            subprocess.run(["git", "-C", str(self.path), "checkout", "-b", branch], check=True)
        
        for file_path, content in files.items():
            full_path = self.path / file_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                full_path.write_bytes(content)
            else:
                full_path.write_text(content)
            subprocess.run(["git", "-C", str(self.path), "add", file_path], check=True)
        
        subprocess.run(
            ["git", "-C", str(self.path), "commit", "-m", message],
            check=True,
            capture_output=True,
        )
        
        result = subprocess.run(
            ["git", "-C", str(self.path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def push(self, branch: str) -> None:
        """Push a branch to the remote."""
        subprocess.run(
            ["git", "-C", str(self.path), "push", "origin", branch],
            check=True,
            capture_output=True,
        )

    def force_push(self, branch: str, onto: str) -> None:
        """Force push a branch after resetting it."""
        subprocess.run(
            ["git", "-C", str(self.path), "reset", "--hard", onto],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.path), "push", "--force", "origin", branch],
            check=True,
            capture_output=True,
        )

    def git(self, *args: str) -> str:
        """Run git command and return stdout."""
        result = subprocess.run(
            ["git", "-C", str(self.path), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()


class Lab:
    """Lab fixture for creating test repositories and workspaces.

    Attributes:
        root: Lab directory (tmp_path / "lab").
        home: Home directory (tmp_path / "home").
        remote(name, ...): Create a bare repo at tmp_path / "remotes" / "<name>.git".
        odoo_files(...): Generate Odoo project file structure dict.
        collaborator(name, of): Clone a remote and return a Collaborator.
        ow(...): Run ow CLI via CliRunner.
        git(path, ...): Run git command at path.
        sha(path, ref): Get SHA of a git ref.
        ws(name): Get workspace path (~/<name>).
    """

    def __init__(self, root: Path, monkeypatch: Any):
        """Initialize lab with root path and monkeypatch."""
        self.root = root / "lab"
        self.root.mkdir(parents=True, exist_ok=True)
        self.home = root / "home"
        self._tmp_root = root  # Store tmp_path root for remotes
        self.monkeypatch = monkeypatch
    def remote(
        self,
        name: str,
        branches: dict[str, dict[str, str | bytes]] | None = None,
        head: str = "18.0",
    ) -> Path:
        """Create a bare remote repository with branches."""
        remotes_dir = self._tmp_root / "remotes"
        remotes_dir.mkdir(parents=True, exist_ok=True)
        bare_path = remotes_dir / f"{name}.git"
        
        # Initialize bare repository
        subprocess.run(["git", "init", "--bare", str(bare_path)], check=True, capture_output=True)
        
        # Configure uploadpack options
        subprocess.run(
            ["git", "-C", str(bare_path), "config", "uploadpack.allowFilter", "true"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(bare_path), "config", "uploadpack.allowAnySHA1InWant", "true"],
            check=True,
            capture_output=True,
        )
        
        # Create branches with commits if provided
        if branches:
            # Create a temporary clone to add commits
            temp_path = self.root / f"temp_{name}"
            subprocess.run(["git", "clone", str(bare_path), str(temp_path)], check=True, capture_output=True)
            
            # Create root commit
            root_commit_file = temp_path / ".root"
            root_commit_file.write_text("root")
            subprocess.run(["git", "-C", str(temp_path), "add", ".root"], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(temp_path), "commit", "-m", "Initial commit"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(temp_path), "push", "-u", "origin", "master"],
                check=True,
                capture_output=True,
            )
            
            # Create branches
            for branch_name, files in branches.items():
                if branch_name != "master":
                    subprocess.run(
                        ["git", "-C", str(temp_path), "checkout", "-b", branch_name],
                        check=True,
                        capture_output=True,
                    )
                else:
                    subprocess.run(
                        ["git", "-C", str(temp_path), "checkout", branch_name],
                        check=True,
                        capture_output=True,
                    )
                
                # Add files
                for file_path, content in files.items():
                    full_path = temp_path / file_path
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    if isinstance(content, bytes):
                        full_path.write_bytes(content)
                    else:
                        full_path.write_text(content)
                    subprocess.run(["git", "-C", str(temp_path), "add", file_path], check=True, capture_output=True)
                
                subprocess.run(
                    ["git", "-C", str(temp_path), "commit", "-m", f"Add files for {branch_name}"],
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "-C", str(temp_path), "push", "-u", "origin", branch_name],
                    check=True,
                    capture_output=True,
                )
            
            # Set HEAD to specified branch
            subprocess.run(
                ["git", "-C", str(bare_path), "symbolic-ref", "HEAD", f"refs/heads/{head}"],
                check=True,
                capture_output=True,
            )
            
            # Clean up temp clone
            import shutil
            shutil.rmtree(temp_path)
        
        return bare_path

    def odoo_files(
        self,
        python: tuple[tuple[int, int], ...] = ((3, 10), (3, 12)),
        with_demo: bool = True,
    ) -> dict[str, str]:
        """Generate Odoo project files structure."""
        # `odoo/tools/config.py` as real Odoo writes it: the option group's
        # `add_option("--with-demo"/"--without-demo", ...)` lives in a method
        # body, never as a bare module-level call.
        config_py = (
            '"""Reduced copy of odoo/tools/config.py."""\n'
            "import optparse\n"
            "\n"
            "\n"
            "class OdooConfig:\n"
            "    def __init__(self, args=None):\n"
            "        parser = optparse.OptionParser()\n"
            '        group = optparse.OptionGroup(parser, "Common options")\n'
        )
        if with_demo:
            config_py += (
                '        group.add_option("--with-demo", dest="with_demo",'
                ' action="store_true", my_default=False)\n'
            )
        else:
            config_py += (
                '        group.add_option("--without-demo", dest="without_demo",'
                " my_default=False)\n"
            )
        config_py += "        parser.add_option_group(group)\n"

        files = {
            "odoo-bin": "#!/usr/bin/env python\n# odoo binary\n",
            "addons/web/__manifest__.py": "{'name': 'Web', 'version': '1.0'}",
            "odoo/addons/base/__manifest__.py": "{'name': 'Base', 'version': '1.0'}",
            "odoo/__init__.py": "# Odoo package\n",
            "odoo/release.py": f"MIN_PY_VERSION = {python[0]}\nMAX_PY_VERSION = {python[-1]}\n",
            "odoo/tools/config.py": config_py,
            "requirements.txt": "# requirements\n",
        }
        return files

    def collaborator(self, name: str, of: Path) -> Collaborator:
        """Create a working clone of a remote repository."""
        collab_dir = self.root / "collab" / name
        subprocess.run(["git", "clone", str(of), str(collab_dir)], check=True, capture_output=True)
        return Collaborator(collab_dir)

    def ow(self, *args: str, input: str | None = None, cwd: Path | None = None) -> Any:
        """Run ow CLI command."""
        runner = CliRunner()
        app = build_app(load_plugins())
        
        original_cwd = None
        if cwd:
            original_cwd = Path.cwd()
            import os
            os.chdir(cwd)
        
        try:
            return runner.invoke(app, list(args), input=input, catch_exceptions=False)
        finally:
            if original_cwd:
                import os
                os.chdir(original_cwd)

    def git(self, path: Path, *args: str) -> str:
        """Run git command at given path."""
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def sha(self, path: Path, ref: str) -> str:
        """Get SHA of a git ref."""
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", ref],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def ws(self, name: str) -> Path:
        """Get workspace path."""
        return self.home / "ws" / name
