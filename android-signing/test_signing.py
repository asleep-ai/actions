"""Tests for signing.py.

The unit tests replace apksigner with a stub that records its arguments, and
read certificates from testdata/, which holds what real apksigner versions
printed. One test signs real APKs and verifies them with every installed
build-tools version when an Android SDK (ANDROID_HOME or ANDROID_SDK_ROOT,
with build-tools and a platform), java and keytool are available, and is
skipped otherwise; ANDROID_SIGNING_INTEGRATION=1 makes a missing tool a
failure instead of a skip.

Run from the repository root:
    uv run --with pytest python -m pytest android-signing -q
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("signing.py")
_spec = importlib.util.spec_from_file_location("signing", SCRIPT)
assert _spec is not None and _spec.loader is not None
signing = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = signing
_spec.loader.exec_module(signing)

TESTDATA = Path(__file__).with_name("testdata")
DIGEST = "3f" * 32
OTHER = "a0" * 32
KEYSTORE = bytes(range(256)) * 9  # long enough for `base64` to wrap it

# testdata/<build-tools version>-<kind of APK>.txt is the output of
# `apksigner verify --verbose --print-certs`, unedited, for APKs signed with
# throwaway keys. Versions 34.0.0 to 36.1.0 print the same certificate lines, so
# 36.1.0 stands for them; 37.0.0 names the signers differently.
FIRST_KEY = "85490f329b6951f13e181db041fcae77c507b7fd0b54084b39f8f6fa3afb997a"
SECOND_KEY = "0bc46f6596db902902f15620cc61d332c51ec7ba74e1845de9ccebedb050e084"
STAMP_KEY = "7dcd89d2adde570df49e2f267c0aac5504314f0f8adf59e836a152bdcd1deca1"
SIGNERS = {
    "default": [FIRST_KEY],  # v2 and v3, what apksigner signs with by default
    "v1v2": [FIRST_KEY],
    "v1only": [FIRST_KEY],
    "rotated": [SECOND_KEY, FIRST_KEY],  # v3.1: the new key from API 33, the old key below it
    "stamp": [FIRST_KEY],  # the source stamp's certificate is not a signer of the APK
}
REAL_OUTPUTS = [f"{version}-{kind}" for version in ("36.1.0", "37.0.0") for kind in SIGNERS]

# Stands in for apksigner: records its arguments and answers from the environment.
FAKE_APKSIGNER = r'''
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_APKSIGNER_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
if len(args) < 3 or args[0] != "verify" or args[1:-1] not in (["--verbose"], ["--verbose", "--print-certs"]):
    sys.exit(f"fake apksigner: unexpected arguments {args}")
print("Verifies" if os.environ.get("FAKE_VERIFY_EXIT", "0") == "0" else "DOES NOT VERIFY")
if "--print-certs" in args:
    sys.stdout.write(os.environ.get("FAKE_PRINT_CERTS", ""))
sys.exit(int(os.environ.get("FAKE_VERIFY_EXIT", "0")))
'''


def real_output(name: str) -> str:
    return (TESTDATA / f"{name}.txt").read_text()


def print_certs(digest: str = DIGEST, signer: str = "Signer #1") -> str:
    """Certificate lines as build-tools up to 36.1.0 print them, for a digest of the test's choosing."""
    return (f"{signer} certificate DN: CN=Test, O=Example\n"
            f"{signer} certificate SHA-256 digest: {digest}\n"
            f"{signer} certificate SHA-1 digest: {'1b' * 20}\n"
            f"{signer} certificate MD5 digest: {'2c' * 16}\n")


def install_apksigner(sdk: Path, version: str) -> Path:
    directory = sdk / "build-tools" / version
    directory.mkdir(parents=True)
    fake = sdk / "fake_apksigner.py"
    fake.write_text(FAKE_APKSIGNER)
    apksigner = directory / "apksigner"
    apksigner.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
    apksigner.chmod(0o755)
    return apksigner


class Runner:
    """Runs main() with the environment a composite step would have."""

    def __init__(self, tmp_path: Path) -> None:
        self.temp = tmp_path / "runner-temp"
        self.temp.mkdir()
        self.sdk = tmp_path / "sdk"
        self.log = tmp_path / "apksigner.jsonl"
        self.output = tmp_path / "output"
        self.apk = tmp_path / "app-release.apk"
        self.apk.write_bytes(b"not a real apk")
        self.env = {"RUNNER_TEMP": str(self.temp), "GITHUB_OUTPUT": str(self.output),
                    "ANDROID_HOME": str(self.sdk), "FAKE_APKSIGNER_LOG": str(self.log),
                    "FAKE_PRINT_CERTS": print_certs()}

    def run(self, operation: str, **env: str) -> int:
        return signing.main({**self.env, "OPERATION": operation, **env})

    def verify(self, **env: str) -> int:
        return self.run("verify", APK_FILE=str(self.apk), **env)

    def outputs(self) -> dict[str, str]:
        lines = self.output.read_text().splitlines() if self.output.exists() else []
        return dict(line.split("=", 1) for line in lines)

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    @property
    def keystore(self) -> Path:
        return self.temp / "release.jks"


@pytest.fixture
def runner(tmp_path: Path) -> Runner:
    return Runner(tmp_path)


def errors(capsys: pytest.CaptureFixture[str]) -> str:
    return "\n".join(line for line in capsys.readouterr().out.splitlines() if line.startswith("::error"))


# decode


def test_decode_writes_the_keystore_for_the_runner_user_only(runner: Runner) -> None:
    assert runner.run("decode", KEYSTORE_BASE64=base64.b64encode(KEYSTORE).decode()) == 0

    assert runner.keystore.read_bytes() == KEYSTORE
    assert stat.S_IMODE(runner.keystore.stat().st_mode) == 0o600
    assert runner.outputs() == {"keystore-file": str(runner.keystore)}


def test_decode_ignores_the_line_breaks_base64_adds(runner: Runner) -> None:
    wrapped = base64.encodebytes(KEYSTORE).decode()
    assert wrapped.count("\n") > 1

    assert runner.run("decode", KEYSTORE_BASE64=f"  {wrapped}\r\n") == 0

    assert runner.keystore.read_bytes() == KEYSTORE


@pytest.mark.parametrize("value", ["", " \n\t"])
def test_decode_fails_on_an_empty_keystore(runner: Runner, capsys: pytest.CaptureFixture[str], value: str) -> None:
    assert runner.run("decode", KEYSTORE_BASE64=value) == 1

    assert "::error title=android-signing::keystore-base64 is empty" in errors(capsys)
    assert not runner.keystore.exists()
    assert runner.outputs() == {}


@pytest.mark.parametrize("value", ["not base64!", "c2VjcmV0LXZhbHV", "===="])
def test_decode_fails_without_quoting_an_undecodable_keystore(
        runner: Runner, capsys: pytest.CaptureFixture[str], value: str) -> None:
    assert runner.run("decode", KEYSTORE_BASE64=value) == 1

    printed = capsys.readouterr().out
    assert "::error title=android-signing::keystore-base64" in printed
    assert value not in printed
    assert not runner.keystore.exists()
    assert runner.outputs() == {}


def test_decode_replaces_what_is_there_instead_of_writing_through_it(runner: Runner, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("untouched")
    runner.keystore.symlink_to(elsewhere)

    assert runner.run("decode", KEYSTORE_BASE64=base64.b64encode(KEYSTORE).decode()) == 0

    assert not runner.keystore.is_symlink()
    assert runner.keystore.read_bytes() == KEYSTORE
    assert elsewhere.read_text() == "untouched"


@pytest.mark.parametrize("temp", ["", "/no/such/directory"])
def test_decode_needs_the_runner_temp_directory(runner: Runner, capsys: pytest.CaptureFixture[str], temp: str) -> None:
    assert runner.run("decode", KEYSTORE_BASE64=base64.b64encode(KEYSTORE).decode(), RUNNER_TEMP=temp) == 1
    assert "RUNNER_TEMP is not set to an existing directory" in errors(capsys)


# Finding apksigner


@pytest.mark.parametrize(("versions", "highest"), [
    (["34.0.0", "35.0.0", "36.0.0"], "36.0.0"),
    (["9.0.0", "10.0.0"], "10.0.0"),  # compared as numbers, not as text
    (["36.0.0", "36.1.0"], "36.1.0"),
    (["36.0.0", "36.0.0-rc1"], "36.0.0"),  # a preview sorts below its release
    (["35.0.0", "36.0.0-rc1"], "36.0.0-rc1"),
    (["36.0.0-rc1", "36.0.0-rc10", "36.0.0-rc2"], "36.0.0-rc10"),
    (["not-a-version", "30.0.3"], "30.0.3"),
    (["not-a-version"], "not-a-version"),
])
def test_highest_build_tools_version_is_used(tmp_path: Path, versions: list[str], highest: str) -> None:
    for version in versions:
        install_apksigner(tmp_path, version)
    (tmp_path / "build-tools" / "99.0.0").mkdir()  # a version without apksigner does not count

    assert signing.find_apksigner(tmp_path) == tmp_path / "build-tools" / highest / "apksigner"


def test_apksigner_that_is_not_executable_does_not_count(tmp_path: Path) -> None:
    install_apksigner(tmp_path, "35.0.0")
    install_apksigner(tmp_path, "36.0.0").chmod(0o644)

    assert signing.find_apksigner(tmp_path) == tmp_path / "build-tools" / "35.0.0" / "apksigner"


def test_named_build_tools_version_is_used_instead_of_the_highest(runner: Runner) -> None:
    for version in ("35.0.0", "36.0.0", "37.0.0"):
        install_apksigner(runner.sdk, version)

    assert signing.find_apksigner(runner.sdk, "36.0.0") == runner.sdk / "build-tools" / "36.0.0" / "apksigner"
    assert runner.verify(BUILD_TOOLS_VERSION=" 35.0.0\n") == 0


@pytest.mark.parametrize("version", ["36.1.0", "36", "../build-tools/36.0.0", "36.0.0/", "/", "."])
def test_named_build_tools_version_must_be_installed(
        runner: Runner, capsys: pytest.CaptureFixture[str], version: str) -> None:
    install_apksigner(runner.sdk, "35.0.0")
    install_apksigner(runner.sdk, "36.0.0")

    assert runner.verify(BUILD_TOOLS_VERSION=version) == 1

    printed = errors(capsys)
    assert f"build-tools-version {version!r} has no apksigner" in printed
    assert printed.endswith("installed: 35.0.0, 36.0.0")
    assert runner.calls() == []


def test_sdk_root_prefers_android_home(tmp_path: Path) -> None:
    assert signing.sdk_root({"ANDROID_HOME": "/home-sdk", "ANDROID_SDK_ROOT": "/root-sdk"}) == Path("/home-sdk")
    assert signing.sdk_root({"ANDROID_HOME": " ", "ANDROID_SDK_ROOT": "/root-sdk"}) == Path("/root-sdk")
    assert signing.sdk_root({"ANDROID_SDK_ROOT": "/root-sdk"}) == Path("/root-sdk")


def test_verify_uses_android_sdk_root_without_android_home(runner: Runner) -> None:
    install_apksigner(runner.sdk, "36.0.0")

    assert runner.verify(ANDROID_HOME="", ANDROID_SDK_ROOT=str(runner.sdk)) == 0

    assert runner.calls() == [["verify", "--verbose", str(runner.apk)]]


def test_verify_fails_without_an_sdk(runner: Runner, capsys: pytest.CaptureFixture[str]) -> None:
    assert runner.verify(ANDROID_HOME="") == 1
    assert "neither ANDROID_HOME nor ANDROID_SDK_ROOT is set" in errors(capsys)


def test_verify_fails_without_apksigner(runner: Runner, capsys: pytest.CaptureFixture[str]) -> None:
    assert runner.verify() == 1  # no build-tools directory at all
    (runner.sdk / "build-tools" / "36.0.0").mkdir(parents=True)
    assert runner.verify() == 1  # a build-tools version without apksigner

    printed = errors(capsys)
    assert printed.count(f"apksigner not found under {runner.sdk / 'build-tools'}") == 2


# Reading the certificate digests


@pytest.mark.parametrize("name", REAL_OUTPUTS)
def test_signer_digests_are_read_from_real_apksigner_output(name: str) -> None:
    output = real_output(name)
    # The file is what the test says it is: it holds the keys' digests and, for
    # the stamped APK, the stamp's, which must not come back as a signer.
    assert ("Source Stamp" in output) == name.endswith("-stamp") == (STAMP_KEY in output)

    assert signing.signer_digests(output) == SIGNERS[name.split("-", 1)[1]]


def test_real_output_names_signers_differently_across_versions() -> None:
    # Why the signer's name is not matched: the same APK, two build-tools versions.
    assert f"Signer #1{signing.CERT_DIGEST}{FIRST_KEY}\n" in real_output("36.1.0-default")
    assert f"V3.0 Signer:{signing.CERT_DIGEST}{FIRST_KEY}\n" in real_output("37.0.0-default")
    assert f"Source Stamp Signer{signing.CERT_DIGEST}{STAMP_KEY}\n" in real_output("36.1.0-stamp")
    assert f"Source Stamp Signer:{signing.CERT_DIGEST}{STAMP_KEY}\n" in real_output("37.0.0-stamp")


def test_line_endings_do_not_matter() -> None:
    assert signing.signer_digests(real_output("37.0.0-rotated").replace("\n", "\r\n")) == [SECOND_KEY, FIRST_KEY]


@pytest.mark.parametrize("output", [
    "", "Verifies\n", "DOES NOT VERIFY\n",
    f"SHA-256 digest: {DIGEST}\n",
    f"certificate SHA-256 digest: {DIGEST}\n",  # no signer is named
    # Forms no apksigner version prints are not read as a certificate.
    f"Signer #1 certificate SHA256 digest: {DIGEST}\n",
    f"Signer #1 certificate SHA-256: {DIGEST}\n",
    f"Signer #1 public key SHA-256 digest: {DIGEST}\n",
])
def test_output_without_a_signer_certificate_gives_no_digest(output: str) -> None:
    assert signing.signer_digests(output) == []


@pytest.mark.parametrize("digest", ["3f3f", DIGEST.upper(), ":".join(re.findall("..", DIGEST)), DIGEST + " extra"])
def test_signer_digest_that_is_not_a_digest_is_an_error(digest: str) -> None:
    with pytest.raises(signing.Failure, match="not 64 hex digits"):
        signing.signer_digests(f"Signer #1 certificate SHA-256 digest: {digest}\n")


def test_certificate_name_cannot_pass_for_an_expected_signer(runner: Runner, capsys: pytest.CaptureFixture[str]) -> None:
    # A certificate's subject is the signer's to choose, so it can spell out a
    # digest line. That adds a line; it cannot remove the real one, and every
    # digest has to be expected.
    install_apksigner(runner.sdk, "37.0.0")
    forged = (f"V3.0 Signer: certificate DN: CN=x, O=V3.0 Signer:{signing.CERT_DIGEST}{DIGEST}\n"
              f"V3.0 Signer:{signing.CERT_DIGEST}{OTHER}\n")
    assert signing.signer_digests(forged) == [DIGEST, OTHER]

    assert runner.verify(EXPECTED_CERT_SHA256=DIGEST, FAKE_PRINT_CERTS=forged) == 1

    assert f"not expected: SHA-256 {OTHER}" in errors(capsys)


@pytest.mark.parametrize(("raw", "expected"), [
    ("", []),
    (" \n", []),
    (DIGEST, [DIGEST]),
    (f"  {DIGEST.upper()}\n", [DIGEST]),
    (":".join(re.findall("..", DIGEST.upper())), [DIGEST]),
    (f"{DIGEST}\n{OTHER}", [DIGEST, OTHER]),
    (f"{DIGEST}, {OTHER}", [DIGEST, OTHER]),
])
def test_expected_digests_ignore_case_and_colons(raw: str, expected: list[str]) -> None:
    assert signing.parse_expected(raw) == expected


@pytest.mark.parametrize("raw", ["3f3f", DIGEST + "3f", DIGEST[:-1] + "g", "sha256:" + DIGEST, f"{DIGEST} nope"])
def test_expected_digest_of_another_shape_fails_before_apksigner_runs(
        runner: Runner, capsys: pytest.CaptureFixture[str], raw: str) -> None:
    install_apksigner(runner.sdk, "36.0.0")

    assert runner.verify(EXPECTED_CERT_SHA256=raw) == 1

    assert "expected-cert-sha256 must be SHA-256 digests" in errors(capsys)
    assert runner.calls() == []


# verify


def test_verify_without_an_expected_digest_checks_validity_only(runner: Runner, capsys: pytest.CaptureFixture[str]) -> None:
    install_apksigner(runner.sdk, "35.0.0")
    install_apksigner(runner.sdk, "36.0.0")

    assert runner.verify(FAKE_PRINT_CERTS="unreadable") == 0

    assert runner.calls() == [["verify", "--verbose", str(runner.apk)]]
    printed = capsys.readouterr().out
    assert f"Using {runner.sdk / 'build-tools' / '36.0.0' / 'apksigner'}" in printed
    # An annotation, so a comparison that silently did not happen shows on the run.
    assert "::notice title=android-signing::The APK signature is valid. The signer certificate was not compared" in printed


def test_verify_passes_when_the_signer_is_expected(runner: Runner, capsys: pytest.CaptureFixture[str]) -> None:
    install_apksigner(runner.sdk, "36.0.0")

    assert runner.verify(EXPECTED_CERT_SHA256=":".join(re.findall("..", DIGEST.upper()))) == 0

    # One apksigner run gives the exit status and the certificates.
    assert runner.calls() == [["verify", "--verbose", "--print-certs", str(runner.apk)]]
    assert "::notice" not in capsys.readouterr().out


@pytest.mark.parametrize("name", REAL_OUTPUTS)
def test_verify_compares_every_signer_of_real_output(runner: Runner, name: str) -> None:
    install_apksigner(runner.sdk, name.split("-")[0])
    signers = SIGNERS[name.split("-", 1)[1]]
    output = real_output(name)

    assert runner.verify(EXPECTED_CERT_SHA256=" ".join(signers), FAKE_PRINT_CERTS=output) == 0
    for missing in signers:
        others = [digest for digest in signers if digest != missing] or [OTHER]
        assert runner.verify(EXPECTED_CERT_SHA256=" ".join(others), FAKE_PRINT_CERTS=output) == 1
    # The stamp's certificate is not a signer, expected or not.
    assert runner.verify(EXPECTED_CERT_SHA256=STAMP_KEY, FAKE_PRINT_CERTS=output) == 1


def test_verify_fails_when_the_signer_is_not_expected(runner: Runner, capsys: pytest.CaptureFixture[str]) -> None:
    install_apksigner(runner.sdk, "36.0.0")

    assert runner.verify(EXPECTED_CERT_SHA256=OTHER) == 1

    assert f"a certificate that is not expected: SHA-256 {DIGEST}" in errors(capsys)


def test_verify_needs_every_signer_to_be_expected(runner: Runner, capsys: pytest.CaptureFixture[str]) -> None:
    install_apksigner(runner.sdk, "36.0.0")
    both = print_certs() + print_certs(OTHER, signer="Signer #2")

    assert runner.verify(EXPECTED_CERT_SHA256=DIGEST, FAKE_PRINT_CERTS=both) == 1
    assert f"not expected: SHA-256 {OTHER}" in errors(capsys)
    assert runner.verify(EXPECTED_CERT_SHA256=f"{OTHER}\n{DIGEST}", FAKE_PRINT_CERTS=both) == 0


def test_verify_fails_on_an_invalid_signature_before_comparing(runner: Runner, capsys: pytest.CaptureFixture[str]) -> None:
    install_apksigner(runner.sdk, "36.0.0")

    assert runner.verify(EXPECTED_CERT_SHA256=DIGEST, FAKE_VERIFY_EXIT="1") == 1

    # The certificates are printed and expected, and the APK still does not verify.
    assert "apksigner verify failed" in errors(capsys)
    assert runner.calls() == [["verify", "--verbose", "--print-certs", str(runner.apk)]]


@pytest.mark.parametrize("output", [
    "Signer #1 certificate fingerprint (SHA2): abc\n",
    "",
    print_certs(signer="Source Stamp Signer"),
], ids=["unknown-format", "empty", "stamp-only"])
def test_verify_fails_when_no_signer_certificate_can_be_read(
        runner: Runner, capsys: pytest.CaptureFixture[str], output: str) -> None:
    install_apksigner(runner.sdk, "36.0.0")

    assert runner.verify(EXPECTED_CERT_SHA256=DIGEST, FAKE_PRINT_CERTS=output) == 1

    assert "no signer certificate SHA-256 could be read" in errors(capsys)


def test_verify_passes_apksigner_an_absolute_path(runner: Runner, monkeypatch: pytest.MonkeyPatch) -> None:
    install_apksigner(runner.sdk, "36.0.0")
    dashed = runner.apk.with_name("-release.apk")
    dashed.write_bytes(b"not a real apk")
    monkeypatch.chdir(dashed.parent)

    assert runner.run("verify", APK_FILE="-release.apk") == 0

    # A relative name starting with "-" would be read as an option.
    assert runner.calls() == [["verify", "--verbose", os.path.join(os.getcwd(), "-release.apk")]]


def test_verify_checks_every_apk_it_is_given(runner: Runner, capsys: pytest.CaptureFixture[str]) -> None:
    install_apksigner(runner.sdk, "36.0.0")
    second = runner.apk.with_name("app-other-release.apk")
    second.write_bytes(b"not a real apk")

    # One path per line; blank lines and surrounding spaces are not paths.
    assert runner.run("verify", APK_FILE=f"{runner.apk}\n\n  {second}  \n") == 0

    assert runner.calls() == [["verify", "--verbose", str(runner.apk)], ["verify", "--verbose", str(second)]]
    printed = capsys.readouterr().out
    assert printed.count("::notice") == 1
    assert "The signatures of 2 APKs are valid. The signer certificate was not compared" in printed


def test_verify_compares_the_certificate_of_every_apk(runner: Runner, capsys: pytest.CaptureFixture[str]) -> None:
    install_apksigner(runner.sdk, "36.0.0")
    second = runner.apk.with_name("app-other-release.apk")
    second.write_bytes(b"not a real apk")

    assert runner.run("verify", APK_FILE=f"{runner.apk}\n{second}", EXPECTED_CERT_SHA256=DIGEST) == 0

    assert [call[-1] for call in runner.calls()] == [str(runner.apk), str(second)]
    assert all("--print-certs" in call for call in runner.calls())
    assert capsys.readouterr().out.count("is valid and the signer certificate is expected") == 2


def test_verify_stops_at_the_first_apk_that_does_not_verify(runner: Runner, capsys: pytest.CaptureFixture[str]) -> None:
    install_apksigner(runner.sdk, "36.0.0")
    second = runner.apk.with_name("app-other-release.apk")
    second.write_bytes(b"not a real apk")

    assert runner.run("verify", APK_FILE=f"{runner.apk}\n{second}", FAKE_VERIFY_EXIT="1") == 1

    assert f"apksigner verify failed for {runner.apk}" in errors(capsys)
    assert len(runner.calls()) == 1


def test_verify_checks_that_every_apk_exists_before_verifying_any(
    runner: Runner, capsys: pytest.CaptureFixture[str]
) -> None:
    install_apksigner(runner.sdk, "36.0.0")

    assert runner.run("verify", APK_FILE=f"{runner.apk}\nno-such.apk") == 1

    assert "apk-file does not exist: no-such.apk" in errors(capsys)
    assert runner.calls() == []


@pytest.mark.parametrize(("apk", "message"), [("", "apk-file is required"), ("no-such.apk", "apk-file does not exist")])
def test_verify_needs_an_apk(runner: Runner, capsys: pytest.CaptureFixture[str], apk: str, message: str) -> None:
    install_apksigner(runner.sdk, "36.0.0")

    assert runner.run("verify", APK_FILE=apk) == 1

    assert message in errors(capsys)
    assert runner.calls() == []


# cleanup


def test_cleanup_deletes_the_keystore_and_the_listed_files(
        runner: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    keystore = tmp_path / "custom.jks"
    for path in (keystore, workspace / "local.properties", tmp_path / "with space.txt", workspace / "kept.txt"):
        path.write_text("secret")

    paths = f"local.properties\n\n  {tmp_path / 'with space.txt'}  \nno-such-file\nno-such-dir/file\nkept.txt/below\n"
    assert runner.run("cleanup", KEYSTORE_FILE=str(keystore), CLEANUP_PATHS=paths) == 0

    assert not keystore.exists()
    assert sorted(path.name for path in workspace.iterdir()) == ["kept.txt"]
    assert not (tmp_path / "with space.txt").exists()


def test_cleanup_without_a_keystore_path_deletes_where_decode_writes(runner: Runner) -> None:
    assert runner.run("decode", KEYSTORE_BASE64=base64.b64encode(KEYSTORE).decode()) == 0
    assert runner.keystore.exists()

    # What a decode step that failed or was skipped leaves in its output.
    assert runner.run("cleanup", KEYSTORE_FILE="") == 0

    assert not runner.keystore.exists()


def test_cleanup_with_nothing_to_delete_succeeds(runner: Runner) -> None:
    assert runner.run("cleanup") == 0
    assert runner.run("cleanup", RUNNER_TEMP="") == 0


def test_cleanup_refuses_a_directory_and_still_deletes_the_rest(
        runner: Runner, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = tmp_path / "build"
    directory.mkdir()
    (directory / "kept.txt").write_text("kept")
    secret = tmp_path / "local.properties"
    secret.write_text("secret")

    assert runner.run("cleanup", CLEANUP_PATHS=f"{directory}\n{secret}\n") == 1

    assert "only files are deleted" in errors(capsys)
    assert (directory / "kept.txt").exists()
    assert not secret.exists()


@pytest.mark.parametrize("pattern", ["*.properties", "local.propertie?", "[kl]ocal.properties", "app/*/secrets.xml"])
def test_cleanup_fails_on_a_pattern_and_still_deletes_the_rest(
        runner: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        pattern: str) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "local.properties").write_text("secret")
    (tmp_path / "other.properties").write_text("secret")

    # Patterns are not expanded: passing this by would leave local.properties behind with a green step.
    assert runner.run("cleanup", CLEANUP_PATHS=f"{pattern}\nother.properties\n") == 1

    assert f"{pattern} does not exist and looks like a pattern" in errors(capsys)
    assert (tmp_path / "local.properties").exists()
    assert not (tmp_path / "other.properties").exists()


def test_cleanup_deletes_a_file_whose_name_only_looks_like_a_pattern(runner: Runner, tmp_path: Path) -> None:
    odd = tmp_path / "notes [draft]?.txt"
    odd.write_text("secret")

    assert runner.run("cleanup", CLEANUP_PATHS=str(odd)) == 0

    assert not odd.exists()


def test_cleanup_deletes_a_symlink_not_its_target(runner: Runner, tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("kept")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    assert runner.run("cleanup", CLEANUP_PATHS=str(link)) == 0

    assert not link.is_symlink()
    assert target.read_text() == "kept"


# The action as a whole


@pytest.mark.parametrize("operation", ["", "sign", "Decode", "decode verify"])
def test_unknown_operation_fails(runner: Runner, capsys: pytest.CaptureFixture[str], operation: str) -> None:
    assert runner.run(operation, KEYSTORE_BASE64=base64.b64encode(KEYSTORE).decode()) == 1

    assert "operation must be one of decode, verify, cleanup" in errors(capsys)
    assert not runner.keystore.exists()


def test_error_text_cannot_break_out_of_the_command(runner: Runner, capsys: pytest.CaptureFixture[str]) -> None:
    assert runner.run("verify", APK_FILE="no\n::warning::injected.apk") == 1

    printed = capsys.readouterr().out.splitlines()
    assert len(printed) == 1 and "%0A::warning::injected" in printed[0]
    assert signing.escape("a\nb%c\r") == "a%0Ab%25c%0D"


def test_script_runs_as_the_action_runs_it(runner: Runner) -> None:
    env = {**os.environ, **runner.env, "OPERATION": "decode", "KEYSTORE_BASE64": base64.b64encode(KEYSTORE).decode()}
    result = subprocess.run([sys.executable, str(SCRIPT)], env=env, text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stdout + result.stderr
    assert runner.outputs() == {"keystore-file": str(runner.keystore)}
    assert base64.b64encode(KEYSTORE).decode() not in result.stdout + result.stderr


def test_action_yml_expressions_only_where_evaluated_on_purpose() -> None:
    # GitHub evaluates ${{ }} anywhere in action.yml, descriptions included, and
    # refuses to load the action when one does not parse.
    allowed = re.compile(r"^\s*(default|value|[A-Z_0-9]+):\s*\$\{\{")
    text = Path(__file__).with_name("action.yml").read_text()
    offending = [line for line in text.splitlines() if "${{" in line and not allowed.match(line)]
    assert offending == []


def test_action_yml_passes_every_input_to_the_script() -> None:
    # CI runs decode and cleanup through action.yml but verify only through
    # main(), so a misspelt variable in the env block would go unnoticed there.
    text = Path(__file__).with_name("action.yml").read_text()
    declared = set(re.findall(r"^  ([a-z0-9-]+):$", text.split("\ninputs:\n")[1].split("\noutputs:\n")[0], re.MULTILINE))
    passed = dict(re.findall(r"^        ([A-Z0-9_]+): \$\{\{ inputs\.([a-z0-9-]+) \}\}$", text, re.MULTILINE))
    script = SCRIPT.read_text()

    assert set(passed.values()) == declared
    assert [name for name in passed if f'env.get("{name}"' not in script] == []


# Real APKs, signed and verified with the real tools


def real_tools() -> dict[str, Path]:
    """Locate the SDK tools and keytool, or skip (fail when the integration run is required)."""
    unavailable = pytest.fail if os.environ.get("ANDROID_SIGNING_INTEGRATION") == "1" else pytest.skip
    try:
        root = signing.sdk_root(os.environ)
        apksigner = signing.find_apksigner(root)
    except signing.Failure as exc:
        unavailable(str(exc))
    aapt2 = apksigner.with_name("aapt2")
    platforms = sorted(root.glob("platforms/*/android.jar"))
    java, keytool = shutil.which("java"), shutil.which("keytool")
    if not aapt2.is_file() or not platforms or java is None or keytool is None:
        unavailable("the real APK test needs aapt2, an SDK platform, java and keytool")
    # macOS ships java and keytool launchers that fail when no JDK is installed.
    if subprocess.run([java, "-version"], capture_output=True, check=False).returncode != 0:
        unavailable("java is on PATH but does not run; is a JDK installed and JAVA_HOME set?")
    return {"apksigner": apksigner, "aapt2": aapt2, "platform": platforms[-1], "keytool": Path(keytool)}


def tool(*command: object) -> None:
    result = subprocess.run([str(part) for part in command], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_apks_are_signed_with_the_decoded_keystore_and_verified_by_every_version(tmp_path: Path) -> None:
    tools = real_tools()
    runner = Runner(tmp_path)
    env = {**os.environ, "RUNNER_TEMP": str(runner.temp), "GITHUB_OUTPUT": str(runner.output)}
    password = "throwaway-test-password"

    def run(operation: str, **inputs: str) -> int:
        return signing.main({**env, "OPERATION": operation, **inputs})

    def new_key(name: str) -> tuple[Path, str]:
        """Create a keystore holding one key, and return it with its certificate's SHA-256."""
        keystore, certificate = tmp_path / f"{name}.jks", tmp_path / f"{name}.der"
        tool(tools["keytool"], "-genkeypair", "-keystore", keystore, "-alias", "test", "-keyalg", "RSA",
             "-keysize", "2048", "-validity", "2", "-dname", f"CN={name} key, O=Example", "-storepass", password,
             "-keypass", password)
        tool(tools["keytool"], "-exportcert", "-keystore", keystore, "-alias", "test", "-storepass", password,
             "-file", certificate)
        return keystore, hashlib.sha256(certificate.read_bytes()).hexdigest()

    def signer(keystore: object) -> list[object]:
        return ["--ks", keystore, "--ks-key-alias", "test", "--ks-pass", f"pass:{password}",
                "--key-pass", f"pass:{password}"]

    manifest = tmp_path / "AndroidManifest.xml"
    manifest.write_text('<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="com.example.app">'
                        '<uses-sdk android:minSdkVersion="24"/><application android:hasCode="false"/></manifest>\n')
    unsigned = tmp_path / "unsigned.apk"
    tool(tools["aapt2"], "link", "-o", unsigned, "--manifest", manifest, "-I", tools["platform"])
    (original, first), (second_keystore, second), (stamp_keystore, stamp) = (
        new_key("first"), new_key("second"), new_key("stamp"))

    # decode: the file a build would sign with.
    assert run("decode", KEYSTORE_BASE64=base64.encodebytes(original.read_bytes()).decode()) == 0
    keystore = runner.outputs()["keystore-file"]

    # The kinds of APK whose certificates apksigner prints differently, and the signers of each.
    lineage = tmp_path / "lineage"
    tool(tools["apksigner"], "rotate", "--out", lineage, "--old-signer", *signer(keystore),
         "--new-signer", *signer(second_keystore))
    kinds: dict[str, tuple[list[object], list[str]]] = {
        "default": ([], [first]),
        "v1v2": (["--v1-signing-enabled", "true", "--v3-signing-enabled", "false"], [first]),
        "v1only": (["--v1-signing-enabled", "true", "--v2-signing-enabled", "false", "--v3-signing-enabled", "false"],
                   [first]),
        "rotated": (["--next-signer", *signer(second_keystore), "--lineage", lineage,
                     "--rotation-min-sdk-version", "33"], [first, second]),
        "stamp": (["--stamp-signer", *signer(stamp_keystore)], [first]),
    }
    for kind, (options, _) in kinds.items():
        tool(tools["apksigner"], "sign", *signer(keystore), *options, "--out", tmp_path / f"{kind}.apk", unsigned)
    signed = str(tmp_path / "default.apk")

    # verify with the highest version: validity alone, then against the
    # certificate in the forms a caller may paste, then what must fail.
    assert run("verify", APK_FILE=signed) == 0
    assert run("verify", APK_FILE=signed, EXPECTED_CERT_SHA256=first) == 0
    assert run("verify", APK_FILE=signed, EXPECTED_CERT_SHA256=":".join(re.findall("..", first.upper()))) == 0
    assert run("verify", APK_FILE=signed, EXPECTED_CERT_SHA256=second) == 1
    assert run("verify", APK_FILE=str(unsigned)) == 1
    assert run("verify", APK_FILE=str(unsigned), EXPECTED_CERT_SHA256=first) == 1

    # Every installed build-tools version, every kind of APK: the action reads
    # each signer, and neither a missing signer nor the stamp's certificate passes.
    versions = sorted(path.parent.name for path in tools["apksigner"].parent.parent.glob("*/apksigner"))
    for version in versions:
        for kind, (_, signers) in kinds.items():
            apk = str(tmp_path / f"{kind}.apk")
            where = f"build-tools {version}, {kind} APK"
            assert run("verify", APK_FILE=apk, BUILD_TOOLS_VERSION=version) == 0, where
            assert run("verify", APK_FILE=apk, BUILD_TOOLS_VERSION=version,
                       EXPECTED_CERT_SHA256=" ".join(signers)) == 0, where
            assert run("verify", APK_FILE=apk, BUILD_TOOLS_VERSION=version,
                       EXPECTED_CERT_SHA256=" ".join([*signers[1:], stamp])) == 1, where

    # cleanup: the keystore and a file next to it.
    assert run("cleanup", KEYSTORE_FILE=keystore, CLEANUP_PATHS=str(manifest)) == 0
    assert not Path(keystore).exists() and not manifest.exists()
