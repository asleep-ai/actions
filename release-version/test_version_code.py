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
from version_code import (  # noqa: E402
    MAX_VERSION_CODE,
    VersionCodeError,
    label_code,
    preview_code,
    qa_code,
    release_code,
)


@pytest.mark.parametrize(
    ("version", "code"),
    [
        ((0, 0, 1), 50_100),
        ((0, 1, 0), 150_000),
        ((1, 13, 0), 101_350_000),
        ((1, 13, 1), 101_350_100),
        ((1, 14, 0), 101_450_000),
        ((2, 0, 0), 200_050_000),
        ((20, 999, 499), 2_099_999_900),
    ],
)
def test_release_codes(version, code):
    assert release_code(*version) == code


@pytest.mark.parametrize(
    ("version", "sequence", "code"),
    [((0, 1), 1, 100_001), ((0, 1), 412, 100_412), ((1, 13), 7, 101_300_007), ((1, 13), 48_999, 101_348_999)],
)
def test_qa_codes(version, sequence, code):
    assert qa_code(*version, sequence) == code


@pytest.mark.parametrize(
    ("version", "number", "code"),
    [((2, 0), 1, 200_049_001), ((2, 0), 12, 200_049_012), ((1, 14), 3, 101_449_003), ((1, 13), 999, 101_349_999)],
)
def test_preview_codes(version, number, code):
    assert preview_code(*version, number) == code


@pytest.mark.parametrize(
    ("release", "number", "code"),
    [((1, 13, 0), 1, 101_350_001), ((1, 13, 0), 99, 101_350_099), ((1, 13, 4), 7, 101_350_407), ((2, 0, 0), 3, 200_050_003)],
)
def test_label_codes(release, number, code):
    assert label_code(*release, number) == code


@pytest.mark.parametrize("minor_version", [(0, 1), (1, 13), (2, 0), (20, 999)])
def test_the_builds_of_a_minor_version_install_in_their_order(minor_version):
    # QA build < preview < release < label build after it < the next patch release: each installs
    # over the one before it, never the reverse.
    codes = [
        qa_code(*minor_version, 1),
        qa_code(*minor_version, version_code.MAX_QA_SEQUENCE),
        preview_code(*minor_version, 1),
        preview_code(*minor_version, version_code.MAX_PREVIEW),
        release_code(*minor_version, 0),
        label_code(*minor_version, 0, 1),
        label_code(*minor_version, 0, version_code.MAX_LABEL),
        release_code(*minor_version, 1),
        label_code(*minor_version, 1, 1),
        release_code(*minor_version, version_code.MAX_PATCH),
        label_code(*minor_version, version_code.MAX_PATCH, version_code.MAX_LABEL),
    ]
    assert codes == sorted(set(codes))
    # The ranges do not share a code: the last QA build is right below where the previews start,
    # the last preview right below the first release, the last label build right below the next
    # patch release, and the last label build of the last patch is the last code of the block.
    assert version_code.MAX_QA_SEQUENCE + 1 == version_code.PREVIEW_OFFSET
    assert version_code.PREVIEW_OFFSET + version_code.MAX_PREVIEW + 1 == version_code.RELEASE_OFFSET
    assert label_code(*minor_version, 0, version_code.MAX_LABEL) + 1 == release_code(*minor_version, 1)
    block = release_code(*minor_version, 0) - version_code.RELEASE_OFFSET
    last = label_code(*minor_version, version_code.MAX_PATCH, version_code.MAX_LABEL)
    assert last == block + version_code.MINOR_BLOCK - 1


@pytest.mark.parametrize("release", [(0, 1, 0), (1, 13, 0), (1, 13, 4), (2, 0, 0)])
def test_a_label_build_sits_between_its_release_and_the_next_patch(release):
    # A named test build of what follows 1.13.0: it goes onto a device that runs 1.13.0, and
    # 1.13.1 goes onto that device afterwards.
    major, minor, patch = release
    first, last = label_code(*release, 1), label_code(*release, version_code.MAX_LABEL)
    assert release_code(*release) < first < label_code(*release, 2) < last < release_code(major, minor, patch + 1)
    # It is above every QA build and every preview of the minor version of its release.
    assert qa_code(major, minor, version_code.MAX_QA_SEQUENCE) < first
    assert preview_code(major, minor, version_code.MAX_PREVIEW) < first


def test_a_preview_of_the_next_minor_version_installs_over_the_release():
    # 2.0.0 is on the devices. A preview of 2.1.0 is above it, and 2.1.0 above the preview.
    installed = release_code(2, 0, 0)
    assert installed < preview_code(2, 1, 1) < release_code(2, 1, 0)
    # Label builds made after 2.0.0, and the patches of 2.0, are below the preview of 2.1.0 too.
    assert label_code(2, 0, 0, version_code.MAX_LABEL) < release_code(2, 0, version_code.MAX_PATCH) < preview_code(2, 1, 1)


def test_previews_of_one_version_grow_with_their_number():
    codes = [preview_code(2, 0, number) for number in (1, 2, 3, 10, 999)]
    assert codes == sorted(set(codes))


def test_a_release_is_higher_than_every_earlier_release():
    ordered = [(0, 0, 1), (0, 1, 0), (0, 1, 1), (0, 2, 0), (1, 0, 0), (1, 13, 0), (1, 13, 1), (1, 14, 0), (2, 0, 0)]
    codes = [release_code(*version) for version in ordered]
    assert codes == sorted(set(codes))


@pytest.mark.parametrize("major", [0, 1, 2, 19])
def test_a_higher_major_line_is_above_the_whole_line_below_it(major):
    # A maintenance branch keeps releasing 1.x after 2.0.0 exists. Whatever it releases or builds
    # for QA stays below everything of the next major, QA builds, previews and label builds included.
    highest_below = max(
        release_code(major, version_code.MAX_MINOR, version_code.MAX_PATCH),
        label_code(major, version_code.MAX_MINOR, version_code.MAX_PATCH, version_code.MAX_LABEL),
        preview_code(major, version_code.MAX_MINOR, version_code.MAX_PREVIEW),
        qa_code(major, version_code.MAX_MINOR, version_code.MAX_QA_SEQUENCE),
    )
    lowest_above = min(release_code(major + 1, 0, 0), preview_code(major + 1, 0, 1), qa_code(major + 1, 0, 1))
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
    assert release_code(1, 13, 1) == 101_350_100
    # Its QA builds of 1.13 start right above that 1.13.0, and a label build made after 1.13.0 is
    # numbered after the code this formula gives 1.13.0: above the one that shipped, below 1.13.1.
    assert 101_300_000 < qa_code(1, 13, 1)
    assert 101_300_000 < label_code(1, 13, 0, 1) == 101_350_001 < release_code(1, 13, 1)
    # heysleep has shipped only QA builds: first 100_000 + N (QA builds of 0.1), then
    # 200_000_000 + N up to N = 112. Its QA builds go on as QA builds of 2.0 from N = 1001: they
    # install over every QA build that shipped, and the release 2.0.0 installs over them and not
    # the other way round.
    assert qa_code(0, 1, 1) == 100_001
    shipped_qa = max(qa_code(0, 1, version_code.MAX_QA_SEQUENCE), qa_code(2, 0, 112))
    assert shipped_qa < qa_code(2, 0, 1001) == 200_001_001
    assert qa_code(2, 0, version_code.MAX_QA_SEQUENCE) < release_code(2, 0, 0) == 200_050_000
    # Its demo sites get previews of 2.0.0 before the release: above the QA builds, below 2.0.0.
    assert qa_code(2, 0, version_code.MAX_QA_SEQUENCE) < preview_code(2, 0, 1) == 200_049_001 < release_code(2, 0, 0)


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
        lambda: release_code(1, 0, 500),
        lambda: release_code(1, version_code.MAX_MINOR + 1, 0),
        lambda: release_code(1, 0, -1),
        lambda: release_code(-1, 0, 0),
        lambda: release_code(21, 0, 0),
        lambda: qa_code(1, 0, 0),
        lambda: qa_code(1, 0, version_code.MAX_QA_SEQUENCE + 1),
        # Where the previews start: a QA build never takes the code of one.
        lambda: qa_code(1, 0, 49_000),
        lambda: qa_code(21, 0, 1),
        lambda: preview_code(1, 0, 0),
        lambda: preview_code(1, 0, version_code.MAX_PREVIEW + 1),
        lambda: preview_code(1, 0, 1_000),
        lambda: preview_code(21, 0, 1),
        lambda: label_code(1, 0, 0, 0),
        # The code after the last label build is the next patch release's.
        lambda: label_code(1, 0, 0, version_code.MAX_LABEL + 1),
        lambda: label_code(1, 0, 0, 100),
        lambda: label_code(1, 0, version_code.MAX_PATCH + 1, 1),
        lambda: label_code(21, 0, 0, 1),
    ],
)
def test_values_outside_the_formula_are_refused(call):
    with pytest.raises(VersionCodeError):
        call()


def test_the_highest_code_is_within_the_limit():
    highest = label_code(20, version_code.MAX_MINOR, version_code.MAX_PATCH, version_code.MAX_LABEL)
    assert release_code(20, version_code.MAX_MINOR, version_code.MAX_PATCH) < highest <= MAX_VERSION_CODE
    assert MAX_VERSION_CODE < 2**31
