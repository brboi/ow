# Configuration

Settings control how ow generates files and runs plugins. They are key-value pairs managed with the `ow config` command.

## Precedence

Settings are checked in this order (first found wins):

1. **Workspace config**: `.ow/config.toml` in the workspace directory.
2. **Global config**: `$XDG_CONFIG_HOME/ow/config.toml` (typically `~/.config/ow/config.toml`).
3. **Built-in defaults**: defined by each plugin.

Use `ow config get <key>` to see the effective value (showing which layer it came from is not yet implemented).

## Reading and Writing

### Get

```sh
ow config get odoo.http_port
# Prints the effective value, or — if unset
```

### Set

```sh
ow config set odoo.http_port 9069
# Writes to .ow/config.toml in the workspace
```

### Set Globally

```sh
ow config set --global odoo.smtp_port 1025
# Writes to ~/.config/ow/config.toml
```

### Unset

```sh
ow config unset odoo.http_port
# Removes the key from .ow/config.toml
```

Multi-word values are joined with spaces:

```sh
ow config set odoo.debug_args --no-http --log-level debug
# Stored as: ["--no-http", "--log-level", "debug"]
```

## Settings Table

| Key | Type | Default | Help |
|---|---|---|---|
| `local.enabled` | bool | True | Enable or disable the local plugin |
| `mise.enabled` | bool | True | Enable or disable the mise plugin |
| `mise.python` | str | — | Python version for mise (e.g. '3.11', '3.12') |
| `odoo.admin_passwd` | str | 'Password' | Odoo admin password |
| `odoo.db_host` | str | 'localhost' | Database host |
| `odoo.db_name` | str | (generated) | Database name (defaults to workspace name) |
| `odoo.db_password` | str | 'odoo' | Database password |
| `odoo.db_port` | int | 5432 | Database port |
| `odoo.db_user` | str | 'odoo' | Database user |
| `odoo.debug_args` | list | (generated) | Debug arguments for Odoo |
| `odoo.debug_test_args` | list | (generated) | Debug test arguments |
| `odoo.enabled` | bool | True | Enable or disable the odoo plugin |
| `odoo.http_port` | int | 8069 | HTTP port for Odoo |
| `odoo.smtp_port` | int | 25 | SMTP port |
| `odoo.smtp_server` | str | 'localhost' | SMTP server |
| `vscode.enabled` | bool | True | Enable or disable the vscode plugin |
| `zed.enabled` | bool | True | Enable or disable the zed plugin |

Defaults marked `(generated)` compute from the workspace context. For example, `odoo.db_name` defaults to the workspace directory name if unset.

## Plugin Enablement

Every plugin has a `<plugin>.enabled` setting. Set it to `false` to disable:

```sh
ow config set vscode.enabled false
ow config set zed.enabled false
```

When a plugin is disabled, it does not contribute generated files. For example, disabling VSCode skips `pyrightconfig.json` and `.vscode/` generation. Settings for disabled plugins are still readable and are preserved in the config file; they only take effect if the plugin is re-enabled.

## Unknown Keys

Setting an unknown key is allowed (for forward compatibility):

```sh
ow config set future.plugin.setting value
```

It will be stored but won't be used unless a future version of ow defines it. A warning is printed when reading unknown keys, to catch typos.

## Rewriting and Dropped Keys

On `ow refresh`, if the config file contains keys from an old version of ow that are no longer recognized, they are left as-is (not deleted). If ow rewrites the entire file (which it does not do by default), dropped keys would be lost. To preserve your config, edit `.ow/config.toml` by hand, or use `ow config unset` to remove them explicitly.

## Configuration File Format

The workspace config (`.ow/config.toml`) is a TOML file with one table per plugin:

```toml
[odoo]
http_port = 8069
db_host = "localhost"
smtp_port = 1025

[mise]
python = "3.11"

[vscode]
enabled = false
```

The global config (`~/.config/ow/config.toml`) has the same format. Workspace settings override global ones.

## Example Workflows

**Change the HTTP port:**

```sh
ow config set odoo.http_port 9069
ow refresh
```

**Use a local PostgreSQL on a custom port:**

```sh
ow config set odoo.db_port 5433
ow config set odoo.db_password mysecret
```

**Disable VSCode, keep Zed:**

```sh
ow config set vscode.enabled false
ow refresh
```

**Globally set SMTP for all workspaces:**

```sh
ow config set --global odoo.smtp_port 1025
```

(Useful if you run a Mailpit container; see [Services](services.md).)
