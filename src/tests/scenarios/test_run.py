"""`ow run` scenarios: single TTY execution, multi-process runner, SPEC:"ARGS", and `--tmp`."""

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
        "print('hello from fake odoo', os.environ.get('ODOO_RC'), flush=True)\n"
        f"with open({str(log_path)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps({'odoo_rc': os.environ.get('ODOO_RC'), 'argv': sys.argv[1:]}) + '\\n')\n",
        encoding="utf-8",
    )
    py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return py


def _read_runs(log_path: Path) -> list[dict]:
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def test_run_installs_requirements_once_for_real_venv_with_pyvenv_cfg(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = init_workspace(lab, "community", name="req-ws")
    venv_dir = venvs_dir() / "py3.12-18.0"
    (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
    (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")

    pip_log = lab.root / "pip.jsonl"
    run_log = lab.root / "runs.jsonl"
    py = venv_dir / "bin" / "python"
    py.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['-m', 'pip']:\n"
        f"    with open({str(pip_log)!r}, 'a', encoding='utf-8') as f:\n"
        "        f.write(json.dumps(args) + '\\n')\n"
        "    sys.exit(0)\n"
        f"with open({str(run_log)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(args) + '\\n')\n",
        encoding="utf-8",
    )
    py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    r1 = lab.ow("run", cwd=ws)
    assert r1.exit_code == 0, r1.stderr + r1.stdout
    assert (venv_dir / ".ow-requirements.sha256").is_file()
    pip_calls_1 = [json.loads(line) for line in pip_log.read_text().splitlines()]
    assert any(call[:3] == ["-m", "pip", "install"] for call in pip_calls_1)

    # Second run skips pip install because the requirements hash is stamped.
    r2 = lab.ow("run", cwd=ws)
    assert r2.exit_code == 0, r2.stderr + r2.stdout
    pip_calls_2 = [json.loads(line) for line in pip_log.read_text().splitlines()]
    assert len(pip_calls_2) == len(pip_calls_1)


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


def test_run_external_pr_without_tmp_refuses_and_guides_user(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    res = lab.ow("run", "enterprise#12345", cwd=lab.home)
    assert res.exit_code == 2
    assert "--tmp" in res.stderr
    assert "ow init" in res.stderr


def test_run_multi_stack_root_launches_all_runnable_stacks_and_skips_voip(lab):
    """`ow run` at a multi-stack workspace root runs all Odoo stacks in parallel and skips `voip`."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "voip", {"master": {"README.md": "voip\n"}}, base="master")

    ws = lab.home / "review-stack"
    ws.mkdir(parents=True)
    assert lab.ow("init", "odoo=community@18.0", "iap=community@18.0", "voip=voip", cwd=ws).exit_code == 0

    log_path = lab.root / "runs.jsonl"
    _install_fake_venv_python(venvs_dir() / "py3.12-18.0", log_path)

    res = lab.ow("run", cwd=ws)
    assert res.exit_code == 0, res.stderr + res.stdout
    assert "voip: no runner available, skipping" in res.stdout
    assert "[odoo] hello from fake odoo" in res.stdout
    assert "[iap] hello from fake odoo" in res.stdout

    runs = _read_runs(log_path)
    assert len(runs) == 2
    rcs = {r["odoo_rc"] for r in runs}
    assert rcs == {str(ws / "odoo" / "odoorc"), str(ws / "iap" / "odoorc")}


def test_run_multi_stack_per_stack_colon_args(lab):
    """`ow run odoo:\"-d plop -i voip_hr\" iap:\"-i phone_service\"` forwards distinct args to each stack."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = lab.home / "per-args"
    ws.mkdir(parents=True)
    assert lab.ow("init", "odoo=community@18.0", "iap=community@18.0", cwd=ws).exit_code == 0

    log_path = lab.root / "runs.jsonl"
    _install_fake_venv_python(venvs_dir() / "py3.12-18.0", log_path)

    res = lab.ow("run", 'odoo:-d plop -i voip_hr', 'iap:-i phone_service', cwd=ws)
    assert res.exit_code == 0, res.stderr + res.stdout

    runs = _read_runs(log_path)
    by_rc = {r["odoo_rc"]: r["argv"] for r in runs}
    assert by_rc[str(ws / "odoo" / "odoorc")][-4:] == ["-d", "plop", "-i", "voip_hr"]
    assert by_rc[str(ws / "iap" / "odoorc")][-2:] == ["-i", "phone_service"]


def test_run_tmp_multi_pr_with_optional_names_colon_args_distinct_ports_and_cleanup(lab):
    """`ow run --tmp` runs multiple PR specs with per-target args, auto names, distinct ports, and cleans up."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ent_remote = add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    ent_collab = lab.collaborator("mic", of=ent_remote)
    ent_collab.commit({"ent/tmp.py": "ok = 1\n"}, message="tmp", branch="18.0-voip-tmp")
    ent_collab.push("18.0-voip-tmp")
    lab.publish_pr(ent_remote, 888, "18.0-voip-tmp")

    iap_remote = add_repo(lab, "iap", {"18.0": {"iap_mod/__manifest__.py": "{}"}})
    iap_collab = lab.collaborator("bob", of=iap_remote)
    iap_collab.commit({"iap_mod/tmp.py": "ok = 1\n"}, message="iap tmp", branch="18.0-iap-tmp")
    iap_collab.push("18.0-iap-tmp")
    lab.publish_pr(iap_remote, 333, "18.0-iap-tmp")
    lab.git(repos_dir() / "iap.git", "config", "ow.github-repo", "odoo/iap-apps")

    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 888,
                "title": "Temp Ent PR",
                "url": "https://github.com/odoo/enterprise/pull/888",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-voip-tmp",
                "baseRefName": "18.0",
            },
            {
                "repo": "odoo/iap-apps",
                "number": 333,
                "title": "Temp IAP PR",
                "url": "https://github.com/odoo/iap-apps/pull/333",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-iap-tmp",
                "baseRefName": "18.0",
            },
        ]
    )
    log_path = lab.root / "runs.jsonl"
    _install_fake_venv_python(venvs_dir() / "py3.12-18.0", log_path)

    parent = lab.home / "scratch"
    parent.mkdir(parents=True)

    result = lab.ow(
        "run",
        "--tmp",
        'https://github.com/odoo/enterprise/pull/888:-d plop -i voip_hr',
        'iap#333:-i phone_service',
        cwd=parent,
    )
    assert result.exit_code == 0, result.stderr + result.stdout
    assert "[18.0-voip-tmp]" in result.stdout
    assert "[18.0-iap-tmp]" in result.stdout

    runs = _read_runs(log_path)
    assert len(runs) == 2
    argvs = [r["argv"] for r in runs]
    assert any(a[-4:] == ["-d", "plop", "-i", "voip_hr"] for a in argvs)
    assert any(a[-2:] == ["-i", "phone_service"] for a in argvs)

    # Nothing left in cwd, index, bare worktrees, local PR branches, or ephemeral PR refs
    assert list(parent.iterdir()) == []
    assert index.read() == ()
    wt_list = lab.git(repos_dir() / "enterprise.git", "worktree", "list", "--porcelain")
    assert "18.0-voip-tmp" not in wt_list
    branches = lab.git(repos_dir() / "enterprise.git", "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    assert "18.0-voip-tmp" not in branches.splitlines()
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

    r2 = lab.ow("run", "-w", "my-ws", "--", "shell", "--no-http", cwd=lab.home)
    assert r2.exit_code == 0, r2.stderr

    runs = _read_runs(log_path)
    assert len(runs) == 2
    assert runs[0]["argv"][-2:] == ["shell", "--no-http"]
    assert runs[1]["argv"][-2:] == ["shell", "--no-http"]


def test_split_run_args_handles_host_port_urls():
    from ow.commands._targets import split_run_args

    assert split_run_args("https://my.runbot:8069/runbot/bundle/1") == (
        "https://my.runbot:8069/runbot/bundle/1",
        (),
    )
    assert split_run_args("my.runbot:8069/runbot/bundle/1") == (
        "my.runbot:8069/runbot/bundle/1",
        (),
    )
    assert split_run_args("rb=my.runbot:8069/runbot/bundle/1:-i sale") == (
        "rb=my.runbot:8069/runbot/bundle/1",
        ("-i", "sale"),
    )
    assert split_run_args("https://my.runbot:8069/runbot/bundle/1:-d test") == (
        "https://my.runbot:8069/runbot/bundle/1",
        ("-d", "test"),
    )


def test_run_multi_fail_fast_terminates_siblings_and_prints_summary(lab):
    import time

    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = lab.home / "ff-ws"
    ws.mkdir(parents=True)
    assert lab.ow("init", "fast=community@18.0", "slow=community@18.0", cwd=ws).exit_code == 0

    venv_py = venvs_dir() / "py3.12-18.0" / "bin" / "python"
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "if '--fail' in sys.argv:\n"
        "    print('failing fast', flush=True)\n"
        "    sys.exit(1)\n"
        "print('sleeping', flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    venv_py.chmod(venv_py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    t0 = time.monotonic()
    res = lab.ow("run", "--fail-fast", "fast:--fail", "slow:--sleep", cwd=ws)
    elapsed = time.monotonic() - t0

    assert res.exit_code == 1
    assert elapsed < 10.0
    assert "# summary:" in res.stdout
    assert "fast: exit 1" in res.stdout


def test_run_ephemeral_pr_ref_cleanup_matches_full_slug(lab):
    from ow.commands.run import _branch_uses_pr_ref

    add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    bare = repos_dir() / "enterprise.git"
    lab.git(bare, "branch", "feat", "origin/18.0")
    lab.git(bare, "config", "branch.feat.ow-pr", "other/enterprise#888")

    assert _branch_uses_pr_ref(bare, "refs/ow/pr/888", pr="odoo/enterprise#888") is False
    assert _branch_uses_pr_ref(bare, "refs/ow/pr/888", pr="other/enterprise#888") is True


def test_run_named_spec_without_tmp_suggests_tmp_command(lab):
    res = lab.ow("run", "iap=iap#1", cwd=lab.home)
    assert res.exit_code == 2
    assert "ow run --tmp iap=iap#1" in res.stderr


def test_run_reset_drops_database_and_filestore_via_postgres_container(lab, monkeypatch):
    from ow.plugins import _containers as containers
    from tests.plugins.test_services import _make_fake_engine

    calls, _running = _make_fake_engine(monkeypatch, running={"ow-postgres": {5432: 5432}})
    monkeypatch.setattr(containers, "is_port_free", lambda port, host="127.0.0.1": True)

    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = init_workspace(lab, "community", name="reset-ws")
    filestore = ws / ".odoo" / "filestore" / "reset-ws"
    filestore.mkdir(parents=True)
    (filestore / "stale.bin").write_bytes(b"old")

    log_path = lab.root / "runs.jsonl"
    _install_fake_venv_python(venvs_dir() / "py3.12-18.0", log_path)

    res = lab.ow("run", "--reset", cwd=ws)
    assert res.exit_code == 0, res.stderr + res.stdout
    assert not filestore.exists()
    assert "# reset-ws: dropped database 'reset-ws' and filestore" in res.stdout
    assert any(
        c[:3] == ["docker", "exec", "ow-postgres"]
        and 'DROP DATABASE IF EXISTS "reset-ws" WITH (FORCE);' in c[-1]
        for c in calls
    )
    assert len(_read_runs(log_path)) == 1


def test_run_reset_falls_back_to_host_psql_and_refuses_system_db(lab, monkeypatch):
    import os
    from ow.plugins import _containers as containers

    monkeypatch.setattr(containers, "detect_engine", lambda: None)

    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = init_workspace(lab, "community", name="host-reset")
    log_path = lab.root / "runs.jsonl"
    psql_log = lab.root / "psql.json"
    _install_fake_venv_python(venvs_dir() / "py3.12-18.0", log_path)

    bin_dir = Path(os.environ["PATH"].split(os.pathsep)[0])
    fake_psql = bin_dir / "psql"
    fake_psql.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(psql_log)!r}, 'w', encoding='utf-8') as f:\n"
        "    json.dump({'argv': sys.argv[1:], 'pgpassword': os.environ.get('PGPASSWORD')}, f)\n",
        encoding="utf-8",
    )
    fake_psql.chmod(fake_psql.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    res = lab.ow("run", "--reset", cwd=ws)
    assert res.exit_code == 0, res.stderr + res.stdout
    assert "# host-reset: dropped database 'host-reset'" in res.stdout
    psql_call = json.loads(psql_log.read_text(encoding="utf-8"))
    assert 'DROP DATABASE IF EXISTS "host-reset" WITH (FORCE);' in psql_call["argv"]
    assert psql_call["pgpassword"] == "odoo"

    # Safety guard: refusing to drop system DB `postgres`
    assert lab.ow("config", "set", "odoo.db_name", "postgres", cwd=ws).exit_code == 0
    refused = lab.ow("run", "--reset", cwd=ws)
    assert refused.exit_code == 2
    assert "refusing to drop database 'postgres'" in refused.stderr


def test_run_reset_multi_stack_only_resets_targeted_stack(lab, monkeypatch):
    from ow.plugins import _containers as containers
    from tests.plugins.test_services import _make_fake_engine

    calls, _running = _make_fake_engine(monkeypatch, running={"ow-postgres": {5432: 5432}})
    monkeypatch.setattr(containers, "is_port_free", lambda port, host="127.0.0.1": True)

    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    multi = lab.home / "multi-reset"
    multi.mkdir(parents=True)
    assert lab.ow("init", "odoo=community@18.0", "iap=community@18.0", cwd=multi).exit_code == 0

    for stack_name in ("odoo", "iap"):
        fs = multi / stack_name / ".odoo" / "filestore" / stack_name
        fs.mkdir(parents=True)
        (fs / "a.bin").write_bytes(b"1")

    log_path = lab.root / "runs.jsonl"
    _install_fake_venv_python(venvs_dir() / "py3.12-18.0", log_path)

    res = lab.ow("run", "--reset", "odoo", cwd=multi)
    assert res.exit_code == 0, res.stderr + res.stdout
    assert not (multi / "odoo" / ".odoo" / "filestore" / "odoo").exists()
    assert (multi / "iap" / ".odoo" / "filestore" / "iap").exists()
    drop_sqls = [c[-1] for c in calls if c[:3] == ["docker", "exec", "ow-postgres"] and "DROP DATABASE" in c[-1]]
    assert any('"odoo"' in s for s in drop_sqls)
    assert not any('"iap"' in s for s in drop_sqls)


def test_run_refreshes_stale_generated_files_when_postgres_port_changes(lab, monkeypatch):
    """When ow-postgres moves from 5432 to 5433 after init, `ow run` refreshes odoorc before launching."""
    from ow.plugins import _containers as containers
    from tests.plugins.test_services import _make_fake_engine

    _calls, running = _make_fake_engine(monkeypatch, running={"ow-postgres": {5432: 5432}})
    monkeypatch.setattr(containers, "is_port_free", lambda port, host="127.0.0.1": True)

    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = init_workspace(lab, "community", name="port-shift")
    assert "db_port = 5432" in (ws / "odoorc").read_text()

    # Later, ow-postgres is running on fallback port 5433 instead of 5432.
    running["ow-postgres"] = {5432: 5433}
    monkeypatch.setattr(containers, "is_port_free", lambda port, host="127.0.0.1": port != 5432)

    log_path = lab.root / "runs.jsonl"
    _install_fake_venv_python(venvs_dir() / "py3.12-18.0", log_path)

    res = lab.ow("run", cwd=ws)
    assert res.exit_code == 0, res.stderr + res.stdout
    assert "# refreshed odoorc" in res.stdout
    assert "db_port = 5433" in (ws / "odoorc").read_text()
    assert len(_read_runs(log_path)) == 1

    # A second `ow run` with the same port does not rewrite or print `# refreshed`.
    res2 = lab.ow("run", cwd=ws)
    assert res2.exit_code == 0, res2.stderr + res2.stdout
    assert "# refreshed" not in res2.stdout
