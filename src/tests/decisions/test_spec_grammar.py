"""The spec grammar of `<repo>@<base>..<branch>`: how items split, and what a fork URL looks like."""

from __future__ import annotations

import pytest

from ow.core import bases
from ow.plugins._github import fork_clone_url


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("18.0", ("18.0", None)),
        ("18.0..18.0-fix", ("18.0", "18.0-fix")),
        ("origin/18.0..dev/18.0-fix", ("origin/18.0", "dev/18.0-fix")),
        (" master .. topic/x ", ("master", "topic/x")),
        ("saas-18.3..saas-18.3-fix", ("saas-18.3", "saas-18.3-fix")),
    ],
)
def test_split_range_cuts_base_from_branch(text, expected):
    assert bases.split_range(text) == expected


@pytest.mark.parametrize("text", ["..topic", "18.0..", "..", " .. "])
def test_split_range_refuses_an_empty_side(text):
    with pytest.raises(bases.BaseError, match="expected <base>..<branch>"):
        bases.split_range(text)


@pytest.mark.parametrize(
    ("ref", "remotes", "expected"),
    [
        ("topic", ("origin", "dev"), (None, "topic")),
        ("dev/topic", ("origin", "dev"), ("dev", "topic")),
        ("dev/topic/deep", ("origin", "dev"), ("dev", "topic/deep")),
        ("feature/x", ("origin", "dev"), (None, "feature/x")),
        ("odoo/dev/topic", ("odoo", "odoo/dev"), ("odoo/dev", "topic")),
        ("dev/", ("dev",), ("dev", "")),  # an empty branch: the caller refuses it
    ],
)
def test_split_remote_prefix_takes_the_longest_remote(ref, remotes, expected):
    assert bases.split_remote_prefix(ref, remotes) == expected


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("git@github.com:odoo/enterprise.git", "git@github.com:odoo-dev/enterprise.git"),
        ("https://github.com/odoo/enterprise.git", "https://github.com/odoo-dev/enterprise.git"),
        ("https://github.com/odoo/enterprise", "https://github.com/odoo-dev/enterprise"),
        ("https://token@github.com/odoo/enterprise.git", "https://token@github.com/odoo-dev/enterprise.git"),
        ("ssh://git@github.com/odoo/enterprise.git", "ssh://git@github.com/odoo-dev/enterprise.git"),
        ("file:///srv/remotes/enterprise.git", "git@github.com:odoo-dev/enterprise.git"),
        (None, "git@github.com:odoo-dev/enterprise.git"),
    ],
)
def test_fork_clone_url_keeps_the_scheme_of_origin(origin, expected):
    assert fork_clone_url(origin, "odoo-dev", "enterprise") == expected
