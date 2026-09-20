"""mise prerequisite and trust: version parsing, and one argv, never a shell.

No test here runs the developer's real mise. Version parsing replaces the
subprocess layer outright; the trust tests run a stand-in binary that does
nothing but record the argv it received.
"""

import os
import subprocess
import sys
from pathlib import Path

from ow.utils import workspace
from ow.utils.workspace import MISE_FLOOR, require_mise, trust_fragment

import pytest


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