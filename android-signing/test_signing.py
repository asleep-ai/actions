"""Tests for signing.py.

The unit tests replace apksigner with a stub that records its arguments. One
test signs and verifies a real APK when an Android SDK (ANDROID_HOME or
ANDROID_SDK_ROOT, with build-tools and a platform), java and keytool are
available, and is skipped otherwise; ANDROID_SIGNING_INTEGRATION=1 makes a
missing tool a failure instead of a skip.

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

DIGEST = "3f" * 32
OTHER = "a0" * 32
KEYSTORE = bytes(range(256)) * 9  # long enough for `base64` to wrap it

# Stands in for apksigner: records its arguments and answers from the environment.
FAKE_APKSIGNER = r'''
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_APKSIGNER_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
if len(args) != 3 or args[0] != "verify" or args[1] not in ("--verbose", "--print-certs"):
    sys.exit(f"fake apksigner: unexpected arguments {args}")
if args[1] == "--verbose":
    print("Verifies")
    sys.exit(int(os.environ.get("FAKE_VERIFY_EXIT", "0")))
sys.stdout.write(os.environ.get("FAKE_PRINT_CERTS", ""))
sys.exit(int(os.environ.get("FAKE_PRINT_CERTS_EXIT", "0")))
'''


def print_certs(digest: str = DIGEST, signer: str = "Signer #1") -> str:
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


@pytest.mark.parametrize("output", [
    print_certs(),
    print_certs(DIGEST.upper()),
    print_certs(":".join(re.findall("..", DIGEST.upper()))),
    print_certs().replace("\n", "\r\n"),
    print_certs().replace("SHA-256 digest", "SHA256 digest"),
    print_certs(signer="Signer (minSdkVersion=24, maxSdkVersion=2147483647)"),
    "WARNING: META-INF/com/android/build/gradle/app-metadata.properties not protected by signature.\n" + print_certs(),
    # --verbose adds the key: its digest is not the certificate's.
    print_certs() + f"Signer #1 key algorithm: RSA\nSigner #1 key size (bits): 2048\n"
                    f"Signer #1 public key SHA-256 digest: {OTHER}\nSigner #1 public key SHA-1 digest: {'4d' * 20}\n",
    # The stamp of a distributing store is not a signer of the APK.
    print_certs() + print_certs(OTHER, signer="Source Stamp Signer"),
], ids=["plain", "uppercase", "colons", "crlf", "sha256", "sdk-range", "warning", "verbose", "source-stamp"])
def test_signer_digest_is_read_from_every_known_output(output: str) -> None:
    assert signing.signer_digests(output) == [DIGEST]


def test_every_signer_is_read() -> None:
    output = (print_certs(signer="Signer (minSdkVersion=33, maxSdkVersion=2147483647)")
              + print_certs(OTHER, signer="Signer (minSdkVersion=24, maxSdkVersion=32)"))
    assert signing.signer_digests(output) == [DIGEST, OTHER]


@pytest.mark.parametrize("output", ["", "Verifies\n", "DOES NOT VERIFY\n", f"SHA-256 digest: {DIGEST}\n"])
def test_output_without_a_signer_certificate_gives_no_digest(output: str) -> None:
    assert signing.signer_digests(output) == []


def test_signer_digest_that_is_not_a_digest_is_an_error() -> None:
    with pytest.raises(signing.Failure, match="not 64 hex digits"):
        signing.signer_digests("Signer #1 certificate SHA-256 digest: 3f3f\n")


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
    assert "the signer certificate is not compared" in printed


def test_verify_passes_when_the_signer_is_expected(runner: Runner) -> None:
    install_apksigner(runner.sdk, "36.0.0")

    assert runner.verify(EXPECTED_CERT_SHA256=":".join(re.findall("..", DIGEST.upper()))) == 0

    assert runner.calls() == [["verify", "--verbose", str(runner.apk)], ["verify", "--print-certs", str(runner.apk)]]


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

    assert "apksigner verify failed" in errors(capsys)
    assert runner.calls() == [["verify", "--verbose", str(runner.apk)]]


@pytest.mark.parametrize("env", [
    {"FAKE_PRINT_CERTS": "Signer #1 certificate fingerprint (SHA2): abc\n"},
    {"FAKE_PRINT_CERTS": ""},
    {"FAKE_PRINT_CERTS_EXIT": "1"},
], ids=["unknown-format", "empty", "failed"])
def test_verify_fails_when_the_certificates_cannot_be_read(
        runner: Runner, capsys: pytest.CaptureFixture[str], env: dict[str, str]) -> None:
    install_apksigner(runner.sdk, "36.0.0")

    assert runner.verify(EXPECTED_CERT_SHA256=DIGEST, **env) == 1

    assert "::error title=android-signing::" in errors(capsys)


def test_verify_passes_apksigner_an_absolute_path(runner: Runner, monkeypatch: pytest.MonkeyPatch) -> None:
    install_apksigner(runner.sdk, "36.0.0")
    dashed = runner.apk.with_name("-release.apk")
    dashed.write_bytes(b"not a real apk")
    monkeypatch.chdir(dashed.parent)

    assert runner.run("verify", APK_FILE="-release.apk") == 0

    # A relative name starting with "-" would be read as an option.
    assert runner.calls() == [["verify", "--verbose", os.path.join(os.getcwd(), "-release.apk")]]


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


# A real APK, signed and verified with the real tools


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


def test_real_apk_is_signed_with_the_decoded_keystore_and_verified(tmp_path: Path) -> None:
    tools = real_tools()
    runner = Runner(tmp_path)
    env = {**os.environ, "RUNNER_TEMP": str(runner.temp), "GITHUB_OUTPUT": str(runner.output)}

    def run(operation: str, **inputs: str) -> int:
        return signing.main({**env, "OPERATION": operation, **inputs})

    manifest = tmp_path / "AndroidManifest.xml"
    manifest.write_text('<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="com.example.app">'
                        '<uses-sdk android:minSdkVersion="24"/><application android:hasCode="false"/></manifest>\n')
    unsigned, signed = tmp_path / "unsigned.apk", tmp_path / "signed.apk"
    tool(tools["aapt2"], "link", "-o", unsigned, "--manifest", manifest, "-I", tools["platform"])
    original, certificate = tmp_path / "original.jks", tmp_path / "certificate.der"
    password = "throwaway-test-password"
    tool(tools["keytool"], "-genkeypair", "-keystore", original, "-alias", "test", "-keyalg", "RSA", "-keysize", "2048",
         "-validity", "2", "-dname", "CN=android-signing test", "-storepass", password, "-keypass", password)
    tool(tools["keytool"], "-exportcert", "-keystore", original, "-alias", "test", "-storepass", password,
         "-file", certificate)
    digest = hashlib.sha256(certificate.read_bytes()).hexdigest()

    # decode: the file a build would sign with.
    assert run("decode", KEYSTORE_BASE64=base64.encodebytes(original.read_bytes()).decode()) == 0
    keystore = runner.outputs()["keystore-file"]
    tool(tools["apksigner"], "sign", "--ks", keystore, "--ks-key-alias", "test", "--ks-pass", f"pass:{password}",
         "--key-pass", f"pass:{password}", "--out", signed, unsigned)

    # verify: validity alone, then against the certificate, in the forms a caller may paste.
    assert run("verify", APK_FILE=str(signed)) == 0
    assert run("verify", APK_FILE=str(signed), EXPECTED_CERT_SHA256=digest) == 0
    assert run("verify", APK_FILE=str(signed), EXPECTED_CERT_SHA256=":".join(re.findall("..", digest.upper()))) == 0
    assert run("verify", APK_FILE=str(signed), EXPECTED_CERT_SHA256=OTHER) == 1
    assert run("verify", APK_FILE=str(unsigned)) == 1
    assert run("verify", APK_FILE=str(unsigned), EXPECTED_CERT_SHA256=digest) == 1

    # The certificate list of every installed build-tools version is readable, not just the highest one's.
    for apksigner in sorted(tools["apksigner"].parent.parent.glob("*/apksigner")):
        result = subprocess.run([str(apksigner), "verify", "--print-certs", str(signed)], text=True,
                                capture_output=True, check=False)
        assert result.returncode == 0, f"{apksigner}: {result.stdout}{result.stderr}"
        assert signing.signer_digests(result.stdout) == [digest], f"{apksigner}: {result.stdout}"

    # cleanup: the keystore and a file next to it.
    assert run("cleanup", KEYSTORE_FILE=keystore, CLEANUP_PATHS=str(manifest)) == 0
    assert not Path(keystore).exists() and not manifest.exists()
