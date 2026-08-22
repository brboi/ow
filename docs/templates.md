# Template System

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

## Bundles

| Bundle | Contents |
|--------|----------|
| `common/` | `mise.toml`, `odoorc`, `odools.toml`, `pyrightconfig.json`, `requirements-dev.txt` |
| `vscode/.vscode/` | `settings.json`, `launch.json` |
| `zed/.zed/` | `settings.json`, `debug.json` |
| `bwrap/` | Sandbox scripts for AI coding assistants |

Templates are Jinja2 (`.j2` extension); static files are copied as-is. Undefined variables
raise at render time — use `{{ vars.key | default(fallback) }}` for optional values.

## Template context keys

| Key | Description |
|-----|-------------|
| `ws_name` | Workspace name |
| `vars` | Merged dict of `config.vars` and `ws.vars` (use `{{ vars.key \| default(fallback) }}`) |
| `addons_paths` | Ordered list of absolute addon paths |
| `odools_path_items` | Relative paths for `odools.toml` |
| `repos` | List of repo aliases |
| `main_repo_alias` | Alias of the Odoo core repo (has `odoo-bin`), or `None` |

## Custom bundles

To create a custom bundle:

```sh
mkdir -p ~/.config/ow/templates/my-setup
$EDITOR ~/.config/ow/templates/my-setup/odoorc.j2
```

Then select it during `ow init`, or add it to `templates` in an existing workspace's
`.ow/config.toml`.

Overrides are per file, not per bundle: taking `common/odoorc.j2` leaves the rest of `common/`
packaged and up to date.

Once you've restored a customization, run `ow apply` on each workspace that uses it — taking a
file changes nothing already materialized until the workspace is re-applied.
