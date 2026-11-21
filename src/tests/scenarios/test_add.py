"""`ow add` scenarios: intra-workspace evolution, PR attach, mono-stack to multi-stack promotion, and strict workspace guard."""

from __future__ import annotations

import json
import tomllib

from ow.core.paths import repos_dir
from tests.conftest import add_repo, file_url


def _setup_pr_repo(lab, name: str, base: str, base_files: dict[str, str], pr_num: int, pr_branch: str, pr_files: dict[str, str]):
    remote = add_repo(lab, name, {base: base_files}, base=base)
    collab = lab.collaborator(f"author-{name}-{pr_num}", of=remote)
    collab.commit(pr_files, message=f"PR #{pr_num}", branch=pr_branch)
    collab.push(pr_branch)
    lab.publish_pr(remote, pr_num, pr_branch)
    return remote


def test_add_outside_workspace_refuses_with_init_hint(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    res = lab.ow("add", "@18.0", cwd=lab.home)
    assert res.exit_code == 2
    assert "ow init" in res.stderr


def test_add_repo_and_attach_pr_onto_existing_detached_worktree(lab):
    """Inside a 1-level workspace on `@18.0`, `ow add upgrade` adds upgrade and `ow add enterprise#99999` attaches the PR."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    _setup_pr_repo(
        lab,
        "enterprise",
        "18.0",
        {"sale_ent/__manifest__.py": "{}"},
        99999,
        "18.0-late-pr",
        {"sale_ent/late.py": "late = True\n"},
    )
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
    _setup_pr_repo(
        lab,
        "enterprise",
        "18.0",
        {"sale_ent/__manifest__.py": "{}"},
        99999,
        "18.0-late-pr",
        {"sale_ent/late.py": "late = True\n"},
    )
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


def test_add_validates_as_before_resolving_targets(lab):
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
    from ow.core.plugins import ResolvedRepo, ResolvedTarget, load_plugins
    from ow.errors import UsageError

    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = lab.home / "attach-no-branch"
    ws.mkdir(parents=True)
    assert lab.ow("init", "community@18.0", cwd=ws).exit_code == 0

    plugins = load_plugins()
    target = ResolvedTarget(
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
            mode=UnitMode.mono(allow_existing_dir=True, allow_attach=True, dry_run=True),
        )


def test_add_healthy_worktree_warns_on_runbot_commit_mismatch(lab, capsys):
    from ow.commands._workspace import add_into_workspace
    from ow.core.plugins import ResolvedRepo, ResolvedTarget, load_plugins

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

    target = ResolvedTarget(
        name="community",
        repos=(ResolvedRepo(name="community", base="origin/18.0", branch=None, commit=sha_old),),
        source="runbot:bundle:1",
    )
    _res, code = add_into_workspace(load_plugins(), ws, target)
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
    assert "no enabled plugin handles @<base> targets" in res.stderr
