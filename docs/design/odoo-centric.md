# ow, Odoo-centric without losing workspace intent

Design for issue #45, revised after the branch review on 2026-09-19.
**Status: target design, not an implementation report.** The current branch still uses
Jinja bundles, workspace `[vars]`, `ow apply` and `ow templates`. Its direct rendering,
output lock and `mise/conf.d/00-ow.toml` are implemented. The command and schema changes
below belong to the subsequent refactor, not to the review corrections.

## 0. Direction and boundaries

ow is an Odoo workspace manager, not a general configuration-management framework.
A workspace remains a plain directory of Git worktrees, not a Git repository at its root.
Infrastructure and documentation workspaces remain valid: without an Odoo core checkout,
ow manages the worktrees and generic tooling but generates no Odoo configuration.

Three different kinds of information must stay separate:

- **Observed state belongs to Git:** current branch, HEAD, upstream, dirty files and
  operations in progress.
- **Workspace intent belongs to ow:** which repos should exist, the base a feature follows,
  a detached ref or SHA, and explicit workspace options.
- **Generated contents belong to the renderer:** values derived from the checkout and
  options. A hand-edited output is not a reliable source of configuration.

Deleting an intent file does not make its information derivable. Conversely, retaining a
small manifest does not require retaining a template framework.

### Decisions

| Concern | Decision |
|---|---|
| Odoo identity | Parse the checkout without executing it; probe capabilities separately |
| Support window | master, the three current stable majors, and their saas branches |
| Rendering | A fixed table of generators, no user-defined template engine |
| Output ownership | Keep the existing output lock and protection of divergent files |
| Workspace state | Keep `.ow/config.toml` as the intent manifest and workspace marker |
| Options | Typed global defaults, with sparse typed workspace overrides |
| Ignore | Global `owignore`, workspace-relative gitignore patterns |
| Commands | Explicit `ow render`, read-only `ow files`, re-runnable `ow init` |
| Sandboxes | Alias scripts present in the Odoo checkout; stop vendoring copies |
| Local files | Optional copy-once seed before the addon scan, not another renderer |

**Support snapshot:** master, 20.0, 19.0, 18.0, and `saas-18.*`, `saas-19.*`,
`saas-20.*`. A new stable major requires an ow support-table update; an unknown newer
major is not silently supported. Removing a major from the window is a documented support
change. The window is not inferred from the wall clock or the branch name.

## 1. Detect the checkout, not its branch name

Keep `is_odoo_main_repo`: `odoo-bin`, `addons/` and `odoo/addons/` identify a core
checkout. Read `<main_repo>/odoo/release.py` for its identity. Branch names are unsuitable
because a checkout may be on a feature branch or a detached SHA.

Read Python syntax with the standard-library AST parser, never `import`, `exec`, or
`odoo-bin --version`. Inspect the literal first two elements of the `version_info`
assignment; the later `FINAL`/`ALPHA` names do not need evaluation. Python bounds are
literal integer tuples. An unfamiliar expression is a diagnostic, not code to execute.

| First two values | Identity |
|---|---|
| `(18, 0)`, `(19, 0)`, `(20, 0)` | Stable series |
| `(20, 1)` | Development series; not a promise that the branch is current master |
| `('saas~19', 4)` | saas series; not the capability profile of stable 19.0 |

Check the numeric major against the supported window for **all** these shapes. An old
`(17, 5)` snapshot must not bypass the window merely because it looks like trunk.
Historical development snapshots inside the window use their actual capabilities, not
those of today's master. If more than one configured repo matches the core markers,
report the ambiguity and do not choose by incidental dictionary order.

Distinguish these outcomes:

1. **No core checkout:** normal non-Odoo workspace; generic outputs only.
2. **Recognized, supported core:** generate generic and Odoo outputs.
3. **Recognized but unsupported version:** warn with series and support window; no new
   Odoo outputs.
4. **Recognized but unreadable/unparseable core metadata:** report the failing path and
   reason; do not classify it as a non-Odoo workspace or guess a version.

For outcomes 3 and 4, list existing Odoo outputs that will remain untouched. The renderer
never deletes them, so it cannot promise that an old launch configuration is safe for the
new checkout. `ow render`, `ow files` and `ow files --diff` all print the diagnostic and
exit non-zero for these two outcomes: a workspace whose Odoo configuration cannot be
generated is not a workspace whose files are in order. `ow status` prints the same
diagnostic and keeps its own exit contract, because it reports state rather than gating on
it. Outcome 1 is not a diagnostic: a workspace with no core checkout is aligned as soon as
its generic outputs are.

## 2. Capabilities and Python selection

### Demo arguments

The option table lives in `odoo/tools/config.py`. Probe actual option declarations, not
an arbitrary string in a comment. A literal `--with-demo` argument in an option declaration
is the capability of interest; do not execute the module to construct its parser.

| Readable option table | Default `debug_args` |
|---|---|
| Declares `--with-demo` | `["--dev=all"]` |
| Declares `--without-demo`, but not `--with-demo` | `["--dev=all", "--without-demo=all"]` |
| Neither declaration recognized, table missing, or parse failed | Diagnostic; no guessed Odoo output |

18.0 uses the second row. saas-18.4 already uses the first, as do 19.0 and later checked
series. This is why there is no `saas~X -> stable X` profile mapping.
`debug_test_args` defaults to `["--test-tags=<workspace-name>"]`. An explicit typed override
replaces the whole argument list. ow does not silently rewrite user-supplied arguments.
The odoorc keys emitted today are shared by the supported series; no per-major key map is
needed. Do not add a profile abstraction for hypothetical future differences.

### Python

Read `MIN_PY_VERSION` and `MAX_PY_VERSION` from `release.py`, falling back to
`odoo/__init__.py` for the 18.0 layout. Missing or invalid bounds are a metadata error,
not an excuse to silently use defaults in a recognized Odoo checkout.

`[mise].python` is a quoted `major.minor` preference, default `"3.12"`. Compare integer
pairs, never floats or lexical strings. Clamp it to the checkout's inclusive minor-version
bounds and warn with requested version, effective version and checkout series when changed.
`MAX_PY_VERSION` is Odoo's declared support ceiling, not necessarily a fatal runtime limit:
19.0's server warns when `sys.version_info[:2] > MAX_PY_VERSION`.
The exact same effective version goes to mise and Pyright. Non-Odoo workspaces use the
preference unchanged. Patch pins and mise aliases are not part of this typed option.

Rendering does not install Python, recreate a venv, or run dependency installation.
When changing the effective minor, warn that an existing `.venv` may need rebuilding;
never delete it. File consistency is not proof of interpreter consistency.
PostgreSQL bounds can be checked against the bundled service image, but rendering does
not inspect or upgrade a user's external database server.

## 3. A small renderer, with the existing ownership boundary

Replace workspace Jinja bundles with `ow/utils/generate.py`: a fixed table of relative
output paths and generator functions `(context) -> str | None`. `None` means no output
for this workspace. Use JSON/TOML serializers where appropriate rather than interpolating
unescaped values. A typed context carries workspace name/path, repo aliases, core identity
and capabilities, addon paths, service locations, and effective options.

No generator registry, plugin API, override tree, inheritance, or multiple render passes.
Generators do not create addons. Optional local seed copying happens before the context is
built (§5), so one scan sees everything. Keep `.local` scanning and loose-addons-first
ordering. Generic and Odoo-specific outputs are explicit table entries, not implicit bundles.
Editor outputs can be opted out globally through `owignore`; their Odoo portions are absent
without a supported core checkout.

### Keep `.ow/rendered.lock.toml`

The lock stores the hash of the last output ow wrote or adopted, keyed by output path.
It is independent of Jinja and survives the generator cutover:

- Absent output: write and record it.
- Existing bytes equal the proposed output: adopt without rewriting.
- Existing bytes match the lock but not the proposed output: update and record it.
- Existing bytes differ from both: preserve them and report `yours`.
- No proposed output: leave disk untouched and report any retained, previously managed file.

Adoption is deliberate: a file classified `yours` may become managed again if a later
render exactly matches it. Do not promise that ownership can never change again.
A missing file can be recreated; the lock is not a tombstone mechanism.

Keep the user-visible states `up to date`, `outdated`, `yours`, `absent`, and `not rendered`;
add `ignored` for the explicit opt-out. An obsolete path recorded in the lock remains
visible while its file exists. Neither removing a generator nor changing Odoo versions
silently hides its retained output. Directories or unsafe output-path conflicts must be
reported as errors, not treated as writable missing files.

`ow files --diff` shows textual changes, additions from `/dev/null`, and a path-naming
notice for binary differences. It does not assert that every output is UTF-8 text.
The preview never writes, adopts, trusts, installs or seeds files.

The global services compose file is outside the workspace path namespace. Keep its existing
`ensure_services_compose` lifecycle as a separate operation; do not pretend workspace ignore
patterns or a workspace lock govern a machine-wide file. `ow files` checks workspace outputs,
not the availability or configuration of running services.

## 4. Global defaults and explicit local exceptions

Replace untyped `[vars]` with typed `[odoo]` and `[mise]` tables. Effective values are:
**built-in default < global value < explicit workspace value**. Override by key, not by
replacing a whole table. Argument arrays replace whole arrays. Validate values before Git
mutations or file writes; unknown keys are configuration errors, not silently ignored data.

Global configuration:

```toml
owignore = [".zed/**", "!.zed/settings.json"]

[odoo]
http_port = 8069
db_host = "localhost"
db_port = 5432
db_user = "odoo"
db_password = "odoo"
admin_passwd = "Password"
smtp_server = "localhost"
smtp_port = 25
# debug_args and debug_test_args are optional complete-list overrides.

[mise]
python = "3.12"
```

Workspace exceptions remain sparse in `.ow/config.toml`:

```toml
version = 2

[repos]
community = "19.0..feature"

[odoo]
http_port = 8068
```

This allows two instances to run on different ports while both keep generated addon paths.
Changing a global default affects the next render in workspaces that do not override it.
The TUI displays inherited values separately from overrides; clearing an override means
inherit, not copying the current global value into the workspace.

### `owignore`

Keep the requested **global-only** pattern list. Match workspace-relative output paths
using `pathspec.GitIgnoreSpec`, in order, with gitignore-style negation. Test anchored names,
nested paths, directory patterns and re-inclusion against that library's documented semantics.
Do not reimplement gitignore with `fnmatch` or shell out to Git at the non-repository root.

Ignored outputs are neither written nor adopted, and are not counted as missing or differing
by the file gate. Existing ignored files stay on disk. The lock entry may remain; removing
an ignore resumes the ordinary ownership check, not unconditional overwrite.

An `odoorc` ignore applies to **every** workspace. It is for a machine-wide preference,
not a workaround for phone-service's port. Workspace-specific settings use the typed
options above; an arbitrary hand-edited file is protected by the lock.

## 5. Local seed files and upstream sandboxes

### A copy-once seed, not a custom bundle

The existing `local` bundle serves a real use: a dev addon and a helper script. Retain that
use without a second template system. If `$XDG_CONFIG_HOME/ow/local/` exists, `ow init`
copies missing regular files into `<workspace>/.local/`, preserving executable bits,
**before** scanning addons. Existing destinations are never overwritten or deleted.
Report conflicting paths; do not follow seed symlinks outside the source tree.
This is an init-time seed, not a continuously synchronized tree; `render`, `files` and
`status` never copy it. Re-running init may add newly supplied, previously absent files.

There is no interpolation and no bundle declaration. Existing `.local` contents stay
valid and continue to participate in addon discovery. Migration does not automatically
reinterpret every arbitrary user template as a seed (§8).

### Sandbox tasks

Stop vendoring the bwrap copies. Inspect each known script in the detected core checkout's
`setup/sandboxing/` and generate a mise task only when that specific script exists.
Use the detected alias, never hardcode `community`. `ODOO_BASE` is the workspace root;
file tasks pass arguments through to the upstream script. Serialize TOML and quote shell
arguments independently, including paths containing spaces.

Expose the firejail task only when its profile exists. Use the profile by its actual
checkout path; editor-local firejail setup remains a manual upstream-documented action.
These are commands from the checkout, not a sandbox implementation audited or owned by ow.

The reviewed upstream snapshot has these scripts on master/20.0, not 18.0/19.0. Detection
is by presence so backports work. A non-Odoo workspace such as voip-infra gets no upstream
sandbox tasks either. This is an intentional feature reduction: users needing a sandbox
there supply their own mise task. Existing wrappers are not deleted; migration reports
that they are no longer maintained rather than presenting them as the new tasks.

## 6. Keep a manifest of intent; do not move it into Git internals

`.ow/config.toml` remains the workspace marker and versioned intent store. Its target schema
contains `version`, `[repos]`, and optional typed `[odoo]`/`[mise]` overrides. `templates`
and free-form `vars` disappear, not the file itself. The workspace directory supplies its name.

Keep BranchSpec's distinction between an attached branch and a detached ref/SHA. Git remains
the observed truth: a manual branch switch or detach is displayed honestly, never hidden by
the manifest. Keep drift detection wherever a command might otherwise act on stale intent.

**Base is not upstream.** A branch may rebase onto `origin/19.0` while pulling its own
published history from `dev/feature`. A `git push -u` must not replace the rebase base.
Preserve the separate concepts already present in `RepoFacts.base` and `RepoFacts.up`.

A missing declared worktree is reportable and repairable even after `git worktree remove`
or `git worktree prune`, because its alias and spec still exist in the manifest. Worktree
discovery corroborates state; it does not silently add unrelated repos or remove declarations.
Use Git's supported worktree interfaces rather than making private `worktrees/*/gitdir`
layout a new persistence API. A repo observed on an unexpected branch is not automatically
switched back by a routine render or status command.

### Why the per-worktree `ow.ref` proposal is rejected

The review exercised disposable bare repositories:

- `git switch --detach <sha>` leaves a pre-existing `ow.ref` unchanged. Comparing HEAD with
  the current ref cannot distinguish manual movement from a remote ref advancing after fetch.
- `git worktree remove` deletes its registration and per-worktree configuration; discovery
  cannot reconstruct the removed workspace membership or pin.
- Enabling `extensions.worktreeConfig` while `core.bare=true` remains in the shared config
  makes worktree commands fail with `fatal: this operation must be run in a work tree`.
  Git requires moving that setting to the principal worktree's `config.worktree` first.

Those problems can be engineered around, but they introduce more state and migration risk
than retaining the existing manifest. ow does not enable this extension for the redesign.

`ow init -c` continues to copy a workspace's declared intent and explicit overrides, not
its transient observed branch state or inherited global values. `mv`, archive/unarchive and
`rm` retain the manifest; rm backs it up with the local options, not merely an alias list.
The index remains a location aid, not a second source of workspace truth.

## 7. Command lifecycle

| Command | Target behavior |
|---|---|
| `ow init` | Create a workspace or materialize missing declared repos; seed local files; render |
| `ow init -r alias:spec` | Add intent and materialize an absent repo; refuse changing an existing conflicting spec |
| `ow switch` | Explicit branch move; persist resulting intent as today; render actual resulting state |
| `ow render` | Render files and refresh services compose; trust the generated mise fragments |
| `ow files` | Read-only output states; non-zero only on the §1 diagnostics |
| `ow files --diff` | Read-only diff/file gate; 0 aligned, 1 differences or §1 diagnostics, including missing outputs |
| `ow status` | Observed Git state, intent drift, missing repos and render diagnostics; no writes |
| `ow fetch` | Refresh the refs followed by declared specs and upstreams; no worktree or render changes |

**The migration has one entry point: `ow render`.** It is the first command that must read
the new options to do its job, it already writes, and it is explicit — so it validates the
whole new configuration, backs up the original and rewrites the manifest before rendering
(§8). `ow init` performs it too, for the workspace it materializes. Every other command
reads a version-1 manifest for as long as one exists: `status`, `files`, `ls`, `fetch` and
the Git commands interpret the legacy `templates`/`[vars]` data, name the pending migration
once, and change nothing on disk. `switch` is the one exception worth stating: it persists
intent after a move, so it writes back the schema it read — a version-1 manifest stays
version 1 with its unknown keys intact, and `ow render` remains the only thing that
converts it. Nothing refuses to run because a manifest is old, and nothing migrates as a
side effect of being asked a question.

A re-runnable init is not permission to reset existing worktrees. Refuse conflicting `-r`
intent with an actionable `ow switch` message. Report existing branch drift rather than
silently reconciling it. Materialize an absent worktree only from its recorded spec; failures
remain visible and leave enough intent for retry.

`switch`, `pull`, `rebase` and `reset` may change the checkout's rendering inputs. After an
actual successful mutation, refresh generated files from the resulting checkout; dry runs
and aborted/conflicted repos do not trigger rendering from an unstable core checkout.
If a multi-repo operation partly succeeds, report both Git results and any render skip/failure.
Never claim a Git rollback because a later render failed. File rendering does not turn a
failed Git operation into success. Existing hand edits remain protected by the lock.

`mv` and unarchive re-render after relocation so computed paths stay current. Archive itself
parks the workspace without running installation. Direct Git commands and global option
edits do not invoke ow: use `ow render`, with `status` able to report pending changes.

`ow apply` is removed only when init's materialization path and render are complete.
`ow templates` becomes `ow files`. Update CLI help, TUI bindings and all callers together;
no compatibility alias. The TUI workspace screen remains useful for repo intent and typed
local overrides; it is not deleted merely because the template selector disappears.

The old `apply --check` checked both Git intent and files. `files --diff` is explicitly a
**file gate**, not proof that the worktrees match their declarations. Status remains the
place to inspect Git drift. File differences include `yours`; `ignored` and deliberately
retained `not rendered` outputs are shown but not proposed changes. Unsupported/broken Odoo
metadata is a diagnostic failure, even if every old output remains on disk.

## 8. Migration and delivery boundaries

This is a breaking schema/CLI refactor, not an invisible implementation swap. Keep the
current phase-1 fixes independently usable; do not publish intermediate states that remove
ownership protection before the replacement renderer and options are ready.

### Configuration migration

The target workspace schema is version 2. A version-1 manifest remains recognizable; its
presence is not a migration marker by itself. Read-only commands may interpret legacy data
and warn, but never rewrite it. A mutating migration validates the complete new configuration,
preserves the original in `$XDG_STATE_HOME/ow/backups/`, then atomically writes the version-2
manifest. Write the version only with the completed new content; an interrupted migration
must be retryable without consulting a stale, supposedly dead config file.

Map supported global `[vars]` keys to typed global tables; explicit new typed keys win.
Map supported workspace `[vars]` keys to **explicit local overrides**, including ports,
Python and debug argument arrays. Do not erase equal-looking workspace values: the old file
cannot tell whether a copied value was meant as an override. Report the preserved overrides
so the user may later clear any they want to inherit globally.

Unknown custom variables, unsupported Python selectors and template customizations require
an explicit migration diagnostic naming what cannot be represented; do not drop them and
mark the migration complete. Before retiring custom bundles, inventory their outputs and
explain which will remain as user-maintained files. Old per-workspace `.ow/templates` edits
are not equivalent to new generated contents and must not be silently discarded.

Keep the output lock throughout. Existing divergent outputs remain protected; identical
outputs may be adopted under the ordinary rule. Preserve and report old root `mise.toml`,
which can override the fragment. Do not auto-delete it, recreate `.venv`, or run install.

### Dependency order, not independent promises

- Establish and test checkout identity/capabilities and typed option resolution before
  replacing version-sensitive output defaults.
- Introduce the fixed renderer with the existing ownership algorithm and manifest intact.
  Migrate configuration, bundle customizations and command callers in the same deliverable;
  Jinja leaves only after both workspace and service rendering no longer depend on it.
- The optional local seed must precede the one-pass addon scan. Sandbox aliases use that
  renderer and are delivered with the explicit 18/19/non-Odoo feature-loss documentation.
- Remove apply/template APIs only after every CLI/TUI/move/archive caller has moved to the
  replacement behavior. Current command docs must never describe this target as already shipped.

### Behavioral acceptance

Keep regression coverage for observable contracts, not a test per renamed function:

- Stable, saas and historical development shapes; unsupported or unreadable metadata;
  option presence distinguished from parse failure; no checkout code execution.
- Demo arguments on both eras; inclusive integer Python bounds; one effective version for
  mise/Pyright; no venv deletion or installation during rendering.
- Sparse workspace overrides and global inheritance, especially distinct concurrent ports.
- First render over an unknown divergent file, identical adoption, subsequent edits,
  ignored/retired paths, missing and binary output diffs, and no destructive migration.
- Base distinct from pushed upstream, manual Git drift, missing worktrees after remove/prune,
  and copying/relocating/archiving intent without replacing it with observed state.
- Init repair without switching existing repos; dry-run/conflict/partial-success rendering
  boundaries; Git success reported separately from a later generation failure.

## Evidence and limits

The earlier R&D collected Odoo checkout facts and mise fragment probes; those are snapshots,
not guarantees for future branches. Relevant upstream sources include
[18.0's Python bounds](https://github.com/odoo/odoo/blob/18.0/odoo/__init__.py),
[19.0's release metadata](https://github.com/odoo/odoo/blob/19.0/odoo/release.py), and
[19.0's inclusive Python ceiling check](https://github.com/odoo/odoo/blob/19.0/odoo/cli/server.py).
The latter three were re-read during this revision. Git's shared/per-worktree configuration
rules are documented in [git-worktree](https://git-scm.com/docs/git-worktree#_configuration_file).
The pin/removal/configuration failure cases above were exercised during the review in
throwaway repositories, without modifying the user's bare repos or workspaces.

The review corrections do not implement this target architecture: they fix legacy `init -c`,
mise fragment trust, binary/missing diffs, generated Python agreement, second-pass reporting,
and visibility of retained locked outputs. They intentionally retain today's public commands
and configuration format until the coordinated refactor described here.
