"""Decision tests for the fetch recipe's per-remote steps."""

from dataclasses import replace
from pathlib import Path

import pytest

from ow.core.facts import RepoFacts
from ow.core.plan import GitStep
from ow.recipes.fetch import fetch_steps

@pytest.fixture
def facts():
    """A healthy repo with a base; override fields with dataclasses.replace()."""
    return RepoFacts(
        name="community",
        path=Path("/ws/community"),
        bare=Path("/data/repos/community.git"),
        remotes=("origin",),
        head=None,
        branch="18.0-voip",
        upstream=None,
        upstream_ab=None,
        base="origin/18.0",
        default_base="origin/18.0",
        explicit_base=None,
        operation=None,
        modified=(),
        untracked=(),
        problem=None,
    )


def test_base_only(facts):
    steps = fetch_steps(facts)
    assert steps == (
        GitStep(
            (
                "git", "-C", "/data/repos/community.git", "fetch", "origin",
                "+refs/heads/18.0:refs/remotes/origin/18.0",
            ),
            mutating=False,
        ),
    )


def test_upstream_on_the_base_remote_gets_its_own_step(facts):
    steps = fetch_steps(replace(facts, upstream="origin/18.0-voip"))
    assert [step.argv for step in steps] == [
        (
            "git", "-C", "/data/repos/community.git", "fetch", "origin",
            "+refs/heads/18.0:refs/remotes/origin/18.0",
        ),
        (
            "git", "-C", "/data/repos/community.git", "fetch", "origin",
            "+refs/heads/18.0-voip:refs/remotes/origin/18.0-voip",
        ),
    ]
    # run_fetches turns a "couldn't find remote ref" failure of this step into
    # a note, so the marker is part of the recipe contract.
    assert steps[1].comment == "upstream origin/18.0-voip"


def test_base_remote_step_first_not_alphabetical(facts):
    dirty = replace(
        facts,
        remotes=("aaa", "dev", "origin"),
        upstream="aaa/topic",
    )
    steps = fetch_steps(dirty)
    assert [step.argv[4] for step in steps] == ["origin", "aaa"]


def test_upstream_equal_to_base_fetches_once(facts):
    steps = fetch_steps(replace(facts, upstream="origin/18.0"))
    assert [step.argv for step in steps] == [
        (
            "git", "-C", "/data/repos/community.git", "fetch", "origin",
            "+refs/heads/18.0:refs/remotes/origin/18.0",
        ),
    ]


def test_remote_with_slash_in_name(facts):
    dirty = replace(facts, remotes=("origin", "gitlab-group/test"), base="gitlab-group/test/main")
    steps = fetch_steps(dirty)
    assert [step.argv for step in steps] == [
        (
            "git", "-C", "/data/repos/community.git", "fetch", "gitlab-group/test",
            "+refs/heads/main:refs/remotes/gitlab-group/test/main",
        ),
    ]


def test_nothing_to_fetch(facts):
    bare = replace(facts, base=None, default_base=None, branch=None)
    assert fetch_steps(bare) == ()


def test_problem_repo_has_no_steps(facts):
    assert fetch_steps(replace(facts, problem="prunable")) == ()


def test_extra_on_base_remote_rides_the_base_step(facts):
    steps = fetch_steps(facts, extra=("origin/19.0",))
    assert [step.argv for step in steps] == [
        (
            "git", "-C", "/data/repos/community.git", "fetch", "origin",
            "+refs/heads/18.0:refs/remotes/origin/18.0",
            "+refs/heads/19.0:refs/remotes/origin/19.0",
        ),
    ]


def test_extra_on_other_remote_gets_its_own_step(facts):
    dirty = replace(facts, remotes=("origin", "dev"), upstream="dev/feature")
    steps = fetch_steps(dirty, extra=("dev/19.0",))
    assert [step.argv for step in steps] == [
        (
            "git", "-C", "/data/repos/community.git", "fetch", "origin",
            "+refs/heads/18.0:refs/remotes/origin/18.0",
        ),
        (
            "git", "-C", "/data/repos/community.git", "fetch", "dev",
            "+refs/heads/feature:refs/remotes/dev/feature",
        ),
        (
            "git", "-C", "/data/repos/community.git", "fetch", "dev",
            "+refs/heads/19.0:refs/remotes/dev/19.0",
        ),
    ]


def test_extra_fetched_without_a_base(facts):
    bare = replace(facts, base=None, default_base=None, branch=None, remotes=("origin", "dev"))
    steps = fetch_steps(bare, extra=("origin/19.0", "dev/saucy"))
    assert [step.argv for step in steps] == [
        (
            "git", "-C", "/data/repos/community.git", "fetch", "origin",
            "+refs/heads/19.0:refs/remotes/origin/19.0",
        ),
        (
            "git", "-C", "/data/repos/community.git", "fetch", "dev",
            "+refs/heads/saucy:refs/remotes/dev/saucy",
        ),
    ]
