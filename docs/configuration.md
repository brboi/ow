# Configuration

`ow`'s configuration and state live under the XDG base directories, not inside any project.
`$XDG_CONFIG_HOME` defaults to `~/.config`, `$XDG_DATA_HOME` to `~/.local/share`, and
`$XDG_STATE_HOME` to `~/.local/state`.

## File locations

| What | Path | Notes |
|------|------|-------|
| Global config | `$XDG_CONFIG_HOME/ow/config.toml` | remotes, typed defaults, `owignore`, editor, theme |
| Local seeds | `$XDG_CONFIG_HOME/ow/local/` | files copied once into a new workspace's `.local/`, before addon discovery |
| Services | `$XDG_CONFIG_HOME/ow/services/compose.yml` | written by a render with a supported Odoo core; see [Services](services.md) |
| Bare repos | `$XDG_DATA_HOME/ow/repos/` | one `<alias>.git` per remote, shared by every workspace on the machine |
| Container volumes | `$XDG_DATA_HOME/ow/volumes/` | used by the generated `compose.yml` for postgres and mailpit data |
| Workspace archive | `$XDG_DATA_HOME/ow/archives/` | where `ow archive` parks a workspace |
| Workspace index | `$XDG_STATE_HOME/ow/workspaces` | plain list of paths `ow ls` and name lookup read; self-healing, never the source of truth |
| `ow rm` backups | `$XDG_STATE_HOME/ow/backups/<name>-<timestamp>.toml` | raw copies of a removed workspace's `.ow/config.toml`, mode `0600` |
| Migration backups | `$XDG_STATE_HOME/ow/backups/migrations/<sha256-abs-path>/<sha256-bytes>.toml` | every config the schema migration replaced; see [Migrating to 3.0](migrating-to-3.0.md) |

A workspace's own config lives inside it, at `.ow/config.toml`. Its name is not stored: it is the
workspace directory's own name, and the generated `odoorc` uses it for `db_name` and `dbfilter`.

Both config files are written atomically and privately: a same-directory temp file at mode `0600`,
then an `os.replace`. Reading never creates anything. `ow init` and `ow render` write the
workspace's own `.ow/config.toml`; the **global** config is written when you save one — from the
dashboard's global-config editor, or by migrating a schema-1 file — so a machine that has never
saved one has no `config.toml` at all and ow runs on the built-in defaults, the community remote
included.

## Schema version

Both files begin with `version = 2`.

```toml
# global
version = 2

# workspace
version = 2

[repos]
community = "master..my-feature"
```

A `version = 1` file is still readable and writable, but only in the way a 1.x file can be: the
save updates the fields that existed then and preserves everything else byte for byte. For the
global config that is `[remotes]`, `editor` and `theme`; for a workspace it is `[repos]` only.
Typed options cannot be saved to a schema-1 file — changing one is refused with a message naming
`ow render`, the command that converts it. Conversion is always explicit: `ow init` (repairing an
existing workspace) or `ow render`, both of which back up every file they replace under
`$XDG_STATE_HOME/ow/backups/migrations/` before writing. See
[Migrating to 3.0](migrating-to-3.0.md).

A file with a newer version is refused with an upgrade message.

## Remotes

```toml
[remotes]
community.origin.url = "git@github.com:odoo/odoo.git"
community.dev.url = "git@github.com:odoo-dev/odoo.git"
community.dev.pushurl = "git@github.com:odoo-dev/odoo.git"
community.dev.fetch = "+refs/heads/*:refs/remotes/dev/*"

[remotes.enterprise]
origin.url = "git@github.com:odoo/enterprise.git"
```

Each remote supports `url`, `pushurl` (optional), and `fetch` (optional refspec).

## Typed options

Every option is optional, and every level is sparse: a key you do not set is inherited, not
pinned. The precedence is **built-in default < global config < workspace config** — a workspace
without an `[odoo]` table at all just uses the global values and the built-ins. Clearing a local
override means deleting the key, not setting it to a default.

Global config:

```toml
[odoo]
http_port = 8069
db_host = "localhost"
db_port = 5432
db_user = "odoo"
db_password = "odoo"
admin_passwd = "Password"
smtp_server = "localhost"
smtp_port = 25
debug_args = ["--dev=all"]
debug_test_args = ["--test-tags=my-workspace"]

[mise]
python = "3.12"
```

Workspace config (`.ow/config.toml`):

```toml
version = 2

[repos]
community = "master..my-feature"

[odoo]
http_port = 8070

[mise]
python = "3.12"
```

| Key | Type | Built-in default |
|-----|------|------------------|
| `odoo.http_port` | integer, 1–65535 | `8069` |
| `odoo.db_host` | non-empty string | `localhost` |
| `odoo.db_port` | integer, 1–65535 | `5432` |
| `odoo.db_user` | non-empty string | `odoo` |
| `odoo.db_password` | string (may be empty) | `odoo` |
| `odoo.admin_passwd` | string (may be empty) | `Password` |
| `odoo.smtp_server` | non-empty string | `localhost` |
| `odoo.smtp_port` | integer, 1–65535 | `25` |
| `odoo.debug_args` | array of strings | `--dev=all`, plus `--without-demo=all` when the core has no demo mode |
| `odoo.debug_test_args` | array of strings | `--test-tags=<workspace name>` |
| `mise.python` | `"MAJOR.MINOR"` | `3.12` (clamped into the core's supported range) |

An empty string or empty array is an explicit override, not an omission — `debug_test_args = []`
means no test tags, not "use the default". Ports are range-checked, strings are checked for
control characters, and an unknown key is an error rather than a silent no-op.

`smtp_port` defaults to `25`, Odoo's own default. The bundled Mailpit container listens for SMTP
on `1025`; opting into it is `smtp_port = 1025`, not an undocumented change to the default.

### Python and how it is chosen

`mise.python` selects the workspace's Python. For a workspace with an Odoo core, the requested
minor is clamped into the range the core declares (`MIN_PY_VERSION`/`MAX_PY_VERSION`), defaulting
to `3.12` when nothing is set, and a clamp is reported as a warning. A **generic** workspace —
one with no Odoo core — gets no Python at all unless it explicitly opts in by setting
`mise.python` at either level. The generated mise fragment only carries a `python` tool and a
`.venv` when there is something to put there.

## Ignore list

`owignore` in the global config is a list of gitignore-style patterns. A generated file matching
one of them is never written, and `ow files` reports it as `ignored`:

```toml
owignore = [".zed/**", "!.zed/settings.json"]
```

The syntax is `pathspec`'s gitignore dialect: a directory pattern such as `.zed/**` covers
everything under that directory, and a leading `!` re-includes a path. An ignored path that
already has a lock entry
keeps it — ignoring is not retiring, and un-ignoring restores the file to ow's ownership. See
[Generated files](files.md).

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