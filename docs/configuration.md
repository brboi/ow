# Configuration

`ow`'s configuration and state live under the XDG base directories, not inside any project.
`$XDG_CONFIG_HOME` defaults to `~/.config`, `$XDG_DATA_HOME` to `~/.local/share`, and
`$XDG_STATE_HOME` to `~/.local/state`.

## File locations

| What | Path | Notes |
|------|------|-------|
| Global config | `$XDG_CONFIG_HOME/ow/config.toml` | `[vars]` + `[remotes]`; bootstrapped with a commented default the first time any command needs it |
| Template overrides | `$XDG_CONFIG_HOME/ow/templates/` | populated one file at a time, by `ow templates --take` |
| Services | `$XDG_CONFIG_HOME/ow/services/` | rendered by `ow init` and `ow apply` from the packaged `compose.yml.j2` |
| Bare repos | `$XDG_DATA_HOME/ow/repos/` | one `<alias>.git` per remote, shared by every workspace on the machine |
| Container volumes | `$XDG_DATA_HOME/ow/volumes/` | used by the rendered `compose.yml` for postgres and mailpit data |
| Workspace index | `$XDG_STATE_HOME/ow/workspaces` | plain list of paths `ow ls` and name lookup read; self-healing, never the source of truth |
| Template baselines | `$XDG_STATE_HOME/ow/template-base/` | pristine copies written by `ow templates --take`, used to detect `taken, outdated` |

A workspace's own config lives inside it, at `.ow/config.toml` — it stores that workspace's
`templates`, `repos`, and `vars`. Its name isn't stored there; it's the directory's own name.
Both config files start with `version = 1`; a file with a newer version is refused with an
upgrade message.

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

Templates use `{{ vars.key | default(fallback) }}` so undefined variables get safe defaults.

## Branch Spec Syntax

| Spec | Worktree mode |
|------|---------------|
| `master` | Detached HEAD at `origin/master` |
| `origin/master` | Detached HEAD at `origin/master` |
| `dev/master-phoenix` | Detached HEAD at `dev/master-phoenix` |
| `master..master-feature` | Attached local branch `master-feature` tracking `origin/master` |
| `dev/master-phoenix..fix` | Attached local branch `fix` tracking `dev/master-phoenix` |

Without `..`, the worktree is detached (read-only tracking). With `..`, a local branch is created — this is what you want for feature development.
