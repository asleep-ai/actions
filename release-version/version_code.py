"""The versionCode of an Android build. This file is the one place that knows the formula.

Android installs an APK over an installed one only when its versionCode is not
lower. The formula therefore decides which build can replace which:

    QA build n of X.Y               X * 100_000_000 + Y * 100_000 + n                    n 1..48_999
    preview N of X.Y.0              X * 100_000_000 + Y * 100_000 + 49_000 + N           N 1..999
    release X.Y.Z                   X * 100_000_000 + Y * 100_000 + 50_000 + Z * 100     Z 0..499
    label build k after X.Y.Z       the code of the release X.Y.Z + k                    k 1..99

Each minor version owns a block of 100_000 codes: QA builds at the bottom in
the order they are built, the previews of X.Y.0 above them in the order of
their number, and the releases in the upper half, one hundred codes apart in
the order of their patch number. The ninety-nine codes after a release are the
label builds made after it: named test builds of what comes after that
release. Within one minor version that gives:

    QA build < preview < release X.Y.Z < label build after X.Y.Z < release X.Y.(Z+1)

- a release is higher than every earlier release;
- every QA build is lower than every preview, and every preview lower than
  every release. So a preview installs over a QA build, a release over both,
  and never the other way round;
- a label build installs over the release it was made after, and the next
  patch release installs over the label build;
- a later QA build is higher than an earlier one, and the same holds for the
  previews of a version and for the label builds after a release.

A QA build is numbered in the block of the version it leads up to, the next
release of its line. While that is a patch release, the block is the one of
the last release, and the QA build is below that release; once X.Y.0 is
planned, the QA builds are in the block of X.Y and higher than everything of
the minor version before it.

A preview is a preview of X.Y.0: of a new minor or major version, whose block
has no release yet. A patch release has no preview. Its codes would be below
the release that is installed.

A major version is a release line and owns a block of 100_000_000 codes. A
maintenance branch can keep releasing 1.x after 2.0.0 exists: every code of a
line, QA and label builds included, is below every code of the next major, and
within a line the order above holds. A release is therefore higher than the
earlier releases of its own line, not than every release made before it.

To change the formula, change this file and its tests in test_version_code.py;
nothing else computes a versionCode.
"""
from __future__ import annotations

# Google Play's limit, which also keeps the value inside a signed 32-bit integer.
MAX_VERSION_CODE = 2_100_000_000

MAJOR_BLOCK = 100_000_000
MINOR_BLOCK = 100_000
PREVIEW_OFFSET = 49_000
RELEASE_OFFSET = 50_000
# The codes from one patch release to the next: the release itself and its label builds.
PATCH_STEP = 100

MAX_MINOR = MAJOR_BLOCK // MINOR_BLOCK - 1  # 999
MAX_PATCH = (MINOR_BLOCK - RELEASE_OFFSET) // PATCH_STEP - 1  # 499
MAX_PREVIEW = RELEASE_OFFSET - PREVIEW_OFFSET - 1  # 999
MAX_QA_SEQUENCE = PREVIEW_OFFSET - 1  # 48_999
MAX_LABEL = PATCH_STEP - 1  # 99


class VersionCodeError(ValueError):
    """The version or sequence number has no versionCode under the formula."""


def _block(major: int, minor: int) -> int:
    if major < 0 or minor < 0:
        raise VersionCodeError(f"version {major}.{minor} has a negative part")
    if minor > MAX_MINOR:
        raise VersionCodeError(f"minor version {minor} is above {MAX_MINOR}, the highest the versionCode formula holds")
    return major * MAJOR_BLOCK + minor * MINOR_BLOCK


def _checked(code: int, what: str) -> int:
    if code > MAX_VERSION_CODE:
        raise VersionCodeError(f"the versionCode of {what} would be {code}, above the limit of {MAX_VERSION_CODE}")
    return code


def release_code(major: int, minor: int, patch: int) -> int:
    """The versionCode of the release major.minor.patch."""
    if not 0 <= patch <= MAX_PATCH:
        raise VersionCodeError(f"patch version {patch} is outside 0..{MAX_PATCH}, the range the versionCode formula holds")
    return _checked(_block(major, minor) + RELEASE_OFFSET + patch * PATCH_STEP, f"{major}.{minor}.{patch}")


def preview_code(major: int, minor: int, number: int) -> int:
    """The versionCode of preview `number` of the version major.minor.0."""
    if not 1 <= number <= MAX_PREVIEW:
        raise VersionCodeError(
            f"preview number {number} is outside 1..{MAX_PREVIEW}, the preview range of one minor version"
        )
    return _checked(_block(major, minor) + PREVIEW_OFFSET + number, f"preview {number} of {major}.{minor}.0")


def qa_code(major: int, minor: int, sequence: int) -> int:
    """The versionCode of QA build number `sequence` of the minor version major.minor."""
    if not 1 <= sequence <= MAX_QA_SEQUENCE:
        raise VersionCodeError(
            f"QA sequence number {sequence} is outside 1..{MAX_QA_SEQUENCE}, the QA range of one minor version: "
            f"above it the code would be a preview's ({PREVIEW_OFFSET} and up)"
        )
    return _checked(_block(major, minor) + sequence, f"QA build {sequence} of {major}.{minor}")


def label_code(major: int, minor: int, patch: int, number: int) -> int:
    """The versionCode of label build `number` made after the release major.minor.patch."""
    if not 1 <= number <= MAX_LABEL:
        raise VersionCodeError(
            f"label build number {number} is outside 1..{MAX_LABEL}, the label builds one release has room for: "
            f"the code after them is the next patch release's"
        )
    return _checked(release_code(major, minor, patch) + number, f"label build {number} after {major}.{minor}.{patch}")
