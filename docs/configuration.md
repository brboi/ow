# Configuration

Settings control what ow generates and how plugins behave. They are key-value pairs, read with `ow config` and stored in TOML files.

## Layers

Three layers, later ones winning:

1. Built-in defaults, declared by each plugin.
2. Global config: `$XDG_CONFIG_HOME/ow/config.toml`, usually `~/.config/ow/config.toml`.
3. Workspace config: `<workspace>/.ow/config.toml`.

## `ow config`

### List every setting

```sh
ow config
```

One line per setting: `key = value (origin) # help`. `origin` is `default`, `global` or `workspace`. Unset values print as `(unset)`; strings are quoted, as TOML would store them.

```
odoo.http_port = 8069 (default) # HTTP port for Odoo
odoo.db_name = "voip" (default) # Database name (defaults to workspace name)
```

Outside a workspace the same listing works, with workspace-derived defaults unset. `--global` ignores the workspace layer and fills from global and defaults only.

### Get

```sh
ow config get odoo.http_port     # effective value
ow config get --global mise.python
```

A string prints raw, so `$(ow config get odoo.db_host)` is `localhost`, not `"localhost"`. An unset key prints nothing (exit 0). An unknown key is a usage error (exit 2).

### Set

```sh
ow config set odoo.http_port 9069
ow config set --global odoo.smtp_port 1025
```

Without `--global` it writes the workspace config and refreshes the generated files. A list setting takes one item per argument; values beginning with `-` need `--` after the key:

```sh
ow config set -w voip odoo.debug_args -- --dev=all --log-level debug
```

An unknown key is refused (exit 2), with the closest known key suggested. An `int` must parse as an integer, a `bool` must be `true` or `false`.

### Unset

```sh
ow config unset odoo.http_port
```

An unknown key is accepted: unsetting is how you clear the unknown-key warning. `[repos]` is not a setting; use `ow repo rm <name>`.

## `[repos]`

The workspace config also carries the repos of the workspace, as a name → base ref table:

```toml
[repos]
community = "origin/master"
enterprise = "origin/18.0"
```

Each value is validated against the bare repo's remotes: the longest remote-name prefix wins, and a bare ref with no known remote prefix is read as `origin/<name>`. A value ow cannot parse as a base — a SHA, an empty or `refs/`-prefixed ref, an invalid branch name, a non-string — is rejected: the repo is not adopted and a warning names the value.

## Settings

`<plugin>.enabled` exists for every plugin and defaults to `true`. The rest are declared by plugins.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `mise.enabled` | bool | `true` | Enable or disable the mise plugin |
| `mise.python` | str | — | Python version for mise (e.g. '3.11', '3.12') |
| `odoo.enabled` | bool | `true` | Enable or disable the odoo plugin |
| `odoo.http_port` | int | `8069` | HTTP port for Odoo |
| `odoo.db_name` | str | workspace name | Database name |
| `odoo.db_host` | str | `"localhost"` | Database host |
| `odoo.db_port` | int | `5432` | Database port |
| `odoo.db_user` | str | `"odoo"` | Database user |
| `odoo.db_password` | str | `"odoo"` | Database password |
| `odoo.admin_passwd` | str | `"Password"` | Odoo admin password |
| `odoo.smtp_server` | str | `"localhost"` | SMTP server |
| `odoo.smtp_port` | int | `25` | SMTP port |
| `odoo.debug_args` | list | `["--dev=all"]` | Debug arguments for Odoo |
| `odoo.debug_test_args` | list | `["--test-tags=<workspace>"]` | Debug test arguments |
| `vscode.enabled` | bool | `true` | Enable or disable the vscode plugin |
| `zed.enabled` | bool | `true` | Enable or disable the zed plugin |
| `local.enabled` | bool | `true` | Enable or disable the local plugin |

`odoo.debug_args` also gets `--without-demo=all` when the Odoo core predates 19. `odoo.db_name` and `odoo.debug_test_args` are computed from the workspace, so they read as `(unset)` outside one.

Disabling a plugin stops its generated files from being produced; see [Generated files](files.md). Its settings stay readable and are kept in the file.

## File format

The config is TOML with one table per plugin, plus `[repos]`:

```toml
[repos]
community = "origin/master"

[odoo]
http_port = 9069
debug_args = ["--dev=all", "--log-level", "debug"]

[mise]
python = "3.12"
```

Every write rewrites the whole file: known values grouped by plugin in declaration order, unset keys left out, `[repos]` kept. Writes are atomic (a `0600` temp file renamed into place) and serialized by an exclusive lock on a sidecar file next to the target — `<workspace>/.ow/.config.toml.lock` or `~/.config/ow/.config.toml.lock`. The config file itself is `0600`.

## Unknown keys and wrong types

Both are kept in the file, and ow reports one warning per config file, naming every unknown key and every wrong-typed key:

```
warning: unknown setting future.x in .ow/config.toml; remove it with `ow config unset future.x`; odoo.http_port in .ow/config.toml must be int; using its default; run `ow init`
```

- An unknown key is ignored until some ow version declares it. `ow config unset` clears it.
- A key whose TOML type does not match its declared type keeps its default. An int is not a bool.

Because a write rewrites the whole file, any key the rewrite does not carry over is dropped, and every dropped key is printed with its old value:

```
warning: dropped future.x = 1
```

## See also

- [Commands](commands.md) — every command with its flags and exit codes.
- [Generated files](files.md) — what settings produce, and how ownership works.
- [Services](services.md) — the optional postgres + pgvector + mailpit stack these ports point at.
