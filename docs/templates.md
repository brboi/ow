# Template System

Templates are materialised, not read live: `ow init` and `ow apply` copy every file of every
declared bundle into `<ws>/.ow/templates/<bundle>/<relpath>` before rendering, and rendering
reads only that copy. Nothing reaches the workspace straight from a bundle's source; editing a
template means editing the copy inside your own workspace, and nothing else touches that copy
until a later `ow apply` decides it should.

The source a copy comes from is the packaged bundle inside `ow` itself, overridden per file by a
user-local bundle at `$XDG_CONFIG_HOME/ow/templates/<bundle>/` (see
[Custom bundles](#custom-bundles)) — that override tree still wins per file, it just feeds the
copy instead of being read at render time. `<ws>/.ow/templates.lock.toml` records the sha256 of
the source file each copy came from; the lock is the baseline, not a separate directory, and
it's what lets `ow apply` tell your edits apart from ow's own updates.

Every `ow apply` walks each file of each declared bundle and applies exactly one of four rules,
per file, never merging and never writing conflict markers:

- the copy still matches the lock (you never touched it) and the source has moved since — the
  copy is overwritten and printed as `updated <name>`;
- the copy was edited by you and the source is unchanged — left alone, silently;
- the copy was edited by you and the source has also moved — left alone, reported in an
  outdated warning that points at `ow templates --diff`;
- the file is present with no entry in the lock, because you added it by hand — never touched,
  never reported.

A workspace with no `.ow/templates` directory yet — brand new, or created before 2.4.0 — only
ever hits the first of those: every file is copied, printed as `materialised <name>`, and the
lock is written. A pre-2.4.0 workspace needs nothing but its first `ow apply` under 2.4.0 to pick
up the new model.

`ow templates [WORKSPACE] [-w WORKSPACE] [--diff]` describes one workspace's materialised files,
not the whole machine: each is `up to date`, `modified` (you edited it, ow's source hasn't
moved), `outdated` (you edited it and ow's source has moved — the case `--diff` explains), or
`unlocked` (present with no lock entry, added by hand). `--diff` prints a unified diff per
outdated file, from your copy (`(yours)`) to ow's current source (`(ow)`).

One consequence worth knowing: a file ow stops shipping in a bundle upstream is not deleted from
your workspace copy — there is no source left to compare it against, so it never shows as
outdated, it just keeps being rendered. That's the same reason `ow apply` already lists the files
of a bundle you've deactivated as orphans instead of removing them: ow never deletes a
materialised file on your behalf.

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
| `vars` | The workspace's own `vars` (use `{{ vars.key \| default(fallback) }}`) — see [Variables](configuration.md#variables) |
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

Overrides are per file, not per bundle: a user-local `common/odoorc.j2` leaves the rest of
`common/` packaged and current.

Editing a bundle, packaged or user-local, changes nothing already materialised by itself — run
`ow apply` on each workspace that uses it, and the usual four upgrade rules decide what happens
to each file from there.
