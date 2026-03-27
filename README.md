# ow — Odoo Workspaces

CLI tool that turns a single `ow.toml` into ready-to-code Odoo workspaces using git worktrees.

[![ow: Streamline Your Odoo Development Workflow](https://repoclip.io/api/badge/7f89fa43-7287-404e-876d-21ae291b6d72)](https://repoclip.io/v/7f89fa43-7287-404e-876d-21ae291b6d72)

## Overview

Here is what `ow` does for you:

- **Git Optimized Commands** — Odoo repositories are huge, but `ow` will clone the repos in less than 5 minutes!
- **Workspace generation** — each workspace is a folder with git worktrees and IDE configs, ready to open in VSCode or Zed
- **Declarative config** — define remotes, workspaces, and template variables in a single `ow.toml`
- **Branch spec syntax** — concise `base..feature` notation to control detached vs attached worktrees
- **Shared bare repos** — all workspaces share the same `.bare-git-repos/`, so fetching once updates refs for all
- **Jinja2 template system** — generates `mise.toml`, `odoorc`, `odools.toml`, `pyrightconfig.json`, and IDE configs from customizable templates
- **Per-workspace variables** — global `[vars]` with per-workspace overrides for ports, DB credentials, etc.
- **Smart rebase** — two-step rebase (upstream then base), with conflict reporting and instructions
- **Rich status** — behind/ahead counts, clickable branch links, PR detection via `gh`, runbot links
- **Optional services** — Docker Compose stack with PostgreSQL, pgweb, and mailpit for local development
- **Clean removal** — archives workspace config, removes worktrees and branches, preserves bare repos
- **Tab completion** — fish, bash, zsh via `argcomplete`
- **Full transparency** — git command that changes your trees are printed to your terminal before execution

## Prerequisites

- **[mise](https://mise.jdx.dev/)** — manages Python, virtualenvs, and dependencies in generated workspaces
- **Odoo system dependencies** — see [Odoo source install docs](https://www.odoo.com/documentation/master/administration/on_premise/source.html#dependencies) (includes wkhtmltopdf, PostgreSQL client libs, etc.)
- **SSH** — configured for access to Odoo S.A. repositories (private repos require Odoo employee access - but you can still use `ow` with the [public repo](https://github.com/odoo/odoo))
- **Docker or Podman** (optional) — to run services like postgres, pgweb, mailpit (see `services/`)

## Quick Start

```sh
# ow will create ow.toml on first run if not found. It will only contain
# the Odoo repository aliased as 'community'. Create a new workspace with:
ow create my_work community:master      # will add the workspace in ow.toml
ow apply my_work                        # will generate the workspace folder in workspaces/my_work
cd workspaces/my_work && mise install   # install Python, create venv, install pip deps
code workpaces/my_work                  # open it in your favorite IDE and enjoy
```

## File Reference

| Path | Description |
|------|-------------|
| `ow.toml` | Active configuration (remotes, variables, workspaces) — created with defaults on first run |
| `.ow.toml.archived-workspaces` | Removed workspace configs (append-only log) |
| `.bare-git-repos/` | Shared bare git repositories |
| `templates/` | Template bundles (git-tracked subdirs: `common/`, `vscode/`, `zed/`) |
| `templates/<name>/` | Individual template bundle, applied in order to workspaces |
| `workspaces/<name>/` | Generated workspace directories |
| `services/` | Optional Docker/Podman service containers |

## Configuration (`ow.toml`)

### Remotes

Define git remotes per repo alias. `origin` is required; additional remotes are optional:

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

Global variables are defined in `[vars]` and available in all templates via `{{ vars.key }}`:

```toml
[vars]
http_port = 8070
db_host = "localhost"
db_port = 5432
db_user = "odoo"
db_password = "odoo"
```

Per-workspace overrides take precedence:

```toml
[[workspace]]
name = "my-feature"
repo.community = "master..master-my-feature"
vars.http_port = 8080  # overrides the global value for this workspace
```

Templates use `{{ vars.key | default(fallback) }}` so undefined variables get safe defaults.

### Workspaces

Each workspace is a `[[workspace]]` section with a name, templates, repo specs, and optional variable overrides:

```toml
[[workspace]]
name = "opw-123456"
templates = ["common", "vscode"]
repo.community = "master..master-opw-123456-ngram"
repo.enterprise = "master"
vars.http_port = 8080
```

Equivalent long form (TOML syntax):

```toml
[[workspace]]
name = "opw-123456"
templates = ["common", "vscode"]

[workspace.repo]
community = "master..master-opw-123456-ngram"
enterprise = "master"

[workspace.vars]
http_port = 8080
```

The `templates` field is required and specifies which template bundles to apply, in order. Later templates override files from earlier ones.

### Branch Spec Syntax

The repo spec string controls how the worktree is created:

| Spec | Parsed as | Worktree mode |
|------|-----------|---------------|
| `master` | `origin/master` | **Detached** HEAD at `origin/master` |
| `origin/master` | `origin/master` | **Detached** HEAD at `origin/master` |
| `dev/master-phoenix` | `dev/master-phoenix` | **Detached** HEAD at `dev/master-phoenix` |
| `master..master-feature` | base=`origin/master`, branch=`master-feature` | **Attached** local branch `master-feature` tracking `origin/master` |
| `dev/master-phoenix..fix` | base=`dev/master-phoenix`, branch=`fix` | **Attached** local branch `fix` tracking `dev/master-phoenix` |

**Key rule:** without `..`, the worktree is detached (read-only tracking). With `..`, a local branch is created and attached — this is what you want for feature development.

Specifying the remote (`dev/branch`) is optional but required if the branch name exists on multiple remotes.

## Configuration Example

Here's a complete example configuration:

```toml
[remotes]
community.origin.url = "git@github.com:odoo/odoo.git"
community.dev.url = "git@github.com:odoo-dev/odoo.git"
community.dev.pushurl = "git@github.com:odoo-dev/odoo.git"
community.dev.fetch = "+refs/heads/*:refs/remotes/dev/*"

enterprise.origin.url = "git@github.com:odoo/enterprise.git"
enterprise.dev.url = "git@github.com:odoo-dev/enterprise.git"

[vars]
http_port = 8070
db_host = "localhost"
db_port = 5432
db_user = "odoo"
db_password = "odoo"
admin_passwd = "Password"
smtp_server = "mailpit"
smtp_port = 1025

[[workspace]]
name = "opw-123456"
templates = ["common", "vscode"]
repo.community = "master..master-opw-123456-ngram"
repo.enterprise = "origin/master"
vars.http_port = 8080
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

## Services

Optional containerized services for local development:

    docker compose -f services/compose.yml up -d

| Service | Port | Description |
|---------|------|-------------|
| postgres | 5432 | PostgreSQL 17 with pgvector |
| pgweb | 8081 | Web-based PostgreSQL browser |
| mailpit | 8025 / 1025 | Email testing (web UI / SMTP) |

Configure your workspaces to use them via `[vars]`:

    [vars]
    db_host = "localhost"
    db_port = 5432
    smtp_server = "localhost"
    smtp_port = 1025

## Sandboxing AI Coding Assistants

`ow` includes sandbox scripts for running AI coding assistants (Opencode, Claude Code) with filesystem isolation using [bubblewrap](https://github.com/containers/bubblewrap). This restricts the assistant's access to only the workspace directory and essential system paths.

### Prerequisites

Install bubblewrap with your package manager:

```sh
# Debian/Ubuntu
sudo apt install bubblewrap

# Fedora
sudo dnf install bubblewrap

# Arch
sudo pacman -S bubblewrap
```

### Usage from Workspaces

Add `bwrap` to your workspace templates:

```toml
[[workspace]]
name = "my-feature"
templates = ["common", "vscode", "bwrap"]  # Add bwrap
```

Run `ow apply` to generate the sandbox scripts in the workspace directory. The scripts are automatically added to PATH via `mise`:

```sh
cd workspaces/my-feature
bwrap-opencode        # Launch Opencode sandboxed
bwrap-claude          # Launch Claude Code sandboxed
```

### Usage from ow Root

To work on `ow` itself, use the scripts at the project root:

```sh
cd /path/to/ow
./bwrap-opencode    # Launch Opencode sandboxed in ow directory
./bwrap-claude      # Launch Claude Code sandboxed in ow directory
```

### Adding Extra Directories

Use `--add-dir` to grant access to additional directories:

```sh
bwrap-opencode --add-dir ~/src/my-addon
```

### What's Mounted

**Read-write:**
- Workspace directory (or ow root)
- AI tool config/cache directories
- Odoo filestore (if present)
- PostgreSQL socket (if present)

**Read-only:**
- System binaries (`/usr`, `/bin`, `/sbin`, `/lib`)
- TLS certificates, hosts, passwd
- Chrome/Chromium (for browser automation)

**Network access** is preserved for outbound connections. The sandbox uses namespace isolation (IPC, UTS, cgroup, PID) while sharing the network namespace.

## Workflow

```sh
# 1. Create a workspace (templates are required)
ow create opw-123456 community:master..master-opw-123456-ngram enterprise:master templates=common,vscode

# 2. Install dependencies
cd workspaces/opw-123456 && mise install

# 3. Open in your IDE
code workspaces/opw-123456        # VSCode
zeditor workspaces/opw-123456    # Zed

# 4. Develop — the workspace has everything: venv, odoorc, debug configs

# 5. Push your work
cd workspaces/opw-123456/community
git push -u dev HEAD

# 6. Check status (from inside the workspace, no name needed)
ow status

# 7. Rebase on latest upstream
ow rebase

# 8. Clean up when done
ow remove opw-123456
```

## Commands

| Command | Description |
|---------|-------------|
| `ow apply [name]` | Create/update workspaces from config |
| `ow create name alias:spec ... templates=t1,t2,... [vars.key=value ...]` | Create a workspace from CLI (saves to `ow.toml`) |
| `ow status [name] [--all]` | Show branch status, behind/ahead counts, PR links |
| `ow rebase [name]` | Fetch and rebase all repos in a workspace |
| `ow remove name` | Remove workspace, archive config, preserve bare repos |

When `name` is omitted for `status` and `rebase`, `ow` reads the `OW_WORKSPACE` environment variable (set automatically by `mise` when you `cd` into a workspace). If neither is available, `status` shows all workspaces while `rebase` exits with an error.

### `ow apply`

Creates or updates workspaces defined in `ow.toml`. For each workspace:

1. Ensures bare repos exist and fetches required refs (parallel, max 2 workers)
2. Creates worktrees (or reconciles existing ones — detached ↔ attached transitions)
3. Applies templates in order (later templates override earlier ones)
4. For new workspaces: trusts `mise.toml` and prints a reminder to run `mise install`

```sh
ow apply             # all workspaces
ow apply my-feature  # single workspace
```

### `ow create`

Shortcut for adding a workspace without editing `ow.toml` manually. Appends the config and runs `ow apply`:

```sh
# Community feature on master with VSCode
ow create opw-123456 community:master..master-opw-123456-ngram enterprise:master templates=common,vscode

# Enterprise feature with Zed
ow create opw-123456 community:master enterprise:master..master-opw-123456-ngram templates=common,zed

# Both + custom port
ow create opw-123456 community:master..master-opw-123456-ngram enterprise:master..master-opw-123456-ngram templates=common,vscode vars.http_port=8080

# Third-party repo
ow create my-addon community:master ngram-addons:main..main-my-addon
```

### `ow status`

Fetches latest refs and displays branch status with color-coded behind/ahead counts. Use `--all` to show all workspaces even when `OW_WORKSPACE` is set.

Output example:

```
[canary]
    branches
        community:  dev/master-canary ↓0 ↑0 (origin/master ↓34 ↑0)
        enterprise: dev/master-canary ↓1 ↑1 (origin/master ↓12 ↑0)
    links
        pr:     odoo/odoo#12345
        pr:     odoo/enterprise#1234
        runbot: master-canary

[fantastic-iap-service]
    branches
        community:  origin/18.0 ↓27 ↑0 (DETACHED: a1b2c3d)
        enterprise: origin/18.0 ↓11 ↑0 (DETACHED: d9c8b7a)
        iap-apps:   origin/18.0-fantastic-service-ngram ↓0 ↑1 (origin/18.0 ↓27 ↑0)
    links
        pr:     odoo/iap-apps#123
        runbot: fantastic-iap-service
```

```sh
ow status opw-123456  # explicit workspace
ow status             # current workspace (via OW_WORKSPACE) or all
ow status --all       # all workspaces regardless of OW_WORKSPACE
```

Clickable elements (Ctrl+Click in terminal):
- Branch names link to the GitHub tree (e.g. `dev/master-canary` → `github.com/odoo-dev/odoo/tree/master-canary`)
- Commit hashes link to the GitHub commit
- PR numbers link to the pull request
- Runbot links go to `runbot.odoo.com/runbot/bundle/<branch>`

> (!) PR detection uses the `gh` CLI — install it for PR links to appear:
> ```bash
> $ mise -E local use github-cli  # add github-cli as a dependency within mise.local.toml file
> $ gh auth login # do not forget to login
> ```

### `ow rebase`

Fetches and rebases all repos in a workspace. Before executing, `ow` displays a summary of the rebase situation for each repo:

```
[workspace-name]
  community: origin/master ← dev/my-feature (3 commits) [rewritten, recoverable]
  enterprise: origin/master (0 commits)
```

The markers indicate potential issues:
- `rewritten, recoverable` — the upstream branch was force-pushed but `ow` can recover via reset + cherry-pick
- `rewritten, no fork-point` — the upstream was force-pushed and `ow` cannot recover automatically (manual intervention required)
- `N unpushed` — local commits not yet pushed to the upstream

#### Recovery Strategy for Rewritten Upstreams

When an upstream branch has been force-pushed, `ow` detects this using `git merge-base --fork-point`:

1. **Fork-point found**: `ow` offers a recovery strategy:
   - `git reset --hard <upstream>` — reset to the new upstream state
   - `git cherry-pick` each local commit in sequence

2. **Fork-point not found**: `ow` skips the repo and provides manual recovery instructions:
   ```
   Error: Cannot recover some repos - fork-point not found.
   Manual recovery required:
     community:
       git reflog HEAD | head -20  # find previous state
       git cherry-pick <commit>...  # manually reapply
   ```

After displaying the summary, `ow` asks for confirmation before proceeding. This gives you a chance to abort if the situation looks risky.

The strategy applied by `ow` depends on the worktree mode:

For **Detached worktree** (e.g. `community:master`), `ow` does something similar to:
```sh
git fetch origin master
git switch --detach origin/master
```

For **Attached worktree with pushed branch** (e.g. `enterprise:master..master-feature`, where `master-feature` exists on a remote), `ow` does something similar to:
```sh
git fetch origin master
git fetch dev master-feature          # fetch the pushed work branch
git rebase dev/master-feature         # step 1: incorporate remote changes
git rebase origin/master              # step 2: rebase onto base branch
```

For **Attached worktree, local only** (branch not yet pushed), `ow` does something similar to:
```sh
git fetch origin master
git rebase origin/master
```

If a rebase or cherry-pick hits conflicts, `ow` reports the conflicting repo with instructions to continue or abort, then moves on to the next repo.

Workspace name can be omitted when running from inside a workspace (via `OW_WORKSPACE`):

```sh
ow rebase              # rebase current workspace
ow rebase opw-123456   # explicit workspace
```

### `ow remove`

Removes a workspace directory and its worktree/branch references. The bare repo is preserved so you can recreate the workspace later.

The workspace config is archived to `.ow.toml.archived-workspaces` (append-only) and removed from `ow.toml`.

## Workspace Independence & Drift

Once created, a workspace is a regular directory with standard git worktrees. You can `cd` into it, run git commands, switch branches — it works without `ow`.

However, if the worktree state diverges from `ow.toml` (e.g. you manually switch branches), `ow` considers this **drift**. Commands like `rebase`, `remove`, and `status` will warn you when drift is detected, but will proceed anyway. This allows you to use `ow` even when you've made manual changes to the worktrees.

**Golden rule:** one local branch = one worktree. Git enforces this — you can't check out the same branch in two worktrees. Use detached mode (no `..`) when you just need a read-only copy of a version.

To reconcile after manual changes, update `ow.toml` to match reality, then run `ow apply`.

## Template System

Templates live in `templates/` at the project root. Each subdirectory is a template bundle that can be applied to workspaces. Bundled templates are git-tracked:

```
templates/
├── common/           # Core files: mise.toml, odoorc, odools.toml, pyrightconfig.json
├── vscode/.vscode/   # VSCode settings and debug config
└── zed/.zed/         # Zed settings and debug config
```

Workspaces declare which templates to apply via the `templates` field. Templates are applied in order — later templates can override files from earlier ones:

```toml
[[workspace]]
name = "my-feature"
templates = ["common", "vscode"]  # common first, then vscode overrides
```

### Creating custom templates

Create a new directory under `templates/` with your files:

```sh
mkdir -p templates/my-setup
cp templates/common/odoorc.j2 templates/my-setup/
# Edit templates/my-setup/odoorc.j2 to your liking
```

Then use it in your workspace:

```toml
[[workspace]]
name = "my-feature"
templates = ["common", "my-setup"]
```

### Generated files by bundle

**common/**:
| Template | Output | Purpose |
|----------|--------|---------|
| `mise.toml.j2` | `mise.toml` | Python version, venv, pip dependencies |
| `odoorc.j2` | `odoorc` | Odoo server config (ports, DB, addons path) |
| `odools.toml.j2` | `odools.toml` | [OdooLS](https://github.com/odoo/odoo-ls) config |
| `pyrightconfig.json.j2` | `pyrightconfig.json` | Pyright type checker config |
| `requirements-dev.txt` | `requirements-dev.txt` | Copied as-is (static file) |

**vscode/.vscode/**:
| Template | Output | Purpose |
|----------|--------|---------|
| `settings.json.j2` | `.vscode/settings.json` | VSCode settings |
| `launch.json.j2` | `.vscode/launch.json` | VSCode debug config |

**zed/.zed/**:
| Template | Output | Purpose |
|----------|--------|---------|
| `settings.json.j2` | `.zed/settings.json` | Zed settings |
| `debug.json.j2` | `.zed/debug.json` | Zed debug config |

### Jinja2 context

Variables available in all templates:

| Variable | Type | Description |
|----------|------|-------------|
| `ws_name` | `str` | Workspace name |
| `main_repo_alias` | `str` | Alias of the repo containing `odoo-bin` (usually `community`) |
| `repos` | `list[str]` | List of repo aliases |
| `vars` | `dict` | Merged global + per-workspace variables |
| `addons_paths` | `list[str]` | Absolute paths to all addons directories |
| `odools_path_items` | `list[str]` | Relative paths for OdooLS config |

## Disclaimer

This is a small personal project built with the help of [Claude](https://claude.ai). It scratches a very specific itch — managing multiple Odoo worktrees side by side — and comes with no warranty. Use at your own risk, and expect rough edges.

## Want to contribute?

Contributions are welcome! If something is broken, confusing, or missing — open an issue. If you have a fix or improvement in mind, PRs are appreciated. No formal process, just keep it simple.
