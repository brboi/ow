"""Unit & integration tests for the built-in `github` plugin (`gh` CLI + TTL cache)."""

from __future__ import annotations

from ow.core.paths import repos_dir
from ow.core.plugins import KnownRepo, load_plugins
from ow.core.session import open_session
from ow.plugins import github
from tests.conftest import add_repo, init_workspace


def test_parse_pr_target_variants():
    assert github.parse_pr_target("enterprise#12345") == (None, "enterprise", 12345)
    assert github.parse_pr_target("odoo/iap-apps#42") == ("odoo", "iap-apps", 42)
    assert github.parse_pr_target("https://github.com/odoo/enterprise/pull/12345") == (
        "odoo",
        "enterprise",
        12345,
    )
    assert github.parse_pr_target("https://github.com/odoo/enterprise/pull/12345/files") == (
        "odoo",
        "enterprise",
        12345,
    )
    assert github.parse_pr_target("18.0-my-fix") is None


def test_resolve_target_maps_alias_and_discovers_sister_pr(lab):
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
        "community": KnownRepo("community", "git@github.com:odoo/odoo.git", aliases=("odoo",)),
        "enterprise": KnownRepo("enterprise", "git@github.com:odoo/enterprise.git"),
    }

    resolved = github.ow_resolve_target("enterprise#12345", catalog, {"github.default_org": "odoo"})

    assert resolved is not None
    assert resolved.name == "12345-18.0-fix-invoice-mic"
    assert resolved.source == "github:odoo/enterprise#12345"
    by_name = {r.name: r for r in resolved.repos}
    assert set(by_name) == {"enterprise", "community"}
    assert by_name["enterprise"].fetch_ref == "refs/pull/12345/head"
    assert by_name["community"].fetch_ref == "refs/pull/99999/head"
    assert by_name["community"].branch == "18.0-fix-invoice-mic"


def test_resolve_target_odoo_alias_maps_to_community(lab):
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
        "community": KnownRepo("community", "git@github.com:odoo/odoo.git", aliases=("odoo",)),
    }

    resolved = github.ow_resolve_target("odoo#777", catalog, {"github.default_org": "odoo"})
    assert resolved is not None
    assert resolved.name == "777-18.0-core-fix"
    assert [r.name for r in resolved.repos] == ["community"]


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
    assert github.parse_pr_target(
        "https://github.com/odoo/enterprise/pull/12345#issuecomment-999"
    ) == ("odoo", "enterprise", 12345)
    assert github.parse_pr_target(
        "https://github.com/odoo/enterprise/pull/12345?w=1"
    ) == ("odoo", "enterprise", 12345)


def test_status_external_target_resolves_alias_and_caches(lab):
    import json

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

    res = lab.ow("status", "community#4242", "--json")
    assert res.exit_code == 0, res.stderr
    payload = json.loads(res.stdout)
    assert payload["schema_version"] == 1
    assert payload["results"][0]["target"] == "odoo/odoo#4242"
    assert "#4242 OPEN (approved)" in payload["results"][0]["summary"]
    assert len(log_path.read_text().splitlines()) == 1

    # Offline serves stale/warm cache for the same target
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
    ws_base = init_workspace(lab, "community", name="base-ws", branch="18.0")

    dry = lab.ow("prune", "-n")
    assert dry.exit_code == 0
    assert "mixed-wip" not in dry.stdout
    assert "base-ws" not in dry.stdout


def test_resolve_target_uses_matching_non_origin_remote_for_base(lab):
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
    resolved = github.ow_resolve_target("enterprise#321", known, {"github.default_org": "odoo"})
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
        "community": KnownRepo("community", "git@github.com:odoo/odoo.git", aliases=("odoo",)),
        "enterprise": KnownRepo("enterprise", "git@github.com:odoo/enterprise.git"),
    }
    resolved = github.ow_resolve_target("enterprise#100", catalog, {"github.default_org": "odoo"})
    assert resolved is not None
    assert [r.name for r in resolved.repos] == ["enterprise"]
