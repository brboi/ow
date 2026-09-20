import stat
from pathlib import Path

import pytest

from ow.utils.odoo import (
    OdooInfo,
    find_addon_paths,
    is_odoo_main_repo,
    probe_odoo,
    workspace_addon_paths,
)

# ---------------------------------------------------------------------------
# Fixture builders -- real checkout syntax, never empty marker-only dirs.
# ---------------------------------------------------------------------------

RELEASE_20_MASTER = """\
RELEASE_LEVELS = [ALPHA, BETA, RELEASE_CANDIDATE, FINAL] = ['alpha', 'beta', 'candidate', 'final']
version_info = (20, 1, 0, ALPHA, 1, '')
series = serie = major_version = '.'.join(str(s) for s in version_info[:2])
MIN_PY_VERSION = (3, 12)
MAX_PY_VERSION = (3, 14)
raise RuntimeError('must never execute')
"""

RELEASE_19_STABLE = """\
RELEASE_LEVELS = [ALPHA, BETA, RELEASE_CANDIDATE, FINAL] = ['alpha', 'beta', 'candidate', 'final']
version_info = (19, 0, 0, FINAL, 0, '')
series = serie = major_version = '.'.join(str(s) for s in version_info[:2])
MIN_PY_VERSION = (3, 10)
MAX_PY_VERSION = (3, 14)
MIN_PG_VERSION = 13
raise RuntimeError('must never execute')
"""

# Real Odoo 18.0 odoo/release.py declares no Python bounds at all; they
# live only in odoo/__init__.py's 18.0 layout.
RELEASE_18_NO_BOUNDS = """\
RELEASE_LEVELS = [ALPHA, BETA, RELEASE_CANDIDATE, FINAL] = ['alpha', 'beta', 'candidate', 'final']
version_info = (18, 0, 0, FINAL, 0, '')
series = serie = major_version = '.'.join(str(s) for s in version_info[:2])
raise RuntimeError('must never execute')
"""

INIT_18_FALLBACK = """\
import sys
MIN_PY_VERSION = (3, 10)
MAX_PY_VERSION = (3, 14)
assert sys.version_info > MIN_PY_VERSION, "Outdated python version detected"
raise RuntimeError('must never execute')
"""

RELEASE_SAAS_19 = """\
RELEASE_LEVELS = [ALPHA, BETA, RELEASE_CANDIDATE, FINAL] = ['alpha', 'beta', 'candidate', 'final']
# NOTE: during release, the MAJOR version can become an arbitrary string ('saas~xx')
version_info = ('saas~19', 4, 0, FINAL, 0, '')
MIN_PY_VERSION = (3, 10)
MAX_PY_VERSION = (3, 14)
raise RuntimeError('must never execute')
"""

RELEASE_17_5_DEV = """\
RELEASE_LEVELS = [ALPHA, BETA, RELEASE_CANDIDATE, FINAL] = ['alpha', 'beta', 'candidate', 'final']
version_info = (17, 5, 0, ALPHA, 0, '')
MIN_PY_VERSION = (3, 10)
MAX_PY_VERSION = (3, 13)
raise RuntimeError('must never execute')
"""

RELEASE_AMBIGUOUS = """\
version_info = (19, 0, 0, 'final', 0, '')
version_info = (19, 1, 0, 'final', 0, '')
MIN_PY_VERSION = (3, 10)
MAX_PY_VERSION = (3, 14)
"""

RELEASE_INVALID_SYNTAX = """\
def broken(:
    pass
"""

CONFIG_WITH_DEMO = """\
class configmanager:
    def __init__(self):
        parser = optparse.OptionParser()
        group = optparse.OptionGroup(parser, "Testing")
        group.add_option('--with-demo', action='store_true', dest='with_demo',
                          help='load demo data for modules to be installed')
        parser.add_option_group(group)
"""

# Real odoo/tools/config.py: the flag lives on `group`, not `parser`, and
# only --without-demo is declared -- with-demo capability is False.
CONFIG_WITHOUT_DEMO_ONLY = """\
class configmanager:
    def __init__(self):
        parser = optparse.OptionParser()
        group = optparse.OptionGroup(parser, "Common options")
        group.add_option("--without-demo", dest="without_demo",
                          help="disable loading demo data for modules to be installed",
                          my_default=False)
        parser.add_option_group(group)
"""

CONFIG_MISLEADING_COMMENT = """\
class configmanager:
    def __init__(self):
        parser = optparse.OptionParser()
        group = optparse.OptionGroup(parser, "Common options")
        # historically we also supported --with-demo here, now removed
        group.add_option("--without-demo", dest="without_demo",
                          help="disable loading demo data, see also --with-demo in old docs",
                          my_default=False)
        parser.add_option_group(group)
"""

CONFIG_NO_DEMO_FLAGS = """\
class configmanager:
    def __init__(self):
        parser = optparse.OptionParser()
        group = optparse.OptionGroup(parser, "Common options")
        group.add_option("--pidfile", dest="pidfile", help="file where the server pid will be stored")
        parser.add_option_group(group)
"""

BWRAP_SCRIPT = "#!/bin/sh\nexec bwrap \"$@\"\n"
FIREJAIL_PROFILE = "# Odoo firejail profile\ninclude /etc/firejail/default.profile\n"


def _make_core(
    tmp_path,
    name,
    *,
    release_source: str | None = RELEASE_19_STABLE,
    config_source: str | None = CONFIG_WITH_DEMO,
    init_source: str | None = None,
) -> Path:
    core = tmp_path / name
    (core / "addons").mkdir(parents=True)
    (core / "odoo" / "addons").mkdir(parents=True)
    (core / "odoo-bin").touch()
    if release_source is not None:
        (core / "odoo" / "release.py").write_text(release_source)
    if init_source is not None:
        (core / "odoo" / "__init__.py").write_text(init_source)
    if config_source is not None:
        (core / "odoo" / "tools").mkdir(parents=True, exist_ok=True)
        (core / "odoo" / "tools" / "config.py").write_text(config_source)
    return core


# ---------------------------------------------------------------------------
# is_odoo_main_repo / find_addon_paths -- transferred addon walker semantics.
# ---------------------------------------------------------------------------


def test_is_odoo_main_repo_true(tmp_path):
    repo = tmp_path / "odoo"
    (repo / "addons").mkdir(parents=True)
    (repo / "odoo" / "addons").mkdir(parents=True)
    (repo / "odoo-bin").touch()
    assert is_odoo_main_repo(repo) is True


def test_is_odoo_main_repo_false_no_odoo_bin(tmp_path):
    repo = tmp_path / "enterprise"
    (repo / "addons").mkdir(parents=True)
    assert is_odoo_main_repo(repo) is False


def test_find_addon_paths_on_file(tmp_path):
    f = tmp_path / "somefile.txt"
    f.touch()
    assert find_addon_paths(f) == []


def test_find_addon_paths_nonexistent(tmp_path):
    assert find_addon_paths(tmp_path / "nonexistent") == []


def test_find_addon_paths_flat_repo(tmp_path):
    repo = tmp_path / "myaddon_repo"
    addon = repo / "myaddon"
    addon.mkdir(parents=True)
    (addon / "__manifest__.py").touch()
    assert find_addon_paths(repo) == [repo]


def test_find_addon_paths_categorized_repo(tmp_path):
    repo = tmp_path / "repo"
    for category, addon in (("messaging", "mail_core"), ("telephony", "voip_core")):
        d = repo / category / addon
        d.mkdir(parents=True)
        (d / "__manifest__.py").touch()
    result = find_addon_paths(repo)
    assert result == sorted([repo / "messaging", repo / "telephony"])


def test_find_addon_paths_handles_symlink_cycle(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    loop = root / "loop"
    loop.mkdir()
    (loop / "back").symlink_to(root, target_is_directory=True)
    result = find_addon_paths(root)
    assert isinstance(result, list)


def test_find_addon_paths_prunes_noise_dirs(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git" / "addons" / "fake_addon").mkdir(parents=True)
    (root / ".git" / "addons" / "fake_addon" / "__manifest__.py").touch()
    (root / "node_modules" / "pkg_addon").mkdir(parents=True)
    (root / "node_modules" / "pkg_addon" / "__manifest__.py").touch()
    (root / "real_addons" / "real_addon").mkdir(parents=True)
    (root / "real_addons" / "real_addon" / "__manifest__.py").touch()
    result = find_addon_paths(root)
    assert result == [root / "real_addons"]


def test_find_addon_paths_ignores_python_packages(tmp_path):
    """A bare __init__.py is a Python package marker, not an addon marker."""
    repo = tmp_path / "repo"
    (repo / "roles" / "some_role" / "tasks").mkdir(parents=True)
    (repo / "roles" / "some_role" / "__init__.py").touch()
    (repo / "custom" / "real_addon").mkdir(parents=True)
    (repo / "custom" / "real_addon" / "__manifest__.py").touch()
    assert find_addon_paths(repo) == [repo / "custom"]


def test_find_addon_paths_excludes_paths(tmp_path):
    repo = tmp_path / "ws"
    core = repo / "community"
    addon = core / "myaddon"
    addon.mkdir(parents=True)
    (addon / "__manifest__.py").touch()
    loose_addon = repo / "extra" / "loose"
    loose_addon.mkdir(parents=True)
    (loose_addon / "__manifest__.py").touch()
    result = find_addon_paths(repo, exclude=[core])
    assert result == [repo / "extra"]


# ---------------------------------------------------------------------------
# workspace_addon_paths -- ordering, generic-workspace skip, deduplication.
# ---------------------------------------------------------------------------


def test_workspace_addon_paths_orders_loose_then_repos_then_core(tmp_path):
    ws_dir = tmp_path / "ws"

    local_addon = ws_dir / ".local" / "local_mod"
    local_addon.mkdir(parents=True)
    (local_addon / "__manifest__.py").touch()

    loose_addon = ws_dir / "extra" / "loose_mod"
    loose_addon.mkdir(parents=True)
    (loose_addon / "__manifest__.py").touch()

    other_repo = ws_dir / "other"
    other_addon = other_repo / "other_mod"
    other_addon.mkdir(parents=True)
    (other_addon / "__manifest__.py").touch()

    core = _make_core(ws_dir, "community")

    result = workspace_addon_paths(ws_dir, ["other", "community"], "community")

    assert result == (
        local_addon.parent,
        loose_addon.parent,
        other_addon.parent,
        core / "addons",
        core / "odoo" / "addons",
    )


def test_workspace_addon_paths_none_core_skips_scan(tmp_path):
    """A generic workspace never walks the tree, even if addons exist."""
    ws_dir = tmp_path / "ws"
    loose_addon = ws_dir / "extra" / "loose_mod"
    loose_addon.mkdir(parents=True)
    (loose_addon / "__manifest__.py").touch()

    assert workspace_addon_paths(ws_dir, ["docs"], None) == ()


def test_workspace_addon_paths_deduplicates_without_reordering(tmp_path):
    ws_dir = tmp_path / "ws"
    other_addon = ws_dir / "other" / "mod"
    other_addon.mkdir(parents=True)
    (other_addon / "__manifest__.py").touch()
    core = _make_core(ws_dir, "community")

    result = workspace_addon_paths(ws_dir, ["other", "other", "community"], "community")

    assert result == (
        other_addon.parent,
        core / "addons",
        core / "odoo" / "addons",
    )


# ---------------------------------------------------------------------------
# probe_odoo -- the six spec outcomes.
# ---------------------------------------------------------------------------


def test_probe_odoo_none_when_no_core(tmp_path):
    repo = tmp_path / "docs"
    repo.mkdir()
    probe = probe_odoo({"docs": repo})
    assert probe.kind == "none"
    assert probe.info is None


def test_probe_odoo_incomplete_when_worktree_missing(tmp_path):
    present = tmp_path / "present"
    present.mkdir()
    absent = tmp_path / "absent"
    probe = probe_odoo({"present": present, "community": absent})
    assert probe.kind == "incomplete"
    assert any("community" in m for m in probe.messages)


def test_probe_odoo_ambiguous_when_multiple_cores(tmp_path):
    core_a = _make_core(tmp_path, "community")
    core_b = _make_core(tmp_path, "fork")
    probe = probe_odoo({"community": core_a, "fork": core_b})
    assert probe.kind == "ambiguous"
    assert probe.info is None
    assert any("community" in m for m in probe.messages)
    assert any("fork" in m for m in probe.messages)


def test_probe_odoo_supported_19_stable(tmp_path):
    core = _make_core(tmp_path, "community", release_source=RELEASE_19_STABLE)
    probe = probe_odoo({"community": core})
    assert probe.kind == "supported"
    assert probe.info == OdooInfo(
        alias="community",
        series=19,
        major=19,
        minor=0,
        python_min=(3, 10),
        python_max=(3, 14),
        with_demo=True,
        sandbox_paths={},
    )


def test_probe_odoo_supported_20_master(tmp_path):
    core = _make_core(tmp_path, "community", release_source=RELEASE_20_MASTER)
    probe = probe_odoo({"community": core})
    assert probe.kind == "supported"
    assert probe.info.major == 20
    assert probe.info.minor == 1
    assert probe.info.python_min == (3, 12)
    assert probe.info.python_max == (3, 14)


def test_probe_odoo_supported_18_fallback_bounds(tmp_path):
    core = _make_core(
        tmp_path,
        "community",
        release_source=RELEASE_18_NO_BOUNDS,
        init_source=INIT_18_FALLBACK,
    )
    probe = probe_odoo({"community": core})
    assert probe.kind == "supported"
    assert probe.info.major == 18
    assert probe.info.python_min == (3, 10)
    assert probe.info.python_max == (3, 14)


def test_probe_odoo_supported_saas_identity(tmp_path):
    core = _make_core(tmp_path, "community", release_source=RELEASE_SAAS_19)
    probe = probe_odoo({"community": core})
    assert probe.kind == "supported"
    assert probe.info.major == 19
    assert probe.info.series == 19
    assert probe.info.minor == 4


def test_probe_odoo_unsupported_old_development(tmp_path):
    core = _make_core(tmp_path, "community", release_source=RELEASE_17_5_DEV)
    probe = probe_odoo({"community": core})
    assert probe.kind == "unsupported"
    assert probe.info.major == 17
    assert probe.info.minor == 5


def test_probe_odoo_invalid_unreadable_release(tmp_path):
    core = _make_core(tmp_path, "community", release_source=None)
    probe = probe_odoo({"community": core})
    assert probe.kind == "invalid"
    assert probe.info is None
    assert any("release.py" in m for m in probe.messages)


def test_probe_odoo_invalid_syntax_error(tmp_path):
    core = _make_core(tmp_path, "community", release_source=RELEASE_INVALID_SYNTAX)
    probe = probe_odoo({"community": core})
    assert probe.kind == "invalid"
    assert probe.info is None


def test_probe_odoo_invalid_ambiguous_version_info_assignment(tmp_path):
    core = _make_core(tmp_path, "community", release_source=RELEASE_AMBIGUOUS)
    probe = probe_odoo({"community": core})
    assert probe.kind == "invalid"
    assert any("version_info" in m for m in probe.messages)


def test_probe_odoo_invalid_no_demo_flags_declared(tmp_path):
    core = _make_core(tmp_path, "community", config_source=CONFIG_NO_DEMO_FLAGS)
    probe = probe_odoo({"community": core})
    assert probe.kind == "invalid"
    assert probe.info is None


def test_probe_odoo_without_demo_only_capability(tmp_path):
    core = _make_core(tmp_path, "community", config_source=CONFIG_WITHOUT_DEMO_ONLY)
    probe = probe_odoo({"community": core})
    assert probe.kind == "supported"
    assert probe.info.with_demo is False


def test_probe_odoo_ignores_misleading_comment_flag(tmp_path):
    """A flag string in a comment is not a declaration."""
    core = _make_core(tmp_path, "community", config_source=CONFIG_MISLEADING_COMMENT)
    probe = probe_odoo({"community": core})
    assert probe.kind == "supported"
    assert probe.info.with_demo is False


# ---------------------------------------------------------------------------
# Sandbox probing -- presence, executability, symlink-escape rejection.
# ---------------------------------------------------------------------------


def _chmod_executable(path):
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_probe_sandbox_paths_discovers_present_executable_assets(tmp_path):
    core = _make_core(tmp_path, "community")
    bwrap_dir = core / "setup" / "sandboxing" / "bwrap"
    bwrap_dir.mkdir(parents=True)
    claude_script = bwrap_dir / "bwrap-claude.sh"
    claude_script.write_text(BWRAP_SCRIPT)
    _chmod_executable(claude_script)

    firejail_dir = core / "setup" / "sandboxing" / "firejail"
    firejail_dir.mkdir(parents=True)
    (firejail_dir / "claude.profile").write_text(FIREJAIL_PROFILE)

    probe = probe_odoo({"community": core})
    assert probe.kind == "supported"
    assert set(probe.info.sandbox_paths) == {"bwrap-claude", "firejail-claude"}
    assert probe.info.sandbox_paths["bwrap-claude"] == claude_script


def test_probe_sandbox_paths_omits_missing_assets(tmp_path):
    core = _make_core(tmp_path, "community")
    probe = probe_odoo({"community": core})
    assert probe.kind == "supported"
    assert probe.info.sandbox_paths == {}


def test_probe_sandbox_paths_omits_non_executable_bwrap_script(tmp_path):
    core = _make_core(tmp_path, "community")
    bwrap_dir = core / "setup" / "sandboxing" / "bwrap"
    bwrap_dir.mkdir(parents=True)
    script = bwrap_dir / "bwrap-pi.sh"
    script.write_text(BWRAP_SCRIPT)
    script.chmod(0o644)

    probe = probe_odoo({"community": core})
    assert probe.kind == "supported"
    assert "bwrap-pi" not in probe.info.sandbox_paths


def test_probe_sandbox_paths_rejects_symlink_escape(tmp_path):
    core = _make_core(tmp_path, "community")
    outside = tmp_path / "outside.sh"
    outside.write_text(BWRAP_SCRIPT)
    _chmod_executable(outside)

    bwrap_dir = core / "setup" / "sandboxing" / "bwrap"
    bwrap_dir.mkdir(parents=True)
    escaping_link = bwrap_dir / "bwrap-claude.sh"
    escaping_link.symlink_to(outside)

    probe = probe_odoo({"community": core})
    assert probe.kind == "supported"
    assert "bwrap-claude" not in probe.info.sandbox_paths


def test_probe_sandbox_paths_rejects_symlinked_directory_escape(tmp_path):
    core = _make_core(tmp_path, "community")
    outside_dir = tmp_path / "outside_sandboxing" / "bwrap"
    outside_dir.mkdir(parents=True)
    outside_script = outside_dir / "bwrap-claude.sh"
    outside_script.write_text(BWRAP_SCRIPT)
    _chmod_executable(outside_script)

    setup_dir = core / "setup"
    setup_dir.mkdir()
    (setup_dir / "sandboxing").symlink_to(tmp_path / "outside_sandboxing", target_is_directory=True)

    probe = probe_odoo({"community": core})
    assert probe.kind == "supported"
    assert "bwrap-claude" not in probe.info.sandbox_paths
