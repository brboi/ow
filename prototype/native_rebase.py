#!/usr/bin/env python3
"""PROTOTYPE — throwaway, never merged.

Question (wayfinder ticket "Does native git (`pull --rebase` + `rebase <base>`)
cover the shared-branch rebase scenarios?", brboi/ow#66):

    Can ow 3's `rebase` recipe be plain git, without losing a scenario the
    team flow needs (a shared feature branch `dev/work`, force-pushed weekly
    to stay level with the target `origin/master`)?

Every scenario is built on real git: two "server" bare repos (origin, dev),
a maintainer clone pushing to origin, a colleague clone pushing to dev, and
ow's own layout locally (one bare repo with core.logAllRefUpdates=true and a
worktree on branch `work`, upstream dev/work). Each scenario is rebuilt from
scratch for each recipe, so recipes never see each other's leftovers.

Recipes compared:
  ow2          ow 2.x today: fetch both refs, then the real gather_facts +
               plan_for from src/ow (step 1 `rebase --onto <up> <bound>`
               only when the upstream brings new patches or was force-pushed)
  pull         `git fetch origin` + `git pull --rebase` (when @{u} is set)
               + `git rebase origin/master` (when it isn't merged)
  fetch+rebase `git fetch --multiple origin dev` + `git rebase` (= --fork-point
               @{u}, when @{u} is set) + `git rebase origin/master` (when not
               merged)
  guarded      fetch+rebase, but skip `git rebase` when @{u} is already an
               ancestor of some past value of the branch (its own reflog):
               the upstream brings nothing this branch did not already have
  guarded+pick guarded, and when @{u} only fast-forwarded past the last value
               the branch took in, `git cherry-pick <that>..dev/work` brings
               just the new commits; a force-pushed @{u} still gets `git rebase`

Run:  python prototype/native_rebase.py [scenario-name ...]
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from ow.commands.rebase import gather_facts  # noqa: E402
from ow.utils.rebase_plan import plan_for  # noqa: E402

BASE = "origin/master"
UP = "dev/work"
ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_EDITOR": "true",
}


# ── plumbing ────────────────────────────────────────────────────────────────

def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=ENV)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {cwd}:\n{r.stdout}{r.stderr}")
    return r


def out(cwd: Path, *args: str) -> str:
    return git(cwd, *args).stdout.strip()


def rev(cwd: Path, ref: str) -> str | None:
    r = git(cwd, "rev-parse", "--verify", "-q", ref + "^{commit}", check=False)
    return r.stdout.strip() or None


def is_ancestor(cwd: Path, a: str, b: str) -> bool:
    return git(cwd, "merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def identity(repo: Path, name: str) -> None:
    git(repo, "config", "user.name", name)
    git(repo, "config", "user.email", f"{name}@example.com")


def commit(repo: Path, subject: str, files: dict[str, str] | None = None) -> None:
    for name, content in (files or {f"{subject}.txt": subject + "\n"}).items():
        (repo / name).write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", subject)


@dataclass
class World:
    root: Path

    def __post_init__(self) -> None:
        r = self.root
        self.origin_srv, self.dev_srv = r / "origin.git", r / "dev.git"
        for srv in (self.origin_srv, self.dev_srv):
            subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(srv)], check=True, env=ENV)

        # The maintainer of the target branch.
        self.maint = r / "maint"
        subprocess.run(["git", "clone", "-q", str(self.origin_srv), str(self.maint)],
                       check=True, env=ENV, capture_output=True)
        identity(self.maint, "maint")
        commit(self.maint, "A", {"f.txt": "1\n2\n3\n4\n5\n"})
        commit(self.maint, "B")
        git(self.maint, "push", "-q", "origin", "master")

        # ow's local layout: one bare repo, one worktree per workspace.
        self.bare = r / "repos" / "community.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(self.origin_srv), str(self.bare)],
                       check=True, env=ENV, capture_output=True)
        git(self.bare, "config", "core.logAllRefUpdates", "true")
        git(self.bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        git(self.bare, "remote", "add", "dev", str(self.dev_srv))
        identity(self.bare, "me")
        git(self.bare, "fetch", "-q", "origin")
        self.wt = r / "ws" / "community"
        git(self.bare, "worktree", "add", "-q", "-b", "work", str(self.wt), BASE)
        git(self.wt, "branch", "-q", "--unset-upstream", check=False)
        self._colleague: Path | None = None

    @property
    def colleague(self) -> Path:
        if self._colleague is None:
            c = self.root / "colleague"
            subprocess.run(["git", "clone", "-q", "-o", "dev", "-b", "work", str(self.dev_srv), str(c)],
                           check=True, env=ENV, capture_output=True)
            identity(c, "colleague")
            git(c, "remote", "add", "origin", str(self.origin_srv))
            git(c, "fetch", "-q", "origin")
            self._colleague = c
        return self._colleague


# ── recipes ─────────────────────────────────────────────────────────────────

@dataclass
class Run:
    commands: list[str] = field(default_factory=list)
    outcome: str = "ok"          # ok | conflict | failed | skipped
    detail: str = ""


class Stop(Exception):
    pass


def step(w: World, run: Run, *args: str) -> None:
    run.commands.append("git " + " ".join(args))
    r = git(w.wt, *args, check=False)
    if r.returncode == 0:
        return
    text = (r.stdout + r.stderr).strip()
    busy = next((op for op in ("rebase", "cherry-pick")
                 if rev(w.wt, op.upper().replace("-", "_") + "_HEAD") is not None), None)
    if busy:
        run.outcome = "conflict"
        run.detail = next((l for l in text.splitlines() if l.startswith("CONFLICT")), text.splitlines()[0])
        git(w.wt, busy, "--abort", check=False)
    else:
        run.outcome = "failed"
        run.detail = next((l for l in text.splitlines() if "fatal" in l or "error" in l), text.splitlines()[0])
    raise Stop


def has_upstream(w: World) -> bool:
    return git(w.wt, "rev-parse", "-q", "--verify", "@{upstream}", check=False).returncode == 0


def recipe_ow2(w: World, run: Run) -> None:
    up_before = rev(w.wt, f"refs/remotes/{UP}")
    step(w, run, "fetch", "-q", "origin", "+refs/heads/master:refs/remotes/origin/master")
    try:
        step(w, run, "fetch", "-q", "-f", "dev", "+work:refs/remotes/dev/work")
    except Stop:
        run.outcome, run.detail = "skipped", "fetch failed, refs are stale (" + run.detail + ")"
        raise
    facts = gather_facts(w.wt, "community", BASE, UP, up_before, is_detached=False)
    plan = plan_for(facts)
    if plan.is_skipped:
        run.outcome, run.detail = "skipped", plan.skip_reason
        raise Stop
    for s in plan.steps:
        step(w, run, *s.args)


def recipe_pull(w: World, run: Run) -> None:
    step(w, run, "fetch", "-q", "origin")
    if has_upstream(w):
        step(w, run, "pull", "-q", "--rebase")
    if not is_ancestor(w.wt, BASE, "HEAD"):
        step(w, run, "rebase", "-q", BASE)


def recipe_fetch_rebase(w: World, run: Run) -> None:
    step(w, run, "fetch", "-q", "--multiple", "origin", "dev")
    if has_upstream(w):
        step(w, run, "rebase", "-q")
    if not is_ancestor(w.wt, BASE, "HEAD"):
        step(w, run, "rebase", "-q", BASE)


def upstream_already_integrated(w: World) -> bool:
    """@{u} is an ancestor of some past value of the local branch: whatever
    it carries, this branch already had it (before a level-up rewrote it)."""
    up = rev(w.wt, "@{upstream}")
    branch = out(w.wt, "symbolic-ref", "--short", "HEAD")
    past = out(w.wt, "log", "-g", "--format=%H", branch).splitlines()
    return any(is_ancestor(w.wt, up, sha) for sha in past)


def recipe_guarded(w: World, run: Run) -> None:
    step(w, run, "fetch", "-q", "--multiple", "origin", "dev")
    if has_upstream(w) and not upstream_already_integrated(w):
        step(w, run, "rebase", "-q")
    if not is_ancestor(w.wt, BASE, "HEAD"):
        step(w, run, "rebase", "-q", BASE)


def last_integrated_upstream(w: World) -> str | None:
    """Newest past value of @{u} (its reflog) that some past value of the
    local branch (its reflog) contained: the last upstream this branch took in."""
    branch = out(w.wt, "symbolic-ref", "--short", "HEAD")
    past = out(w.wt, "log", "-g", "--format=%H", branch).splitlines()
    for up in out(w.wt, "log", "-g", "--format=%H", UP).splitlines():
        if any(is_ancestor(w.wt, up, sha) for sha in past):
            return up
    return None


def recipe_guarded_pick(w: World, run: Run) -> None:
    step(w, run, "fetch", "-q", "--multiple", "origin", "dev")
    if has_upstream(w):
        known = last_integrated_upstream(w)
        up = rev(w.wt, "@{upstream}")
        if known == up:
            pass                                       # nothing new upstream
        elif known and is_ancestor(w.wt, known, up):
            step(w, run, "cherry-pick", f"{known[:10]}..{UP}")  # only the new commits
        else:
            step(w, run, "rebase", "-q")               # force-pushed: fork-point
    if not is_ancestor(w.wt, BASE, "HEAD"):
        step(w, run, "rebase", "-q", BASE)


RECIPES = {
    "ow2": recipe_ow2,
    "pull": recipe_pull,
    "fetch+rebase": recipe_fetch_rebase,
    "guarded": recipe_guarded,
    "guarded+pick": recipe_guarded_pick,
}


def execute(w: World, recipe) -> Run:
    run = Run()
    try:
        recipe(w, run)
    except Stop:
        pass
    return run


# ── scenario building blocks ────────────────────────────────────────────────

def publish_work(w: World) -> None:
    """A - B <- origin/master ; work = B - X - Y, published as dev/work.
    Then the target moves on: origin/master = A - B - C - D (not yet fetched)."""
    commit(w.wt, "X")
    commit(w.wt, "Y")
    git(w.wt, "push", "-q", "-u", "dev", "work")
    commit(w.maint, "C")
    commit(w.maint, "D")
    git(w.maint, "push", "-q", "origin", "master")


def squash(c: Path, onto: str, subject: str, files: dict[str, str]) -> None:
    git(c, "fetch", "-q", "--all")
    git(c, "reset", "-q", "--hard", onto)
    commit(c, subject, files)
    git(c, "push", "-q", "-f", "dev", "work")


@dataclass
class Scenario:
    name: str
    story: str
    setup: callable          # (world, recipe) -> None ; may run the recipe as "a previous run"
    expect: list[str]        # subjects of origin/master..HEAD, oldest first


def s_first_run(w, recipe):
    publish_work(w)


def s_second_run(w, recipe):
    publish_work(w)
    execute(w, recipe)


def publish_conflicting_work(w: World) -> None:
    """publish_work, plus the target's E and our published X2 editing the same line."""
    publish_work(w)
    commit(w.maint, "E", {"f.txt": "1\nE\n3\n4\n5\n"})
    git(w.maint, "push", "-q", "origin", "master")
    commit(w.wt, "X2", {"f.txt": "1\nX2\n3\n4\n5\n"})
    git(w.wt, "push", "-q", "dev", "work")


def level_through_conflict(repo: Path) -> None:
    """`git rebase origin/master`, resolving E vs X2 by hand."""
    git(repo, "fetch", "-q", "origin")
    r = git(repo, "rebase", "-q", BASE, check=False)
    assert r.returncode != 0, "expected a conflict on f.txt"
    (repo / "f.txt").write_text("1\nE+X2\n3\n4\n5\n")
    git(repo, "add", "f.txt")
    git(repo, "rebase", "--continue")


def s_second_run_after_conflict(w, recipe):
    # I level onto the target myself, resolving the conflict by hand, and
    # have not force-pushed yet.
    publish_conflicting_work(w)
    level_through_conflict(w.wt)


def s_colleague_pushed_after_conflict(w, recipe):
    # I levelled through the conflict and added W, unpushed; meanwhile the
    # colleague pushed Z on top of the old, unlevelled dev/work.
    publish_conflicting_work(w)
    level_through_conflict(w.wt)
    commit(w.wt, "W")
    c = w.colleague
    commit(c, "Z")
    git(c, "push", "-q", "dev", "work")


def s_colleague_pushed(w, recipe):
    publish_work(w)
    execute(w, recipe)
    commit(w.wt, "W")
    c = w.colleague
    git(c, "pull", "-q", "--ff-only")
    commit(c, "Z")
    git(c, "push", "-q", "dev", "work")


def s_squash_force_push(w, recipe):
    publish_work(w)
    commit(w.wt, "W")
    base_b = out(w.maint, "rev-parse", "HEAD~2")
    squash(w.colleague, base_b, "XY", {"X.txt": "X\n", "Y.txt": "Y\n"})


def s_squash_already_fetched(w, recipe):
    s_squash_force_push(w, recipe)
    git(w.wt, "fetch", "-q", "--all")  # e.g. `ow status --fetch`, or a plain fetch


def s_squash_no_reflog(w, recipe):
    s_squash_already_fetched(w, recipe)
    git(w.bare, "reflog", "expire", "--expire=now", "--all")
    git(w.bare, "reflog", "delete", "refs/remotes/dev/work@{0}", check=False)


def s_weekly_level_by_colleague(w, recipe):
    publish_work(w)
    commit(w.wt, "W")
    c = w.colleague
    git(c, "pull", "-q", "--ff-only")
    git(c, "fetch", "-q", "origin")
    git(c, "rebase", "-q", "origin/master")
    git(c, "push", "-q", "-f", "dev", "work")


def s_weekly_level_both(w, recipe):
    publish_work(w)
    execute(w, recipe)            # I level my branch myself ...
    commit(w.wt, "W")
    c = w.colleague               # ... and so does the colleague, and pushes first
    git(c, "fetch", "-q", "origin")
    git(c, "rebase", "-q", "origin/master")
    git(c, "push", "-q", "-f", "dev", "work")


def s_weekly_level_colleague_resolved(w, recipe):
    publish_conflicting_work(w)
    commit(w.wt, "W")
    c = w.colleague
    level_through_conflict(c)
    git(c, "push", "-q", "-f", "dev", "work")


def s_upstream_deleted(w, recipe):
    publish_work(w)
    git(w.dev_srv, "update-ref", "-d", "refs/heads/work")


def s_never_pushed(w, recipe):
    commit(w.wt, "X")
    commit(w.wt, "Y")
    commit(w.maint, "C")
    commit(w.maint, "D")
    git(w.maint, "push", "-q", "origin", "master")


SCENARIOS = [
    Scenario("first-run", "X, Y published on dev/work; the target moved to D.",
             s_first_run, ["X", "Y"]),
    Scenario("second-run", "Run the same recipe twice; nothing moved in between (dev/work is still the pre-rebase X, Y).",
             s_second_run, ["X", "Y"]),
    Scenario("second-run-after-conflict", "I rebased onto the target myself and resolved a conflict (E vs X2), not pushed yet; now run the recipe.",
             s_second_run_after_conflict, ["X", "Y", "X2"]),
    Scenario("colleague-pushed", "After my first run (not pushed), I add W; a colleague pushes Z on top of the old dev/work.",
             s_colleague_pushed, ["X", "Y", "Z", "W"]),
    Scenario("colleague-pushed-after-conflict", "I levelled through the conflict (E vs X2) and added W, unpushed; the colleague pushed Z on the old dev/work.",
             s_colleague_pushed_after_conflict, ["X", "Y", "X2", "Z", "W"]),
    Scenario("squash-force-push", "I have local W; the colleague squashes X, Y into XY (same files) and force-pushes.",
             s_squash_force_push, ["XY", "W"]),
    Scenario("squash-already-fetched", "Same, but a plain fetch (ow status --fetch) absorbed the force-push before the recipe runs.",
             s_squash_already_fetched, ["XY", "W"]),
    Scenario("squash-no-reflog", "Same, and the remote-tracking reflog is gone (bare repo cloned before logAllRefUpdates).",
             s_squash_no_reflog, ["XY", "W"]),
    Scenario("weekly-level-by-colleague", "I have local W on the old X, Y; the colleague rebases dev/work onto D and force-pushes.",
             s_weekly_level_by_colleague, ["X", "Y", "W"]),
    Scenario("weekly-level-both", "I already levelled onto D and added W; the colleague levelled too and force-pushed first.",
             s_weekly_level_both, ["X", "Y", "W"]),
    Scenario("weekly-level-colleague-resolved", "The colleague levelled through a conflict (E vs X2) and force-pushed; I have W on the old branch.",
             s_weekly_level_colleague_resolved, ["X", "Y", "X2", "W"]),
    Scenario("upstream-deleted", "dev/work was deleted on the server (merged); @{u} is still configured locally.",
             s_upstream_deleted, ["X", "Y"]),
    Scenario("never-pushed", "Local X, Y never pushed, no @{u}; the target moved to D.",
             s_never_pushed, ["X", "Y"]),
]


# ── measurement ─────────────────────────────────────────────────────────────

@dataclass
class Result:
    run: Run
    picks: list[str]
    subjects: list[str]
    on_base: bool
    verdict: str


def measure(w: World, run: Run, reflog_before: int, expect: list[str]) -> Result:
    log = out(w.wt, "reflog", "--format=%gs", "HEAD").splitlines()
    new = log[: len(log) - reflog_before][::-1]
    picks = [l.split(": ", 1)[1] for l in new if "(pick)" in l or l.startswith("cherry-pick:")]
    subjects = out(w.wt, "log", "--reverse", "--format=%s", f"{BASE}..HEAD").splitlines()
    on_base = is_ancestor(w.wt, BASE, "HEAD")
    if run.outcome != "ok":
        verdict = run.outcome
    elif sorted(subjects) == sorted(expect) and on_base:
        verdict = "correct"
    elif len(subjects) != len(set(subjects)):
        verdict = "DUPLICATES"
    elif not on_base:
        verdict = "NOT ON BASE"
    else:
        verdict = "WRONG"
    return Result(run, picks, subjects, on_base, verdict)


def play(sc: Scenario, name: str, recipe) -> Result:
    with tempfile.TemporaryDirectory(prefix="ow-proto-") as tmp:
        w = World(Path(tmp))
        sc.setup(w, recipe)
        before = len(out(w.wt, "reflog", "--format=%gs", "HEAD").splitlines())
        run = execute(w, recipe)
        return measure(w, run, before, sc.expect)


def main(argv: list[str]) -> None:
    verbose = "-v" in argv
    wanted = [a for a in argv if not a.startswith("-")]
    rows = []
    for sc in SCENARIOS:
        if wanted and sc.name not in wanted:
            continue
        print(f"\n━━ {sc.name} ━━  {sc.story}")
        print(f"   expected on top of {BASE}: {', '.join(sc.expect)}")
        row = [sc.name]
        for rname, recipe in RECIPES.items():
            r = play(sc, rname, recipe)
            row.append(f"{r.verdict} ({len(r.picks)} picks)" if r.verdict == "correct" else r.verdict)
            print(f"   [{rname}] {r.verdict}")
            for c in r.run.commands:
                print(f"       $ {c}")
            if r.run.detail:
                print(f"       ! {r.run.detail}")
            print(f"       replayed: {', '.join(r.picks) or '—'}")
            print(f"       result:   {', '.join(r.subjects) or '—'}{'' if r.on_base else '   (HEAD does not contain ' + BASE + ')'}")
        rows.append(row)

    print("\n\nSummary")
    widths = [max(len(str(x)) for x in col) for col in zip(["scenario", *RECIPES], *rows)]
    for line in (["scenario", *RECIPES], *rows):
        print("  " + "  ".join(str(x).ljust(wd) for x, wd in zip(line, widths)))


if __name__ == "__main__":
    main(sys.argv[1:])
