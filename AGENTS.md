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
    └── test_workspace.py # file generators, odoorc/odools/zed output
```

## Key abstractions

- **`BranchSpec`** — represents `"master"` / `"master..feature"` / `"dev/master-phoenix..fix"`. Knows remote, branch, local_branch, detached vs attached.
- **`Config`** / **`WorkspaceConfig`** — parsed from `ow.toml`. Config holds global odoorc defaults, remotes, and workspace list.
- **`ow/git.py`** — stateless git helpers. All bare-repo operations run with `git -C <bare_repo>`, worktree operations with `git -C <worktree>`. `parallel_fetch` uses `ThreadPoolExecutor(max_workers=2)`.
- **`ow/workspace.py`** — file generators (`make_*`) + command functions (`cmd_*`). Commands call git helpers then write generated files.

## Conventions

- Bare repos live in `.bare-git-repos/<alias>.git`.
- Workspace dirs are `workspaces/<name>/`, with subdirs matching repo aliases.
- `community` is always the Odoo core repo; its addons are at `community/addons` and `community/odoo/addons`.
- All other repo aliases are treated as plain addons dirs (mounted at their alias name).
- `ow.toml` is user-local (gitignored). Removed workspaces are archived to `.ow.toml.archived-workspaces` (also gitignored).
