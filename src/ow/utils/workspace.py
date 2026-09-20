"""Runtime prerequisites for a workspace's mise files, and its one true inspection.

Two things stand between ow and a workspace mise can use: a mise recent
enough to read `mise/conf.d/*.toml` (2026.8.13 is the floor — the release
that introduced visible fragments), and mise's explicit trust of the
fragment ow wrote. `inspect_workspace` never runs either check nor mise
itself — it is the single read-only pass every command that shows
workspace state shares: worktree presence, the Odoo core, resolved
options, addon paths, and the fixed generator's proposal against the
lock. `refresh_workspace` is the only place any of that inspection is
acted on, and `trust` is its one call that changes anything outside the
workspace tree.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import replace
from pathlib import Path

import pathspec
from rich.markup import escape

from ow.utils import paths
from ow.utils.config import Config, WorkspaceConfig, report_pending_migration
from ow.utils.display import console, err_console
from ow.utils.generate import GenerationContext, generate_files
from ow.utils.git import get_worktree_common_dir, in_progress_operation, run_cmd
from ow.utils.migration import plan_migration
from ow.utils.odoo import SUPPORTED_MAJORS, probe_odoo, workspace_addon_paths
from ow.utils.options import resolve_options
from ow.utils.render import RenderPlan, RenderResult, plan_files, write_files
from ow.utils.services import COMPOSE_NAME, ensure_services_compose

MISE_FLOOR = (2026, 8, 13)

_VERSION_TOKEN = re.compile(r"\d{4}\.\d+\.\d+")


def _version_text(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def _requirement() -> str:
    return f"mise {_version_text(MISE_FLOOR)} or newer is required"


def require_mise() -> tuple[int, int, int]:
    """The running mise's version, or a `ValueError` saying why it is unusable.

    The first `YYYY.M.PATCH` token in `mise --version` is the whole parse:
    that is the shape mise has published since its date-versioned releases,
    and anything else cannot be compared against the floor.
    """
    try:
        result = run_cmd(
            ["mise", "--version"], quiet=True, capture_output=True, text=True, check=True
        )
    except OSError as exc:
        raise ValueError(f"{_requirement()}; could not run it: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"{_requirement()}; 'mise --version' failed: {exc}") from exc

    match = _VERSION_TOKEN.search(result.stdout or "")
    if match is None:
        raise ValueError(
            f"{_requirement()}; no version in 'mise --version' output: {result.stdout.strip()!r}"
        )
    major, minor, patch = (int(part) for part in match.group().split("."))
    version = (major, minor, patch)
    if version < MISE_FLOOR:
        raise ValueError(f"{_requirement()}; found {_version_text(version)}")
    return version


def trust_fragment(fragment: Path) -> None:
    """Ask mise to trust one fragment, as one argument.

    The caller owns the policy — ow trusts only the fragment it wrote,
    updated, adopted, or found already equal to its proposal, and only
    after the surrounding render succeeded. The path is an argv element and
    never shell text, so a workspace path with spaces or quotes reaches
    mise whole.
    """
    run_cmd(["mise", "trust", str(fragment)], check=True)


# ---------------------------------------------------------------------------
# inspect_workspace / refresh_workspace
# ---------------------------------------------------------------------------

_REQUIREMENTS_DEV = "requirements-dev.txt"
_MISE_FRAGMENT = "mise/conf.d/00-ow.toml"
_LEGACY_MISE_MARKER = "OW_WORKSPACE"


def _worktree_problems(root: Path, aliases: list[str]) -> tuple[str, ...]:
    """Every reason the declared worktrees are not ready to inspect further.

    A missing worktree names `ow init` as the repair: that is the one
    command allowed to create it. A worktree mid rebase/merge/cherry-pick
    is left alone rather than probed further, so a caller never proposes a
    generated file into a repository Git itself is in the middle of.
    """
    problems: list[str] = []
    missing = sorted(alias for alias in aliases if not (root / alias).is_dir())
    if missing:
        problems.append(
            "worktree(s) missing: " + ", ".join(missing)
            + f"; run `ow init` in {root} to repair it"
        )
        return tuple(problems)
    for alias in aliases:
        operation = in_progress_operation(root / alias)
        if operation is not None:
            op_name, cont, abort = operation
            problems.append(f"{alias}: {op_name} in progress ({cont} or {abort})")
    return tuple(problems)


def _venv_minor_mismatch(venv_cfg: Path, wanted: str) -> str | None:
    """A warning naming a `.venv/pyvenv.cfg` whose Python minor disagrees with `wanted`.

    Parsed as plain text — a `key = value` line per pyvenv.cfg's own
    format — and never by importing or executing anything in the venv: the
    venv might target a Python this process cannot even load.
    """
    try:
        text = venv_cfg.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep or key.strip() != "version":
            continue
        found = value.strip()
        pieces = found.split(".")
        if len(pieces) < 2:
            return None
        minor = f"{pieces[0]}.{pieces[1]}"
        if minor != wanted:
            return f"{venv_cfg}: venv is Python {found} but the workspace wants {wanted}"
        return None
    return None


def _legacy_mise_shadow(root: Path) -> str | None:
    """`<root>/mise.toml` left behind by a pre-#45 ow, or None.

    mise loads `mise.toml` *and* `mise/conf.d/*.toml`, the former taking
    precedence — a root file ow itself wrote before it moved to the
    fragment shadows every key the fragment sets. Recognised by ow's own
    marker, never by guessing at arbitrary user content.
    """
    path = root / "mise.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if _LEGACY_MISE_MARKER not in text:
        return None
    return f"{path} was written by an older ow and shadows {_MISE_FRAGMENT}"


def _fallback_plan(root: Path, owignore: tuple[str, ...], errors: tuple[str, ...]) -> RenderPlan:
    """A blocked plan that still shows every locked output, retained, for visibility.

    `plan_files` with an empty proposal treats every current lock entry as
    a path no generator wants any more: exactly the safe, read-only view a
    blocked inspection can still offer, without proposing a single byte.
    """
    return replace(plan_files(root, (), owignore), errors=errors)


def inspect_workspace(config: Config, ws: WorkspaceConfig, root: Path) -> RenderPlan:
    """One complete, read-only inspection: probe, resolve, generate, plan.

    Never touches mise, services, or `.local` seeding, and never starts a
    network operation — every command that only shows workspace state can
    call this freely. A legacy (schema-1) `ws` is still fully inspected:
    the fixed generator's outputs do not depend on the on-disk config
    format, only on the typed fields the legacy translation already
    populated. Its pending conversion is additionally surfaced — blocking
    diagnostics as `gate_blockers`, the retirement report as `warnings` —
    without executing that conversion.
    """
    aliases = list(ws.repos)
    repo_dirs = {alias: root / alias for alias in aliases}

    blockers = _worktree_problems(root, aliases)
    if blockers:
        return _fallback_plan(root, config.owignore, blockers)

    probe = probe_odoo(repo_dirs)
    if probe.kind in ("invalid", "ambiguous"):
        return _fallback_plan(
            root, config.owignore, probe.messages or (f"odoo core: {probe.kind}",)
        )
    if probe.kind == "unsupported" and probe.info is not None:
        core = probe.info
        supported = ", ".join(str(major) for major in sorted(SUPPORTED_MAJORS))
        return _fallback_plan(
            root,
            config.owignore,
            (f"{core.alias}: Odoo {core.series} is not a supported major ({supported})",),
        )

    core = probe.info
    warnings: list[str] = []

    effective = resolve_options(
        config.odoo, config.mise, ws.odoo, ws.mise, core=core, workspace_name=root.name
    )
    warnings.extend(effective.warnings)

    addon_paths = workspace_addon_paths(root, aliases, core.alias if core is not None else None)

    common_dirs: list[Path] = []
    seen: set[Path] = set()
    for alias in aliases:
        common = get_worktree_common_dir(repo_dirs[alias])
        if common is not None and common not in seen:
            seen.add(common)
            common_dirs.append(common)

    try:
        ignore_spec = pathspec.GitIgnoreSpec.from_lines(config.owignore)
    except ValueError as exc:
        return _fallback_plan(root, (), (f"owignore: {exc}",))
    dev_requirements_ignored = ignore_spec.match_file(_REQUIREMENTS_DEV)
    use_dev_requirements = (root / _REQUIREMENTS_DEV).exists() or not dev_requirements_ignored

    context = GenerationContext(
        root=root,
        repos=tuple(aliases),
        core=core,
        options=effective,
        addon_paths=addon_paths,
        services_compose=paths.services_dir() / COMPOSE_NAME,
        git_common_dirs=tuple(common_dirs),
        use_dev_requirements=use_dev_requirements,
    )
    try:
        outputs = generate_files(context)
    except ValueError as exc:
        return _fallback_plan(root, config.owignore, (str(exc),))

    conflicts = tuple(
        f"{gf.path}: would be written inside the declared worktree '{gf.path.parts[0]}'"
        for gf in outputs
        if gf.data is not None
        and gf.path.parts
        and gf.path.parts[0] in ws.repos
        and not ignore_spec.match_file(gf.path.as_posix())
    )
    if conflicts:
        return _fallback_plan(root, config.owignore, conflicts)

    gate_blockers: list[str] = []
    shadow = _legacy_mise_shadow(root)
    if shadow is not None:
        gate_blockers.append(shadow)

    if effective.python is not None:
        mismatch = _venv_minor_mismatch(root / ".venv" / "pyvenv.cfg", effective.python)
        if mismatch is not None:
            warnings.append(mismatch)

    if ws.version == 1:
        migration_plan = plan_migration(config, ws, root)
        gate_blockers.extend(migration_plan.errors)
        warnings.extend(migration_plan.warnings)

    plan = plan_files(root, outputs, config.owignore)
    return replace(plan, warnings=tuple(warnings), gate_blockers=tuple(gate_blockers))


def refresh_workspace(config: Config, ws: WorkspaceConfig, root: Path, *, trust: bool) -> RenderResult:
    """Write what `inspect_workspace` planned, refresh services, then optionally trust.

    Refuses a schema-1 `ws` outright: converting it is `ow init`'s or `ow
    render`'s job, explicitly, never a side effect of writing generated
    files. The caller owns the one mise-prerequisite check — this function
    never runs it — and owns deciding whether to reload configs from disk
    first; it acts on exactly the `ws`/`config` it is given. A write
    failure stops before services or trust are touched.
    """
    if ws.version == 1:
        return RenderResult(
            errors=(f"{root}: workspace is still schema 1; run `ow render` to migrate it first",)
        )

    plan = inspect_workspace(config, ws, root)
    if plan.errors:
        return RenderResult(errors=plan.errors, warnings=plan.warnings + plan.gate_blockers)

    result = write_files(plan)
    if result.errors:
        return result

    warnings = tuple(result.warnings) + plan.gate_blockers

    repo_dirs = {alias: root / alias for alias in ws.repos}
    probe = probe_odoo(repo_dirs)
    errors: tuple[str, ...] = ()
    if probe.kind == "supported":
        try:
            ensure_services_compose()
        except (OSError, ValueError) as exc:
            errors = (f"{paths.services_dir() / COMPOSE_NAME}: {exc}",)

    if trust and not errors:
        touched = (
            _MISE_FRAGMENT in result.wrote
            or _MISE_FRAGMENT in result.updated
            or _MISE_FRAGMENT in result.adopted
        )
        if touched:
            try:
                trust_fragment(root / _MISE_FRAGMENT)
            except (OSError, subprocess.CalledProcessError) as exc:
                errors = (f"could not trust {root / _MISE_FRAGMENT}: {exc}",)

    return replace(result, warnings=warnings, errors=errors)


# ---------------------------------------------------------------------------
# refresh_after_git
# ---------------------------------------------------------------------------


def print_files_refreshed() -> None:
    """Say the one refresh a mutating command ran actually happened.

    `ow switch`'s old note claimed a switch never re-rendered anything;
    what replaced it is this line — printed only after the write
    succeeded, so it is never a promise the run did not keep.
    """
    console.print("[dim]Generated files refreshed.[/]")


def print_render_pointer() -> None:
    """Say the generated files were left alone, and what refreshes them.

    Every path that returns before `refresh_after_git` — a dry run, a
    refusal, a decline, a run where nothing moved — owes the user this
    line rather than silence: the same run without `--dry-run` would have
    refreshed the files itself, and `ow render` is the command that does
    it on its own.
    """
    console.print("[dim]Files were not refreshed — run `ow render` if you need them up to date.[/]")


def refresh_after_git(
    config: Config, ws: WorkspaceConfig, root: Path, *, changed: set[str], failed: bool
) -> bool:
    """Refresh generated files once, after a batch of Git mutations.

    Every mutating command (`switch`, `pull`, `rebase`, `reset`) shares
    this boundary instead of re-rendering per repo: one inspection and one
    write for the whole workspace, run only when at least one repo's plan
    actually executed. Returns True exactly when Git itself succeeded but
    the refresh could not run or could not finish — the caller's cue to
    still exit nonzero, without turning a real Git failure into a second,
    misleading one.

    `changed` empty means nothing moved: no prerequisite is checked, no
    file is touched, nothing is printed — a caller that got here with
    nothing changed says so itself with `print_render_pointer`. A `failed`
    batch is left exactly as Git left it — refreshing on top of a
    half-finished batch would mix generated output from before and after
    the failure, so the skip is explained but nothing is added to the
    failure. A schema-1 `config` or `ws` is not converted here — that is
    `ow init`'s or `ow render`'s job — so the pending-migration notice is
    shown and refresh is skipped without counting as a failure of this
    run.
    """
    if not changed:
        return False
    if failed:
        console.print(
            "[dim]Files were not refreshed: at least one repo's Git operation failed.[/]"
        )
        return False
    if config.version == 1 or ws.version == 1:
        report_pending_migration(config, ws, root)
        return False

    try:
        require_mise()
    except ValueError as exc:
        err_console.print(
            f"[yellow]Git succeeded, but files were not refreshed[/]: {escape(str(exc))}"
        )
        return True

    result = refresh_workspace(config, ws, root, trust=False)
    if result.errors:
        for error in result.errors:
            err_console.print(
                f"[yellow]Git succeeded, but files were not refreshed[/]: {escape(error)}"
            )
        return True

    for warning in result.warnings:
        console.print(f"[dim]{escape(warning)}[/]")
    print_files_refreshed()
    return False