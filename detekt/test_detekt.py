"""Tests for detekt.sh and the detekt hook in .pre-commit-hooks.yaml.

Every scenario runs against a fake toolchain (stand-ins for mise and
detekt-cli), so only git, bash and pre-commit >= 4.4.0 are needed.
DETEKT_INTEGRATION=1 also runs the shared scenarios with the real mise, java
and detekt-cli: the throwaway caller repository gets the mise.toml that
README.md shows, so the run proves that those lines put detekt-cli on PATH.
DETEKT_INTEGRATION=path is for a machine without mise: the real detekt-cli
found on PATH (it needs java), with mise still a stand-in.

Run from the repository root:
    uv run --with pytest python -m pytest detekt -q
    DETEKT_INTEGRATION=1 uv run --with pytest python -m pytest detekt -q
    DETEKT_INTEGRATION=path uv run --with pytest python -m pytest detekt -q
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "detekt" / "detekt.sh"
CONFIG = "config/detekt.yml"
BASELINE = "config/detekt-baseline.xml"
FINDINGS = 2  # detekt-cli's exit status when it reports findings

INTEGRATION = os.environ.get("DETEKT_INTEGRATION", "")  # "1": real mise; "path": real detekt-cli on PATH

CONFIG_YML = "style:\n  MagicNumber:\n    active: false\n"
# A second configuration that turns off every rule SMELLY breaks.
RELAXED_YML = ("empty-blocks:\n  EmptyCatchBlock:\n    active: false\n"
               "exceptions:\n  TooGenericExceptionCaught:\n    active: false\n  SwallowedException:\n    active: false\n")
CLEAN = "fun clean(value: Int): Int = value + 1\n"
# detekt's default rules report the empty catch block; the fake keys on the catch clause.
SMELLY = 'fun swallow() {\n    try {\n        println("x")\n    } catch (e: Exception) {\n    }\n}\n'

# Stands in for detekt-cli: records its arguments and reports SMELLY files that
# no baseline names, unless a configuration mentions the rule (RELAXED_YML).
FAKE_DETEKT = r'''
import json, os, re, sys

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
relaxed = any("EmptyCatchBlock" in open(path).read() for path in re.split("[,;]", options["--config"]))
status = int(os.environ.get("FAKE_DETEKT_EXIT", "0"))
for name in options["--input"].split(","):
    if not os.path.isfile(name):
        sys.exit(f"Input path does not exist: {name}")
    if "catch (e: Exception)" in open(name).read() and os.path.basename(name) not in baseline and not relaxed:
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
    logs: Path | None = None  # where the stand-in mise and detekt-cli record their calls
    mise_toml: str | None = None  # the real mise reads the caller's configuration

    def mise_calls(self) -> list[tuple[str, str]]:
        assert self.logs is not None
        path = self.logs / "mise.log"
        lines = path.read_text().splitlines() if path.exists() else []
        return [tuple(line.split("\t", 1)) for line in lines]  # type: ignore[misc]

    def detekt_calls(self) -> list[list[str]]:
        assert self.logs is not None
        path = self.logs / "detekt.log"
        return [json.loads(line)["args"] for line in path.read_text().splitlines()] if path.exists() else []


def readme_mise_toml() -> str:
    """The mise.toml that README.md tells a caller to write."""
    [block] = re.findall(r"```toml\n(.*?)```", (REPO / "detekt" / "README.md").read_text(), re.DOTALL)
    return block


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
    if request.param == "real":
        if INTEGRATION not in ("1", "path"):
            pytest.skip("the real detekt-cli runs only with DETEKT_INTEGRATION=1 or DETEKT_INTEGRATION=path")
        if INTEGRATION == "1":
            if shutil.which("mise", path=base_env["PATH"]) is None:
                pytest.fail("DETEKT_INTEGRATION=1 needs mise on PATH; use DETEKT_INTEGRATION=path without it")
            return Toolchain("real", dict(base_env), mise_toml=readme_mise_toml())
        if shutil.which("detekt-cli", path=base_env["PATH"]) is None:
            pytest.fail("DETEKT_INTEGRATION=path needs detekt-cli on PATH")
    fake = tmp_path / "fake"
    for directory in ("bin", "detekt", "logs"):
        (fake / directory).mkdir(parents=True)
    executable(fake / "bin" / "mise", FAKE_MISE)
    env = dict(base_env)
    env.update(PATH=f"{fake / 'bin'}{os.pathsep}{base_env['PATH']}", FAKE_MISE_LOG=str(fake / "logs" / "mise.log"))
    if request.param == "real":
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
        self.env = {**toolchain.env, "MISE_TRUSTED_CONFIG_PATHS": str(root)}
        self.git("init", "-q")
        if toolchain.mise_toml:
            (root / "mise.toml").write_text(toolchain.mise_toml)
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
            mise = ["mise", "exec", "--"] if self.toolchain.mise_toml else []
            subprocess.run([*mise, "detekt-cli", "--build-upon-default-config", "--config", config,
                            "--baseline", baseline, "--create-baseline", "--input", ",".join(files)],
                           cwd=self.root, env=self.env, capture_output=True, check=False)
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


def test_several_configs_are_all_applied(caller: Caller) -> None:
    caller.add("Smelly.kt", SMELLY)
    caller.add("config/detekt-relaxed.yml", RELAXED_YML)
    both = f"{CONFIG},config/detekt-relaxed.yml"

    caller.run("Smelly.kt", expected=FINDINGS)
    # The second file turns the reported rules off, so detekt read both.
    caller.run("--config", both, "Smelly.kt")
    caller.run("--config", f"config/detekt-relaxed.yml;{CONFIG}", "Smelly.kt")

    result = caller.run("--config", f"{both},config/missing.yml", "Smelly.kt", expected=1)
    assert "no detekt configuration at config/missing.yml" in result.stderr
    if caller.toolchain.name == "fake":
        assert [call[1:3] for call in caller.toolchain.detekt_calls()] == [
            ["--config", CONFIG], ["--config", both], ["--config", f"config/detekt-relaxed.yml;{CONFIG}"]]


def empty_jar(caller: Caller, name: str) -> None:
    """A jar that holds no rule set: detekt loads it and finds nothing to add."""
    path = caller.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as jar:
        jar.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")


def test_plugin_jars_are_passed_to_detekt(caller: Caller) -> None:
    caller.add("Clean.kt", CLEAN)
    caller.add("Smelly.kt", SMELLY)
    empty_jar(caller, "config/detekt-plugins/rules-a.jar")
    empty_jar(caller, "config/detekt-plugins/rules-b.jar")
    plugins = "config/detekt-plugins/rules-a.jar,config/detekt-plugins/rules-b.jar"

    caller.run("--plugins", plugins, "Clean.kt")
    caller.run("--plugins", plugins, "Smelly.kt", expected=FINDINGS)

    missing = caller.run("--plugins", f"{plugins};config/detekt-plugins/missing.jar", "Clean.kt", expected=1)
    assert "no detekt plugin jar at config/detekt-plugins/missing.jar" in missing.stderr
    if caller.toolchain.name == "fake":
        assert caller.toolchain.detekt_calls() == [
            ["--build-upon-default-config", "--config", CONFIG, "--plugins", plugins, "--input", "Clean.kt"],
            ["--build-upon-default-config", "--config", CONFIG, "--plugins", plugins, "--input", "Smelly.kt"]]


def test_default_config_can_be_left_out(caller: Caller) -> None:
    fake_only(caller.toolchain)
    caller.add("Clean.kt", CLEAN)

    caller.run("--no-build-upon-default-config", "Clean.kt")

    assert caller.toolchain.detekt_calls() == [["--config", CONFIG, "--input", "Clean.kt"]]


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


@pytest.mark.parametrize(("args", "message"), [
    # The next option would be swallowed as the value, and detekt would fail on it with a stack trace.
    (["--jvm-target", "--language-version", "2.0"], "--jvm-target needs a value, not the option --language-version"),
    (["--baseline", "-baseline.xml"], "--baseline needs a value, not the option -baseline.xml"),
    (["--config", ","], "--config has an empty entry"),
    (["--config", "config/detekt.yml,"], "--config has an empty entry"),
    (["--config", ";config/detekt.yml"], "--config has an empty entry"),
    (["--plugins", "a.jar,,b.jar"], "--plugins has an empty entry"),
])
def test_option_values_detekt_would_misread_are_rejected(caller: Caller, args: list[str], message: str) -> None:
    fake_only(caller.toolchain)
    caller.add("Clean.kt", CLEAN)

    assert message in caller.run(*args, "Clean.kt", expected=2).stderr
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

    assert "detekt-cli is not on PATH inside `mise exec`" in result.stderr
    assert "mise.toml" in result.stderr


def test_readme_shows_the_mise_configuration_the_integration_run_uses() -> None:
    # DETEKT_INTEGRATION=1 writes this block as the caller's mise.toml.
    block = readme_mise_toml()
    assert block.count("[tools]") == 1
    assert re.search(r'^java = "', block, re.MULTILINE) and "detekt" in block


def pre_commit_command(env: dict[str, str]) -> list[str]:
    # A developer run may skip without pre-commit; an integration run must not pass without it.
    unavailable = pytest.fail if INTEGRATION in ("1", "path") else pytest.skip
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

    if caller.toolchain.logs:
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
