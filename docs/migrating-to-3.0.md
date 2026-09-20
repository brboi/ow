# Migrating to ow 3.0

ow 3.0 removes the template system and replaces it with a fixed set of generated files, and it
bumps the config schema to 2. The template bundles, the `.j2` sources, the user override tree,
`[vars]`, `ow apply` and `ow templates` are all gone — not renamed, not aliased.

If you are coming from ow 1.x, do the layout move first ([Migrating from 1.x to the 2.0
layout](migrating-to-2.0.md)); this page is the second step, and it is the one that converts the
config.

## Before you start

**Upgrade mise.** ow 3.0 writes its settings as a *fragment* — `mise/conf.d/00-ow.toml` — instead
of a root `mise.toml`, and that needs mise `2026.8.13` or newer. Every command that has to touch
the fragment checks this and stops with the version it found. Upgrade mise before running `ow
render`:

```sh
mise self-update
```

**Nothing migrates itself.** A schema-1 config is still readable — `ow status`, `ow files`,
`ow ls`, the dashboard and every Git command work on it — but only `ow init` and `ow render`
convert it, explicitly, with a backup. Automatic refreshes never do: after a `ow switch` or
`ow pull` on a schema-1 workspace, ow says the files were not refreshed and points at
`ow render -w <path>`.

## Commands that no longer exist

| Removed | Use instead |
|---------|-------------|
| `ow apply` | `ow init` to repair a workspace (recreate a missing worktree, add a repo); `ow switch` to realign a repo that drifted from its spec; `ow render` to write the generated files |
| `ow apply --check` | `ow files --diff` for the file half, `ow status` for the Git half |
| `ow templates` | `ow files` |
| `ow templates --take` | nothing — there is no override tree any more; see [Template customizations](#template-customizations) |
| `ow init -t/--template` | nothing — the outputs are fixed; a file you want to opt out of is ignored with `owignore` |
| `ow update` (gone since 2.0) | `ow render` |

They are not recognized: `ow apply` fails with Typer's unknown-command error, and template
completion is gone with them. Guidance that names them is stale — the replacement is not one
command.

## Running the migration

Either command converts, and both do the same preflight:

```sh
ow render                 # the current workspace (OW_WORKSPACE or walk-up)
ow render parrot          # by name
ow render -w ./parrot     # by path
ow init                   # repairing an existing workspace does it too
```

The preflight reads both the global config and the workspace's, and **either side's blockers abort
every write** — a workspace whose data cannot be represented must not have its lock retired either.
Blockers are data ow cannot carry over: an unknown legacy `vars` key, a value the typed parsers
reject (a port out of range, a wrong type), a malformed `templates` list, or a lock it cannot read.
They are printed with the path and the key, and nothing is written until you resolve them.

Customizations are *not* blockers. ow cannot keep rendering a template it no longer ships, and it
must not overwrite what that template produced, so customizations are handled by retirement
instead — see below.

After the configs are converted, `ow render` writes the generated files and trusts the mise
fragment. `ow init` does the same after materializing what is missing.

## Backups

Every config the migration replaces is preserved first, byte for byte, at:

```
$XDG_STATE_HOME/ow/backups/migrations/<sha256 of the absolute source path>/<sha256 of the original bytes>.toml
```

Both path segments are hex digests, so a backup is not identifiable by name — the directory names
the file it came from, the file names the content. Two consequences are deliberate:

- A retry after an interruption cannot be blocked by a half-written backup of its own: the same
  original always lands on the same name, and an existing backup with *equal* bytes is reused.
- A backup whose bytes differ from what the migration would write is refused rather than
  overwritten, so a mismatch is never silent data loss.

`ow prune --also-backups` deletes the `ow rm` backups in `$XDG_STATE_HOME/ow/backups/`; it never
touches `backups/migrations/`.

## What happens to `[vars]`

Each schema-1 key has exactly one schema-2 home:

| 1.x / 2.0 `[vars]` key | 3.0 key |
|------------------------|---------|
| `python` | `mise.python` |
| `http_port` | `odoo.http_port` |
| `db_host` | `odoo.db_host` |
| `db_port` | `odoo.db_port` |
| `db_user` | `odoo.db_user` |
| `db_password` | `odoo.db_password` |
| `admin_passwd` | `odoo.admin_passwd` |
| `smtp_server` | `odoo.smtp_server` |
| `smtp_port` | `odoo.smtp_port` |
| `debug_args` | `odoo.debug_args` |
| `debug_test_args` | `odoo.debug_test_args` |

A transitional `[odoo]`/`[mise]` table, if the file already had one, wins over `[vars]` for its own
field only. Any other `vars` key is an unknown var and blocks the migration — resolve it by moving
it into a supported key or deleting it, then re-run.

### Live defaults versus preserved local overrides

This is the biggest behavioural change, so it is worth stating plainly.

In 2.0 the render context read *only* the workspace's own `vars`: global `[vars]` were just the
initial values `ow init` copied in, and editing the global file afterwards changed nothing.
In 3.0 the levels are sparse and inherited: **built-in default < global config < workspace
config**.

The migration preserves what each workspace actually had: every `[vars]` key it carried becomes an
explicit local override in its `.ow/config.toml`. A key the workspace never set stays unset, and
now follows the global value — which makes the global `[odoo]`/`[mise]` tables *live* defaults for
every workspace that has not overridden them. To let a workspace follow a global value again,
delete the key from its `.ow/config.toml`; clearing an override is a deletion, not a copy of the
current default.

Built-in defaults apply when neither level sets a key, so a workspace that set nothing gets
`http_port = 8069`, `db_host = localhost`, `db_port = 5432`, `db_user = odoo`,
`db_password = odoo`, `admin_passwd = Password`, `smtp_server = localhost`, `smtp_port = 25`, and
`mise.python = 3.12` clamped into the core's supported range. See
[Configuration](configuration.md) for the full table.

`smtp_port` is the one to check by hand: 2.0's bundled Mailpit listened on `1025`, and if you were
pointing Odoo at it you set `smtp_port = 1025` in `vars` — the migration carries it over. If you
never set it, you keep Odoo's own default of `25`.

## Template customizations

Old ow rendered from bundles: `common`, `odoo`, `vscode`, `zed`, `bwrap`, plus any custom bundle
you declared in `templates`. 3.0 renders from a fixed table instead, so a bundle — and every file
in it — is **retired**, with a report naming each source and the output it produced:

- **Your override, custom bundle, or an unprovable copy** of a path the new generators also
  produce retires that output's lock entry. ow stops owning the file. It stays exactly as it is
  on disk, and `ow files` reports it as `yours` — the new proposal never overwrites it. If the
  new proposal happens to be byte-identical to your file, the next render adopts it as `up to
  date` instead.
- **A stock copy** of a shipped bundle changes nothing: the file was already ow's, and the fixed
  generator takes over the paths it produces.
- **A path no generator produces** keeps its lock entry and stays visible as `not rendered` while
  the file is on disk. Nothing rewrites or deletes it.
- **A declared custom bundle with no source left** cannot be attributed, so every generated entry
  of that workspace is retired, with a report saying exactly that.

The retirement report is printed by `ow render` (and by `ow init`) as warnings. Review the
resulting states with `ow files`, and each difference with `ow files --diff`. There is no
`--force`, no `--take`, and no `template-base` command: to take a file back, delete it and let the
next render write it, or edit the file yourself and ow will leave it alone.

Old sources are never deleted. `$XDG_CONFIG_HOME/ow/templates/` and a workspace's old
`.ow/templates/` copy are read only by the migration, to classify stock versus custom; nothing
renders from them afterwards.

### Lockless outputs

A file in the workspace that no lock entry covers is `yours`. 2.0 could leave files behind that
way — an output written before the lock existed, a file from a second render pass, a copy you made
yourself — and 3.0 does not infer ownership from a filename. Every one of them is left alone, and
adoption happens only when the bytes are already identical to what ow would write. If you want ow
to own one, delete it and run `ow render`.

## The generated files

The nine outputs are fixed; there are no bundles to choose. `mise.toml` is replaced by
`mise/conf.d/00-ow.toml`, and the six Odoo-only files (including `.vscode/launch.json` and
`.zed/debug.json`) exist only when a declared repo is the Odoo core. See
[Generated files](files.md) for the table, the ownership states, and the ignore list.

### The editor files

Both editors are generated by default, and their generation changed shape: they are now valid
JSON produced by serializers, not JSONC text produced by templates — comments you had added to
`.vscode/settings.json` or `.zed/settings.json` are not a compatibility API. A file you edited
is `yours` and stays as it is; a file ow owned is rewritten. Opting out of one editor's files is
now a global ignore (`owignore`), not a bundle you decline to declare.

The `editor` and `theme` settings themselves are unchanged, and remain global.

## Seeds

There is no automatic conversion of old templates into seeds. If a file you used to render was
really a static file you wanted copied into every workspace, put it under
`$XDG_CONFIG_HOME/ow/local/` and `ow init` will copy it into a new workspace's `.local/`, once,
before addon discovery runs. The copy is missing-only: existing files — changed ones included —
are never overwritten, there is no interpolation, and `ow render`, `ow files` and `ow status`
never seed. Copying your old files in by hand is the migration; keeping the workspace outputs as
they now are is equally fine, and nothing is deleted either way.

## The legacy root `mise.toml`

An older ow wrote a root `mise.toml`, and mise gives it precedence over everything under
`mise/conf.d/` — so it silently shadows the fragment 3.0 generates. ow recognises its own
`OW_WORKSPACE` marker in the file and warns, but it will not delete a file it did not write, and
it will not guess at one without the marker.

To resolve it, delete the root `mise.toml` (or move any keys you added by hand into
`mise.local.toml`, which mise also gives precedence and ow never touches), then reopen your shell.
Until it is gone, `ow files --diff` fails the file gate, because the fragment it reports is not
the file mise will actually read.

The same applies to `OW_WORKSPACE`: an old root `mise.toml` may export a workspace *name* where
3.0's fragment exports mise's `{{config_root}}` — an absolute path. A stale name makes every ow
command run inside that shell fail; reopen the shell after removing the file.

## Sandbox wrappers

An older ow rendered its own `bwrap-claude`/`bwrap-opencode` wrappers into the workspace root.
They are not generated any more and stay on disk as `not rendered`; delete them yourself. 3.0
wires tasks into the mise fragment instead, pointing at the scripts and profiles the Odoo checkout
ships under `setup/sandboxing/`. See [Sandboxing](sandboxing.md).

## Archive and `ow rm`

Both are safe across the migration, and neither converts anything:

- `ow archive`/`ow unarchive` move a workspace without touching its schema. A schema-1 archive
  comes back schema 1, exactly as it left; run `ow render` after restoring it. A schema-2
  workspace is refreshed at its new path, so the absolute paths in `odoorc` and the mise fragment
  follow it.
- `ow rm` saves a raw copy of the workspace's `.ow/config.toml` at
  `$XDG_STATE_HOME/ow/backups/<name>-<timestamp>.toml` (mode `0600`) and prints the path with its
  restore hint: `ow init <name> -c <backup>`. The restored workspace is schema 1 if the backup was,
  and `ow render` converts it. `ow prune --also-backups` deletes those copies when you are done
  with them.

## Recovery from an interrupted migration

The migration is ordered so an interruption is always safe to retry:

1. blockers are checked, and any blocker stops everything before a byte is written;
2. every backup is written (lock, global config, workspace config);
3. the lock is retired, then the global config is replaced, then the workspace config —
   atomically, per file.

A crash leaves a schema-1 workspace whose next attempt re-plans the same retirement, and a backup
whose bytes are content-addressed so it cannot be truncated under its own name. Replacements are
atomic per file, not across files: a global config replaced followed by a failed workspace config
is retryable, not falsely rolled back.

To recover, **just run the migration again**:

```sh
ow render -w <the workspace>
```

A retry must re-plan: the previous plan held the bytes of the file as they were before the
interruption, and the recheck refuses a plan built from stale bytes. That refusal is by design —
only a fresh read can tell what is left to do. If it says a source "changed since migration
planning", you (or another command) edited the file in between: re-running reads it again and
plans from what is there now.
