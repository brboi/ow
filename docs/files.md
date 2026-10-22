# Generated files

ow generates and maintains a set of workspace files: Odoo config, Python environment and IDE settings. It tracks what it wrote, never overwrites a hand edit, and never deletes.

## The lock

`.ow/rendered.lock.toml` records, for each generated path, the SHA-256 of the bytes ow last wrote or adopted. It is the source of truth for ownership.

```toml
# Managed by ow.
".vscode/settings.json" = "d89faae4d0f4ed3f137c2ebab18f9eed714a3a6cfbca1db87551352bbe797c9c"
"mise/conf.d/00-ow.toml" = "0b65e75ac6cfe566dbad81cc1711d03880c718ac28474ce7841b0e34a8cfb8e0"
odoorc = "4bbae601dedf2efc38cdd231f885c4bc3f166b21c87c763191bd1e7679ae7f1a"
```

A flat path → hash map with a one-line header, sorted, keys quoted only when TOML needs it. ow rewrites it only when its bytes change. Do not edit it by hand. A malformed lock is reported (`malformed lock at .ow/rendered.lock.toml; files that differ from ow's are treated as yours`) and treated as empty.

## States

Every generated file has one state, comparing disk with the lock and ow's proposal:

- **`absent`** — the file should exist and doesn't. The next refresh writes it.
- **`up to date`** — disk matches ow's proposal.
- **`outdated`** — disk differs from the proposal but matches the lock: ow wrote it, and something else changed. The next refresh rewrites it.
- **`yours`** — disk matches neither. Someone edited it by hand, or it is not a writable regular file (a directory, a symlink, an unreadable file). ow leaves it alone.
- **`skipped`** — a parent directory is a symlink, or is not a directory, so writing through it is unsafe.

During a refresh, `yours` and `skipped` files are reported:

```
warning: odoorc: not overwritten (yours); delete it and run `ow refresh` to get ow's version back
warning: .zed/settings.json: skipped (.zed is a symlink)
```

A `yours` file that is a directory gets a different hint: `move it away and run \`ow refresh\` to get ow's version back`.

## `ow files`

```sh
ow files            # path <tab> state
ow files --diff     # unified diff of every file that differs
ow files -w myws
```

```
mise/conf.d/00-ow.toml	up to date
odoorc	yours
odools.toml	up to date
pyrightconfig.json	absent
.vscode/settings.json	up to date
.zed/settings.json	skipped	.zed is a symlink
```

Tab-separated, one line per file, no header, stdout verbatim. `yours` and `skipped` lines carry a reason. A lock entry no plugin produces any more has no state column and prints as `stale (no longer produced)`; its file stays on disk:

```
.zed/debug.json	stale (no longer produced)
```

`--diff` reads current → ow's proposal, so `+` lines are what ow would add:

```
--- odoorc
+++ ow's odoorc
@@ -1,5 +1,5 @@
 [options]
-http_port = 9069
+http_port = 8070
```

An absent file has no current bytes, so its diff is all additions. A file ow has no text for prints as `path: binary`.

If any plugin reports an `error` diagnostic, the errors are printed and `ow files` exits 1 — the same diagnostic that would block a refresh. Otherwise it exits 0. `ow files` only reads; it never writes.

## What ow generates

| Plugin | Path | Written when |
|---|---|---|
| mise | `mise/conf.d/00-ow.toml` | a plugin answers `mise_environment`, or `mise.python` is set |
| odoo | `odoorc` (mode `0600`) | the workspace has exactly one readable Odoo core |
| odoo | `odools.toml` | same |
| odoo | `pyrightconfig.json` | same |
| odoo | `requirements-dev.txt` | same |
| vscode | `.vscode/settings.json` | a plugin contributes a language server or an exclude |
| vscode | `.vscode/launch.json` | a plugin contributes a launch config |
| zed | `.zed/settings.json` | a plugin contributes a language server or an exclude |
| zed | `.zed/debug.json` | a plugin contributes a launch config |

`odoorc` holds the `[options]` Odoo loads: ports, database settings, `addons_path`, and `data_dir` pointing at `.odoo/`. A workspace with no Odoo core and no `mise.python` gets nothing.

The local plugin is different: instead of generating files, it copies the regular files under `~/.config/ow/local` into `.local/` when the workspace is created, and only where nothing is there yet. Those copies are not tracked in the lock.

## Never overwrite

ow writes a file only when its state is `absent` or `outdated`. Before writing, it re-reads the destination: if the bytes changed since the projection, that is a hand edit landing in between, so ow leaves the file alone and keeps the old lock entry — the next run reads it as `yours`.

Other guarantees:

- No `--force`. A `yours` file stays until you delete it or make the config match it.
- Writes go through a same-directory temp file and a rename, so a destination is never truncated in place, and a symlink there is never followed.
- ow never deletes files. When a plugin is disabled, its lock entries are dropped and the files stay. Re-enabling regenerates them.

To get ow's version of a `yours` file back:

```sh
ow files --diff                 # see the difference
rm .vscode/settings.json        # accept that yours goes away
ow refresh
```

A corrupted lock is recoverable the same way: delete `.ow/rendered.lock.toml` and refresh. Files matching ow's proposal are relocked as `up to date`; files that differ are reported as `yours`.

## When refresh runs

- After `ow init`, `ow repo add` and `ow repo rm`.
- After `ow switch`, `ow pull`, `ow rebase` and `ow reset`, when a repo actually changed.
- After `ow config set` and `ow config unset`.
- On explicit `ow refresh`.

A dry run or a declined prompt does not refresh.

## Diagnostics

- A `yours` or `skipped` file produces a warning (above).
- A plugin `info` diagnostic is not shown by a refresh; `ow status` prints it as `# <plugin>: <text>`.
- A plugin `warning` diagnostic is reported by its name during a refresh.
- A plugin `error` diagnostic blocks the whole refresh: nothing is written and the command exits 1.
- A generated path that is absolute, escapes the workspace, or targets `.ow/config.toml` or the lock aborts the command, naming the plugin.

## See also

- [Configuration](configuration.md) — the settings behind these files.
- [Commands](commands.md) — `ow refresh` and every other command.
