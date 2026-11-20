# Sandboxing AI Coding Assistants

ow's generated Odoo repository is a natural target for AI coding assistants: it's the entire codebase, and any file is editable. The risk is that an uncontrolled agent can write outside the workspace—to your home directory, SSH keys, or system files.

Bubblewrap (`bwrap`) provides lightweight process-level sandboxing using Linux namespaces. You run the agent inside a sandbox that sees only what you mount in, and any changes stay inside that namespace.

## Setup

Install bubblewrap:

```sh
# Debian / Ubuntu
sudo apt install bubblewrap

# Arch
sudo pacman -S bubblewrap

# Fedora
sudo dnf install bubblewrap

# openSUSE
sudo zypper install bubblewrap
```

The sandbox wrapper scripts (`bwrap-claude.sh`, `bwrap-opencode`) are installed on `PATH` via Odoo's install instructions in its README.

## Shell Function for Claude Code

Add this function to your shell profile (`~/.bashrc`, `~/.zshrc`, etc.):

```bash
claude-sandbox() {
  ws=$(command ow cd) || return
  ODOO_BASE="$ws" bwrap-claude.sh --add-dir "${XDG_DATA_HOME:-$HOME/.local/share}/ow/repos" "$@"
}
```

Then run:

```sh
claude-sandbox claude
```

Or just:

```sh
claude-sandbox
```

if `bwrap-claude.sh` starts Claude Code by default.

## Firejail Alternative

[Firejail](https://firejail.sourceforge.io/) is a higher-level sandbox manager with profile-based rules. Install it:

```sh
sudo apt install firejail
```

Create `~/.config/firejail/claude.local`:

```
# Allow edits only in the workspace
whitelist ~/ws
blacklist ~

# Allow network
netfilter yes
```

Then:

```sh
firejail --noprofile --appimage claude
```

Firejail's downside is extra setup and slower startups; its advantage is composable profiles you can share and reuse across agents.

## Whole-Editor Sandboxing

For IDE sandboxing, you can run a whole-editor variant like `code.local` (if installed per Odoo's setup) inside the sandbox. Keep the wrapper outside the sandbox and use the sandboxed binary only for file operations:

1. The editor writes inside the sandbox.
2. You open the result in your editor outside.
3. You run build/tests/linters normally (unsandboxed, so they can access system tools).

This hybrid approach contains the agent's writes while keeping full access to your dev tools.

## Trade-offs

**Protections:**
- File writes stay within the workspace.
- SSH keys and home directory files are inaccessible.
- System binaries and config are read-only.

**Limitation:**
- The entire `~/.local/share/ow/repos` directory is exposed (every workspace's worktrees, not just the current one).

For stronger isolation (network, resource limits), use a full container. For a quick boundary that catches accidental writes, this is it.
