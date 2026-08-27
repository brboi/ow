"""`ow shell-init` — print the shell snippet that makes `ow cd` change directory.

`ow cd` prints a path; the shell function this emits intercepts `ow cd`,
reads that path, and does the `cd` itself. `command ow` inside the function
reaches the real binary, so the snippet is the whole integration.
"""

import sys

_POSIX = '''\
# ow shell integration — add to your shell rc:
#   eval "$(ow shell-init {shell})"
ow() {{
  if [ "$1" = "cd" ]; then
    shift
    local _ow_target
    _ow_target="$(command ow cd "$@")" || return $?
    cd "$_ow_target"
  else
    command ow "$@"
  fi
}}
'''

_FISH = '''\
# ow shell integration — add to your config.fish:
#   ow shell-init fish | source
function ow
    if test (count $argv) -gt 0; and test "$argv[1]" = "cd"
        set -l _ow_target (command ow cd $argv[2..-1])
        if test -z "$_ow_target"
            return 1
        end
        cd $_ow_target
    else
        command ow $argv
    end
end
'''


def cmd_shell_init(shell: str) -> None:
    if shell in ("bash", "zsh"):
        print(_POSIX.format(shell=shell), end="")
    elif shell == "fish":
        print(_FISH, end="")
    else:
        print(
            f"Error: unsupported shell '{shell}'. Supported: bash, zsh, fish.",
            file=sys.stderr,
        )
        sys.exit(1)
