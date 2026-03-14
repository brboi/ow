# ow — Module Structure

```
ow/
├── __init__.py          # empty
├── __main__.py          # CLI entry point (argparse + argcomplete)
├── config.py            # Config dataclasses, TOML loading/writing
├── git.py               # All git operations via subprocess
├── workspace.py         # File generators + command implementations
└── tests/
    ├── __init__.py
    ├── test_config.py   # parse_branch_spec, load_config, format_workspace
    ├── test_git.py      # git functions (subprocess mocked)
    └── test_workspace.py # file generators, template rendering, cache/drift
```

## Key abstractions

- **`BranchSpec`** — represents `"master"` / `"master..feature"` / `"dev/master-phoenix..fix"`. Knows remote, branch, local_branch, detached vs attached.
- **`Config`** / **`WorkspaceConfig`** — parsed from `ow.toml`. `Config.vars` holds global template variable defaults; `WorkspaceConfig.vars` holds per-workspace overrides. Merged at render time: `{**config.vars, **ws.vars}`.
- **`ow/git.py`** — stateless git helpers. All bare-repo operations run with `git -C <bare_repo>`, worktree operations with `git -C <worktree>`. `parallel_fetch` uses `ThreadPoolExecutor(max_workers=2)`. `get_remote_ref_for_branch` scans ow.toml-configured remotes for a pushed local branch (non-base remotes checked first). `get_remote_url` falls back to `git remote get-url` for remotes not in ow.toml.
- **`ow/workspace.py`** — file generators + command functions (`cmd_*`). Commands call git helpers then render Jinja2 templates. Also owns cache/drift helpers (see below).

## Template system

Workspace files are generated from `workspaces/.template/` (user-editable copy of `workspaces/.template.init/`). Templates are Jinja2 (`.j2` extension); static files are copied as-is.

Template context keys:
- `ws_name` — workspace name
- `vars` — merged dict of `config.vars` and `ws.vars` (use `{{ vars.key | default(fallback) }}`)
- `addons_paths` — ordered list of absolute addon paths
- `odools_path_items` — relative paths for odools.toml
- `repos` — list of repo aliases
- `main_repo_alias` — alias of the Odoo core repo (has `odoo-bin`), or `None`

## Cache / drift detection

`workspace.py` maintains `.ow.cache` (JSON, gitignored) to track hashes of shipped files:

- `workspaces/.template.init/` — checked in `cmd_apply` before copying to `.template/`
- `ow.toml.example` — checked in `__main__.py` at every invocation

If the hash has changed since last record:
- Non-TTY: warning printed to stderr, continues
- TTY: interactive prompt — `[c]` continue (warns again next time), `[s]` skip until next update (saves hash), `[a]` abort

Cache helpers: `_compute_hash`, `_load_cache`, `_save_cache`, `_check_source_drift`, `_record_hash`.

## Conventions

- Bare repos live in `.bare-git-repos/<alias>.git`.
- Workspace dirs are `workspaces/<name>/`, with subdirs matching repo aliases.
- `community` is always the Odoo core repo; its addons are at `community/addons` and `community/odoo/addons`.
- All other repo aliases are treated as plain addons dirs (mounted at their alias name).
- `ow.toml` is user-local (gitignored). Removed workspaces are archived to `.ow.toml.archived-workspaces` (also gitignored). `.ow.cache` is also gitignored.
