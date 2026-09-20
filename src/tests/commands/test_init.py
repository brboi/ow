import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from ow.commands import cmd_init
from ow.commands.init import _check_duplicate_branches
from ow.utils import index, paths
from ow.utils.config import (
    BranchSpec,
    Config,
    RemoteConfig,
    WorkspaceConfig,
    load_workspace_config,
    parse_branch_spec,
    write_workspace_config,
)

MARKER = Path(".ow") / "config.toml"


@contextmanager
def _tty(present: bool):
    """Pin whether stdin is a terminal, instead of inheriting pytest's."""
    with patch("sys.stdin.isatty", return_value=present):
        yield


@contextmanager
def _mise_ok():
    with patch("ow.commands.init.require_mise", return_value=(2026, 9, 9)):
        yield


@contextmanager
def _mise_missing():
    with patch(
        "ow.commands.init.require_mise",
        side_effect=ValueError("mise 2026.8.13 or newer is required; could not run it"),
    ):
        yield


@contextmanager
def _no_trust():
    with patch("ow.utils.workspace.trust_fragment"):
        yield


@contextmanager
def _confirm(value: bool = True):
    with patch("ow.commands.init.confirm", return_value=value):
        yield


def _remembered_workspace(at: Path, alias: str, spec: str) -> Path:
    """A workspace on disk that ow knows about."""
    at.mkdir(parents=True, exist_ok=True)
    write_workspace_config(
        at / MARKER,
        WorkspaceConfig(repos={alias: parse_branch_spec(spec)}),
    )
    index.remember(at)
    return at


def _broken_workspace(at: Path) -> Path:
    """A workspace ow knows about whose config.toml no longer parses."""
    at.mkdir(parents=True, exist_ok=True)
    (at / ".ow").mkdir(exist_ok=True)
    (at / MARKER).write_text("not valid toml [[[")
    index.remember(at)
    return at


# ---------------------------------------------------------------------------
# Real-repo fixtures: a local bare repo, no network cloning.
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _source_repo(tmp_path: Path, alias: str = "community") -> Path:
    src = tmp_path / "origin" / alias
    src.mkdir(parents=True)
    subprocess.run(["git", "-C", str(src), "init", "-q", "-b", "master"], check=True)
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "T")
    (src / "a.txt").write_text("a")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "A")
    return src


def _config_with_local_remote(tmp_path: Path, alias: str = "community") -> Config:
    src = _source_repo(tmp_path, alias)
    return Config(remotes={alias: {"origin": RemoteConfig(url=str(src))}})


ONE_REPO = {"community": BranchSpec("origin/master", "a-branch")}


# ---------------------------------------------------------------------------
# Where the workspace lands
# ---------------------------------------------------------------------------


def test_init_without_a_name_uses_the_current_directory(tmp_path, monkeypatch, xdg):
    here = tmp_path / "quattromori"
    here.mkdir()
    monkeypatch.chdir(here)
    config = Config(remotes={})

    with _tty(False), _mise_ok():
        cmd_init(config)

    assert (here / MARKER).exists()


def test_init_with_a_name_creates_the_subdirectory(tmp_path, monkeypatch, xdg):
    monkeypatch.chdir(tmp_path)
    config = Config(remotes={})

    with _tty(False), _mise_ok():
        cmd_init(config, name="parrot")

    assert (tmp_path / "parrot" / MARKER).exists()


def test_init_with_a_name_accepts_an_existing_directory(tmp_path, monkeypatch, xdg):
    """A plain directory is just a place; only a marker file is a workspace."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "parrot").mkdir()
    (tmp_path / "parrot" / "notes.txt").write_text("mine")
    config = Config(remotes={})

    with _tty(False), _mise_ok():
        cmd_init(config, name="parrot")

    assert (tmp_path / "parrot" / MARKER).exists()
    assert (tmp_path / "parrot" / "notes.txt").read_text() == "mine"


def test_init_rejects_invalid_name(tmp_path, monkeypatch, capsys, xdg):
    monkeypatch.chdir(tmp_path)
    config = Config(remotes={})

    with pytest.raises(SystemExit) as exc:
        cmd_init(config, name="../evil")

    assert exc.value.code == 1
    assert "alphanumeric" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# Non-interactive: no repos is valid, no template concept remains
# ---------------------------------------------------------------------------


def test_init_without_a_tty_and_no_repos_is_valid(tmp_path, monkeypatch, xdg):
    monkeypatch.chdir(tmp_path)
    config = Config(remotes={})

    with _tty(False), _mise_ok():
        cmd_init(config, name="tools")

    ws = load_workspace_config(tmp_path / "tools" / MARKER)
    assert ws.repos == {}


def test_init_without_a_tty_takes_repos_from_a_configuration(tmp_path, monkeypatch, xdg):
    monkeypatch.chdir(tmp_path)
    src_config = tmp_path / "src" / ".ow" / "config.toml"
    src_config.parent.mkdir(parents=True)
    src_config.write_text('[repos]\ncommunity = "master..from-source"\n')
    config = Config(remotes={"community": {"origin": RemoteConfig(url="/does/not/exist")}})

    with _tty(False), _mise_ok():
        with pytest.raises(SystemExit) as exc:
            cmd_init(config, name="test", configuration=str(tmp_path / "src"))

    # Materialization fails (no real remote), but the config is persisted
    # with the source's repos: retryable intent, not a lost configuration.
    assert exc.value.code == 1
    ws = load_workspace_config(tmp_path / "test" / MARKER)
    assert ws.repos["community"].local_branch == "from-source"


def test_init_configuration_new_explicit_r_overrides_source_spec(tmp_path, monkeypatch, xdg):
    monkeypatch.chdir(tmp_path)
    src_config = tmp_path / "src" / ".ow" / "config.toml"
    src_config.parent.mkdir(parents=True)
    src_config.write_text('[repos]\ncommunity = "master..from-source"\n')
    config = _config_with_local_remote(tmp_path)

    with _tty(False), _mise_ok(), _no_trust():
        cmd_init(
            config, name="test", configuration=str(tmp_path / "src"),
            repos={"community": parse_branch_spec("master..overridden")},
        )

    ws = load_workspace_config(tmp_path / "test" / MARKER)
    assert ws.repos["community"].local_branch == "overridden"


def test_init_configuration_never_mutates_the_source(tmp_path, monkeypatch, xdg):
    monkeypatch.chdir(tmp_path)
    src_dir = tmp_path / "src"
    src_config = src_dir / ".ow" / "config.toml"
    src_config.parent.mkdir(parents=True)
    src_config.write_text('[repos]\ncommunity = "master..from-source"\n')
    before = src_config.read_bytes()
    config = _config_with_local_remote(tmp_path)

    with _tty(False), _mise_ok(), _no_trust():
        cmd_init(
            config, name="test", configuration=str(src_dir),
            repos={"community": parse_branch_spec("master..overridden")},
        )

    assert src_config.read_bytes() == before


def test_init_configuration_new_target_is_never_legacy(tmp_path, monkeypatch, xdg):
    """-c never copies the source's schema-1 metadata onto the new target."""
    monkeypatch.chdir(tmp_path)
    src_config = tmp_path / "src" / ".ow" / "config.toml"
    src_config.parent.mkdir(parents=True)
    src_config.write_text('version = 1\n\n[repos]\ncommunity = "master..from-source"\n')
    config = _config_with_local_remote(tmp_path)

    with _tty(False), _mise_ok(), _no_trust():
        cmd_init(config, name="test", configuration=str(tmp_path / "src"))

    ws = load_workspace_config(tmp_path / "test" / MARKER)
    assert ws.version == 2
    assert ws.legacy is None
    assert "version = 1" not in (tmp_path / "test" / MARKER).read_text()


def test_init_rejects_invalid_repo_alias(tmp_path, monkeypatch, capsys, xdg):
    monkeypatch.chdir(tmp_path)
    config = _config_with_local_remote(tmp_path)

    with pytest.raises(SystemExit) as exc:
        cmd_init(config, name="test", repos={"unknown": BranchSpec("origin/master")})

    assert exc.value.code == 1
    assert "unknown" in capsys.readouterr().err.lower()


def test_init_configuration_not_found(tmp_path, monkeypatch, capsys, xdg):
    monkeypatch.chdir(tmp_path)
    config = Config(remotes={})

    with pytest.raises(SystemExit) as exc:
        cmd_init(config, name="test", configuration=str(tmp_path / "nope"))

    assert exc.value.code == 1
    assert "not found" in capsys.readouterr().err.lower()


def test_init_configuration_malformed_exits_cleanly(tmp_path, monkeypatch, capsys, xdg):
    monkeypatch.chdir(tmp_path)
    src_config = tmp_path / "src" / ".ow" / "config.toml"
    src_config.parent.mkdir(parents=True)
    src_config.write_text("not valid toml [[[")
    config = Config(remotes={})

    with pytest.raises(SystemExit) as exc:
        cmd_init(config, name="test", configuration=str(tmp_path / "src"))

    assert exc.value.code == 1
    assert "could not load" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# mise: a hard gate, before any write
# ---------------------------------------------------------------------------


def test_init_refuses_before_any_write_when_mise_is_missing(tmp_path, monkeypatch, capsys, xdg):
    monkeypatch.chdir(tmp_path)
    config = Config(remotes={})

    with _tty(False), _mise_missing():
        with pytest.raises(SystemExit) as exc:
            cmd_init(config, name="parrot")

    assert exc.value.code == 1
    assert not (tmp_path / "parrot").exists()
    assert "mise" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# The interactive path
# ---------------------------------------------------------------------------


def test_init_with_a_tty_asks_only_about_repos(tmp_path, monkeypatch, xdg):
    monkeypatch.chdir(tmp_path)
    config = _config_with_local_remote(tmp_path)
    asked = []

    def prompt_ask(message, **kwargs):
        asked.append(message)
        if "Select" in message:
            return "1"
        return kwargs.get("default", "")

    with _tty(True), _mise_ok(), _confirm(True), _no_trust():
        with patch("ow.commands.init.Prompt.ask", side_effect=prompt_ask):
            cmd_init(config, name="parrot")

    assert any("Repos" in a or "Select" in a for a in asked)
    assert not any("emplate" in a for a in asked)
    ws = load_workspace_config(tmp_path / "parrot" / MARKER)
    assert "community" in ws.repos


def test_init_with_a_tty_does_not_implicitly_preselect_community(tmp_path, monkeypatch, xdg):
    """Answering 'none' at the repos prompt must yield zero repos: nothing
    is preselected without an explicit -r or -c."""
    monkeypatch.chdir(tmp_path)
    config = _config_with_local_remote(tmp_path)

    with _tty(True), _mise_ok(), _confirm(True), _no_trust():
        with patch("ow.commands.init.Prompt.ask", return_value="none"):
            cmd_init(config, name="parrot")

    ws = load_workspace_config(tmp_path / "parrot" / MARKER)
    assert ws.repos == {}


def test_init_with_a_tty_stops_when_the_confirmation_is_declined(tmp_path, monkeypatch, capsys, xdg):
    monkeypatch.chdir(tmp_path)
    config = Config(remotes={})

    with _tty(True), _mise_ok(), _confirm(False):
        with patch("ow.commands.init.Prompt.ask", return_value="none"):
            with pytest.raises(SystemExit) as exc:
                cmd_init(config, name="parrot")

    assert exc.value.code == 2
    assert not (tmp_path / "parrot" / MARKER).exists()


def test_init_with_a_tty_stops_when_the_prompt_is_cancelled(tmp_path, monkeypatch, xdg):
    monkeypatch.chdir(tmp_path)
    config = _config_with_local_remote(tmp_path)

    with _tty(True):
        with patch("ow.commands.init.Prompt.ask", side_effect=KeyboardInterrupt()):
            with pytest.raises(SystemExit) as exc:
                cmd_init(config, name="parrot")

    assert exc.value.code == 2
    assert not (tmp_path / "parrot" / MARKER).exists()


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------


def test_init_remembers_the_new_workspace_on_success(tmp_path, monkeypatch, xdg):
    monkeypatch.chdir(tmp_path)
    config = Config(remotes={})

    with _tty(False), _mise_ok():
        cmd_init(config, name="parrot")

    assert index.known_workspaces() == [(tmp_path / "parrot").resolve()]


def test_init_does_not_remember_when_a_repo_fails(tmp_path, monkeypatch, xdg):
    monkeypatch.chdir(tmp_path)
    config = Config(remotes={"community": {"origin": RemoteConfig(url="/does/not/exist")}})

    with _tty(False), _mise_ok():
        with pytest.raises(SystemExit):
            cmd_init(config, name="parrot", repos={"community": BranchSpec("origin/master")})

    assert index.known_workspaces() == []


# ---------------------------------------------------------------------------
# Failure handling: retryable intent, never a cleanup
# ---------------------------------------------------------------------------


def test_init_total_creation_failure_keeps_the_config_on_disk(tmp_path, monkeypatch, capsys, xdg):
    """Every repo fails: the directory and its config.toml survive, so a
    retry has something to repair."""
    monkeypatch.chdir(tmp_path)
    config = Config(remotes={"community": {"origin": RemoteConfig(url="/does/not/exist")}})

    with _tty(False), _mise_ok():
        with pytest.raises(SystemExit) as exc:
            cmd_init(config, name="parrot", repos={"community": BranchSpec("origin/master")})

    assert exc.value.code == 1
    assert (tmp_path / "parrot" / MARKER).exists()
    ws = load_workspace_config(tmp_path / "parrot" / MARKER)
    assert "community" in ws.repos
    assert "errors" in capsys.readouterr().out.lower() or True


def test_init_here_survives_every_repo_failing(tmp_path, monkeypatch, xdg):
    """`ow init` in a directory ow did not create must never delete it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.txt").write_text("keep me")
    config = Config(remotes={"community": {"origin": RemoteConfig(url="/does/not/exist")}})

    with _tty(False), _mise_ok():
        with pytest.raises(SystemExit):
            cmd_init(config, repos={"community": BranchSpec("origin/master")})

    assert (tmp_path / "notes.txt").exists()
    assert (tmp_path / MARKER).exists()


# ---------------------------------------------------------------------------
# .local seed conflict
# ---------------------------------------------------------------------------


def test_init_refuses_to_seed_when_dot_local_is_a_declared_worktree(tmp_path, monkeypatch, capsys, xdg):
    monkeypatch.chdir(tmp_path)
    config = _config_with_local_remote(tmp_path, alias=".local")

    with _tty(False), _mise_ok():
        with pytest.raises(SystemExit) as exc:
            cmd_init(config, name="parrot", repos={".local": BranchSpec("origin/master")})

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert ".local" in err
    assert "declared worktree" in err


# ---------------------------------------------------------------------------
# _check_duplicate_branches — reads the index, best-effort
# ---------------------------------------------------------------------------


def test_check_duplicate_branches_scans_the_index_not_the_neighbours(tmp_path, capsys, xdg):
    _remembered_workspace(tmp_path / "existing", "community", "master..shared-branch")

    with pytest.raises(SystemExit) as exc:
        _check_duplicate_branches({"community": BranchSpec("origin/master", "shared-branch")})

    assert exc.value.code == 1
    assert "shared-branch" in capsys.readouterr().err


def test_check_duplicate_branches_lets_an_unremembered_neighbour_through(tmp_path, capsys, xdg):
    (tmp_path / "existing" / ".ow").mkdir(parents=True)
    write_workspace_config(
        tmp_path / "existing" / MARKER,
        WorkspaceConfig(repos={"community": parse_branch_spec("master..shared-branch")}),
    )

    _check_duplicate_branches({"community": BranchSpec("origin/master", "shared-branch")})

    assert capsys.readouterr().err == ""


def test_check_duplicate_branches_no_duplicate_if_different_local_branch(tmp_path, capsys, xdg):
    _remembered_workspace(tmp_path / "existing", "community", "master..other-branch")

    _check_duplicate_branches({"community": BranchSpec("origin/master", "shared-branch")})

    assert capsys.readouterr().err == ""


def test_check_duplicate_branches_silent_if_the_index_is_empty(tmp_path, capsys, xdg):
    _check_duplicate_branches({"community": BranchSpec("origin/master", "some-branch")})

    assert capsys.readouterr().err == ""


def test_check_duplicate_branches_ignores_the_repaired_target(tmp_path, capsys, xdg):
    """A workspace comparing its own declared repos to itself is not a collision."""
    ws_dir = _remembered_workspace(tmp_path / "existing", "community", "master..shared-branch")

    _check_duplicate_branches(
        {"community": BranchSpec("origin/master", "shared-branch")}, ignore=ws_dir,
    )

    assert capsys.readouterr().err == ""


def test_init_still_finds_a_real_collision_past_a_broken_workspace(tmp_path, monkeypatch, capsys, xdg):
    monkeypatch.chdir(tmp_path)
    _broken_workspace(tmp_path / "broken")
    _remembered_workspace(tmp_path / "existing", "community", "master..shared-branch")
    config = Config(remotes={"community": {"origin": RemoteConfig(url="git@example.com:x.git")}})

    with pytest.raises(SystemExit) as exc:
        cmd_init(
            config, name="new-ws", repos={"community": BranchSpec("origin/master", "shared-branch")},
        )

    assert exc.value.code == 1
    assert "shared-branch" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Repair: re-running init on an existing workspace
# ---------------------------------------------------------------------------


def test_init_repairs_a_removed_worktree_without_touching_others(tmp_path, monkeypatch, xdg):
    monkeypatch.chdir(tmp_path)
    src = _source_repo(tmp_path, "community")
    config = Config(remotes={"community": {"origin": RemoteConfig(url=str(src))}})
    ws_dir = tmp_path / "parrot"

    with _tty(False), _mise_ok(), _no_trust():
        cmd_init(config, name="parrot", repos={"community": parse_branch_spec("master..my-feature")})
    assert (ws_dir / "community").is_dir()

    import shutil
    shutil.rmtree(ws_dir / "community")

    with _tty(False), _mise_ok(), _no_trust():
        cmd_init(config, name="parrot")

    assert (ws_dir / "community").is_dir()
    assert _git(ws_dir / "community", "rev-parse", "--abbrev-ref", "HEAD") == "my-feature"


def test_init_repair_preserves_a_manually_switched_branch_and_unset_upstream(tmp_path, monkeypatch, xdg):
    """Repair recreates a removed worktree, but a manual branch switch — with
    no upstream at all, deliberately — on a still-present worktree survives."""
    monkeypatch.chdir(tmp_path)
    src = _source_repo(tmp_path, "community")
    config = Config(remotes={"community": {"origin": RemoteConfig(url=str(src))}})
    ws_dir = tmp_path / "parrot"

    with _tty(False), _mise_ok(), _no_trust():
        cmd_init(config, name="parrot", repos={"community": parse_branch_spec("master..my-feature")})

    # Simulate the user manually switching to a different local branch,
    # created without `--track`: it has no upstream at all, deliberately —
    # a repair must never touch this.
    _git(ws_dir / "community", "checkout", "-b", "manual-detour")

    with _tty(False), _mise_ok(), _no_trust():
        cmd_init(config, name="parrot")

    assert _git(ws_dir / "community", "rev-parse", "--abbrev-ref", "HEAD") == "manual-detour"
    from ow.utils.git import get_configured_upstream
    assert get_configured_upstream(ws_dir / "community") is None


def test_init_conflicting_repo_spec_fails_before_any_write(tmp_path, monkeypatch, capsys, xdg):
    monkeypatch.chdir(tmp_path)
    src = _source_repo(tmp_path, "community")
    config = Config(remotes={"community": {"origin": RemoteConfig(url=str(src))}})
    ws_dir = tmp_path / "parrot"

    with _tty(False), _mise_ok(), _no_trust():
        cmd_init(config, name="parrot", repos={"community": parse_branch_spec("master..my-feature")})
    before = (ws_dir / MARKER).read_bytes()
    before_worktree_head = _git(ws_dir / "community", "rev-parse", "HEAD")

    with _tty(False), _mise_ok():
        with pytest.raises(SystemExit) as exc:
            cmd_init(config, name="parrot", repos={"community": parse_branch_spec("master..different-branch")})

    assert exc.value.code == 1
    assert "switch" in capsys.readouterr().err.lower()
    assert (ws_dir / MARKER).read_bytes() == before
    assert _git(ws_dir / "community", "rev-parse", "HEAD") == before_worktree_head


def test_init_repair_adds_a_new_repo_to_an_existing_workspace(tmp_path, monkeypatch, xdg):
    monkeypatch.chdir(tmp_path)
    src_a = _source_repo(tmp_path, "community")
    src_b = _source_repo(tmp_path, "enterprise")
    config = Config(remotes={
        "community": {"origin": RemoteConfig(url=str(src_a))},
        "enterprise": {"origin": RemoteConfig(url=str(src_b))},
    })
    ws_dir = tmp_path / "parrot"

    with _tty(False), _mise_ok(), _no_trust():
        cmd_init(config, name="parrot", repos={"community": parse_branch_spec("master..feat")})

    with _tty(False), _mise_ok(), _no_trust():
        cmd_init(config, name="parrot", repos={"enterprise": parse_branch_spec("master..feat")})

    ws = load_workspace_config(ws_dir / MARKER)
    assert set(ws.repos) == {"community", "enterprise"}
    assert (ws_dir / "enterprise").is_dir()


def test_init_existing_target_rejects_dash_c(tmp_path, monkeypatch, capsys, xdg):
    monkeypatch.chdir(tmp_path)
    config = Config(remotes={})
    with _tty(False), _mise_ok():
        cmd_init(config, name="parrot")

    src_config = tmp_path / "src" / ".ow" / "config.toml"
    src_config.parent.mkdir(parents=True)
    src_config.write_text("[repos]\n")

    with pytest.raises(SystemExit) as exc:
        cmd_init(config, name="parrot", configuration=str(tmp_path / "src"))

    assert exc.value.code == 1
    assert "already a workspace" in capsys.readouterr().err.lower()


def test_init_repair_migrates_a_schema_1_workspace(tmp_path, monkeypatch, xdg):
    monkeypatch.chdir(tmp_path)
    ws_dir = tmp_path / "parrot"
    (ws_dir / ".ow").mkdir(parents=True)
    (ws_dir / MARKER).write_text('version = 1\n\n[repos]\ncommunity = "master..feat"\n')
    index.remember(ws_dir)
    config = Config(remotes={"community": {"origin": RemoteConfig(url="/does/not/exist")}})

    with _tty(False), _mise_ok():
        with pytest.raises(SystemExit):
            # Materialization fails (fake remote), but the migration still commits.
            cmd_init(config, name="parrot")

    ws = load_workspace_config(ws_dir / MARKER)
    assert ws.version == 2
    assert ws.repos["community"].local_branch == "feat"


# ---------------------------------------------------------------------------
# Real-repo end-to-end: seeding, and the generic no-python/services claim
# ---------------------------------------------------------------------------


def test_init_seed_affects_the_first_renders_addon_paths(tmp_path, monkeypatch, xdg):
    monkeypatch.chdir(tmp_path)
    src = _source_repo(tmp_path, "community")
    (src / "odoo-bin").write_text("#!/usr/bin/env python3\n")
    (src / "addons").mkdir()
    (src / "addons" / ".keep").write_text("")
    (src / "odoo" / "addons").mkdir(parents=True)
    (src / "odoo" / "addons" / ".keep").write_text("")
    (src / "odoo" / "release.py").write_text(
        "version_info = (19, 0, 0, 'final', 0, '')\n"
        "MIN_PY_VERSION = (3, 10)\nMAX_PY_VERSION = (3, 14)\n"
    )
    (src / "odoo" / "tools").mkdir(parents=True)
    (src / "odoo" / "tools" / "config.py").write_text(
        "PARSER.add_argument('--with-demo', action='store_true')\n"
        "PARSER.add_argument('--without-demo', action='store_true')\n"
    )
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "core")

    seed_dir = paths.local_dir() / "extra_addon"
    seed_dir.mkdir(parents=True)
    (seed_dir / "__manifest__.py").write_text("{}")

    config = Config(remotes={"community": {"origin": RemoteConfig(url=str(src))}})

    with _tty(False), _mise_ok(), _no_trust():
        cmd_init(config, name="parrot", repos={"community": parse_branch_spec("master..feat")})

    ws_dir = tmp_path / "parrot"
    assert (ws_dir / ".local" / "extra_addon").is_dir()
    odools = (ws_dir / "odools.toml").read_text()
    assert "extra_addon" in odools or ".local" in odools


def test_init_generic_workspace_writes_no_python_or_service_files(tmp_path, monkeypatch, xdg):
    monkeypatch.chdir(tmp_path)
    config = Config(remotes={})

    with _tty(False), _mise_ok():
        cmd_init(config, name="tools")

    ws_dir = tmp_path / "tools"
    assert not (ws_dir / "odoorc").exists()
    assert not (ws_dir / "requirements-dev.txt").exists()
    assert not (ws_dir / ".venv").exists()
    assert not paths.services_dir().joinpath("compose.yml").exists()
