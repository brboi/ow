# ow — Module Structure

```
ow/
├── __init__.py          # empty
├── __main__.py          # CLI entry point (argparse + argcomplete)
├── config.py            # Config dataclasses, TOML loading/writing
├── git.py               # All git operations via subprocess
├── workspace.py         # File generators + command implementations + drift detection
└── tests/
    ├── __init__.py
    ├── test_config.py   # parse_branch_spec, load_config, format_workspace
    ├── test_git.py      # git functions (subprocess mocked)
    ├── test_workspace.py # file generators, template rendering, cache/drift, DriftResult
    └── test_commands.py # cmd_status, cmd_rebase, cmd_remove integration tests
```

## Key abstractions

- **`BranchSpec`** — represents `"master"` / `"master..feature"` / `"dev/master-phoenix..fix"`. Knows remote, branch, local_branch, detached vs attached.
- **`Config`** / **`WorkspaceConfig`** — parsed from `ow.toml`. `Config.vars` holds global template variable defaults; `WorkspaceConfig.vars` holds per-workspace overrides. Merged at render time: `{**config.vars, **ws.vars}`.
- **`ow/git.py`** — stateless git helpers. All bare-repo operations run with `git -C <bare_repo>`, worktree operations with `git -C <worktree>`. `parallel_fetch` uses `ThreadPoolExecutor(max_workers=2)`. `get_remote_ref_for_branch` scans ow.toml-configured remotes for a pushed local branch (non-base remotes checked first). `get_remote_url` falls back to `git remote get-url` for remotes not in ow.toml.
  - `run_cmd(args, quiet=False)` wraps `subprocess.run`, printing `$ cmd` to stderr unless `quiet=True`. Action functions (clone, fetch, worktree add/remove, switch, rebase) use `run_cmd`; probes (rev-parse, symbolic-ref, rev-list) use `subprocess.run` directly.
  - `ordered_remotes(alias_remotes)` returns remote names with `origin` first, then alphabetical. Used in `resolve_spec`, `resolve_spec_local`, `get_remote_ref_for_branch`, `ensure_bare_repo`.
  - `get_worktree_branch(worktree_path)` returns the current branch name or `None` if detached (uses `rev-parse --abbrev-ref HEAD`).
- **`ow/workspace.py`** — file generators + command functions (`cmd_*`). Commands call git helpers then render Jinja2 templates. Also owns cache/drift helpers and worktree drift detection (see below).
  - `DriftResult` / `check_drift` / `assert_no_drift` — detect when worktree branch state doesn't match config (e.g. config says detached but worktree is on a branch). `cmd_status`, `cmd_rebase`, `cmd_remove` call `assert_no_drift` before proceeding; `cmd_apply` does NOT (it is the reconciliation command).
  - `cmd_rebase` does a two-step rebase for attached repos: first onto the pushed work branch (upstream), then onto the base/track branch. If the work branch hasn't been pushed, only the track branch rebase runs. Conflicts are reported with continue/abort instructions and the command moves on to the next repo.

## Template system

Workspace files are generated from `workspaces/.template/` (git-tracked). Templates are Jinja2 (`.j2` extension); static files are copied as-is. Local overrides can be placed in `workspaces/.template.overrides/` (gitignored), which will overwrite files from `.template/` with the same path.

Template context keys:
- `ws_name` — workspace name
- `vars` — merged dict of `config.vars` and `ws.vars` (use `{{ vars.key | default(fallback) }}`)
- `addons_paths` — ordered list of absolute addon paths
- `odools_path_items` — relative paths for odools.toml
- `repos` — list of repo aliases
- `main_repo_alias` — alias of the Odoo core repo (has `odoo-bin`), or `None`

## Conventions

- Bare repos live in `.bare-git-repos/<alias>.git`.
- Workspace dirs are `workspaces/<name>/`, with subdirs matching repo aliases.
- `community` is always the Odoo core repo; its addons are at `community/addons` and `community/odoo/addons`. Note that `community` is the default proposed alias for the main `odoo/odoo` repository, but it can be changed. There is `is_odoo_main_repo` that is made to discover which repo is the main.
- `ow.toml` is user-local (gitignored). Removed workspaces are archived to `.ow.toml.archived-workspaces` (also gitignored).
- `workspaces/.template/` contains git-tracked templates; `workspaces/.template.overrides/` contains local overrides (gitignored).
