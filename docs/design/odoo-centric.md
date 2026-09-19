# ow, odoo-centric again

Design decisions for issue #45 — R&D phase. **Status: draft, awaiting validation.**
Nothing below is implemented. Phase 1 on `rnd/45-odoo-separation` (the three axes, the
output lock, `mise/conf.d/00-ow.toml`) is; §9 says what survives of it.

The target in one paragraph: ow generates a handful of files into a workspace. Which files,
and what they contain, follows from three things only — what the workspace's repos *are*
(an Odoo checkout, and which version of it), what you configured once, globally, and what
you told ow to keep its hands off. No template system, no per-workspace config, no lock,
no `ow apply`. ow owns the files it writes, you own everything else, and the boundary is a
pattern list. The workspace stays a plain directory of git worktrees; ow stops being a small
configuration manager and goes back to being an Odoo workspace manager.

## Checklist

| # | Question | Decision |
|---|----------|----------|
| 1 | Detecting the Odoo version | §1 — parse `odoo/release.py`, map to a profile |
| 2 | Files that follow the version | §2 — profile table: odoorc keys, editor args |
| 3 | Generation in the code, not in templates | §3 — `_static/templates/` and Jinja die |
| 4 | `owignore` | §4 — a global pattern list, gitignore syntax, `pathspec` |
| 5 | bwrap/firejail from the Odoo repo | §5 — mise tasks alias upstream scripts; vendoring dies |
| 6 | The end of `ow apply` | §6 — worktrees to `ow init`, files to `ow render` |
| 7 | The end of `.ow/config.toml` | §7 — specs live in git; the directory is the marker |
| 8 | Global `[vars]` become ow options | §8 — `[odoo]` + `[mise]`, read at render time |

## 0. Constraints

- **ow stays an Odoo tool.** The generic machinery (worktrees, refspecs, bare repos) is the
  means; serving an Odoo checkout is the point. Every decision below may cut a feature to
  keep that line straight.
- **Support window** (user decision, 2026-09-19): master, the three stable majors below it,
  and the saas branches in the gaps. Today: **master, 20.0, 19.0, 18.0**, plus `saas-18.*`,
  `saas-19.*`, `saas-20.*`. Older versions are out.
- All four real workspaces fall inside it — quattromori (19.0, pinned by sha), phone-service
  (18.0), voip (master), voip-infra (no Odoo at all).
- **The workspace is not a git repository.** It is a directory whose subdirectories are
  worktrees of the shared bare repos under `$XDG_DATA_HOME/ow/repos/` (`utils/git.py:559`,
  `paths.py:44`); no code runs `git init` at its root.
- Evidence convention: every claim cites a command or a `file:line`. Where nothing was run,
  the claim is labelled. Four fact-finding passes were run for this document (see the
  "Evidence" notes per section); their raw payloads are in the session artifacts.

## 1. Detecting the Odoo version

**Facts.** Every checkout carries its identity in `odoo/release.py`, at the same path and
with the same shape from 11.0 through master:

```python
# origin/18.0   version_info = (18, 0, 0, FINAL, 0, '')
# origin/19.0   version_info = (19, 0, 0, FINAL, 0, '')
# 20.0 branch   version_info = (20, 0, 0, FINAL, 0, '')
# saas-18.4     version_info = ('saas~18', 4, 0, FINAL, 0, '')
# master today  version_info = (20, 1, 0, ALPHA, 1, '')
series = serie = major_version = '.'.join(str(s) for s in version_info[:2])
```

(`git show <ref>:odoo/release.py` on the local mirror for 18.0/19.0/saas-18.4/saas-19.4;
`raw.githubusercontent.com/odoo/odoo/{20.0,master}/odoo/release.py` for the two the local
mirror is behind on.) Nothing else in the tree is a version source: `odoo/__init__.py`,
`odoo-bin` and `setup.py` either carry none or `exec` this file.

Three shapes, and the middle one is the trap: **master's series is not the next major.**
It goes `18.5` → `19.1` → … → `19.5` → `20.1` (one bump per saas cycle — `git log -- odoo/release.py
origin/master`), so a checkout of master says `20.1` while 20.0 is a released branch. And a
saas branch's series is the literal string `saas~18.4`, not its base major.

**Decision.** Read `<main_repo>/odoo/release.py` with a regex, and map `version_info` to a
*profile* by shape, never by number:

| `version_info[:2]` | series | what it is |
|---|---|---|
| `(18, 0)` | `18.0` | a stable major, in the window |
| `(19, 0)` | `19.0` | a stable major, in the window |
| `(20, 0)` | `20.0` | a stable major, in the window |
| `(20, 1)` … | `20.1` | **trunk** (master — whatever major it becomes) |
| `('saas~19', 4)` | `saas~19.4` | a saas of the 19 series — its code is its cut era's, §2 |

So: minor `0` → a stable major, an integer minor ≥ 1 → the trunk, a `saas~X` major → the saas
series of major X. What the series decides is **the window** — whether this checkout is one
ow supports at all — and nothing about the content it writes: the trunk is keyed by *kind*
(which is why the `19.5 → 20.1` jump on master changes nothing), and a saas branch carries
the code of the era it was cut from, not of the major in its name (§2).

The same parse picks up the Python and PostgreSQL bounds, because the checkout states them
itself — and *where* it states them moved inside the window:

| ref | file | min Python | max Python | min PostgreSQL |
|---|---|---|---|---|
| 18.0 | `odoo/__init__.py` | 3.10 | 3.14 | — |
| 19.0 | `odoo/release.py` | 3.10 | 3.14 | 13 |
| saas-18.4 | `odoo/release.py` | 3.10 | 3.14 | 13 |
| saas-19.4 / 20.0 / trunk | `odoo/release.py` | 3.12 | 3.14 | 16 |

`odoo/__init__.py` up to 18.0 carries `MIN_PY_VERSION`/`MAX_PY_VERSION` and asserts the floor
at import (`assert sys.version_info > MIN_PY_VERSION, "Outdated python version detected…"`);
from 19.0 on they live in `odoo/release.py`, with `MIN_PG_VERSION`. (Replay: `git show
<ref>:odoo/__init__.py` and `:odoo/release.py` through `sed -n '/MIN_PY_VERSION/p;/MAX_PY_VERSION/p;/MIN_PG_VERSION/p'`.)
The series (the window), the Python bounds, and the option table (§2) are the only three
things ow reads out of a checkout; everything else it writes is fixed.

Rejected alternatives:

- **Importing `release.py`** — running a repo's code to ask its name is an execution surface
  for a value that a 20-line regex reads.
- **`odoo-bin --version`** — needs a working interpreter and dependencies; ow renders files
  *before* `mise install` has ever run in a fresh workspace.
- **`git describe` / branch names** — a worktree's branch is `master-voip-boi` or a raw sha
  (quattromori's three specs are pinned shas); the file is the only truth.

**Out of window.** An Odoo checkout older than the window (17.0 and below) gets a warning
naming the version and the window, and **no Odoo files are generated** for it: a 17.0 odoorc
fed 19.0 keys would be wrong in ways the user cannot see. Worktrees, `status`, `fetch`,
`switch` keep working; only generation stops. (17.0's `xmlrpc_port` key and the 19.0
`with_demo` flip in §2 are exactly the kind of difference a "best effort" would get wrong.)

**Missing or unreadable `release.py`** — not an Odoo workspace: `is_odoo_main_repo` already
requires `odoo-bin` + `addons/` + `odoo/addons/` (`utils/templates.py:36`), and a checkout
that has those but no parseable `release.py` is broken, so it gets the same warning path.

## 2. What the version changes

The option table is **optparse in `odoo/tools/config.py`**, not argparse in `odoo/cli/server.py`
(that file only calls `config.parse_config()`); the config-file keys are the same `dest`s.
Within the window, three differences matter to what ow writes, and one of them is a live bug.

**Demo data (18.0 installs it, 19.0+ does not).** 18.0 has `--without-demo`, and
`odoo/modules/loading.py` loads demo unless it is set — demo is on by default. 19.0 replaced
it with `--with-demo` (`my_default=False`) and `odoo/orm/registry.py` reads
`config['with_demo']` — demo is off by default. The 18.0 spelling `--without-demo=all` still
parses on 19.0+ but warns (`"since 19.0, invalid boolean value"`).

**`--dev` grew a mode.** `--dev=all` is accepted everywhere, but 18.0 expands it to
`reload,qweb,xml` and 19.0+ to `access,qweb,reload,xml`. Nothing ow writes depends on the
expansion; the value is forwarded.

**Editor args.** ow's current default, `["--dev=all", "--with-demo"]`
(`vscode/.vscode/launch.json.j2:12`, `zed/.zed/debug.json.j2:15`), is **rejected outright by
18.0** — the option does not exist there and unrecognised arguments are fatal
(`die(args, "unrecognized parameters")`). phone-service survives today only because its
config overrides `debug_args`.

**Decision — one probe, on the checkout's own option table.** The only generated content that
follows the version is the demo argument, and the option table says which one applies: a
checkout whose `odoo/tools/config.py` declares `--with-demo` installs no demo data by default
and needs no flag; a checkout without it installs demo by default and gets
`--without-demo=all`. The series is deliberately *not* the discriminator — a saas branch runs
the code of the era it was cut from, not of the major in its name: `saas-18.4` carries
`--with-demo`, `new_db_demo` and the `--stop-after-init` forcing exactly like 19.0, while
18.0 carries none of them.

| checkout | `--with-demo` in the option table | default `debug_args` |
|---|---|---|
| 18.0 | no — demo on by default | `["--dev=all", "--without-demo=all"]` |
| saas-18.4, 19.0, 20.0, trunk | yes — demo off by default | `["--dev=all"]` |

`debug_test_args` stays `["--test-tags=<ws>"]` everywhere, and the odoorc keys ow writes
(`http_port`, `addons_path`, `data_dir`, `admin_passwd`, `db_name`, `dbfilter`, `db_host`,
`db_port`, `db_user`, `db_password`, `smtp_server`, `smtp_port`) are valid in every version in
the window — **no odoorc key differs**, and the 18.0-only `xmlrpc_port → http_port` rename map
is legacy import, not something ow emits. So the version decides the demo flag, not odoorc
content — today.

(Replay: `git show <ref>:odoo/tools/config.py | sed -n '/add_option("--with-demo"/p;/"--xmlrpc-port"/p'`;
the demo default through `odoo/modules/loading.py` — `tools.config['without_demo']` — against
`odoo/orm/registry.py` — `new_db_demo = config['with_demo']`; the odoorc keys with
`sed -n 's/.*dest="\([a-z_]*\)".*/\1/p' | sort -u`.)

Two facts worth keeping in view, both era-discriminated the same way: `--test-tags` forces
`--stop-after-init` from the 19-era on (a test config that waits for a server that exits), and
`--xmlrpc-port` is rejected from the 19-era on. Neither changes what ow writes.

**Python, from the same file.** The checkout states the Python range it supports (§1), and ow
uses it instead of guessing: `[mise] python` (§8) is **clamped into
`[MIN_PY_VERSION, MAX_PY_VERSION]`** — nearest bound wins, with a warning — and the effective
version is what both the mise fragment and `pyrightconfig.json` get. Today they can disagree:
the fragment renders `{{ vars.python | default('3.12') }}` while `pyrightconfig.json.j2`
hardcodes `"3.12"`. The default preference stays `3.12`, which sits inside every range in the
window (18.0/19.0: 3.10–3.14; 20.0/trunk: 3.12–3.14), so no venv rebuilds on upgrade; a
workspace whose checkout moves to 20.0 with the preference left at 3.10 gets a warning and
3.12, instead of `import odoo` failing on Odoo's own assert. `MIN_PG_VERSION` (13, then 16)
needs nothing from ow: the compose stack ships `pgvector/pgvector:pg17` (§3, `compose.yml.j2`),
above every floor in the window.

**Where all of it is read.** Rendering already rescans the workspace before writing; the
version, the bounds and the option-table probe come from the same pass. Because the main
repo's branch can move under ow (`ow switch 19.0`), **`ow switch` re-renders** (§6) — a
workspace on 18.0 that switches to master gets master's args without anyone remembering to
ask.

## 3. Generation moves into the code

**Decision.** `src/ow/utils/generate.py` replaces the template system: a table of managed
files, each with a generator function `(ctx) -> str | None`, where `None` means "no file for
this workspace". `ctx` is a dataclass — workspace name and path, repo aliases, main repo
alias, **the profile from §1**, addon paths, odools items, service paths, and the options
from §8. `ensure_services_compose` is the same mechanism for the one file ow writes outside a
workspace.

**Dies with the template system:** `src/ow/_static/templates/**` and its `package-data`
entry (`pyproject.toml:40`), the Jinja2 dependency, bundle discovery and the per-file
`$XDG_CONFIG_HOME/ow/templates/<bundle>/` override tree (`templates.py:203-254`), the
`templates` field of `WorkspaceConfig`, `selectable_templates`/`effective_bundles`/the three
axes, the `.j2` suffix and output-collision logic, and the second render pass
(`_first_verdict`, `templates.py:512`) — nothing ow generates lands inside an addons
directory any more.

**Survives:** `is_odoo_main_repo` and the addon scan (`find_addon_paths`, including the
`.local` root and the loose-addons-first ordering — both earned by #42), the "ow never
deletes a workspace file" rule, and `ow files` (renamed from `ow templates`, §6).

**Also decided: the rendered lock dies.** Phase 1 introduced
`<ws>/.ow/rendered.lock.toml` to tell ow's files from the user's. It bought safety at the
price of five states, a sha256 state file per workspace, and a rule ("edit a managed file and
it becomes yours") that exists nowhere else in ow. The cut version is one rule instead:
**ow owns what it writes; `owignore` is the only boundary.** `ow files` still shows what
would change and `--diff` still prints the difference — before it is applied, not after.
What this costs: a hand-edit inside a managed file is overwritten at the next render, and the
way to keep it is to ignore the file (then it is yours to maintain) or to move the setting
into `[odoo]` (§8). On the four real workspaces this is a non-event — phone-service's
`odoorc`/`odools.toml` edits added `.local/odoo_addons` to the addons path, which the
generator now computes by itself.

## 4. owignore

**Decision.** A top-level array in the global config — global-only, as asked:

```toml
owignore = [".zed/**", "pyrightconfig.json", "!.zed/settings.json"]
```

gitignore syntax, matched against workspace-relative output paths (`.zed/settings.json`,
`mise/conf.d/00-ow.toml`, `odoorc`), in order, last match wins, `!` re-includes. An ignored
path is never written and never reported as absent; a file already on disk stays (ow never
deletes). `ow files` lists it as `ignored`.

**Implementation: a new `pathspec` dependency** (not installed today — `python -c "import
pathspec"` fails, and it is absent from `pyproject.toml` and from every `METADATA` in the
runtime env). It is the library black depends on for exactly this (`pathspec>=1.0.0` in its
`pyproject.toml` — GitPython, for the record, does not use it: it depends on `gitdb`); note
its current API spells the pattern class `GitIgnoreSpec`/`GitIgnoreBasicPattern`, with
`GitWildMatchPattern` deprecated in 1.0. Rejected:

- **`fnmatch` / `Path.match`** — not gitignore. `fnmatch('sub/debug.json', 'debug.json')`
  is `False` (a bare name matches at any depth in gitignore), `**` collapses to `*`
  (CPython `fnmatch._translate`), there is no `!`, no directory-only pattern, and
  `Path.match` matches `**` as a single segment — verified on 3.12, and documented as
  "not supported by this method" from 3.13 on. A faithful subset is a reimplementation.
- **`git check-ignore`** — the workspace root is not a git repository: run outside one it
  exits 128 (`fatal: not a git repository`), and it hides tracked files, which is backwards
  for files ow writes and git never sees.

The dependency is net-neutral: Jinja2 leaves in §3.

## 5. Sandboxes: borrow the scripts, stop shipping them

**Facts.** `setup/sandboxing/` ships on **master and 20.0 only** — `README.md`,
`bwrap/{bwrap-claude.sh,bwrap-opencode.sh,bwrap-pi.sh}` and `firejail/{claude.profile,code.local}`;
19.0 and 18.0 answer 404. The scripts wrap a fixed binary each
(`OPENCODE_BIN`/`CLAUDE_BIN`/`PI_BIN`), take the workspace from **`ODOO_BASE`** (first allowed
dir and `--chdir`), consume `--add-dir <path>` and forward everything else; they carry a root
guard, `--hostname dev-sandbox`, a `FORBIDDEN_DIRS` pre-flight on `~/.ssh` and `~/.gnupg`, and
(`bwrap-claude.sh`) an `--openrouter` mode. ow's vendored copies are a re-derivation, not
copies: `WORKSPACE_DIR` from the script's own directory, no guards, no hostname, no
`--openrouter`, `--ro-bind-try` for `/etc/alternatives` — and a third generation of the same
script sits in ow's own `scripts/` with `ca-certificates` mounts added. Three copies, no
shared source.

**Decision.** Delete the vendored wrappers and the `bwrap` bundle. When the main repo carries
`setup/sandboxing/`, ow's mise fragment defines tasks that alias the upstream scripts:

```toml
[env]
ODOO_BASE = "{{config_root}}"          # what the upstream scripts read

[tasks.bwrap-opencode]
file = "{{config_root}}/community/setup/sandboxing/bwrap/bwrap-opencode.sh"

[tasks.bwrap-claude]
file = "{{config_root}}/community/setup/sandboxing/bwrap/bwrap-claude.sh"

[tasks.bwrap-pi]
file = "{{config_root}}/community/setup/sandboxing/bwrap/bwrap-pi.sh"
```

(`file =` is a mise shebang task: the script's own interpreter, arguments passed as `$@`;
`{{config_root}}` is the workspace root. Verified on mise 2026.9.9: a fragment at
`mise/conf.d/00-x.toml` holding `[env] ODOO_BASE = "{{config_root}}"` and
`[tasks.t] file = "<abs>/scripts/hello.sh"` answers `mise run t -- a b` with `ARGS: a b` and
`ODOO_BASE=<project root>`; the 2026-09-15 probe on 2026.9.7 had already shown the fragment
discovered with no `mise.toml` present and `{{config_root}}` resolving to the project root.)
`mise run bwrap-opencode -- --add-dir ~/src/x` then
does what `<ws>/bwrap-opencode` does today, with upstream's guards included.

**firejail**: the profiles are installed by hand (`cp setup/sandboxing/firejail/*
~/.config/firejail/`) and used as `firejail --profile=claude.profile --whitelist=$PWD claude`.
One task covers it — `[tasks.firejail-claude] run = "firejail --profile={{config_root}}/community/setup/sandboxing/firejail/claude.profile --whitelist={{config_root}} claude"` —
and nothing more: the editor-side `code.local` is a file to copy, not something ow aliases.

**What this costs, stated plainly:** a workspace on 18.0 or 19.0 gets **no sandbox scripts**
— upstream does not ship them there, and ow stops shipping them everywhere. A workspace on
master/20.0 gets upstream's, better than today's. `docs/sandboxing.md` shrinks to a pointer at
`setup/sandboxing/README.md` plus the `mise run` line.

## 6. The end of `ow apply`

**What it is today** (`commands/apply.py:20-105`, fact sheet): resolve → `--check` (drift +
stale templates) → `ensure_workspace_materialized` (bare repos, refs, worktrees) →
`ensure_services_compose` → `apply_templates` → reports → legacy `mise.toml` warning →
`mise trust` on the rendered fragments → exit code. Its render half has four callers
(`init`, `apply`, `unarchive`, `mv`); its worktree half has two (`init`, `apply`).

**Decision: `ow apply` disappears, and its two halves go separate ways.**

- **Worktrees → `ow init`.** `ow init` is already the creator; it becomes re-runnable in an
  existing workspace — with `.ow/` present it asks nothing (options are global, repos come
  from the command line or from disk) and reconciles: materialize what is missing, re-render.
  That is the repair path after a `git worktree remove` or an `rm -rf community`, and the way
  to add a repo to an existing workspace (`ow init -r community:19.0`). Same shape as
  `git init` in an existing repository.
- **Files → `ow render`** (new, small): render the managed files, refresh the services
  compose, `mise trust` the fragments it wrote, print what changed. `ow init` and `ow switch`
  call it; so can you, after editing `[odoo]` or upgrading ow. `ow apply --check` becomes
  **`ow files --diff`**, which exits non-zero when anything differs or is missing — one
  command for the CI gate, no second semantics.
- **Rendering is not implicit.** `ow status` stays read-only and *reports* drift instead
  ("3 files would change — ow render"). Every command that resolves a workspace and writes
  to it (init, switch, render) keeps its writes explainable; nothing else surprises you.
- `utils/refs.py:116`'s error message ("run `ow apply` to materialize it") points at `ow init`.

**Consequences to carry:** the TUI's `a` binding becomes `ow render`; the "edit config then
apply" screen disappears with the config (§7); `docs/commands.md`, `docs/services.md`,
`AGENTS.md` and `docs/configuration.md` are updated in the same step (configuration.md is
**already stale today**: it still documents `.ow/templates/<bundle>/` and
`.ow/templates.lock.toml`, which phase 1 removed).

## 7. The end of `.ow/config.toml`

**What it holds today**: `repos` (alias → BranchSpec), `templates`, `vars`, `version`. Nine
readers (all commands through `resolver.py:36`, plus `ls`, `prune`, `rm`, `archive`,
`init -c`, the duplicate-branch check and three TUI screens) and three writers (`init`,
`switch` after a move, the TUI editor) — the full inventory is in the coupling fact sheet.
`utils/drift.py` exists to detect what the file cannot: a worktree whose branch moved
underneath it.

**What git already knows.** ow writes `branch.<name>.remote` / `branch.<name>.merge` into
the bare repo for every attached spec (`git.py:536-556`, written directly because a
`--single-branch` bare repo has no refspec for `--set-upstream-to` to validate against), and
the real repos carry them: `branch.master-voip-boi.remote=origin`,
`merge=refs/heads/master`. So an attached worktree's spec — local branch, and the base it
tracks — is *already* stored in git. For a detached pin, git keeps the sha and nothing else:
`git worktree list --porcelain` prints `detached` + sha, and the ref name survives only in
the worktree's HEAD reflog (`checkout: moving from <sha> to origin/18.0`) — which the real
`community.git` expires after a day (`gc.reflogExpire=1.day.ago`), and which is already gone
in quattromori's three sha-pinned worktrees. The reflog is not a store.

**Decision.**

- **A worktree is a local branch, or a pin at a ref.** Attached: the spec is
  `branch.<name>.remote` + `branch.<name>.merge` when set, and *nothing* when not — a branch
  the user created by hand with no upstream has no base, and `ow rebase`/`pull` say so
  instead of falling back to a remembered base (`switch.py:293-311` loses its fallback).
  Detached: the ref the pin follows is stored **in git's per-worktree config** —
  `git config --worktree ow.ref origin/18.0`, in `<bare>/worktrees/<name>/config.worktree`,
  enabled by `extensions.worktreeConfig=true` (verified working in a bare-repo worktree).
  HEAD remains the truth; `ow.ref` is the pin's intent, so a hand-made `git switch --detach`
  degrades to a sha-pin and `ow status` can say the pin moved.
- **The repos of a workspace are discovered, not declared.** For each bare repo under
  `$XDG_DATA_HOME/ow/repos/`, the entries in `<bare>/worktrees/*/gitdir` whose worktree path
  lies under the workspace give the alias (the directory name). One readdir per bare repo,
  no git subprocess, no file to keep in sync. A worktree that vanished is a repo that
  vanished — visible in `ow status`, restorable with `ow init -r`.
- **The marker becomes the `.ow/` directory** (the lock file dies in §3, so it cannot be
  that). The directory holds nothing: no config, no lock, no state — `resolver.py:18`,
  `index.py:19`, `init.py:39`, `ls.py:19`, `rm.py:25`, `archive.py:22`, `dashboard.py:45` and
  the two literals in `prune.py`/`workspace_forms.py` all switch to it.
- **`utils/drift.py` dies**: with the spec read from git, a worktree cannot disagree with it.
  What remains is the pin case above, reported by `ow status`.
- **`ow rm`** keeps a backup, reduced to what is still worth keeping: an `alias: spec` list
  written to `$XDG_STATE_HOME/ow/backups/` (the config-file copy it takes today,
  `rm.py:143`, would copy a file that no longer exists). `ow mv`/`ow archive`/`unarchive`
  stop reading a config and re-render after the move, as they already do.

**Migration of an existing workspace** (the four real ones): read `.ow/config.toml` once —
attached specs are already in `branch.*`, detached ones write their `ow.ref` — then leave the
file on disk (ow deletes nothing) and never read it again. Nothing to re-create by hand, and
`ow init -r` remains available for anything the discovery cannot see.

**What this costs:** a detached pin whose `ow.ref` is absent (a hand-made detach) is only a
sha; a local branch with no upstream has no base; and `ow fetch` refreshes branches'
upstreams, not pins — `ow switch <ref>` is how a pin moves, and it fetches on demand
(`switch.py:_fetch_target`). All three are the same statement: git is the state, ow reads it.

## 8. Global `[vars]` become `[odoo]` options

**Today**: the global `[vars]` table is copied into each workspace's config at `ow init` and
read from there only (`init.py:194-196`, `docs/configuration.md`); editing the global table
afterwards affects new workspaces only. The real global config holds two keys (`http_port`,
`db_host`); the rest of the odoorc values are template defaults. Workspaces override
per-workspace (phone-service: `http_port = 8068`, `debug_args` with test tags and `-u`).

**Decision**: one typed table in the global config, read at render time by §3's generators —
no copy, no per-workspace override:

```toml
[odoo]
http_port = 8069
db_host = "localhost"
db_port = 5432
db_user = "odoo"
db_password = "odoo"
admin_passwd = "Password"
smtp_server = "localhost"
smtp_port = 25
# debug_args / debug_test_args default per profile (§2); set them to override.

[mise]
python = "3.12"        # clamped into the checkout's MIN/MAX_PY_VERSION (§2)
```

`[vars]` disappears. Migration: keys found under `[vars]` are read as fallbacks with a
one-time note, `[odoo]` wins, and the next write (TUI save) rewrites the file in the new
shape. Changing a port now affects **every** workspace at the next render, which is what a
global option should do; a workspace that needs to differ keeps the difference by editing the
generated file — with §3's rule, that means `owignore`ing it and owning it, and
phone-service's `http_port = 8068` is the first candidate.

Rejected: keeping per-workspace option overrides (that is `.ow/config.toml` again), and
keeping free-form `vars` (nothing documents them, nothing type-checks them, and the render
context's `{{ vars.x | default(...) }}` dance exists only because they are strings).

## 9. What survives of phase 1

| | |
|---|---|
| **Survives** | `is_odoo_main_repo` and the addon scan with `.local` and loose-addons-first (`templates.py:36-171`); `mise/conf.d/00-ow.toml` as the fragment location, with the legacy `mise.toml` warning; "ow never deletes a workspace file"; the "no Odoo checkout → no Odoo files" behaviour, now with a version behind it. |
| **Dies** | The output lock and its five states; the three axes (bundles, `common`/`odoo`/declared); `$XDG_CONFIG_HOME/ow/templates/` overrides; the `templates` field; `selectable_templates`, `effective_bundles`, `.j2` machinery, the second render pass. |
| **Changes** | `ow templates` → `ow files` (same listing, states now `same`/`differs`/`absent`/`ignored`, `--diff` keeps the exit code); `apply_templates` → `render_files`; the render context gains the profile and loses `vars`. |

## 10. Perimeter and order

Five steps, each shippable on its own, each ending with the test suite green and the docs
matching the code.

1. **Generation in the code + `owignore`.** `ow/utils/generate.py`, the file table, `ow files`
   (renamed), `owignore`, `pathspec` in and Jinja2 out, the lock gone. No user-visible
   behaviour change beyond the command name and the new config key; fixes the stale
   `docs/configuration.md` while touching the same ground.
2. **Version detection and the profile.** `release.py` parsing, the profile table, per-profile
   args, the Python bounds with the clamped `[mise] python`, out-of-window warning, re-render
   on `ow switch`. First step that changes what lands on disk, and it fixes the 18.0
   `--with-demo` bug.
3. **`[odoo]` / `[mise]` options.** Typed table, `[vars]` migration, TUI global-config screen,
   `docs/configuration.md` and `docs/services.md` updated.
4. **Specs in git, config gone.** `ow.ref` for pins, repos from worktree discovery, `.ow/` as
   the marker, `drift.py` gone, `ow apply` → `ow init` (idempotent) + `ow render`, migration
   of the four real workspaces, `ow rm`'s reduced backup. The riskiest step: it touches every
   command, so it comes after 1–3 have settled the file model it re-renders into.
5. **Sandboxes from the repo.** Mise tasks for `setup/sandboxing/`, the vendored wrappers and
   the `bwrap` bundle deleted, `docs/sandboxing.md` shrunk. Independent of 2–4; needs 1 for
   the fragment generator.

**Test impact, per step** (module names as they stand today): step 1 rewrites
`tests/utils/test_templates.py`, `test_templates_extended.py`, `test_templates_extended2.py`
and `tests/commands/test_templates_cmd.py` around the file table — the lock tests leave with
the lock — and adds `tests/commands/test_files.py` for the renamed command. Step 2 adds
`tests/utils/test_odoo_version.py` (release.py parsing, the three shapes, out-of-window) and
pins the per-profile editor args where the current defaults are asserted. Step 3 moves the
`[vars]` cases of `tests/utils/test_config.py` to `[odoo]` and touches
`tests/tui/test_global_config.py`. Step 4 is the wide one: `tests/utils/test_drift.py` is
deleted, `tests/commands/test_apply.py` becomes `test_render.py`, and every test that builds
a workspace through `.ow/config.toml` is reworked to build one through worktrees —
`tests/utils/test_resolver.py`, `test_index.py`, and
`tests/commands/test_{switch,init,init_extended,mv,archive,rm,prune,ls,misc_extended}.py`.
Step 5 touches only the fragment and sandbox expectations.

## Open questions

1. **The window.** Read as master + the three stable majors (today 20.0, 19.0, 18.0) + the
   saas branches of those majors, 17.0 and below out. Confirm — one line in §1 changes if the
   window is narrower or wider.
2. **Out-of-window policy**: warning + no Odoo files (§1), rather than best-effort with the
   nearest profile. Confirm.
3. **The `local` bundle.** `~/.config/ow/templates/local/` is yours: a dev addon
   (`dev_module`) and a script, dropped into `<ws>/.local/` of every workspace that declares
   `local` (phone-service does). It dies with the bundles. Existing workspaces keep their
   files — `.local` is scanned and reaches `addons_path` — but new workspaces would not get
   them. Recommendation: **cut it**; if you want it back, the smallest honest form is
   "copy `$XDG_CONFIG_HOME/ow/local/` into `<ws>/.local/` when it exists", one rule, no
   declaration. Your call.
4. **`ow render`'s name and existence.** The alternative — render implicitly on every command
   that resolves a workspace — was rejected (§6) because it writes during `ow status`. If you
   prefer "ow's files are always current" over "read-only commands stay read-only", it is a
   one-line change.
5. **The lock's removal** (§3) is the decision most likely to bite: a hand-edited managed
   file is overwritten at the next render, and the opt-out is `owignore`. Say so if you want
   a milder rule (e.g. "never overwrite a file that differs from what ow last wrote", which is
   the lock again, or "write if absent, never update", which freezes ow's output).
