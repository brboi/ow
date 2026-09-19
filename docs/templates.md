# Template System

`ow` renders straight from the bundles packaged inside itself
(`ow/_static/templates/<bundle>/`), overridden per file by a user-local bundle at
`$XDG_CONFIG_HOME/ow/templates/<bundle>/` (see [Custom bundles](#custom-bundles)). There is no
copy in between: `<ws>/.ow/templates/` and `<ws>/.ow/templates.lock.toml` do not exist any more.
A workspace created by an older `ow` still has them lying around — nothing reads them, they are
just dead files, and removing them changes nothing.

Three axes decide what ends up on disk in a given workspace, without overlap. The `common`
bundle is rendered for every workspace, always, and is never named anywhere — it is the one
thing every workspace needs regardless of what it's for. The `odoo` bundle is rendered exactly
when one of the workspace's worktrees looks like the Odoo core repo (`odoo-bin`, `addons/`, and
`odoo/addons/` all present — `is_odoo_main_repo`), and is likewise never declared: a workspace
either has an Odoo checkout or it doesn't, there is nothing to configure. Everything else —
`vscode`, `zed`, `bwrap`, and any bundle of your own — goes through the `templates` field of
`.ow/config.toml`, exactly as before.

`<ws>/.ow/rendered.lock.toml` records the sha256 of every output `ow` wrote (or adopted as
already identical), keyed by its workspace-relative path — the lock is of outputs now, not of the
sources they came from. Each file `ow` would write is in exactly one of four states: absent, and
`ow` writes it; present and byte-identical to what `ow` would render, and `ow` adopts it into the
lock without writing anything; present and matching the lock, and `ow` rewrites it because the
render has changed since; present and *not* matching the lock, meaning you changed it yourself,
and `ow` leaves it alone — there is no reconciliation step, editing a managed file is how you
take it out of `ow`'s hands. A later render that happens to be byte-identical to your file adopts
it back into the lock, but until then nothing `ow` writes will overwrite it. Edit `odoorc`
directly; it is not a template, it is the file, and your edit survives every later `ow apply`.

A `.j2` whose render is nothing but whitespace writes no file at all, and a file left on disk by
an earlier render is not removed either — `ow` never deletes a workspace file, it only stops
speaking for it, and `ow templates` reports it as `not rendered`. The same goes for a path `ow`
wrote in the past that no current bundle produces any more: while the file is still on disk it is
reported `not rendered`, kept in the lock, and neither `ow apply` nor anything else rewrites or
deletes it (delete the file and it disappears from the listing entirely — `ow` keeps no
tombstone). This is why a workspace with no Odoo checkout has neither `.vscode/launch.json` nor
`.zed/debug.json`: the templates exist, they just render empty outside an Odoo workspace. It
doubles as today's opt-out — empty a managed file yourself and it no longer matches the lock, so
it becomes yours and `ow` stops touching it. Deleting it is not an opt-out: a file whose render
is non-empty comes back at the next `ow apply`.

`ow` writes the mise fragment as `mise/conf.d/00-ow.toml`, not `mise.toml`. `mise` gives
`mise.toml` and `mise.local.toml` higher precedence than anything under `conf.d/`, so either one
is yours to keep permanently and `ow` will never contest it. Both `ow init` and `ow apply` run
`mise trust` on every fragment they manage under `mise/` (on a fresh workspace, that is
`mise/conf.d/00-ow.toml`), so nothing waits on a trust prompt; when `mise` isn't available, the
command to run by hand is printed instead. A `mise.toml` left behind by an `ow` from before this
change shadows the fragment silently as far as `mise` is concerned; `ow apply` warns about it
every time but does not remove it — that file might hold things you added by hand.

## Bundles

| Bundle | Contents |
|--------|----------|
| `common/` | `mise/conf.d/00-ow.toml` |
| `odoo/` | `odoorc`, `odools.toml`, `pyrightconfig.json`, `requirements-dev.txt` |
| `vscode/.vscode/` | `settings.json`, `launch.json` (renders empty outside an Odoo workspace) |
| `zed/.zed/` | `settings.json`, `debug.json` (renders empty outside an Odoo workspace) |
| `bwrap/` | Sandbox scripts for AI coding assistants |

Templates are Jinja2 (`.j2` extension); static files are copied as-is. Undefined variables
raise at render time — use `{{ vars.key | default(fallback) }}` for optional values.

## Template context keys

| Key | Description |
|-----|--------------|
| `ws_name` | Workspace name |
| `vars` | The workspace's own `vars` (use `{{ vars.key \| default(fallback) }}`) — see [Variables](configuration.md#variables) |
| `addons_paths` | Ordered list of absolute addon paths |
| `odools_path_items` | Relative paths for `odools.toml` |
| `repos` | List of repo aliases |
| `main_repo_alias` | Alias of the Odoo core repo (has `odoo-bin`), or `None` outside an Odoo workspace — this is the guard a custom bundle should use before assuming any Odoo content applies |

## Custom bundles

To create a custom bundle:

```sh
mkdir -p ~/.config/ow/templates/my-setup
$EDITOR ~/.config/ow/templates/my-setup/odoorc.j2
```

Then select it during `ow init`, or add it to `templates` in an existing workspace's
`.ow/config.toml`.

Overrides are per file, not per bundle: a user-local `odoo/odoorc.j2` leaves the rest of the
packaged `odoo/` bundle in effect.

Editing a bundle, packaged or user-local, changes nothing already on disk by itself — run
`ow apply` on each workspace that uses it, and the states above decide what happens to each file
from there.

`ow templates [WORKSPACE] [-w WORKSPACE] [--diff]` lists the files `ow` manages for one
workspace and their state — `up to date`, `outdated`, `yours`, `absent`, or `not rendered` for a
template that currently renders empty (or whose bundle no longer produces it). `--diff` prints a
unified diff, from your file (`(yours)`) to what `ow` would write (`(ow)`), for every file that
differs: a file that isn't there yet is an addition, from `/dev/null`, and a file whose bytes
aren't UTF-8 text is named with a one-line reason instead of a diff. Like the listing, `--diff`
writes nothing, and differences do not change the exit status: a successful inspection exits 0
whether or not anything differs (a resolution or render failure still fails).
