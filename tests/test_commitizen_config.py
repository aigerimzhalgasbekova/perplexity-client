"""The release config is a hand-copy of commitizen's built-in conventional-commits
plugin, so it has to be checked against the original.

`pyproject.toml` uses `cz_customize` rather than `cz_conventional_commits` for one
reason: `commit_parser`, the regex deciding which commits reach CHANGELOG.md and the
GitHub Release notes, is read only off the plugin class and never off the config file,
and the built-in one hardcodes it to feat|fix|refactor|perf. `cz_customize` is the only
place that key is configurable.

The cost is that every *other* setting -- how a version is bumped, how a commit message
is validated -- is now a copy rather than an inheritance, and a commitizen upgrade can
move the original out from under it. These tests are that alarm.
"""

from commitizen.config import read_cfg
from commitizen.cz.conventional_commits import ConventionalCommitsCz as Stock
from commitizen.factory import committer_factory

CUSTOM = read_cfg().settings["customize"]


def test_copied_settings_still_match_the_stock_plugin():
    """Everything we copied verbatim is still what commitizen itself would have used."""
    stock = Stock(read_cfg())
    assert CUSTOM["schema"] == stock.schema()
    assert CUSTOM["schema_pattern"] == stock.schema_pattern()
    assert CUSTOM["bump_pattern"] == stock.bump_pattern


def test_the_bump_maps_are_stock_plus_the_chore_rule_and_nothing_else():
    """The one divergence, pinned so a second one cannot slip in unnoticed."""
    stock = Stock(read_cfg())
    for ours, theirs in (
        (CUSTOM["bump_map"], stock.bump_map),
        (CUSTOM["bump_map_major_version_zero"], stock.bump_map_major_version_zero),
    ):
        assert ours == dict(theirs) | {"^chore": "PATCH"}
        # First match wins, so a rule added ahead of the breaking-change keys would
        # quietly downgrade a major release.
        assert list(ours)[:-1] == list(theirs)


def test_a_chore_alone_is_enough_to_release():
    """`^chore` has to be reachable, not just present: an earlier key that also matches
    a chore subject would shadow it and the version would never move."""
    from commitizen.bump import find_increment
    from commitizen.git import GitCommit

    cz = committer_factory(read_cfg())

    def increment(*subjects: str) -> str | None:
        return find_increment(
            [GitCommit(rev="0" * 40, title=s) for s in subjects],
            regex=cz.bump_pattern,
            increments_map=cz.bump_map,
        )

    assert increment("chore: bump playwright to 1.50") == "PATCH"
    assert increment("chore!: drop Python 3.13") == "MAJOR"
    assert increment("docs: reword the readme") is None
    # A chore must not be able to hold back a bigger increment.
    assert increment("chore: x", "feat: y") == "MINOR"


def test_changelog_pattern_differs_from_stock_only_by_the_bump_guard():
    assert CUSTOM["changelog_pattern"] == stock_with_guard()


def stock_with_guard() -> str:
    return Stock.changelog_pattern.replace("^", "^(?!bump)", 1)


def test_the_config_is_what_commitizen_actually_loads():
    """A key in the wrong TOML table is silently ignored: assert the loaded object."""
    cz = committer_factory(read_cfg())
    assert cz.commit_parser == CUSTOM["commit_parser"]
    assert cz.change_type_order[:3] == ["BREAKING CHANGE", "Feat", "Fix"]


def test_every_type_the_commit_msg_hook_accepts_reaches_the_changelog():
    """The bug this config exists to fix: a type that passes validation and is then
    dropped by the parser is a commit that vanishes from its own release notes."""
    import re

    cz = committer_factory(read_cfg())
    parser = re.compile(cz.commit_parser)
    keep = re.compile(cz.changelog_pattern)
    accepted = re.compile(cz.schema_pattern())

    types = [
        "build", "bump", "chore", "ci", "docs", "feat",
        "fix", "perf", "refactor", "revert", "style", "test",
    ]  # fmt: skip
    for t in types:
        msg = f"{t}(scope): a subject"
        assert accepted.match(msg), f"{t} is no longer accepted by the commit-msg hook"
        if t == "bump":  # deliberately excluded: the release job's own commit
            assert not keep.match(msg)
            assert not parser.match(msg)
            continue
        assert keep.match(msg), f"{t} is filtered out of the changelog"
        m = parser.match(msg)
        assert m and m.group("change_type") == t, f"{t} is dropped by the parser"
        assert m.group("scope") == "scope"
        assert m.group("message") == "a subject"


def test_a_prose_line_in_a_commit_body_is_not_a_change_type():
    """commitizen runs commit_parser over every paragraph of the body too, so a
    permissive `\\w+` type would turn ordinary prose into changelog sections."""
    import re

    parser = re.compile(committer_factory(read_cfg()).commit_parser)
    for line in ("Note: this was measured on the runner", "Verified: two runs agree"):
        assert parser.match(line) is None, f"{line!r} would become a section heading"


def test_a_breaking_change_still_parses():
    import re

    cz = committer_factory(read_cfg())
    m = re.compile(cz.commit_parser).match("feat(api)!: drop the old endpoint")
    assert m and m.group("change_type") == "feat" and m.group("breaking") == "!"
