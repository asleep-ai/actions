"""The versionCode of an Android build. This file is the one place that knows the formula.

Android installs an APK over an installed one only when its versionCode is not
lower. The formula therefore decides which build can replace which:

    release X.Y.Z                   X * 100_000_000 + Y * 100_000 + 50_000 + Z
    preview N of X.Y.Z              X * 100_000_000 + Y * 100_000 + 40_000 + N
    QA build N of X.Y               X * 100_000_000 + Y * 100_000 + N

Each minor version owns a block of 100_000 codes: QA builds at the bottom in
the order they are built, previews above them in the order of their number,
releases in the upper half in the order of their patch number. Within one
minor version that gives:

- a release is higher than every earlier release;
- every QA build is lower than every preview, and every preview lower than
  every release. So a preview installs over a QA build, a release over both,
  and never the other way round;
- a later QA build is higher than an earlier one, and a later preview of one
  version higher than an earlier one.

A QA build is numbered in the block of the version it leads up to, the next
release of its line. While that is a patch release, the block is the one of
the last release; once X.Y.0 is planned or released, the QA builds are in the
block of X.Y and higher than everything of the minor version before it.

The number of a preview counts within its version X.Y.Z, and the code within
the minor version X.Y. A preview of a patch release therefore sits below the
releases that minor version already has: previews are for a version whose
minor has no release yet.

A major version is a release line and owns a block of 100_000_000 codes. A
maintenance branch can keep releasing 1.x after 2.0.0 exists: every code of a
line, QA builds included, is below every code of the next major, and within a
line the order above holds. A release is therefore higher than the earlier
releases of its own line, not than every release made before it.

To change the formula, change this file and its tests in test_version_code.py;
nothing else computes a versionCode.
"""
from __future__ import annotations

# Google Play's limit, which also keeps the value inside a signed 32-bit integer.
MAX_VERSION_CODE = 2_100_000_000

MAJOR_BLOCK = 100_000_000
MINOR_BLOCK = 100_000
PREVIEW_OFFSET = 40_000
RELEASE_OFFSET = 50_000

MAX_MINOR = MAJOR_BLOCK // MINOR_BLOCK - 1  # 999
MAX_PATCH = MINOR_BLOCK - RELEASE_OFFSET - 1  # 49_999
MAX_PREVIEW = RELEASE_OFFSET - PREVIEW_OFFSET - 1  # 9_999
MAX_QA_SEQUENCE = PREVIEW_OFFSET - 1  # 39_999


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
    return _checked(_block(major, minor) + RELEASE_OFFSET + patch, f"{major}.{minor}.{patch}")


def preview_code(major: int, minor: int, number: int) -> int:
    """The versionCode of preview `number` of a version major.minor.*."""
    if not 1 <= number <= MAX_PREVIEW:
        raise VersionCodeError(
            f"preview number {number} is outside 1..{MAX_PREVIEW}, the preview range of one minor version"
        )
    return _checked(_block(major, minor) + PREVIEW_OFFSET + number, f"preview {number} of {major}.{minor}")


def qa_code(major: int, minor: int, sequence: int) -> int:
    """The versionCode of QA build number `sequence` of the minor version major.minor."""
    if not 1 <= sequence <= MAX_QA_SEQUENCE:
        raise VersionCodeError(
            f"QA sequence number {sequence} is outside 1..{MAX_QA_SEQUENCE}, the QA range of one minor version: "
            f"above it the code would be a preview's ({PREVIEW_OFFSET} and up)"
        )
    return _checked(_block(major, minor) + sequence, f"QA build {sequence} of {major}.{minor}")
