"""`ow run` scenarios: local Odoo execution, target creation/reuse, and `--tmp` cleanup."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

from ow.core import index
from ow.core.paths import repos_dir, venvs_dir
from tests.conftest import add_repo, init_workspace


def _install_fake_venv_python(venv_dir: Path, log_path: Path) -> Path:
    py = venv_dir / "bin" / "python"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(log_path)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps({'odoo_rc': os.environ.get('ODOO_RC'), 'argv': sys.argv[1:]}) + '\\n')\n",
        encoding="utf-8",
    )
    py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return py


def _read_runs(log_path: Path) -> list[dict]:
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def test_run_inside_workspace_passes_args_and_odoorc(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = init_workspace(lab, "community", name="my-ws")
    log_path = lab.root / "runs.jsonl"
    _install_fake_venv_python(venvs_dir() / "py3.12-18.0", log_path)

    result = lab.ow("run", "--", "-i", "sale", "--stop-after-init", cwd=ws)

    assert result.exit_code == 0, result.stderr
    runs = _read_runs(log_path)
    assert len(runs) == 1
    assert runs[0]["odoo_rc"] == str(ws / "odoorc")
    assert runs[0]["argv"] == [
        str(ws / "community" / "odoo-bin"),
        "-c",
        str(ws / "odoorc"),
        "-i",
        "sale",
        "--stop-after-init",
    ]


def test_run_target_creates_and_reuses_persistent_workspace(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    remote = add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    collab = lab.collaborator("mic", of=remote)
    collab.commit({"ent/fix.py": "ok = 1\n"}, message="fix", branch="18.0-mic-fix")
    collab.push("18.0-mic-fix")
    lab.publish_pr(remote, 12345, "18.0-mic-fix")
    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 12345,
                "title": "Mic fix",
                "url": "https://github.com/odoo/enterprise/pull/12345",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-mic-fix",
                "baseRefName": "18.0",
            }
        ]
    )
    log_path = lab.root / "runs.jsonl"
    _install_fake_venv_python(venvs_dir() / "py3.12-18.0", log_path)

    parent = lab.home / "odoo"
    parent.mkdir(parents=True)

    first = lab.ow("run", "enterprise#12345", "--", "-i", "account", cwd=parent)
    assert first.exit_code == 0, first.stderr
    ws = parent / "12345-18.0-mic-fix"
    assert ws.is_dir()

    # Second run reuses the existing workspace without failing on "already exists"
    second = lab.ow("run", "enterprise#12345", "--", "--dev=all", cwd=parent)
    assert second.exit_code == 0, second.stderr

    runs = _read_runs(log_path)
    assert len(runs) == 2
    assert runs[0]["odoo_rc"] == str(ws / "odoorc")
    assert runs[0]["argv"][-2:] == ["-i", "account"]
    assert runs[1]["odoo_rc"] == str(ws / "odoorc")
    assert runs[1]["argv"][-1:] == ["--dev=all"]


def test_run_tmp_cleans_up_workspace_and_worktrees_on_exit(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    remote = add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    collab = lab.collaborator("mic", of=remote)
    collab.commit({"ent/tmp.py": "ok = 1\n"}, message="tmp", branch="18.0-tmp-pr")
    collab.push("18.0-tmp-pr")
    lab.publish_pr(remote, 888, "18.0-tmp-pr")
    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 888,
                "title": "Temp PR",
                "url": "https://github.com/odoo/enterprise/pull/888",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-tmp-pr",
                "baseRefName": "18.0",
            }
        ]
    )
    log_path = lab.root / "runs.jsonl"
    _install_fake_venv_python(venvs_dir() / "py3.12-18.0", log_path)

    parent = lab.home / "odoo"
    parent.mkdir(parents=True)

    result = lab.ow("run", "enterprise#888", "--tmp", "--", "--stop-after-init", cwd=parent)
    assert result.exit_code == 0, result.stderr
    runs = _read_runs(log_path)
    assert len(runs) == 1
    assert runs[0]["argv"][-1] == "--stop-after-init"
    # Nothing left in cwd, index, or bare worktree registrations
    assert list(parent.iterdir()) == []
    assert index.read() == ()
    wt_list = lab.git(repos_dir() / "enterprise.git", "worktree", "list", "--porcelain")
    assert "888-18.0-tmp-pr" not in wt_list
    # Ephemeral --tmp run checks out detached and leaves no local PR branch or PR ref in bare
    branches = lab.git(repos_dir() / "enterprise.git", "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    assert "18.0-tmp-pr" not in branches.splitlines()
    pr_refs = lab.git(repos_dir() / "enterprise.git", "for-each-ref", "--format=%(refname)", "refs/ow/pr/")
    assert "refs/ow/pr/888" not in pr_refs.splitlines()


def test_run_forwards_positional_subcommand_after_double_dash(lab):
    """`ow run -- shell` and `ow run -w ws -- shell` must forward `shell`, not treat it as TARGET."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = init_workspace(lab, "community", name="my-ws")
    log_path = lab.root / "runs.jsonl"
    _install_fake_venv_python(venvs_dir() / "py3.12-18.0", log_path)

    r1 = lab.ow("run", "--", "shell", "--no-http", cwd=ws)
    assert r1.exit_code == 0, r1.stderr
    assert not (ws / "shell").exists()

    r2 = lab.ow("run", "-w", "my-ws", "--", "shell", cwd=lab.home)
    assert r2.exit_code == 0, r2.stderr

    runs = _read_runs(log_path)
    assert len(runs) == 2
    assert runs[0]["argv"][-2:] == ["shell", "--no-http"]
    assert runs[1]["argv"][-1:] == ["shell"]
