"""`ow sandbox` tests: bwrap plan assembly, isolation guarantees, CLI scenarios, and real bwrap."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ow.commands import sandbox as sandbox_cmd
from ow.core.paths import global_config, repos_dir, venvs_dir
from ow.core.plugins import load_plugins
from ow.core.refresh import read_lock
from ow.core.session import open_session
from ow.errors import UsageError
from tests.conftest import add_repo, init_workspace, workspace
from tests.lab import make_executable


def _mount_pairs(argv: tuple[str, ...]) -> list[tuple[str, str, str]]:
    """Extract `(flag, src, dst)` tuples for mount/symlink flags in `argv` in order."""
    pairs: list[tuple[str, str, str]] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("--bind", "--ro-bind", "--symlink") and i + 2 < len(argv):
            pairs.append((tok, argv[i + 1], argv[i + 2]))
            i += 3
            continue
        i += 1
    return pairs


def _setenv_map(argv: tuple[str, ...]) -> dict[str, str]:
    env: dict[str, str] = {}
    i = 0
    while i < len(argv):
        if argv[i] == "--setenv" and i + 2 < len(argv):
            env[argv[i + 1]] = argv[i + 2]
            i += 3
            continue
        i += 1
    return env


def _install_fake_bwrap(bin_dir: Path, log_path: Path, *, exit_code: int = 0) -> Path:
    script = bin_dir / "bwrap"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(log_path)!r}, 'w', encoding='utf-8') as f:\n"
        "    json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd()}, f)\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    make_executable(script)
    return script


# -- Pure plan / policy tests --------------------------------------------


def test_env_allowlist_strips_secrets_and_forwards_safe_vars(lab):
    root = workspace(lab, name="sec-ws")
    plugins = load_plugins()
    session = open_session(str(root), need_workspace=True, plugins=plugins, cwd=root)
    assert session.workspace is not None

    host_env = {
        "HOME": str(lab.home),
        "USER": "dev",
        "PATH": "/usr/bin:/bin",
        "TERM": "xterm-256color",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "C.UTF-8",
        "SHELL": "/bin/bash",
        "GITHUB_TOKEN": "ghp_secret",
        "AWS_SECRET_ACCESS_KEY": "aws_secret",
        "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
        "DATABASE_URL": "postgres://prod:pw@db/prod",
        "ANTHROPIC_API_KEY": "sk-ant-secret",
    }

    plan = sandbox_cmd.build_sandbox_plan(
        target_root=root,
        target_name="sec-ws",
        workspaces=(session.workspace,),
        locations=session.locations,
        plugins=plugins,
        command=("echo", "hi"),
        host_env=host_env,
        cwd=root,
    )

    assert "--clearenv" in plan.bwrap_argv
    env = _setenv_map(plan.bwrap_argv)
    assert env["HOME"] == str(lab.home.resolve())
    assert env["TERM"] == "xterm-256color"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["LC_ALL"] == "C.UTF-8"
    assert env["OW_SANDBOX"] == "1"
    assert env["OW_WORKSPACE"] == "sec-ws"

    for leaked in (
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "SSH_AUTH_SOCK",
        "DATABASE_URL",
        "ANTHROPIC_API_KEY",
    ):
        assert leaked not in env


def test_profile_autodetect_and_override(lab):
    root = workspace(lab, name="prof-ws")
    plugins = load_plugins()
    session = open_session(str(root), need_workspace=True, plugins=plugins, cwd=root)
    assert session.workspace is not None

    (lab.home / ".claude").mkdir()
    (lab.home / ".claude.json").write_text("{}\n")
    (lab.home / ".opencode").mkdir()
    (lab.home / ".config" / "opencode").mkdir(parents=True)

    host_env = {
        "HOME": str(lab.home),
        "USER": "dev",
        "PATH": "/usr/bin:/bin",
        "ANTHROPIC_API_KEY": "sk-ant-123",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "OPENAI_API_KEY": "sk-openai-456",
        "GITHUB_TOKEN": "ghp_nope",
    }

    claude_plan = sandbox_cmd.build_sandbox_plan(
        target_root=root,
        target_name="prof-ws",
        workspaces=(session.workspace,),
        locations=session.locations,
        plugins=plugins,
        command=("claude", "--version"),
        host_env=host_env,
        cwd=root,
    )
    assert claude_plan.profile == "claude"
    mounts = _mount_pairs(claude_plan.bwrap_argv)
    assert ("--bind", str(lab.home / ".claude"), str(lab.home / ".claude")) in mounts
    assert ("--bind", str(lab.home / ".claude.json"), str(lab.home / ".claude.json")) in mounts
    # Does NOT mount other harnesses' dirs
    assert not any("opencode" in src for _, src, _ in mounts)
    env = _setenv_map(claude_plan.bwrap_argv)
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-123"
    assert env["CLAUDE_CODE_USE_VERTEX"] == "1"
    assert "OPENAI_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env

    # Dry-run redacts secret env values while keeping keys visible
    preview = sandbox_cmd.format_dry_run(claude_plan)
    assert "ANTHROPIC_API_KEY" in preview
    assert "sk-ant-123" not in preview
    assert "<redacted>" in preview

    # Explicit `--profile none` disables auto-detection
    none_plan = sandbox_cmd.build_sandbox_plan(
        target_root=root,
        target_name="prof-ws",
        workspaces=(session.workspace,),
        locations=session.locations,
        plugins=plugins,
        command=("claude", "--version"),
        cli_profile="none",
        host_env=host_env,
        cwd=root,
    )
    assert none_plan.profile is None
    assert "ANTHROPIC_API_KEY" not in _setenv_map(none_plan.bwrap_argv)

    # Unknown profile raises UsageError
    with pytest.raises(UsageError, match="unknown sandbox profile"):
        sandbox_cmd.resolve_profile(("claude",), cli_profile="bogus")


def test_partial_rw_bare_repo_and_ro_ow_and_git_file(lab):
    root_a = workspace(lab, name="ws-a", repo="community", branch="feat-a")
    bare = (repos_dir() / "community.git").resolve()
    root_b = workspace(lab, name="ws-b", repo="community", bare=bare, branch="feat-b")

    plugins = load_plugins()
    sess_a = open_session(str(root_a), need_workspace=True, plugins=plugins, cwd=root_a)
    sess_b = open_session(str(root_b), need_workspace=True, plugins=plugins, cwd=root_b)
    assert sess_a.workspace is not None
    wt_a = sess_a.locations[0].worktree_id
    wt_b = sess_b.locations[0].worktree_id
    assert wt_a != wt_b

    plan = sandbox_cmd.build_sandbox_plan(
        target_root=root_a,
        target_name="ws-a",
        workspaces=(sess_a.workspace,),
        locations=sess_a.locations,
        plugins=plugins,
        command=("git", "status"),
        cwd=root_a,
    )

    mounts = _mount_pairs(plan.bwrap_argv)
    ws_bind_idx = mounts.index(("--bind", str(root_a.resolve()), str(root_a.resolve())))
    ow_ro_idx = mounts.index(("--ro-bind", str((root_a / ".ow").resolve()), str((root_a / ".ow").resolve())))
    git_ro_idx = mounts.index(
        ("--ro-bind", str((root_a / "community" / ".git").resolve()), str((root_a / "community" / ".git").resolve()))
    )
    # RO overlays must come AFTER the workspace RW bind
    assert ow_ro_idx > ws_bind_idx
    assert git_ro_idx > ws_bind_idx

    # Bare root is RO, only objects/refs/logs/worktrees/<wt_a> are RW
    bare_ro_idx = mounts.index(("--ro-bind", str(bare), str(bare)))
    for sub in ("objects", "refs", "logs", f"worktrees/{wt_a}"):
        sub_idx = mounts.index(("--bind", str(bare / sub), str(bare / sub)))
        assert sub_idx > bare_ro_idx

    # Sibling workspace's worktree dir, hooks, and config are NOT bound RW
    assert ("--bind", str(bare / "worktrees" / wt_b), str(bare / "worktrees" / wt_b)) not in mounts
    assert ("--bind", str(bare / "hooks"), str(bare / "hooks")) not in mounts
    assert ("--bind", str(bare / "config"), str(bare / "config")) not in mounts


def test_git_config_ro_excludes_credentials(lab):
    root = workspace(lab, name="gitcfg-ws")
    (lab.home / ".gitconfig").write_text("[user]\nname = Dev\n")
    (lab.home / ".git-credentials").write_text("https://user:secret@github.com\n")
    (lab.home / ".config" / "git").mkdir(parents=True, exist_ok=True)
    (lab.home / ".config" / "git" / "config").write_text("[core]\npager = cat\n")
    (lab.home / ".config" / "git" / "credentials").write_text("secret\n")

    plugins = load_plugins()
    session = open_session(str(root), need_workspace=True, plugins=plugins, cwd=root)
    assert session.workspace is not None

    plan = sandbox_cmd.build_sandbox_plan(
        target_root=root,
        target_name="gitcfg-ws",
        workspaces=(session.workspace,),
        locations=session.locations,
        plugins=plugins,
        command=("git", "status"),
        cwd=root,
    )
    mounts = _mount_pairs(plan.bwrap_argv)
    assert ("--ro-bind", str(lab.home / ".gitconfig"), str(lab.home / ".gitconfig")) in mounts
    assert (
        "--ro-bind",
        str(lab.home / ".config" / "git" / "config"),
        str(lab.home / ".config" / "git" / "config"),
    ) in mounts
    assert not any("credentials" in src for _, src, _ in mounts)


def test_plugin_mounts_for_odoo_venv_and_mise(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws_root = init_workspace(lab, "community", name="odoo-ws")

    venv_dir = venvs_dir() / "py3.12-18.0"
    (venv_dir / "bin").mkdir(parents=True)
    mise_data = lab.home / ".local" / "share" / "mise"
    mise_data.mkdir(parents=True)

    plugins = load_plugins()
    session = open_session(str(ws_root), need_workspace=True, plugins=plugins, cwd=ws_root)
    assert session.workspace is not None

    plan = sandbox_cmd.build_sandbox_plan(
        target_root=ws_root,
        target_name="odoo-ws",
        workspaces=(session.workspace,),
        locations=session.locations,
        plugins=plugins,
        command=("python", "--version"),
        cwd=ws_root,
    )
    mounts = _mount_pairs(plan.bwrap_argv)
    assert ("--ro-bind", str(venv_dir), str(venv_dir)) in mounts
    assert ("--ro-bind", str(mise_data), str(mise_data)) in mounts

    env = _setenv_map(plan.bwrap_argv)
    assert env["ODOO_RC"] == str(ws_root / "odoorc")
    assert env["MISE_CACHE_DIR"].startswith("/tmp/")
    assert env["MISE_STATE_DIR"].startswith("/tmp/")


def test_auto_mounts_home_binary_and_symlink_target(lab):
    root = workspace(lab, name="bin-ws")
    pkg = lab.home / ".local" / "lib" / "my-agent"
    pkg.mkdir(parents=True)
    target = pkg / "cli.sh"
    target.write_text("#!/bin/sh\necho ok\n")
    make_executable(target)

    local_bin = lab.home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    link = local_bin / "my-agent"
    link.symlink_to("../lib/my-agent/cli.sh")

    plugins = load_plugins()
    session = open_session(str(root), need_workspace=True, plugins=plugins, cwd=root)
    assert session.workspace is not None

    plan = sandbox_cmd.build_sandbox_plan(
        target_root=root,
        target_name="bin-ws",
        workspaces=(session.workspace,),
        locations=session.locations,
        plugins=plugins,
        command=("my-agent",),
        host_env={
            "HOME": str(lab.home),
            "PATH": f"{local_bin}:/usr/bin:/bin",
        },
        cwd=root,
    )
    mounts = _mount_pairs(plan.bwrap_argv)
    assert ("--ro-bind", str(target.resolve()), str(target.resolve())) in mounts
    assert ("--symlink", "../lib/my-agent/cli.sh", str(link)) in mounts


# -- CLI scenario tests --------------------------------------------------


def test_cli_dry_run_without_command_defaults_to_shell(lab):
    root = workspace(lab, name="shell-ws")

    res = lab.ow("sandbox", "-n", cwd=root)

    assert res.exit_code == 0, res.stderr
    assert f"# workspace: {root.resolve()}" in res.stdout
    assert "# profile: none" in res.stdout
    assert "bwrap" in res.stdout
    assert "not found on PATH" in res.stderr


def test_cli_dry_run_with_profile_and_extra_mounts_and_env(lab, monkeypatch):
    root = workspace(lab, name="opts-ws")
    extra_ro = lab.home / "docs-ro"
    extra_rw = lab.home / "notes-rw"
    extra_ro.mkdir()
    extra_rw.mkdir()
    monkeypatch.setenv("CUSTOM_TOKEN", "super-secret")
    monkeypatch.setenv("CUSTOM_FLAG", "yes")

    res = lab.ow(
        "sandbox",
        "-n",
        "--profile",
        "codex",
        "--ro",
        str(extra_ro),
        "--rw",
        str(extra_rw),
        "--env",
        "CUSTOM_TOKEN",
        "--env",
        "CUSTOM_FLAG",
        "--",
        "echo",
        "hello",
        cwd=root,
    )

    assert res.exit_code == 0, res.stderr
    assert "# profile: codex" in res.stdout
    assert f"--ro-bind {shlex.quote(str(extra_ro))} {shlex.quote(str(extra_ro))}" in res.stdout
    assert f"--bind {shlex.quote(str(extra_rw))} {shlex.quote(str(extra_rw))}" in res.stdout
    assert f"CUSTOM_TOKEN {shlex.quote('<redacted>')}" in res.stdout
    assert "super-secret" not in res.stdout
    assert "CUSTOM_FLAG yes" in res.stdout
    assert "-- echo hello" in res.stdout


def test_cli_errors_without_bwrap_or_on_non_linux_or_missing_path(lab, monkeypatch):
    root = workspace(lab, name="err-ws")

    missing_bwrap = lab.ow("sandbox", "--", "echo", "hi", cwd=root)
    assert missing_bwrap.exit_code == 2
    assert "bubblewrap (`bwrap`) not found" in missing_bwrap.stderr

    missing_ro = lab.ow("sandbox", "-n", "--ro", str(lab.home / "does-not-exist"), cwd=root)
    assert missing_ro.exit_code == 2
    assert "--ro path does not exist" in missing_ro.stderr

    monkeypatch.setattr(sandbox_cmd.sys, "platform", "darwin")
    non_linux = lab.ow("sandbox", "-n", cwd=root)
    assert non_linux.exit_code == 2
    assert "requires Linux" in non_linux.stderr


def test_cli_executes_bwrap_creates_dirs_starts_services_and_propagates_exit_code(lab, monkeypatch):
    root = workspace(lab, name="exec-ws")
    bin_dir = Path(os.environ["PATH"].split(os.pathsep)[0])
    log_path = lab.root / "bwrap.json"
    _install_fake_bwrap(bin_dir, log_path, exit_code=42)

    services_called: list[tuple[str, ...]] = []

    def fake_prepare_services(workspaces, plugins):
        services_called.append(tuple(ws.name for ws in workspaces))

    monkeypatch.setattr(sandbox_cmd, "prepare_services", fake_prepare_services)

    res = lab.ow("sandbox", "--profile", "claude", "--", "claude", "-p", "hi", cwd=root)

    assert res.exit_code == 42
    assert services_called == [("exec-ws",)]
    assert not (lab.home / ".claude").exists()
    assert (repos_dir() / "community.git" / "logs").is_dir()

    recorded = json.loads(log_path.read_text(encoding="utf-8"))
    argv = tuple(recorded["argv"])
    assert argv[-3:] == ("--", "claude", "-p", "hi")[-3:]
    assert argv[-4:] == ("--", "claude", "-p", "hi")


def test_cli_respects_global_and_workspace_sandbox_config(lab, monkeypatch):
    root = workspace(lab, name="cfg-ws")
    extra = lab.home / "shared-ref"
    extra.mkdir()
    global_config().parent.mkdir(parents=True, exist_ok=True)
    global_config().write_text(
        f'[sandbox]\nro_paths = ["{extra}"]\npass_env = ["MY_CFG_VAR"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MY_CFG_VAR", "from-global")

    res = lab.ow("sandbox", "-n", "--", "true", cwd=root)
    assert res.exit_code == 0, res.stderr
    assert str(extra) in res.stdout
    assert "MY_CFG_VAR from-global" in res.stdout


def test_cli_multi_stack_root_and_single_stack_targeting(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "voip", {"master": {"README.md": "voip\n"}}, base="master")

    multi = lab.home / "review-multi"
    multi.mkdir(parents=True)
    assert lab.ow("init", "odoo=community@18.0", "iap=community@18.0", "voip=voip", cwd=multi).exit_code == 0

    # At multi-stack root: whole root RW, root .ow RO (no per-stack .ow), conflicting ODOO_RC omitted
    res_root = lab.ow("sandbox", "-n", "--", "true", cwd=multi)
    assert res_root.exit_code == 0, res_root.stderr
    assert f"--bind {shlex.quote(str(multi.resolve()))} {shlex.quote(str(multi.resolve()))}" in res_root.stdout
    root_ow = (multi / ".ow").resolve()
    assert f"--ro-bind {shlex.quote(str(root_ow))} {shlex.quote(str(root_ow))}" in res_root.stdout
    for stack_name in ("odoo", "iap", "voip"):
        assert not (multi / stack_name / ".ow").exists()
    assert "ODOO_RC" not in res_root.stdout

    # Targeting a single stack via `-w odoo`: scoped only to `review-multi/odoo`, ODOO_RC set
    res_stack = lab.ow("sandbox", "-w", "odoo", "-n", "--", "true", cwd=multi)
    assert res_stack.exit_code == 0, res_stack.stderr
    odoo_root = (multi / "odoo").resolve()
    assert f"--bind {shlex.quote(str(odoo_root))} {shlex.quote(str(odoo_root))}" in res_stack.stdout
    assert f"ODOO_RC {odoo_root / 'odoorc'}" in res_stack.stdout
    assert str(multi / "iap" / ".ow") not in res_stack.stdout


# -- Real bwrap integration test (when kernel/container permits userns) --


def _real_bwrap_available() -> str | None:
    if sys.platform != "linux":
        return None
    bwrap = shutil.which("bwrap", path="/usr/bin:/bin")
    if not bwrap:
        return None
    probe = subprocess.run(
        [bwrap, "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc",
         "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
         "--symlink", "usr/lib64", "/lib64", "--unshare-pid", "--", "/bin/true"],
        capture_output=True,
        check=False,
    )
    return bwrap if probe.returncode == 0 else None


def test_real_bwrap_enforces_isolation_and_allows_git_commit(lab, monkeypatch):
    bwrap = _real_bwrap_available()
    if bwrap is None:
        pytest.skip("real bubblewrap namespace creation unavailable in this test environment")

    root = workspace(lab, name="real-bwrap-ws", repo="community", branch="18.0-agent")
    bare = (repos_dir() / "community.git").resolve()

    # Plant secrets outside the workspace and in the host environment.
    ssh_dir = lab.home / ".ssh"
    ssh_dir.mkdir()
    secret_key = ssh_dir / "id_rsa"
    secret_key.write_text("TOP-SECRET-KEY\n")
    outside_file = lab.home / "outside.txt"
    outside_file.write_text("untouched\n")
    (lab.home / ".gitconfig").write_text("[user]\nname = Sandbox Agent\nemail = agent@example.com\n")

    bin_dir = Path(os.environ["PATH"].split(os.pathsep)[0])
    (bin_dir / "bwrap").symlink_to(bwrap)
    monkeypatch.setattr(sandbox_cmd, "prepare_services", lambda workspaces, plugins: None)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_super_secret")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

    check_script = (
        "set -e\n"
        # 1. Secret env vars stripped
        'test -z "${GITHUB_TOKEN:-}"\n'
        'test "$OW_SANDBOX" = "1"\n'
        # 2. Host ~/.ssh and outside files are invisible/unwritable
        f'test ! -e "{secret_key}"\n'
        f'test ! -e "{outside_file}"\n'
        # 3. .ow/config.toml, bare/config, and bare/hooks are read-only
        f'! touch "{root / ".ow" / "pwn"}" 2>/dev/null\n'
        f'! touch "{bare / "hooks" / "pre-commit"}" 2>/dev/null\n'
        f'! sh -c \'echo "# pwn" >> "{bare / "config"}"\' 2>/dev/null\n'
        # 4. Workspace edits + git commit succeed inside partial-RW bare repo
        f'cd "{root / "community"}"\n'
        'echo "hello from sandbox" > agent.txt\n'
        'git add agent.txt\n'
        'git commit -m "sandbox commit"\n'
    )

    res = lab.ow("sandbox", "--", "/bin/bash", "-c", check_script, cwd=root)
    assert res.exit_code == 0, res.stderr + res.stdout
    assert (root / "community" / "agent.txt").read_text() == "hello from sandbox\n"
    assert "sandbox commit" in lab.git(root / "community", "log", "-1", "--pretty=%s")


# -- Experimental notice, CLI shape, enablement --------------------------


def test_sandbox_is_announced_as_experimental_in_help_and_at_launch(lab):
    root = workspace(lab, name="exp-ws")

    helped = lab.ow("sandbox", "--help")
    first_line = next(line for line in helped.stdout.splitlines() if line.strip() and not line.startswith("Usage"))
    assert helped.exit_code == 0
    assert first_line.strip().lower().startswith("experimental")

    res = lab.ow("sandbox", "-n", "--", "true", cwd=root)
    assert res.exit_code == 0, res.stderr
    assert "# experimental:" in res.stderr
    assert "experimental" not in res.stdout


def test_new_session_flag_is_never_passed_to_bwrap(lab):
    root = workspace(lab, name="nosess-ws")

    res = lab.ow("sandbox", "-n", "--", "true", cwd=root)

    assert res.exit_code == 0, res.stderr
    assert "--new-session" not in res.stdout


def test_cli_takes_workspace_positionally_and_command_only_after_double_dash(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    multi = lab.home / "pos-multi"
    multi.mkdir(parents=True)
    assert lab.ow("init", "odoo=community@18.0", "iap=community@18.0", cwd=multi).exit_code == 0

    res = lab.ow("sandbox", "iap", "-n", cwd=multi)
    assert res.exit_code == 0, res.stderr
    assert f"# workspace: {(multi / 'iap').resolve()}" in res.stdout

    stray = lab.ow("sandbox", "iap", "echo", "-n", cwd=multi)
    assert stray.exit_code == 2
    assert "pass commands after `--`" in stray.stderr

    short = lab.ow("sandbox", "iap", "-n", "-p", "codex", "--", "echo", "hi", cwd=multi)
    assert short.exit_code == 0, short.stderr
    assert "# profile: codex" in short.stdout
    assert "-- echo hi" in short.stdout


def test_dry_run_redacts_only_secret_variables(lab, monkeypatch):
    root = workspace(lab, name="redact-ws")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Ada Lovelace")
    monkeypatch.setenv("MY_AUTH_HEADER", "Bearer abc")

    res = lab.ow("sandbox", "-n", "--env", "MY_AUTH_HEADER", "--", "true", cwd=root)

    assert res.exit_code == 0, res.stderr
    assert f"GIT_AUTHOR_NAME {shlex.quote('Ada Lovelace')}" in res.stdout
    assert "Bearer abc" not in res.stdout
    for secret in ("API_KEY", "GITHUB_TOKEN", "DB_PASSWORD", "AUTH", "X_AUTH_Y", "CLIENT_SECRET"):
        assert sandbox_cmd._is_secret_var(secret), secret
    for plain in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "AUTHOR", "TERM"):
        assert not sandbox_cmd._is_secret_var(plain), plain


def test_sandbox_refuses_when_disabled(lab):
    root = workspace(lab, name="off-ws")
    global_config().parent.mkdir(parents=True, exist_ok=True)
    global_config().write_text("[sandbox]\nenabled = false\n", encoding="utf-8")

    res = lab.ow("sandbox", "-n", "--", "true", cwd=root)

    assert res.exit_code == 1
    assert "sandbox.enabled" in res.stderr
    assert res.stdout == ""


# -- Profiles only mount what exists -------------------------------------


def test_profile_mounts_only_existing_host_paths_and_creates_nothing(lab):
    root = workspace(lab, name="lean-ws")
    plugins = load_plugins()
    session = open_session(str(root), need_workspace=True, plugins=plugins, cwd=root)
    assert session.workspace is not None

    def plan_for():
        return sandbox_cmd.build_sandbox_plan(
            target_root=root,
            target_name="lean-ws",
            workspaces=(session.workspace,),
            locations=session.locations,
            plugins=plugins,
            command=("claude",),
            cli_profile="claude",
            cwd=root,
        )

    bare = (repos_dir() / "community.git").resolve()
    absent = plan_for()
    assert not any(".claude" in tok for tok in absent.bwrap_argv)
    assert all(d.is_relative_to(bare) for d in absent.ensure_dirs)
    assert not (lab.home / ".claude").exists()

    (lab.home / ".claude").mkdir()
    (lab.home / ".claude.json").write_text("{}")
    present = _mount_pairs(plan_for().bwrap_argv)
    assert ("--bind", str(lab.home / ".claude"), str(lab.home / ".claude")) in present
    assert ("--bind", str(lab.home / ".claude.json"), str(lab.home / ".claude.json")) in present
    assert not any(src == str(lab.home / ".cache" / "claude") for _, src, _ in present)


# -- Plugin mounts come only from `ow_sandbox_mounts` --------------------


def test_no_hardcoded_host_postgres_socket_mount(monkeypatch):
    real_is_dir = Path.is_dir
    monkeypatch.setattr(
        Path, "is_dir", lambda self: str(self) == "/var/run/postgresql" or real_is_dir(self)
    )
    args: list[str] = []

    sandbox_cmd._append_system_mounts(args)

    assert "/var/run/postgresql" not in args


def test_plugin_hook_mounts_and_env_are_applied(lab):
    ro_dir = lab.home / "sockets"
    rw_dir = lab.home / "scratch"
    ro_dir.mkdir()
    rw_dir.mkdir()
    plugin = (
        "from pathlib import Path\n"
        "from ow.plugin_api import SandboxMounts, hookimpl\n"
        "OW_API_VERSION = 1\n"
        'PLUGIN_NAME = "demo_mounts"\n'
        "@hookimpl\n"
        "def ow_sandbox_mounts():\n"
        f"    return SandboxMounts(ro_paths=(Path({str(ro_dir)!r}),), rw_paths=(Path({str(rw_dir)!r}),),"
        f' env={{"DEMO_SOCKET": {str(ro_dir / "s")!r}}})\n'
    )
    add_repo(lab, "demo", {"main": {"ow_plugin.py": plugin}}, base="main")
    ws = lab.home / "demo-ws"
    ws.mkdir(parents=True)
    assert lab.ow("init", "demo@main", cwd=ws).exit_code == 0
    assert lab.ow("trust", "-y", cwd=ws).exit_code == 0

    res = lab.ow("sandbox", "-n", "--", "true", cwd=ws)

    assert res.exit_code == 0, res.stderr
    assert f"--ro-bind {shlex.quote(str(ro_dir))} {shlex.quote(str(ro_dir))}" in res.stdout
    assert f"--bind {shlex.quote(str(rw_dir))} {shlex.quote(str(rw_dir))}" in res.stdout
    assert f"DEMO_SOCKET {shlex.quote(str(ro_dir / 's'))}" in res.stdout


# -- Files a session must not rewrite ------------------------------------

_GUARD_PLUGIN = (
    "from ow.plugin_api import hookimpl\n"
    "OW_API_VERSION = 1\n"
    'PLUGIN_NAME = "demo_guard"\n'
    "@hookimpl\n"
    "def ow_known_repos():\n"
    "    return []\n"
)


def _guarded_workspace(lab, name: str = "guard-ws") -> Path:
    """An Odoo workspace (so ow generates `odoorc`) whose repo carries an in-repo plugin."""
    add_repo(lab, "community", {"18.0": {**lab.odoo_files(series="18.0"), "ow_plugin.py": _GUARD_PLUGIN}})
    return init_workspace(lab, "community", name=name)


def _ro_bound(argv: tuple[str, ...], path: Path) -> bool:
    return ("--ro-bind", str(path), str(path)) in _mount_pairs(argv)


def _plan(root: Path, name: str):
    plugins = load_plugins()
    session = open_session(str(root), need_workspace=True, plugins=plugins, cwd=root)
    assert session.workspace is not None
    return sandbox_cmd.build_sandbox_plan(
        target_root=root,
        target_name=name,
        workspaces=(session.workspace,),
        locations=session.locations,
        plugins=session.plugins,
        command=("true",),
        cwd=root,
    ), session


def test_worktree_commondir_and_gitdir_are_read_only_over_the_rw_worktree_dir(lab):
    root_a = workspace(lab, name="pin-a", repo="community", branch="feat-a")
    bare = (repos_dir() / "community.git").resolve()
    root_b = workspace(lab, name="pin-b", repo="community", bare=bare, branch="feat-b")

    plan, session = _plan(root_a, "pin-a")
    other = open_session(str(root_b), need_workspace=True, plugins=load_plugins(), cwd=root_b)

    mounts = _mount_pairs(plan.bwrap_argv)
    wt_dir = bare / "worktrees" / session.locations[0].worktree_id
    rw_idx = mounts.index(("--bind", str(wt_dir), str(wt_dir)))
    for name in ("commondir", "gitdir"):
        assert mounts.index(("--ro-bind", str(wt_dir / name), str(wt_dir / name))) > rw_idx
    # The index and HEAD of the worktree stay writable (git add / commit), siblings are not pinned here.
    assert not _ro_bound(plan.bwrap_argv, wt_dir / "index")
    sibling = bare / "worktrees" / other.locations[0].worktree_id
    assert not any(src.startswith(str(sibling)) for _, src, _ in mounts)


def test_lock_files_and_trusted_plugin_files_are_read_only(lab):
    root = _guarded_workspace(lab)
    plugin_file = (root / "community" / "ow_plugin.py").resolve()
    odoorc = (root / "odoorc").resolve()
    assert "odoorc" in read_lock(root)

    untrusted, _ = _plan(root, "guard-ws")
    assert _ro_bound(untrusted.bwrap_argv, odoorc)
    assert not _ro_bound(untrusted.bwrap_argv, plugin_file)

    assert lab.ow("trust", "-y", cwd=root).exit_code == 0
    trusted, _ = _plan(root, "guard-ws")
    mounts = _mount_pairs(trusted.bwrap_argv)
    ws_bind = mounts.index(("--bind", str(root.resolve()), str(root.resolve())))
    for protected in (odoorc, plugin_file):
        assert mounts.index(("--ro-bind", str(protected), str(protected))) > ws_bind

    # A lock entry for a file that is gone is skipped, not mounted.
    odoorc.unlink()
    gone, _ = _plan(root, "guard-ws")
    assert not any(src == str(odoorc) for _, src, _ in _mount_pairs(gone.bwrap_argv))


def test_multi_stack_lock_files_are_mounted_per_target_and_stacks_have_no_ow_dir(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    multi = lab.home / "lock-multi"
    multi.mkdir(parents=True)
    assert lab.ow("init", "odoo=community@18.0", "iap=community@18.0", cwd=multi).exit_code == 0
    assert "odoo/odoorc" in read_lock(multi)

    def ro(stdout: str, path: Path) -> bool:
        quoted = shlex.quote(str(path.resolve()))
        return f"--ro-bind {quoted} {quoted}" in stdout

    whole = lab.ow("sandbox", "-n", "--", "true", cwd=multi)
    assert whole.exit_code == 0, whole.stderr
    assert ro(whole.stdout, multi / "odoo" / "odoorc")
    assert ro(whole.stdout, multi / "iap" / "odoorc")
    assert ro(whole.stdout, multi / ".ow")

    one = lab.ow("sandbox", "iap", "-n", "--", "true", cwd=multi)
    assert one.exit_code == 0, one.stderr
    assert ro(one.stdout, multi / "iap" / "odoorc")
    assert not ro(one.stdout, multi / "odoo" / "odoorc")
    assert not ro(one.stdout, multi / ".ow")


def test_real_bwrap_pins_worktree_admin_lock_and_plugin_files_but_not_tracked_files(lab, monkeypatch):
    bwrap = _real_bwrap_available()
    if bwrap is None:
        pytest.skip("real bubblewrap namespace creation unavailable in this test environment")

    root = _guarded_workspace(lab, name="pinned-ws")
    assert lab.ow("trust", "-y", cwd=root).exit_code == 0
    assert "odoorc" in read_lock(root)
    bare = (repos_dir() / "community.git").resolve()
    (wt_dir,) = (bare / "worktrees").iterdir()
    commondir = wt_dir / "commondir"
    odoorc = root / "odoorc"
    plugin_file = root / "community" / "ow_plugin.py"
    originals = {p: p.read_bytes() for p in (commondir, odoorc, plugin_file)}

    bin_dir = Path(os.environ["PATH"].split(os.pathsep)[0])
    (bin_dir / "bwrap").symlink_to(bwrap)
    monkeypatch.setattr(sandbox_cmd, "prepare_services", lambda workspaces, plugins: None)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

    script = (
        "set -e\n"
        f"sh -c 'echo x >> \"{commondir}\"' 2>/dev/null && exit 11\n"
        f"sh -c 'echo x >> \"{wt_dir / 'gitdir'}\"' 2>/dev/null && exit 12\n"
        f"sh -c 'echo x >> \"{odoorc}\"' 2>/dev/null && exit 13\n"
        f"sh -c 'echo x >> \"{plugin_file}\"' 2>/dev/null && exit 14\n"
        f'cd "{root / "community"}"\n'
        "echo '# edited' >> odoo-bin\n"
        "git add odoo-bin\n"
        'git commit -m "tracked edit"\n'
    )
    res = lab.ow("sandbox", "--", "/bin/bash", "-c", script, cwd=root)

    assert res.exit_code == 0, res.stderr + res.stdout
    assert {p: p.read_bytes() for p in originals} == originals
    assert "tracked edit" in lab.git(root / "community", "log", "-1", "--pretty=%s")
    assert (root / "community" / "odoo-bin").read_text().endswith("# edited\n")
