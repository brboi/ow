"""The hermetic fixture keeps the host's `OW_*` variables out of every test."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]


def test_no_ow_variable_reaches_a_test():
    assert [var for var in os.environ if var.startswith(("OW_", "_OW_"))] == []


def test_a_host_that_sets_them_cannot_start_containers_in_tests():
    env = {
        **os.environ,
        "OW_CONTAINER_ENGINE": "podman",
        "OW_SANDBOX": "1",
        "OW_WORKSPACE": "host-ws",
        "_OW_COMPLETE": "complete_bash",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"{Path(__file__)}::test_no_ow_variable_reaches_a_test"],
        capture_output=True, text=True, env=env, cwd=SRC.parent,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
