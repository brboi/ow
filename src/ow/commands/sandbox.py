"""`ow sandbox` (experimental) — run a command or AI agent harness inside a bubblewrap sandbox."""

from __future__ import annotations

import os
import pwd
import shlex
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.commands._runtime import default_shell, open_target, run_foreground
from ow.commands._shared import prepare_services
from ow.core import trust
from ow.core.plugins import SandboxMounts
from ow.core.refresh import read_lock
from ow.core.session import global_settings
from ow.core.workspace import RepoLocation, Workspace, resolve_target_root
from ow.errors import OwError, UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


@dataclass(frozen=True)
class HarnessProfile:
    """Minimal allowlisted mounts and env vars for a known AI agent harness."""

    name: str
    binaries: tuple[str, ...]
    rw_dirs: tuple[str, ...] = ()
    rw_files: tuple[str, ...] = ()
    ro_paths: tuple[str, ...] = ()
    env_exact: tuple[str, ...] = ()
    env_prefixes: tuple[str, ...] = ()


HARNESS_PROFILES: Mapping[str, HarnessProfile] = {
    "claude": HarnessProfile(
        name="claude",
        binaries=("claude",),
        rw_dirs=(
            "~/.claude",
            "~/.cache/claude",
            "~/.cache/claude-cli-nodejs",
            "~/.local/state/claude",
        ),
        rw_files=("~/.claude.json",),
        ro_paths=(
            "~/.local/share/claude",
            "~/.local/bin/claude",
        ),
        env_exact=(
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_MODEL",
            "ANTHROPIC_SMALL_FAST_MODEL",
        ),
        env_prefixes=("CLAUDE_CODE_",),
    ),
    "opencode": HarnessProfile(
        name="opencode",
        binaries=("opencode",),
        rw_dirs=(
            "~/.config/opencode",
            "~/.local/share/opencode",
            "~/.cache/opencode",
            "~/.local/state/opencode",
        ),
        ro_paths=("~/.opencode",),
        env_exact=(
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "OPENROUTER_API_KEY",
            "AZURE_OPENAI_API_KEY",
            "GROQ_API_KEY",
            "XAI_API_KEY",
            "DEEPSEEK_API_KEY",
        ),
        env_prefixes=("OPENCODE_",),
    ),
    "codex": HarnessProfile(
        name="codex",
        binaries=("codex",),
        rw_dirs=(
            "~/.codex",
            "~/.cache/codex",
        ),
        env_exact=(
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_ORG_ID",
            "OPENAI_PROJECT",
        ),
        env_prefixes=("CODEX_",),
    ),
    "pi": HarnessProfile(
        name="pi",
        binaries=("pi",),
        rw_dirs=(
            "~/.pi",
            "~/.config/pi",
            "~/.cache/pi",
        ),
        ro_paths=(
            "~/.local/lib/node_modules/@earendil-works",
            "~/.local/bin/pi",
        ),
        env_exact=(
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "OPENROUTER_API_KEY",
        ),
        env_prefixes=("PI_",),
    ),
    "omp": HarnessProfile(
        name="omp",
        binaries=("omp",),
        rw_dirs=(
            "~/.omp",
            "~/.config/omp",
            "~/.cache/omp",
        ),
        ro_paths=("~/.local/bin/omp",),
        env_exact=(
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "OPENROUTER_API_KEY",
        ),
        env_prefixes=("OMP_",),
    ),
}

_BINARY_TO_PROFILE: dict[str, HarnessProfile] = {
    binary: profile
    for profile in HARNESS_PROFILES.values()
    for binary in profile.binaries
}

# Host env vars forwarded unconditionally when set (non-secret terminal/locale/identity settings).
_SAFE_ENV_EXACT: tuple[str, ...] = (
    "TERM",
    "COLORTERM",
    "NO_COLOR",
    "FORCE_COLOR",
    "CLICOLOR",
    "CLICOLOR_FORCE",
    "LANG",
    "LANGUAGE",
    "TZ",
    "EDITOR",
    "VISUAL",
    "PAGER",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
)

_SAFE_ENV_PREFIXES: tuple[str, ...] = ("LC_",)

# CA bundle variables forwarded only when pointing inside system directories.
_SYSTEM_CA_VARS: tuple[str, ...] = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
)

_GIT_CONFIG_FILES: tuple[str, ...] = (
    ".gitconfig",
    ".config/git/config",
    ".config/git/ignore",
    ".config/git/attributes",
)

_SECRET_VAR_TOKENS: tuple[str, ...] = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
)

# Too short to match as a substring (`GIT_AUTHOR_NAME` is not a secret): only whole `_`-separated words.
_SECRET_VAR_WORDS: tuple[str, ...] = ("AUTH", "AUTHORIZATION")

# Files git reads from a worktree's admin dir that must not change under a session.
_WORKTREE_PINNED_FILES: tuple[str, ...] = ("commondir", "gitdir")

EXPERIMENTAL_NOTICE = "# experimental: `ow sandbox` may change or break during 3.x; read the docs before relying on it"

_DISABLED = "`ow sandbox` is disabled for this workspace (sandbox.enabled = false)"


@dataclass(frozen=True)
class SandboxPlan:
    """Resolved bubblewrap invocation, mounts, environment, and dirs to ensure before launch."""

    target_root: Path
    chdir: Path
    profile: str | None
    command: tuple[str, ...]
    bwrap_argv: tuple[str, ...]
    env: Mapping[str, str]
    ensure_dirs: tuple[Path, ...]


def _is_secret_var(name: str) -> bool:
    """Whether `name` looks like it holds a secret: a secret-ish substring, or `AUTH` as a whole word."""
    upper = name.upper()
    return any(token in upper for token in _SECRET_VAR_TOKENS) or any(
        word in _SECRET_VAR_WORDS for word in upper.split("_")
    )


def _locked_files(workspaces: Sequence[Workspace], root: Path) -> list[Path]:
    """Regular files under `root` that the rendered lock of `workspaces` records and that exist on disk."""
    lock_roots = {ws.root.resolve().parent if ws.is_stack else ws.root.resolve() for ws in workspaces}
    files: list[Path] = []
    for lock_root in sorted(lock_roots):
        for rel in read_lock(lock_root):
            path = lock_root / rel
            # A key with `..` or a symlinked component resolves elsewhere: never mount what it escapes to.
            if path.resolve() == path and path.is_file() and path.is_relative_to(root) and path not in files:
                files.append(path)
    return files


def _trusted_plugin_files(locations: Sequence[RepoLocation], root: Path) -> list[Path]:
    """In-repo plugin files of `locations` that `ow trust` approved, under `root`."""
    found = trust.discover_repo_plugins([(loc.name, loc.path) for loc in locations])
    return [item.path for item in found if item.path.is_relative_to(root) and trust.is_trusted(item.path)]


def _host_home(host_env: Mapping[str, str]) -> Path:
    raw = host_env.get("HOME", "").strip()
    if raw:
        return Path(raw).resolve()
    return Path.home().resolve()


def _host_user(host_env: Mapping[str, str]) -> str:
    user = host_env.get("USER") or host_env.get("LOGNAME")
    if user:
        return user
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return "user"


def _expand_user_path(raw: str, home: Path, base: Path) -> Path:
    """Expand leading `~` against `home` and resolve relative paths against `base`."""
    if raw == "~":
        return home
    if raw.startswith("~/"):
        return home / raw[2:]
    p = Path(raw)
    if not p.is_absolute():
        return (base / p).resolve()
    return p


def resolve_profile(
    command: Sequence[str],
    *,
    cli_profile: str | None = None,
    config_profile: str | None = None,
) -> HarnessProfile | None:
    """Resolve the active harness profile from CLI override, config default, or command basename."""
    raw = cli_profile if cli_profile is not None else config_profile
    if raw is not None:
        normalized = raw.strip().lower()
        if normalized in ("", "auto"):
            pass
        elif normalized == "none":
            return None
        elif normalized in HARNESS_PROFILES:
            return HARNESS_PROFILES[normalized]
        else:
            valid = ", ".join(["auto", "none", *HARNESS_PROFILES])
            raise UsageError(f"unknown sandbox profile {raw!r}; expected one of: {valid}")

    if not command:
        return None
    binary = Path(command[0]).name
    return _BINARY_TO_PROFILE.get(binary)


def _merge_plugin_mounts(
    workspaces: Sequence[Workspace], plugins: Plugins
) -> tuple[tuple[Path, ...], tuple[Path, ...], dict[str, str]]:
    """Collect `ow_sandbox_mounts` across all stacks; keep env keys that agree across stacks."""
    ro_paths: list[Path] = []
    rw_paths: list[Path] = []
    env_values: dict[str, set[str]] = {}

    for ws in workspaces:
        for _name, answer in plugins.call("ow_sandbox_mounts", ws=ws):
            if not isinstance(answer, SandboxMounts):
                continue
            for p in answer.ro_paths:
                if p not in ro_paths:
                    ro_paths.append(p)
            for p in answer.rw_paths:
                if p not in rw_paths:
                    rw_paths.append(p)
            for k, v in answer.env.items():
                env_values.setdefault(k, set()).add(v)

    merged_env = {
        k: next(iter(vals))
        for k, vals in env_values.items()
        if len(vals) == 1
    }
    return tuple(ro_paths), tuple(rw_paths), merged_env


def _is_already_mounted(path: Path, seen: set[tuple[str, str]]) -> bool:
    """Whether `path` or one of its ancestors is already mounted RO or RW in `seen`."""
    current = path
    while True:
        s = str(current)
        if ("ro", s) in seen or ("rw", s) in seen:
            return True
        if current.parent == current:
            return False
        current = current.parent


def _append_ro_path(
    args: list[str],
    path: Path,
    seen: set[tuple[str, str]],
) -> None:
    """Append a read-only mount (preserving symlinks by binding the target + `--symlink`)."""
    if not path.exists():
        return
    if path.is_symlink():
        resolved = path.resolve()
        if resolved.exists() and not _is_already_mounted(resolved, seen):
            res_key = ("ro", str(resolved))
            seen.add(res_key)
            args.extend(["--ro-bind", str(resolved), str(resolved)])
        link_target = os.readlink(path)
        sym_key = ("symlink", str(path))
        if sym_key not in seen:
            seen.add(sym_key)
            args.extend(["--symlink", link_target, str(path)])
        return

    key = ("ro", str(path))
    if key not in seen:
        seen.add(key)
        args.extend(["--ro-bind", str(path), str(path)])


def _append_rw_path(
    args: list[str],
    path: Path,
    seen: set[tuple[str, str]],
    *,
    allow_missing: bool = False,
) -> None:
    if not allow_missing and not path.exists():
        return
    key = ("rw", str(path))
    if key not in seen:
        seen.add(key)
        args.extend(["--bind", str(path), str(path)])


def _append_system_mounts(args: list[str]) -> None:
    """Mount Linux system directories read-only (distro-agnostic merged-/non-merged usr)."""
    if Path("/usr").exists():
        args.extend(["--ro-bind", "/usr", "/usr"])

    for entry in ("/bin", "/sbin", "/lib", "/lib64"):
        p = Path(entry)
        if p.is_symlink():
            args.extend(["--symlink", os.readlink(p), entry])
        elif p.is_dir():
            args.extend(["--ro-bind", entry, entry])

    if Path("/etc").is_dir():
        args.extend(["--ro-bind", "/etc", "/etc"])

    # If /etc/resolv.conf symlinks into /run/... (systemd-resolved, NetworkManager, resolvconf),
    # mount its parent directory RO so DNS resolution works inside the sandbox.
    resolv = Path("/etc/resolv.conf")
    if resolv.is_symlink():
        try:
            real_resolv = resolv.resolve()
        except OSError:
            real_resolv = None
        if real_resolv is not None and real_resolv.exists() and real_resolv.is_relative_to("/run"):
            parent = real_resolv.parent
            args.extend(["--ro-bind", str(parent), str(parent)])

    for optional_ro in ("/opt", "/nix"):
        if Path(optional_ro).is_dir():
            args.extend(["--ro-bind", optional_ro, optional_ro])


def _build_env(
    *,
    target_name: str,
    home: Path,
    shell: str,
    host_env: Mapping[str, str],
    plugin_env: Mapping[str, str],
    profile: HarnessProfile | None,
    pass_env: Sequence[str],
) -> dict[str, str]:
    """Construct the strict allowlisted environment for the sandboxed process."""
    user = _host_user(host_env)
    env: dict[str, str] = {
        "HOME": str(home),
        "USER": user,
        "LOGNAME": user,
        "PATH": host_env.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
        "SHELL": shell,
        "TMPDIR": "/tmp",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "OW_SANDBOX": "1",
        "OW_WORKSPACE": target_name,
    }

    for key in _SAFE_ENV_EXACT:
        if key in host_env:
            env[key] = host_env[key]
    for key, value in host_env.items():
        if key.startswith(_SAFE_ENV_PREFIXES):
            env[key] = value

    for key in _SYSTEM_CA_VARS:
        val = host_env.get(key)
        if val and (val.startswith("/etc/") or val.startswith("/usr/")):
            env[key] = val

    env.update(plugin_env)

    if profile is not None:
        for key in profile.env_exact:
            if key in host_env:
                env[key] = host_env[key]
        if profile.env_prefixes:
            for key, value in host_env.items():
                if key.startswith(profile.env_prefixes):
                    env[key] = value

    for key in pass_env:
        name = key.strip()
        if not name:
            continue
        if name in host_env:
            env[name] = host_env[name]

    return env


def _collect_str_list(settings_list: Sequence[Mapping[str, object]], key: str) -> list[str]:
    """Collect deduplicated string items for list setting `key` across layers/stacks."""
    items: list[str] = []
    for settings in settings_list:
        raw = settings.get(key)
        if isinstance(raw, (list, tuple)):
            for entry in raw:
                if isinstance(entry, str) and entry.strip() and entry not in items:
                    items.append(entry)
    return items


def build_sandbox_plan(
    *,
    target_root: Path,
    target_name: str,
    workspaces: Sequence[Workspace],
    locations: Sequence[RepoLocation],
    plugins: Plugins,
    command: Sequence[str],
    cli_profile: str | None = None,
    cli_ro: Sequence[str] = (),
    cli_rw: Sequence[str] = (),
    cli_env: Sequence[str] = (),
    settings_layers: Sequence[Mapping[str, object]] = (),
    host_env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    bwrap_bin: str = "bwrap",
) -> SandboxPlan:
    """Build the complete `bwrap` command line and environment (pure; performs no mutations)."""
    env_source = os.environ if host_env is None else host_env
    home = _host_home(env_source)
    resolved_root = target_root.resolve()
    base_cwd = (cwd or resolved_root).resolve()
    chdir = base_cwd if base_cwd.is_relative_to(resolved_root) else resolved_root

    shell = default_shell(env_source)
    effective_cmd = tuple(command) if command else (shell,)

    config_profile: str | None = None
    for settings in settings_layers:
        val = settings.get("sandbox.profile")
        if isinstance(val, str) and val.strip():
            config_profile = val
            break

    profile = resolve_profile(
        command,  # Auto-detect only from an explicit user command, not fallback shell.
        cli_profile=cli_profile,
        config_profile=config_profile,
    )

    plugin_ro, plugin_rw, plugin_env = _merge_plugin_mounts(workspaces, plugins)

    config_pass_env = _collect_str_list(settings_layers, "sandbox.pass_env")
    pass_env = [*config_pass_env, *cli_env]

    sandbox_env = _build_env(
        target_name=target_name,
        home=home,
        shell=shell,
        host_env=env_source,
        plugin_env=plugin_env,
        profile=profile,
        pass_env=pass_env,
    )

    args: list[str] = [bwrap_bin]
    ensure_dirs: list[Path] = []
    seen_before_ws: set[tuple[str, str]] = set()

    # 1. System base (RO)
    _append_system_mounts(args)

    # 2. Virtual filesystems & empty $HOME allowlist
    args.extend(["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"])
    args.extend(["--tmpfs", str(home)])

    # 3. Git config files in $HOME (RO; credentials files are intentionally excluded)
    for rel in _GIT_CONFIG_FILES:
        _append_ro_path(args, home / rel, seen_before_ws)

    # 4. Plugin contributions (mise, odoo shared venvs, etc.)
    for p in plugin_ro:
        _append_ro_path(args, p, seen_before_ws)
    for p in plugin_rw:
        _append_rw_path(args, p, seen_before_ws)

    # 5. Harness profile mounts: only paths that exist on the host. Nothing is created there: $HOME is a
    #    tmpfs, so a harness creates whatever state it needs itself (it is lost when the session ends).
    if profile is not None:
        for raw in profile.ro_paths:
            _append_ro_path(args, _expand_user_path(raw, home, resolved_root), seen_before_ws)
        for raw in (*profile.rw_dirs, *profile.rw_files):
            _append_rw_path(args, _expand_user_path(raw, home, resolved_root), seen_before_ws)

    # Auto-mount the resolved executable if it lives inside $HOME (e.g. ~/.local/bin/<tool>).
    cmd_bin = shutil.which(effective_cmd[0], path=env_source.get("PATH"))
    if cmd_bin:
        cmd_path = Path(cmd_bin)
        if cmd_path.is_relative_to(home) or cmd_path.resolve().is_relative_to(home):
            _append_ro_path(args, cmd_path, seen_before_ws)

    # 6. Workspace root (RW) + RO overlays: `.ow/`, `<repo>/.git`, every file ow generated
    #    (rendered lock) and the trusted in-repo plugin files, so a session cannot rewrite what
    #    ow or the user's tools will run later.
    args.extend(["--bind", str(resolved_root), str(resolved_root)])

    root_ow = resolved_root / ".ow"
    if root_ow.exists():
        args.extend(["--ro-bind", str(root_ow), str(root_ow)])

    for loc in locations:
        git_file = loc.path.resolve() / ".git"
        if git_file.exists():
            args.extend(["--ro-bind", str(git_file), str(git_file)])

    protected: set[tuple[str, str]] = set()
    for protected_file in (*_locked_files(workspaces, resolved_root), *_trusted_plugin_files(locations, resolved_root)):
        _append_ro_path(args, protected_file, protected)

    # 7. Partial-RW shared bare repos: bare root, config, hooks, and sibling worktrees stay RO;
    #    objects/, refs/, logs/, and this workspace's worktrees/<id>/ are RW, except that
    #    worktrees/<id>/{commondir,gitdir} are RO again.
    bare_worktrees: dict[Path, list[str]] = {}
    for loc in locations:
        bare = loc.bare.resolve()
        bare_worktrees.setdefault(bare, [])
        if loc.worktree_id not in bare_worktrees[bare]:
            bare_worktrees[bare].append(loc.worktree_id)

    for bare, wt_ids in bare_worktrees.items():
        if not bare.is_dir():
            continue
        args.extend(["--ro-bind", str(bare), str(bare)])
        for sub in ("objects", "refs"):
            sub_path = bare / sub
            if sub_path.is_dir():
                args.extend(["--bind", str(sub_path), str(sub_path)])
        logs_dir = bare / "logs"
        if not logs_dir.exists():
            ensure_dirs.append(logs_dir)
        args.extend(["--bind", str(logs_dir), str(logs_dir)])
        for wt_id in wt_ids:
            wt_dir = bare / "worktrees" / wt_id
            if not wt_dir.is_dir():
                continue
            args.extend(["--bind", str(wt_dir), str(wt_dir)])
            # `commondir` and `gitdir` decide which repository and checkout git resolves: they stay RO.
            for name in _WORKTREE_PINNED_FILES:
                pinned = wt_dir / name
                if pinned.is_file():
                    args.extend(["--ro-bind", str(pinned), str(pinned)])

    # 8. User-configured and CLI extra mounts (applied last so explicit overrides win)
    seen_extra: set[tuple[str, str]] = set()
    for raw in _collect_str_list(settings_layers, "sandbox.ro_paths"):
        p = _expand_user_path(raw, home, resolved_root)
        if not p.exists():
            term.warn(f"sandbox.ro_paths: skipping missing path {p}")
            continue
        _append_ro_path(args, p, seen_extra)

    for raw in _collect_str_list(settings_layers, "sandbox.rw_paths"):
        p = _expand_user_path(raw, home, resolved_root)
        if not p.exists():
            term.warn(f"sandbox.rw_paths: skipping missing path {p}")
            continue
        _append_rw_path(args, p, seen_extra)

    for raw in cli_ro:
        p = _expand_user_path(raw, home, base_cwd)
        if not p.exists():
            raise UsageError(f"--ro path does not exist: {p}")
        _append_ro_path(args, p, seen_extra)

    for raw in cli_rw:
        p = _expand_user_path(raw, home, base_cwd)
        if not p.exists():
            raise UsageError(f"--rw path does not exist: {p}")
        _append_rw_path(args, p, seen_extra)

    # 9. Namespace isolation, cwd, clean env, and target command
    args.extend([
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--share-net",
        "--die-with-parent",
        "--chdir",
        str(chdir),
        "--clearenv",
    ])
    for key in sorted(sandbox_env):
        args.extend(["--setenv", key, sandbox_env[key]])

    args.append("--")
    args.extend(effective_cmd)

    return SandboxPlan(
        target_root=resolved_root,
        chdir=chdir,
        profile=profile.name if profile is not None else None,
        command=effective_cmd,
        bwrap_argv=tuple(args),
        env=sandbox_env,
        ensure_dirs=tuple(ensure_dirs),
    )


def format_dry_run(plan: SandboxPlan) -> str:
    """Format a dry-run preview with secret environment values redacted."""
    redacted_argv: list[str] = []
    idx = 0
    argv = plan.bwrap_argv
    while idx < len(argv):
        tok = argv[idx]
        if tok == "--setenv" and idx + 2 < len(argv):
            key, val = argv[idx + 1], argv[idx + 2]
            shown_val = "<redacted>" if _is_secret_var(key) else val
            redacted_argv.extend(["--setenv", key, shown_val])
            idx += 3
            continue
        redacted_argv.append(tok)
        idx += 1

    profile_label = plan.profile or "none"
    lines = [
        f"# workspace: {plan.target_root}",
        f"# profile: {profile_label}",
        f"# chdir: {plan.chdir}",
        f"# env: {', '.join(sorted(plan.env))}",
        shlex.join(redacted_argv),
    ]
    return "\n".join(lines)


def _load_target(
    workspace: str | None, plugins: Plugins, cwd: Path
) -> tuple[
    Plugins,
    Path,
    str,
    tuple[Workspace, ...],
    tuple[RepoLocation, ...],
    list[Mapping[str, object]],
]:
    """Resolve mono-stack workspace, stack, or multi-stack root into plugins, workspaces & locations.

    Raises `OwError` when `sandbox.enabled` is false for the target (globally, for a multi-stack root).
    """
    root, kind = resolve_target_root(workspace, cwd=cwd)
    target = open_target(root, kind, plugins)
    eff = target.plugins
    if target.multi is not None and not eff.is_enabled("sandbox", None):
        raise OwError(_DISABLED)
    for sess in target.sessions:
        if sess.workspace is not None and not eff.is_enabled("sandbox", sess.workspace):
            raise OwError(_DISABLED)

    settings_layers: list[Mapping[str, object]] = []
    if target.multi is None:
        ws = target.workspace
        settings_layers.append(ws.settings)
        return eff, ws.root, ws.name, (ws,), target.sessions[0].locations, settings_layers

    settings_layers.append(global_settings(eff))
    locations: list[RepoLocation] = []
    for sess in target.sessions:
        locations.extend(sess.locations)
        if sess.workspace is not None:
            settings_layers.append(sess.workspace.settings)
    return eff, target.multi.root, target.multi.name, target.multi.stacks, tuple(locations), settings_layers


def run(
    plugins: Plugins,
    *,
    command: Sequence[str] = (),
    workspace: str | None = None,
    profile: str | None = None,
    ro_paths: Sequence[str] = (),
    rw_paths: Sequence[str] = (),
    pass_env: Sequence[str] = (),
    dry_run: bool = False,
    cwd: Path | None = None,
) -> int:
    """Run `command` (or an interactive shell) inside a bubblewrap sandbox for a workspace."""
    if sys.platform != "linux":
        raise UsageError("`ow sandbox` requires Linux (bubblewrap `bwrap`)")

    base_cwd = (cwd or Path.cwd()).resolve()
    plugins, target_root, target_name, workspaces, locations, settings_layers = _load_target(
        workspace, plugins, base_cwd
    )

    bwrap_bin = shutil.which("bwrap")
    if bwrap_bin is None:
        if dry_run:
            term.warn("bubblewrap (`bwrap`) not found on PATH; dry-run uses placeholder `bwrap`")
            bwrap_bin = "bwrap"
        else:
            raise UsageError(
                "bubblewrap (`bwrap`) not found on PATH; install it via your package manager "
                "(e.g. `sudo apt install bubblewrap` or `sudo pacman -S bubblewrap`)"
            )

    plan = build_sandbox_plan(
        target_root=target_root,
        target_name=target_name,
        workspaces=workspaces,
        locations=locations,
        plugins=plugins,
        command=command,
        cli_profile=profile,
        cli_ro=ro_paths,
        cli_rw=rw_paths,
        cli_env=pass_env,
        settings_layers=settings_layers,
        cwd=base_cwd,
        bwrap_bin=bwrap_bin,
    )

    print(EXPERIMENTAL_NOTICE, file=sys.stderr)
    if dry_run:
        term.out(format_dry_run(plan))
        return 0

    for directory in plan.ensure_dirs:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            term.warn(f"could not create {directory}: {exc}")

    prepare_services(workspaces, plugins)

    return run_foreground(plan.bwrap_argv, cwd=plan.chdir, interactive=not command)
