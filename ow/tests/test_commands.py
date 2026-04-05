import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from ow.config import BranchSpec, Config
from ow.workspace import cmd_create, cmd_rebase, cmd_status, cmd_update


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
    """Write a .ow/config file in the workspace directory using the real writer."""
    from ow.config import WorkspaceConfig, write_workspace_config, parse_branch_spec

    ws = WorkspaceConfig(
        repos={alias: parse_branch_spec(spec) for alias, spec in repos.items()},
        templates=templates,
        vars=vars or {},
    )
    ow_config = ws_dir / ".ow" / "config"
    write_workspace_config(ow_config, ws)


def _make_subprocess_mock(
    *,
    rebase_fail_on: list[str] | None = None,
    track_calls: dict[str, list] | None = None,
) -> Any:
    """Create a unified subprocess.run mock that works across both ow.workspace and ow.git.

    Args:
        rebase_fail_on: list of worktree path strings; rebase fails (returncode=1)
                        when the worktree path (args[2]) matches one of these.
                        Each worktree fails only once (tracked internally).
        track_calls: optional dict with keys like "rebase", "switch", "fetch";
                     matching calls get appended to the corresponding list.
    """
    failed_rebases: set[str] = set()

    def side_effect(args, **kwargs):
        mock = MagicMock(returncode=0)
        mock.stdout = "0\t0\n"

        if track_calls is not None:
            if "rebase" in args and "rebase" in track_calls:
                track_calls["rebase"].append(args[-1])
            if "switch" in args and "switch" in track_calls:
                track_calls["switch"].append(list(args))
            if "fetch" in args and "fetch" in track_calls:
                track_calls["fetch"].append(list(args))

        if rebase_fail_on is not None and "rebase" in args:
            worktree = args[2] if len(args) > 2 else None
            if worktree in rebase_fail_on and worktree not in failed_rebases:
                failed_rebases.add(worktree)
                mock.returncode = 1

        return mock

    return side_effect


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------

def test_cmd_status_drift_warns(tmp_path, capsys):
    """cmd_status warns when drift is detected but continues."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    bare_repos_dir = tmp_path / ".bare-git-repos"
    bare_repo = bare_repos_dir / "community.git"
    bare_repo.mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master..my-feature"})
    config = _make_config(root_dir=tmp_path)

    track_run = _make_subprocess_mock()

    with (
        patch("ow.workspace.get_worktree_branch", return_value="wrong-branch"),
        patch("ow.workspace.resolve_spec_local", return_value=BranchSpec("origin/master")),
        patch("ow.workspace.subprocess.run", side_effect=track_run),
        patch("ow.git.subprocess.run", side_effect=track_run),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_status(config)

    captured = capsys.readouterr()
    assert "Warning" in captured.err


def test_cmd_status_fetches_before_display(tmp_path):
    """cmd_status fetches track branch before displaying status."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    bare_repos_dir = tmp_path / ".bare-git-repos"
    bare_repo = bare_repos_dir / "community.git"
    bare_repo.mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master"})
    config = _make_config(root_dir=tmp_path)

    fetch_calls: list = []
    track_run = _make_subprocess_mock(track_calls={"fetch": fetch_calls})

    with (
        patch("ow.workspace.get_worktree_branch", return_value=None),  # detached = no drift
        patch("ow.workspace.resolve_spec_local", return_value=BranchSpec("origin/master")),
        patch("ow.workspace.subprocess.run", side_effect=track_run),
        patch("ow.git.subprocess.run", side_effect=track_run),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_status(config)

    assert any("fetch" in c for c in fetch_calls)


# ---------------------------------------------------------------------------
# cmd_rebase
# ---------------------------------------------------------------------------

def test_cmd_rebase_drift_warns(tmp_path, capsys):
    """cmd_rebase warns when drift is detected but continues."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    bare_repos_dir = tmp_path / ".bare-git-repos"
    bare_repo = bare_repos_dir / "community.git"
    bare_repo.mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master..my-feature"})
    config = _make_config(root_dir=tmp_path)

    track_run = _make_subprocess_mock()

    with (
        patch("ow.workspace.get_worktree_branch", return_value="wrong-branch"),
        patch("ow.workspace.resolve_spec", return_value=BranchSpec("origin/master")),
        patch("ow.git.subprocess.run", side_effect=track_run),
        patch("ow.workspace.subprocess.run", side_effect=track_run),
        patch("builtins.input", return_value=""),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_rebase(config)

    captured = capsys.readouterr()
    assert "Warning" in captured.err


def test_cmd_rebase_detached_switches(tmp_path):
    """Detached repos get switch --detach to latest track ref."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    bare_repos_dir = tmp_path / ".bare-git-repos"
    bare_repo = bare_repos_dir / "community.git"
    bare_repo.mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master"})
    config = _make_config(root_dir=tmp_path)

    switch_calls: list = []
    track_run = _make_subprocess_mock(track_calls={"switch": switch_calls})

    with (
        patch("ow.workspace.get_worktree_branch", return_value=None),
        patch("ow.workspace.resolve_spec", return_value=BranchSpec("origin/master")),
        patch("ow.git.subprocess.run", side_effect=track_run),
        patch("ow.workspace.subprocess.run", side_effect=track_run),
        patch("builtins.input", return_value=""),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_rebase(config)

    assert any("--detach" in c for c in switch_calls)


def test_cmd_rebase_two_step_rebase(tmp_path):
    """When work branch is pushed to a remote, rebase onto both upstream and track."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    bare_repos_dir = tmp_path / ".bare-git-repos"
    bare_repo = bare_repos_dir / "community.git"
    bare_repo.mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master..my-feature"})
    config = _make_config(root_dir=tmp_path)

    rebase_targets: list = []
    track_run = _make_subprocess_mock(track_calls={"rebase": rebase_targets})

    # Return a fixed resolved spec: the work branch IS pushed to dev remote
    # so the track ref is dev/my-feature, upstream is origin/master
    def mock_resolve(bare_repo, spec, remotes):
        if spec.local_branch == "my-feature":
            return BranchSpec("dev/my-feature", "my-feature")
        return BranchSpec("origin/master")

    with (
        patch("ow.workspace.get_worktree_branch", return_value="my-feature"),
        patch("ow.workspace.resolve_spec", side_effect=mock_resolve),
        patch("ow.git.subprocess.run", side_effect=track_run),
        patch("ow.workspace.subprocess.run", side_effect=track_run),
        patch("builtins.input", return_value=""),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_rebase(config)

    assert rebase_targets == ["dev/my-feature", "origin/master"]


def test_cmd_rebase_conflict_reports_and_continues(tmp_path, capsys):
    """On conflict, report and continue to other repos."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (ws_dir / "enterprise").mkdir(parents=True)
    bare_repos_dir = tmp_path / ".bare-git-repos"
    (bare_repos_dir / "community.git").mkdir(parents=True)
    (bare_repos_dir / "enterprise.git").mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {
        "community": "master..my-feature",
        "enterprise": "master..my-feature",
    })
    config = _make_config(root_dir=tmp_path)

    community_path = str(ws_dir / "community")
    track_run = _make_subprocess_mock(rebase_fail_on=[community_path])

    def mock_resolve(bare_repo, spec, remotes):
        return BranchSpec("origin/master", spec.local_branch)

    with (
        patch("ow.workspace.get_worktree_branch", return_value="my-feature"),
        patch("ow.workspace.resolve_spec", side_effect=mock_resolve),
        patch("ow.git.subprocess.run", side_effect=track_run),
        patch("ow.workspace.subprocess.run", side_effect=track_run),
        patch("builtins.input", return_value=""),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        with pytest.raises(SystemExit):
            cmd_rebase(config)

    captured = capsys.readouterr()
    assert "CONFLICT" in captured.err


def test_cmd_rebase_no_upstream_when_not_pushed(tmp_path):
    """When work branch is not on any remote, only rebase onto track."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    bare_repos_dir = tmp_path / ".bare-git-repos"
    bare_repo = bare_repos_dir / "community.git"
    bare_repo.mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master..my-feature"})
    config = _make_config(root_dir=tmp_path)

    rebase_targets: list = []
    track_run = _make_subprocess_mock(track_calls={"rebase": rebase_targets})

    def mock_resolve(bare_repo, spec, remotes):
        return BranchSpec("origin/master", spec.local_branch)

    with (
        patch("ow.workspace.get_worktree_branch", return_value="my-feature"),
        patch("ow.workspace.resolve_spec", side_effect=mock_resolve),
        patch("ow.git.subprocess.run", side_effect=track_run),
        patch("ow.workspace.subprocess.run", side_effect=track_run),
        patch("builtins.input", return_value=""),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_rebase(config)

    assert rebase_targets == ["origin/master"]


# ---------------------------------------------------------------------------
# cmd_create with CLI args
# ---------------------------------------------------------------------------


def test_cmd_create_with_cli_args(tmp_path, config_with_remotes):
    """cmd_create accepts pre-populated CLI args and skips those questions."""
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
        if "Templates" in message:
            mock.ask.return_value = ["common"]
        else:
            mock.ask.return_value = ["community"]
        return mock

    def mock_confirm(message):
        mock = MagicMock()
        mock.ask.return_value = True
        return mock

    with (
        patch("ow.workspace.questionary.checkbox", side_effect=mock_checkbox),
        patch("ow.workspace.questionary.text", side_effect=mock_text),
        patch("ow.workspace.questionary.confirm", side_effect=mock_confirm),
        patch("ow.workspace._ensure_workspace_materialized", return_value=(tmp_path / "workspaces" / "my-ws", {"community"}, {})),
        patch("ow.workspace._apply_templates"),
        patch("ow.workspace.write_workspace_config"),
        patch("ow.workspace.run_cmd"),
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
    """cmd_create exits with error for unknown template."""
    (tmp_path / "templates" / "common").mkdir(parents=True)

    with pytest.raises(SystemExit) as exc:
        cmd_create(config, name="test", templates=["nonexistent"])

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "unknown template" in captured.err.lower()
    assert "common" in captured.err


def test_cmd_create_rejects_invalid_repo_alias(tmp_path, capsys, config_with_remotes):
    """cmd_create exits with error for unknown repo alias."""
    (tmp_path / "templates" / "common").mkdir(parents=True)
    config = config_with_remotes

    with pytest.raises(SystemExit) as exc:
        cmd_create(config, name="test", repos={"unknown": BranchSpec("origin/master")})

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "unknown repo alias" in captured.err.lower()
    assert "community" in captured.err


def test_cmd_create_rejects_existing_workspace(tmp_path, capsys, config):
    """cmd_create exits with error when workspace name already exists (CLI arg)."""
    (tmp_path / "templates" / "common").mkdir(parents=True)
    (tmp_path / "workspaces" / "parrot").mkdir(parents=True)

    with pytest.raises(SystemExit) as exc:
        cmd_create(config, name="parrot")

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "already exists" in captured.err.lower()


def test_cmd_create_rejects_invalid_name(tmp_path, capsys, config):
    """cmd_create exits with error for invalid name from CLI arg."""
    (tmp_path / "templates" / "common").mkdir(parents=True)

    with pytest.raises(SystemExit) as exc:
        cmd_create(config, name="bad name!")

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "alphanumeric" in captured.err.lower()


def test_cmd_create_rejects_duplicate_branch(tmp_path, capsys, config_with_remotes):
    """cmd_create exits with error when target branch is already in use by another workspace."""
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
        patch("ow.workspace.questionary.checkbox", side_effect=mock_checkbox),
        patch("ow.workspace.questionary.text", side_effect=mock_text),
    ):
        with pytest.raises(SystemExit) as exc:
            cmd_create(config, name="new-ws", repos={"community": BranchSpec("origin/master", "master-parrot")})

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "already uses" in captured.err.lower()
    assert "master-parrot" in captured.err


def test_cmd_create_accepts_different_branch(tmp_path, config_with_remotes):
    """cmd_create succeeds when target branch is unique."""
    (tmp_path / "templates" / "common").mkdir(parents=True)
    existing_ws = tmp_path / "workspaces" / "parrot"
    existing_ws.mkdir(parents=True)
    ow_config = existing_ws / ".ow" / "config"
    ow_config.parent.mkdir(parents=True)
    ow_config.write_text('templates = ["common"]\n\n[repos]\ncommunity = "master..master-parrot"\n')

    config = config_with_remotes

    with (
        patch("ow.workspace.questionary.checkbox", side_effect=lambda *a, **kw: MagicMock(ask=lambda: ["common"] if "Templates" in kw.get("message", "") else ["community"])),
        patch("ow.workspace.questionary.text", return_value=MagicMock(ask=lambda: "")),
        patch("ow.workspace.questionary.confirm", return_value=MagicMock(ask=lambda: True)),
        patch("ow.workspace._ensure_workspace_materialized", return_value=(tmp_path / "workspaces" / "new-ws", {"community"}, {})),
        patch("ow.workspace._apply_templates") as mock_apply,
        patch("ow.workspace.write_workspace_config") as mock_write,
        patch("ow.workspace.run_cmd"),
    ):
        cmd_create(config, name="new-ws", repos={"community": BranchSpec("origin/master", "master-new")})

    mock_apply.assert_called_once()
    mock_write.assert_called_once()


def test_cmd_create_configuration_duplicates(tmp_path, config_with_remotes):
    """cmd_create -c duplicates source workspace config."""
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
        patch("ow.workspace.questionary.checkbox", side_effect=mock_checkbox),
        patch("ow.workspace.questionary.text", side_effect=mock_text),
        patch("ow.workspace.questionary.confirm", side_effect=mock_confirm),
        patch("ow.workspace._ensure_workspace_materialized", return_value=(tmp_path / "workspaces" / "new-ws", {"community"}, {})),
        patch("ow.workspace._apply_templates"),
        patch("ow.workspace.write_workspace_config"),
        patch("ow.workspace.run_cmd"),
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
    """cmd_create -c exits with error if source config references a repo not in ow.toml."""
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
    """cmd_update calls _apply_templates and _ensure_workspace_materialized."""
    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master"})

    with (
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
        patch("ow.workspace._ensure_workspace_materialized", return_value=(ws_dir, {"community"}, {})) as mock_mat,
        patch("ow.workspace._apply_templates") as mock_apply,
    ):
        from ow.workspace import cmd_update
        cmd_update(config)

    mock_mat.assert_called_once()
    mock_apply.assert_called_once()


def test_cmd_update_merges_missing_vars(tmp_path, config):
    """cmd_update adds missing vars from ow.toml to .ow/config."""
    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master"}, vars={"http_port": 9090})
    config.vars = {"http_port": 8069, "db_host": "localhost"}

    with (
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
        patch("ow.workspace._ensure_workspace_materialized", return_value=(ws_dir, {"community"}, {})),
        patch("ow.workspace._apply_templates"),
    ):
        from ow.workspace import cmd_update
        cmd_update(config)

    from ow.config import load_workspace_config
    updated = load_workspace_config(ws_dir / ".ow" / "config")
    assert updated.vars["http_port"] == 9090
    assert updated.vars["db_host"] == "localhost"


def test_cmd_update_preserves_existing_vars(tmp_path, config):
    """cmd_update does not overwrite existing workspace var overrides."""
    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master"}, vars={"http_port": 9090})
    config.vars = {"http_port": 8069}

    with (
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
        patch("ow.workspace._ensure_workspace_materialized", return_value=(ws_dir, {"community"}, {})),
        patch("ow.workspace._apply_templates"),
    ):
        from ow.workspace import cmd_update
        cmd_update(config)

    from ow.config import load_workspace_config
    updated = load_workspace_config(ws_dir / ".ow" / "config")
    assert updated.vars["http_port"] == 9090


def test_cmd_prune_no_bare_repos(tmp_path, capsys, config):
    """cmd_prune handles missing bare repos directory gracefully."""
    from ow.workspace import cmd_prune
    cmd_prune(config)
    captured = capsys.readouterr()
    assert "No bare repos found" in captured.out


def test_cmd_prune_cleans_repos(tmp_path, capsys, config):
    """cmd_prune runs worktree prune and deletes orphaned branches on each bare repo."""
    bare_dir = tmp_path / ".bare-git-repos"
    bare_dir.mkdir()
    (bare_dir / "community.git").mkdir()
    (bare_dir / "enterprise.git").mkdir()

    with patch("ow.workspace.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        from ow.workspace import cmd_prune
        cmd_prune(config)

    # Each repo: worktree prune + worktree list + branch list = 3 calls minimum
    assert mock_run.call_count >= 6
    calls = mock_run.call_args_list
    # Verify both repos are touched
    all_args = " ".join(str(c) for c in calls)
    assert "community" in all_args
    assert "enterprise" in all_args
    # Verify worktree prune is called for each (check the actual args list, not string repr)
    prune_calls = [c for c in calls if c[0][0][3:5] == ["worktree", "prune"]]
    assert len(prune_calls) == 2
