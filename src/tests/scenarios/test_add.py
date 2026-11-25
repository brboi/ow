"""`ow add` scenarios: intra-workspace evolution, PR attach, mono-stack to multi-stack promotion, and strict workspace guard."""

from __future__ import annotations

import json
import tomllib

from ow.core.paths import repos_dir
from tests.conftest import add_repo, file_url, git_ok, setup_pr_repo


def test_add_outside_workspace_refuses_with_init_hint(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    res = lab.ow("add", "@18.0", cwd=lab.home)
    assert res.exit_code == 2
    assert "ow init" in res.stderr


def test_add_repo_and_attach_pr_onto_existing_detached_worktree(lab):
    """Inside a 1-level workspace on `@18.0`, `ow add upgrade` adds upgrade and `ow add enterprise#99999` attaches the PR."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    setup_pr_repo(lab,
    "enterprise",
    "18.0",
    {"sale_ent/__manifest__.py": "{}"},
    99999,
    "18.0-late-pr",
    {"sale_ent/late.py": "late = True\n"},)
    add_repo(lab, "upgrade", {"master": {"migrations/u.py": "u = 1\n"}}, base="master")
    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 99999,
                "title": "Late PR",
                "url": "https://github.com/odoo/enterprise/pull/99999",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-late-pr",
                "baseRefName": "18.0",
            }
        ]
    )

    ws = lab.home / "my-feat"
    ws.mkdir(parents=True)
    assert lab.ow("init", "@18.0", cwd=ws).exit_code == 0

    # 1. Add a new repo `upgrade` (falls back to origin/master since upgrade has no 18.0)
    r_upg = lab.ow("add", "upgrade", cwd=ws)
    assert r_upg.exit_code == 0, r_upg.stderr + r_upg.stdout
    assert (ws / "upgrade" / "migrations" / "u.py").is_file()

    # 2. If enterprise has uncommitted changes, attaching PR refuses cleanly
    (ws / "enterprise" / "dirty.txt").write_text("wip\n")
    r_dirty = lab.ow("add", "enterprise#99999", cwd=ws)
    assert r_dirty.exit_code == 2
    assert "uncommitted changes" in r_dirty.stderr
    (ws / "enterprise" / "dirty.txt").unlink()

    # 3. Clean detached enterprise switches onto PR #99999 and records ow-pr
    r_pr = lab.ow("add", "enterprise#99999", cwd=ws)
    assert r_pr.exit_code == 0, r_pr.stderr + r_pr.stdout
    assert lab.git(ws / "enterprise", "branch", "--show-current") == "18.0-late-pr"
    assert (ws / "enterprise" / "sale_ent" / "late.py").read_text() == "late = True\n"
    assert lab.git(
        repos_dir() / "enterprise.git", "config", "--get", "branch.18.0-late-pr.ow-pr"
    ) == "odoo/enterprise#99999"


def test_add_named_stack_promotes_mono_stack_workspace_with_confirmation_and_preserves_db_name(lab, monkeypatch):
    """Adding `iap=iap@18.0` inside a mono-stack workspace promotes it to multi-stack cleanly."""
    from ow import term

    add_repo(
        lab,
        "community",
        {"master": lab.odoo_files(series="master"), "18.0": lab.odoo_files(series="18.0")},
        base="master",
    )
    add_repo(lab, "enterprise", {"master": {"ent/__manifest__.py": "{}"}}, base="master")
    add_repo(lab, "iap", {"18.0": {"iap_mod/__manifest__.py": "{}"}}, base="18.0")
    lab.git(repos_dir() / "iap.git", "config", "ow.github-repo", "odoo/iap-apps")

    ws = lab.home / "my-feature"
    ws.mkdir(parents=True)
    assert lab.ow("init", "@master", cwd=ws).exit_code == 0
    assert "db_name = my-feature" in (ws / "odoorc").read_text()

    # Dry-run shows plan and moves nothing
    dry = lab.ow("add", "iap=iap@18.0", "-n", cwd=ws)
    assert dry.exit_code == 0, dry.stderr
    assert "worktree repair" in dry.stdout
    assert (ws / "community" / "odoo-bin").is_file()
    assert not (ws / "odoo").exists()

    # Declined prompt aborts cleanly without moving anything
    monkeypatch.setattr(term, "interactive", lambda: True)
    declined = lab.ow("add", "iap=iap@18.0", input="n\n", cwd=ws)
    assert declined.exit_code == 2
    assert (ws / "community" / "odoo-bin").is_file()

    # Accepted promotion (with explicit `--as main-odoo`) moves existing stack, repairs worktrees,
    # preserves `odoo.db_name = "my-feature"`, keeps port 8069, and adds `iap/` on port 8070!
    ok = lab.ow("add", "iap=iap@18.0", "--as", "main-odoo", "-y", cwd=ws)
    assert ok.exit_code == 0, ok.stderr + ok.stdout

    assert (ws / "main-odoo" / "community" / "odoo-bin").is_file()
    assert (ws / "main-odoo" / "enterprise" / "ent" / "__manifest__.py").is_file()
    assert (ws / "iap" / "iap" / "iap_mod" / "__manifest__.py").is_file()
    assert (ws / "iap" / "community" / "odoo-bin").is_file()

    # DB name preserved for original stack!
    old_odoorc = (ws / "main-odoo" / "odoorc").read_text()
    assert "db_name = my-feature" in old_odoorc
    assert "http_port = 8069" in old_odoorc
    assert "http_port = 8070" in (ws / "iap" / "odoorc").read_text()

    # Worktrees are healthy (not prunable) and `.code-workspace` is generated
    st = lab.ow("status", cwd=ws)
    assert st.exit_code == 0, st.stderr
    assert "prunable" not in st.stdout
    code_ws = ws / "my-feature.code-workspace"
    assert code_ws.is_file()
    folders = [f["name"] for f in json.loads(code_ws.read_text())["folders"]]
    assert folders == ["iap", "main-odoo"]

    # Single root `.ow/config.toml` declares both stacks; no `.ow/` inside stacks
    assert not (ws / "main-odoo" / ".ow").exists()
    assert not (ws / "iap" / ".ow").exists()
    cfg = tomllib.loads((ws / ".ow" / "config.toml").read_text())
    assert set(cfg["stacks"]) == {"main-odoo", "iap"}
    assert cfg["stacks"]["main-odoo"]["odoo"]["db_name"] == "my-feature"


def test_add_named_stack_in_empty_workspace_does_not_promote(lab):
    """Adding `voip=community@18.0` after bare `ow init` converts cleanly to multi-stack without `main/`."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")}, base="18.0")

    ws = lab.home / "porting"
    ws.mkdir(parents=True)
    assert lab.ow("init", cwd=ws).exit_code == 0

    bad_as = lab.ow("add", "voip=community@18.0", "--as", "main", cwd=ws)
    assert bad_as.exit_code == 2
    assert "--as" in bad_as.stderr

    res = lab.ow("add", "voip=community@18.0", cwd=ws)
    assert res.exit_code == 0, res.stderr + res.stdout
    assert "promoting" not in res.stdout
    assert not (ws / "main").exists()
    assert (ws / ".ow" / "config.toml").is_file()
    assert not (ws / "voip" / ".ow").exists()
    assert (ws / "voip" / "community" / "odoo-bin").is_file()
    assert "http_port = 8069" in (ws / "voip" / "odoorc").read_text()

    code_ws = ws / "porting.code-workspace"
    assert code_ws.is_file()
    folders = [f["name"] for f in json.loads(code_ws.read_text())["folders"]]
    assert folders == ["voip"]


def test_add_promotes_when_auto_name_equals_repo_dir_name(lab):
    """Promotion staging must succeed even when existing stack's auto-name (`voip`) equals its repo dir (`voip/`)."""
    add_repo(lab, "voip", {"master": {"README.md": "voip\n"}}, base="master")
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")}, base="18.0")

    ws = lab.home / "voip-ws"
    ws.mkdir(parents=True)
    assert lab.ow("init", "voip", cwd=ws).exit_code == 0

    res = lab.ow("add", "odoo=community@18.0", "-y", cwd=ws)
    assert res.exit_code == 0, res.stderr + res.stdout
    assert (ws / "voip" / "voip" / "README.md").is_file()
    assert (ws / "odoo" / "community" / "odoo-bin").is_file()


def test_add_in_multi_stack_updates_existing_stack_or_adds_new_stack(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    add_repo(lab, "upgrade", {"master": {"u.py": "1\n"}}, base="master")
    add_repo(lab, "voip", {"master": {"v.py": "1\n"}}, base="master")

    ws = lab.home / "multi"
    ws.mkdir(parents=True)
    assert lab.ow("init", "odoo=community@18.0", cwd=ws).exit_code == 0

    # From root: `odoo=enterprise@18.0` adds enterprise into existing `odoo` stack!
    r1 = lab.ow("add", "odoo=enterprise@18.0", cwd=ws)
    assert r1.exit_code == 0, r1.stderr + r1.stdout
    assert (ws / "odoo" / "enterprise" / "ent" / "__manifest__.py").is_file()

    # From inside `odoo/`: unnamed `upgrade` adds into `odoo/`, named `voip=voip` adds sibling stack `voip/`!
    r2 = lab.ow("add", "upgrade", cwd=ws / "odoo")
    assert r2.exit_code == 0, r2.stderr + r2.stdout
    assert (ws / "odoo" / "upgrade" / "u.py").is_file()

    r3 = lab.ow("add", "voip=voip", cwd=ws / "odoo")
    assert r3.exit_code == 0, r3.stderr + r3.stdout
    assert (ws / "voip" / "voip" / "v.py").is_file()


def test_add_dry_run_non_promotion_leaves_workspace_untouched(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    setup_pr_repo(lab,
    "enterprise",
    "18.0",
    {"sale_ent/__manifest__.py": "{}"},
    99999,
    "18.0-late-pr",
    {"sale_ent/late.py": "late = True\n"},)
    add_repo(lab, "upgrade", {"master": {"migrations/u.py": "u = 1\n"}}, base="master")
    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 99999,
                "title": "Late PR",
                "url": "https://github.com/odoo/enterprise/pull/99999",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-late-pr",
                "baseRefName": "18.0",
            }
        ]
    )

    ws = lab.home / "dry-add-mono"
    ws.mkdir(parents=True)
    assert lab.ow("init", "@18.0", cwd=ws).exit_code == 0

    dry_repo = lab.ow("add", "-n", "upgrade", cwd=ws)
    assert dry_repo.exit_code == 0, dry_repo.stderr
    assert "worktree add" in dry_repo.stdout
    assert not (ws / "upgrade").exists()
    assert "upgrade" not in (ws / ".ow" / "config.toml").read_text()

    dry_pr = lab.ow("add", "-n", "enterprise#99999", cwd=ws)
    assert dry_pr.exit_code == 0, dry_pr.stderr
    assert "switch" in dry_pr.stdout
    assert lab.git(ws / "enterprise", "branch", "--show-current") == ""

    multi = lab.home / "dry-add-multi"
    multi.mkdir(parents=True)
    assert lab.ow("init", "stack-a=@18.0", cwd=multi).exit_code == 0
    dry_stack = lab.ow("add", "-n", "stack-b=@18.0", cwd=multi)
    assert dry_stack.exit_code == 0, dry_stack.stderr
    assert not (multi / "stack-b").exists()
    assert "stack-b" not in (multi / "dry-add-multi.code-workspace").read_text()


def test_add_dry_run_reports_each_worktree_state_like_a_real_add(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    add_repo(lab, "upgrade", {"master": {"migrations/u.py": "u = 1\n"}}, base="master")
    ws = lab.home / "dry-states"
    ws.mkdir(parents=True)
    assert lab.ow("init", "@18.0", cwd=ws).exit_code == 0

    healthy = lab.ow("add", "-n", "community", cwd=ws)
    assert healthy.exit_code == 0, healthy.stderr
    assert "# community: already exists" in healthy.stdout
    assert "worktree add" not in healthy.stdout

    (ws / "upgrade").mkdir()  # an empty directory: an interrupted worktree
    broken = lab.ow("add", "-n", "upgrade", cwd=ws)
    assert broken.exit_code == 0, broken.stderr
    assert "remove incomplete worktree" in broken.stdout
    assert "worktree add" in broken.stdout
    assert list((ws / "upgrade").iterdir()) == []

    (ws / "upgrade" / "stray.txt").write_text("not a worktree\n")
    foreign = lab.ow("add", "-n", "upgrade", cwd=ws)
    assert foreign.exit_code == 2
    assert "not a worktree of upgrade" in foreign.stderr


def test_add_validates_as_before_resolving_specs(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    gh_log = lab.fake_gh([])

    ws = lab.home / "early-as-mono"
    ws.mkdir(parents=True)
    assert lab.ow("init", "@18.0", cwd=ws).exit_code == 0

    r_mono = lab.ow("add", "enterprise#99999", "--as", "bad", cwd=ws)
    assert r_mono.exit_code == 2
    assert "--as" in r_mono.stderr
    assert not gh_log.exists()

    multi = lab.home / "early-as-multi"
    multi.mkdir(parents=True)
    assert lab.ow("init", "odoo=@18.0", cwd=multi).exit_code == 0

    r_multi = lab.ow("add", "enterprise#99999", "--as", "bad", cwd=multi)
    assert r_multi.exit_code == 2
    assert "--as" in r_multi.stderr
    assert not gh_log.exists()


def test_add_attach_without_branch_raises_usage_error_not_assert(lab):
    import pytest

    from ow.commands._workspace import UnitMode, create_workspace_unit
    from ow.core.plugins import ResolvedRepo, ResolvedSpec, load_plugins
    from ow.errors import UsageError

    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = lab.home / "attach-no-branch"
    ws.mkdir(parents=True)
    assert lab.ow("init", "community@18.0", cwd=ws).exit_code == 0

    plugins = load_plugins()
    target = ResolvedSpec(
        name="community",
        repos=(
            ResolvedRepo(
                name="community",
                base="origin/18.0",
                fetch_ref="refs/pull/1/head",
                branch=None,
            ),
        ),
        source="test",
    )
    with pytest.raises(UsageError, match="without a target branch"):
        create_workspace_unit(
            plugins,
            root=ws,
            resolved=target,
            mode=UnitMode.mono(allow_existing_dir=True, allow_attach=True),
            dry_run=True,
        )


def test_add_healthy_worktree_warns_on_runbot_commit_mismatch(lab, capsys):
    from ow.commands._workspace import create_workspace_units, existing_unit_job
    from ow.core.plugins import ResolvedRepo, ResolvedSpec, load_plugins

    remote = add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    bare = repos_dir() / "community.git"
    sha_old = lab.sha(bare, "refs/remotes/origin/18.0")

    collab = lab.collaborator("adv", of=remote)
    collab.git("checkout", "18.0")
    collab.commit({"addons/web/new.py": "new = 1\n"}, message="advance")
    collab.push("18.0")

    ws = lab.home / "commit-warn"
    ws.mkdir(parents=True)
    assert lab.ow("init", "community@18.0", cwd=ws).exit_code == 0
    capsys.readouterr()

    target = ResolvedSpec(
        name="community",
        repos=(ResolvedRepo(name="community", base="origin/18.0", branch=None, commit=sha_old),),
        source="runbot:bundle:1",
    )
    _res, code = create_workspace_units(load_plugins(), [existing_unit_job(ws, target)])
    assert code == 0
    captured = capsys.readouterr()
    assert "requested commit" in captured.err
    assert "was not checked out" in captured.err


def test_add_expands_base_with_workspace_default_repos(lab):
    """`odoo.default_repos` set in the workspace wins over the global/default value for `ow add @<base>`."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "upgrade", {"18.0": {"migrations/u.py": "u = 1\n"}})

    ws = lab.home / "ws-default-repos"
    ws.mkdir(parents=True)
    assert lab.ow("init", "community@18.0", cwd=ws).exit_code == 0
    set_repos = lab.ow("config", "set", "odoo.default_repos", "community", "upgrade", cwd=ws)
    assert set_repos.exit_code == 0, set_repos.stderr

    res = lab.ow("add", "@18.0", cwd=ws)
    assert res.exit_code == 0, res.stderr + res.stdout
    assert (ws / "upgrade" / "migrations" / "u.py").is_file()
    assert not (ws / "enterprise").exists()


def test_add_honours_workspace_plugin_enablement(lab):
    """A workspace-level `odoo.enabled = false` stops `ow add @<base>` from using the odoo plugin."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})

    ws = lab.home / "ws-odoo-disabled"
    ws.mkdir(parents=True)
    assert lab.ow("init", "community@18.0", cwd=ws).exit_code == 0
    assert lab.ow("config", "set", "odoo.enabled", "false", cwd=ws).exit_code == 0

    res = lab.ow("add", "@18.0", cwd=ws)
    assert res.exit_code == 2
    assert "no enabled plugin handles @<base> specs" in res.stderr


def test_add_honours_an_in_repo_plugins_known_repos(lab):
    """A trusted in-repo plugin's `ow_known_repos` is part of the catalog `ow add` resolves names with."""
    extra = lab.remote("extra", {"main": {"README.md": "extra\n"}}, head="main")
    plugin = (
        "from ow.plugin_api import KnownRepo, hookimpl\n"
        "OW_API_VERSION = 1\n"
        'PLUGIN_NAME = "demo_known"\n'
        "@hookimpl\n"
        "def ow_known_repos():\n"
        f'    return [KnownRepo(name="extra", url="{file_url(extra)}")]\n'
    )
    add_repo(
        lab,
        "demo",
        {"main": {"pyproject.toml": '[tool.ow]\nplugin = "ow_plugin.py"\n', "ow_plugin.py": plugin}},
        base="main",
    )
    ws = lab.home / "demo-ws"
    ws.mkdir(parents=True)
    assert lab.ow("init", "demo@main", cwd=ws).exit_code == 0
    assert lab.ow("trust", "-y", cwd=ws).exit_code == 0
    assert not (repos_dir() / "extra.git").exists()

    res = lab.ow("add", "extra@main", cwd=ws)

    assert res.exit_code == 0, res.stderr + res.stdout
    assert (ws / "extra" / "README.md").is_file()
    assert (repos_dir() / "extra.git").is_dir()


def _init_edited_community(lab, name: str):
    """A one-repo workspace whose community worktree holds a tracked edit and an untracked file."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = lab.home / name
    ws.mkdir(parents=True)
    assert lab.ow("init", "community@18.0", cwd=ws).exit_code == 0
    (ws / "community" / "odoo-bin").write_text("my edit\n")
    (ws / "community" / "notes.txt").write_text("not committed\n")
    return ws


def test_add_on_a_moved_workspace_repairs_the_worktree_and_keeps_the_work(lab):
    """R1: a moved workspace's worktrees are re-registered, never treated as broken and removed."""
    ws = _init_edited_community(lab, "before-move")
    moved = lab.home / "after-move"
    ws.rename(moved)

    res = lab.ow("add", "community", cwd=moved)

    assert res.exit_code == 0, res.stderr + res.stdout
    assert "worktree repair" in res.stdout
    assert (moved / "community" / "odoo-bin").read_text() == "my edit\n"
    assert (moved / "community" / "notes.txt").read_text() == "not committed\n"
    listing = lab.git(repos_dir() / "community.git", "worktree", "list", "--porcelain")
    assert f"worktree {moved / 'community'}" in listing


def test_add_after_a_prune_of_a_moved_workspace_refuses_and_keeps_the_work(lab):
    """R1: once `git worktree prune` forgot a moved worktree, `ow add` refuses; the files are the user's."""
    ws = _init_edited_community(lab, "prune-before")
    moved = lab.home / "prune-after"
    ws.rename(moved)
    lab.git(repos_dir() / "community.git", "worktree", "prune")

    res = lab.ow("add", "community", cwd=moved)

    assert res.exit_code == 2
    assert "forgot" in res.stderr
    assert (moved / "community" / "odoo-bin").read_text() == "my edit\n"
    assert (moved / "community" / "notes.txt").read_text() == "not committed\n"


def test_add_refuses_to_replace_an_incomplete_worktree_that_holds_files(lab):
    """R1: only a directory proven incomplete is replaced; a `.git` file beside real files is not."""
    ws = _init_edited_community(lab, "has-files")
    add_repo(lab, "upgrade", {"master": {"migrations/u.py": "u = 1\n"}}, base="master")
    (ws / "upgrade").mkdir()
    (ws / "upgrade" / ".git").write_text("gitdir: /nowhere/upgrade.git/worktrees/upgrade\n")
    (ws / "upgrade" / "mine.txt").write_text("work\n")

    res = lab.ow("add", "upgrade", cwd=ws)

    assert res.exit_code != 0
    assert (ws / "upgrade" / "mine.txt").read_text() == "work\n"


def test_add_base_dotdot_branch_creates_the_branch_with_its_base_and_an_upstream(lab):
    """Q24 + Q20: `repo@<base>..<branch>` creates the branch from the base, upstream pre-set on origin."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    remote = add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    ws = lab.home / "fresh-branch"
    ws.mkdir(parents=True)
    assert lab.ow("init", "community@18.0", cwd=ws).exit_code == 0

    res = lab.ow("add", "enterprise@18.0..18.0-fix", cwd=ws)

    assert res.exit_code == 0, res.stderr + res.stdout
    wt = ws / "enterprise"
    bare = repos_dir() / "enterprise.git"
    assert lab.git(wt, "branch", "--show-current") == "18.0-fix"
    assert lab.git(bare, "config", "branch.18.0-fix.ow-base") == "origin/18.0"
    assert lab.git(bare, "config", "branch.18.0-fix.remote") == "origin"
    assert lab.git(bare, "config", "branch.18.0-fix.merge") == "refs/heads/18.0-fix"
    assert "base = origin/18.0" not in (ws / ".ow" / "config.toml").read_text()

    # G3: a bare `git pull` before the first push fails and merges nothing; the first `git push` creates it.
    head = lab.sha(wt, "HEAD")
    assert not git_ok(wt, "pull")
    assert lab.sha(wt, "HEAD") == head
    assert git_ok(wt, "push")
    assert lab.sha(remote, "refs/heads/18.0-fix") == head


def test_add_base_dotdot_branch_checks_out_a_branch_a_remote_has_and_tracks_it(lab):
    """Q24: when a remote holds the branch it is checked out tracking it, with the spelled base."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    remote = add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    collab = lab.collaborator("feat-author", of=remote)
    collab.git("checkout", "18.0")
    collab.commit({"ent/feat.py": "feat = 1\n"}, message="feature", branch="18.0-feat")
    collab.push("18.0-feat")
    ws = lab.home / "existing-branch"
    ws.mkdir(parents=True)
    assert lab.ow("init", "community@18.0", cwd=ws).exit_code == 0

    res = lab.ow("add", "enterprise@18.0..18.0-feat", cwd=ws)

    assert res.exit_code == 0, res.stderr + res.stdout
    bare = repos_dir() / "enterprise.git"
    assert (ws / "enterprise" / "ent" / "feat.py").read_text() == "feat = 1\n"
    assert lab.git(bare, "config", "branch.18.0-feat.ow-base") == "origin/18.0"
    assert lab.git(ws / "enterprise", "rev-parse", "--abbrev-ref", "@{u}") == "origin/18.0-feat"


def test_add_base_dotdot_remote_branch_takes_the_branch_from_that_remote(lab):
    """Q24: `repo@<base>..<remote>/<branch>` names the remote that holds the branch."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    remote = add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    fork = lab.remote("enterprise-dev", {"18.0": {"ent/__manifest__.py": "{}"}})
    collab = lab.collaborator("fork-author", of=fork)
    collab.git("checkout", "18.0")
    collab.commit({"ent/fork.py": "fork = 1\n"}, message="fork work", branch="18.0-fork")
    collab.push("18.0-fork")
    lab.git(repos_dir() / "enterprise.git", "remote", "add", "dev", str(fork))
    ws = lab.home / "remote-branch"
    ws.mkdir(parents=True)
    assert lab.ow("init", "community@18.0", cwd=ws).exit_code == 0
    assert remote.exists()

    res = lab.ow("add", "enterprise@18.0..dev/18.0-fork", cwd=ws)

    assert res.exit_code == 0, res.stderr + res.stdout
    assert (ws / "enterprise" / "ent" / "fork.py").read_text() == "fork = 1\n"
    assert lab.git(ws / "enterprise", "rev-parse", "--abbrev-ref", "@{u}") == "dev/18.0-fork"
    assert lab.git(repos_dir() / "enterprise.git", "config", "branch.18.0-fork.ow-base") == "origin/18.0"


def test_add_at_base_dotdot_branch_puts_every_default_repo_on_the_branch(lab):
    """Q24: `@<base>..<branch>` spans `odoo.default_repos`."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "upgrade", {"18.0": {"migrations/u.py": "u = 1\n"}})
    ws = lab.home / "span-default-repos"
    ws.mkdir(parents=True)
    assert lab.ow("init", "community@18.0", cwd=ws).exit_code == 0
    assert lab.ow("config", "set", "odoo.default_repos", "community", "upgrade", cwd=ws).exit_code == 0

    res = lab.ow("add", "@18.0..18.0-span", cwd=ws)

    assert res.exit_code == 0, res.stderr + res.stdout
    for repo in ("community", "upgrade"):
        assert lab.git(ws / repo, "branch", "--show-current") == "18.0-span"
        assert lab.git(repos_dir() / f"{repo}.git", "config", "branch.18.0-span.ow-base") == "origin/18.0"


def test_add_branch_without_a_known_base_warns_and_suggests_the_range_form(lab):
    """Q21: `repo@<branch>` keeps inferring the base; a fall back to origin/HEAD is warned about."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    remote = add_repo(lab, "tools", {"main": {"tool.py": "t = 1\n"}}, base="main")
    collab = lab.collaborator("tools-author", of=remote)
    collab.git("checkout", "main")
    collab.commit({"tool2.py": "t = 2\n"}, message="topic work", branch="topic")
    collab.push("topic")
    ws = lab.home / "no-base"
    ws.mkdir(parents=True)
    assert lab.ow("init", "community@18.0", cwd=ws).exit_code == 0

    res = lab.ow("add", "tools@topic", cwd=ws)

    assert res.exit_code == 0, res.stderr + res.stdout
    assert "tools@<base>..topic" in res.stderr


def test_add_shows_the_whole_plan_including_the_clone_before_acting(lab):
    """Q11: a repo that is not cloned yet is a step of the plan; `-n` clones nothing."""
    extra = lab.remote("extra", {"main": {"README.md": "extra\n"}}, head="main")
    plugin = (
        "from ow.plugin_api import KnownRepo, hookimpl\n"
        "OW_API_VERSION = 1\n"
        'PLUGIN_NAME = "demo_known"\n'
        "@hookimpl\n"
        "def ow_known_repos():\n"
        f'    return [KnownRepo(name="extra", url="{file_url(extra)}")]\n'
    )
    add_repo(
        lab,
        "demo",
        {"main": {"pyproject.toml": '[tool.ow]\nplugin = "ow_plugin.py"\n', "ow_plugin.py": plugin}},
        base="main",
    )
    ws = lab.home / "plan-first"
    ws.mkdir(parents=True)
    assert lab.ow("init", "demo@main", cwd=ws).exit_code == 0
    assert lab.ow("trust", "-y", cwd=ws).exit_code == 0

    dry = lab.ow("add", "-n", "extra@main", cwd=ws)

    assert dry.exit_code == 0, dry.stderr + dry.stdout
    assert "# extra: clone bare repo from" in dry.stdout
    assert "# extra: add worktree at" in dry.stdout
    assert not (repos_dir() / "extra.git").exists()
    assert not (ws / "extra").exists()

    real = lab.ow("add", "extra@main", cwd=ws)

    assert real.exit_code == 0, real.stderr + real.stdout
    lines = real.stdout.splitlines()
    clone = next(i for i, line in enumerate(lines) if line.startswith("# extra: clone bare repo"))
    result = next(i for i, line in enumerate(lines) if line.startswith(("ready ", "updated ")))
    assert clone < result
    assert (ws / "extra" / "README.md").is_file()


def test_add_base_dotdot_branch_attaches_to_a_detached_worktree_with_an_upstream(lab):
    """Q20: attaching a new branch to an existing detached worktree pre-sets its upstream like a creation does."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    remote = add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    ws = lab.home / "attach-branch"
    ws.mkdir(parents=True)
    assert lab.ow("init", "community@18.0,enterprise@18.0", cwd=ws).exit_code == 0
    assert lab.git(ws / "enterprise", "branch", "--show-current") == ""

    res = lab.ow("add", "enterprise@18.0..18.0-attached", cwd=ws)

    assert res.exit_code == 0, res.stderr + res.stdout
    wt = ws / "enterprise"
    bare = repos_dir() / "enterprise.git"
    assert lab.git(wt, "branch", "--show-current") == "18.0-attached"
    assert lab.git(bare, "config", "branch.18.0-attached.ow-base") == "origin/18.0"
    assert lab.git(bare, "config", "branch.18.0-attached.remote") == "origin"
    assert lab.git(bare, "config", "branch.18.0-attached.merge") == "refs/heads/18.0-attached"
    assert not git_ok(wt, "pull")
    assert git_ok(wt, "push")
    assert git_ok(remote, "rev-parse", "--verify", "refs/heads/18.0-attached")


def test_add_keeps_the_config_keys_of_plugins_that_are_not_loaded_and_of_in_repo_plugins(lab):
    """Unknown keys survive the config rewrite of `ow add`, an in-repo plugin's among them."""
    import tomllib

    plugin = (
        "from ow.plugin_api import Setting, hookimpl\n"
        "OW_API_VERSION = 1\n"
        'PLUGIN_NAME = "slot"\n'
        "@hookimpl\n"
        "def ow_settings():\n"
        '    return [Setting(key="slot.name", type=str, default="shared", help="a slot")]\n'
    )
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "upgrade", {"18.0": {"migrations/u.py": "u = 1\n"}})
    add_repo(
        lab,
        "slot",
        {"main": {"pyproject.toml": '[tool.ow]\nplugin = "ow_plugin.py"\n', "ow_plugin.py": plugin}},
        base="main",
    )
    ws = lab.home / "keeps-keys"
    ws.mkdir(parents=True)
    assert lab.ow("init", "community@18.0,slot@main", cwd=ws).exit_code == 0
    assert lab.ow("trust", "-y", cwd=ws).exit_code == 0
    assert lab.ow("config", "set", "slot.name", "mine", cwd=ws).exit_code == 0
    config = ws / ".ow" / "config.toml"
    config.write_text(config.read_text() + '\n[elsewhere]\nwho = "me"\n')

    res = lab.ow("add", "upgrade", cwd=ws)

    assert res.exit_code == 0, res.stderr + res.stdout
    data = tomllib.loads(config.read_text())
    assert data["slot"] == {"name": "mine"}
    assert data["elsewhere"] == {"who": "me"}
    assert "upgrade" in data["repos"]
