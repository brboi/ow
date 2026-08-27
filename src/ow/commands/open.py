"""`ow open` — open a workspace in the configured editor.

One config key, `editor`, defaulting to `code`. No `xdg-open`, `$EDITOR`, or
`$VISUAL` cascade: a cascade makes the command's behaviour depend on ambient
environment, which is the opposite of what a workspace manager should do.
"""

import shlex
import subprocess
import sys
from pathlib import Path

from ow.utils import paths
from ow.utils.config import Config
from ow.utils.display import err_console
from ow.utils.resolver import resolve_workspace


def cmd_open(config: Config, workspace: str | None = None) -> None:
    """Open a workspace in the configured editor."""
    ws_dir, _ws = resolve_workspace(name=workspace)
    argv = shlex.split(config.editor)
    if not argv:
        print("Error: 'editor' is empty in the global config.", file=sys.stderr)
        sys.exit(1)
    try:
        result = subprocess.run([*argv, str(ws_dir)])
    except OSError as exc:
        err_console.print(
            f"Error: could not run editor '{argv[0]}': {exc}",
            f"       set `editor` in {paths.config_file()}",
            markup=False,
        )
        sys.exit(1)
    if result.returncode != 0:
        sys.exit(result.returncode)
