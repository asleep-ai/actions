"""The versionCode of an Android build. This file is the one place that knows the formula.

Android installs an APK over an installed one only when its versionCode is not
lower. The formula therefore decides which build can replace which:

    release X.Y.Z                  X * 100_000_000 + Y * 100_000 + 50_000 + Z
    QA build N after release X.Y.*  X * 100_000_000 + Y * 100_000 + N

Each minor version owns a block of 100_000 codes. QA builds take the lower half
in the order they are built, releases the upper half in the order of their
patch number. Within one minor version that gives:

- a release is higher than every earlier release;
- every QA build is lower than every release, so a release installs over a QA
  build and a QA build never installs over a release;
- a later QA build is higher than an earlier one.

A QA build is numbered from the last release tag, so after X.Y.0 is released
the QA builds move into the block of X.Y and are higher than the releases of
the minor version before it.

To change the formula, change this file and its tests in test_version_code.py;
nothing else computes a versionCode.
"""
from __future__ import annotations

# Google Play's limit, which also keeps the value inside a signed 32-bit integer.
MAX_VERSION_CODE = 2_100_000_000

MAJOR_BLOCK = 100_000_000
MINOR_BLOCK = 100_000
RELEASE_OFFSET = 50_000

MAX_MINOR = MAJOR_BLOCK // MINOR_BLOCK - 1  # 999
MAX_PATCH = MINOR_BLOCK - RELEASE_OFFSET - 1  # 49_999
MAX_QA_SEQUENCE = RELEASE_OFFSET - 1  # 49_999


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


def qa_code(major: int, minor: int, sequence: int) -> int:
    """The versionCode of QA build number `sequence` built after a release of major.minor."""
    if not 1 <= sequence <= MAX_QA_SEQUENCE:
        raise VersionCodeError(
            f"QA sequence number {sequence} is outside 1..{MAX_QA_SEQUENCE}, the QA range of one minor version"
        )
    return _checked(_block(major, minor) + sequence, f"QA build {sequence} of {major}.{minor}")
