"""Tests for detekt.sh and the detekt hook in .pre-commit-hooks.yaml.

Every scenario runs against a fake toolchain (stand-ins for mise and
detekt-cli), so only git, bash and pre-commit >= 4.4.0 are needed.
DETEKT_INTEGRATION=1 also runs the shared scenarios with the real detekt-cli
found on PATH, which needs java. mise stays a stand-in there: the hook only
asks it to run a command, and the ktfmt tests cover the real one.

Run from the repository root:
    uv run --with pytest python -m pytest detekt -q
    DETEKT_INTEGRATION=1 uv run --with pytest python -m pytest detekt -q
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
SCRIPT = REPO / "detekt" / "detekt.sh"
CONFIG = "config/detekt.yml"
BASELINE = "config/detekt-baseline.xml"
FINDINGS = 2  # detekt-cli's exit status when it reports findings

CONFIG_YML = "style:\n  MagicNumber:\n    active: false\n"
CLEAN = "fun clean(value: Int): Int = value + 1\n"
# detekt's default rules report the empty catch block; the fake keys on the catch clause.
SMELLY = 'fun swallow() {\n    try {\n        println("x")\n    } catch (e: Exception) {\n    }\n}\n'

# Stands in for detekt-cli: records its arguments and reports SMELLY files that no baseline names.
FAKE_DETEKT = r'''
import json, os, sys

ARITY = {"--build-upon-default-config": 0, "--config": 1, "--baseline": 1, "--plugins": 1, "--jvm-target": 1,
         "--language-version": 1, "--input": 1}
args = sys.argv[1:]
with open(os.environ["FAKE_DETEKT_LOG"], "a") as log:
    log.write(json.dumps({"cwd": os.getcwd(), "args": args}) + "\n")
options, index = {}, 0
while index < len(args):
    if args[index] not in ARITY:
        sys.exit(f"fake detekt-cli: unexpected argument {args[index]}")
    count = ARITY[args[index]]
    options[args[index]] = args[index + 1] if count else True
    index += 1 + count
baseline = open(options["--baseline"]).read() if "--baseline" in options else ""
status = int(os.environ.get("FAKE_DETEKT_EXIT", "0"))
for name in options["--input"].split(","):
    if not os.path.isfile(name):
        sys.exit(f"Input path does not exist: {name}")
    if "catch (e: Exception)" in open(name).read() and os.path.basename(name) not in baseline:
        print(f"{name}:4:7: Empty catch block detected. [EmptyCatchBlock]")
        status = status or 2
sys.exit(status)
'''

# Stands in for mise: `exec` runs the command, with the fake detekt-cli on PATH when there is one.
FAKE_MISE = r'''#!/usr/bin/env bash
set -euo pipefail
printf '%s\t%s\n' "$PWD" "$*" >>"$FAKE_MISE_LOG"
case "${1-}" in
  exec)
    shift
    if [ "${1-}" = -- ]; then shift; fi
    PATH="${FAKE_DETEKT_DIR:+$FAKE_DETEKT_DIR:}$PATH" exec "$@"
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
    logs: Path

    def mise_calls(self) -> list[tuple[str, str]]:
        path = self.logs / "mise.log"
        lines = path.read_text().splitlines() if path.exists() else []
        return [tuple(line.split("\t", 1)) for line in lines]  # type: ignore[misc]

    def detekt_calls(self) -> list[list[str]]:
        path = self.logs / "detekt.log"
        return [json.loads(line)["args"] for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture(scope="session")
def pre_commit_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("pre-commit-home")


@pytest.fixture(scope="session")
def base_env(pre_commit_home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "DETEKT_HOOK_IN_MISE"}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="detekt test",
        GIT_AUTHOR_EMAIL="detekt@example.invalid",
        GIT_COMMITTER_NAME="detekt test",
        GIT_COMMITTER_EMAIL="detekt@example.invalid",
        PRE_COMMIT_HOME=str(pre_commit_home),
    )
    return env


@pytest.fixture(params=["fake", "real"])
def toolchain(request: pytest.FixtureRequest, tmp_path: Path, base_env: dict[str, str]) -> Toolchain:
    fake = tmp_path / "fake"
    for directory in ("bin", "detekt", "logs"):
        (fake / directory).mkdir(parents=True)
    executable(fake / "bin" / "mise", FAKE_MISE)
    env = dict(base_env)
    env.update(PATH=f"{fake / 'bin'}{os.pathsep}{base_env['PATH']}", FAKE_MISE_LOG=str(fake / "logs" / "mise.log"))
    if request.param == "real":
        if os.environ.get("DETEKT_INTEGRATION") != "1":
            pytest.skip("the real detekt-cli runs only with DETEKT_INTEGRATION=1")
        if shutil.which("detekt-cli", path=base_env["PATH"]) is None:
            pytest.fail("DETEKT_INTEGRATION=1 needs detekt-cli on PATH")
        return Toolchain("real", env, fake / "logs")
    (fake / "detekt.py").write_text(FAKE_DETEKT)
    executable(fake / "detekt" / "detekt-cli", f'#!/bin/sh\nexec "{sys.executable}" "{fake / "detekt.py"}" "$@"\n')
    env.update(FAKE_DETEKT_DIR=str(fake / "detekt"), FAKE_DETEKT_LOG=str(fake / "logs" / "detekt.log"))
    return Toolchain("fake", env, fake / "logs")


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
        self.env = dict(toolchain.env)
        self.git("init", "-q")
        self.add(CONFIG, CONFIG_YML)

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.root, env=self.env, text=True,
                              capture_output=True, check=True).stdout

    def add(self, name: str, text: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self.git("add", "--", name)

    def run(self, *args: str, expected: int = 0, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["bash", str(SCRIPT), *args], cwd=cwd or self.root, env=self.env,
                                text=True, capture_output=True, check=False)
        assert result.returncode == expected, result.stdout + result.stderr
        return result

    def create_baseline(self, *files: str, config: str = CONFIG, baseline: str = BASELINE) -> None:
        """Record the current findings of `files`, as `detekt-cli --create-baseline` does."""
        (self.root / baseline).parent.mkdir(parents=True, exist_ok=True)
        if self.toolchain.name == "real":
            subprocess.run(["detekt-cli", "--build-upon-default-config", "--config", config, "--baseline", baseline,
                            "--create-baseline", "--input", ",".join(files)], cwd=self.root, env=self.env,
                           capture_output=True, check=False)
        else:
            ids = "".join(f"    <ID>EmptyCatchBlock:{Path(name).name}$catch (e: Exception) {{ }}</ID>\n" for name in files)
            (self.root / baseline).write_text(
                f"<SmellBaseline>\n  <ManuallySuppressedIssues/>\n  <CurrentIssues>\n{ids}  </CurrentIssues>\n"
                "</SmellBaseline>\n")
        assert Path(files[0]).name in (self.root / baseline).read_text()


@pytest.fixture
def caller(tmp_path: Path, toolchain: Toolchain) -> Caller:
    root = tmp_path / "caller"
    root.mkdir()
    return Caller(root, toolchain)


def test_hook_script_is_executable() -> None:
    # unsupported_script executes the entry directly from the hook clone.
    assert os.access(SCRIPT, os.X_OK)


def test_findings_fail_and_clean_files_pass(caller: Caller) -> None:
    caller.add("Clean.kt", CLEAN)
    caller.add("sub dir/Bad Space.kt", SMELLY)
    caller.add("-Dash.kt", SMELLY)

    caller.run("Clean.kt")
    result = caller.run("Clean.kt", "sub dir/Bad Space.kt", "-Dash.kt", expected=FINDINGS)

    output = result.stdout + result.stderr
    assert "EmptyCatchBlock" in output
    assert "Bad Space.kt" in output and "-Dash.kt" in output
    if caller.toolchain.name == "fake":
        # No baseline exists, so none is passed. The dash-led path is passed as
        # ./-Dash.kt: a value starting with "-" could be read as an option.
        assert caller.toolchain.detekt_calls()[-1] == [
            "--build-upon-default-config", "--config", CONFIG, "--input", "Clean.kt,sub dir/Bad Space.kt,./-Dash.kt"]


def test_baseline_exempts_recorded_findings_only(caller: Caller) -> None:
    caller.add("Legacy.kt", SMELLY)
    caller.create_baseline("Legacy.kt")
    caller.add("New.kt", SMELLY)

    caller.run("Legacy.kt")
    result = caller.run("Legacy.kt", "New.kt", expected=FINDINGS)

    output = result.stdout + result.stderr
    assert "New.kt" in output and "Legacy.kt" not in output
    if caller.toolchain.name == "fake":
        assert caller.toolchain.detekt_calls()[-1] == [
            "--build-upon-default-config", "--config", CONFIG, "--baseline", BASELINE, "--input", "Legacy.kt,New.kt"]


def test_custom_config_and_baseline_paths(caller: Caller) -> None:
    caller.git("mv", CONFIG, "detekt.yml")
    (caller.root / "config").rmdir()
    caller.add("Legacy.kt", SMELLY)
    caller.create_baseline("Legacy.kt", config="detekt.yml", baseline="lint/baseline.xml")

    caller.run("--config", "detekt.yml", "--baseline", "lint/baseline.xml", "Legacy.kt")

    caller.run("--config", "detekt.yml", "Legacy.kt", expected=FINDINGS)  # the default path has no baseline
    assert f"no detekt configuration at {CONFIG}" in caller.run("Legacy.kt", expected=1).stderr
    if caller.toolchain.name == "fake":
        assert caller.toolchain.detekt_calls() == [
            ["--build-upon-default-config", "--config", "detekt.yml", "--baseline", "lint/baseline.xml",
             "--input", "Legacy.kt"],
            ["--build-upon-default-config", "--config", "detekt.yml", "--input", "Legacy.kt"]]


def test_compiler_options_are_passed_only_when_given(caller: Caller) -> None:
    caller.add("Clean.kt", CLEAN)

    caller.run("--jvm-target", "17", "--language-version", "2.0", "Clean.kt")

    if caller.toolchain.name == "fake":
        assert caller.toolchain.detekt_calls() == [
            ["--build-upon-default-config", "--config", CONFIG, "--jvm-target", "17", "--language-version", "2.0",
             "--input", "Clean.kt"]]


def test_plugins_and_a_standalone_config(caller: Caller) -> None:
    fake_only(caller.toolchain)
    caller.add("Clean.kt", CLEAN)
    caller.add("config/detekt-plugins/rules-a.jar", "jar")
    caller.add("config/detekt-plugins/rules-b.jar", "jar")
    plugins = "config/detekt-plugins/rules-a.jar,config/detekt-plugins/rules-b.jar"

    caller.run("--plugins", plugins, "--no-build-upon-default-config", "Clean.kt")

    assert caller.toolchain.detekt_calls() == [["--config", CONFIG, "--plugins", plugins, "--input", "Clean.kt"]]
    missing = caller.run("--plugins", f"{plugins};config/detekt-plugins/missing.jar", "Clean.kt", expected=1)
    assert "no detekt plugin jar at config/detekt-plugins/missing.jar" in missing.stderr
    assert len(caller.toolchain.detekt_calls()) == 1


def test_several_configs_must_all_exist(caller: Caller) -> None:
    fake_only(caller.toolchain)
    caller.add("Clean.kt", CLEAN)
    caller.add("config/detekt-compose.yml", CONFIG_YML)

    caller.run("--config", f"{CONFIG},config/detekt-compose.yml", "Clean.kt")

    assert caller.toolchain.detekt_calls()[-1][1:3] == ["--config", f"{CONFIG},config/detekt-compose.yml"]
    result = caller.run("--config", f"{CONFIG},config/missing.yml", "Clean.kt", expected=1)
    assert "no detekt configuration at config/missing.yml" in result.stderr


def test_named_baseline_must_exist(caller: Caller) -> None:
    fake_only(caller.toolchain)
    caller.add("Clean.kt", CLEAN)

    result = caller.run("--baseline", "lint/baseline.xml", "Clean.kt", expected=1)

    assert "no detekt baseline at lint/baseline.xml" in result.stderr
    assert caller.toolchain.detekt_calls() == []


@pytest.mark.parametrize("name", ["a,b.kt", "a;b.kt", "sub,dir/A.kt"])
def test_paths_detekt_would_split_are_rejected(caller: Caller, name: str) -> None:
    fake_only(caller.toolchain)
    caller.add("Clean.kt", CLEAN)
    caller.add(name, SMELLY)

    result = caller.run("Clean.kt", name, expected=1)

    assert f"detekt cannot accept commas or semicolons in paths: {name}" in result.stderr
    assert caller.toolchain.detekt_calls() == []


def test_missing_file_fails(caller: Caller) -> None:
    fake_only(caller.toolchain)
    caller.add("Clean.kt", CLEAN)

    assert "no such file: Missing.kt" in caller.run("Clean.kt", "Missing.kt", expected=1).stderr
    assert caller.toolchain.detekt_calls() == []


def test_no_files_runs_nothing(caller: Caller) -> None:
    fake_only(caller.toolchain)

    # Without --input detekt would scan the whole repository.
    caller.run()
    caller.run("--jvm-target", "17", "--")

    assert caller.toolchain.detekt_calls() == []


def test_option_like_file_name_is_checked_not_parsed(caller: Caller) -> None:
    fake_only(caller.toolchain)
    name = "--config=Bad.kt"
    caller.add(name, SMELLY)

    result = caller.run(name, expected=FINDINGS)

    assert name in result.stdout
    assert caller.toolchain.detekt_calls()[-1][-2:] == ["--input", f"./{name}"]


def test_unsafe_invocations_are_rejected(caller: Caller) -> None:
    fake_only(caller.toolchain)
    caller.add("pkg/Clean.kt", CLEAN)

    assert "repository root" in caller.run("Clean.kt", expected=1, cwd=caller.root / "pkg").stderr
    assert "unknown option: --frobnicate" in caller.run("--frobnicate", "pkg/Clean.kt", expected=2).stderr
    assert "unknown option: --create-baseline" in caller.run("--create-baseline", "pkg/Clean.kt", expected=2).stderr
    assert "--config needs a value" in caller.run("--config", expected=2).stderr
    assert "--jvm-target needs a value" in caller.run("--jvm-target", "", "pkg/Clean.kt", expected=2).stderr
    assert caller.toolchain.detekt_calls() == []


def test_detekt_exit_status_is_propagated(caller: Caller) -> None:
    fake_only(caller.toolchain)
    caller.add("Clean.kt", CLEAN)
    caller.env["FAKE_DETEKT_EXIT"] = "3"  # detekt-cli's status for an invalid configuration

    caller.run("Clean.kt", expected=3)


def test_mise_runs_the_hook_in_the_caller_root(caller: Caller) -> None:
    fake_only(caller.toolchain)
    caller.add("Clean.kt", CLEAN)

    caller.run("Clean.kt")

    [(cwd, args)] = caller.toolchain.mise_calls()
    assert os.path.realpath(cwd) == os.path.realpath(caller.root)
    assert args == f"exec -- bash {SCRIPT} Clean.kt"


def test_missing_mise_fails(caller: Caller, tmp_path: Path) -> None:
    fake_only(caller.toolchain)
    caller.add("Clean.kt", CLEAN)
    bash = shutil.which("bash")
    assert bash is not None
    empty = tmp_path / "empty"
    empty.mkdir()

    result = subprocess.run([bash, str(SCRIPT), "Clean.kt"], cwd=caller.root, env={**caller.env, "PATH": str(empty)},
                            text=True, capture_output=True, check=False)

    assert result.returncode == 1
    assert "mise is not on PATH" in result.stderr


def test_missing_detekt_cli_fails_with_a_hint(caller: Caller) -> None:
    fake_only(caller.toolchain)
    caller.add("Clean.kt", CLEAN)
    # Drop the fake and any detekt-cli the machine itself has.
    caller.env.pop("FAKE_DETEKT_DIR")
    caller.env["PATH"] = os.pathsep.join(
        directory for directory in caller.env["PATH"].split(os.pathsep)
        if not os.path.exists(os.path.join(directory, "detekt-cli")))

    result = caller.run("Clean.kt", expected=1)

    assert "detekt-cli is not on PATH in the project's mise environment" in result.stderr
    assert "mise.toml" in result.stderr


def pre_commit_command(env: dict[str, str]) -> list[str]:
    # A developer run may skip without pre-commit; an integration run must not pass without it.
    unavailable = pytest.fail if os.environ.get("DETEKT_INTEGRATION") == "1" else pytest.skip
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
    (root / "detekt").mkdir()
    shutil.copy2(SCRIPT, root / "detekt" / "detekt.sh")
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "Add detekt hook"]):
        subprocess.run(["git", *args], cwd=root, env=base_env, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, env=base_env, text=True, check=True,
                         capture_output=True).stdout.strip()
    return root, sha


def adopt(caller: Caller, hook_repo: tuple[Path, str], args: str = "") -> None:
    repo, sha = hook_repo
    caller.add(".pre-commit-config.yaml",
               f"repos:\n  - repo: {repo}\n    rev: {sha}\n    hooks:\n      - id: detekt\n{args}")


def pre_commit(caller: Caller, *args: str, expected: int = 0) -> str:
    command = pre_commit_command(caller.env)
    result = subprocess.run([*command, "run", "--color", "never", *args], cwd=caller.root, env=caller.env,
                            text=True, capture_output=True, check=False)
    assert result.returncode == expected, result.stdout + result.stderr
    return result.stdout + result.stderr


def test_remote_hook_runs_from_its_clone_in_the_caller_root(
        caller: Caller, hook_repo: tuple[Path, str], pre_commit_home: Path) -> None:
    adopt(caller, hook_repo)
    caller.add("Clean.kt", CLEAN)
    caller.add("sub dir/Bad Space.kt", SMELLY)

    output = pre_commit(caller, "detekt", "--files", "sub dir/Bad Space.kt", expected=1)
    assert "Bad Space.kt" in output and "EmptyCatchBlock" in output
    pre_commit(caller, "detekt", "--files", "Clean.kt")

    calls = caller.toolchain.mise_calls()
    # mise reads the caller's configuration: every call runs in the caller root.
    assert {os.path.realpath(cwd) for cwd, _ in calls} == {os.path.realpath(caller.root)}
    script = calls[0][1].split(" ")[3]
    assert os.path.realpath(script).startswith(os.path.realpath(pre_commit_home))
    assert not os.path.realpath(script).startswith(str(REPO))


def test_hook_args_replace_the_defaults(caller: Caller, hook_repo: tuple[Path, str]) -> None:
    adopt(caller, hook_repo, "        args: [--config, detekt.yml, --baseline, lint/baseline.xml, --jvm-target, '17']\n")
    caller.git("mv", CONFIG, "detekt.yml")
    caller.add("Legacy.kt", SMELLY)
    caller.create_baseline("Legacy.kt", config="detekt.yml", baseline="lint/baseline.xml")
    caller.add("New.kt", SMELLY)

    pre_commit(caller, "detekt", "--files", "Legacy.kt")
    output = pre_commit(caller, "detekt", "--files", "Legacy.kt", "New.kt", expected=1)

    assert "New.kt" in output and "Legacy.kt" not in output
    if caller.toolchain.name == "fake":
        assert caller.toolchain.detekt_calls()[-1] == [
            "--build-upon-default-config", "--config", "detekt.yml", "--baseline", "lint/baseline.xml",
            "--jvm-target", "17", "--input", "Legacy.kt,New.kt"]


def test_hook_receives_kotlin_files_only(caller: Caller, hook_repo: tuple[Path, str]) -> None:
    fake_only(caller.toolchain)
    adopt(caller, hook_repo)
    caller.add("README.md", "# readme\n")
    caller.add("src/Clean.kt", CLEAN)
    caller.add("build.gradle.kts", "plugins {}\n")

    pre_commit(caller, "--all-files")

    [call] = caller.toolchain.detekt_calls()
    assert sorted(call[-1].split(",")) == ["build.gradle.kts", "src/Clean.kt"]
