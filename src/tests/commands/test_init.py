from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from ow.commands import cmd_init
from ow.commands.init import _check_duplicate_branches, _cleanup_failed_workspace
from ow.utils import index, paths
from ow.utils.config import (
    BranchSpec,
    Config,
    WorkspaceConfig,
    parse_branch_spec,
    write_workspace_config,
)


def _make_config(vars=None, remotes=None) -> Config:
    return Config(
        vars=vars
        if vars is not None
        else {"http_port": 8069, "db_host": "localhost", "db_port": 5432},
        remotes=remotes or {},
    )


@contextmanager
def _tty(present: bool):
    """Pin whether stdin is a terminal, instead of inheriting pytest's."""
    stdin = MagicMock()
    stdin.isatty.return_value = present
    with patch("sys.stdin", stdin):
        yield


@contextmanager
def _questionary_answers(templates=("common",), aliases=("community",), spec="master", confirm=True):
    """Make every prompt answerable, and record what was asked.

    Deliberate: the non-interactive refusal has to be provable against a
    questionnaire that *would* have succeeded. Without this, deleting the
    isatty guard would make the refusal tests fail for want of a terminal
    rather than pass, and they would be green either way.
    """
    asked = []

    def checkbox(message, choices=None, **kwargs):
        asked.append(message)
        mock = MagicMock()
        mock.ask.return_value = list(templates) if "Templates" in message else list(aliases)
        return mock

    def text(message):
        asked.append(message)
        mock = MagicMock()
        mock.ask.return_value = spec
        return mock

    def confirm_(message):
        asked.append(message)
        mock = MagicMock()
        mock.ask.return_value = confirm
        return mock

    with (
        patch("questionary.checkbox", side_effect=checkbox),
        patch("questionary.text", side_effect=text),
        patch("questionary.confirm", side_effect=confirm_),
    ):
        yield asked


@contextmanager
def _no_git(ws_dir, errors=None):
    """Skip the real worktree work; keep the .ow/config.toml write real."""
    with (
        patch("ow.commands.init.ensure_workspace_materialized", return_value=(ws_dir, set(), errors or {})),
        patch("ow.commands.init.apply_templates"),
        patch("ow.commands.init.run_cmd"),
    ):
        yield


ONE_REPO = {"community": BranchSpec("origin/master", "a-branch")}


def _remembered_workspace(at, alias, spec):
    """A workspace on disk that ow knows about."""
    at.mkdir(parents=True, exist_ok=True)
    write_workspace_config(
        at / ".ow" / "config.toml",
        WorkspaceConfig(repos={alias: parse_branch_spec(spec)}, templates=["common"]),
    )
    index.remember(at)
    return at


# ---------------------------------------------------------------------------
# Where the workspace lands
# ---------------------------------------------------------------------------

def test_init_without_a_name_uses_the_current_directory(tmp_path, monkeypatch, config_with_remotes):
    here = tmp_path / "quattromori"
    here.mkdir()
    monkeypatch.chdir(here)

    with _tty(False), _questionary_answers(), _no_git(here):
        cmd_init(config_with_remotes, templates=["common"], repos=dict(ONE_REPO))

    assert (here / ".ow" / "config.toml").exists()
    assert [p.name for p in here.iterdir()] == [".ow"]


def test_init_with_a_name_creates_the_subdirectory(tmp_path, monkeypatch, config_with_remotes):
    monkeypatch.chdir(tmp_path)

    with _tty(False), _questionary_answers(), _no_git(tmp_path / "parrot"):
        cmd_init(config_with_remotes, name="parrot", templates=["common"], repos=dict(ONE_REPO))

    assert (tmp_path / "parrot" / ".ow" / "config.toml").exists()


def test_init_with_a_name_accepts_an_existing_directory(tmp_path, monkeypatch, config_with_remotes):
    """Only a .ow/config.toml is a refusal; a plain directory is just a place."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "parrot").mkdir()
    (tmp_path / "parrot" / "notes.txt").write_text("mine")

    with _tty(False), _questionary_answers(), _no_git(tmp_path / "parrot"):
        cmd_init(config_with_remotes, name="parrot", templates=["common"], repos=dict(ONE_REPO))

    assert (tmp_path / "parrot" / ".ow" / "config.toml").exists()
    assert (tmp_path / "parrot" / "notes.txt").read_text() == "mine"


def test_init_here_accepts_a_directory_name_it_would_reject_as_an_argument(tmp_path, monkeypatch, config_with_remotes):
    """The charset rule guards a name ow turns into a directory, not one it finds."""
    here = tmp_path / "my.workspace"
    here.mkdir()
    monkeypatch.chdir(here)

    with _tty(False), _questionary_answers(), _no_git(here):
        cmd_init(config_with_remotes, templates=["common"], repos=dict(ONE_REPO))

    assert (here / ".ow" / "config.toml").exists()


def test_init_rejects_invalid_name(tmp_path, monkeypatch, capsys, config):
    monkeypatch.chdir(tmp_path)
    with _tty(False), pytest.raises(SystemExit) as exc:
        cmd_init(config, name="bad name!")
    assert exc.value.code == 1
    assert "alphanumeric" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# Refusing an existing workspace
# ---------------------------------------------------------------------------

def test_init_refuses_a_named_directory_that_is_already_a_workspace(tmp_path, monkeypatch, capsys, config_with_remotes):
    monkeypatch.chdir(tmp_path)
    marker = tmp_path / "parrot" / ".ow" / "config.toml"
    marker.parent.mkdir(parents=True)
    marker.write_text('templates = []\n')

    with _tty(False), _questionary_answers(), pytest.raises(SystemExit) as exc:
        cmd_init(config_with_remotes, name="parrot", templates=["common"], repos=dict(ONE_REPO))

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "already a workspace" in err
    assert str(marker) in err


def test_init_refuses_the_current_directory_when_it_is_already_a_workspace(tmp_path, monkeypatch, capsys, config_with_remotes):
    monkeypatch.chdir(tmp_path)
    marker = tmp_path / ".ow" / "config.toml"
    marker.parent.mkdir(parents=True)
    marker.write_text('templates = []\n')

    with _tty(False), _questionary_answers(), pytest.raises(SystemExit) as exc:
        cmd_init(config_with_remotes, templates=["common"], repos=dict(ONE_REPO))

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "already a workspace" in err
    assert str(marker) in err


# ---------------------------------------------------------------------------
# The questionnaire only happens on a terminal
# ---------------------------------------------------------------------------

def test_init_without_a_tty_refuses_when_no_template_is_given(tmp_path, monkeypatch, capsys, config_with_remotes):
    monkeypatch.chdir(tmp_path)

    with _tty(False), _questionary_answers(), _no_git(tmp_path), pytest.raises(SystemExit) as exc:
        cmd_init(config_with_remotes, repos=dict(ONE_REPO))

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "not a terminal" in err
    assert "--template" in err
    assert not (tmp_path / ".ow" / "config.toml").exists()


def test_init_without_a_tty_refuses_when_no_repo_is_given(tmp_path, monkeypatch, capsys, config_with_remotes):
    monkeypatch.chdir(tmp_path)

    with _tty(False), _questionary_answers(), _no_git(tmp_path), pytest.raises(SystemExit) as exc:
        cmd_init(config_with_remotes, templates=["common"])

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "not a terminal" in err
    assert "--repo" in err
    assert not (tmp_path / ".ow" / "config.toml").exists()


def test_init_without_a_tty_asks_nothing(tmp_path, monkeypatch, config_with_remotes):
    monkeypatch.chdir(tmp_path)

    with _tty(False), _questionary_answers() as asked, _no_git(tmp_path):
        cmd_init(config_with_remotes, templates=["common"], repos=dict(ONE_REPO))

    assert asked == []
    assert (tmp_path / ".ow" / "config.toml").exists()


def test_init_without_a_tty_takes_everything_from_a_configuration(tmp_path, monkeypatch, config_with_remotes):
    """-c alone satisfies the flags: it carries both templates and repos."""
    source = tmp_path / "source"
    (source / ".ow").mkdir(parents=True)
    (source / ".ow" / "config.toml").write_text(
        'templates = ["common"]\n\n[repos]\ncommunity = "master..from-source"\n'
    )
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.chdir(target)

    with _tty(False), _questionary_answers() as asked, _no_git(target):
        cmd_init(config_with_remotes, configuration=str(source))

    assert asked == []
    assert "from-source" in (target / ".ow" / "config.toml").read_text()


def test_init_with_a_tty_asks(tmp_path, monkeypatch, config_with_remotes):
    monkeypatch.chdir(tmp_path)

    with _tty(True), _questionary_answers() as asked, _no_git(tmp_path):
        cmd_init(config_with_remotes, templates=["common"], repos=dict(ONE_REPO))

    assert any("Templates" in message for message in asked)
    assert any("Proceed" in message for message in asked)


def test_init_with_a_tty_stops_when_the_confirmation_is_declined(tmp_path, monkeypatch, capsys, config_with_remotes):
    monkeypatch.chdir(tmp_path)

    with _tty(True), _questionary_answers(confirm=False), _no_git(tmp_path):
        cmd_init(config_with_remotes, templates=["common"], repos=dict(ONE_REPO))

    assert "Aborted." in capsys.readouterr().out
    assert not (tmp_path / ".ow" / "config.toml").exists()


def test_init_checkbox_uses_choice_objects(tmp_path, monkeypatch, config):
    """The questionnaire offers Choice objects, none selected unless a flag says so."""
    monkeypatch.chdir(tmp_path)
    config.remotes = {
        "brboi-addons": {"origin": MagicMock(url="git@github.com:brboi/addons.git")},
        "community": {"origin": MagicMock(url="git@github.com:odoo/odoo.git")},
    }

    checkbox_calls = []

    def mock_checkbox(message, choices=None, **kwargs):
        checkbox_calls.append({"message": message, "choices": choices})
        mock = MagicMock()
        if "Templates" in message:
            mock.ask.return_value = ["common", "vscode"]
        else:
            mock.ask.return_value = ["brboi-addons", "community"]
        return mock

    with (
        _tty(True),
        _no_git(tmp_path),
        patch("questionary.checkbox", side_effect=mock_checkbox),
        patch("questionary.text", side_effect=lambda message: MagicMock(ask=lambda: "master")),
        patch("questionary.confirm", side_effect=lambda message: MagicMock(ask=lambda: True)),
    ):
        cmd_init(config)

    template_checkbox = checkbox_calls[0]
    assert "Templates" in template_checkbox["message"]
    template_names = [c.title for c in template_checkbox["choices"]]
    assert template_names == sorted(template_names)
    assert {"common", "vscode", "zed"} <= set(template_names)  # packaged templates
    assert not any(c.checked for c in template_checkbox["choices"])

    repo_checkbox = checkbox_calls[1]
    assert "Repos" in repo_checkbox["message"]
    repo_names = [c.title for c in repo_checkbox["choices"]]
    assert repo_names == ["brboi-addons", "community"]  # declaration order, not sorted
    assert not any(c.checked for c in repo_checkbox["choices"])


# ---------------------------------------------------------------------------
# Validation of the flags
# ---------------------------------------------------------------------------

def test_init_rejects_invalid_template(tmp_path, monkeypatch, capsys, config):
    monkeypatch.chdir(tmp_path)
    (paths.templates_dir() / "common").mkdir(parents=True)
    with _tty(False), pytest.raises(SystemExit) as exc:
        cmd_init(config, name="test", templates=["nonexistent"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "unknown template" in captured.err.lower()
    assert "common" in captured.err


def test_init_rejects_invalid_repo_alias(tmp_path, monkeypatch, capsys, config_with_remotes):
    monkeypatch.chdir(tmp_path)
    with _tty(False), pytest.raises(SystemExit) as exc:
        cmd_init(config_with_remotes, name="test", repos={"unknown": BranchSpec("origin/master")})
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "unknown repo alias" in captured.err.lower()
    assert "community" in captured.err


def test_init_configuration_preselects_its_templates_and_repos(tmp_path, monkeypatch, config_with_remotes):
    monkeypatch.chdir(tmp_path)
    src_ws = tmp_path / "source"
    (src_ws / ".ow").mkdir(parents=True)
    (src_ws / ".ow" / "config.toml").write_text(
        'templates = ["common", "vscode"]\n\n'
        '[repos]\ncommunity = "master..master-source"\n\n'
        '[vars]\nhttp_port = 9000\n'
    )
    config = _make_config(vars={"http_port": 8069}, remotes=config_with_remotes.remotes)

    checkbox_calls = []

    def mock_checkbox(message, choices=None, **kwargs):
        checkbox_calls.append({"message": message, "choices": choices})
        mock = MagicMock()
        mock.ask.return_value = ["common", "vscode"] if "Templates" in message else ["community"]
        return mock

    with (
        _tty(True),
        _no_git(tmp_path / "target"),
        patch("questionary.checkbox", side_effect=mock_checkbox),
        patch("questionary.text", side_effect=lambda message: MagicMock(ask=lambda: "master")),
        patch("questionary.confirm", side_effect=lambda message: MagicMock(ask=lambda: True)),
    ):
        cmd_init(config, name="target", configuration=str(src_ws))

    checked_templates = [c.title for c in checkbox_calls[0]["choices"] if c.checked]
    assert {"common", "vscode"} <= set(checked_templates)
    checked_repos = [c.title for c in checkbox_calls[1]["choices"] if c.checked]
    assert "community" in checked_repos


def test_init_configuration_rejects_unknown_remote(tmp_path, monkeypatch, capsys, xdg):
    monkeypatch.chdir(tmp_path)
    src_ws = tmp_path / "source"
    (src_ws / ".ow").mkdir(parents=True)
    (src_ws / ".ow" / "config.toml").write_text(
        'templates = ["common"]\n\n'
        '[repos]\ncommunity = "master"\nenterprise = "master"\n'
    )
    config = _make_config(
        remotes={"community": {"origin": MagicMock(url="git@github.com:odoo/odoo.git")}},
    )

    with _tty(False), pytest.raises(SystemExit) as exc:
        cmd_init(config, name="target", configuration=str(src_ws))

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "enterprise" in captured.err.lower()
    assert "not defined" in captured.err.lower()
    assert "community" in captured.err


def test_init_configuration_not_found(tmp_path, monkeypatch, capsys, config):
    monkeypatch.chdir(tmp_path)
    with _tty(False), pytest.raises(SystemExit) as exc:
        cmd_init(config, name="target", configuration=str(tmp_path / "nowhere"))
    assert exc.value.code == 1
    assert "not found" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------

def test_init_remembers_the_new_workspace(tmp_path, monkeypatch, config_with_remotes):
    monkeypatch.chdir(tmp_path)

    with _tty(False), _questionary_answers(), _no_git(tmp_path / "parrot"):
        cmd_init(config_with_remotes, name="parrot", templates=["common"], repos=dict(ONE_REPO))

    assert index.known_workspaces() == [(tmp_path / "parrot").resolve()]


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

def test_init_here_survives_every_repo_failing(tmp_path, monkeypatch, capsys, config_with_remotes):
    """`ow init` in a directory ow did not create must never delete it."""
    here = tmp_path / "quattromori"
    here.mkdir()
    monkeypatch.chdir(here)

    with (
        _tty(False),
        _questionary_answers(),
        _no_git(here, errors={"community": "boom"}),
        pytest.raises(SystemExit) as exc,
    ):
        cmd_init(config_with_remotes, templates=["common"], repos=dict(ONE_REPO))

    assert exc.value.code == 1
    assert here.exists()
    assert "all repos failed" in capsys.readouterr().err


def test_init_removes_the_directory_it_created_when_every_repo_fails(tmp_path, monkeypatch, config_with_remotes):
    monkeypatch.chdir(tmp_path)

    with (
        _tty(False),
        _questionary_answers(),
        _no_git(tmp_path / "parrot", errors={"community": "boom"}),
        pytest.raises(SystemExit),
    ):
        cmd_init(config_with_remotes, name="parrot", templates=["common"], repos=dict(ONE_REPO))

    assert not (tmp_path / "parrot").exists()


def test_init_keeps_going_when_only_some_repos_fail(tmp_path, monkeypatch, capsys, config_full):
    monkeypatch.chdir(tmp_path)
    config_full.remotes["enterprise"] = {"origin": MagicMock(url="git@github.com:odoo/enterprise.git")}
    repos = {
        "community": BranchSpec("origin/master", "a-branch"),
        "enterprise": BranchSpec("origin/master", "b-branch"),
    }

    with _tty(False), _questionary_answers(), _no_git(tmp_path, errors={"community": "boom"}):
        cmd_init(config_full, templates=["common"], repos=repos)

    assert (tmp_path / ".ow" / "config.toml").exists()
    assert "some repos failed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _cleanup_failed_workspace
# ---------------------------------------------------------------------------

def test_cleanup_failed_workspace_removes_if_empty(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)
    _cleanup_failed_workspace(ws_dir)
    assert not ws_dir.exists()


def test_cleanup_failed_workspace_removes_if_only_ow_dir(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / ".ow").mkdir(parents=True)
    _cleanup_failed_workspace(ws_dir)
    assert not ws_dir.exists()


def test_cleanup_failed_workspace_keeps_if_has_files(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)
    (ws_dir / "somefile.txt").touch()
    _cleanup_failed_workspace(ws_dir)
    assert ws_dir.exists()
    assert (ws_dir / "somefile.txt").exists()


def test_cleanup_failed_workspace_does_nothing_if_not_exists(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    _cleanup_failed_workspace(ws_dir)  # should not raise
    assert not ws_dir.exists()


# ---------------------------------------------------------------------------
# _check_duplicate_branches — reads the index, best-effort
# ---------------------------------------------------------------------------

def test_check_duplicate_branches_scans_the_index_not_the_neighbours(tmp_path, monkeypatch, capsys, xdg):
    """A workspace anywhere on the machine collides, as long as ow knows it."""
    _remembered_workspace(tmp_path / "elsewhere" / "parrot", "community", "master..shared-branch")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        _check_duplicate_branches({"community": BranchSpec("origin/master", "shared-branch")})

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "parrot" in err
    assert "shared-branch" in err


def test_check_duplicate_branches_lets_an_unremembered_neighbour_through(tmp_path, monkeypatch, capsys, xdg):
    """Best-effort: not in the index means invisible, and git still refuses later."""
    neighbour = tmp_path / "parrot"
    neighbour.mkdir()
    write_workspace_config(
        neighbour / ".ow" / "config.toml",
        WorkspaceConfig(repos={"community": parse_branch_spec("master..shared-branch")}, templates=["common"]),
    )
    monkeypatch.chdir(tmp_path)

    _check_duplicate_branches({"community": BranchSpec("origin/master", "shared-branch")})

    assert capsys.readouterr().err == ""


def test_check_duplicate_branches_no_duplicate_if_different_local_branch(tmp_path, capsys, xdg):
    _remembered_workspace(tmp_path / "existing", "community", "master..other-branch")

    _check_duplicate_branches({"community": BranchSpec("origin/master", "my-branch")})

    assert capsys.readouterr().err == ""


def test_check_duplicate_branches_no_duplicate_if_different_alias(tmp_path, capsys, xdg):
    """git allows the same branch name under two different bare repos."""
    _remembered_workspace(tmp_path / "existing", "enterprise", "master..shared-branch")

    _check_duplicate_branches({"community": BranchSpec("origin/master", "shared-branch")})

    assert capsys.readouterr().err == ""


def test_check_duplicate_branches_silent_if_the_index_is_empty(tmp_path, capsys, xdg):
    _check_duplicate_branches({"community": BranchSpec("origin/master", "some-branch")})

    assert capsys.readouterr().err == ""


def test_init_rejects_a_duplicate_branch(tmp_path, monkeypatch, capsys, config_with_remotes):
    _remembered_workspace(tmp_path / "parrot", "community", "master..master-parrot")
    monkeypatch.chdir(tmp_path)

    with _tty(False), _questionary_answers(), pytest.raises(SystemExit) as exc:
        cmd_init(
            config_with_remotes,
            name="new-ws",
            templates=["common"],
            repos={"community": BranchSpec("origin/master", "master-parrot")},
        )

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "already uses" in err.lower()
    assert "master-parrot" in err


def test_init_accepts_a_different_branch(tmp_path, monkeypatch, config_with_remotes):
    _remembered_workspace(tmp_path / "parrot", "community", "master..master-parrot")
    monkeypatch.chdir(tmp_path)

    with _tty(False), _questionary_answers(), _no_git(tmp_path / "new-ws"):
        cmd_init(
            config_with_remotes,
            name="new-ws",
            templates=["common"],
            repos={"community": BranchSpec("origin/master", "master-new")},
        )

    assert (tmp_path / "new-ws" / ".ow" / "config.toml").exists()
