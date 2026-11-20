"""`ow init` scenarios: empty init, mono-stack, multi-stack, target DSL, resume, and repair."""

from __future__ import annotations

import json
import shutil

from ow.core.paths import repos_dir
from tests.conftest import add_repo, file_url


def _setup_pr_repo(lab, name: str, base: str, base_files: dict[str, str], pr_num: int, pr_branch: str, pr_files: dict[str, str]):
    remote = add_repo(lab, name, {base: base_files}, base=base)
    collab = lab.collaborator(f"author-{name}-{pr_num}", of=remote)
    collab.commit(pr_files, message=f"PR #{pr_num}", branch=pr_branch)
    collab.push(pr_branch)
    lab.publish_pr(remote, pr_num, pr_branch)
    return remote


def test_init_empty_creates_empty_workspace(lab):
    ws = lab.home / "empty-ws"
    ws.mkdir(parents=True)

    res = lab.ow("init", cwd=ws)
    assert res.exit_code == 0, res.stderr + res.stdout
    assert (ws / ".ow" / "config.toml").is_file()
    ls = lab.ow("ls")
    assert "empty-ws" in ls.stdout


def test_init_single_pr_creates_mono_stack_workspace_and_auto_expands_community(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    _setup_pr_repo(
        lab,
        "enterprise",
        "18.0",
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

    ws = lab.home / "my-review"
    ws.mkdir(parents=True)
    res = lab.ow("init", "enterprise#12345", cwd=ws)

    assert res.exit_code == 0, res.stderr + res.stdout
    assert (ws / "community" / "odoo-bin").is_file()
    assert (ws / "enterprise" / "sale_ent" / "fix.py").read_text() == "fixed = True\n"
    assert lab.git(ws / "enterprise", "symbolic-ref", "--short", "HEAD") == "18.0-fix-invoice-mic"
    # community is detached on origin/18.0
    assert lab.git(ws / "community", "rev-parse", "HEAD") == lab.sha(
        repos_dir() / "community.git", "refs/remotes/origin/18.0"
    )
    assert (ws / "odoorc").is_file()


def test_init_pr_discovers_sister_pr_on_community(lab):
    _setup_pr_repo(
        lab,
        "community",
        "18.0",
        lab.odoo_files(series="18.0"),
        9001,
        "18.0-sister-fix",
        {"addons/web/sister.py": "community_fix = True\n"},
    )
    _setup_pr_repo(
        lab,
        "enterprise",
        "18.0",
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

    ws = lab.home / "sister-ws"
    ws.mkdir(parents=True)
    res = lab.ow("init", "enterprise#9002", cwd=ws)

    assert res.exit_code == 0, res.stderr + res.stdout
    assert (ws / "community" / "addons" / "web" / "sister.py").read_text() == "community_fix = True\n"
    assert (ws / "enterprise" / "ent" / "sister.py").read_text() == "ent_fix = True\n"
    assert lab.git(ws / "community", "symbolic-ref", "--short", "HEAD") == "18.0-sister-fix"
    assert lab.git(ws / "enterprise", "symbolic-ref", "--short", "HEAD") == "18.0-sister-fix"


def test_init_multi_stack_named_and_composite_comma(lab):
    """Multi-version review: odoo=community#56789,enterprise#12345,upgrade#2345 iap=iap#4321 voip=voip."""
    add_repo(
        lab,
        "community",
        {"master": lab.odoo_files(series="master"), "18.0": lab.odoo_files(series="18.0")},
        base="master",
    )
    comm_collab = lab.collaborator("comm-pr", of=lab._tmp_root / "remotes" / "community.git")
    comm_collab.git("checkout", "master")
    comm_collab.commit({"addons/web/pr.py": "comm_pr = 1\n"}, message="comm pr", branch="master-voip-pr")
    comm_collab.push("master-voip-pr")
    lab.publish_pr(lab._tmp_root / "remotes" / "community.git", 56789, "master-voip-pr")

    _setup_pr_repo(
        lab,
        "enterprise",
        "master",
        {"voip_ent/__manifest__.py": "{}"},
        12345,
        "master-voip-pr",
        {"voip_ent/ent.py": "ent = 1\n"},
    )
    _setup_pr_repo(
        lab,
        "upgrade",
        "master",
        {"migrations/base.py": "base = 1\n"},
        2345,
        "master-voip-pr",
        {"migrations/upg.py": "upg = 1\n"},
    )
    _setup_pr_repo(
        lab,
        "iap",
        "18.0",
        {"iap_mod/__manifest__.py": "{}"},
        4321,
        "18.0-iap-pr",
        {"iap_mod/iap.py": "iap = 1\n"},
    )
    lab.git(repos_dir() / "iap.git", "config", "ow.github-repo", "odoo/iap-apps")
    add_repo(lab, "voip", {"master": {"server.py": "voip = 1\n"}}, base="master")

    lab.fake_gh(
        [
            {
                "repo": "odoo/odoo",
                "number": 56789,
                "title": "Comm PR",
                "url": "https://github.com/odoo/odoo/pull/56789",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "master-voip-pr",
                "baseRefName": "master",
            },
            {
                "repo": "odoo/enterprise",
                "number": 12345,
                "title": "Ent PR",
                "url": "https://github.com/odoo/enterprise/pull/12345",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "master-voip-pr",
                "baseRefName": "master",
            },
            {
                "repo": "odoo/upgrade",
                "number": 2345,
                "title": "Upgrade PR",
                "url": "https://github.com/odoo/upgrade/pull/2345",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "master-voip-pr",
                "baseRefName": "master",
            },
            {
                "repo": "odoo/iap-apps",
                "number": 4321,
                "title": "IAP PR",
                "url": "https://github.com/odoo/iap-apps/pull/4321",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-iap-pr",
                "baseRefName": "18.0",
            },
        ]
    )

    ws = lab.home / "voip-review"
    ws.mkdir(parents=True)
    res = lab.ow(
        "init",
        "odoo=community#56789,enterprise#12345,upgrade#2345",
        "iap=iap#4321",
        "voip=voip",
        cwd=ws,
    )
    assert res.exit_code == 0, res.stderr + res.stdout

    # Stack 1: community + enterprise + upgrade on master PRs
    assert (ws / "odoo" / "community" / "addons" / "web" / "pr.py").is_file()
    assert (ws / "odoo" / "enterprise" / "voip_ent" / "ent.py").is_file()
    assert (ws / "odoo" / "upgrade" / "migrations" / "upg.py").is_file()
    assert "http_port = 8069" in (ws / "odoo" / "odoorc").read_text()

    # Stack 2: iap PR #4321 + auto-expanded community detached on 18.0!
    assert (ws / "iap" / "iap" / "iap_mod" / "iap.py").is_file()
    assert (ws / "iap" / "community" / "odoo-bin").is_file()
    assert lab.git(ws / "iap" / "community", "rev-parse", "HEAD") == lab.sha(
        repos_dir() / "community.git", "refs/remotes/origin/18.0"
    )
    assert "http_port = 8070" in (ws / "iap" / "odoorc").read_text()

    # Stack 3: voip standalone detached on origin/master (no community)
    assert (ws / "voip" / "voip" / "server.py").is_file()
    assert not (ws / "voip" / "community").exists()

    # .code-workspace lists all three stacks
    code_ws = ws / "voip-review.code-workspace"
    assert code_ws.is_file()
    folders = [f["name"] for f in json.loads(code_ws.read_text())["folders"]]
    assert folders == ["iap", "odoo", "voip"]


def test_init_fresh_multi_stack_with_at_base_and_detached_no_branch_collision(lab):
    """Fresh multi-stack: `odoo=@master iap=iap@18.0 voip=voip` checks out upstream bases detached with zero collisions."""
    add_repo(
        lab,
        "community",
        {"master": lab.odoo_files(series="master"), "18.0": lab.odoo_files(series="18.0")},
        base="master",
    )
    add_repo(
        lab,
        "enterprise",
        {
            "master": {"ent/__manifest__.py": "{'version': 'master'}"},
            "18.0": {"ent/__manifest__.py": "{'version': '18.0'}"},
        },
        base="master",
    )
    add_repo(lab, "iap", {"18.0": {"iap_mod/__manifest__.py": "{}"}}, base="18.0")
    lab.git(repos_dir() / "iap.git", "config", "ow.github-repo", "odoo/iap-apps")
    add_repo(lab, "voip", {"master": {"README.md": "voip\n"}}, base="master")

    ws1 = lab.home / "fresh-1"
    ws1.mkdir(parents=True)
    r1 = lab.ow("init", "odoo=@master", "iap=iap@18.0", "voip=voip", cwd=ws1)
    assert r1.exit_code == 0, r1.stderr + r1.stdout

    # Creating a SECOND workspace with the exact same bases MUST succeed (all bases are detached!)
    ws2 = lab.home / "fresh-2"
    ws2.mkdir(parents=True)
    r2 = lab.ow("init", "odoo=@master", "iap=iap@18.0", "voip=voip", cwd=ws2)
    assert r2.exit_code == 0, r2.stderr + r2.stdout

    # Verify detached state (no local `master` or `18.0` branches created)
    assert lab.git(ws1 / "odoo" / "community", "branch", "--show-current") == ""
    assert lab.git(ws1 / "iap" / "iap", "branch", "--show-current") == ""
    assert lab.git(ws1 / "voip" / "voip", "branch", "--show-current") == ""
    assert (ws1 / "iap" / "community" / "odoo-bin").is_file()


def test_init_unnamed_multi_targets_auto_name_and_deduplicate(lab):
    """Passing 2+ unnamed targets to `ow init` creates a multi-stack workspace with deduplicated auto names."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")}, base="18.0")
    add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}}, base="18.0")
    add_repo(lab, "voip", {"master": {"README.md": "voip\n"}}, base="master")

    ws = lab.home / "auto-multi"
    ws.mkdir(parents=True)
    res = lab.ow("init", "@18.0", "@18.0", "voip", cwd=ws)
    assert res.exit_code == 0, res.stderr + res.stdout
    assert (ws / "18.0" / "community" / "odoo-bin").is_file()
    assert (ws / "18.0-2" / "community" / "odoo-bin").is_file()
    assert (ws / "voip" / "voip" / "README.md").is_file()


def test_init_branch_target_discovers_remote_and_infers_odoo_base(lab):
    """`ow init enterprise@18.0-port-in` finds `dev` remote, infers `origin/18.0`, and auto-adds `community`."""
    add_repo(
        lab,
        "community",
        {"master": lab.odoo_files(series="master"), "18.0": lab.odoo_files(series="18.0")},
        base="master",
    )
    add_repo(
        lab,
        "enterprise",
        {
            "master": {"sale_ent/__manifest__.py": "{'version': 'master'}"},
            "18.0": {"sale_ent/__manifest__.py": "{'version': '18.0'}"},
        },
        base="master",
    )
    dev_remote = lab.remote(
        "enterprise-dev",
        {"18.0-port-in": {"sale_ent/__manifest__.py": "{}", "sale_ent/dev.py": "from_dev = True\n"}},
        head="18.0-port-in",
    )
    lab.git(repos_dir() / "enterprise.git", "remote", "add", "dev", file_url(dev_remote))

    ws = lab.home / "dev-branch"
    ws.mkdir(parents=True)
    res = lab.ow("init", "enterprise@18.0-port-in", cwd=ws)
    assert res.exit_code == 0, res.stderr + res.stdout
    assert (ws / "community" / "odoo-bin").is_file()
    assert (ws / "enterprise" / "sale_ent" / "dev.py").read_text() == "from_dev = True\n"
    assert lab.git(ws / "enterprise", "branch", "--show-current") == "18.0-port-in"
    assert lab.git(ws / "enterprise", "config", "--get", "branch.18.0-port-in.remote") == "dev"
    assert lab.git(ws / "enterprise", "config", "--get", "branch.18.0-port-in.ow-base") == "origin/18.0"


def test_init_resumes_interrupted_workspace_with_broken_worktree(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    _setup_pr_repo(
        lab,
        "enterprise",
        "18.0",
        {"sale_ent/__manifest__.py": "{}"},
        135585,
        "18.0-port-in-numbers",
        {"sale_ent/port.py": "port = True\n"},
    )
    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 135585,
                "title": "Port in numbers",
                "url": "https://github.com/odoo/enterprise/pull/135585",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-port-in-numbers",
                "baseRefName": "18.0",
            }
        ]
    )

    ws = lab.home / "interrupted"
    ws.mkdir(parents=True)
    comm_bare = repos_dir() / "community.git"
    ent_bare = repos_dir() / "enterprise.git"
    lab.git(comm_bare, "worktree", "add", "--detach", str(ws / "community"), "origin/18.0")
    lab.git(ent_bare, "fetch", "origin", "+refs/pull/135585/head:refs/ow/pr/135585")
    lab.git(
        ent_bare,
        "worktree",
        "add",
        "--no-track",
        "-b",
        "18.0-port-in-numbers",
        str(ws / "enterprise"),
        "refs/ow/pr/135585",
    )
    ent_admin = next((ent_bare / "worktrees").iterdir())
    (ent_admin / "locked").write_text("initializing\n")

    res = lab.ow("init", "enterprise#135585", cwd=ws)
    assert res.exit_code == 0, res.stderr + res.stdout
    assert (ws / ".ow" / "config.toml").is_file()
    assert (ws / "enterprise" / "sale_ent" / "port.py").read_text() == "port = True\n"
    assert lab.git(ws / "enterprise", "branch", "--show-current") == "18.0-port-in-numbers"


def test_init_repairs_moved_workspace_and_multi_stack_workspace(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    orig = lab.home / "orig-ws"
    orig.mkdir(parents=True)
    assert lab.ow("init", "a=community@18.0", "b=community@18.0", cwd=orig).exit_code == 0

    moved = lab.home / "moved-ws"
    shutil.move(str(orig), str(moved))

    repair = lab.ow("init", cwd=moved)
    assert repair.exit_code == 0, repair.stderr + repair.stdout
    assert (moved / "moved-ws.code-workspace").is_file()
    st = lab.ow("status", cwd=moved)
    assert st.exit_code == 0
    assert "prunable" not in st.stdout


def test_init_refuses_parent_dir_holding_standalone_workspaces_and_duplicate_repos(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    dev_root = lab.home / "dev"
    child = dev_root / "ws-one"
    child.mkdir(parents=True)
    assert lab.ow("init", "community@18.0", cwd=child).exit_code == 0

    # Running `ow init` in ~/dev (parent of standalone workspace) must refuse
    bad_parent = lab.ow("init", "@18.0", cwd=dev_root)
    assert bad_parent.exit_code == 2
    assert "already contains standalone ow workspaces" in bad_parent.stderr

    # Duplicate repo in composite `,` target must refuse
    ws2 = lab.home / "dup"
    ws2.mkdir(parents=True)
    dup = lab.ow("init", "community@18.0,community@18.0", cwd=ws2)
    assert dup.exit_code == 2
    assert "more than once" in dup.stderr


def test_init_dry_run_never_mutates_disk_or_index(lab):
    from ow.core import index

    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})

    empty_dir = lab.home / "dry-empty"
    empty_dir.mkdir(parents=True)
    r_empty = lab.ow("init", "-n", cwd=empty_dir)
    assert r_empty.exit_code == 0, r_empty.stderr
    assert "initialize empty workspace" in r_empty.stdout
    assert not (empty_dir / ".ow").exists()
    assert index.read() == ()

    mono_dir = lab.home / "dry-mono"
    mono_dir.mkdir(parents=True)
    r_mono = lab.ow("init", "-n", "@18.0", cwd=mono_dir)
    assert r_mono.exit_code == 0, r_mono.stderr
    assert "worktree add" in r_mono.stdout
    assert not (mono_dir / ".ow").exists()
    assert not (mono_dir / "community").exists()
    assert index.read() == ()

    multi_dir = lab.home / "dry-multi"
    multi_dir.mkdir(parents=True)
    r_multi = lab.ow("init", "-n", "a=@18.0", "b=@18.0", "--json", cwd=multi_dir)
    assert r_multi.exit_code == 0, r_multi.stderr
    payload = json.loads(r_multi.stdout)
    assert payload["result"]["dry_run"] is True
    assert payload["result"]["multi_stack"] is True
    assert [w["action"] for w in payload["result"]["workspaces"]] == ["planned", "planned"]
    assert [w["ports"] for w in payload["result"]["workspaces"]] == [
        {"http": 8069, "gevent": 8072},
        {"http": 8070, "gevent": 8073},
    ]
    assert list(multi_dir.iterdir()) == []
    assert index.read() == ()


def test_init_does_not_treat_empty_dot_ow_dir_as_resumable_workspace(lab):
    from ow.commands._workspace import is_resumable_workspace

    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})

    ws = lab.home / "empty-dot-ow"
    (ws / ".ow").mkdir(parents=True)
    assert is_resumable_workspace(ws) is False

    res = lab.ow("init", "@18.0", cwd=ws)
    assert res.exit_code == 0, res.stderr + res.stdout
    assert (ws / ".ow" / "config.toml").is_file()
    assert (ws / "community" / "odoo-bin").is_file()
    assert is_resumable_workspace(ws) is True
