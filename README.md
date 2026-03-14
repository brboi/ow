# ow - Odoo Workspaces

Manage your Odoo development workspaces using [Git worktrees](https://git-scm.com/docs/git-worktree) and [mise](https://mise.jdx.dev/).

Odoo Workspaces will generate workspaces folders that you can open in VSCode and Zed.

## Pre-requisites

You need to have properly configured the following tools:

- SSH: to access Odoo S.A. repositories (private repos require Odoo employee access)
- mise: will be used in the generated workspaces to manage Python dependencies and virtual environments
- Docker or Podman (optional but recommended): useful to run services like postgres, pgweb, mailpit

## Repo Structure

Here is the main structure:
```
.
├── .bare-git-repos/    # internal folder to store the bare git repositories
├── ow/                 # the main ow codebase
├── services/           # optional services containers (postgres, pgweb, mailpit...)
├── workspaces/         # generated workspaces folders
├── ow.toml.example     # example configuration file for ow
└── README.md           # this file
```

Workspace folders once generated will look like this:
```
.
└── workspaces/
    ├── opw-123456/         # this is your workspace name
    │   ├── .git            # git worktree file pointing to the corresponding bare git repository
    │   ├── .vscode/        # VSCode configuration: `settings.json`, `launch.json`
    │   ├── .zed/           # Zed configuration: `settings.json`, `debug.json`
    │   ├── community/      # the community codebase (git worktree)
    │   ├── enterprise/     # optional - the enterprise codebase (git worktree)
    │   ├── ngram-addons/   # optional - any other git worktree you want to add (e.g. ngram-addons)
    │   ├── mise.toml       # mise configuration (python dependencies, virtual env...)
    │   └── odools.toml     # (OdooLS)[https://github.com/odoo/odoo-ls] configuration
    └── other-workspace/    # this is another workspace with all the same structure as the previous one
```


## Usage

### Getting Started

```sh
$ cp ow.toml.example ow.toml # then configure ow.toml with your preferences
$ ow apply # apply the whole configuration and generate the workspaces folders
```

Check how the config looks like: [ow.toml.example](./ow.toml.example).

### Commands

| Command | Description |
|---------|-------------|
| `ow --help` or `ow {command} --help` | Shows the help message for `ow` or for a specific command. |
| `ow apply [name]` | Applies the configuration and generates the workspaces folders. If a name is provided, only the workspace with that name will be generated. |
| `ow status [name]` | Shows the status of the workspaces. If a name is provided, only the workspace with that name will be shown. |
| `ow create {name} {alias:spec} ... [vars.key=value ...]` | Creates a new workspace with the given name and branches specifications. This is a shortcut for creating a new workspace without having to edit the config file and run `ow apply`. The branches specifications use the same syntax as in the config file (e.g. `community:master`, `enterprise:master-opw-123456-ngram..master`). You can also pass per-workspace template variables inline (e.g. `vars.http_port=8080`). The new workspace configuration will be automatically saved to the `ow.toml` file. |
| `ow remove {name}` | Removes the workspace with the given name. This will not delete the corresponding bare git repository, so you can generate it again later if needed. |
| `ow rebase {name}` | Rebases the workspace with the given name. This will fetch the latest changes from the remote branches and rebase the worktree branches on top of them. This is a shortcut for running `git fetch` and `git switch --detach`/`git rebase` from each of the worktree folders. |

### Tab Completion

One-time setup (fish):
```sh
register-python-argcomplete --shell fish ow > ~/.config/fish/completions/ow.fish
```
Or for bash/zsh: `activate-global-python-argcomplete` (adds a hook to your shell profile).

## Recommended Workflow

### Key Rules & Constraints
- **One Branch, One Worktree:** Git prevents checking out the same local branch in two different folders. Use different local branch names (e.g., `master-a`, `master-b`) or **Detached HEAD** for running the same version in multiple workspaces.
- Each branch lives in its own directory — ban `git checkout` to switch context!
- All worktrees share the same bare repo, so fetching from one updates refs for all.
- Git worktrees cannot share the same branch, so you will want to use detached worktrees for the main branches (i.e. `master`, `19.0`, `saas-19.2`...) and create only your feature branches. This is why `ow` uses detached worktrees by default and only attaches a branch to the worktree when you have specified a feature branch using the double dots syntax in the config file.
- Git worktrees are not meant to be used as long-term branches, but rather as temporary contexts for development. Once a feature is merged, the corresponding workspace can be removed and the branch could also be deleted from the bare repository. Use `ow remove <workspace-name>` to remove the workspace folder. The branch will also be deleted.

### Workspace Creation

The easiest way to start a new feature is to create a new workspace with the `ow create` command and specify the branch you want to work on using the double dots syntax in the config file. For example, if you want to start a new feature on top of `master`, you can run:
> If you need to work on a community feature: `ow create <workspace-name> community:master..master-opw-123456-ngram enterprise:master`
> Or an enterprise feature: `ow create <workspace-name> community:master enterprise:master..master-opw-123456-ngram`
> Or both: `ow create <workspace-name> community:master..master-opw-123456-ngram enterprise:master..master-opw-123456-ngram`
> With a custom port: `ow create <workspace-name> community:master vars.http_port=8080`

Once the workspace is created, you will be able to open it like this:
```bash
$ code workspaces/<workspace-name>
# or if you are using Zed
$ zeditor workspaces/<workspace-name>
```

### Workspace Status Check

If you have the following workspace config:
```toml
[[workspace]]
name = "canary"
repo.community = "master..master-canary"
repo.enterprise = "master..master-canary"

[[workspace]]
name = "canary-can-fly"
repo.community = "master-canary..master-canary-can-fly"
repo.enterprise = "master-canary..master-canary-can-fly"

[[workspace]]
name = "fantastic-iap-service"
repo.community = "18.0"
repo.enterprise = "18.0"
repo.iap-apps = "18.0..18.0-fantastic-service-ngram"
```

The command `ow status` will check and give you the whole status. Output is color-coded in the terminal (bold cyan headers, green/yellow counts, etc.).
```bash
$ ow status
[canary]
    branches
        community:  dev/master-canary ↓0 ↑0 (origin/master ↓34 ↑0)
        enterprise: dev/master-canary ↓1 ↑1 (origin/master ↓12 ↑0)
    links
        runbot: master-canary

[canary-can-fly]
    branches
        community:  dev/master-canary-can-fly ↓0 ↑3 (dev/master-canary ↓1 ↑0)
        enterprise: master-canary-can-fly (local) (dev/master-canary ↓0 ↑0)
    links
        runbot: master-canary-can-fly

[fantastic-iap-service]
    branches
        community: origin/18.0 ↓27 ↑0 (DETACHED: a1b2c3d)
        enterprise: origin/18.0 ↓11 ↑0 (DETACHED: d9c8b7a)
        iap-apps:  origin/18.0-fantastic-service-ngram ↓0 ↑1 (origin/18.0 ↓27 ↑0)
    links
        runbot: fantastic-iap-service
```

Note that you can CTRL+Click on those parts to open your browser:
- the branch name e.g. `dev/master-canary` redirects to https://github.com/odoo-dev/odoo/tree/master-canary
- the runbot link e.g. `master-canary` redirects to https://runbot.odoo.com/runbot/bundle/master-canary
- the commit hash i.e. `a1b2c3d` redirects to https://github.com/odoo/odoo/commit/a1b2c3d82ca4e6332131c832eb2b9a2750c3279d

### Rebase Your Work

Since all worktrees share the same bare repository, you can fetch and rebase from any of the worktrees. For example, if you want to rebase your feature branch on top of the latest `master`, you can simply run `git fetch origin master` and `git rebase origin/master` from any of the worktrees. But if one of your workspace folder points to a detached worktree, you also have to `git switch --detach origin/master` from that folder location.

The easiest way to do this is to use the `ow rebase <workspace-name>` command that will do all the necessary git commands for you in the correct order. This is a shortcut for running `git fetch` and `git switch --detach`/`git rebase` from each of the worktree folders. Here is what it will do:

```bash
# For a DETACHED head, i.e. with `community:master` branch spec:
git fetch origin master
git switch --detach origin/master
# For an ATTACHED head, i.e. with `enterprise:master..anything` branch spec:
git fetch origin master
git rebase origin/master
# The remote is always `origin` unless you have specified
# another one: i.e. `community:dev/master-phoenix..master-phoenix-improvement`
git fetch dev master-phoenix
git rebase dev/master-phoenix
```

### Push Your Work

From each of your workspace attached worktree folders, you can create an upstream branch like this:
```bash
git push -u <remote> HEAD
```

`ow` does not provide a shortcut command for this since you usually want to have more control over the branch name and the remote name.

### Remove Your Work When It Is Done

Once your feature is merged, you can remove the workspace folder using the `ow remove <workspace-name>` command. This will not delete the corresponding bare git repository, only worktree and branch references.
