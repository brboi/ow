# ow — Odoo Workspaces

CLI tool that turns interactive prompts into ready-to-code Odoo workspaces using git worktrees.

## Overview

- **Git Optimized Commands** — Clone Odoo repos in minutes using shared bare repos
- **Workspace generation** — each workspace is a folder with git worktrees and IDE configs, ready to open in VSCode or Zed
- **Interactive setup** — `ow create` guides you through name, templates, repos, and variables
- **Branch spec syntax** — concise `base..feature` notation to control detached vs attached worktrees
- **Shared bare repos** — all workspaces share the same `.bare-git-repos/`, so fetching once updates refs for all
- **Jinja2 template system** — generates `mise.toml`, `odoorc`, `odools.toml`, `pyrightconfig.json`, and IDE configs from customizable templates
- **Per-workspace variables** — global `[vars]` with per-workspace overrides for ports, DB credentials, etc.
- **Smart rebase** — two-step rebase (upstream then base), with conflict reporting and instructions
- **Rich status** — behind/ahead counts with color-coded output
- **Optional services** — Docker Compose stack with PostgreSQL, pgweb, and mailpit for local development
- **Tab completion** — fish, bash, zsh via `argcomplete`
- **Full transparency** — git commands that change your trees are printed to your terminal before execution

## Prerequisites

- **[mise](https://mise.jdx.dev/)** — manages Python, virtualenvs, and dependencies in generated workspaces
- **Odoo system dependencies** — see [Odoo source install docs](https://www.odoo.com/documentation/master/administration/on_premise/source.html#dependencies) (includes wkhtmltopdf, PostgreSQL client libs, etc.)
- **SSH** — configured for access to Odoo repositories
- **Docker or Podman** (optional) — to run services like postgres, pgweb, mailpit (see `services/`)

## Installation

```sh
pipx install odoo-workspaces   # recommended
pip install odoo-workspaces    # or in an active venv
```

## Quick Start

```sh
ow init                              # initialize project (ow.toml, templates/, services/)
# optionally edit ow.toml to add enterprise or dev remotes
ow create                            # interactive form: name, templates, repos, vars
cd workspaces/my_work && mise install
code workspaces/my_work              # open in your IDE and enjoy
```

## Commands

| Command | Flags | Description |
|---------|-------|-------------|
| `ow init` | `--force`, `--force-with-backup` | Initialize a new ow project |
| `ow create` | `-n/--name`, `-t/--template`, `-r/--repo`, `-c/--configuration` | Create a workspace interactively |
| `ow update` | `[workspace]` | Re-render templates and materialize worktrees |
| `ow status` | `[workspace]` | Show branch status with behind/ahead counts |
| `ow rebase` | `[workspace]` | Fetch and rebase all repos in a workspace |
| `ow prune` | — | Clean up stale worktree references from bare repos |

`ow` walks up from the current directory to find `ow.toml` (the project root). Commands that need a workspace resolve it from the `OW_WORKSPACE` environment variable (set automatically by `mise`), or by walking up for `.ow/config`.

### `ow init`

Initializes a new ow project in the current directory:

- `ow.toml` — minimal config with the Odoo community remote
- `workspaces/` — empty directory for future workspaces
- `templates/` — copy of the bundled templates (customize freely)
- `mise.toml` — Python + Node tooling for ow itself
- `services/` — Docker Compose stack (postgres, pgweb, mailpit)

Use `--force` to overwrite existing files, or `--force-with-backup` to back them up first (`.bak` suffix).

### `ow create`

Interactive form: workspace name → template selection → repo aliases + branch specs → variable defaults. After confirmation:

1. Clones bare repos if needed
2. Fetches required refs
3. Creates worktrees
4. Applies templates in order
5. Writes `workspaces/<name>/.ow/config`
6. Trusts `mise.toml` and prints a reminder to run `mise install`

### `ow update`

Re-renders templates and materializes worktrees for the current workspace. Also creates any missing worktrees and merges new vars from `ow.toml`. Useful after template changes or to regenerate workspace files.

### `ow status`

Fetches latest refs and displays branch status with color-coded behind/ahead counts:

```
[canary]
    branches
        community:  dev/master-canary ↓0 ↑0 (origin/master ↓34 ↑0)
        enterprise: dev/master-canary ↓1 ↑1 (origin/master ↓12 ↑0)
```

### `ow rebase`

Fetches and rebases all repos in a workspace. Before executing, displays a summary of the rebase situation for each repo and asks for confirmation. Handles detached worktrees, force-pushed upstreams (with or without a recoverable fork-point), and normal two-step rebases.

### `ow prune`

Cleans up stale worktree references from all bare repos. Run after manually removing a workspace directory:

```sh
rm -rf workspaces/my-workspace
ow prune
```

## Configuration (`ow.toml`)

### Remotes

```toml
[remotes]
community.origin.url = "git@github.com:odoo/odoo.git"
community.dev.url = "git@github.com:odoo-dev/odoo.git"
community.dev.pushurl = "git@github.com:odoo-dev/odoo.git"
community.dev.fetch = "+refs/heads/*:refs/remotes/dev/*"

enterprise.origin.url = "git@github.com:odoo/enterprise.git"
enterprise.dev.url = "git@github.com:odoo-dev/enterprise.git"
```

Each remote supports `url`, `pushurl` (optional), and `fetch` (optional refspec).

### Variables

```toml
[vars]
http_port = 8069
db_host = "localhost"
db_port = 5432
db_user = "odoo"
db_password = "odoo"
```

Templates use `{{ vars.key | default(fallback) }}` so undefined variables get safe defaults.

### Branch Spec Syntax

| Spec | Worktree mode |
|------|---------------|
| `master` | Detached HEAD at `origin/master` |
| `origin/master` | Detached HEAD at `origin/master` |
| `dev/master-phoenix` | Detached HEAD at `dev/master-phoenix` |
| `master..master-feature` | Attached local branch `master-feature` tracking `origin/master` |
| `dev/master-phoenix..fix` | Attached local branch `fix` tracking `dev/master-phoenix` |

Without `..`, the worktree is detached (read-only tracking). With `..`, a local branch is created — this is what you want for feature development.

## Template System

Templates live in `templates/` at the project root. Each subdirectory is a bundle that can be applied to workspaces during `ow create`. Bundles are applied in order — later ones override files from earlier ones.

| Bundle | Contents |
|--------|----------|
| `common/` | `mise.toml`, `odoorc`, `odools.toml`, `pyrightconfig.json`, `requirements-dev.txt` |
| `vscode/.vscode/` | `settings.json`, `launch.json` |
| `zed/.zed/` | `settings.json`, `debug.json` |
| `bwrap/` | Sandbox scripts for AI coding assistants |

Templates are Jinja2 (`.j2` extension); static files are copied as-is. `ow init` seeds `templates/` with the bundled defaults so you can customize them.

To create a custom bundle:

```sh
mkdir -p templates/my-setup
cp templates/common/odoorc.j2 templates/my-setup/
# edit templates/my-setup/odoorc.j2
```

Then select it during `ow create` or add it to an existing workspace's `.ow/config`.

## Services

Optional containerized services for local development:

```sh
docker compose -f services/compose.yml up -d
```

| Service | Port | Description |
|---------|------|-------------|
| postgres | 5432 | PostgreSQL 17 with pgvector |
| pgweb | 8081 | Web-based PostgreSQL browser |
| mailpit | 8025 / 1025 | Email testing (web UI / SMTP) |

Configure your workspaces to use them via `[vars]` in `ow.toml`:

```toml
[vars]
db_host = "localhost"
db_port = 5432
smtp_server = "localhost"
smtp_port = 1025
```

## Tab Completion

Fish (one-time setup):
```sh
register-python-argcomplete --shell fish ow > ~/.config/fish/completions/ow.fish
```

Bash/Zsh:
```sh
activate-global-python-argcomplete
```

## Sandboxing AI Coding Assistants

`ow` includes sandbox scripts for running AI coding assistants (Opencode, Claude Code) with filesystem isolation using [bubblewrap](https://github.com/containers/bubblewrap).

Install bubblewrap:

```sh
sudo apt install bubblewrap   # Debian/Ubuntu
sudo dnf install bubblewrap   # Fedora
sudo pacman -S bubblewrap     # Arch
```

Add `bwrap` to your workspace templates during `ow create`. The scripts are automatically added to PATH via `mise`:

```sh
bwrap-opencode        # Launch Opencode sandboxed
bwrap-claude          # Launch Claude Code sandboxed
bwrap-opencode --add-dir ~/src/my-addon   # grant access to an extra directory
```

To work on `ow` itself, use the scripts at the project root:

```sh
./bwrap-opencode    # Launch Opencode sandboxed in ow directory
./bwrap-claude      # Launch Claude Code sandboxed in ow directory
```

## Disclaimer

This is a small personal project built with the help of [Claude](https://claude.ai). It scratches a very specific itch — managing multiple Odoo worktrees side by side — and comes with no warranty. Use at your own risk, and expect rough edges.

## Want to contribute?

Contributions are welcome! If something is broken, confusing, or missing — open an issue. If you have a fix or improvement in mind, PRs are appreciated.
