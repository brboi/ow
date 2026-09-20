"""mise prerequisite and trust: version parsing, and one argv, never a shell.

No test here runs the developer's real mise. Version parsing replaces the
subprocess layer outright; the trust tests run a stand-in binary that does
nothing but record the argv it received.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ow.utils import paths, workspace
from ow.utils.config import Config, WorkspaceConfig, parse_branch_spec
from ow.utils.render import RENDERED_LOCK, write_files
from ow.utils.workspace import MISE_FLOOR, inspect_workspace, refresh_workspace, require_mise, trust_fragment


def _stub_version(monkeypatch, output: str) -> None:
    monkeypatch.setattr(
        workspace,
        "run_cmd",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, output, ""),
    )


def test_an_older_mise_is_rejected(monkeypatch):
    _stub_version(monkeypatch, "mise 2026.8.12 linux-x64 (2026-08-12)\n")

    with pytest.raises(ValueError) as caught:
        require_mise()

    assert "2026.8.13" in str(caught.value)
    assert "2026.8.12" in str(caught.value)


def test_the_floor_version_is_accepted(monkeypatch):
    _stub_version(monkeypatch, "2026.8.13 linux-x64\n")

    assert require_mise() == MISE_FLOOR


def test_a_newer_mise_is_accepted(monkeypatch):
    _stub_version(monkeypatch, "mise 2026.9.9 linux-x64 (2026-09-19)\n")

    assert require_mise() == (2026, 9, 9)


def test_output_naming_no_version_is_rejected(monkeypatch):
    _stub_version(monkeypatch, "mise (devel)\n")

    with pytest.raises(ValueError) as caught:
        require_mise()

    assert "2026.8.13" in str(caught.value)


def test_a_missing_mise_is_rejected(monkeypatch):
    def absent(args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "mise")

    monkeypatch.setattr(workspace, "run_cmd", absent)

    with pytest.raises(ValueError) as caught:
        require_mise()

    assert "2026.8.13" in str(caught.value)


def _fake_mise(tmp_path: Path, log: Path, exit_code: int = 0) -> None:
    """A `mise` on PATH that records its argv, one NUL-terminated element each."""
    binary = tmp_path / "bin" / "mise"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"with open({str(log)!r}, 'ab') as handle:\n"
        "    for arg in sys.argv[1:]:\n"
        "        handle.write(arg.encode() + b'\\x00')\n"
        f"sys.exit({exit_code})\n"
    )
    binary.chmod(0o755)
    return binary


def test_trust_passes_the_fragment_as_one_argument(tmp_path, monkeypatch):
    """A workspace path with a space and a quote arrives as one argv element."""
    log = tmp_path / "argv.log"
    _fake_mise(tmp_path, log)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    fragment = tmp_path / "my workspace" / "mise" / "conf.d" / "00-ow.toml"
    fragment.parent.mkdir(parents=True)
    fragment.write_text("[tools]\n")

    trust_fragment(fragment)

    assert log.read_bytes().split(b"\x00")[:-1] == [b"trust", str(fragment).encode()]


def test_a_failed_trust_is_not_swallowed(tmp_path, monkeypatch):
    log = tmp_path / "argv.log"
    _fake_mise(tmp_path, log, exit_code=3)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")

    with pytest.raises(subprocess.CalledProcessError):
        trust_fragment(tmp_path / "fragment.toml")


# ---------------------------------------------------------------------------
# inspect_workspace / refresh_workspace: a recognized-but-unusable core
# (invalid, ambiguous, unsupported) blocks every output write. There is no
# guessed generic fallback for a core ow can identify but must not act on.
# ---------------------------------------------------------------------------

RELEASE_19_STABLE = """\
version_info = (19, 0, 0, 'final', 0, '')
MIN_PY_VERSION = (3, 10)
MAX_PY_VERSION = (3, 14)
"""

RELEASE_17_5_DEV = """\
version_info = (17, 5, 0, 'alpha', 0, '')
MIN_PY_VERSION = (3, 10)
MAX_PY_VERSION = (3, 13)
"""

RELEASE_INVALID_SYNTAX = "def broken(:\n    pass\n"

CONFIG_WITH_DEMO = """\
class configmanager:
    def __init__(self):
        group.add_option('--with-demo', action='store_true')
"""


def _make_core(root: Path, alias: str, *, release_source: str) -> Path:
    core = root / alias
    (core / "addons").mkdir(parents=True)
    (core / "odoo" / "addons").mkdir(parents=True)
    (core / "odoo-bin").touch()
    (core / "odoo" / "release.py").write_text(release_source)
    (core / "odoo" / "tools").mkdir(parents=True)
    (core / "odoo" / "tools" / "config.py").write_text(CONFIG_WITH_DEMO)
    return core


def test_inspect_workspace_blocks_on_an_invalid_core(tmp_path, xdg):
    ws_dir = tmp_path / "ws"
    _make_core(ws_dir, "community", release_source=RELEASE_INVALID_SYNTAX)
    ws = WorkspaceConfig(repos={"community": parse_branch_spec("master")})

    plan = inspect_workspace(Config(remotes={}), ws, ws_dir)

    assert plan.errors
    assert plan.outputs == ()


def test_inspect_workspace_blocks_on_an_ambiguous_core(tmp_path, xdg):
    ws_dir = tmp_path / "ws"
    _make_core(ws_dir, "community", release_source=RELEASE_19_STABLE)
    _make_core(ws_dir, "fork", release_source=RELEASE_19_STABLE)
    ws = WorkspaceConfig(repos={
        "community": parse_branch_spec("master"),
        "fork": parse_branch_spec("master"),
    })

    plan = inspect_workspace(Config(remotes={}), ws, ws_dir)

    assert plan.errors
    assert any("community" in e or "fork" in e for e in plan.errors)
    assert plan.outputs == ()


def test_inspect_workspace_blocks_on_an_unsupported_core(tmp_path, xdg):
    """Odoo 17.5 is a recognized, well-formed core outside SUPPORTED_MAJORS:
    it must block exactly like invalid/ambiguous, never render generic
    fallback output."""
    ws_dir = tmp_path / "ws"
    _make_core(ws_dir, "community", release_source=RELEASE_17_5_DEV)
    ws = WorkspaceConfig(repos={"community": parse_branch_spec("master")})

    plan = inspect_workspace(Config(remotes={}), ws, ws_dir)

    assert plan.errors
    assert any("17.5" in e for e in plan.errors)
    assert any("community" in e for e in plan.errors)
    assert plan.outputs == ()


def test_inspect_workspace_unsupported_core_still_shows_retained_locked_outputs(tmp_path, xdg):
    """A checkout downgraded to an unsupported major after a successful
    render must still surface what is already on disk, retained — never
    silently dropped from view."""
    ws_dir = tmp_path / "ws"
    _make_core(ws_dir, "community", release_source=RELEASE_19_STABLE)
    ws = WorkspaceConfig(repos={"community": parse_branch_spec("master")})
    config = Config(remotes={})

    first = inspect_workspace(config, ws, ws_dir)
    assert not first.errors
    write_files(first)
    assert (ws_dir / "mise" / "conf.d" / "00-ow.toml").exists()

    (ws_dir / "community" / "odoo" / "release.py").write_text(RELEASE_17_5_DEV)

    second = inspect_workspace(config, ws, ws_dir)

    assert second.errors
    assert second.outputs == ()
    retained = {s.path for s in second.states}
    assert "mise/conf.d/00-ow.toml" in retained
    # The file itself is untouched by the blocked inspection.
    assert (ws_dir / "mise" / "conf.d" / "00-ow.toml").exists()


def test_refresh_workspace_writes_nothing_for_an_unsupported_core(tmp_path, xdg):
    ws_dir = tmp_path / "ws"
    _make_core(ws_dir, "community", release_source=RELEASE_17_5_DEV)
    ws = WorkspaceConfig(repos={"community": parse_branch_spec("master")})

    result = refresh_workspace(Config(remotes={}), ws, ws_dir, trust=True)

    assert result.failed
    assert result.wrote == () and result.updated == () and result.adopted == ()
    assert not (ws_dir / "mise").exists()
    assert not (ws_dir / RENDERED_LOCK).exists()
    assert not (paths.services_dir() / "compose.yml").exists()