"""Tests of the versionCode formula.

Run: uv run --with pytest python -m pytest release-version -q

The first tests pin the numbers; the rest state what the numbers are for, so a
changed formula has to keep the install order to pass.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import version_code  # noqa: E402
from version_code import MAX_VERSION_CODE, VersionCodeError, qa_code, release_code  # noqa: E402


@pytest.mark.parametrize(
    ("version", "code"),
    [
        ((0, 0, 1), 50_001),
        ((0, 1, 0), 150_000),
        ((1, 13, 1), 101_350_001),
        ((1, 14, 0), 101_450_000),
        ((2, 0, 0), 200_050_000),
        ((20, 999, 49_999), 2_099_999_999),
    ],
)
def test_release_codes(version, code):
    assert release_code(*version) == code


@pytest.mark.parametrize(
    ("version", "sequence", "code"),
    [((0, 1), 1, 100_001), ((0, 1), 412, 100_412), ((1, 13), 7, 101_300_007), ((1, 13), 49_999, 101_349_999)],
)
def test_qa_codes(version, sequence, code):
    assert qa_code(*version, sequence) == code


def test_a_release_is_higher_than_every_earlier_release():
    ordered = [(0, 0, 1), (0, 1, 0), (0, 1, 1), (0, 2, 0), (1, 0, 0), (1, 13, 0), (1, 13, 1), (1, 14, 0), (2, 0, 0)]
    codes = [release_code(*version) for version in ordered]
    assert codes == sorted(set(codes))


@pytest.mark.parametrize("major", [0, 1, 2, 19])
def test_a_higher_major_line_is_above_the_whole_line_below_it(major):
    # A maintenance branch keeps releasing 1.x after 2.0.0 exists. Whatever it releases or builds
    # for QA stays below everything of the next major, QA builds included.
    highest_below = max(
        release_code(major, version_code.MAX_MINOR, version_code.MAX_PATCH),
        qa_code(major, version_code.MAX_MINOR, version_code.MAX_QA_SEQUENCE),
    )
    lowest_above = min(release_code(major + 1, 0, 0), qa_code(major + 1, 0, 1))
    assert highest_below < lowest_above


def test_a_line_keeps_its_order_while_another_line_releases():
    # Released in this order in time; each line only has to rise within itself.
    in_time = [(1, 13, 0), (2, 0, 0), (1, 13, 1), (2, 0, 1), (1, 14, 0), (2, 1, 0)]
    for major in (1, 2):
        codes = [release_code(*version) for version in in_time if version[0] == major]
        assert codes == sorted(set(codes))


def test_the_codes_continue_above_what_the_apps_have_shipped():
    # sleephub shipped 1.13.0 as 101_300_000 under its earlier formula, with test builds up to
    # 101_150_086; its next release is 1.13.1 or 1.14.0.
    assert min(release_code(1, 13, 1), release_code(1, 14, 0)) > 101_300_000
    # heysleep has shipped only QA builds, numbered 100_000 + N by this same formula, and its
    # first release 0.1.0 has to install over them.
    assert qa_code(0, 1, 1) == 100_001
    assert release_code(0, 1, 0) > qa_code(0, 1, version_code.MAX_QA_SEQUENCE)


@pytest.mark.parametrize("last_release", [(0, 1, 0), (1, 13, 0), (1, 13, 4), (3, 0, 0)])
def test_a_qa_build_sits_below_the_releases_of_its_cycle(last_release):
    major, minor, patch = last_release
    first, last = qa_code(major, minor, 1), qa_code(major, minor, version_code.MAX_QA_SEQUENCE)
    # QA builds grow in the order they are built.
    assert first < qa_code(major, minor, 2) < last
    # A QA build never installs over the release it was built after...
    assert last < release_code(*last_release)
    # ...and every release that can follow installs over it.
    for following in ((major, minor, patch + 1), (major, minor + 1, 0), (major + 1, 0, 0)):
        assert last < release_code(*following)


def test_qa_builds_keep_growing_when_the_last_release_moves_to_a_new_minor():
    assert qa_code(1, 13, version_code.MAX_QA_SEQUENCE) < qa_code(1, 14, 1)


@pytest.mark.parametrize(
    "call",
    [
        lambda: release_code(1, 0, version_code.MAX_PATCH + 1),
        lambda: release_code(1, version_code.MAX_MINOR + 1, 0),
        lambda: release_code(1, 0, -1),
        lambda: release_code(-1, 0, 0),
        lambda: release_code(21, 0, 0),
        lambda: qa_code(1, 0, 0),
        lambda: qa_code(1, 0, version_code.MAX_QA_SEQUENCE + 1),
        lambda: qa_code(21, 0, 1),
    ],
)
def test_values_outside_the_formula_are_refused(call):
    with pytest.raises(VersionCodeError):
        call()


def test_the_highest_code_is_within_the_limit():
    assert release_code(20, version_code.MAX_MINOR, version_code.MAX_PATCH) <= MAX_VERSION_CODE
    assert MAX_VERSION_CODE < 2**31
