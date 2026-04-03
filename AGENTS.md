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
│   ├── test_config.py   # parse_branch_spec, load_config, format_workspace
│   ├── test_git.py      # git functions (subprocess mocked)
│   ├── test_workspace.py # file generators, template rendering, cache/drift, DriftResult
│   └── test_commands.py # cmd_status, cmd_rebase, cmd_remove integration tests
├── bwrap-opencode       # Opencode sandbox wrapper (for developing on ow)
├── bwrap-claude         # Claude Code sandbox wrapper (for developing on ow)
└── templates/
    ├── bwrap/           # Sandbox scripts template for workspaces
    │   ├── bwrap-opencode.j2
    │   └── bwrap-claude.j2
```

## Key abstractions

- **`BranchSpec`** — represents `"master"` / `"master..feature"` / `"dev/master-phoenix..fix"`. Knows remote, branch, local_branch, detached vs attached.
- **`Config`** / **`WorkspaceConfig`** — parsed from `ow.toml`. `Config.vars` holds global template variable defaults; `WorkspaceConfig.vars` holds per-workspace overrides. Merged at render time: `{**config.vars, **ws.vars}`.
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
  - `DriftResult` / `check_drift` / `warn_if_drifted` — detect when worktree branch state doesn't match config (e.g. config says detached but worktree is on a branch). `cmd_status`, `cmd_rebase`, `cmd_remove` call `warn_if_drifted` to display warnings but proceed anyway; `cmd_apply` does NOT (it is the reconciliation command).
  - `RebasePlan` / `_analyze_repo_for_rebase` — analyze the rebase situation for a single repo: track ref, upstream, fork_point, commits_to_reapply, local commits, unpushed commits, whether upstream was rewritten.
  - `_recover_with_cherry_pick(worktree, upstream, commits)` — reset hard to upstream and cherry-pick commits sequentially. Returns None on success, or the failing commit hash on conflict.
  - `cmd_rebase` handles three cases: (1) detached worktree → switch to track ref, (2) upstream rewritten with fork-point → recovery via reset + cherry-pick, (3) upstream rewritten without fork-point → skip with manual recovery instructions, (4) normal rebase → two-step rebase onto upstream then track ref. Displays summary and asks for confirmation before proceeding.

## Template system

Workspace files are generated from `templates/` at the project root. Each subdirectory is a template bundle. Workspaces declare templates via the `templates` field (required, non-empty array). Templates are applied in order — later templates can override files from earlier ones.

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
- `ow.toml` is user-local (gitignored). Removed workspaces are archived to `.ow.toml.archived-workspaces` (also gitignored).
- `templates/` contains git-tracked template bundles; subdirectories like `common/`, `vscode/`, `zed/` are whitelisted in `.gitignore`.
