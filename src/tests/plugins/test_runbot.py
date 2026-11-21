"""Tests for the built-in `runbot` plugin: companion SHA pinning, direct URL targets, and status."""

from __future__ import annotations

import json

from ow.core.paths import repos_dir
from ow.plugins import runbot
from tests.conftest import add_repo, init_workspace


def _setup_pr_repo(lab, name: str, base: str, base_files: dict[str, str], pr_num: int, pr_branch: str, pr_files: dict[str, str]):
    remote = add_repo(lab, name, {base: base_files}, base=base)
    collab = lab.collaborator(f"author-{name}-{pr_num}", of=remote)
    collab.commit(pr_files, message=f"PR #{pr_num}", branch=pr_branch)
    collab.push(pr_branch)
    lab.publish_pr(remote, pr_num, pr_branch)
    return remote


def test_parse_runbot_url_variants():
    assert runbot.parse_runbot_target("https://runbot.odoo.com/runbot/bundle/12345") == ("bundle", "12345")
    assert runbot.parse_runbot_target("https://runbot.odoo.com/runbot/bundle/master-1") == ("bundle", "master-1")
    assert runbot.parse_runbot_target("https://runbot.odoo.com/runbot/batch/987654") == ("batch", "987654")
    assert runbot.parse_runbot_target("https://example.com/runbot/bundle/12345") is None
    assert runbot.parse_runbot_target(
        "https://custom.runbot.internal/runbot/bundle/42",
        base_url="https://custom.runbot.internal",
    ) == ("bundle", "42")


def test_runbot_pins_community_to_exact_base_match_commit_on_pr_init(lab):
    """When `enterprise#12345` is initialized, `community` is pinned to Runbot's exact `base_match` SHA, not latest master."""
    comm_remote = add_repo(lab, "community", {"master": lab.odoo_files(series="master")}, base="master")
    comm_bare = repos_dir() / "community.git"
    pinned_sha = lab.sha(comm_bare, "refs/remotes/origin/master")

    # Push a newer commit on community `master` after the PR's base commit
    comm_collab = lab.collaborator("comm-adv", of=comm_remote)
    comm_collab.git("checkout", "master")
    latest_sha = comm_collab.commit({"addons/web/newer.py": "newer = True\n"}, message="newer master")
    comm_collab.push("master")
    assert latest_sha != pinned_sha

    _setup_pr_repo(
        lab,
        "enterprise",
        "master",
        {"sale_ent/__manifest__.py": "{}"},
        12345,
        "master-voip-review",
        {"sale_ent/fix.py": "fixed = True\n"},
    )
    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 12345,
                "title": "VoIP review",
                "url": "https://github.com/odoo/enterprise/pull/12345",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "master-voip-review",
                "baseRefName": "master",
            }
        ]
    )
    lab.fake_runbot(
        bundles=[
            {
                "id": 500,
                "name": "master-voip-review",
                "version": "master",
                "last_batch_id": 900,
                "batches": [{"id": 900, "state": "done", "result": "ok"}],
                "prs": ["enterprise:12345"],
            }
        ],
        batches=[
            {
                "id": 900,
                "bundle_id": 500,
                "bundle_name": "master-voip-review",
                "state": "done",
                "result": "ok",
                "commits": [
                    {"repo": "odoo", "sha": pinned_sha},
                    {"repo": "enterprise", "sha": "0" * 40},
                ],
            }
        ],
    )

    ws = lab.home / "review-pr"
    ws.mkdir(parents=True)
    res = lab.ow("init", "enterprise#12345", cwd=ws)
    assert res.exit_code == 0, res.stderr + res.stdout
    assert f"# community: pinned to {pinned_sha[:10]} (runbot bundle #500)" in res.stdout

    # `community` is detached at the EXACT Runbot `base_match` SHA, NOT `latest_sha`!
    assert lab.git(ws / "community", "rev-parse", "HEAD") == pinned_sha
    assert not (ws / "community" / "addons" / "web" / "newer.py").exists()
    # Default base in `.ow/config.toml` still points to `origin/master`
    assert 'community = "origin/master"' in (ws / ".ow" / "config.toml").read_text()
    # `enterprise` is on the PR branch
    assert lab.git(ws / "enterprise", "branch", "--show-current") == "master-voip-review"


def test_runbot_direct_bundle_url_filters_to_local_and_default_repos(lab):
    """`ow init https://runbot.odoo.com/runbot/bundle/777` checks out exact batch SHAs for local/default repos only."""
    comm_remote = add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")}, base="18.0")
    ent_remote = add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}}, base="18.0")
    comm_sha = lab.sha(repos_dir() / "community.git", "refs/remotes/origin/18.0")
    ent_sha = lab.sha(repos_dir() / "enterprise.git", "refs/remotes/origin/18.0")

    # Push newer commits so we prove exact SHA pinning
    c_col = lab.collaborator("c-adv", of=comm_remote)
    c_col.git("checkout", "18.0")
    c_col.commit({"addons/web/after.py": "after = 1\n"}, message="after")
    c_col.push("18.0")
    e_col = lab.collaborator("e-adv", of=ent_remote)
    e_col.git("checkout", "18.0")
    e_col.commit({"ent/after.py": "after = 1\n"}, message="after")
    e_col.push("18.0")

    lab.fake_runbot(
        bundles=[
            {
                "id": 777,
                "name": "18.0-runbot-bundle",
                "version": "18.0",
                "last_batch_id": 888,
                "batches": [{"id": 888, "state": "done", "result": "ok"}],
            }
        ],
        batches=[
            {
                "id": 888,
                "bundle_id": 777,
                "bundle_name": "18.0-runbot-bundle",
                "state": "done",
                "result": "ok",
                "commits": [
                    {"repo": "odoo", "sha": comm_sha},
                    {"repo": "enterprise", "sha": ent_sha},
                    # Not registered locally & not in odoo.default_repos -> must NOT trigger a clone!
                    {"repo": "design-themes", "sha": "a" * 40},
                ],
            }
        ],
    )

    ws = lab.home / "rb-ws"
    ws.mkdir(parents=True)
    res = lab.ow("init", "https://runbot.odoo.com/runbot/bundle/777", cwd=ws)
    assert res.exit_code == 0, res.stderr + res.stdout
    assert lab.git(ws / "community", "rev-parse", "HEAD") == comm_sha
    assert lab.git(ws / "enterprise", "rev-parse", "HEAD") == ent_sha
    assert not (ws / "design-themes").exists()


def test_runbot_status_workspace_external_and_ttl_cache(lab):
    add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}}, base="18.0")
    lab.git(repos_dir() / "enterprise.git", "config", "ow.github-repo", "odoo/enterprise")
    ws = init_workspace(lab, "enterprise", name="rb-status", branch="18.0-rb-fix")

    calls = lab.fake_runbot(
        bundles=[
            {
                "id": 601,
                "name": "18.0-rb-fix",
                "version": "18.0",
                "last_batch_id": 901,
                "batches": [{"id": 901, "state": "done", "result": "ko"}],
                "prs": ["enterprise:4242"],
            }
        ],
        batches=[
            {
                "id": 901,
                "bundle_id": 601,
                "bundle_name": "18.0-rb-fix",
                "state": "done",
                "result": "ko",
                "commits": [],
            }
        ],
    )

    # 1. Offline with cold cache: zero HTTP calls
    off = lab.ow("status", "--offline", cwd=ws)
    assert off.exit_code == 0
    assert calls == []

    # 2. Online workspace status fetches once and caches
    st1 = lab.ow("status", cwd=ws)
    assert st1.exit_code == 0
    assert "# runbot: bundle #601 (18.0-rb-fix): done ko" in st1.stdout
    assert len(calls) == 1

    # 3. Second call within TTL hits cache
    st2 = lab.ow("status", cwd=ws)
    assert st2.exit_code == 0
    assert "# runbot: bundle #601 (18.0-rb-fix): done ko" in st2.stdout
    assert len(calls) == 1

    # 4. External Runbot URL status
    ext = lab.ow("status", "https://runbot.odoo.com/runbot/bundle/601", "--json")
    assert ext.exit_code == 0, ext.stderr
    payload = json.loads(ext.stdout)
    assert any(r["plugin"] == "runbot" and "done ko" in r["summary"] for r in payload["results"])

    # 5. External PR status combines Runbot status too
    ext_pr = lab.ow("status", "enterprise#4242", "--json")
    assert ext_pr.exit_code == 0, ext_pr.stderr
    pr_payload = json.loads(ext_pr.stdout)
    assert any(r["plugin"] == "runbot" and "bundle #601" in r["summary"] for r in pr_payload["results"])


def test_runbot_unreachable_prints_fallback_notice(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    _setup_pr_repo(
        lab,
        "enterprise",
        "18.0",
        {"sale_ent/__manifest__.py": "{}"},
        31415,
        "18.0-offline-fallback",
        {"sale_ent/fix.py": "ok = True\n"},
    )
    lab.fake_gh(
        [
            {
                "repo": "odoo/enterprise",
                "number": 31415,
                "title": "Offline test",
                "url": "https://github.com/odoo/enterprise/pull/31415",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-offline-fallback",
                "baseRefName": "18.0",
            }
        ]
    )

    ws = lab.home / "offline-rb"
    ws.mkdir(parents=True)
    res = lab.ow("init", "enterprise#31415", cwd=ws)
    assert res.exit_code == 0, res.stderr + res.stdout
    assert "# runbot: unreachable, using base ref" in res.stdout


def test_runbot_missing_bundle_warns_about_private_project_fallback(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    _setup_pr_repo(
        lab,
        "iap-apps",
        "18.0",
        {"iap_mod/__manifest__.py": "{}"},
        1992,
        "18.0-phone-service",
        {"iap_mod/phone.py": "ok = True\n"},
    )
    lab.fake_gh(
        [
            {
                "repo": "odoo/iap-apps",
                "number": 1992,
                "title": "Phone service",
                "url": "https://github.com/odoo/iap-apps/pull/1992",
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "18.0-phone-service",
                "baseRefName": "18.0",
            }
        ]
    )
    lab.fake_runbot(bundles=[], batches=[])

    ws = lab.home / "iap-private"
    ws.mkdir(parents=True)
    res = lab.ow("init", "iap-apps#1992", cwd=ws)
    assert res.exit_code == 0, res.stderr + res.stdout
    assert "warning: runbot: no public bundle found for iap-apps:1992" in res.stderr
    assert "using base ref for community" in res.stderr
