import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ow.commands import cmd_create, cmd_prune, cmd_rebase, cmd_status, cmd_update
from ow.utils.config import BranchSpec, Config, load_workspace_config, WorkspaceConfig, parse_branch_spec, write_workspace_config

def _make_config(
    root_dir=None,
    vars=None,
    remotes=None,
) -> Config:
    return Config(
        vars=vars
        if vars is not None
        else {"http_port": 8069, "db_host": "localhost", "db_port": 5432},
        remotes=remotes or {},
        root_dir=root_dir or Path("/root"),
    )


def write_ow_config(ws_dir: Path, templates: list[str], repos: dict[str, str], vars: dict | None = None) -> None:
    ws = WorkspaceConfig(
        repos={alias: parse_branch_spec(spec) for alias, spec in repos.items()},
        templates=templates,
        vars=vars or {},
    )
    write_workspace_config(ws_dir / ".ow" / "config", ws)


def _make_subprocess_mock(
    *,
    rebase_fail_on: list[str] | None = None,
    track_calls: dict[str, list] | None = None,
) -> Any:
    failed_rebases: set[str] = set()

    def side_effect(args, **kwargs):
        mock = MagicMock(returncode=0)
        mock.stdout = "0\t0\n"
        if track_calls is not None:
            if "rebase" in args and "rebase" in track_calls:
                track_calls["rebase"].append(args[-1])
            if "switch" in args and "switch" in track_calls:
                track_calls["switch"].append(list(args))
        if rebase_fail_on is not None and "rebase" in args:
            worktree = args[2] if len(args) > 2 else None
            if worktree in rebase_fail_on and worktree not in failed_rebases:
                failed_rebases.add(worktree)
                mock.returncode = 1
        return mock

    return side_effect


def _mock_parallel_exec(tasks):
    return {k: fn() for k, fn in tasks.items()}


def _mock_fetch(tracks, upstreams, specs):
    def _mock(ws, wsdir, config, **kw):
        return (tracks, upstreams, specs)
    return _mock


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------

def test_cmd_status_drift_warns(tmp_path, capsys):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (tmp_path / ".bare-git-repos" / "community.git").mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master..my-feature"})
    config = _make_config(root_dir=tmp_path)

    resolved_spec = BranchSpec("origin/master")
    fetch_return = ({"community": "origin/master"}, {}, {"community": resolved_spec})

    with (
        patch("ow.utils.drift.get_worktree_branch", return_value="wrong-branch"),
        patch("ow.utils.drift.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.refs.fetch_workspace_refs", return_value=fetch_return),
        patch("ow.commands.status.get_all_remote_refs", return_value={"origin/master"}),
        patch("ow.commands.status._gather_repo_status", return_value=MagicMock(
            status_line="        community: origin/master", first_attached_branch=None, github_link=None,
        )),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_status(config)

    captured = capsys.readouterr()
    assert "Warning" in captured.err


def test_cmd_status_fetches_before_display(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (tmp_path / ".bare-git-repos" / "community.git").mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master"})
    config = _make_config(root_dir=tmp_path)

    fetch_called = [False]
    resolved_spec = BranchSpec("origin/master")

    def mock_fetch(*a, **kw):
        fetch_called[0] = True
        return ({"community": "origin/master"}, {}, {"community": resolved_spec})

    with (
        patch("ow.utils.drift.get_worktree_branch", return_value=None),
        patch("ow.utils.drift.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.commands.status.fetch_workspace_refs", side_effect=mock_fetch),
        patch("ow.commands.status.get_all_remote_refs", return_value={"origin/master"}),
        patch("ow.commands.status._gather_repo_status", return_value=MagicMock(
            status_line="        community: origin/master", first_attached_branch=None, github_link=None,
        )),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_status(config)

    assert fetch_called[0]


# ---------------------------------------------------------------------------
# cmd_rebase
# ---------------------------------------------------------------------------

def test_cmd_rebase_drift_warns(tmp_path, capsys):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (tmp_path / ".bare-git-repos" / "community.git").mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master..my-feature"})
    config = _make_config(root_dir=tmp_path)

    resolved_spec = BranchSpec("origin/master")
    fetch_return = ({"community": "origin/master"}, {}, {"community": resolved_spec})

    with (
        patch("ow.utils.drift.get_worktree_branch", return_value="wrong-branch"),
        patch("ow.utils.drift.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.refs.fetch_workspace_refs", return_value=fetch_return),
        patch("ow.commands.rebase.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.git.subprocess.run", side_effect=_make_subprocess_mock()),
        patch("builtins.input", return_value=""),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_rebase(config)

    captured = capsys.readouterr()
    assert "Warning" in captured.err


def test_cmd_rebase_detached_switches(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (tmp_path / ".bare-git-repos" / "community.git").mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master"})
    config = _make_config(root_dir=tmp_path)

    switch_calls: list = []
    resolved_spec = BranchSpec("origin/master")
    fetch_return = ({"community": "origin/master"}, {}, {"community": resolved_spec})
    mock_sub = _make_subprocess_mock(track_calls={"switch": switch_calls})

    with (
        patch("ow.utils.drift.get_worktree_branch", return_value=None),
        patch("ow.utils.drift.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.refs.fetch_workspace_refs", return_value=fetch_return),
        patch("ow.commands.rebase.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.git.subprocess.run", side_effect=mock_sub),
        patch("builtins.input", return_value=""),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_rebase(config)

    assert any("--detach" in c for c in switch_calls)


def test_cmd_rebase_two_step_rebase(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (tmp_path / ".bare-git-repos" / "community.git").mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master..my-feature"})
    config = _make_config(root_dir=tmp_path)

    rebase_targets: list = []
    track_run = _make_subprocess_mock(track_calls={"rebase": rebase_targets})

    def mock_spec(bare_repo, spec, remotes):
        if spec.local_branch == "my-feature":
            return BranchSpec("dev/my-feature", "my-feature")
        return BranchSpec("origin/master")

    fetch_return = (
        {"community": "dev/my-feature"},
        {"community": "origin/master"},
        {"community": BranchSpec("dev/my-feature", "my-feature")},
    )

    with (
        patch("ow.utils.drift.get_worktree_branch", return_value="my-feature"),
        patch("ow.utils.drift.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.refs.fetch_workspace_refs", return_value=fetch_return),
        patch("ow.commands.rebase.resolve_spec", side_effect=mock_spec),
        patch("ow.commands.rebase.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.git.subprocess.run", side_effect=track_run),
        patch("builtins.input", return_value=""),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_rebase(config)

    assert rebase_targets == ["dev/my-feature", "origin/master"]


def test_cmd_rebase_conflict_reports_and_continues(tmp_path, capsys):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (ws_dir / "enterprise").mkdir(parents=True)
    (tmp_path / ".bare-git-repos" / "community.git").mkdir(parents=True)
    (tmp_path / ".bare-git-repos" / "enterprise.git").mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {
        "community": "master..my-feature",
        "enterprise": "master..my-feature",
    })
    config = _make_config(root_dir=tmp_path)

    community_path = str(ws_dir / "community")
    track_run = _make_subprocess_mock(rebase_fail_on=[community_path])

    def mock_spec(bare_repo, spec, remotes):
        return BranchSpec("origin/master", spec.local_branch)

    spec = BranchSpec("origin/master", "my-feature")
    fetch_return = (
        {"community": "origin/master", "enterprise": "origin/master"},
        {"community": "origin/master", "enterprise": "origin/master"},
        {"community": spec, "enterprise": spec},
    )

    with (
        patch("ow.utils.drift.get_worktree_branch", return_value="my-feature"),
        patch("ow.utils.drift.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.refs.fetch_workspace_refs", return_value=fetch_return),
        patch("ow.commands.rebase.resolve_spec", side_effect=mock_spec),
        patch("ow.commands.rebase.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.git.subprocess.run", side_effect=track_run),
        patch("builtins.input", return_value=""),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        with pytest.raises(SystemExit):
            cmd_rebase(config)

    captured = capsys.readouterr()
    assert "CONFLICT" in captured.err


def test_cmd_rebase_no_upstream_when_not_pushed(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (tmp_path / ".bare-git-repos" / "community.git").mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master..my-feature"})
    config = _make_config(root_dir=tmp_path)

    rebase_targets: list = []
    track_run = _make_subprocess_mock(track_calls={"rebase": rebase_targets})

    def mock_spec(bare_repo, spec, remotes):
        return BranchSpec("origin/master", spec.local_branch)

    spec = BranchSpec("origin/master", "my-feature")
    fetch_return = ({"community": "origin/master"}, {}, {"community": spec})

    with (
        patch("ow.utils.drift.get_worktree_branch", return_value="my-feature"),
        patch("ow.utils.drift.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.refs.fetch_workspace_refs", return_value=fetch_return),
        patch("ow.commands.rebase.resolve_spec", side_effect=mock_spec),
        patch("ow.commands.rebase.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.git.subprocess.run", side_effect=track_run),
        patch("builtins.input", return_value=""),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_rebase(config)

    assert rebase_targets == ["origin/master"]


# ---------------------------------------------------------------------------
# cmd_create with CLI args
# ---------------------------------------------------------------------------

def test_cmd_create_with_cli_args(tmp_path, config_with_remotes):
    (tmp_path / "templates" / "common").mkdir(parents=True)
    (tmp_path / "templates" / "vscode").mkdir(parents=True)
    config = config_with_remotes

    text_calls = []
    def mock_text(message):
        text_calls.append(message)
        mock = MagicMock()
        mock.ask.return_value = ""
        return mock

    def mock_checkbox(message, choices=None, **kwargs):
        mock = MagicMock()
        mock.ask.return_value = ["common"] if "Templates" in message else ["community"]
        return mock

    def mock_confirm(message):
        mock = MagicMock()
        mock.ask.return_value = True
        return mock

    with (
        patch("questionary.checkbox", side_effect=mock_checkbox),
        patch("questionary.text", side_effect=mock_text),
        patch("questionary.confirm", side_effect=mock_confirm),
        patch("ow.commands.create.ensure_workspace_materialized", return_value=(tmp_path / "workspaces" / "my-ws", {"community"}, {})),
        patch("ow.commands.create.apply_templates"),
        patch("ow.commands.create.write_workspace_config"),
        patch("ow.commands.create.run_cmd"),
    ):
        cmd_create(
            config,
            name="my-ws",
            templates=["common"],
            repos={"community": BranchSpec("origin/master", "master-my-ws")},
        )

    assert not any("Workspace name" in m for m in text_calls)
    assert not any("branch spec" in m for m in text_calls)


def test_cmd_create_rejects_invalid_template(tmp_path, capsys, config):
    (tmp_path / "templates" / "common").mkdir(parents=True)
    with pytest.raises(SystemExit) as exc:
        cmd_create(config, name="test", templates=["nonexistent"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "unknown template" in captured.err.lower()
    assert "common" in captured.err


def test_cmd_create_rejects_invalid_repo_alias(tmp_path, capsys, config_with_remotes):
    (tmp_path / "templates" / "common").mkdir(parents=True)
    config = config_with_remotes
    with pytest.raises(SystemExit) as exc:
        cmd_create(config, name="test", repos={"unknown": BranchSpec("origin/master")})
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "unknown repo alias" in captured.err.lower()
    assert "community" in captured.err


def test_cmd_create_rejects_existing_workspace(tmp_path, capsys, config):
    (tmp_path / "templates" / "common").mkdir(parents=True)
    (tmp_path / "workspaces" / "parrot").mkdir(parents=True)
    with pytest.raises(SystemExit) as exc:
        cmd_create(config, name="parrot")
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "already exists" in captured.err.lower()


def test_cmd_create_rejects_invalid_name(tmp_path, capsys, config):
    (tmp_path / "templates" / "common").mkdir(parents=True)
    with pytest.raises(SystemExit) as exc:
        cmd_create(config, name="bad name!")
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "alphanumeric" in captured.err.lower()


def test_cmd_create_rejects_duplicate_branch(tmp_path, capsys, config_with_remotes):
    (tmp_path / "templates" / "common").mkdir(parents=True)
    existing_ws = tmp_path / "workspaces" / "parrot"
    existing_ws.mkdir(parents=True)
    ow_config = existing_ws / ".ow" / "config"
    ow_config.parent.mkdir(parents=True)
    ow_config.write_text('templates = ["common"]\n\n[repos]\ncommunity = "master..master-parrot"\n')
    config = config_with_remotes

    def mock_checkbox(message, choices=None, **kwargs):
        mock = MagicMock()
        mock.ask.return_value = ["common"] if "Templates" in message else ["community"]
        return mock

    def mock_text(message):
        mock = MagicMock()
        mock.ask.return_value = ""
        return mock

    with (
        patch("questionary.checkbox", side_effect=mock_checkbox),
        patch("questionary.text", side_effect=mock_text),
    ):
        with pytest.raises(SystemExit) as exc:
            cmd_create(config, name="new-ws", repos={"community": BranchSpec("origin/master", "master-parrot")})

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "already uses" in captured.err.lower()
    assert "master-parrot" in captured.err


def test_cmd_create_accepts_different_branch(tmp_path, config_with_remotes):
    (tmp_path / "templates" / "common").mkdir(parents=True)
    existing_ws = tmp_path / "workspaces" / "parrot"
    existing_ws.mkdir(parents=True)
    ow_config = existing_ws / ".ow" / "config"
    ow_config.parent.mkdir(parents=True)
    ow_config.write_text('templates = ["common"]\n\n[repos]\ncommunity = "master..master-parrot"\n')
    config = config_with_remotes

    with (
        patch("questionary.checkbox", side_effect=lambda *a, **kw: MagicMock(ask=lambda: ["common"] if "Templates" in kw.get("message", "") else ["community"])),
        patch("questionary.text", return_value=MagicMock(ask=lambda: "")),
        patch("questionary.confirm", return_value=MagicMock(ask=lambda: True)),
        patch("ow.commands.create.ensure_workspace_materialized", return_value=(tmp_path / "workspaces" / "new-ws", {"community"}, {})),
        patch("ow.commands.create.apply_templates") as mock_apply,
        patch("ow.commands.create.write_workspace_config") as mock_write,
        patch("ow.commands.create.run_cmd"),
    ):
        cmd_create(config, name="new-ws", repos={"community": BranchSpec("origin/master", "master-new")})

    mock_apply.assert_called_once()
    mock_write.assert_called_once()


def test_cmd_create_configuration_duplicates(tmp_path, config_with_remotes):
    (tmp_path / "templates" / "common").mkdir(parents=True)
    (tmp_path / "templates" / "vscode").mkdir(parents=True)
    src_ws = tmp_path / "workspaces" / "parrot"
    src_ws.mkdir(parents=True)
    ow_config = src_ws / ".ow" / "config"
    ow_config.parent.mkdir(parents=True)
    ow_config.write_text(
        'templates = ["common", "vscode"]\n\n'
        '[repos]\ncommunity = "master..master-parrot"\n\n'
        '[vars]\nhttp_port = 9000\n'
    )
    config = _make_config(
        root_dir=tmp_path,
        vars={"http_port": 8069},
        remotes=config_with_remotes.remotes,
    )

    checkbox_calls = []
    def mock_checkbox(message, choices=None, **kwargs):
        checkbox_calls.append({"message": message, "choices": choices})
        mock = MagicMock()
        mock.ask.return_value = ["common", "vscode"] if "Templates" in message else ["community"]
        return mock

    def mock_text(message):
        mock = MagicMock()
        mock.ask.return_value = ""
        return mock

    def mock_confirm(message):
        mock = MagicMock()
        mock.ask.return_value = True
        return mock

    with (
        patch("questionary.checkbox", side_effect=mock_checkbox),
        patch("questionary.text", side_effect=mock_text),
        patch("questionary.confirm", side_effect=mock_confirm),
        patch("ow.commands.create.ensure_workspace_materialized", return_value=(tmp_path / "workspaces" / "new-ws", {"community"}, {})),
        patch("ow.commands.create.apply_templates"),
        patch("ow.commands.create.write_workspace_config"),
        patch("ow.commands.create.run_cmd"),
    ):
        cmd_create(config, name="new-ws", repos={"community": BranchSpec("origin/master", "master-new")}, configuration=str(src_ws))

    template_checkbox = checkbox_calls[0]
    checked = [c.title for c in template_checkbox["choices"] if c.checked]
    assert "common" in checked
    assert "vscode" in checked

    repo_checkbox = checkbox_calls[1]
    checked = [c.title for c in repo_checkbox["choices"] if c.checked]
    assert "community" in checked


def test_cmd_create_configuration_rejects_unknown_remote(tmp_path, capsys):
    (tmp_path / "templates" / "common").mkdir(parents=True)
    src_ws = tmp_path / "workspaces" / "parrot"
    src_ws.mkdir(parents=True)
    ow_config = src_ws / ".ow" / "config"
    ow_config.parent.mkdir(parents=True)
    ow_config.write_text(
        'templates = ["common"]\n\n'
        '[repos]\ncommunity = "master"\nenterprise = "master"\n'
    )
    config = _make_config(
        root_dir=tmp_path,
        remotes={"community": {"origin": MagicMock(url="git@github.com:odoo/odoo.git")}},
    )

    with pytest.raises(SystemExit) as exc:
        cmd_create(config, name="new-ws", configuration=str(src_ws))

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "enterprise" in captured.err.lower()
    assert "not defined" in captured.err.lower()
    assert "community" in captured.err


# ---------------------------------------------------------------------------
# cmd_update
# ---------------------------------------------------------------------------

def test_cmd_update_renders_templates_and_materializes(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master"})

    with (
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
        patch("ow.commands.update.ensure_workspace_materialized", return_value=(ws_dir, {"community"}, {})) as mock_mat,
        patch("ow.commands.update.apply_templates") as mock_apply,
    ):
        cmd_update(config)

    mock_mat.assert_called_once()
    mock_apply.assert_called_once()


def test_cmd_update_merges_missing_vars(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master"}, vars={"http_port": 9090})
    config.vars = {"http_port": 8069, "db_host": "localhost"}

    with (
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
        patch("ow.commands.update.ensure_workspace_materialized", return_value=(ws_dir, {"community"}, {})),
        patch("ow.commands.update.apply_templates"),
    ):
        cmd_update(config)

    updated = load_workspace_config(ws_dir / ".ow" / "config")
    assert updated.vars["http_port"] == 9090
    assert updated.vars["db_host"] == "localhost"


def test_cmd_update_preserves_existing_vars(tmp_path, config):
    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master"}, vars={"http_port": 9090})
    config.vars = {"http_port": 8069}

    with (
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
        patch("ow.commands.update.ensure_workspace_materialized", return_value=(ws_dir, {"community"}, {})),
        patch("ow.commands.update.apply_templates"),
    ):
        cmd_update(config)

    updated = load_workspace_config(ws_dir / ".ow" / "config")
    assert updated.vars["http_port"] == 9090


def test_cmd_prune_no_bare_repos(tmp_path, capsys, config):
    cmd_prune(config)
    captured = capsys.readouterr()
    assert "No bare repos found" in captured.out


def test_cmd_prune_cleans_repos(tmp_path, capsys, config):
    bare_dir = tmp_path / ".bare-git-repos"
    bare_dir.mkdir()
    (bare_dir / "community.git").mkdir()
    (bare_dir / "enterprise.git").mkdir()

    with patch("ow.commands.prune.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        cmd_prune(config)

    assert mock_run.call_count >= 6
    calls = mock_run.call_args_list
    all_args = " ".join(str(c) for c in calls)
    assert "community" in all_args
    assert "enterprise" in all_args
    prune_calls = [c for c in calls if c[0][0][3:5] == ["worktree", "prune"]]
    assert len(prune_calls) == 2
