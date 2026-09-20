# Migrating to ow 2.0 — the layout move

**This page is step 1 of moving a 1.x install to ow 3.0.** It converts the project-scoped layout
to the user-level one; it does not convert the config schema. Every workspace it produces is
still schema 1, and [Migrating to 3.0](migrating-to-3.0.md) — `ow render` per workspace — is
step 2. If you are already on a user-level 2.0 layout, skip straight there.

ow 1.x was project-scoped: one `ow.toml` at a project root, with `templates/`,
`services/`, `workspaces/` and `.bare-git-repos/` beside it. 2.0 is user-level.
There is no project root any more.

Nothing migrates itself. This is a one-time move you do by hand. It takes a few
minutes and it keeps your bare repos — no re-cloning.

The mechanical steps (config copy, bare repo move, worktree repair, workspace
config rename) are automated by a migration script:

```sh
python scripts/migrate-to-2.0.py "$OLD"               # dry-run: show the plan
python scripts/migrate-to-2.0.py "$OLD" --yes         # execute
python scripts/migrate-to-2.0.py "$OLD" --yes --render  # also run ow render per workspace
```

Pass `--render` to also run `ow render` on each workspace — that is step 2's schema migration,
plus registering the workspace in the index and writing its generated files. Template
customizations are not handled here at all: 3.0 removed the template system, and the schema
migration retires them instead — see [Migrating to 3.0](migrating-to-3.0.md#template-customizations).
The sections that follow describe what each step does, for context and for doing it by hand.

Set this once, and paste the rest as is:

```sh
OLD=~/src/odoo          # your old project root, the directory holding ow.toml
```

Paths below are the XDG defaults. ow reads `XDG_CONFIG_HOME`, `XDG_DATA_HOME`
and `XDG_STATE_HOME`; if you set any of them, substitute accordingly.

## What breaks

| 1.x | 2.0 |
|---|---|
| `$OLD/ow.toml` | `~/.config/ow/config.toml`, one per user |
| `$OLD/.bare-git-repos/` | `~/.local/share/ow/repos/` |
| `$OLD/templates/` | packaged inside ow; per-file overrides in `~/.config/ow/templates/` |
| `$OLD/services/` | packaged inside ow; no ow command reads it |
| `$OLD/workspaces/<name>/` | anywhere; an index at `~/.local/state/ow/workspaces` remembers where |
| `.ow/config` | `.ow/config.toml` |
| `OW_WORKSPACE=<name>` or a path | an absolute path only |
| project root | gone |

Until you migrate, ow points back here. It detects two things: an old
`ow.toml` at or above the current directory while no global config exists yet,
and a `.ow/config` with no `.ow/config.toml` beside it. Every command refuses to
run on either — except `ow ls`, which prints the same pointer and then lists
whatever it already knows, since it reads nothing but its own index.

## Migrate

### 1. Global config

Your old `ow.toml` holds `[vars]` and `[remotes]`. The new global config takes
exactly the same shape, so copy it verbatim.

```sh
mkdir -p ~/.config/ow
cp "$OLD/ow.toml" ~/.config/ow/config.toml
```

Carries over: every key under `[vars]`, and every entry under `[remotes]` with
its `url`, `pushurl` and `fetch`.

Does not carry over: nothing else was ever read from that file. What is gone is
the project root it used to define — the location of your bare repos, templates
and workspaces was derived from where `ow.toml` sat, and each of those is now
resolved on its own.

To start clean instead, skip the copy: ow runs on built-in defaults — the
community remote included — and creates no global config until you save one from
the dashboard. Run `ow init`, `ow render`, `ow status` or `ow rebase` **from
outside `$OLD`**. Inside `$OLD` the check above fires first — ow sees the old
`ow.toml` and stops, which is the one thing that would leave you going in
circles. `ow ls`, `ow prune` and `ow cd` need no configuration, so they never
create it either.

You can leave the old `ow.toml` in place. Once `~/.config/ow/config.toml`
exists, ow stops looking for it.

### 2. Bare repos — move them, do not copy

```sh
mkdir -p ~/.local/share/ow
mv "$OLD/.bare-git-repos" ~/.local/share/ow/repos
```

`mv`, not `cp`. These are several gigabytes; a copy leaves you with two of them,
and only one keeps receiving fetches. The layout inside is unchanged — one
`<alias>.git` directory per repo.

### 3. Repair the worktrees

Every worktree's `.git` file holds an absolute path to its bare repo, so moving
the bare repos breaks all of them:

```
fatal: not a git repository: (null)
```

git fixes this itself. Run it once per bare repo:

```sh
for repo in ~/.local/share/ow/repos/*.git; do
    git -C "$repo" worktree repair
done
```

Do this before running any ow command. Skipping it does not fail cleanly:
every ow operation that runs git inside a worktree — `ow switch`, `ow rebase`,
`ow pull`, `ow reset`, and the inspection behind `ow status` and `ow render` —
reads that broken `.git` file. `ow status` prints `community: (error)` and moves
on; a Git mutation can end in a `CalledProcessError: ... 'switch' ... returned
non-zero exit status 128` with `fatal: not a git repository: (null)` printed
above it. Neither is a bug report waiting to happen — the fix is the
`git worktree repair` above, run once per bare repo.

### 4. Rename each workspace config

```sh
for ws in "$OLD"/workspaces/*/; do
    [ -f "$ws/.ow/config" ] && mv "$ws/.ow/config" "$ws/.ow/config.toml"
done
```

The contents do not change: same `templates`, `repos` and `vars`. It is still a
schema-1 file — step 2 (`ow render`) is what converts it to schema 2.

Workspaces no longer have to sit under a `workspaces/` directory — they can
live anywhere. If you intend to move them, do it now, before the next step.

### 5. Let ow discover the workspaces

`ow ls`, and looking a workspace up by bare name, read a discovery index at
`~/.local/state/ow/workspaces`. It is a plain list of paths, not a database:
the `.ow/config.toml` on disk remains the only truth. The index self-heals —
every workspace ow successfully resolves is remembered, and entries that no
longer point at a workspace are dropped as the file is read.

Resolving a workspace registers it, so a single `ow render` per workspace registers it, migrates
its config to schema 2, and writes its generated files:

```sh
for ws in "$OLD"/workspaces/*/; do
    ow render "$ws"
done
ow ls
```

Until a workspace has been resolved once, `ow status <name>` cannot find it by
name. Pass a path (`ow status ./name`) or `cd` into it.

## Templates

2.0 had a template system: bundles, `$OLD/templates/` copies, `ow templates --take`, and a
per-file override tree under `~/.config/ow/templates/`. **ow 3.0 removed all of it.** There is
nothing to take, no baseline, and no `template-base` command; `ow files` replaces `ow templates`.

Your old `$OLD/templates/` tree is not copied anywhere by this step, and 3.0 does not read it
either. What step 2 inventories is a workspace's own `.ow/templates/` copy — the tree 1.x
materialized inside each workspace — and your 2.0-era `~/.config/ow/templates/` overrides. Each
legacy source it finds is classified as stock or customized; the generated files you customized
are then retired and left as `yours`, and nothing is deleted. If your 1.x customization lived only
in `$OLD/templates/`, copy it into the workspace's `.ow/templates/` (or into
`~/.config/ow/templates/`) before running step 2, or review the output by hand afterwards with
`ow files --diff`. See
[Migrating to 3.0 § Template customizations](migrating-to-3.0.md#template-customizations).

## Services

3.0 writes `~/.config/ow/services/compose.yml` as one JSON document, refreshed by a render that
found a supported Odoo core; `ow render` is the command that does it on its own. The generated
mise fragment exports `COMPOSE_FILE`, so `docker compose up` works from inside any workspace. If
you were running the old stack, your existing copy still works — or delete it and run
`ow render` to write a fresh one. See [Services](services.md).

## `ow rebase` changed

`ow rebase` was rewritten. Three differences will catch anyone with muscle
memory for the old one.

**The prompt defaults to no.** It used to be `Proceed? [Y/n]`, where anything
other than `n` — a bare Enter included — went ahead. It is now
`Proceed? [y/N]`: only `y` or `yes` proceed, and end-of-input aborts. Nothing
happens unless you say so. Pass `-y` in scripts.

**It is idempotent.** Running it twice with nothing changed in between does
nothing the second time. No commit is rewritten.

**It has flags now** — the old one had none:

| Flag | Effect |
|---|---|
| `--only a,b` | restrict the whole run to those repo aliases |
| `--autostash` | stash and restore uncommitted changes around each rebase; without it a dirty worktree is skipped and the run exits non-zero |
| `--dry-run` | fetch, print the plan, touch no worktree |
| `-y`, `--yes` | skip the confirmation prompt |

Nothing is ever pushed, then or now.

## Command renames

The middle column is the name 2.0 gave it; the right one is what ow 3.0 ships. `ow apply` and
`ow templates` existed in 2.0 and are **gone in 3.0** — see
[Migrating to 3.0](migrating-to-3.0.md#commands-that-no-longer-exist) for what replaced them.

| 1.x | 2.0 | 3.0 |
|---|---|---|
| `ow init` (set up a project) | gone — the global config bootstraps itself | `ow init` creates or repairs a workspace; the global config is written only when you save one |
| `ow create` | `ow init` | `ow init` |
| `ow update` | `ow apply` | `ow render` writes the generated files; `ow init` repairs a workspace |
| `ow status` | unchanged | unchanged |
| `ow rebase` | unchanged in name; see above | unchanged |
| `ow prune` | unchanged | unchanged |
| — | `ow ls` | unchanged |
| — | `ow templates` | `ow files` — list the managed files and their state, or diff them |

`ow -v` is gone: the version flag is `--version` or `-V`. `-v` almost everywhere
means `--verbose`, and ow shells out to git constantly, so the short spelling is
left free for that.

`ow init` behaves like `git init`: it creates a workspace in the current
directory, or in `./NAME` if you pass a name — and it is re-runnable, repairing
one already there. The old `-n/--name` option is gone; the name is the argument.

`-c/--configuration` is unchanged. `-t/--template` and `-r/--repo` changed shape:

| Flag | 1.x | 2.0 | 3.0 |
|---|---|---|---|
| `-t/--template` | one `-t`, a space-separated list: `-t common vscode` | one template per `-t`, repeated: `-t common -t vscode` | removed — the generated outputs are fixed |
| `-r/--repo` | one `-r`, two words: `-r community master..x` | one `-r ALIAS:SPEC`, repeated for more: `-r community:master..x` | unchanged |

Old habits fail loudly rather than silently doing the wrong thing:
`ow init -r community master..x` errors with
`--repo expects ALIAS:SPEC (got 'community')`, and on 3.0 `ow init -t vscode`
is an unknown option.

## `OW_WORKSPACE`

It takes exactly one form now: an absolute path to a workspace directory. A
bare workspace name, which 1.x accepted and resolved under
`<root>/workspaces/`, is rejected:

```
Error: OW_WORKSPACE='canary' is not an absolute path
```

`~` is not expanded either. Export the full path — or export nothing at all:
with `OW_WORKSPACE` unset, ow walks up from the current directory to find the
workspace, which is what you want most of the time.

Your existing `mise.toml` files were generated by 1.x and export a name:

```toml
[env]
OW_WORKSPACE = "canary"
```

so every ow command run inside a mise-activated workspace fails on it. 3.0
generates `mise/conf.d/00-ow.toml` instead, exporting mise's `{{config_root}}` —
the workspace directory — so the `ow render` from step 5 writes the correct
value. Reopen your shell afterwards so mise drops the stale value.

A root `mise.toml` is not rewritten by any ow version — it might hold things you
added by hand — and mise gives it precedence over the fragment, so it silently
shadows the generated file. 3.0 recognises its own `OW_WORKSPACE` marker and
warns about it; delete the file (or move your own keys into `mise.local.toml`,
which ow never touches) and re-run `ow render`. See
[Migrating to 3.0 § The legacy root `mise.toml`](migrating-to-3.0.md#the-legacy-root-misetoml).

## What's left in `$OLD`

Once the above is done, `$OLD/mise.toml` and `$OLD/templates/` are dead —
nothing reads either any more — and safe to delete. `$OLD/ow.toml` is covered
above: keep it or delete it, ow no longer looks for it once the global config
exists.

`$OLD/workspaces/` is only dead once it's empty. Each subdirectory under it is
a real workspace, worktrees and all — moving it out from under `$OLD` is
optional, not required, and one you leave in place is still live and in use,
not inert. Only delete `$OLD/workspaces/` once every workspace under it has
been moved out or is one you no longer need.

## What changed in 2.0.0 (historical)

If you were already on ow 2.0-dev or 2.0.0-rc, these are the final 2.0.0 changes. Some of them
describe 2.0's template system, which 3.0 removed — see
[Migrating to 3.0](migrating-to-3.0.md) for the current behaviour.

- **`ow status` is offline by default.** It reads local refs without fetching. Pass `-f`/`--fetch`
  to fetch first, then show status.
- **`--only` removed from `apply` and `status`.** Kept on `rebase` (where selective rebase is a
  real workflow).
- **Config schemas versioned.** Both `config.toml` and `.ow/config.toml` start with `version = 1`.
  A file with a newer version is refused with an upgrade message. Existing files without the field
  are treated as version 1 — no migration needed.
- **Services compose file auto-rendered.** `ow init` and `ow apply` render `compose.yml` into
  `~/.config/ow/services/` with the container volume path baked in. `mise.toml` exports
  `COMPOSE_FILE`; `odoorc` sets `data_dir = <workspace>/.odoo`.
- **Template `StrictUndefined`.** Undefined variables in Jinja2 templates raise at render time
  instead of rendering empty. Use `{{ vars.key | default(fallback) }}` for optional values.
- **`ow apply` no longer back-fills global vars** into `.ow/config.toml`. The render merge still
  makes globals visible to templates; editing a global var now affects all workspaces.
- **Orphan file check.** `ow apply` lists files from template bundles no longer in your config,
  so you can remove stale IDE configs manually.
