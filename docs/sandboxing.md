# Sandboxing AI Coding Assistants

An Odoo checkout ships its own sandbox wrappers under `setup/sandboxing/` — bubblewrap scripts
for Opencode, Claude Code and Pi, and a firejail profile for Claude Code. `ow` does not ship
sandboxes and does not install them: when it finds those assets in the core checkout, it wires
them into the generated mise fragment as tasks, so `mise run <task>` — or the task name directly
in a mise-activated shell — launches the assistant behind them.

## Prerequisites

The wrappers are the checkout's, so their prerequisites are too:

- **[bubblewrap](https://github.com/containers/bubblewrap)** for the `bwrap-*` tasks:

  ```sh
  sudo apt install bubblewrap   # Debian/Ubuntu
  sudo dnf install bubblewrap   # Fedora
  sudo pacman -S bubblewrap     # Arch
  ```

- **[firejail](https://firejail.wordpress.com/)** for the `firejail-claude` task.

`ow` never checks for either binary, never installs one, and never runs a wrapper. It only reads
the checkout's filesystem to see what the checkout provides.

## The tasks

| Task | Wired from | Runs |
|------|-----------|------|
| `bwrap-claude` | `setup/sandboxing/bwrap/bwrap-claude.sh` | that script, with `--add-dir` per Git metadata dir, and `ODOO_BASE={{config_root}}` |
| `bwrap-opencode` | `setup/sandboxing/bwrap/bwrap-opencode.sh` | that script, with the same `--add-dir` arguments and `ODOO_BASE` |
| `bwrap-pi` | `setup/sandboxing/bwrap/bwrap-pi.sh` | that script, with the same `--add-dir` arguments and `ODOO_BASE` |
| `firejail-claude` | `setup/sandboxing/firejail/claude.profile` | `firejail --profile=<profile> --whitelist=<workspace> --whitelist=<git metadata dir>… claude` |

A task is generated only when its asset is present *and usable*:

- a `bwrap` script must be a regular file and executable;
- the firejail profile must be a regular file — firejail reads it, it is never executed;
- a path whose resolved real location leaves the checkout (a symlinked leaf, or a symlinked
  ancestor directory) is rejected rather than followed.

`ow` probes presence and executability, never content. A checkout with none of these assets gets
no `tasks` table at all, which is also the state of an older checkout, a checkout that does not
carry the sandboxing directory, and any non-Odoo workspace.

## Why the extra directories

A linked worktree's `.git` is a file pointing at the shared bare repo, which lives outside the
workspace — and every worktree of a repo shares one such store. A sandbox that only sees the
workspace therefore breaks every git command inside it. `ow` gives each task the checkout's Git
common directories:

- the `bwrap` tasks get `--add-dir <git common dir>` for each distinct one, followed by whatever
  arguments you passed;
- `firejail-claude` gets `--whitelist=<git common dir>` for each, alongside
  `--whitelist=<workspace>`.

The `bwrap` tasks also export `ODOO_BASE={{config_root}}` — the workspace root — for the
checkout's own scripts to consume. The firejail task sets no task environment: its wrapper is a
profile, not a script ow runs.

## Security assumptions

The wrappers own their security. `ow` does not add sandbox flags, widen a profile, or audit what
the checkout's scripts do; it reproduces the invocation they expect. If a wrapper's isolation
changes, that is the checkout's change, not ow's.

Two consequences worth being plain about:

- The `firejail-claude` task runs Claude Code behind the checkout's profile. `ow` does not
  configure any editor to be launched inside that sandbox, and does not install editor
  integrations.
- The wrappers are only as good as the checkout they come from. Running one from an untrusted
  checkout runs that checkout's own shell script.

## Upgrading from an older ow

Older ow rendered its own wrapper scripts into the workspace root (`bwrap-claude`,
`bwrap-opencode`). 3.0 removes the template system, so those files are no longer generated. They
are left on disk and reported as `not rendered` by `ow files` — ow never deletes a workspace file.
The tasks now point at the checkout's own scripts instead, so you can delete the stale copies
yourself.