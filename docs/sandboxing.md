# Sandboxing AI assistants

ow ships no sandbox. Odoo's checkout carries the wrappers under `setup/sandboxing/` (bubblewrap scripts and firejail profiles); follow its setup instructions to install them. This page covers what an ow workspace needs mounted.

## Paths to mount

An ow workspace needs up to three paths visible inside the sandbox:

- the workspace itself, which holds the worktrees;
- the bare repos at `$XDG_DATA_HOME/ow/repos` (usually `~/.local/share/ow/repos`): a worktree's `.git` points into its bare repo, so git fails without it;
- the shared virtualenvs at `$XDG_DATA_HOME/ow/venvs`, when `mise.shared_venv = true` (the default): `odools.toml` and `ow run` point at them. Set `mise.shared_venv = false` to use a local `.venv/` instead.

## Example: bubblewrap

With Odoo's `bwrap-claude.sh` on `PATH`, add to `~/.bashrc`, `~/.zshrc` or equivalent:

```bash
claude-sandbox() {
  ws=$(command ow cd) || return
  ODOO_BASE="$ws" bwrap-claude.sh \
    --add-dir "${XDG_DATA_HOME:-$HOME/.local/share}/ow/repos" \
    --add-dir "${XDG_DATA_HOME:-$HOME/.local/share}/ow/venvs" "$@"
}
```

Then, in a workspace:

```sh
claude-sandbox
```

`ODOO_BASE` is the directory mounted read-write and the sandbox's working directory. `--add-dir <path>` mounts one more directory read-write. `command ow cd` bypasses the `ow cd` shell function installed by `ow shell-init`. The other wrappers (`bwrap-opencode.sh`, `bwrap-pi.sh`) take the same `ODOO_BASE` and `--add-dir`.

## Trade-offs

- Writes stay inside the mounted directories.
- The whole `$XDG_DATA_HOME/ow/repos` directory is visible, not only the current workspace's bare repos.
- Network stays available.
