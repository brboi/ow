# ow

A CLI that keeps several git repos side by side as worktrees of shared bare repos, grouped into
workspaces, with everything beyond that core (Odoo, mise, editors) contributed by plugins.

## Language

### Repos and workspaces

**Workspace**:
A directory holding one worktree per repo it uses, named after the directory itself; the worktrees present are what define its repos.
_Avoid_: project, environment

**Repo**:
A named source of code, backed by one bare repo shared by every workspace on the machine.
_Avoid_: alias (as a noun for the thing), clone

**Bare**:
The `<name>.git` directory under `$XDG_DATA_HOME/ow/repos` holding a repo's refs and objects; every worktree of that repo is a checkout of it.
_Avoid_: mirror, source repo

**Remote**:
A named URL a repo fetches from or pushes to, such as `origin` or `dev`.

**Known repo**:
A repo a plugin can add by name alone because it knows where it lives, such as `enterprise`.

**Stray checkout**:
A direct subdirectory of a workspace holding a `.git` that ow does not manage: a second worktree of the same bare, a plain clone, or a worktree of a repo outside the workspace. `ow rm` lists them and refuses.
_Avoid_: extra repo, unmanaged clone

**Adopted bare**:
A bare repo ow 2.x left without reflogs, `remote.origin.tagOpt` and `origin/HEAD` that 3.0 relies on; `ow init` repairs it in place when it finds one.
_Avoid_: legacy repo, migrated repo

### Branches

**Base**:
The remote branch a branch is kept level with, such as `origin/18.0`, recorded per branch as `branch.<name>.ow-base` in the bare; each branch remembers its own.
_Avoid_: target, version, branch spec

**Default base**:
The base a workspace keeps for a repo in its `[repos]` table, used when it creates a branch there or leaves it detached; a branch's recorded base wins over it.
_Avoid_: fallback branch, main branch

**Branch**:
The local branch a workspace works on, carrying the same name in every repo of the workspace.
_Avoid_: feature, branch spec

**Upstream**:
The remote branch collaborators share and push to for a branch, such as `dev/18.0-voip-boi`; distinct from its base.
_Avoid_: tracking branch, base

**Orphan commits**:
Commits reachable from a detached HEAD and from no branch nor remote-tracking ref; ow refuses to drop them without saying so.
_Avoid_: lost commits, dangling commits

### Commands

**Plan**:
The exact git commands a command will run in each repo, and the repos it refuses, shown before ow asks to proceed.
_Avoid_: preview, dry run

### Extension

**Plugin**:
A unit outside the core that contributes commands, settings, generated files, known repos and diagnostics, and reacts to what happens in a workspace.
_Avoid_: extension, module, template

**Generated file**:
A file a plugin contributes to a workspace and ow writes, rewritten only while nobody has edited it since ow last wrote it.
_Avoid_: rendered file, template output

**Setting**:
A named, typed value a plugin declares, such as `odoo.http_port`, whose effective value each workspace resolves from its default, then the user's global choice, then the workspace's own.
_Avoid_: option, override

**Refresh**:
Recomputing every generated file of a workspace from its current state and settings, then writing the ones ow still owns.
_Avoid_: render, regenerate

### Odoo

**Odoo core**:
The one repo of a workspace that holds Odoo's server itself, whatever it is named; a workspace with none, or with two, gets nothing from the Odoo plugin.
_Avoid_: core (alone, which means ow's own core), main repo, community
