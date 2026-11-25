"""`ow init` scenarios: empty init, mono-stack, multi-stack, target DSL, resume, and repair."""

from __future__ import annotations

import json
import shutil

from ow.core.paths import repos_dir
from tests.conftest import add_repo, file_url, git_ok, setup_pr_repo


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
    setup_pr_repo(lab,
    "enterprise",
    "18.0",
    {"sale_ent/__manifest__.py": "{'name': 'Sale Ent'}"},
    12345,
    "18.0-fix-invoice-mic",
    {"sale_ent/fix.py": "fixed = True\n"},)
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
    setup_pr_repo(lab,
    "community",
    "18.0",
    lab.odoo_files(series="18.0"),
    9001,
    "18.0-sister-fix",
    {"addons/web/sister.py": "community_fix = True\n"},)
    setup_pr_repo(lab,
    "enterprise",
    "18.0",
    {"ent/__manifest__.py": "{}"},
    9002,
    "18.0-sister-fix",
    {"ent/sister.py": "ent_fix = True\n"},)
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

    setup_pr_repo(lab,
    "enterprise",
    "master",
    {"voip_ent/__manifest__.py": "{}"},
    12345,
    "master-voip-pr",
    {"voip_ent/ent.py": "ent = 1\n"},)
    setup_pr_repo(lab,
    "upgrade",
    "master",
    {"migrations/base.py": "base = 1\n"},
    2345,
    "master-voip-pr",
    {"migrations/upg.py": "upg = 1\n"},)
    setup_pr_repo(lab,
    "iap",
    "18.0",
    {"iap_mod/__manifest__.py": "{}"},
    4321,
    "18.0-iap-pr",
    {"iap_mod/iap.py": "iap = 1\n"},)
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
    setup_pr_repo(lab,
    "enterprise",
    "18.0",
    {"sale_ent/__manifest__.py": "{}"},
    135585,
    "18.0-port-in-numbers",
    {"sale_ent/port.py": "port = True\n"},)
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
    r_multi = lab.ow("init", "-n", "a=@18.0", "b=@18.0", cwd=multi_dir)
    assert r_multi.exit_code == 0, r_multi.stderr
    planned = [line for line in r_multi.stdout.splitlines() if line.startswith("planned ")]
    assert [line.split()[1] for line in planned] == ["a", "b"]
    assert "(port 8069)" in planned[0] and "(port 8070)" in planned[1]
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


def test_init_suffixes_the_db_name_when_another_workspace_resolves_to_the_same_one(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    first = lab.home / "one" / "voip"
    second = lab.home / "two" / "voip"
    third = lab.home / "three" / "voip"
    for path in (first, second, third):
        path.mkdir(parents=True)

    assert lab.ow("init", "community@18.0", cwd=first).exit_code == 0
    assert lab.ow("init", "community@18.0", cwd=second).exit_code == 0
    assert lab.ow("init", "community@18.0", cwd=third).exit_code == 0

    assert "db_name" not in (first / ".ow" / "config.toml").read_text()
    assert lab.ow("config", "get", "odoo.db_name", cwd=first).stdout.strip() == "voip"
    assert lab.ow("config", "get", "odoo.db_name", cwd=second).stdout.strip() == "voip-2"
    assert lab.ow("config", "get", "odoo.db_name", cwd=third).stdout.strip() == "voip-3"
    assert 'db_name = "voip-2"' in (second / ".ow" / "config.toml").read_text()
    assert "db_name = voip-2" in (second / "odoorc").read_text()


def test_init_migrates_a_workspace_created_by_3_0_0a1(lab):
    """Q18: `[repos]` in the `<base>..<feature>` format becomes the default base, and `ow-base` lands on the branch."""
    import tomllib

    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = lab.home / "from-a1"
    ws.mkdir(parents=True)
    assert lab.ow("init", "community@18.0", cwd=ws).exit_code == 0
    bare = repos_dir() / "community.git"
    lab.git(ws / "community", "switch", "--no-track", "-c", "18.0-mine", "origin/18.0")
    assert not git_ok(bare, "config", "--get", "branch.18.0-mine.ow-base")
    config = ws / ".ow" / "config.toml"
    config.write_text('version = 2\n\n[repos]\ncommunity = "18.0..18.0-mine"\n')

    refused = lab.ow("status", cwd=ws)
    assert "run `ow init` to migrate it" in refused.stderr
    assert refused.stderr.count("older ow format (version)") == 1

    dry = lab.ow("init", "-n", cwd=ws)
    assert dry.exit_code == 0, dry.stderr + dry.stdout
    assert "# version: removed" in dry.stdout
    assert "branch.18.0-mine.ow-base origin/18.0" in dry.stdout
    assert config.read_text().startswith("version = 2")

    res = lab.ow("init", cwd=ws)

    assert res.exit_code == 0, res.stderr + res.stdout
    assert tomllib.loads(config.read_text()) == {"repos": {"community": "origin/18.0"}}
    assert lab.git(bare, "config", "branch.18.0-mine.ow-base") == "origin/18.0"
    assert "dropped repos.community" not in res.stderr
    assert "3.0.0a1" not in lab.ow("status", cwd=ws).stderr


def test_init_dry_run_migration_changes_nothing(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = lab.home / "from-a1-dry"
    ws.mkdir(parents=True)
    assert lab.ow("init", "community@18.0", cwd=ws).exit_code == 0
    lab.git(ws / "community", "switch", "--no-track", "-c", "18.0-mine", "origin/18.0")
    config = ws / ".ow" / "config.toml"
    legacy = 'version = 2\n\n[repos]\ncommunity = "18.0..18.0-mine"\n'
    config.write_text(legacy)

    res = lab.ow("init", "-n", cwd=ws)

    assert res.exit_code == 0, res.stderr + res.stdout
    assert "branch.18.0-mine.ow-base origin/18.0" in res.stdout
    assert config.read_text() == legacy
    assert not git_ok(repos_dir() / "community.git", "config", "--get", "branch.18.0-mine.ow-base")


def test_init_multi_stack_spells_a_base_and_branch_per_stack(lab):
    """Q24: `odoo=@18.0..18.0-fix iap=iap@18.0..topic` gives each stack its branches over a base."""
    import tomllib

    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    add_repo(lab, "iap", {"18.0": {"iap_mod/__manifest__.py": "{}"}})
    root = lab.home / "per-stack"
    root.mkdir(parents=True)

    res = lab.ow("init", "odoo=@18.0..18.0-fix", "iap=iap@18.0..topic", cwd=root)

    assert res.exit_code == 0, res.stderr + res.stdout
    for repo in ("community", "enterprise"):
        assert lab.git(root / "odoo" / repo, "branch", "--show-current") == "18.0-fix"
        bare = repos_dir() / f"{repo}.git"
        assert lab.git(bare, "config", "branch.18.0-fix.ow-base") == "origin/18.0"
        assert lab.git(bare, "config", "branch.18.0-fix.remote") == "origin"
        assert lab.git(bare, "config", "branch.18.0-fix.merge") == "refs/heads/18.0-fix"
    assert lab.git(root / "iap" / "iap", "branch", "--show-current") == "topic"
    assert lab.git(repos_dir() / "iap.git", "config", "branch.topic.ow-base") == "origin/18.0"
    stacks = tomllib.loads((root / ".ow" / "config.toml").read_text())["stacks"]
    assert stacks["odoo"]["repos"]["community"] == "origin/18.0"
    assert stacks["iap"]["repos"]["iap"] == "origin/18.0"


def test_init_spells_a_base_and_branch_in_a_mono_stack(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    remote = add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    ws = lab.home / "mono-range"
    ws.mkdir(parents=True)

    res = lab.ow("init", "community@18.0..18.0-mine,enterprise@18.0..18.0-mine", cwd=ws)

    assert res.exit_code == 0, res.stderr + res.stdout
    assert lab.git(ws / "enterprise", "branch", "--show-current") == "18.0-mine"
    assert git_ok(ws / "enterprise", "push")
    assert git_ok(remote, "rev-parse", "--verify", "refs/heads/18.0-mine")


def test_init_refuses_malformed_ranges_before_creating_anything(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = lab.home / "bad-range"
    ws.mkdir(parents=True)

    for spec in ("community@..topic", "community@18.0..", "@18.0..", "community@18.0..-x"):
        res = lab.ow("init", spec, cwd=ws)
        assert res.exit_code == 2, spec
    assert list(ws.iterdir()) == []


def test_init_refuses_the_branch_option_it_no_longer_has(lab):
    ws = lab.home / "no-branch-option"
    ws.mkdir(parents=True)

    assert lab.ow("init", "--branch", "x", cwd=ws).exit_code == 2
    assert lab.ow("add", "--branch", "x", "community", cwd=ws).exit_code == 2


def test_init_plans_every_stack_before_touching_any(lab):
    """Q11: a refusal in the second stack leaves the first one uncreated, nothing cloned or added."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    root = lab.home / "all-or-nothing"
    root.mkdir(parents=True)
    (root / "second").mkdir()
    (root / "second" / "stray.txt").write_text("a directory ow did not create\n")

    res = lab.ow("init", "first=community@18.0", "second=community@18.0", cwd=root)

    assert res.exit_code == 2
    assert not (root / "first").exists()
    assert not (root / ".ow").exists()


def test_a_failed_repo_undoes_a_new_workspace_and_keeps_an_existing_one(lab, tmp_path):
    """The plan runs as one batch: a repo that cannot be cloned rolls a new workspace back whole."""
    from ow.core import index
    from tests.conftest import route_github

    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    route_github(odoo=tmp_path / "nowhere")  # `upgrade` is a known repo whose URL leads nowhere
    ws = lab.home / "fails-new"
    ws.mkdir(parents=True)

    res = lab.ow("init", "community@18.0,upgrade", cwd=ws)

    assert res.exit_code == 1
    assert not (ws / "community").exists()
    assert not (ws / ".ow").exists()
    assert index.read() == ()
    listing = lab.git(repos_dir() / "community.git", "worktree", "list", "--porcelain")
    assert str(ws) not in listing
    assert not (repos_dir() / "upgrade.git").exists()

    keep = lab.home / "fails-existing"
    keep.mkdir(parents=True)
    assert lab.ow("init", "community@18.0", cwd=keep).exit_code == 0

    again = lab.ow("add", "upgrade", cwd=keep)

    assert again.exit_code == 1
    assert (keep / "community" / "odoo-bin").is_file()
    assert not (keep / "upgrade").exists()
    assert "upgrade" not in (keep / ".ow" / "config.toml").read_text()


def test_add_puts_several_specs_into_one_plan_and_refuses_a_repo_given_twice(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "upgrade", {"18.0": {"migrations/u.py": "u = 1\n"}})
    add_repo(lab, "iap", {"18.0": {"iap_mod/__manifest__.py": "{}"}})
    ws = lab.home / "several-specs"
    ws.mkdir(parents=True)
    assert lab.ow("init", "community@18.0", cwd=ws).exit_code == 0

    twice = lab.ow("add", "upgrade@18.0", "upgrade@18.0..again", cwd=ws)
    assert twice.exit_code == 2
    assert "given more than once" in twice.stderr
    assert not (ws / "upgrade").exists()

    res = lab.ow("add", "upgrade@18.0", "iap@18.0", cwd=ws)

    assert res.exit_code == 0, res.stderr + res.stdout
    assert (ws / "upgrade" / "migrations" / "u.py").is_file()
    assert (ws / "iap" / "iap_mod" / "__manifest__.py").is_file()


def test_an_interrupted_init_leaves_no_worktree_behind(lab, monkeypatch, tmp_path):
    """Ctrl+C in the middle of the batch removes what the plan already added, as a failure does."""
    from ow.commands import _workspace
    from tests.conftest import route_github

    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    route_github(odoo=tmp_path / "nowhere")

    def interrupted(name, url):
        raise KeyboardInterrupt

    monkeypatch.setattr(_workspace, "ensure_bare", interrupted)
    ws = lab.home / "ctrl-c"
    ws.mkdir(parents=True)

    res = lab.ow("init", "community@18.0,upgrade", cwd=ws)

    assert res.exit_code != 0
    assert list(ws.iterdir()) == []
    listing = lab.git(repos_dir() / "community.git", "worktree", "list", "--porcelain")
    assert str(ws) not in listing


_V1_CONFIG = """version = 1
templates = ["odoorc", "launch.json"]

[vars]
http_port = 8070
db_host = "db.local"
db_port = 5433
db_user = "from-vars"
db_password = "pw"
admin_passwd = "admin"
smtp_server = "mail.local"
smtp_port = 1025
custom_thing = "kept"

[odoo]
db_user = "already-set"
"""


def test_init_migrates_a_v1_workspace_config(lab):
    """Q18: `version`, `templates` go; `[vars]` keys that are `odoo.*` settings move, the rest stay."""
    import tomllib

    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = lab.home / "from-v1"
    ws.mkdir(parents=True)
    assert lab.ow("init", "community@18.0", cwd=ws).exit_code == 0
    config = ws / ".ow" / "config.toml"
    config.write_text(_V1_CONFIG)

    outside = lab.ow("status", cwd=ws)
    assert outside.stderr.count("older ow format") == 1
    assert "run `ow init` to migrate it" in outside.stderr
    older = outside.stderr.split("older ow format (")[1].split(")")[0]
    assert "vars.custom_thing" not in older  # `ow init` does not migrate it: no advice to run it
    assert "vars.http_port" in older
    assert "unknown setting vars.custom_thing" in outside.stderr

    dry = lab.ow("init", "-n", cwd=ws)

    assert dry.exit_code == 0, dry.stderr + dry.stdout
    assert "# version: removed" in dry.stdout
    assert "# templates: removed" in dry.stdout
    assert "# vars.http_port moves to odoo.http_port" in dry.stdout
    assert "vars.db_user dropped: [odoo] already sets odoo.db_user" in dry.stderr
    assert config.read_text() == _V1_CONFIG

    res = lab.ow("init", cwd=ws)

    assert res.exit_code == 0, res.stderr + res.stdout
    assert tomllib.loads(config.read_text()) == {
        "repos": {"community": "origin/18.0"},
        "odoo": {
            "http_port": 8070,
            "db_host": "db.local",
            "db_port": 5433,
            "db_user": "already-set",
            "db_password": "pw",
            "admin_passwd": "admin",
            "smtp_server": "mail.local",
            "smtp_port": 1025,
        },
        "vars": {"custom_thing": "kept"},
    }
    after = lab.ow("status", cwd=ws).stderr
    assert "older ow format" not in after  # nothing left for `ow init` to do: no loop
    assert "unknown setting vars.custom_thing" in after
    assert lab.ow("config", "get", "odoo.http_port", cwd=ws).stdout.strip() == "8070"


def test_init_migrates_the_version_key_of_a_multi_stack_root(lab):
    import tomllib

    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    root = lab.home / "multi-a1"
    root.mkdir(parents=True)
    assert lab.ow("init", "one=community@18.0", "two=community@18.0", cwd=root).exit_code == 0
    config = root / ".ow" / "config.toml"
    config.write_text("version = 2\n\n" + config.read_text())

    res = lab.ow("init", cwd=root)

    assert res.exit_code == 0, res.stderr + res.stdout
    data = tomllib.loads(config.read_text())
    assert "version" not in data
    assert set(data["stacks"]) == {"one", "two"}
