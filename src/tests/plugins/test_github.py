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
