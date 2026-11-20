"""`ow add` scenarios: PR resolution, sister PRs, multi-workspace tasks (issue #84), fresh workspaces."""

from __future__ import annotations

import json

from ow.core.paths import repos_dir
from tests.conftest import add_repo


def _setup_pr_repo(lab, name: str, base_files: dict[str, str], pr_num: int, pr_branch: str, pr_files: dict[str, str]):
    remote = add_repo(lab, name, {"18.0": base_files})
    collab = lab.collaborator(f"author-{name}-{pr_num}", of=remote)
    collab.commit(pr_files, message=f"PR #{pr_num}", branch=pr_branch)
    collab.push(pr_branch)
    lab.publish_pr(remote, pr_num, pr_branch)
    return remote


def test_add_pr_creates_num_branch_dir_and_auto_adds_community(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    _setup_pr_repo(
        lab,
        "enterprise",
        {"sale_ent/__manifest__.py": "{'name': 'Sale Ent'}"},
        12345,
        "18.0-fix-invoice-mic",
        {"sale_ent/fix.py": "fixed = True\n"},
    )
    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 12345,
                "title": "Fix invoice",
                "url": "https://github.com/odoo/enterprise/pull/12345",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-fix-invoice-mic",
                "baseRefName": "18.0",
            }
        ]
    )

    parent = lab.home / "odoo"
    parent.mkdir(parents=True)
    result = lab.ow("add", "enterprise#12345", cwd=parent)

    assert result.exit_code == 0, result.stderr + result.stdout
    ws = parent / "12345-18.0-fix-invoice-mic"
    assert ws.is_dir()
    assert (ws / "community" / "odoo-bin").is_file()
    assert (ws / "enterprise" / "sale_ent" / "fix.py").read_text() == "fixed = True\n"
    assert lab.git(ws / "enterprise", "symbolic-ref", "--short", "HEAD") == "18.0-fix-invoice-mic"
    # community was auto-expanded detached on 18.0
    assert lab.git(ws / "community", "rev-parse", "HEAD") == lab.sha(
        repos_dir() / "community.git", "refs/remotes/origin/18.0"
    )
    assert (ws / "odoorc").is_file()


def test_add_pr_discovers_sister_pr_on_community(lab):
    _setup_pr_repo(
        lab,
        "community",
        lab.odoo_files(series="18.0"),
        9001,
        "18.0-sister-fix",
        {"addons/web/sister.py": "community_fix = True\n"},
    )
    _setup_pr_repo(
        lab,
        "enterprise",
        {"ent/__manifest__.py": "{}"},
        9002,
        "18.0-sister-fix",
        {"ent/sister.py": "ent_fix = True\n"},
    )
    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 9002,
                "title": "Sister ent",
                "url": "https://github.com/odoo/enterprise/pull/9002",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-sister-fix",
                "baseRefName": "18.0",
            },
            {
                "repo": "odoo/odoo",
                "number": 9001,
                "title": "Sister comm",
                "url": "https://github.com/odoo/odoo/pull/9001",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-sister-fix",
                "baseRefName": "18.0",
            },
        ]
    )

    parent = lab.home / "odoo"
    parent.mkdir(parents=True)
    result = lab.ow("add", "enterprise#9002", cwd=parent)

    assert result.exit_code == 0, result.stderr + result.stdout
    ws = parent / "9002-18.0-sister-fix"
    assert (ws / "community" / "addons" / "web" / "sister.py").read_text() == "community_fix = True\n"
    assert (ws / "enterprise" / "ent" / "sister.py").read_text() == "ent_fix = True\n"
    assert lab.git(ws / "community", "symbolic-ref", "--short", "HEAD") == "18.0-sister-fix"
    assert lab.git(ws / "enterprise", "symbolic-ref", "--short", "HEAD") == "18.0-sister-fix"


def test_add_multi_workspace_task_in_one_command_issue_84(lab):
    """Single command creates iap (+enterprise), client, and voip workspaces linked by a task."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    _setup_pr_repo(
        lab,
        "iap-apps",
        {"iap_mod/__manifest__.py": "{}"},
        42,
        "18.0-iap-voip",
        {"iap_mod/app.py": "iap = 1\n"},
    )
    _setup_pr_repo(
        lab,
        "enterprise",
        {"voip_ent/__manifest__.py": "{}"},
        15402,
        "18.0-ent-voip",
        {"voip_ent/ent.py": "ent = 1\n"},
    )
    _setup_pr_repo(
        lab,
        "voip",
        {"README.md": "voip stack\n"},
        89,
        "voip-stack-pr",
        {"server.py": "voip = 1\n"},
    )
    lab.fake_gh(
        [
            {
                "repo": "odoo/iap-apps",
                "number": 42,
                "title": "IAP side",
                "url": "https://github.com/odoo/iap-apps/pull/42",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-iap-voip",
                "baseRefName": "18.0",
            },
            {
                "repo": "odoo/enterprise",
                "number": 15402,
                "title": "Enterprise side",
                "url": "https://github.com/odoo/enterprise/pull/15402",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-ent-voip",
                "baseRefName": "18.0",
            },
            {
                "repo": "odoo/voip",
                "number": 89,
                "title": "VoIP service",
                "url": "https://github.com/odoo/voip/pull/89",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "voip-stack-pr",
                "baseRefName": "18.0",
            },
        ]
    )

    parent = lab.home / "odoo"
    parent.mkdir(parents=True)
    result = lab.ow(
        "add",
        "-t",
        "voip-task",
        "iap-apps#42+enterprise",
        "enterprise#15402",
        "voip#89",
        cwd=parent,
    )

    assert result.exit_code == 0, result.stderr + result.stdout
    ws_iap = parent / "42-18.0-iap-voip"
    ws_client = parent / "15402-18.0-ent-voip"
    ws_voip = parent / "89-voip-stack-pr"

    # 1. Composite target got community (auto) + iap-apps (PR #42) + enterprise (18.0 detached)
    assert (ws_iap / "community" / "odoo-bin").is_file()
    assert (ws_iap / "iap-apps" / "iap_mod" / "app.py").is_file()
    assert (ws_iap / "enterprise" / "voip_ent" / "__manifest__.py").is_file()
    # 2. Client got community (auto) + enterprise (PR #15402)
    assert (ws_client / "community" / "odoo-bin").is_file()
    assert (ws_client / "enterprise" / "voip_ent" / "ent.py").is_file()
    # 3. VoIP stack is non-Odoo: only voip repo
    assert (ws_voip / "voip" / "server.py").is_file()
    assert not (ws_voip / "community").exists()

    # Ports auto-allocated without collision
    assert "http_port = 8069" in (ws_iap / "odoorc").read_text()
    assert "http_port = 8070" in (ws_client / "odoorc").read_text()

    # Multi-root .code-workspace written in common parent
    code_ws = parent / "voip-task.code-workspace"
    assert code_ws.is_file()
    folders = [f["name"] for f in json.loads(code_ws.read_text())["folders"]]
    assert folders == ["42-18.0-iap-voip", "15402-18.0-ent-voip", "89-voip-stack-pr"]


def test_add_refuses_existing_directory_no_adopt(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files()})
    parent = lab.home / "odoo"
    (parent / "existing").mkdir(parents=True)

    result = lab.ow("add", "existing", "-r", "community", "--base", "18.0", cwd=parent)
    assert result.exit_code == 2
    assert "already exists" in result.stderr


def test_add_rejects_path_traversal_workspace_and_task_names(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files()})
    parent = lab.home / "odoo"
    parent.mkdir(parents=True)

    bad_ws = lab.ow("add", "../escape", "-r", "community", "--base", "18.0", cwd=parent)
    assert bad_ws.exit_code == 2
    assert "invalid workspace name" in bad_ws.stderr

    bad_task = lab.ow("add", "-t", "../escape", "ok-ws", "-r", "community", "--base", "18.0", cwd=parent)
    assert bad_task.exit_code == 2
    assert "invalid task name" in bad_task.stderr


def test_add_fork_pr_with_existing_local_branch_fetches_pr_ref(lab):
    """If a bare already has local branch `18.0`, adding a fork PR from `18.0` still fetches the PR."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    remote = _setup_pr_repo(
        lab,
        "enterprise",
        {"ent/__manifest__.py": "{}"},
        7777,
        "fork-18.0-branch",
        {"ent/fork_fix.py": "from_fork = True\n"},
    )
    # Create a pre-existing local branch `18.0` in enterprise bare pointing at old base.
    lab.git(repos_dir() / "enterprise.git", "branch", "18.0", "origin/18.0")
    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 7777,
                "title": "Fork PR from 18.0",
                "url": "https://github.com/odoo/enterprise/pull/7777",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0",
                "baseRefName": "18.0",
            }
        ]
    )
    parent = lab.home / "odoo"
    parent.mkdir(parents=True)

    res = lab.ow("add", "enterprise#7777", cwd=parent)
    assert res.exit_code == 0, res.stderr + res.stdout
    ws = parent / "7777-18.0"
    assert (ws / "enterprise" / "ent" / "fork_fix.py").read_text() == "from_fork = True\n"
    branch = lab.git(ws / "enterprise", "branch", "--show-current")
    assert branch == "pr-7777-18.0"
    assert lab.git(repos_dir() / "enterprise.git", "config", "--get", f"branch.{branch}.ow-pr") == "odoo/enterprise#7777"


def test_add_fresh_workspace_name_with_plus_sign(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files()})
    parent = lab.home / "odoo"
    parent.mkdir(parents=True)

    res = lab.ow("add", "fix-a+b", "-r", "community", "--base", "18.0", cwd=parent)
    assert res.exit_code == 0, res.stderr
    assert (parent / "fix-a+b" / ".ow" / "config.toml").is_file()


def test_add_pr_refuses_overwriting_unpushed_local_pr_branch(lab):
    """Never silently `git branch -f` an existing local PR branch holding unpushed commits."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    _setup_pr_repo(
        lab,
        "enterprise",
        {"ent/__manifest__.py": "{}"},
        7778,
        "fork-18.0-unpushed",
        {"ent/pr.py": "pr = 1\n"},
    )
    bare = repos_dir() / "enterprise.git"
    # Pre-existing `18.0` triggers fallback to `pr-7778-18.0`, which already exists with an unpushed commit.
    lab.git(bare, "branch", "18.0", "origin/18.0")
    tmp_wt = lab.root / "tmp-unpushed"
    lab.git(bare, "worktree", "add", "-b", "pr-7778-18.0", str(tmp_wt), "origin/18.0")
    (tmp_wt / "local_wip.txt").write_text("do not lose me\n")
    lab.git(tmp_wt, "add", "local_wip.txt")
    lab.git(tmp_wt, "commit", "-m", "unpushed local wip")
    wip_sha = lab.git(tmp_wt, "rev-parse", "HEAD")
    lab.git(bare, "worktree", "remove", str(tmp_wt))

    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 7778,
                "title": "Fork PR",
                "url": "https://github.com/odoo/enterprise/pull/7778",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0",
                "baseRefName": "18.0",
            }
        ]
    )
    parent = lab.home / "odoo"
    parent.mkdir(parents=True)

    res = lab.ow("add", "enterprise#7778", cwd=parent)
    assert res.exit_code == 2
    assert "unpushed commits" in res.stderr
    assert lab.sha(bare, "refs/heads/pr-7778-18.0") == wip_sha
    assert not (parent / "7778-18.0").exists()


def test_add_rejects_duplicate_repo_in_composite_target(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    _setup_pr_repo(lab, "enterprise", {"ent/__manifest__.py": "{}"}, 101, "18.0-a", {"ent/a.py": "a\n"})
    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 101,
                "title": "PR A",
                "url": "https://github.com/odoo/enterprise/pull/101",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-a",
                "baseRefName": "18.0",
            },
            {
                "repo": "odoo/enterprise",
                "number": 102,
                "title": "PR B",
                "url": "https://github.com/odoo/enterprise/pull/102",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-b",
                "baseRefName": "18.0",
            },
        ]
    )
    parent = lab.home / "odoo"
    parent.mkdir(parents=True)

    res = lab.ow("add", "enterprise#101+enterprise#102", cwd=parent)
    assert res.exit_code == 2
    assert "more than once" in res.stderr


def test_add_rolls_back_completely_on_partial_repo_failure(lab):
    """If the second repo fails during `ow add`, roll back the first worktree and directory."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    # PR 9999 does NOT exist on the remote, so community succeeds and enterprise fetch fails.
    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 9999,
                "title": "Missing ref PR",
                "url": "https://github.com/odoo/enterprise/pull/9999",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-missing-ref",
                "baseRefName": "18.0",
            }
        ]
    )
    parent = lab.home / "odoo"
    parent.mkdir(parents=True)

    res = lab.ow("add", "enterprise#9999", cwd=parent)
    assert res.exit_code == 1
    assert not (parent / "9999-18.0-missing-ref").exists()
    wt_list = lab.git(repos_dir() / "community.git", "worktree", "list", "--porcelain")
    assert "9999-18.0-missing-ref" not in wt_list
