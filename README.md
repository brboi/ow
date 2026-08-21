# ow — Odoo Workspaces

CLI tool that turns interactive prompts into ready-to-code Odoo workspaces using git worktrees.

> Upgrading from a pre-2.0 install? Read [`docs/migrating-to-2.0.md`](docs/migrating-to-2.0.md)
> first — there is no project root any more.

## Overview

- **Git Optimized Commands** — Clone Odoo repos in minutes using shared bare repos
- **Workspace generation** — each workspace is a folder with git worktrees and IDE configs, ready to open in VSCode or Zed
- **Interactive setup** — `ow init` guides you through templates, repos and branch specs
- **Branch spec syntax** — concise `base..feature` notation to control detached vs attached worktrees
- **Shared bare repos** — every workspace on the machine shares the same set of bare repos, so fetching once updates refs for all of them, not just the ones under one project
- **Jinja2 template system** — generates `mise.toml`, `odoorc`, `odools.toml`, `pyrightconfig.json`, and IDE configs from customizable templates
- **Per-workspace variables** — global `[vars]` with per-workspace overrides for ports, DB credentials, etc.
- **Idempotent rebase** — integrates a published branch only when it has moved
- **Rich status** — behind/ahead counts with color-coded output
- **Optional services** — Docker Compose stack with PostgreSQL, pgweb, and mailpit for local development
- **Tab completion** — fish, bash, zsh, powershell via `ow --install-completion`
- **Full transparency** — git commands that change your trees are printed to your terminal before execution

## Prerequisites

- **[mise](https://mise.jdx.dev/)** — manages Python, virtualenvs, and dependencies in generated workspaces
- **Odoo system dependencies** — see [Odoo source install docs](https://www.odoo.com/documentation/master/administration/on_premise/source.html#dependencies) (includes wkhtmltopdf, PostgreSQL client libs, etc.)
- **SSH** — configured for access to Odoo repositories
- **Docker or Podman** (optional) — `ow` ships a compose file for postgres, pgweb and mailpit, but runs none of it itself; see [Services](#services)

## Installation

```sh
pipx install odoo-workspaces   # recommended
pip install odoo-workspaces    # or in an active venv
```

## Quick Start

```sh
mkdir -p ~/.config/ow              # ow does not create this on its own
$EDITOR ~/.config/ow/config.toml   # add your remotes (or skip both lines and
                                   # let `ow init` below write a commented
                                   # default first)
mkdir my_work && cd my_work
ow init                            # interactive form: templates, repos, branch specs
mise install
code .                             # open in your IDE and enjoy
```

## Commands

| Command | Flags | Description |
|---------|-------|-------------|
| `ow init` | `[NAME]`, `-c/--configuration`, `-t/--template`, `-r/--repo` | Create a workspace here, or in `./NAME` |
| `ow apply` | `[workspace]`, `--only` | Re-render templates and materialize worktrees |
| `ow status` | `[workspace]` | Show branch status with behind/ahead counts |
| `ow rebase` | `[workspace]`, `--only`, `--autostash`, `--dry-run`, `-y/--yes` | Fetch and rebase repos in a workspace |
| `ow prune` | `--dry-run`, `-y/--yes` | Clean up stale worktree references, orphaned branches, and dead index entries |
| `ow ls` | — | List every known workspace, its path, and its repos |
| `ow templates` | `--take`, `--diff` | List template files and their state, take one, or diff the stale ones |

A command that takes a `[workspace]` resolves it in exactly one of four forms, never falling
back from one to the next:

- a **path** (starts with `~`, is `.`/`..`, or contains a separator, e.g. `./canary`) — must
  contain `.ow/config.toml` or the command fails
- a bare **name** (e.g. `ow status canary`) — looked up in the discovery index; zero matches or
  more than one is an error naming the fix (`ow ls`, or pass a path)
- no argument, **`OW_WORKSPACE` set** — must be an absolute path (not a name, not `~`, not
  relative); `mise` exports it as such automatically inside a generated workspace
- no argument, **`OW_WORKSPACE` unset** — walk up from the current directory looking for
  `.ow/config.toml`

`ow init` doesn't go through this: it resolves its *target* directory itself (the current
directory, or `./NAME`), since the workspace doesn't exist yet.

### `ow init`

Creates a workspace: in the current directory by default, or in `./NAME` if given — mirrors
`git init`. Interactive by default (templates → repos → branch specs, pre-filled from any flags
given); when stdin isn't a terminal, flags (or `-c/--configuration`, to duplicate an existing
workspace's config) must supply everything, or the command refuses to guess.

```sh
ow init my_work -r community:master..my-feature -r enterprise:master..my-feature -t common -t vscode
```

`-r` takes a single `ALIAS:SPEC` argument and `-t` a single template name; repeat either flag
to pass more than one. A `-r` value without a `:` is rejected rather than ignored. `NAME`, when
given, must be alphanumeric plus `-`/`_`.

After confirmation, `ow` sets up each repo's bare clone and required refs, creates (or
reconciles) its worktree, applies templates, writes `.ow/config.toml`, trusts `mise.toml` if the
templates produced one, and remembers the workspace in the discovery index. A repo that fails to
set up is reported; the workspace is still created as long as at least one repo succeeded, and
the command exits non-zero — the workspace exists, but it is not the one you asked for.

### `ow apply`

Re-renders templates and materializes worktrees for a workspace: creates any missing worktree,
reconciles attached/detached state for existing ones, and tops up the workspace's `vars` with
any global default not already overridden there. Useful after changing templates or the global
config without recreating the workspace.

`--only alias1,alias2` narrows which repos get materialized; templates still render against the
whole config, since `addons_path` is built from every repo regardless.

If any template file you took has since changed upstream, `ow apply` lists it and points at
`ow templates --diff`.

Like `ow init` and `ow rebase`, `ow apply` exits non-zero when any repo failed, even though
everything else — templates, vars, the repos that worked — is applied.

### `ow status`

Fetches latest refs and displays branch status with color-coded behind/ahead counts:

```
[canary]
    branches
        community:  dev/master-canary ↓0 ↑0 (origin/master ↓34 ↑0)
        enterprise: dev/master-canary ↓1 ↑1 (origin/master ↓12 ↑0)
    links
        runbot: master-canary
        community:  https://github.com/odoo-dev/odoo/tree/master-canary
        enterprise: https://github.com/odoo-dev/enterprise/tree/master-canary
```

### `ow rebase`

Fetches the latest refs and rebases each repo of a workspace onto its base branch.
Shows a summary and asks for confirmation before touching anything.

```sh
ow rebase                                  # every repo of the current workspace
ow rebase parrot --only community          # one repo of a named workspace
ow rebase --dry-run                        # fetch, then print the plan — no worktree touched
```

Running it twice in a row with nothing changed in between does nothing the second
time — no commit is rewritten.

For a repo whose branch is also published on a remote (`master..my-feature` with
`dev/my-feature` pushed), `ow` first integrates that remote copy only when it
carries commits yours does not, then rebases everything onto the base branch. A
force-pushed remote copy is detected by comparing the ref before and after the
fetch, and handled with a single `git rebase --onto`.

A repo is skipped, and the run exits non-zero, when a git operation is already in
progress (the message gives the exact `--continue` / `--abort` command), when the
worktree has uncommitted changes, or when the worktree is missing. `--autostash` stashes and restores uncommitted changes instead.
`--only` restricts the whole run to the selected repos, including drift warnings and ref fetching.

On conflict, resolve, `git rebase --continue`, then re-run `ow rebase --only <alias>`. Nothing is
ever pushed: the `git push --force-with-lease` stays yours.

`--dry-run` fetches refs to show you what would happen, but runs no command that
touches your worktrees.

### `ow prune`

Cleans up stale worktree references and orphaned local branches from every bare repo, and drops
dead entries from the workspace discovery index. Run after manually removing a workspace
directory:

```sh
rm -rf ~/wherever/my-workspace
ow prune
```

Deleting a branch is the one step that can lose work, so it is confirmed first,
defaulting to no; `-y/--yes` skips the prompt and `--dry-run` stops after the
survey. A branch holding commits no remote has is never deleted — it is listed,
with the command to delete it by hand.

### `ow ls`

Lists every workspace `ow` currently knows about, in name order — name, path (home-relative), and its repos
with their branch specs — read from the discovery index and each workspace's own
`.ow/config.toml`. No git, no network: this is local files only. A workspace config that fails
to parse shows as an error in place of its repos rather than aborting the listing.

### `ow templates`

Lists every template file `ow` can use, with its state:

- `packaged` — shipped inside `ow`, unmodified
- `taken` — you have a local override
- `taken, outdated` — the packaged file changed since you took it

`--take BUNDLE/PATH` copies one packaged file into your local overrides, plus a pristine
baseline used later to detect drift. `--diff` prints a unified diff (baseline vs. current
packaged) for every outdated file. See [Template System](#template-system).

## Configuration

`ow`'s configuration and state live under the XDG base directories, not inside any project.
`$XDG_CONFIG_HOME` defaults to `~/.config`, `$XDG_DATA_HOME` to `~/.local/share`, and
`$XDG_STATE_HOME` to `~/.local/state`.

| What | Path | Notes |
|------|------|-------|
| Global config | `$XDG_CONFIG_HOME/ow/config.toml` | `[vars]` + `[remotes]`; bootstrapped with a commented default the first time any command needs it |
| Template overrides | `$XDG_CONFIG_HOME/ow/templates/` | populated one file at a time, by `ow templates --take` |
| Services | `$XDG_CONFIG_HOME/ow/services/` | conventional place for your own copy of the packaged compose file; no `ow` command reads it |
| Bare repos | `$XDG_DATA_HOME/ow/repos/` | one `<alias>.git` per remote, shared by every workspace on the machine |
| Container volumes | `$XDG_DATA_HOME/ow/volumes/` | defined for this purpose, but the packaged `compose.yml` uses `./volumes/`, relative to wherever you keep the file — not this directory |
| Workspace index | `$XDG_STATE_HOME/ow/workspaces` | plain list of paths `ow ls` and name lookup read; self-healing, never the source of truth |
| Template baselines | `$XDG_STATE_HOME/ow/template-base/` | pristine copies written by `ow templates --take`, used to detect `taken, outdated` |

A workspace's own config lives inside it, at `.ow/config.toml` — it stores that workspace's
`templates`, `repos`, and `vars`. Its name isn't stored there; it's the directory's own name.

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

Templates are lazy: nothing is copied anywhere by `ow init` or `ow apply`. Each workspace
declares a `templates` list (bundle names); at render time, `ow` reads each bundle's files
straight out of wherever they live — packaged inside `ow` itself, or, for a file you've taken,
your local override — and renders them directly into the workspace.

Overriding is **per file, not per bundle**: `ow templates --take common/odoorc.j2` puts your own
copy of that one file at `$XDG_CONFIG_HOME/ow/templates/common/odoorc.j2`; the rest of `common/`
keeps coming from the packaged version and stays current. `--take` also writes a pristine
baseline copy alongside the file, in `$XDG_STATE_HOME/ow/template-base/` — that's how `ow
templates` can later tell you a file you took has drifted from what `ow` now ships
(`taken, outdated`), and `ow templates --diff` can show you exactly what changed. A file you copy
in by hand instead of through `--take` has no baseline, so it's never flagged as stale.

| Bundle | Contents |
|--------|----------|
| `common/` | `mise.toml`, `odoorc`, `odools.toml`, `pyrightconfig.json`, `requirements-dev.txt` |
| `vscode/.vscode/` | `settings.json`, `launch.json` |
| `zed/.zed/` | `settings.json`, `debug.json` |
| `bwrap/` | Sandbox scripts for AI coding assistants |

Templates are Jinja2 (`.j2` extension); static files are copied as-is.

To create a custom bundle:

```sh
mkdir -p ~/.config/ow/templates/my-setup
$EDITOR ~/.config/ow/templates/my-setup/odoorc.j2
```

Then select it during `ow init`, or add it to `templates` in an existing workspace's
`.ow/config.toml`.

## Services

`ow` packages a Docker Compose stack (postgres, pgweb, mailpit) for local development, but no
`ow` command starts, stops, or otherwise reads it — you drive it yourself with plain `docker
compose`. Fetch it into `$XDG_CONFIG_HOME/ow/services/` — the conventional spot — and run it
from there:

```sh
mkdir -p ~/.config/ow/services
curl -fsSL https://raw.githubusercontent.com/brboi/ow/main/src/ow/_static/services/compose.yml \
    -o ~/.config/ow/services/compose.yml
docker compose -f ~/.config/ow/services/compose.yml up -d
```

It is also readable in the
[repo](https://github.com/brboi/ow/blob/main/src/ow/_static/services/compose.yml). If you
installed with `pip install` into an active venv rather than `pipx` (which puts `ow` in its own
venv, unreachable from the system `python`), that venv's `python` can get you the same file:

```sh
cp "$(python -c 'import ow, pathlib; print(pathlib.Path(ow.__file__).parent / "_static/services/compose.yml")')" \
   ~/.config/ow/services/
```

| Service | Port | Description |
|---------|------|-------------|
| postgres | 5432 | PostgreSQL 17 with pgvector |
| pgweb | 8081 | Web-based PostgreSQL browser |
| mailpit | 8025 / 1025 | Email testing (web UI / SMTP) |

Point your workspaces at them via `[vars]`, either globally or per workspace:

```toml
[vars]
db_host = "localhost"
db_port = 5432
smtp_server = "localhost"
smtp_port = 1025
```

## Tab Completion

One-time setup for your current shell:
```sh
ow --install-completion
```

Then restart your shell. To inspect the generated script instead of installing it:
```sh
ow --show-completion
```

Completion covers template names (`ow init -t <TAB>`), repo aliases (`ow init -r <TAB>`,
which only offers aliases you haven't already passed) and workspace names
(`ow status <TAB>`, from the same discovery index `ow ls` reads — so a workspace `ow` has
never resolved is not offered).

## Sandboxing AI Coding Assistants

`ow` includes sandbox scripts for running AI coding assistants (Opencode, Claude Code) with filesystem isolation using [bubblewrap](https://github.com/containers/bubblewrap).

Install bubblewrap:

```sh
sudo apt install bubblewrap   # Debian/Ubuntu
sudo dnf install bubblewrap   # Fedora
sudo pacman -S bubblewrap     # Arch
```

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

## Disclaimer

This is a small personal project built with the help of [Claude](https://claude.ai). It scratches a very specific itch — managing multiple Odoo worktrees side by side — and comes with no warranty. Use at your own risk, and expect rough edges.

## Want to contribute?

Contributions are welcome! If something is broken, confusing, or missing — open an issue. If you have a fix or improvement in mind, PRs are appreciated.
