# ow — Module Structure

```
ow/
├── __init__.py          # empty
├── __main__.py          # CLI entry point (argparse + argcomplete)
├── config.py            # Config dataclasses, TOML loading/writing
├── git.py               # All git operations via subprocess
├── workspace.py         # File generators + command implementations + drift detection
├── tests/
│   ├── __init__.py
│   ├── test_config.py   # parse_branch_spec, load_config, load_workspace_config
│   ├── test_git.py      # git functions (subprocess mocked)
│   ├── test_workspace.py # file generators, template rendering, cache/drift, DriftResult
│   └── test_commands.py # cmd_status, cmd_rebase integration tests
├── bwrap-opencode       # Opencode sandbox wrapper (for developing on ow)
├── bwrap-claude         # Claude Code sandbox wrapper (for developing on ow)
├── scripts/
│   └── migrate-to-1.0.0.py  # One-time migration script for v1.0.0
└── templates/
    ├── bwrap/           # Sandbox scripts template for workspaces
    │   ├── bwrap-opencode.j2
    │   └── bwrap-claude.j2
```

`ow/__main__.py` is the CLI entry point. `init` is handled before the config-loading block (it doesn't require an existing `ow.toml`) and returns early; all other commands go through `find_root()` + config loading.

## Key abstractions

- **`BranchSpec`** — represents `"master"` / `"master..feature"` / `"dev/master-phoenix..fix"`. Knows remote, branch, local_branch, detached vs attached.
- **`Config`** — parsed from `ow.toml`. `Config.vars` holds global template variable defaults. Merged at render time: `{**config.vars, **ws.vars}`.
- **`WorkspaceConfig`** — parsed from `.ow/config` inside each workspace. `WorkspaceConfig.vars` holds per-workspace overrides.
- **`ow/git.py`** — stateless git helpers. All bare-repo operations run with `git -C <bare_repo>`, worktree operations with `git -C <worktree>`. `get_remote_ref_for_branch` scans ow.toml-configured remotes for a pushed local branch (non-base remotes checked first). `get_remote_url` falls back to `git remote get-url` for remotes not in ow.toml.
  - `run_cmd(args, quiet=False)` wraps `subprocess.run`, printing `$ cmd` to stderr unless `quiet=True`. Action functions (clone, fetch, worktree add/remove, switch, rebase) use `run_cmd`; probes (rev-parse, symbolic-ref, rev-list) use `subprocess.run` directly.
  - `git(repo, *args)` — central wrapper that adds `-C` automatically. Use this for all git commands.
  - `git_fetch(repo, remote, refspec, *, force=False)` — fetch with optional force (+refspec).
  - `git_switch(worktree, ref, *, detach=False, create=False)` — unified switch with detach/create options.
  - `git_rebase(worktree, onto)` — rebase onto ref, returns CompletedProcess for caller to check.
  - `git_merge_base_fork_point(worktree, upstream, branch)` — find fork-point between branch and upstream; returns None if upstream was rewritten.
  - `git_rev_list(repo, commit_range, *, reverse=False)` — return list of commit hashes in range.
  - `git_log_oneline(repo, commit)` — return one-line log for a commit: 'hash message'.
  - `git_cherry_pick(worktree, commit)` — cherry-pick a commit, returns CompletedProcess.
  - `git_reset_hard(worktree, ref)` — reset worktree to ref with --hard.
  - `ordered_remotes(alias_remotes)` returns remote names with `origin` first, then alphabetical. Used in `resolve_spec`, `resolve_spec_local`, `get_remote_ref_for_branch`, `ensure_bare_repo`.
  - `get_worktree_branch(worktree_path)` returns the current branch name or `None` if detached (uses `rev-parse --abbrev-ref HEAD`).
- **`ow/workspace.py`** — file generators + command functions (`cmd_*`). Commands call git helpers then render Jinja2 templates. Also owns cache/drift helpers and worktree drift detection (see below).
  - `_copy_packaged_templates(dest)` — copies all bundled template directories from the installed package to `dest`.
  - `_copy_ow_services(dest)` — copies the bundled `services/` files from the installed package to `dest`.
  - `resolve_workspace(path, config)` — resolves a workspace from explicit path, `OW_WORKSPACE` env var, or cwd walk-up for `.ow/config`.
  - `DriftResult` / `check_drift` / `warn_if_drifted` — detect when worktree branch state doesn't match config. `cmd_status`, `cmd_rebase`, `cmd_update` call `warn_if_drifted` to display warnings but proceed anyway.
  - `RebasePlan` / `_analyze_repo_for_rebase` — analyze the rebase situation for a single repo: track ref, upstream, fork_point, commits_to_reapply, local commits, unpushed commits, whether upstream was rewritten.
  - `_recover_with_cherry_pick(worktree, upstream, commits)` — reset hard to upstream and cherry-pick commits sequentially. Returns None on success, or the failing commit hash on conflict.
  - `cmd_rebase` handles four cases: (1) detached worktree → switch to track ref, (2) upstream rewritten with fork-point → recovery via reset + cherry-pick, (3) upstream rewritten without fork-point → skip with manual recovery instructions, (4) normal rebase → two-step rebase onto upstream then track ref. Displays summary and asks for confirmation before proceeding.

## Commands

| Command | Signature | Purpose |
|---------|-----------|---------|
| `ow init` | `cmd_init(path, *, force, with_backup)` | Initialize new project directory (no `ow.toml` required) |
| `ow create` | `cmd_create(config, ...)` | Interactive form → create workspace + `.ow/config` |
| `ow update` | `cmd_update(config)` | Re-render templates + materialize worktrees |
| `ow status` | `cmd_status(config)` | Show workspace branch status |
| `ow rebase` | `cmd_rebase(config)` | Fetch + rebase workspace branches |
| `ow prune` | `cmd_prune(config)` | Clean up stale worktree references from bare repos |

All commands resolve the current workspace via `OW_WORKSPACE` env var or cwd walk-up for `.ow/config`.

## Template system

Workspace files are generated from `templates/` at the project root. Each subdirectory is a template bundle. Workspaces declare templates via the `templates` field (required array). Templates are applied in order — later templates can override files from earlier ones.

Bundled templates (git-tracked):
- `templates/common/` — core files: mise.toml, odoorc, odools.toml, pyrightconfig.json, requirements-dev.txt
- `templates/vscode/.vscode/` — VSCode settings and debug config
- `templates/zed/.zed/` — Zed settings and debug config
- `templates/bwrap/` — sandbox scripts for AI coding assistants (bwrap-opencode, bwrap-claude)

Templates are Jinja2 (`.j2` extension); static files are copied as-is.

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
- `ow.toml` is user-local (gitignored). Contains only `[vars]` and `[remotes]`. No workspace declarations.
- Each workspace has its own `.ow/config` file (gitignored) that stores its config: name, templates, repos, vars.
- `templates/` contains git-tracked template bundles; subdirectories like `common/`, `vscode/`, `zed/` are whitelisted in `.gitignore`.
