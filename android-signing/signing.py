"""Keystore and signature steps of a signed Android build.

action.yml passes every input through an environment variable, so no input is
ever interpolated into a shell command.

Operations:
- decode: write the base64 keystore to RUNNER_TEMP/release.jks, readable by
  the runner's user only, and report its path. The runner empties RUNNER_TEMP
  at the start and the end of every job, so a runner killed mid-job keeps the
  keystore only until its next job.
- verify: run `apksigner verify` from the Android SDK, by default from its
  highest build-tools version. With expected certificate digests, every signer
  certificate of the APK must be one of them.
- cleanup: delete the keystore and the other files named by the caller. A file
  that is not there is fine; anything that could not be deleted fails the step,
  and so does a path that is not there and looks like a pattern.

Whatever cannot be confirmed fails the step: an empty or undecodable keystore,
a missing apksigner, a certificate list that cannot be read.
"""
from __future__ import annotations

import base64
import binascii
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Mapping

KEYSTORE_NAME = "release.jks"

# What follows a signer's name on its certificate line in the output of
# `apksigner verify --print-certs`. The name itself differs between build-tools
# versions and kinds of APK, so only this part is matched:
#   up to 36.1.0  Signer #1 certificate SHA-256 digest: <hex>
#                 Signer (minSdkVersion=24, maxSdkVersion=32) certificate SHA-256 digest: <hex>
#                 Source Stamp Signer certificate SHA-256 digest: <hex>
#   37.0.0        V3.0 Signer: certificate SHA-256 digest: <hex>
#                 V3.1 Signer: (minSdkVersion=33, maxSdkVersion=2147483647) certificate SHA-256 digest: <hex>
#                 Source Stamp Signer: certificate SHA-256 digest: <hex>
# The "public key SHA-256 digest" lines of --verbose are another value and do
# not contain this text.
CERT_DIGEST = " certificate SHA-256 digest: "
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
# In a cleanup path, a sign that the caller expected a pattern to be expanded.
PATTERN_CHARACTERS = "*?["


class Failure(Exception):
    """A state in which the operation cannot be confirmed to have worked."""


def escape(text: str) -> str:
    """Escape workflow-command data so a message cannot end the command early."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def annotate(level: str, message: str) -> None:
    print(f"::{level} title=android-signing::{escape(message)}")


def set_output(env: Mapping[str, str], name: str, value: str) -> None:
    if "\n" in value or "\r" in value:
        raise Failure(f"the {name} output would contain a line break")
    output_path = env.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")


def decode(env: Mapping[str, str]) -> int:
    # `base64` wraps its output, so a secret pasted from it holds line breaks.
    encoded = "".join(env.get("KEYSTORE_BASE64", "").split())
    if not encoded:
        raise Failure("keystore-base64 is empty; is the secret set and available to this job?")
    try:
        keystore = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        # The value is a secret: say what is wrong without quoting any of it.
        raise Failure("keystore-base64 is not valid base64") from None
    if not keystore:
        raise Failure("keystore-base64 decodes to an empty file")
    temp = env.get("RUNNER_TEMP", "").strip()
    if not temp or not os.path.isdir(temp):
        raise Failure("RUNNER_TEMP is not set to an existing directory")
    path = Path(temp) / KEYSTORE_NAME
    # Remove whatever is there, then create the file exclusively: the keystore
    # is never written through a symlink or into a file others can read.
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
        file.write(keystore)
    set_output(env, "keystore-file", str(path))
    print(f"Decoded the keystore to {path} ({len(keystore)} bytes).")
    return 0


def normalize_digest(text: str) -> str | None:
    """Return a SHA-256 digest as 64 lowercase hex digits, or None when it is not one."""
    digest = re.sub(r"[:\s]", "", text).lower()
    return digest if SHA256_HEX.fullmatch(digest) else None


def parse_expected(raw: str) -> list[str]:
    digests = []
    for value in raw.replace(",", " ").split():
        digest = normalize_digest(value)
        if digest is None:
            raise Failure("expected-cert-sha256 must be SHA-256 digests of 64 hex digits each, with or without colons")
        digests.append(digest)
    return digests


def signer_digests(output: str) -> list[str]:
    """Return the SHA-256 digest of each signer certificate in `--print-certs` output."""
    digests = []
    for line in output.splitlines():
        signer, found, digest = line.strip().partition(CERT_DIGEST)
        # "Source Stamp Signer" is the stamp of the distributing store, not a signer of the APK.
        if not found or "signer" not in signer.lower() or "source stamp" in signer.lower():
            continue
        if not SHA256_HEX.fullmatch(digest):
            raise Failure(f"apksigner printed a signer certificate SHA-256 that is not 64 hex digits: {line.strip()}")
        digests.append(digest)
    return digests


def version_key(name: str) -> tuple:
    """Order build-tools directory names: 34.0.0 < 36.0.0-rc1 < 36.0.0 < 36.1.0."""
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(.*)", name)
    if not match:
        return (), 0, ()
    numbers = tuple(int(part) for part in match.group(1).split("."))
    # A suffix marks a preview of that version, which sorts below the release.
    suffix = tuple((0, int(part), "") if part.isdigit() else (1, 0, part)
                   for part in re.findall(r"\d+|\D+", match.group(2)))
    return numbers, 0 if suffix else 1, suffix


def sdk_root(env: Mapping[str, str]) -> Path:
    root = env.get("ANDROID_HOME", "").strip() or env.get("ANDROID_SDK_ROOT", "").strip()
    if not root:
        raise Failure("neither ANDROID_HOME nor ANDROID_SDK_ROOT is set; apksigner comes from the Android SDK")
    return Path(root)


def find_apksigner(root: Path, version: str = "") -> Path:
    """Return apksigner of the build-tools `version`, or of the highest installed one."""
    build_tools = root / "build-tools"
    versions = []
    if build_tools.is_dir():
        versions = [path for path in build_tools.iterdir()
                    if (path / "apksigner").is_file() and os.access(path / "apksigner", os.X_OK)]
    if not versions:
        raise Failure(f"apksigner not found under {build_tools}; install an Android SDK build-tools package")
    if not version:
        return max(versions, key=lambda path: version_key(path.name)) / "apksigner"
    # Compared with the directory names, so the value can never leave build-tools.
    installed = sorted((path.name for path in versions), key=version_key)
    if version not in installed:
        raise Failure(f"build-tools-version {version!r} has no apksigner under {build_tools}; "
                      f"installed: {', '.join(installed)}")
    return build_tools / version / "apksigner"


def apksigner_run(command: list[str], env: Mapping[str, str], capture: bool) -> subprocess.CompletedProcess:
    sys.stdout.flush()
    try:
        return subprocess.run(command, env=dict(env), text=True, errors="replace", check=False,
                              stdout=subprocess.PIPE if capture else None)
    except OSError as exc:
        raise Failure(f"could not run {command[0]}: {exc}") from None


def verify_apk(apksigner: str, apk_input: str, expected: list[str], env: Mapping[str, str]) -> None:
    # An absolute path cannot be read as an option by apksigner.
    apk = os.path.abspath(apk_input)
    if not expected:
        result = apksigner_run([apksigner, "verify", "--verbose", apk], env, capture=False)
        if result.returncode != 0:
            raise Failure(f"apksigner verify failed for {apk_input} (exit status {result.returncode})")
        return

    # One run gives both the exit status and the certificates.
    result = apksigner_run([apksigner, "verify", "--verbose", "--print-certs", apk], env, capture=True)
    print(result.stdout, end="")
    if result.returncode != 0:
        raise Failure(f"apksigner verify failed for {apk_input} (exit status {result.returncode})")
    found = signer_digests(result.stdout)
    if not found:
        raise Failure("no signer certificate SHA-256 could be read from the apksigner output above; "
                      "the output of this build-tools version may differ from what the action knows")
    # Every signer has to be expected, not just one of them: a line that only
    # looks like a signer, or a second signer, then fails instead of passing.
    unexpected = sorted(set(found) - set(expected))
    if unexpected:
        raise Failure(f"{apk_input} is signed with a certificate that is not expected: "
                      f"SHA-256 {', '.join(unexpected)}")
    print(f"The signature of {apk_input} is valid and the signer certificate is expected: "
          f"SHA-256 {', '.join(sorted(set(found)))}")


def verify(env: Mapping[str, str]) -> int:
    # One path per line: a build with several flavors has several APKs.
    apk_inputs = [line.strip() for line in env.get("APK_FILE", "").splitlines() if line.strip()]
    if not apk_inputs:
        raise Failure("apk-file is required for verify")
    # Before anything is verified: a path that is wrong must not hide behind the ones that are right.
    missing = [apk_input for apk_input in apk_inputs if not os.path.isfile(os.path.abspath(apk_input))]
    if missing:
        raise Failure("apk-file does not exist: " + "\n".join(missing))
    expected = parse_expected(env.get("EXPECTED_CERT_SHA256", ""))
    apksigner = str(find_apksigner(sdk_root(env), env.get("BUILD_TOOLS_VERSION", "").strip()))
    print(f"Using {apksigner}")

    # Every APK is verified, and the first that fails fails the step.
    for apk_input in apk_inputs:
        verify_apk(apksigner, apk_input, expected, env)
    if not expected:
        # An annotation, not a log line: a misspelt or deleted variable gives an
        # empty input too, and the step is green either way.
        subject = "The APK signature is" if len(apk_inputs) == 1 else f"The signatures of {len(apk_inputs)} APKs are"
        annotate("notice", f"{subject} valid. The signer certificate was not compared, because "
                           "expected-cert-sha256 is empty.")
    return 0


def cleanup(env: Mapping[str, str]) -> int:
    targets = []
    keystore = env.get("KEYSTORE_FILE", "").strip()
    temp = env.get("RUNNER_TEMP", "").strip()
    if keystore:
        targets.append(keystore)
    elif temp:
        # Where decode writes. A decode step that failed or was skipped reports
        # no path, and the keystore still has to go.
        targets.append(os.path.join(temp, KEYSTORE_NAME))
    targets.extend(line.strip() for line in env.get("CLEANUP_PATHS", "").splitlines() if line.strip())

    failed = False
    for target in targets:
        try:
            if stat.S_ISDIR(os.lstat(target).st_mode):
                raise IsADirectoryError("it is a directory, and only files are deleted")
            os.unlink(target)
        except (FileNotFoundError, NotADirectoryError):
            if any(character in target for character in PATTERN_CHARACTERS):
                # Patterns are not expanded. Passing this by would report success
                # while the files the caller meant are still there.
                failed = True
                annotate("error", f"{target} does not exist and looks like a pattern; "
                                  "patterns are not expanded, so list each file")
            else:
                print(f"Not present: {target}")
        except OSError as exc:
            # Keep going: one file that cannot be deleted must not leave the rest behind.
            failed = True
            annotate("error", f"could not delete {target}: {exc}")
        else:
            print(f"Deleted: {target}")
    return 1 if failed else 0


OPERATIONS = {"decode": decode, "verify": verify, "cleanup": cleanup}


def main(env: Mapping[str, str] = os.environ) -> int:
    operation = env.get("OPERATION", "").strip()
    try:
        if operation not in OPERATIONS:
            raise Failure(f"operation must be one of {', '.join(OPERATIONS)}, got {operation!r}")
        return OPERATIONS[operation](env)
    except Failure as exc:
        annotate("error", str(exc))
    except OSError as exc:
        annotate("error", f"{operation} failed: {exc}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
