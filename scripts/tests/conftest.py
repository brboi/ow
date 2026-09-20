"""Make `ow` importable when scripts/tests runs on its own.

The package lives in `src/`, which pytest only puts on `sys.path` while
collecting `src/tests`. These tests exercise the migration script's output
against ow's own config loader, so they must not depend on that collection
order.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
