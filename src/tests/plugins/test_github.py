"""Unit & integration tests for the built-in `github` plugin (`gh` CLI + TTL cache)."""

from __future__ import annotations

from ow.core.paths import repos_dir
from ow.core.plugins import KnownRepo
from ow.plugins import github
from ow.plugins._github import BareCatalog, parse_pr_spec
from tests.conftest import add_repo, init_workspace


def test_parse_pr_target_variants():
    assert parse_pr_spec("enterprise#12345") == (None, "enterprise", 12345)
    assert parse_pr_spec("odoo/iap-apps#42") == ("odoo", "iap-apps", 42)
    assert parse_pr_spec("odoo/odoo/pull/1234") == ("odoo", "odoo", 1234)
    assert parse_pr_spec("odoo/odoo/pull/1234/files") == ("odoo", "odoo", 1234)
    assert parse_pr_spec("iap/pull/1992") == (None, "iap", 1992)
    assert parse_pr_spec("github.com/odoo/iap-apps/pull/1234") == ("odoo", "iap-apps", 1234)
    assert parse_pr_spec("https://github.com/odoo/iap-apps/pull/1234") == (
        "odoo",
        "iap-apps",
        1234,
    )
    assert parse_pr_spec("https://github.com/odoo/enterprise/pull/12345") == (
        "odoo",
        "enterprise",
        12345,
    )
    assert parse_pr_spec("https://github.com/odoo/enterprise/pull/12345/files") == (
        "odoo",
        "enterprise",
        12345,
    )
    assert parse_pr_spec("github.com/odoo/pull/1234") is None
    assert parse_pr_spec("18.0-my-fix") is None


def test_resolve_spec_maps_alias_and_discovers_sister_pr(lab):
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
            },
            {
                "repo": "odoo/odoo",
                "number": 99999,
                "title": "Fix invoice (community)",
                "url": "https://github.com/odoo/odoo/pull/99999",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-fix-invoice-mic",
                "baseRefName": "18.0",
            },
        ]
    )
    catalog = {
        "community": KnownRepo(name="community", url="git@github.com:odoo/odoo.git"),
        "enterprise": KnownRepo(name="enterprise", url="git@github.com:odoo/enterprise.git"),
    }

    resolved = github.ow_resolve_spec("enterprise#12345", catalog, {"github.default_org": "odoo"})

    assert resolved is not None
    assert resolved.name == "18.0-fix-invoice-mic"
    assert resolved.source == "github:odoo/enterprise#12345"
    by_name = {r.name: r for r in resolved.repos}
    assert set(by_name) == {"enterprise", "community"}
    assert by_name["enterprise"].fetch_ref == "refs/pull/12345/head"
    assert by_name["community"].fetch_ref == "refs/pull/99999/head"
    assert by_name["community"].branch == "18.0-fix-invoice-mic"


def test_resolve_spec_odoo_alias_maps_to_community(lab):
    lab.fake_gh(
        [
            {
                "repo": "odoo/odoo",
                "number": 777,
                "title": "Core fix",
                "url": "https://github.com/odoo/odoo/pull/777",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-core-fix",
                "baseRefName": "18.0",
            }
        ]
    )
    catalog = {
        "community": KnownRepo(name="community", url="git@github.com:odoo/odoo.git"),
    }

    resolved = github.ow_resolve_spec("odoo#777", catalog, {"github.default_org": "odoo"})
    assert resolved is not None
    assert resolved.name == "18.0-core-fix"
    assert [r.name for r in resolved.repos] == ["community"]

    by_path = github.ow_resolve_spec("odoo/odoo/pull/777", catalog, {"github.default_org": "odoo"})
    assert by_path is not None
    assert by_path.source == "github:odoo/odoo#777"
    assert [r.name for r in by_path.repos] == ["community"]


def test_status_uses_ttl_cache_and_respects_offline(lab):
    log_path = lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 15402,
                "title": "VOIP fix",
                "url": "https://github.com/odoo/enterprise/pull/15402",
                "state": "OPEN",
                "isDraft": True,
                "reviewDecision": "",
                "headRefName": "18.0-voip-fix",
                "baseRefName": "18.0",
            }
        ]
    )
    add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    lab.git(repos_dir() / "enterprise.git", "config", "ow.github-repo", "odoo/enterprise")
    ws = init_workspace(lab, "enterprise", branch="18.0-voip-fix")

    # Offline with cold cache: zero gh calls
    offline_first = lab.ow("status", "--offline", cwd=ws)
    assert offline_first.exit_code == 0
    assert not log_path.exists()

    # Online call hits gh once and caches
    online = lab.ow("status", cwd=ws)
    assert online.exit_code == 0
    assert "#15402 DRAFT" in online.stdout
    calls_after_first = len(log_path.read_text().splitlines())
    assert calls_after_first == 1

    # Second call within TTL hits cache: still 1 gh call!
    second = lab.ow("status", cwd=ws)
    assert second.exit_code == 0
    assert "#15402 DRAFT" in second.stdout
    assert len(log_path.read_text().splitlines()) == calls_after_first


def test_prune_candidates_detects_merged_pr_and_skips_dirty(lab):
    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 555,
                "title": "Merged fix",
                "url": "https://github.com/odoo/enterprise/pull/555",
                "state": "MERGED",
                "isDraft": False,
                "headRefName": "18.0-merged-fix",
                "baseRefName": "18.0",
            }
        ]
    )
    add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    lab.git(repos_dir() / "enterprise.git", "config", "ow.github-repo", "odoo/enterprise")
    ws = init_workspace(lab, "enterprise", name="clean-merged", branch="18.0-merged-fix")

    # Offline skips merged-PR pruning
    offline_prune = lab.ow("prune", "-n", "--offline")
    assert offline_prune.exit_code == 0
    assert "clean-merged" not in offline_prune.stdout

    # Online dry-run detects the merged PR workspace
    dry = lab.ow("prune", "-n")
    assert dry.exit_code == 0
    assert "prune workspace clean-merged: PR #555 merged" in dry.stdout
    assert ws.exists()

    # Make it dirty -> prune safely refuses to remove it
    (ws / "enterprise" / "dirty.txt").write_text("unsaved\n")
    dirty_prune = lab.ow("prune", "-y")
    assert dirty_prune.exit_code == 0
    assert "skip pruning clean-merged" in dirty_prune.stdout
    assert ws.exists()

    # Clean it -> prune removes it cleanly
    (ws / "enterprise" / "dirty.txt").unlink()
    real_prune = lab.ow("prune", "-y")
    assert real_prune.exit_code == 0
    assert not ws.exists()


def test_parse_pr_url_with_fragment_and_query():
    assert parse_pr_spec(
        "https://github.com/odoo/enterprise/pull/12345#issuecomment-999"
    ) == ("odoo", "enterprise", 12345)
    assert parse_pr_spec(
        "https://github.com/odoo/enterprise/pull/12345?w=1"
    ) == ("odoo", "enterprise", 12345)


def test_status_external_target_resolves_alias_and_caches(lab):

    log_path = lab.fake_gh(
        [
            {
                "repo": "odoo/odoo",
                "number": 4242,
                "title": "Community PR",
                "url": "https://github.com/odoo/odoo/pull/4242",
                "state": "OPEN",
                "isDraft": False,
                "reviewDecision": "APPROVED",
                "headRefName": "18.0-comm-pr",
                "baseRefName": "18.0",
            }
        ]
    )

    res = lab.ow("status", "community#4242")
    assert res.exit_code == 0, res.stderr
    plugin, subject, summary = res.stdout.splitlines()[0].split("\t")
    assert subject == "odoo/odoo#4242"
    assert "#4242 OPEN (approved)" in summary
    assert len(log_path.read_text().splitlines()) == 1

    # Offline serves stale/warm cache for the same PR
    off = lab.ow("status", "community#4242", "--offline")
    assert off.exit_code == 0, off.stderr
    assert "#4242 OPEN (approved)" in off.stdout
    assert len(log_path.read_text().splitlines()) == 1


def test_prune_does_not_delete_workspace_with_untracked_wip_repo_or_base_branch(lab):
    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 555,
                "title": "Merged fix",
                "url": "https://github.com/odoo/enterprise/pull/555",
                "state": "MERGED",
                "isDraft": False,
                "headRefName": "18.0-merged-fix",
                "baseRefName": "18.0",
            },
            {
                "repo": "odoo/odoo",
                "number": 111,
                "title": "Random closed PR from fork named 18.0",
                "url": "https://github.com/odoo/odoo/pull/111",
                "state": "CLOSED",
                "isDraft": False,
                "headRefName": "18.0",
                "baseRefName": "18.0",
            },
        ]
    )
    add_repo(lab, "community", {"18.0": lab.odoo_files()})
    add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    lab.git(repos_dir() / "community.git", "config", "ow.github-repo", "odoo/odoo")
    lab.git(repos_dir() / "enterprise.git", "config", "ow.github-repo", "odoo/enterprise")

    # Workspace 1: enterprise on merged PR, community on local WIP branch with no PR -> MUST NOT prune
    ws_wip = init_workspace(lab, "community", "enterprise", name="mixed-wip", branch="18.0-local-wip")
    lab.git(ws_wip / "enterprise", "switch", "-c", "18.0-merged-fix")

    # Workspace 2: community on base branch 18.0 -> MUST NOT match random fork PR named 18.0
    init_workspace(lab, "community", name="base-ws", branch="18.0")

    dry = lab.ow("prune", "-n")
    assert dry.exit_code == 0
    assert "mixed-wip" not in dry.stdout
    assert "base-ws" not in dry.stdout


def test_resolve_spec_uses_matching_non_origin_remote_for_base(lab):
    """When the GitHub repo matches `upstream` rather than `origin`, base uses `upstream/<baseRef>`."""
    add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    bare = repos_dir() / "enterprise.git"
    lab.git(bare, "remote", "add", "upstream", "git@github.com:odoo/enterprise.git")
    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 321,
                "title": "Upstream remote PR",
                "url": "https://github.com/odoo/enterprise/pull/321",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-up-fix",
                "baseRefName": "18.0",
            }
        ]
    )
    known = {"enterprise": KnownRepo(name="enterprise", url="git@github.com:odoo/enterprise.git")}
    resolved = github.ow_resolve_spec("enterprise#321", known, {"github.default_org": "odoo"})
    assert resolved is not None
    assert resolved.repos[0].remote == "upstream"
    assert resolved.repos[0].base == "upstream/18.0"


def test_sister_pr_filters_by_head_owner(lab):
    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 100,
                "title": "Alice fix",
                "url": "https://github.com/odoo/enterprise/pull/100",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "patch-1",
                "baseRefName": "18.0",
                "headRepositoryOwner": {"login": "alice"},
                "author": {"login": "alice"},
            },
            {
                "repo": "odoo/odoo",
                "number": 200,
                "title": "Bob unrelated patch-1",
                "url": "https://github.com/odoo/odoo/pull/200",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "patch-1",
                "baseRefName": "18.0",
                "headRepositoryOwner": {"login": "bob"},
                "author": {"login": "bob"},
            },
        ]
    )
    catalog = {
        "community": KnownRepo(name="community", url="git@github.com:odoo/odoo.git"),
        "enterprise": KnownRepo(name="enterprise", url="git@github.com:odoo/enterprise.git"),
    }
    resolved = github.ow_resolve_spec("enterprise#100", catalog, {"github.default_org": "odoo"})
    assert resolved is not None
    assert [r.name for r in resolved.repos] == ["enterprise"]


def test_resolve_spec_uses_local_bare_origin_slug_and_skips_duplicate_sister(lab):
    """A registered local bare `iap.git` -> `odoo/iap-apps` resolves `iap#1992` without duplicating `iap-apps`."""
    bare = repos_dir() / "iap.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    lab.git(bare.parent, "init", "--bare", str(bare))
    lab.git(bare, "remote", "add", "origin", "git@github.com:odoo/iap-apps.git")

    lab.fake_gh(
        [
            {
                "repo": "odoo/iap-apps",
                "number": 1992,
                "title": "IAP port-in",
                "url": "https://github.com/odoo/iap-apps/pull/1992",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "master-port-in-numbers-basm",
                "baseRefName": "master",
            }
        ]
    )
    catalog = {
        "community": KnownRepo(name="community", url="git@github.com:odoo/odoo.git"),
        "iap-apps": KnownRepo(name="iap-apps", url="git@github.com:odoo/iap-apps.git"),
    }

    resolved = github.ow_resolve_spec("iap#1992", catalog, {"github.default_org": "odoo"})
    assert resolved is not None
    assert resolved.source == "github:odoo/iap-apps#1992"
    assert [r.name for r in resolved.repos] == ["iap"]
    assert resolved.repos[0].url == "git@github.com:odoo/iap-apps.git"
    assert resolved.repos[0].base == "origin/master"


def test_resolve_spec_uses_local_bare_for_non_default_org(lab):
    """`brboi-addons#42` and `brboi/odoo-addons#42` both resolve via the registered bare `brboi-addons.git`."""
    bare = repos_dir() / "brboi-addons.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    lab.git(bare.parent, "init", "--bare", str(bare))
    lab.git(bare, "remote", "add", "origin", "git@github.com:brboi/odoo-addons.git")

    lab.fake_gh(
        [
            {
                "repo": "brboi/odoo-addons",
                "number": 42,
                "title": "Custom addon fix",
                "url": "https://github.com/brboi/odoo-addons/pull/42",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-custom",
                "baseRefName": "18.0",
            }
        ]
    )

    short = github.ow_resolve_spec("brboi-addons#42", {}, {"github.default_org": "odoo"})
    assert short is not None
    assert short.source == "github:brboi/odoo-addons#42"
    assert [r.name for r in short.repos] == ["brboi-addons"]

    qualified = github.ow_resolve_spec("brboi/odoo-addons#42", {}, {"github.default_org": "odoo"})
    assert qualified is not None
    assert qualified.source == "github:brboi/odoo-addons#42"
    assert [r.name for r in qualified.repos] == ["brboi-addons"]


def test_status_external_target_uses_local_bare_origin(lab):

    bare = repos_dir() / "iap.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    lab.git(bare.parent, "init", "--bare", str(bare))
    lab.git(bare, "remote", "add", "origin", "git@github.com:odoo/iap-apps.git")

    lab.fake_gh(
        [
            {
                "repo": "odoo/iap-apps",
                "number": 1992,
                "title": "IAP PR",
                "url": "https://github.com/odoo/iap-apps/pull/1992",
                "state": "OPEN",
                "isDraft": False,
                "reviewDecision": "",
                "headRefName": "master-port-in",
                "baseRefName": "master",
            }
        ]
    )

    res = lab.ow("status", "iap#1992")
    assert res.exit_code == 0, res.stderr
    assert res.stdout.splitlines()[0].split("\t")[1] == "odoo/iap-apps#1992"


def test_resolve_spec_and_sister_pr_reuse_existing_local_bare_for_known_slug(lab):
    """When only `iap.git` -> `odoo/iap-apps` exists locally, `odoo/iap-apps#1992`, `iap-apps#1992`,
    and sister-PR discovery all reuse `iap` instead of cloning a duplicate `iap-apps.git`."""
    bare = repos_dir() / "iap.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    lab.git(bare.parent, "init", "--bare", str(bare))
    lab.git(bare, "remote", "add", "origin", "git@github.com:odoo/iap-apps.git")

    lab.fake_gh(
        [
            {
                "repo": "odoo/iap-apps",
                "number": 1992,
                "title": "IAP port-in",
                "url": "https://github.com/odoo/iap-apps/pull/1992",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "master-port-in",
                "baseRefName": "master",
            },
            {
                "repo": "odoo/enterprise",
                "number": 135585,
                "title": "Enterprise port-in",
                "url": "https://github.com/odoo/enterprise/pull/135585",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "master-port-in",
                "baseRefName": "master",
            },
        ]
    )
    catalog = {
        "enterprise": KnownRepo(name="enterprise", url="git@github.com:odoo/enterprise.git"),
        "iap-apps": KnownRepo(name="iap-apps", url="git@github.com:odoo/iap-apps.git"),
    }

    by_url = github.ow_resolve_spec(
        "https://github.com/odoo/iap-apps/pull/1992", catalog, {"github.default_org": "odoo"}
    )
    assert by_url is not None
    assert [r.name for r in by_url.repos] == ["iap", "enterprise"]

    by_known = github.ow_resolve_spec("iap-apps#1992", catalog, {"github.default_org": "odoo"})
    assert by_known is not None
    assert [r.name for r in by_known.repos] == ["iap", "enterprise"]

    by_sister = github.ow_resolve_spec("enterprise#135585", catalog, {"github.default_org": "odoo"})
    assert by_sister is not None
    assert [r.name for r in by_sister.repos] == ["enterprise", "iap"]


def test_status_prefers_fork_head_owner_for_pr_opened_after_the_fact(lab):
    """When a branch tracks `dev` (`odoo-dev/enterprise`), status picks the `odoo-dev` PR over other forks."""
    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 1001,
                "title": "Other dev's PR",
                "url": "https://github.com/odoo/enterprise/pull/1001",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-shared-name",
                "baseRefName": "18.0",
                "headRepositoryOwner": {"login": "random-contributor"},
            },
            {
                "repo": "odoo/enterprise",
                "number": 2002,
                "title": "Odoo-dev PR",
                "url": "https://github.com/odoo/enterprise/pull/2002",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-shared-name",
                "baseRefName": "18.0",
                "headRepositoryOwner": {"login": "odoo-dev"},
            },
        ]
    )
    add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    bare = repos_dir() / "enterprise.git"
    lab.git(bare, "config", "ow.github-repo", "odoo/enterprise")
    lab.git(bare, "remote", "add", "dev", "git@github.com:odoo-dev/enterprise.git")
    ws = init_workspace(lab, "enterprise", name="fork-pr-ws", branch="18.0-shared-name")
    lab.git(bare, "config", "branch.18.0-shared-name.remote", "dev")
    lab.git(bare, "config", "branch.18.0-shared-name.merge", "refs/heads/18.0-shared-name")

    st = lab.ow("status", cwd=ws)
    assert st.exit_code == 0, st.stderr
    assert "#2002 OPEN" in st.stdout
    assert "#1001" not in st.stdout


def test_bare_catalog_scans_and_marks_sisters_non_explicit(lab):
    bare = repos_dir() / "iap.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    lab.git(bare.parent, "init", "--bare", str(bare))
    lab.git(bare, "remote", "add", "origin", "git@github.com:odoo/iap-apps.git")

    cat = BareCatalog.scan()
    assert cat.by_name("iap") is not None
    assert cat.resolve_by_slug("odoo", "iap-apps", {})[:2] == ("iap", "odoo")
    assert cat.remote_for_slug("iap", "odoo/iap-apps") == "origin"

    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 111,
                "title": "Main PR",
                "url": "https://github.com/odoo/enterprise/pull/111",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-sister-flag",
                "baseRefName": "18.0",
            },
            {
                "repo": "odoo/iap-apps",
                "number": 222,
                "title": "Sister PR",
                "url": "https://github.com/odoo/iap-apps/pull/222",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-sister-flag",
                "baseRefName": "18.0",
            },
        ]
    )
    known = {
        "enterprise": KnownRepo(name="enterprise", url="git@github.com:odoo/enterprise.git"),
        "iap-apps": KnownRepo(name="iap-apps", url="git@github.com:odoo/iap-apps.git"),
    }
    resolved = github.ow_resolve_spec("enterprise#111", known, {"github.default_org": "odoo"})
    assert resolved is not None
    by_name = {r.name: r for r in resolved.repos}
    assert by_name["enterprise"].explicit is True
    assert by_name["iap"].explicit is False
