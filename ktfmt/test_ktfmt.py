"""Tests for ktfmt.sh and the ktfmt hooks in .pre-commit-hooks.yaml.

Every scenario runs against a fake toolchain (stand-ins for mise and java), so
only git, bash and pre-commit >= 4.4.0 are needed. KTFMT_INTEGRATION=1 also
runs the shared scenarios with the real mise, java and ktfmt, pinned in the
throwaway caller repository's mise.toml (override the versions with
KTFMT_INTEGRATION_KTFMT and KTFMT_INTEGRATION_JAVA).

Run from the repository root:
    uv run --with pytest python -m pytest ktfmt -q
    KTFMT_INTEGRATION=1 uv run --with pytest python -m pytest ktfmt -q
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "ktfmt" / "ktfmt.sh"
BASELINE = "config/ktfmt-baseline.txt"
TOOL = "aqua:Kotlin/ktfmt"

CLEAN = "fun clean(value: Int): Int = value + 1\n"
UNFORMATTED = "fun  bad( a : Int ):Int{return a}\n"
UNFORMATTED_EDIT = "fun  bad( a : Int ):Int{return  a}\n"
FORMATTED = "fun bad(a: Int): Int {\n    return a\n}\n"  # ktfmt's output for both of the above
UNPARSEABLE = "fun broken( {{{\n"

# Stands in for `java -jar <ktfmt jar> @argfile`, judging files by the fixtures above.
FAKE_KTFMT = r'''
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
if len(args) != 3 or args[0] != "-jar" or not args[2].startswith("@"):
    sys.exit(f"fake java: unexpected arguments {args}")
lines = Path(args[2][1:]).read_text().splitlines()
with open(os.environ["FAKE_KTFMT_LOG"], "a") as log:
    log.write(json.dumps({"cwd": os.getcwd(), "jar": args[1], "args": lines}) + "\n")
if args[1] != os.path.join(os.environ["FAKE_KTFMT_HOME"], "ktfmt"):
    sys.exit(f"fake java: unexpected jar {args[1]}")
options = [line for line in lines if line.startswith("--")]
for option in options:
    if option not in ("--kotlinlang-style", "--enable-editorconfig", "--dry-run"):
        sys.exit(f"Unexpected option: {option}")
formatted = set(json.loads(os.environ["FAKE_KTFMT_FORMATTED"]))
status = 0
for name in (line for line in lines if not line.startswith("--")):
    path = Path(name)
    if not path.is_file():
        continue  # ktfmt silently skips missing files
    text = path.read_text()
    if "{{{" in text or name == os.environ.get("FAKE_KTFMT_FAIL_ON"):
        print(f"{name}:1:12: error: Expecting ')'", file=sys.stderr)
        status = 1
    elif text not in formatted:
        if "--dry-run" in options:
            print(name)
        else:
            path.write_text(os.environ["FAKE_KTFMT_OUTPUT"])
sys.exit(status)
'''

# Stands in for mise: `exec` puts the fake java on PATH, `current`/`where` report ktfmt.
FAKE_MISE = r'''#!/usr/bin/env bash
set -euo pipefail
printf '%s\t%s\n' "$PWD" "$*" >>"$FAKE_MISE_LOG"
case "${1-}" in
  exec)
    shift
    if [ "${1-}" = -- ]; then shift; fi
    # Like mise, replace bytes that are not valid UTF-8 in arguments with U+FFFD.
    PATH="$FAKE_JAVA_DIR:$PATH" exec "$FAKE_PYTHON" -c 'import os, sys
args = [arg.encode("utf-8", "surrogateescape").decode("utf-8", "replace") for arg in sys.argv[1:]]
os.execvp(args[0], args)' "$@"
    ;;
  current | where)
    [ "${2-}" = aqua:Kotlin/ktfmt ] || { echo "fake mise: unexpected tool ${2-}" >&2; exit 2; }
    if [ -z "${FAKE_KTFMT_VERSION-}" ]; then
      [ "$1" = current ] && { echo "mise WARN  Plugin $2 does not have a version set" >&2; exit 0; }
      echo "mise ERROR $2 not installed" >&2
      exit 1
    fi
    if [ "$1" = current ]; then echo "$FAKE_KTFMT_VERSION"; else echo "$FAKE_KTFMT_HOME"; fi
    ;;
  *)
    echo "fake mise: unexpected arguments: $*" >&2
    exit 2
    ;;
esac
'''


@dataclass
class Toolchain:
    name: str
    env: dict[str, str]
    mise_toml: str | None = None
    logs: Path | None = None

    def mise_calls(self) -> list[tuple[str, str]]:
        assert self.logs is not None
        path = self.logs / "mise.log"
        lines = path.read_text().splitlines() if path.exists() else []
        return [tuple(line.split("\t", 1)) for line in lines]  # type: ignore[misc]

    def ktfmt_calls(self) -> list[dict]:
        assert self.logs is not None
        path = self.logs / "ktfmt.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture(scope="session")
def pre_commit_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("pre-commit-home")


@pytest.fixture(scope="session")
def base_env(pre_commit_home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("KTFMT_HOOK_IN_MISE", "KTFMT_JAR")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="ktfmt test",
        GIT_AUTHOR_EMAIL="ktfmt@example.invalid",
        GIT_COMMITTER_NAME="ktfmt test",
        GIT_COMMITTER_EMAIL="ktfmt@example.invalid",
        PRE_COMMIT_HOME=str(pre_commit_home),
    )
    return env


@pytest.fixture(params=["fake", "real"])
def toolchain(request: pytest.FixtureRequest, tmp_path: Path, base_env: dict[str, str]) -> Toolchain:
    if request.param == "real":
        if os.environ.get("KTFMT_INTEGRATION") != "1":
            pytest.skip("real mise/java/ktfmt run only with KTFMT_INTEGRATION=1")
        ktfmt = os.environ.get("KTFMT_INTEGRATION_KTFMT", "0.64")
        java = os.environ.get("KTFMT_INTEGRATION_JAVA", "temurin-17.0.20+8")
        return Toolchain("real", dict(base_env), f'[tools]\njava = "{java}"\n"{TOOL}" = "{ktfmt}"\n')
    fake = tmp_path / "fake"
    for directory in ("bin", "java", "ktfmt-home", "logs"):
        (fake / directory).mkdir(parents=True)
    (fake / "ktfmt.py").write_text(FAKE_KTFMT)
    (fake / "ktfmt-home" / "ktfmt").write_text("not a real jar\n")
    executable(fake / "bin" / "mise", FAKE_MISE)
    executable(fake / "java" / "java", f'#!/bin/sh\nexec "{sys.executable}" "{fake / "ktfmt.py"}" "$@"\n')
    env = dict(base_env)
    env.update(
        PATH=f"{fake / 'bin'}{os.pathsep}{base_env['PATH']}",
        FAKE_MISE_LOG=str(fake / "logs" / "mise.log"),
        FAKE_KTFMT_LOG=str(fake / "logs" / "ktfmt.log"),
        FAKE_JAVA_DIR=str(fake / "java"),
        FAKE_PYTHON=sys.executable,
        FAKE_KTFMT_HOME=str(fake / "ktfmt-home"),
        FAKE_KTFMT_VERSION="0.64",
        FAKE_KTFMT_FORMATTED=json.dumps([CLEAN, FORMATTED]),
        FAKE_KTFMT_OUTPUT=FORMATTED,
    )
    return Toolchain("fake", env, logs=fake / "logs")


def executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def fake_only(toolchain: Toolchain) -> None:
    if toolchain.name != "fake":
        pytest.skip("needs the fake toolchain")


class Caller:
    """A throwaway repository that adopts the hook."""

    def __init__(self, root: Path, toolchain: Toolchain) -> None:
        self.root = root
        self.toolchain = toolchain
        self.env = {**toolchain.env, "MISE_TRUSTED_CONFIG_PATHS": str(root)}
        self.git("init", "-q")
        if toolchain.mise_toml:
            (root / "mise.toml").write_text(toolchain.mise_toml)

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.root, env=self.env, text=True,
                              capture_output=True, check=True).stdout

    def add(self, name: str, text: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self.git("add", "--", name)

    def run(self, *args: str | bytes, expected: int = 0, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["bash", str(SCRIPT), *args], cwd=cwd or self.root, env=self.env,
                                encoding="utf-8", errors="replace", capture_output=True, check=False)
        assert result.returncode == expected, result.stdout + result.stderr
        return result

    def fail_command(self, name: str) -> None:
        """Put a `name` that always fails ahead of the real one on PATH."""
        directory = self.root.parent / "failing"
        directory.mkdir(exist_ok=True)
        executable(directory / name, f"#!/bin/sh\necho '{name}: injected failure' >&2\nexit 1\n")
        self.env["PATH"] = f"{directory}{os.pathsep}{self.env['PATH']}"

    def entries(self, baseline: str = BASELINE) -> list[str]:
        lines = (self.root / baseline).read_text().splitlines()
        return [line for line in lines if line and not line.startswith("#")]

    def entry(self, name: str) -> str:
        return f"{self.git('hash-object', '--', name).strip()} {name}"


@pytest.fixture
def caller(tmp_path: Path, toolchain: Toolchain) -> Caller:
    root = tmp_path / "caller"
    root.mkdir()
    return Caller(root, toolchain)


def test_hook_script_is_executable() -> None:
    # unsupported_script executes the entry directly from the hook clone.
    assert os.access(SCRIPT, os.X_OK)


def test_check_names_unformatted_files_including_unusual_names(caller: Caller) -> None:
    caller.add("Clean.kt", CLEAN)
    caller.add("sub dir/Bad Space.kt", UNFORMATTED)
    caller.add("-Dash.kts", UNFORMATTED)

    caller.run("Clean.kt")
    result = caller.run("Clean.kt", "sub dir/Bad Space.kt", "-Dash.kts", expected=1)

    assert result.stdout.splitlines() == ["-Dash.kts", "sub dir/Bad Space.kt"]
    assert "ktfmt-write" in result.stderr
    if caller.toolchain.name == "fake":
        argfile = caller.toolchain.ktfmt_calls()[-1]["args"]
        assert argfile[:3] == ["--kotlinlang-style", "--enable-editorconfig", "--dry-run"]
        # ktfmt has no `--`, so the dash-led path is passed as ./-Dash.kts.
        assert argfile[3:] == ["Clean.kt", "sub dir/Bad Space.kt", "./-Dash.kts"]


def test_clean_project_needs_no_baseline_or_config_dir(caller: Caller) -> None:
    caller.add("src/Clean.kt", CLEAN)

    caller.run("src/Clean.kt")
    result = caller.run("--update-baseline")

    assert "nothing to refresh" in result.stderr
    assert not (caller.root / "config").exists()


def test_parse_failure_is_reported_as_a_formatter_failure(caller: Caller) -> None:
    caller.add("Broken.kt", UNPARSEABLE)

    result = caller.run("Broken.kt", expected=1)

    assert "ktfmt failed while checking Kotlin files" in result.stderr
    assert "Format them with" not in result.stderr


def test_baseline_exempts_legacy_files_only_while_unchanged(caller: Caller) -> None:
    caller.add("Clean.kt", CLEAN)
    caller.add("Zeta.kt", UNFORMATTED)
    caller.add("Alpha Space.kt", UNFORMATTED)
    caller.add("-Dash.kt", UNFORMATTED)

    caller.run("--bootstrap-baseline")

    assert caller.entries() == [caller.entry("-Dash.kt"), caller.entry("Alpha Space.kt"), caller.entry("Zeta.kt")]
    caller.run("Clean.kt", "Zeta.kt", "Alpha Space.kt", "-Dash.kt")
    # Any edit, even one that leaves the file just as unformatted, ends the exemption.
    (caller.root / "Alpha Space.kt").write_text(UNFORMATTED_EDIT)
    result = caller.run("Clean.kt", "Zeta.kt", "Alpha Space.kt", "-Dash.kt", expected=1)
    assert result.stdout.splitlines() == ["Alpha Space.kt"]


def test_refresh_only_drops_entries(caller: Caller) -> None:
    for name in ("Alpha.kt", "Gamma.kt", "Old.kt", "Zeta.kt"):
        caller.add(name, UNFORMATTED)
    caller.run("--bootstrap-baseline")
    (caller.root / "Alpha.kt").write_text(UNFORMATTED_EDIT)  # edited: exemption is stale
    caller.run("--write", "Gamma.kt")  # formatted: exemption is obsolete
    caller.git("rm", "-q", "-f", "--", "Old.kt")  # removed
    caller.add("NewBad.kt", UNFORMATTED)  # new: must never be exempted

    caller.run("--update-baseline")

    assert caller.entries() == [caller.entry("Zeta.kt")]
    assert caller.run("Alpha.kt", "Gamma.kt", "NewBad.kt", "Zeta.kt", expected=1).stdout.splitlines() == [
        "Alpha.kt", "NewBad.kt"]


def test_failed_bootstrap_writes_nothing(caller: Caller) -> None:
    caller.add("Zeta.kt", UNFORMATTED)
    caller.add("Broken.kt", UNPARSEABLE)

    result = caller.run("--bootstrap-baseline", expected=1)

    assert "no baseline written" in result.stderr
    assert not (caller.root / "config").exists()


def test_failed_refresh_leaves_baseline_untouched(caller: Caller) -> None:
    fake_only(caller.toolchain)
    caller.add("Alpha.kt", UNFORMATTED)
    caller.add("Zeta.kt", UNFORMATTED)
    caller.run("--bootstrap-baseline")
    (caller.root / "Alpha.kt").write_text(UNFORMATTED_EDIT)
    before = (caller.root / BASELINE).read_bytes()

    # ktfmt failing on a baselined file must not shrink the baseline to a partial scan.
    caller.env["FAKE_KTFMT_FAIL_ON"] = "Zeta.kt"
    result = caller.run("--update-baseline", expected=1)

    assert "left unchanged" in result.stderr
    assert (caller.root / BASELINE).read_bytes() == before
    assert sorted(p.name for p in (caller.root / "config").iterdir()) == ["ktfmt-baseline.txt"]


@pytest.mark.parametrize(("mode", "command"), [
    ("check", "sed"), ("check", "sort"), ("bootstrap", "sort"), ("refresh", "sed")])
def test_failures_around_ktfmt_fail_closed(caller: Caller, mode: str, command: str) -> None:
    # A failing sed or sort stands in for any failure writing the argfile or
    # processing ktfmt's output, such as a full disk.
    caller.add("Alpha.kt", UNFORMATTED)
    caller.add("Zeta.kt", UNFORMATTED)
    before = b""
    if mode == "refresh":
        caller.run("--bootstrap-baseline")
        (caller.root / "Alpha.kt").write_text(UNFORMATTED_EDIT)  # a successful refresh would drop it
        before = (caller.root / BASELINE).read_bytes()
    caller.fail_command(command)

    args = {"check": ["Alpha.kt", "Zeta.kt"], "bootstrap": ["--bootstrap-baseline"],
            "refresh": ["--update-baseline"]}[mode]
    result = caller.run(*args, expected=1)

    assert f"{command}: injected failure" in result.stderr
    if mode == "refresh":
        assert (caller.root / BASELINE).read_bytes() == before
        assert sorted(p.name for p in (caller.root / "config").iterdir()) == ["ktfmt-baseline.txt"]
    else:
        assert not (caller.root / "config").exists()


def test_missing_file_fails_with_or_without_a_baseline(caller: Caller) -> None:
    # Given other files, ktfmt itself silently skips a path it cannot open.
    caller.add("Clean.kt", CLEAN)
    caller.add("Zeta.kt", UNFORMATTED)

    assert "no such file: Missing.kt" in caller.run("Clean.kt", "Missing.kt", expected=1).stderr
    assert "no such file: Missing.kt" in caller.run("--write", "Clean.kt", "Missing.kt", expected=1).stderr
    caller.run("--bootstrap-baseline")
    assert "no such file: Missing.kt" in caller.run("Zeta.kt", "Missing.kt", expected=1).stderr


def test_non_utf8_name_fails_closed(caller: Caller) -> None:
    name = b"Bad\xff.kt"
    try:
        with open(os.path.join(os.fsencode(caller.root), name), "wb") as file:
            file.write(UNFORMATTED.encode())
    except OSError:  # macOS APFS refuses names that are not UTF-8
        created = False
    else:
        created = True
        subprocess.run([b"git", b"add", b"--", name], cwd=caller.root, env=caller.env, check=True)

    assert "not valid UTF-8" in caller.run(name, expected=1).stderr
    if created:
        assert "not valid UTF-8" in caller.run("--bootstrap-baseline", expected=1).stderr
        assert not (caller.root / "config").exists()
    if caller.toolchain.name == "fake":
        assert caller.toolchain.ktfmt_calls() == []


def utf8_locale() -> str:
    available = subprocess.run(["locale", "-a"], text=True, capture_output=True, check=True).stdout.split()
    names = [name for name in ("C.UTF-8", "C.utf8", "en_US.UTF-8", "en_US.utf8") if name in available]
    if not names:
        pytest.skip("no UTF-8 locale is installed")
    return names[0]


def test_non_ascii_names_need_a_utf8_locale(caller: Caller) -> None:
    name = "Ünï 한글.kt"
    caller.add(name, UNFORMATTED)
    caller.env["LC_ALL"] = utf8_locale()

    assert caller.run(name, expected=1).stdout.splitlines() == [name]
    caller.run("--bootstrap-baseline")
    assert caller.entries() == [caller.entry(name)]
    caller.run(name)

    # Without a UTF-8 locale the JVM may miss the file or mangle its name.
    (caller.root / name).write_text(UNFORMATTED_EDIT)  # a successful refresh would drop the entry
    before = (caller.root / BASELINE).read_bytes()
    caller.env["LC_ALL"] = "C"
    assert "UTF-8 locale" in caller.run(name, expected=1).stderr
    assert "UTF-8 locale" in caller.run("--update-baseline", expected=1).stderr
    assert (caller.root / BASELINE).read_bytes() == before


def test_bootstrap_refuses_to_overwrite_a_baseline(caller: Caller) -> None:
    caller.add("Zeta.kt", UNFORMATTED)
    caller.run("--bootstrap-baseline")
    before = (caller.root / BASELINE).read_bytes()
    caller.add("NewBad.kt", UNFORMATTED)

    result = caller.run("--bootstrap-baseline", expected=1)

    assert "refuses to overwrite" in result.stderr
    assert (caller.root / BASELINE).read_bytes() == before


def test_custom_baseline_path(caller: Caller) -> None:
    caller.add("Zeta.kt", UNFORMATTED)

    caller.run("--baseline", "lint/ktfmt.txt", "--bootstrap-baseline")

    assert caller.entries("lint/ktfmt.txt") == [caller.entry("Zeta.kt")]
    caller.run("--baseline", "lint/ktfmt.txt", "Zeta.kt")
    caller.run("Zeta.kt", expected=1)  # the default path has no baseline


def test_undeclared_ktfmt_fails_instead_of_floating(caller: Caller) -> None:
    fake_only(caller.toolchain)
    caller.add("Zeta.kt", UNFORMATTED)
    caller.env["FAKE_KTFMT_VERSION"] = ""

    result = caller.run("Zeta.kt", expected=1)

    assert f"does not declare {TOOL}" in result.stderr
    assert caller.toolchain.ktfmt_calls() == []


def test_unsafe_invocations_are_rejected(caller: Caller) -> None:
    fake_only(caller.toolchain)
    caller.add("pkg/Zeta.kt", UNFORMATTED)

    assert "repository root" in caller.run("Zeta.kt", expected=1, cwd=caller.root / "pkg").stderr
    assert "newline" in caller.run("bad\nname.kt", expected=1).stderr
    assert "unknown option" in caller.run("--frobnicate", expected=2).stderr
    assert "takes no file arguments" in caller.run("--update-baseline", "pkg/Zeta.kt", expected=2).stderr


def pre_commit_command(env: dict[str, str]) -> list[str]:
    # A developer run may skip without pre-commit; an integration run must not pass without it.
    unavailable = pytest.fail if os.environ.get("KTFMT_INTEGRATION") == "1" else pytest.skip
    if shutil.which("pre-commit", path=env["PATH"]) is None:
        unavailable("pre-commit is not installed")
    result = subprocess.run(["pre-commit", "--version"], env=env, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        unavailable(f"pre-commit --version failed: {result.stderr.strip()}")
    version = result.stdout
    found = tuple(int(part) for part in re.findall(r"\d+", version)[:3])
    if found < (4, 4, 0):
        unavailable(f"unsupported_script needs pre-commit 4.4.0, found {version.strip()}")
    return ["pre-commit"]


@pytest.fixture(scope="session")
def hook_repo(tmp_path_factory: pytest.TempPathFactory, base_env: dict[str, str]) -> tuple[Path, str]:
    """A committed copy of this repository's hook files, as a caller would fetch them."""
    root = tmp_path_factory.mktemp("hook-repo")
    shutil.copy2(REPO / ".pre-commit-hooks.yaml", root / ".pre-commit-hooks.yaml")
    (root / "ktfmt").mkdir()
    shutil.copy2(SCRIPT, root / "ktfmt" / "ktfmt.sh")
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "Add ktfmt hooks"]):
        subprocess.run(["git", *args], cwd=root, env=base_env, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, env=base_env, text=True, check=True,
                         capture_output=True).stdout.strip()
    return root, sha


@pytest.fixture
def adopter(caller: Caller, hook_repo: tuple[Path, str]) -> Caller:
    repo, sha = hook_repo
    hooks = "".join(f"      - id: {hook}\n" for hook in (
        "ktfmt", "ktfmt-write", "ktfmt-baseline-refresh", "ktfmt-baseline-bootstrap"))
    caller.add(".pre-commit-config.yaml", f"repos:\n  - repo: {repo}\n    rev: {sha}\n    hooks:\n{hooks}")
    return caller


def pre_commit(caller: Caller, *args: str, expected: int = 0) -> str:
    command = pre_commit_command(caller.env)
    result = subprocess.run([*command, "run", "--color", "never", *args], cwd=caller.root, env=caller.env,
                            text=True, capture_output=True, check=False)
    assert result.returncode == expected, result.stdout + result.stderr
    return result.stdout + result.stderr


def test_remote_hook_runs_from_its_clone_in_the_caller_root(adopter: Caller, pre_commit_home: Path) -> None:
    adopter.add("Clean.kt", CLEAN)
    adopter.add("sub dir/Bad Space.kt", UNFORMATTED)

    output = pre_commit(adopter, "ktfmt", "--files", "sub dir/Bad Space.kt", expected=1)
    assert "sub dir/Bad Space.kt" in output
    pre_commit(adopter, "ktfmt", "--files", "Clean.kt")

    if adopter.toolchain.name == "fake":
        calls = adopter.toolchain.mise_calls()
        # mise reads the caller's configuration: every call runs in the caller root.
        assert {os.path.realpath(cwd) for cwd, _ in calls} == {os.path.realpath(adopter.root)}
        script = next(args for _, args in calls if args.startswith("exec ")).split(" ")[3]
        assert os.path.realpath(script).startswith(os.path.realpath(pre_commit_home))
        assert not os.path.realpath(script).startswith(str(REPO))


def test_option_like_file_name_is_checked_not_parsed(adopter: Caller) -> None:
    name = "--baseline=Bad.kt"
    adopter.add(name, UNFORMATTED)

    # pre-commit's own parser needs the "./"; it hands the hook the root-relative name.
    output = pre_commit(adopter, "ktfmt", "--files", f"./{name}", expected=1)

    assert name in output
    assert "not ktfmt-formatted" in output
    if adopter.toolchain.name == "fake":
        assert any(args.endswith(f" {name}") for _, args in adopter.toolchain.mise_calls() if args.startswith("exec "))


def test_manual_hooks_run_only_when_requested(adopter: Caller) -> None:
    adopter.add("Clean.kt", CLEAN)
    adopter.add("Zeta.kt", UNFORMATTED)

    output = pre_commit(adopter, "--all-files", expected=1)
    assert "Zeta.kt" in output
    assert not (adopter.root / "config").exists()
    assert (adopter.root / "Zeta.kt").read_text() == UNFORMATTED

    pre_commit(adopter, "ktfmt-baseline-bootstrap", "--hook-stage", "manual", "--all-files")
    assert adopter.entries() == [adopter.entry("Zeta.kt")]
    adopter.git("add", "--", BASELINE)
    pre_commit(adopter, "--all-files")

    # Hooks that rewrite tracked files fail so the change gets reviewed.
    pre_commit(adopter, "ktfmt-write", "--hook-stage", "manual", "--files", "Zeta.kt", expected=1)
    assert (adopter.root / "Zeta.kt").read_text() == FORMATTED
    adopter.git("add", "--", "Zeta.kt")
    output = pre_commit(adopter, "ktfmt-baseline-refresh", "--hook-stage", "manual", "--all-files", expected=1)
    assert "files were modified by this hook" in output
    assert adopter.entries() == []
