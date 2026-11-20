# ow

A CLI that keeps several git repos side by side as worktrees of shared bare repos, grouped into
workspaces. The core is plain git: Odoo, GitHub and Runbot knowledge lives in plugins, along with
mise and editor support.

## Language

### Repos, workspaces and stacks

**Workspace**:
A directory initialized with `ow init` and tracked in the workspace index. A mono-stack workspace holds worktrees directly at its root; a multi-stack workspace groups named **stacks** as direct subdirectories with coordinated ports and a generated `<workspace>.code-workspace`.
_Avoid_: project, environment, task

**Stack**:
A named subdirectory inside a multi-stack workspace (e.g. `odoo/`, `iap/`, `voip/`) carrying `[workspace] stack = true` in its `.ow/config.toml`. Each stack owns its own worktrees, ports and editor config, plus an `odoorc` when it holds an Odoo core; address it from anywhere as `<workspace>/<stack>` or locally as `<stack>`.
_Avoid_: sub-workspace, task

**Promotion**:
Turning a mono-stack workspace into a multi-stack one when `ow add` or `ow init` gets a named spec (`<name>=<spec>`): the existing worktrees move into a stack subdirectory whose name ow infers unless `--as NAME` gives it.
_Avoid_: upgrade, migration, conversion

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

**Companion repo**:
A repo a plugin adds to a spec's repos without the user naming it, such as the Odoo core next to an addon PR, detached on its base; the Runbot plugin may pin it to the commit Runbot built against.
_Avoid_: dependency, implicit repo

**Stray checkout**:
A direct subdirectory of a workspace holding a `.git` that ow does not manage: a second worktree of the same bare, a plain clone, or a worktree of a repo outside the workspace. `ow rm` lists them and refuses.
_Avoid_: extra repo, unmanaged clone

**Adopted bare**:
A bare repo ow 2.x left without reflogs, `remote.origin.tagOpt` and `origin/HEAD` that 3.0 relies on; `ow repo add` repairs it in place when it finds one.
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

**Spec**:
The grammar `ow init`, `ow add` and `ow run --tmp` accept: `[<name>=]<item>[,<item>...]`, where `<name>` names the stack and each item is a PR (`enterprise#12345`), a Runbot URL, `<repo>@<ref>`, `@<base>` (e.g. `@18.0`) or a bare `<repo>`. Plugins resolve PRs and URLs.
_Avoid_: target, target DSL

**Sister PR**:
An open PR with the same branch name as the spec's PR on another known repo of the same GitHub organization, which the GitHub plugin adds with it.
_Avoid_: linked PR, related PR

**Plan**:
The exact git commands a command will run in each repo, and the repos it refuses, shown before ow asks to proceed.
_Avoid_: preview, dry run

**Runner**:
The command `ow run` executes for a workspace or stack, supplied by a plugin's `ow_runner` hook, such as `odoo-bin -c odoorc` from the Odoo plugin; a stack without one is skipped.
_Avoid_: task, launcher

### Extension

**Plugin**:
A unit outside the core that contributes settings, generated files, spec resolution, status, runners, known repos and diagnostics, and reacts to what happens in a workspace.
_Avoid_: extension, module, template

**Generated file**:
A file a plugin contributes to a workspace and ow writes, rewritten only while nobody has edited it since ow last wrote it.
_Avoid_: rendered file, template output

**Setting**:
A named, typed value a plugin declares, such as `odoo.http_port`, whose effective value each workspace resolves from its default, then the user's global choice, then the workspace's own.
_Avoid_: option, override

**Refresh**:
Recomputing every generated file of a workspace (and `<workspace>.code-workspace` for a multi-stack workspace) from its current state and settings, then writing the ones ow still owns.
_Avoid_: render, regenerate

### Odoo

**Odoo core**:
The one repo of a workspace or stack that holds Odoo's server itself, whatever it is named; a workspace with none, or with two, gets nothing from the Odoo plugin.
_Avoid_: core (alone, which means ow's own core), main repo, community

**Series**:
An Odoo release line named by its branch: `18.0`, `saas-18.2` or `master`. Series branches are checked out detached, and a feature branch such as `18.0-fix` takes `origin/18.0` as its base.
_Avoid_: version, release branch

