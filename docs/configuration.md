# Configuration

`ow`'s configuration and state live under the XDG base directories, not inside any project.
`$XDG_CONFIG_HOME` defaults to `~/.config`, `$XDG_DATA_HOME` to `~/.local/share`, and
`$XDG_STATE_HOME` to `~/.local/state`.

## File locations

| What | Path | Notes |
|------|------|-------|
| Global config | `$XDG_CONFIG_HOME/ow/config.toml` | `[vars]` + `[remotes]`; bootstrapped with a commented default the first time any command needs it |
| Template overrides | `$XDG_CONFIG_HOME/ow/templates/` | a bundle tree you create yourself; overrides the packaged bundles per file at render time |
| Services | `$XDG_CONFIG_HOME/ow/services/` | rendered by `ow init` and `ow apply` from the packaged `compose.yml.j2` |
| Bare repos | `$XDG_DATA_HOME/ow/repos/` | one `<alias>.git` per remote, shared by every workspace on the machine |
| Container volumes | `$XDG_DATA_HOME/ow/volumes/` | used by the rendered `compose.yml` for postgres and mailpit data |
| Workspace index | `$XDG_STATE_HOME/ow/workspaces` | plain list of paths `ow ls` and name lookup read; self-healing, never the source of truth |

A workspace's own config lives inside it, at `.ow/config.toml` — it stores that workspace's
`templates`, `repos`, and `vars`. Its name isn't stored there; it's the directory's own name.
Both config files start with `version = 1`; a file with a newer version is refused with an
upgrade message.

A workspace's rendered files are compared against a lock inside it too:
`.ow/rendered.lock.toml` records the sha256 of every output `ow` wrote (or adopted as already
identical), keyed by its workspace-relative path — see [Template System](templates.md) for the
states that lock drives. A workspace created by an older `ow` still has the `.ow/templates/` tree
and `.ow/templates.lock.toml` it used to materialise; nothing reads them any more, so edits you
made in that tree are ignored — `ow` renders from the packaged bundles (and
`$XDG_CONFIG_HOME/ow/templates/` overrides) instead.

## Remotes

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

## Variables

```toml
[vars]
http_port = 8069
db_host = "localhost"
db_port = 5432
db_user = "odoo"
db_password = "odoo"
```

The render context reads only the workspace's own `vars`, never the global table directly: the
global `[vars]` are just the initial values `ow init` copies into a new workspace's
`.ow/config.toml` (a `-c/--configuration` source workspace's vars win over the global ones for
keys both define). Editing `$XDG_CONFIG_HOME/ow/config.toml` afterwards therefore only affects
workspaces created from then on; an existing workspace keeps its own copy, and you edit that
copy directly, in its own `.ow/config.toml`.

Templates use `{{ vars.key | default(fallback) }}` so undefined variables get safe defaults.

## Editor and theme

```toml
editor = "code"
theme = "textual-dark"
```

`editor` is the command `ow open` runs (may include flags, e.g. `"code -n"`). `theme` is
the dashboard's Textual theme — press `Ctrl+P` in the dashboard, type "theme", and pick one
from the palette (the choice is persisted here), or set it by hand. Any theme name Textual
ships is accepted; an unknown name is reported and the default is used.

## Branch Spec Syntax

| Spec | Worktree mode |
|------|---------------|
| `master` | Detached HEAD at `origin/master` |
| `origin/master` | Detached HEAD at `origin/master` |
| `dev/master-phoenix` | Detached HEAD at `dev/master-phoenix` |
| `master..master-feature` | Attached local branch `master-feature` tracking `origin/master` |
| `dev/master-phoenix..fix` | Attached local branch `fix` tracking `dev/master-phoenix` |

Without `..`, the worktree is detached (read-only tracking). With `..`, a local branch is created — this is what you want for feature development.
