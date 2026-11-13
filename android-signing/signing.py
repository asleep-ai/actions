"""Keystore and signature steps of a signed Android build.

action.yml passes every input through an environment variable, so no input is
ever interpolated into a shell command.

Operations:
- decode: write the base64 keystore to RUNNER_TEMP/release.jks, readable by
  the runner's user only, and report its path. The runner empties RUNNER_TEMP
  at the start and the end of every job, so a runner killed mid-job keeps the
  keystore only until its next job.
- verify: run `apksigner verify` from the highest build-tools version of the
  Android SDK. With expected certificate digests, every signer certificate of
  the APK must be one of them.
- cleanup: delete the keystore and the other files named by the caller. A file
  that is not there is fine; anything that could not be deleted fails the step.

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

# One line of `apksigner verify --print-certs`. The text before "certificate"
# names the signer and has changed between build-tools versions ("Signer #1",
# "Signer (minSdkVersion=24, maxSdkVersion=32)"), so it is not matched exactly.
# "Signer #1 public key SHA-256 digest" is another value and does not match.
CERT_DIGEST = re.compile(
    r"(?P<signer>.+?)\s+certificate\s+SHA-?\s?256(?:\s+digest)?\s*:\s*(?P<digest>\S.*)",
    re.IGNORECASE,
)
SHA256_HEX = re.compile(r"[0-9a-f]{64}")


class Failure(Exception):
    """A state in which the operation cannot be confirmed to have worked."""


def escape(text: str) -> str:
    """Escape workflow-command data so a message cannot end the command early."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


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
        match = CERT_DIGEST.fullmatch(line.strip())
        # "Source Stamp Signer" is the stamp of the distributing store, not a signer of the APK.
        if not match or not match.group("signer").lower().startswith("signer"):
            continue
        digest = normalize_digest(match.group("digest"))
        if digest is None:
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


def find_apksigner(root: Path) -> Path:
    build_tools = root / "build-tools"
    versions = []
    if build_tools.is_dir():
        versions = [path for path in build_tools.iterdir()
                    if (path / "apksigner").is_file() and os.access(path / "apksigner", os.X_OK)]
    if not versions:
        raise Failure(f"apksigner not found under {build_tools}; install an Android SDK build-tools package")
    return max(versions, key=lambda path: version_key(path.name)) / "apksigner"


def apksigner_run(command: list[str], env: Mapping[str, str], capture: bool) -> subprocess.CompletedProcess:
    sys.stdout.flush()
    try:
        return subprocess.run(command, env=dict(env), text=True, errors="replace", check=False,
                              stdout=subprocess.PIPE if capture else None)
    except OSError as exc:
        raise Failure(f"could not run {command[0]}: {exc}") from None


def verify(env: Mapping[str, str]) -> int:
    apk_input = env.get("APK_FILE", "").strip()
    if not apk_input:
        raise Failure("apk-file is required for verify")
    # An absolute path cannot be read as an option by apksigner.
    apk = os.path.abspath(apk_input)
    if not os.path.isfile(apk):
        raise Failure(f"apk-file does not exist: {apk_input}")
    expected = parse_expected(env.get("EXPECTED_CERT_SHA256", ""))
    apksigner = str(find_apksigner(sdk_root(env)))
    print(f"Using {apksigner}")

    result = apksigner_run([apksigner, "verify", "--verbose", apk], env, capture=False)
    if result.returncode != 0:
        raise Failure(f"apksigner verify failed for {apk_input} (exit status {result.returncode})")
    if not expected:
        print("The signature is valid. No expected-cert-sha256 was given, so the signer certificate is not compared.")
        return 0

    result = apksigner_run([apksigner, "verify", "--print-certs", apk], env, capture=True)
    print(result.stdout, end="")
    if result.returncode != 0:
        raise Failure(f"apksigner verify --print-certs failed for {apk_input} (exit status {result.returncode})")
    found = signer_digests(result.stdout)
    if not found:
        raise Failure("no signer certificate SHA-256 could be read from the apksigner output above; "
                      "the output of this build-tools version may differ from what the action knows")
    # Every signer has to be expected, not just one of them: a line that only
    # looks like a signer, or a second signer, then fails instead of passing.
    unexpected = sorted(set(found) - set(expected))
    if unexpected:
        raise Failure(f"the APK is signed with a certificate that is not expected: SHA-256 {', '.join(unexpected)}")
    print(f"The signature is valid and the signer certificate is expected: SHA-256 {', '.join(sorted(set(found)))}")
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
            print(f"Not present: {target}")
        except OSError as exc:
            # Keep going: one file that cannot be deleted must not leave the rest behind.
            failed = True
            print(f"::error title=android-signing::{escape(f'could not delete {target}: {exc}')}")
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
        print(f"::error title=android-signing::{escape(str(exc))}")
    except OSError as exc:
        print(f"::error title=android-signing::{escape(f'{operation} failed: {exc}')}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
