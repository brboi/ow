# Generated Files and Refresh

ow automatically generates and maintains a set of workspace files: IDE configs, Python environments, Odoo configs, and tool setup.

## File Ownership Model

Every file has a state that determines whether ow manages it:

- **`absent`**: The file should exist but doesn't. ow will write it on the next refresh.
- **`up to date`**: The file on disk matches ow's output exactly. ow maintains it.
- **`outdated`**: The file differs from ow's output but matches ow's previous lock entry (ow wrote it). ow will rewrite it on the next refresh.
- **`yours`**: The file was edited by hand or written by another tool. ow skips it and prints a warning.
- **`skipped`**: The plugin didn't contribute this file (e.g. Zed settings when Zed is disabled).

The ownership is tracked in `.ow/rendered.lock.toml`: a map of path → SHA-256 of the bytes ow last wrote or adopted. This lock is the source of truth.

## Safety Rules

- **ow never deletes files.** If a file should no longer be generated (the plugin is disabled, or the output condition no longer holds), the lock entry is dropped, and the file stays on disk.
- **No `--force` flag.** If a file is "yours", use `ow files --diff` to inspect the difference, then either:
  - Delete the file and run `ow refresh` to get ow's version back.
  - Edit `.ow/config.toml` or the plugin settings to match the file's content.
  - Leave it as-is; ow won't touch it.
- **Symlink safety:** if a file's parent directory is a symlink, the file is skipped (treated as "yours") to avoid unexpected writes through symlinks.

## Refresh Runs When

- After `ow init`, `ow repo add`, or `ow repo rm`.
- After `ow switch`, `ow pull`, `ow rebase`, or `ow reset`, when at least one repo was updated successfully.
- After `ow config set` or `ow config unset`.
- On explicit `ow refresh`.

A dry run (`-n`) or a declined prompt does not trigger refresh. A refusal (repo can't proceed) doesn't block refresh; other repos' files still update.

## Generated Files Table

| Plugin | Path | Written When |
|---|---|---|
| mise | `mise/conf.d/00-ow.toml` | A plugin answers `mise_environment`, or `mise.python` is set |
| odoo | `odoorc` (mode `0600`) | One readable Odoo core repo in the workspace |
| odoo | `odools.toml` | Same |
| odoo | `pyrightconfig.json` | Same |
| odoo | `requirements-dev.txt` | Same |
| vscode | `.vscode/settings.json` | A plugin contributes a language server, exclude, or theme |
| vscode | `.vscode/launch.json` | A plugin contributes a launch config |
| zed | `.zed/settings.json` | A plugin contributes a language server, exclude, or theme |
| zed | `.zed/debug.json` | A plugin contributes a launch config |

A non-Odoo workspace with no `mise.python` gets **no generated files**.

## Plugin Enablement and File Removal

Disable a plugin with:

```sh
ow config set vscode.enabled false
```

On the next refresh:
- Files only that plugin writes are removed from the lock.
- The files stay on disk (ow never deletes).
- If you re-enable the plugin, the files are regenerated.

## Configuration as an Implementation Detail

Generated files come from the workspace state (repos, branches, bases) and settings (`.ow/config.toml` and global config). The config files themselves are not documented here; they are implementation details. Use `ow config get/set/unset` to manage them, not by hand-editing (though you can if needed).

## Viewing File States and Diffs

### List All Files and Their States

```sh
ow files
```

Output shows path, state, and owner:

```
.vscode/settings.json        up to date     vscode
.vscode/launch.json          outdated       vscode
odoorc                       yours          odoo
pyrightconfig.json           absent         odoo
mise/conf.d/00-ow.toml       skipped        mise
```

### Show Diffs of Outdated or "yours" Files

```sh
ow files --diff
```

Displays a unified diff for every file that differs:

```diff
--- .vscode/launch.json (on disk)
+++ .vscode/launch.json (ow's version)
@@ -5,7 +5,7 @@
    "name": "Odoo",
-   "port": 8069,
+   "port": 9069,
    "request": "attach"
```

### Check a Specific Workspace

```sh
ow files -w myworkspace
ow files -w /path/to/workspace
```

## Refresh Diagnostics

When `ow refresh` runs:

- If a file is "yours", a warning is printed with the path and a hint to review `ow files --diff`.
- If a plugin raises an exception, a warning is printed naming the plugin.
- If a file has an invalid output path (absolute, escapes the root, or conflicts with core paths like `.ow/config.toml`), the command aborts, naming the plugin.
- If a plugin produces an `error` diagnostic, the entire refresh is blocked; no files are written.

## Lock File

`.ow/rendered.lock.toml` maps file paths to the SHA-256 hash of their content the last time ow wrote them. This is how ow knows whether a file has been edited by hand.

Example:

```toml
[".vscode/settings.json"]
sha256 = "a3b2c1d0e1f2g3h4i5j6k7l8m9n0o1p2q3r4s5t6u7v8w9x0y1z2a3b4c5d6"

[".vscode/launch.json"]
sha256 = "b4c3d2e1f0g1h2i3j4k5l6m7n8o9p0q1r2s3t4u5v6w7x8y9z0a1b2c3d4e5"
```

Don't edit this file by hand. It is rewritten by `ow refresh` and managed automatically.

## Recovery

If a file is accidentally deleted and you want ow's version back:

```sh
# See what ow would write
ow files --diff

# Delete the "yours" file if you accidentally edited it
rm .vscode/settings.json

# Refresh to regenerate
ow refresh
```

If the lock is corrupted or out of sync, delete it and run `ow refresh`:

```sh
rm .ow/rendered.lock.toml
ow refresh
```

All files will be recomputed and re-locked.
