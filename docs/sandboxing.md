# Sandboxing AI Coding Assistants

`ow` includes sandbox scripts for running AI coding assistants (Opencode, Claude Code) with filesystem isolation using [bubblewrap](https://github.com/containers/bubblewrap).

## Install bubblewrap

```sh
sudo apt install bubblewrap   # Debian/Ubuntu
sudo dnf install bubblewrap   # Fedora
sudo pacman -S bubblewrap     # Arch
```

## Usage

Add `bwrap` to your workspace templates during `ow init`. The scripts are automatically added to PATH via `mise`:

```sh
bwrap-opencode        # Launch Opencode sandboxed
bwrap-claude          # Launch Claude Code sandboxed
bwrap-opencode --add-dir ~/src/my-addon   # grant access to an extra directory
```

To work on `ow` itself, use the scripts at the repository root:

```sh
./bwrap-opencode    # Launch Opencode sandboxed in ow's own repo
./bwrap-claude      # Launch Claude Code sandboxed in ow's own repo
```
