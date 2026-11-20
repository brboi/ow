# Sandboxing AI assistants

ow ships no sandbox of its own. Odoo's checkout carries the wrappers, under `setup/sandboxing/`: bubblewrap scripts for Opencode, Claude Code and Pi, and a firejail profile for Claude Code. They run an assistant with filesystem isolation, so it sees only the directories you mount in.

An ow workspace needs two paths visible: the workspace itself, which holds the worktrees, and the bare repos at `$XDG_DATA_HOME/ow/repos` (usually `~/.local/share/ow/repos`). A worktree's `.git` is a file pointing into its bare repo, so git inside the sandbox cannot work without that second path.

## Bubblewrap

Install bubblewrap and put Odoo's scripts on `PATH`:

```sh
sudo apt install bubblewrap                    # Debian / Ubuntu; use your package manager elsewhere
mkdir -p ~/.local/bin
cp setup/sandboxing/bwrap/*.sh ~/.local/bin/   # from the Odoo checkout
```

On Ubuntu 24.04 and later, load the AppArmor profile bwrap needs, or it fails with `bwrap: setting up uid map: Permission denied`:

```sh
sudo apt install apparmor-profiles
sudo ln -s /usr/share/apparmor/extra-profiles/bwrap-userns-restrict /etc/apparmor.d/
sudo apparmor_parser /etc/apparmor.d/bwrap-userns-restrict
```

Each script is a drop-in replacement for its CLI: `bwrap-claude.sh` starts Claude, not a shell. `ODOO_BASE` is the directory mounted read-write and the sandbox's working directory; it defaults to `~/src/odoo`. `--add-dir <path>` mounts one more directory read-write, and `bwrap-claude.sh` also passes it to Claude's own `--add-dir`.

### Claude Code

Add to `~/.bashrc`, `~/.zshrc` or equivalent:

```bash
claude-sandbox() {
  ws=$(command ow cd) || return
  ODOO_BASE="$ws" bwrap-claude.sh --add-dir "${XDG_DATA_HOME:-$HOME/.local/share}/ow/repos" "$@"
}
```

Then, in a workspace:

```sh
claude-sandbox
```

Claude starts on the workspace, with the bare repos mounted so git works. `command ow cd` bypasses the `ow cd` shell function installed by `ow shell-init`.

### Opencode and Pi

```sh
ODOO_BASE=$(command ow cd) bwrap-opencode.sh --add-dir "${XDG_DATA_HOME:-$HOME/.local/share}/ow/repos"
ODOO_BASE=$(command ow cd) bwrap-pi.sh --add-dir "${XDG_DATA_HOME:-$HOME/.local/share}/ow/repos"
```

## Firejail

Firejail applies a profile. Copy the ones Odoo ships, then edit `~/.config/firejail/code.local`: it whitelists `~/src` by default.

```sh
sudo apt install firejail
mkdir -p ~/.config/firejail
cp setup/sandboxing/firejail/* ~/.config/firejail/   # from the Odoo checkout
```

### Claude Code standalone

From a workspace:

```sh
firejail --profile=~/.config/firejail/claude.profile \
  --whitelist=$PWD --whitelist="${XDG_DATA_HOME:-$HOME/.local/share}/ow/repos" claude
```

Firejail always creates a PID namespace, so `claude /ide` cannot attach to your editor; use `bwrap-claude.sh` when you need that.

Claude's auto-update breaks under the sandbox: `~/.local/bin/claude` is a symlink and the sandbox hides its parent directory. Reinstall outside the sandbox when Claude stops starting:

```sh
rm -f ~/.local/bin/claude && curl -fsSL https://claude.ai/install.sh | bash
```

### The whole editor

`code.local` whitelists your projects directory and the editor's own config, so the editor, its extensions, the integrated terminal and any agent started from it all run inside the sandbox:

```sh
firejail code                                          # VSCode: code.profile includes code.local
firejail --profile=/etc/firejail/code.profile codium   # VSCodium, Cursor: no codium.profile
```

## Trade-offs

- Writes stay inside the mounted directories. `~/.ssh`, `~/.gnupg` and the rest of `$HOME` are hidden; `bwrap-claude.sh` refuses to start if it can still read them.
- The whole `$XDG_DATA_HOME/ow/repos` directory is visible, not only the current workspace's bare repos.
- Network stays available: the bwrap scripts share the network namespace, and the firejail profile keeps `inet`.
- System binaries are mounted read-only.

For network or resource isolation beyond this, use a container.

## See also

- [Commands](commands.md) — `ow cd` and `ow shell-init`.
- [Migrating to 3.0](migrating-to-3.0.md) — the sandbox tasks ow 2 had are gone.
