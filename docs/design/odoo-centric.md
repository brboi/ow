# ow: an opinionated Odoo workspace manager

Decision record for issue #45, revised 2026-09-20 under the user's mandate to choose the most KISS, opinionated and pragmatic solution.

**Status: implementation target, not shipped behavior.** The reviewed branch still has Jinja bundles, `templates`, `[vars]`, `ow apply` and `ow templates`. This document supersedes its earlier target design. The executable work breakdown is [the implementation plan](../superpowers/plans/2026-09-20-odoo-workspace-cutover.md). Both documents describe one coordinated breaking release, **ow 3.0**, with **configuration schema 2**; those version numbers are not interchangeable.

## 1. Product boundary and rejected alternatives

ow manages a directory of Git worktrees for an Odoo developer: multiple repositories, multiple remotes, feature branches, detached series, missing-worktree repair, relocation and archival. A documentation or infrastructure workspace is equally valid. It is not a template engine, provisioning framework, sandbox implementation or dependency installer.

Three sources of truth remain separate:

- Git owns observed HEAD, branch, upstream, dirty state and operations in progress.
- `.ow/config.toml` owns membership, the base a feature follows, detached pins and explicit local options.
- The generator owns proposed file contents; `.ow/rendered.lock.toml` records the bytes ow last wrote or adopted.

**Base is not upstream.** Rebasing a feature onto `origin/19.0` is distinct from pulling its published history from `dev/feature`. Preserve the current `BranchSpec`, planners and remotes model. Do not introduce an organization abstraction over remote names.

Chosen: a fixed set of generators with typed options, invoked at the right lifecycle boundaries. Rejected: keeping a nicer public bundle framework (the user still has to configure the machinery), and putting membership/base intent in per-worktree Git config (removed worktrees lose the information needed to repair them). Do not enable `extensions.worktreeConfig`, invent a registry/plugin system, or turn the workspace root into a Git repository.

### Release invariants

| ID | Requirement |
|---|---|
| R01 | Preserve multirepo/multi-remote Git semantics and manifest intent, including missing members and base versus upstream. |
| R02 | Derive Odoo identity and capabilities from checkout files without executing checkout Python. |
| R03 | Use one effective, validated Python minor and capability-correct debug arguments. |
| R04 | Replace vars with typed global defaults and sparse local overrides; do not copy defaults into new workspaces. |
| R05 | Replace bundles/Jinja with fixed serialized outputs; a generic workspace gets no Odoo runtime and no implicit Python. |
| R06 | Retain byte ownership, adoption, divergent-file protection and retired-file visibility; reject unsafe output paths. |
| R07 | Implement global-only `owignore` using gitignore semantics, not a second configuration hierarchy. |
| R08 | Migrate explicitly, back up original config bytes, preserve unknown legacy data and reject lossy conversion. |
| R09 | Make init re-runnable and create-only for missing worktrees; never switch existing ones during repair. |
| R10 | Refresh once after successful Git mutation batches, not dry-runs/no-ops/conflicts; preserve Git outcomes. |
| R11 | Preserve intent and ownership through copy/move/archive/unarchive/removal backup. |
| R12 | Remove template/vars UI, provide typed inherited/local options, and preserve editor/theme/remotes behavior. |
| R13 | Remove the service template engine without adding a YAML dependency or starting services. |
| R14 | Copy optional local seeds once, before addon scanning, without interpolation or overwrites. |
| R15 | Delegate sandbox execution to scripts present in the checkout; do not vendor sandbox implementations. |
| R16 | Remove old commands, flags, imports, assets and dependency together; no compatibility command aliases. |
| R17 | Make inspection genuinely read-only, including config bootstrap, resolver and index behavior. |
| R18 | Deliver a documented major-version cutover with offline regression evidence and actual CLI/TUI smoke verification. |

## 2. Runtime and supported Odoo checkouts

- ow's own Python floor stays **3.11**. This is independent of workspace Python.
- Generated fragments stay at **`mise/conf.d/00-ow.toml`**. Do not cause a second relocation.
- Their supported mise floor is **2026.8.13**, which introduced that visible fragment directory. Document it and check `mise --version` before explicit init/render mutates anything. Parse the first `YYYY.M.PATCH` token as an integer tuple; absent, unparseable or older mise is an actionable error. Inspection does not invoke mise. Automatic refresh checks this prerequisite after Git, never reverses Git if it is missing.
- Odoo support is the explicit set **18, 19, 20**, their saas series, and current master whose inspected identity is `(20, 1)`. No wall-clock calculation or branch-name inference. A future major requires a support-table change.

Keep the three core markers: `odoo-bin`, `addons/`, `odoo/addons/`. Inspect only declared repositories. Missing declared worktrees or an in-progress Git operation in any existing declared repository block generation of the whole workspace. They must not make a missing core look like a valid non-Odoo workspace.

Probe outcomes are `none`, `supported`, `unsupported`, `invalid`, `ambiguous`, `incomplete`. More than one core is ambiguous: do not choose the first alias. No core, with all declared worktrees present and stable, is the ordinary generic case.

Read `odoo/release.py` with `ast.parse`. Inspect the first two literal elements of the module-level `version_info` assignment; later names such as `FINAL` are irrelevant. Accept `(18, 0)`, `(20, 1)`, and `('saas~19', 4)` shapes; validate the major for every shape. Reject booleans, malformed values and multiple ambiguous assignments. Never import the file, use `exec`, or run `odoo-bin --version`.

Read literal two-integer `MIN_PY_VERSION`/`MAX_PY_VERSION` tuples from `release.py`; for a missing bound, inspect `odoo/__init__.py` for the 18.0 layout. A present but invalid bound is an error, not permission to fall back. Require ordered bounds with Python major 3. Probe literal flag arguments in actual `add_option`/`add_argument` calls in `odoo/tools/config.py`; a string in a comment or unrelated expression is not a declaration.

| Capability | Default debug arguments |
|---|---|
| `--with-demo` declared | `["--dev=all"]` |
| Only `--without-demo` declared | `["--dev=all", "--without-demo=all"]` |
| Neither recognized, file unreadable or AST invalid | Blocking diagnostic |

Defaults intentionally avoid enabling demo data. `debug_test_args` defaults to `["--test-tags=<workspace-name>"]`; the test launch combines effective debug arguments and effective test arguments, as the existing launchers do. An explicit list replaces its corresponding whole list, including an explicit empty list.

Unsupported, invalid, ambiguous and incomplete outcomes block **all output writes**, including services and trust. Inspection still names the reason and existing locked files retained on disk. There is no guessed generic fallback for a recognized but unusable core. This fail-before-writing policy replaces the earlier generic-only fallback proposal.

## 3. Typed configuration and inheritance

Both global and workspace configs use `version = 2`. Omitted version means legacy schema 1. Reject versions above 2. Preserve the XDG locations, `repos`, remotes (`url`, `pushurl`, `fetch`), `editor` and `theme`.

New workspace example:

```toml
version = 2

[repos]
community = "19.0..feature"
enterprise = "19.0..feature"

[odoo]
http_port = 8068
```

Global example; tables and individual options are optional:

```toml
version = 2
editor = "code"
theme = "textual-dark"
owignore = [".zed/**"]

[remotes.community]
origin.url = "git@github.com:odoo/odoo.git"
dev.url = "git@github.com:odoo-dev/odoo.git"

[odoo]
db_host = "localhost"
db_user = "odoo"
db_password = "odoo"
```

Effective precedence is **built-in default < explicit global key < explicit workspace key**. Merge by key, not by replacing tables. No generic dictionaries in the public options API. No user-defined keys, substitution language or arbitrary generator options.

Both configs can hold `odoo.db_password` and `odoo.admin_passwd`, so every writer — init, render, migration, switch, relocation and the TUI — creates or atomically replaces `$XDG_CONFIG_HOME/ow/config.toml` and `.ow/config.toml` with mode 0600. Atomic replacement installs a new inode, so the mode is set on the temp file before the rename rather than inherited. One rule for all writers: a migrated file that the next ordinary save turns world-readable would defeat the whole point.

| Table/key | Type and validation | Built-in value |
|---|---|---|
| `odoo.http_port` | integer 1..65535, not bool | 8069 |
| `odoo.db_port` | integer 1..65535, not bool | 5432 |
| `odoo.smtp_port` | integer 1..65535, not bool | 25 |
| `odoo.db_host` | nonempty string | `localhost` |
| `odoo.db_user` | nonempty string | `odoo` |
| `odoo.db_password` | string, empty permitted | `odoo` |
| `odoo.admin_passwd` | string, empty permitted | `Password` |
| `odoo.smtp_server` | nonempty string | `localhost` |
| `odoo.debug_args` | list of strings | capability-dependent above |
| `odoo.debug_test_args` | list of strings | workspace-name default above |
| `mise.python` | string matching `3.MINOR`, integer minor | Odoo: `3.12`; generic: absent |
| global `owignore` | list of strings | `[]` |

Reject NUL/CR/LF in scalar options and argument elements. Empty argument arrays are valid. Unknown keys in schema-2 owned tables are configuration errors. A workspace `owignore` is rejected: there is only the global list. Workspace options remain sparse when written; equal-to-global explicit values remain explicit. In the TUI, disabling an override means inheritance, while an enabled empty password means the empty string.

Clamp an Odoo Python preference to the checkout's inclusive minor bounds with integer tuples; warn with requested/effective version and series. Use the same string for mise and Pyright. A generic workspace has no Python tool, venv or pip hook unless global or local `mise.python` is explicitly set. When opted in, use that minor unchanged and only the generic ensurepip hook. Global defaults are live, so a later render uses changed global keys unless locally overridden.

Rendering never installs Python/dependencies or rebuilds `.venv`. If `.venv/pyvenv.cfg` exposes a different minor, warn; do not launch that interpreter or delete the environment. Workspace names used in regexes are escaped; reject comma/newline-bearing addon paths that Odoo's comma-separated `addons_path` cannot represent rather than producing malformed configuration.

## 4. Fixed generation, no public framework

Use one typed context and a fixed ordered table of generator functions. Each proposes UTF-8 bytes or no output. JSON uses `json.dumps`, TOML uses the already installed `tomli_w`; INI uses `ConfigParser(interpolation=None)` with explicit formatting. No Jinja, user overrides tree, registry, plugin API, inheritance chain or multiple render passes.

| Output | When | Notes |
|---|---|---|
| `mise/conf.d/00-ow.toml` | Every complete workspace | `OW_WORKSPACE`, workspace PATH; optional Python; Odoo env/tasks only for a supported core |
| `.vscode/settings.json` | Every complete workspace | Odoo settings only with core |
| `.zed/settings.json` | Every complete workspace | Include the actual `mise/conf.d/*.toml` path, not just retired `mise.toml`; Odoo settings only with core |
| `odoorc` | Supported core | Addons/data/database paths derived from workspace; typed connection options |
| `odools.toml` | Supported core | Detected alias, relative addon paths |
| `pyrightconfig.json` | Supported core | Effective Python minor and detected alias |
| `requirements-dev.txt` | Supported core | Preserve `inotify\n` |
| `.vscode/launch.json` | Supported core | Preserve existing run/test actions, effective arguments |
| `.zed/debug.json` | Supported core | Preserve existing run/test actions, effective arguments |

Both editors are generated by default. Opting out is a global output-path ignore, not a resurrected bundle choice. JSONC comments are not a compatibility API: generated editor files become valid JSON, which both editors accept. Generated files are 0644 except new/replaced `odoorc` at 0600. Ownership is byte-based; do not chmod an adopted or divergent existing file merely to normalize mode.

The mise fragment keeps `{{config_root}}` in environment values where mise performs its own expansion. Serialize the surrounding TOML. Shell hooks refer to the resulting `"$OW_WORKSPACE"` environment variable; never inject a Tera-expanded root into shell source and assume prior quoting still protects a quote in that root. Quote literal relative operands separately with `shlex.quote`. Odoo emits `ODOO_RC`, `COMPOSE_FILE`, the core directory on PATH, `osh`, and a postinstall hook for core requirements plus `requirements-dev.txt`. If `requirements-dev.txt` is ignored and absent, omit that `-r` operand; if it already exists, it remains usable. Ignoring `odoorc` is an intentional user takeover; document that Odoo launch commands require their own usable file. No generated root shell wrappers remain.

### Addon ordering and local seeds

Retain the existing tested discovery rules (`__manifest__.py`/`__openerp__.py`, not `__init__.py`; hidden-directory pruning; cycle protection). Preserve this order: `.local` addons, other loose workspace addons, non-core repos in manifest order, core `addons/`, core `odoo/addons/`. Deduplicate paths without reordering.

`$XDG_CONFIG_HOME/ow/local/` is an optional copy-once source, not a shipped bundle. Init copies missing regular files to `<ws>/.local/` before building the context. Preserve executable bits but not special permission bits. Reject source/destination symlinks and non-regular path conflicts; do not follow them. Existing destination regular files, including changed files, are untouched. An existing destination directory is allowed only as a directory ancestor, never as a file replacement. Preflight the seed tree before copying. Render/files/status never seed; rerunning init may add new missing seed files. There is no interpolation.

### Services

Move compose generation to `utils/services.py`, with a fixed Python dictionary serialized as JSON into the existing `services/compose.yml` (JSON is accepted by Compose as YAML). Preserve postgres `pgvector/pgvector:pg17` on 5432, mailpit on 8025/1025, pgweb on 8081, environments and volume locations. Prefer Compose's long bind-mount syntax so a colon in an absolute source path is not misparsed. No PyYAML dependency.

The machine-wide compose file remains ow-managed, outside workspace ignore/lock rules, and is refreshed only by a successful supported-Odoo write render. Generic workspaces do not create services. Equal bytes preserve mtime; writes are atomic. Existing service customization is not silently protected by the workspace lock: document that ow regenerates this file and user-owned compose files must be separate. Never start Docker/Podman or upgrade a running database.

### Sandboxes

Do not vendor bwrap scripts. Probe these actual checkout-relative paths separately:

- `setup/sandboxing/bwrap/bwrap-claude.sh` → task `bwrap-claude`;
- `setup/sandboxing/bwrap/bwrap-opencode.sh` → task `bwrap-opencode`;
- `setup/sandboxing/bwrap/bwrap-pi.sh` → task `bwrap-pi`;
- `setup/sandboxing/firejail/claude.profile` → task `firejail-claude`.

Tasks live in the same mise fragment. Use a short shebang `run` task that `exec`s the upstream command and forwards `"$@"` exactly once; there is no copied wrapper file. Bwrap tasks set `ODOO_BASE` to the workspace root. Pass each distinct existing declared worktree's absolute `git rev-parse --git-common-dir` as `--add-dir`, so the worktrees' external Git storage is accessible without mounting every unrelated ow repository. Firejail uses the checkout profile and explicit workspace/common-dir whitelists. Quote every path independently of TOML serialization. Do not expose a task for a missing/non-executable bwrap script or missing profile.

This is upstream delegation, not a guarantee that every upstream sandbox profile supports every host. Do not install sandbox software or copy `code.local` into the home directory. Editor sandbox setup stays upstream-documented. Older 18/19 checkouts and non-Odoo workspaces normally get no sandbox tasks; presence detection permits backports. Retain and report old generated wrappers rather than deleting them.

## 5. Ownership, ignore and inspection

Keep `.ow/rendered.lock.toml` as a sorted output-path → SHA-256 mapping. No format migration is necessary.

| Disk/proposal | Action/state |
|---|---|
| Proposed, absent | Write, record hash / `absent` before write |
| Equal to proposal | Adopt without rewriting / `up to date` |
| Equals locked hash but not proposal | Update, record hash / `outdated` before write |
| Differs from lock and proposal | Preserve / `yours` |
| No proposal, previously locked file still exists | Preserve lock and file / `not rendered` |
| Ignored | Neither write nor adopt / `ignored` |

An ignored path absent from disk is still shown if it has a generator. An obsolete locked path disappears from listings when its file is absent; the lock is not a tombstone mechanism. Removing an ignore resumes normal ownership checks, never unconditional overwrite. Identical future content can adopt a formerly divergent file again.

Use `pathspec.GitIgnoreSpec` with workspace-relative POSIX paths, ordered patterns and negation. Match known proposed/locked paths individually, not via a pruning walker that would prevent re-inclusion. Compile once per operation.

Before any write, validate every nonignored generated or locked path: relative normalized POSIX form, no `..`, no reserved config/lock destination, no symlink in destination ancestry, no directory at a proposed file path, no non-regular file. Validate `.ow` and lock storage too. Never follow a lock entry outside the workspace. Ignored paths are not opened. Invalid lock TOML/hash values are diagnostics, not permission to reset ownership. Use atomic same-directory replace for outputs and lock; recheck the destination before replacing so a file changed since inspection becomes `yours`. Successfully written files remain written if a later I/O operation fails; record only completed writes and report partial results. Do not pretend there is a filesystem transaction or roll back Git.

A nonignored generated output must not land inside a declared worktree; report a path conflict instead of making that repository dirty. A declared `.local` worktree likewise conflicts with the seed destination. The user can choose nonconflicting aliases or globally ignore a colliding output; ow does not silently redirect generated paths.

`ow files` lists states; `ow files --diff` produces diffs from `/dev/null` for additions and path-naming notices for non-UTF-8 differences. `yours` is a file difference; `ignored` and retained `not rendered` are not proposed changes. Do not read secrets into diagnostic summaries. Existing generated root `mise.toml` identified by the legacy `OW_WORKSPACE` marker is a shared diagnostic in files/render/status: it may shadow the fragment. Preserve it; instruct the user to review and remove/move it. It makes the diff gate fail until resolved, not merely the writing command warn.

## 6. Configuration migration and real read-only operation

Only explicit **init** and **render** migrate schema 1. Loaders translate known old values in memory but retain original text and migration diagnostics in a `LegacyConfig` record. The runtime model has no `vars` or `templates` aliases. Never execute old Jinja to interpret a legacy manifest.

Map `python` to `mise.python`; map `http_port`, `db_host`, `db_port`, `db_user`, `db_password`, `admin_passwd`, `smtp_server`, `smtp_port`, `debug_args`, `debug_test_args` to their `odoo` counterparts. New typed keys, if present in a transitional version-1 document, win. Preserve every known workspace value as an explicit override even if it equals a global default; historical files cannot prove whether it was deliberately set.

Both global and workspace migrations preflight together. Unknown vars, unrepresentable selectors/types and unknown owned configuration keys are blocking migration diagnostics naming paths/keys, never a reason to silently discard data. Customized template sources do **not** block: ow cannot keep rendering them, and it must not overwrite what they produced, so both outcomes are handled by retirement instead of a dead end. Known old bundle names (`common`, `odoo`, `vscode`, `zed`, `bwrap`) and declared custom bundles alike are retired with an explicit report naming each source and the outputs it produced; sources and outputs stay on disk and ow stops rendering them. A custom bundle is inventoried by relative source/output names without rendering. Stock copies proven byte-identical by a frozen last-v2 source-hash inventory are not customizations; no runtime legacy templates need be shipped. The old source lock can aid inventory but cannot prove that its original source was stock. Unknown or unprovable copies are conservatively treated as customized. The source-hash inventory contains digests and paths only, never a second rendering implementation.

Ownership must follow that report, because a retained lock entry is what authorizes overwriting. Migration retires the lock entry of every output produced by a customized or overridden source **that a fixed 3.0 generator also produces**, so that file becomes `yours` and stock content never replaces it — `.vscode/settings.json`, `.zed/settings.json`, `odoorc` and `pyrightconfig.json` are exactly the paths users override. An output is attributed to every bundle that declared it, and a single overriding or custom contributor is enough to retire it: over-retiring only makes ow own less. A locked path no 3.0 generator produces keeps its entry and stays visible as `not rendered`; retiring it would hide an old bwrap wrapper or custom output from `ow files` while protecting nothing. Shipped bundle names remain attributable through the frozen digest inventory even though 3.0 ships no template tree. Only a declared **custom** bundle — a name that inventory does not know — whose source directory under `$XDG_CONFIG_HOME/ow/templates/` is gone cannot be attributed at all: there, retire every entry of that workspace whose path a 3.0 generator produces and report it, leaving those files `yours` until the user reviews `ow files --diff` and deletes the ones ow should own again. Retirement rewrites the lock only; it never deletes or edits a generated file, and a later byte-identical render re-adopts the path normally.

Do not automatically convert arbitrary templates into seeds. The retirement report describes copying already-reviewed static files under the new global `local/` source, or simply keeping the workspace outputs as they now are. Old files are not deleted.

Migration sequence:

1. Parse both configs, translate, validate, inventory customizations and report the typed overrides retained.
2. If either plan has blockers, write nothing: no config backup, no worktree mutation, no output or lock change.
3. Save each original file's exact bytes — both configs, plus `.ow/rendered.lock.toml` when retirement will rewrite it — under `$XDG_STATE_HOME/ow/backups/migrations/<sha256-of-absolute-source-path>/<sha256-of-original-bytes>.toml`; directories 0700, files 0600. Write a same-directory temp file, flush and fsync it, then `os.replace` onto the hashed name: an interrupted backup must never leave a truncated file whose bytes contradict its own name and block every later retry. An existing destination with equal bytes is reuse; a mismatching one is an error naming the path. Backup failure aborts conversion.
4. Atomically rewrite `.ow/rendered.lock.toml` without the retired entries, when there are any. Retirement precedes config replacement because it only ever makes ow own less: interrupted here, the workspace is still schema 1, the next attempt re-plans the same retirement, and no customized file was at risk in between. A failure aborts before any config is replaced.
5. Atomically replace each config with complete schema-2 content, preserving remotes/editor/theme and supported non-render fields. No version bump before content is ready. This is atomic per file, not across global/workspace; interruption after global migration leaves the workspace v1 and safely retryable.
6. Re-read the committed configs, then perform requested materialization/rendering. A later render failure does not invalidate a completed config migration or its backup.

A schema-1 switch updates only `[repos]` in the retained TOML document, preserving unknown data/comments and schema version. Global remotes/editor/theme edits similarly round-trip a legacy document. TUI typed-option editing is disabled until migration, with `ow render` guidance; existing repo/remotes/editor/theme operations remain available. Automatic post-Git/relocation refresh never migrates a legacy global or workspace config: report the skipped refresh and the exact `ow render -w PATH` action, without turning successful Git/relocation into failure solely for this pending migration.

Read-only means read-only: `load_global_config()` returns in-memory defaults if absent; it no longer bootstraps on reads. Init/render explicitly write initial schema-2 config. `resolve_workspace` does not remember locations, and `known_workspaces` filters dead entries without rewriting the index. Successful init/render/move/unarchive remember locations; archive/rm forget; prune removes dead entries. `status`, `files`, `ls`, completion and dry-runs do not create or modify configs, outputs, services, index or trust state. Fetch still updates Git refs, but no config, worktree, render or index state. Keep read-v1/write-v1 support through the 3.x release line; it is data compatibility, not old command aliases.

Git inspection must also disable optional index-refresh writes on status probes (`--no-optional-locks` or probe-local `GIT_OPTIONAL_LOCKS=0`). Do not disable the real locks of mutation commands. Read-only acceptance includes the worktrees' Git index files, not only ow's location index.

## 7. Lifecycle and exit contracts

| Operation | Behavior |
|---|---|
| New `ow init [NAME] [-r alias:spec] [-c CONFIG]` | Resolve target as today; create intent, create worktrees, seed, render. No `-t`, no vars questionnaire. In non-TTY mode, no repos is a valid empty workspace. |
| Existing `ow init [NAME]` | Repair declared missing worktrees, seed missing files, render; no prompt to reselect existing repos. |
| Existing init with `-r` | Add undeclared repos; identical specs allowed; conflicting existing specs refused before writes with `ow switch` guidance. Existing `-c` is refused before writes. |
| `ow render [WORKSPACE]` | Explicit migration/creation of config if needed, file generation, supported-Odoo services refresh, trust only safe generated mise content. Never materialize repos, fetch, switch or seed. |
| `ow files [WORKSPACE]` | Read-only states; exit 0 even for ordinary differences, 1 for blocking input/path/migration diagnostics. |
| `ow files --diff` | File gate: exit 1 for differences, blocking diagnostics or legacy mise shadowing; 0 otherwise. Not a Git-drift gate. |
| `ow status` | Existing Git status plus generation diagnostics/pending difference counts. Preserve its existing exit contract; never write. |
| `ow switch/pull/rebase/reset` | Keep Git semantics and existing exit codes; refresh from the full resulting workspace once after a successful mutating batch. |
| `ow fetch` | Ref-only; no render. |
| `ow mv`, unarchive | Relocate and update index, then refresh paths for schema 2. No implicit migration. |
| Archive | Move the entire workspace including manifest/lock without render/install/migration. |
| rm | Preserve existing safeguards and raw complete manifest backup, including typed overrides. |

Initial repository selection has no magic `community` alias: selected repos may propose `master` as the existing simple default; do not preselect a core repo merely because of its name. `-c` copies declared intent and sparse overrides, not inherited defaults or transient observed branches. A v1 source is translated/validated in memory for the new target and never rewritten as a side effect of copying it.

**Create-only repair:** the current `ensure_workspace_materialized` attaches/detaches existing worktrees and rewrites upstreams. Replace that behavior, do not merely call it from a newly re-runnable init. For every existing valid declared worktree, leave HEAD/branch/upstream/index/worktree untouched and report drift. For an absent path, ensure bare repo/ref and create from its recorded spec. A path occupied by an unrelated directory or invalid registration is an error, never deleted or commandeered. Persist complete intended membership before creation so failed repos remain retryable. Never clean up an existing workspace; retain a new manifest on partial or total repo-creation failure too.

When the absent worktree's local branch already exists, preserve its HEAD and published upstream (or intentionally absent upstream). Set the base as upstream only when creating a new local branch. The current `create_worktree` writes upstream even for an existing branch, so that helper must change with repair; otherwise recreating a feature worktree would erase the base/upstream distinction.

**Automatic refresh gate:** track successful executed mutating plans, not just command exit or HEAD hashes (a hard reset can change files without changing HEAD). No refresh for dry-run, declined action, no-op batch or fetch. If any repo operation in the batch failed, or any configured existing repo is busy afterwards, skip the whole refresh and report why. This conservative partial-failure policy avoids rendering a mixed state. The original Git failure remains nonzero. An all-success batch refreshes using the full workspace, never the `--only` subset.

Generation failure after successful Git or relocation makes the overall command exit 1, while explicitly reporting that Git/the move succeeded and files were not refreshed. It does not print a fictitious rollback. Pending legacy migration alone is a warning/skip, not a new Git failure. User cancellation keeps code 2 for prompted destructive operations; init cancellation is standardized to 2.

Trust is explicit: init/render may `mise trust` only the one fragment written, updated or equal to the current generated proposal, never `yours`, ignored or unsafe content. Automatic refresh does not newly trust files. A paranoid mise setup may require the user to run explicit render after generated changes. Trust failure is reported as incomplete environment preparation with exit 1; generated files and successful Git operations stay in place.

## 8. CLI, TUI and dependency cutover

Remove `ow apply`, `ow templates`, init `-t/--template`, template completion, bundle APIs, `VarsEditor`, `.vars`/`.templates` public model fields, vendored wrappers and all packaged Jinja assets. No aliases or deprecated forwarding modules. `ow render` and `ow files` support the existing positional/`-w` workspace-resolution rule. `--check` is not transferred blindly: its old Git-and-files meaning is replaced by status plus the explicit file gate.

Removing the command means removing its name from the messages that recommend it, and the replacement is not one command. A worktree that drifted from its spec, or sits detached where the config names a branch, is realigned by `ow switch` — rendering never moves a worktree. A missing worktree or bare repo is repaired by `ow init` inside the workspace. Only "files were not re-rendered" becomes `ow render`, and the old `ow apply --check` becomes `ow files --diff` for file state plus `ow status` for Git state. Guidance that still names a removed command is a cutover defect, not cosmetic wording.

TUI retains the dashboard and Git workflows. Replace `a Apply` with `a Render`; add `F Files` (diff/file gate) and `I Repair` (existing-workspace init). Keep `f` fetch, `r` reset, `R` rebase, `P` pull, `S` switch and other bindings. Update HelpScreen and operation labels together. Repo edits save intent and offer repair; option-only edits offer render. Saving global defaults never iterates and mutates every known workspace.

Use a finite typed options editor, not a renamed arbitrary key/value table. Show inherited value and source (built-in/global/local), and an explicit override switch. Support integers, strings and JSON-style argument arrays; unset means inherit, an enabled empty string/list is a real override. Mask passwords in summaries/logs. Global editor exposes fixed Odoo/mise defaults and a simple ordered ignore-pattern list; keep remote URL editing and theme/editor behavior. Preserve the dashboard's shared Config object when reloading, including new fields and legacy metadata.

Keep Typer, Rich, Textual, tomli-w and tomlkit. Add **`pathspec>=0.12,<1`**. Remove Jinja2 only when workspace and service rendering no longer depend on it; no new templating or YAML package. Update all callers, constructors, fixtures, completion/help, package data and migration scripts in the same release. Historical plans remain historical; current user docs must describe only shipped behavior after implementation.

## 9. Acceptance and evidence

The implementation plan maps every R01–R18 requirement to exact tasks and checks. Permanent tests defend observable behavior: inheritance/reset, no code execution, supported shapes, ownership transitions, ignore precedence, migration interruption/unknown data, repair without switching, dry-run/failure boundaries, relocation, and meaningful CLI/TUI interaction. Do not retain tests that merely pin renamed symbols, headers, plumbing or incidental wording.

Required end-to-end evidence before implementation is declared complete: isolated XDG directories; disposable local bare repositories with separate base and upstream histories; valid synthetic Odoo metadata; generic and Odoo init; repair, switch/pull/rebase/reset including conflict; files gate; move/archive/unarchive; v1 migration; wheel-installed CLI and actual TUI interaction. No production workspace or network clone is needed. Recheck the supported mise floor in addition to the installed newer version. No implementation is claimed by this planning document.

Planning evidence checked on 2026-09-20:

- Current source CLI reports `2.4.2.dev4+gbcb32de84.d20260919`; plain source invocation needs `PYTHONPATH=src` in this environment.
- [mise 2026.8.13 release](https://github.com/jdx/mise/releases/tag/v2026.8.13) introduced visible `mise/conf.d` fragments; [configuration precedence](https://mise.jdx.dev/configuration.html) documents root overrides.
- A disposable fragment on installed mise 2026.9.9 resolved `config_root` to a workspace with spaces, and a file task preserved arguments with spaces and quotes. The proposed shebang-task wrappers and sandbox mounts still require their implementation smoke checks; this is not a sandbox-security certification.
- [Odoo 18 Python bounds](https://github.com/odoo/odoo/blob/18.0/odoo/__init__.py), [19 release](https://github.com/odoo/odoo/blob/19.0/odoo/release.py), [20 release](https://github.com/odoo/odoo/blob/20.0/odoo/release.py), and [master release](https://github.com/odoo/odoo/blob/master/odoo/release.py) were read. 18/19 currently declare 3.10–3.14, 20/master 3.12–3.14. Runtime code reads the checkout rather than embedding these bounds.
- [Upstream sandbox README](https://github.com/odoo/odoo/blob/master/setup/sandboxing/README.md) and actual bwrap/firejail paths were inspected. `ODOO_BASE` alone does not expose external Git common directories.
- Four read-only inventories covered config/migration/index, Git lifecycle, CLI/TUI/docs, and every generated asset. No application implementation was performed.
