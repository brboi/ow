"""Lab fixture for creating test repositories and workspaces."""
from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from pytest import MonkeyPatch
from typer.testing import CliRunner

from ow.cli import build_app
from ow.core.plugins import load_plugins


def git(path: Path, *args: str) -> str:
    """Run `git -C <path> <args>`, failing loudly, and return its stripped stdout."""
    result = subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def git_ok(path: Path, *args: str) -> bool:
    """Whether `git -C <path> <args>` succeeds; a failure is a state under test."""
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True).returncode == 0


def file_url(path: Path) -> str:
    """The `file://` URL of a local repo, as `ow repo add` takes it."""
    return f"file://{path}"


def _write_files(root: Path, files: dict[str, str | bytes]) -> None:
    """Materialise `files` (relative path -> content) under `root`."""
    for file_path, content in files.items():
        full_path = root / file_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            full_path.write_bytes(content)
        else:
            full_path.write_text(content)


class Collaborator:
    """A working clone of a remote repository."""

    def __init__(self, path: Path):
        """Initialize collaborator with a clone path."""
        self.path = path

    def commit(self, files: dict[str, str | bytes], message: str = "Test commit", branch: str | None = None) -> str:
        """Commit files to the repository; returns the new HEAD's sha."""
        if branch:
            git(self.path, "checkout", "-b", branch)
        _write_files(self.path, files)
        git(self.path, "add", *files)
        git(self.path, "commit", "-m", message)
        return git(self.path, "rev-parse", "HEAD")

    def push(self, branch: str) -> None:
        """Push a branch to the remote."""
        git(self.path, "push", "origin", branch)

    def force_push(self, branch: str, onto: str) -> None:
        """Force push a branch after resetting it."""
        git(self.path, "reset", "--hard", onto)
        git(self.path, "push", "--force", "origin", branch)

    def git(self, *args: str) -> str:
        """Run git command in the clone and return stdout."""
        return git(self.path, *args)


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
        ws(name): Get workspace path (~/ws/<name>).
    """

    def __init__(self, root: Path, monkeypatch: MonkeyPatch):
        """Initialize lab with root path and monkeypatch."""
        self.root = root / "lab"
        self.root.mkdir(parents=True, exist_ok=True)
        self.home = root / "home"
        self._tmp_root = root  # Store tmp_path root for remotes
        self._monkeypatch = monkeypatch

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

        git(remotes_dir, "init", "--bare", str(bare_path))
        git(bare_path, "config", "uploadpack.allowFilter", "true")
        git(bare_path, "config", "uploadpack.allowAnySHA1InWant", "true")

        if not branches:
            return bare_path

        # A temporary clone adds the commits the bare is fed with.
        temp_path = self.root / f"temp_{name}"
        git(remotes_dir, "clone", str(bare_path), str(temp_path))

        (temp_path / ".root").write_text("root")
        git(temp_path, "add", ".root")
        git(temp_path, "commit", "-m", "Initial commit")
        git(temp_path, "push", "-u", "origin", "master")

        for branch_name, files in branches.items():
            if branch_name == "master":
                git(temp_path, "checkout", branch_name)
            else:
                git(temp_path, "checkout", "-b", branch_name)
            _write_files(temp_path, files)
            git(temp_path, "add", *files)
            git(temp_path, "commit", "-m", f"Add files for {branch_name}")
            git(temp_path, "push", "-u", "origin", branch_name)

        git(bare_path, "symbolic-ref", "HEAD", f"refs/heads/{head}")

        shutil.rmtree(temp_path)
        return bare_path

    def odoo_files(
        self,
        python: tuple[tuple[int, int], ...] = ((3, 10), (3, 12)),
        with_demo: bool = True,
        series: str | None = None,
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

        release_py = f"MIN_PY_VERSION = {python[0]}\nMAX_PY_VERSION = {python[-1]}\n"
        if series is not None:
            release_py += f"serie = {series!r}\n"

        files = {
            "odoo-bin": "#!/usr/bin/env python\n# odoo binary\n",
            "addons/web/__manifest__.py": "{'name': 'Web', 'version': '1.0'}",
            "odoo/addons/base/__manifest__.py": "{'name': 'Base', 'version': '1.0'}",
            "odoo/__init__.py": "# Odoo package\n",
            "odoo/release.py": release_py,
            "odoo/tools/config.py": config_py,
            "requirements.txt": "# requirements\n",
        }
        return files

    def publish_pr(self, remote: Path, number: int, branch: str) -> str:
        """Expose `refs/heads/<branch>` on `remote` as `refs/pull/<number>/head` (like GitHub)."""
        sha = git(remote, "rev-parse", f"refs/heads/{branch}")
        git(remote, "update-ref", f"refs/pull/{number}/head", sha)
        return sha

    def fake_gh(self, prs: list[dict[str, Any]]) -> Path:
        """Install a hermetic `gh` shim on PATH backed by `prs` (list of dicts with `repo` slug)."""
        db_path = self._tmp_root / "gh_prs.json"
        db_path.write_text(json.dumps(prs), encoding="utf-8")
        log_path = self._tmp_root / "gh_calls.log"

        shim = self._tmp_root / "bin" / "gh"
        shim.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            f"DB = {str(db_path)!r}\n"
            f"LOG = {str(log_path)!r}\n"
            "args = sys.argv[1:]\n"
            "with open(LOG, 'a', encoding='utf-8') as lf:\n"
            "    lf.write(json.dumps(args) + '\\n')\n"
            "with open(DB, encoding='utf-8') as f:\n"
            "    prs = json.load(f)\n"
            "def opt(flag):\n"
            "    if flag in args:\n"
            "        i = args.index(flag)\n"
            "        return args[i + 1] if i + 1 < len(args) else None\n"
            "    return None\n"
            "if len(args) >= 2 and args[0] == 'pr' and args[1] == 'view':\n"
            "    num = int(args[2])\n"
            "    repo = opt('--repo')\n"
            "    for pr in prs:\n"
            "        if pr.get('number') == num and (repo is None or pr.get('repo') == repo):\n"
            "            print(json.dumps(pr))\n"
            "            sys.exit(0)\n"
            "    sys.stderr.write(f'no pull request #{num} in {repo}\\n')\n"
            "    sys.exit(1)\n"
            "if len(args) >= 2 and args[0] == 'pr' and args[1] == 'list':\n"
            "    repo = opt('--repo')\n"
            "    head = opt('--head')\n"
            "    state = (opt('--state') or 'open').upper()\n"
            "    out = []\n"
            "    for pr in prs:\n"
            "        if repo and pr.get('repo') != repo:\n"
            "            continue\n"
            "        if head and pr.get('headRefName') != head:\n"
            "            continue\n"
            "        if state != 'ALL' and str(pr.get('state', 'OPEN')).upper() != state:\n"
            "            continue\n"
            "        out.append(pr)\n"
            "    print(json.dumps(out))\n"
            "    sys.exit(0)\n"
            "sys.stderr.write('unsupported gh command\\n')\n"
            "sys.exit(2)\n",
            encoding="utf-8",
        )
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return log_path

    def collaborator(self, name: str, of: Path) -> Collaborator:
        """Create a working clone of a remote repository."""
        collab_dir = self.root / "collab" / name
        git(self.root, "clone", str(of), str(collab_dir))
        return Collaborator(collab_dir)

    def ow(self, *args: str, input: str | None = None, cwd: Path | None = None) -> Any:
        """Run ow CLI command; `cwd` is the directory the command sees."""
        runner = CliRunner()
        app = build_app(load_plugins())
        if cwd is None:
            return runner.invoke(app, list(args), input=input, catch_exceptions=False)
        with self._monkeypatch.context() as patch:
            patch.chdir(cwd)
            return runner.invoke(app, list(args), input=input, catch_exceptions=False)

    def git(self, path: Path, *args: str) -> str:
        """Run git command at given path."""
        return git(path, *args)

    def sha(self, path: Path, ref: str) -> str:
        """Get SHA of a git ref."""
        return git(path, "rev-parse", ref)

    def ws(self, name: str) -> Path:
        """Get workspace path (~/ws/<name>)."""
        return self.home / "ws" / name
