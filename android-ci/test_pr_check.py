"""Tests of android-pr-check.yml and android-pr-check-jobs.yml.

The run steps are executed as the shell GitHub would run, against stand-ins for
gradlew and keytool. The wiring tests keep the copies that have to match in
step: the verify and release-build jobs, and the primary and fallback calls.
GitHub's expressions (job conditions, runs-on) are not evaluated here.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ENTRY = "android-pr-check.yml"
JOBS = "android-pr-check-jobs.yml"
EXAMPLE = ROOT / "android-ci" / "pr-check.yml.example"
OWN_ACTION = re.compile(r"^asleep-ai/actions/(?P<name>[^@/]+)@(?P<ref>.+)$")


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def trigger(workflow: dict) -> dict:
    # YAML 1.1 reads the key `on` as the boolean true.
    return workflow[True]["workflow_call"]


def step(job: dict, name: str) -> dict:
    matches = [candidate for candidate in job["steps"] if candidate.get("name") == name]
    assert len(matches) == 1, f"{name!r}: {len(matches)} steps"
    return matches[0]


def run_script(script: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a step's script the way `shell: bash` does."""
    environment = {"PATH": os.environ["PATH"], "HOME": str(cwd), **env}
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture(scope="module")
def entry() -> dict:
    return load(ENTRY)


@pytest.fixture(scope="module")
def jobs() -> dict:
    return load(JOBS)["jobs"]


# --- result -----------------------------------------------------------------

PASS = ("pass", "pass", "pass")
NONE = ("", "", "")


def result_env(labels, self_hosted, primary, primary_verdicts, fallback, fallback_verdicts) -> dict[str, str]:
    return {
        "RUNNER_LABELS": labels,
        "FALLBACK_RUNS_ON": "ubuntu-latest",
        "SELF_HOSTED": self_hosted,
        "PRIMARY": primary,
        "PRIMARY_CODE_STYLE": primary_verdicts[0],
        "PRIMARY_VERIFY": primary_verdicts[1],
        "PRIMARY_RELEASE_BUILD": primary_verdicts[2],
        "FALLBACK": fallback,
        "FALLBACK_CODE_STYLE": fallback_verdicts[0],
        "FALLBACK_VERIFY": fallback_verdicts[1],
        "FALLBACK_RELEASE_BUILD": fallback_verdicts[2],
    }


LABELS = '["self-hosted","Linux","X64"]'
RESULT_CASES = {
    # name: (env, exit status, text the log must contain, text it must not contain)
    "self-hosted pass": (
        result_env(LABELS, "true", "success", PASS, "skipped", NONE),
        0,
        "Passed on the self-hosted runners.",
        "::warning",
    ),
    "self-hosted unchecked pass": (
        result_env(LABELS, "", "success", PASS, "skipped", NONE),
        0,
        "Passed on the self-hosted runners.",
        "::warning",
    ),
    "runners offline": (
        result_env(LABELS, "false", "success", PASS, "skipped", NONE),
        0,
        "::warning title=Self-hosted runners offline::",
        "::error",
    ),
    "github-hosted only": (
        result_env("", "", "success", PASS, "skipped", NONE),
        0,
        "Passed on ubuntu-latest.",
        "::warning",
    ),
    "code failure is final": (
        result_env(LABELS, "true", "failure", ("pass", "code", "pass"), "skipped", NONE),
        1,
        "::error::Failed on the self-hosted runners (result=failure, code-style=pass, verify=code, release-build=pass).",
        "::warning",
    ),
    "infra failure passes on the re-run": (
        result_env(LABELS, "true", "failure", ("pass", "infra", "pass"), "success", PASS),
        0,
        "::warning title=Self-hosted runner problem::",
        "::error",
    ),
    "runner went away, re-run passes": (
        result_env(LABELS, "true", "failure", ("pass", "", "pass"), "success", PASS),
        0,
        "verify=none",
        "::error",
    ),
    "job timeout, re-run passes": (
        result_env(LABELS, "true", "cancelled", ("pass", "infra", "pass"), "success", PASS),
        0,
        "Passed on the re-run on ubuntu-latest.",
        "::error",
    ),
    "re-run fails too": (
        result_env(LABELS, "true", "failure", ("pass", "infra", "pass"), "failure", ("pass", "code", "pass")),
        1,
        "::error::Did not pass on the self-hosted runners",
        "Passed",
    ),
    "re-run cancelled": (
        result_env(LABELS, "true", "failure", ("pass", "infra", "pass"), "cancelled", ("pass", "", "")),
        1,
        "::error::Did not pass on the self-hosted runners",
        "Passed",
    ),
    "cancelled run": (
        result_env(LABELS, "true", "cancelled", NONE, "skipped", NONE),
        1,
        "::error::Did not finish on the self-hosted runners",
        "Passed",
    ),
    "nothing ran": (
        result_env(LABELS, "", "skipped", NONE, "skipped", NONE),
        1,
        "::error::Did not finish",
        "Passed",
    ),
    "github-hosted infra failure is not re-run": (
        result_env(LABELS, "false", "failure", ("pass", "infra", "pass"), "skipped", NONE),
        1,
        "::error::Failed on ubuntu-latest (no self-hosted runner was online)",
        "Passed",
    ),
    # The verdict speaks for the build step, the result for the job: neither alone is a pass.
    "pass verdicts with a failed result": (
        result_env("", "", "failure", PASS, "skipped", NONE),
        1,
        "::error::Failed on ubuntu-latest (result=failure",
        "Passed",
    ),
    "success without a verdict": (
        result_env(LABELS, "true", "success", ("pass", "", "pass"), "skipped", NONE),
        1,
        "succeeded without a pass verdict from every job",
        "Passed",
    ),
    "success with a code verdict": (
        result_env(LABELS, "true", "success", ("pass", "code", "pass"), "skipped", NONE),
        1,
        "succeeded without a pass verdict from every job",
        "Passed",
    ),
    "re-run success without a verdict": (
        result_env(LABELS, "true", "failure", ("pass", "infra", "pass"), "success", ("pass", "pass", "")),
        1,
        "The re-run on ubuntu-latest succeeded without a pass verdict from every job",
        "Passed",
    ),
}


@pytest.mark.parametrize("case", RESULT_CASES)
def test_result(entry, tmp_path, case):
    env, status, expected, unexpected = RESULT_CASES[case]
    summary = tmp_path / "summary.md"
    script = step(entry["jobs"]["result"], "Combine the runs")["run"]
    result = run_script(script, {**env, "GITHUB_STEP_SUMMARY": str(summary)}, tmp_path)
    output = result.stdout + result.stderr
    assert result.returncode == status, output
    assert expected in output
    assert unexpected not in output
    rows = summary.read_text()
    assert f"| primary | {env['PRIMARY'] or 'none'} |" in rows
    assert f"| fallback | {env['FALLBACK'] or 'none'} |" in rows


def test_result_reads_every_job_of_both_runs(entry):
    result = entry["jobs"]["result"]
    assert result["if"] == "${{ always() }}", "a skipped result job would count as a passing required check"
    assert result["needs"] == ["pick-runner", "primary", "fallback"]
    assert result["runs-on"] == "ubuntu-latest"
    env = step(result, "Combine the runs")["env"]
    for run in ("primary", "fallback"):
        assert env[run.upper()] == f"${{{{ needs.{run}.result }}}}"
        for job in ("code-style", "verify", "release-build"):
            name = f"{run}_{job}".upper().replace("-", "_")
            assert env[name] == f"${{{{ needs.{run}.outputs.{job}-verdict }}}}"


# --- checkout -----------------------------------------------------------------


def checkout_step(job: dict) -> dict:
    (checkout,) = [item for item in job["steps"] if item.get("id") == "checkout"]
    return checkout


@pytest.mark.parametrize("job", ["code-style", "verify", "release-build"])
def test_stale_git_locks_are_removed_before_checkout_on_self_hosted_runners(jobs, job):
    first, second = jobs[job]["steps"][:2]
    assert first["name"] == "Remove stale git lock files"
    assert first["if"] == "${{ runner.environment == 'self-hosted' }}"
    assert second is checkout_step(jobs[job])
    assert first["run"] == jobs["verify"]["steps"][0]["run"]


def test_stale_git_locks_of_the_repository_and_its_submodules_are_removed(jobs, tmp_path):
    workspace = tmp_path / "work folder"
    git = workspace / ".git"
    submodule = git / "modules" / "external" / "content assets"
    locks = [
        git / "index.lock",
        git / "shallow.lock",
        git / "refs" / "heads" / "main.lock",
        submodule / "index.lock",
        submodule / "config.lock",
        submodule / "modules" / "nested" / "HEAD.lock",
    ]
    kept = [
        git / "index",
        git / "config",
        submodule / "index",
        submodule / "HEAD",
        # Lock files of package managers are part of the checkout, not git's.
        workspace / "yarn.lock",
        workspace / "external" / "content assets" / "Podfile.lock",
    ]
    for path in locks + kept:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    # A directory is never a lock.
    (git / "modules" / "odd.lock").mkdir()
    script = jobs["verify"]["steps"][0]["run"]
    result = run_script(script, {"GITHUB_WORKSPACE": str(workspace)}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert [path for path in locks if path.exists()] == []
    assert [path for path in kept if not path.exists()] == []
    assert (git / "modules" / "odd.lock").is_dir()
    assert "Removing .git/modules/external/content assets/index.lock, left by an earlier job" in result.stdout
    assert result.stdout.count("Removing ") == len(locks)


def test_stale_git_lock_removal_does_nothing_in_a_fresh_work_folder(jobs, tmp_path):
    script = jobs["verify"]["steps"][0]["run"]
    for workspace in (tmp_path / "empty", tmp_path / "clean"):
        workspace.mkdir()
    (tmp_path / "clean" / ".git").mkdir()
    (tmp_path / "clean" / ".git" / "index").write_text("")
    for workspace in (tmp_path / "empty", tmp_path / "clean"):
        result = run_script(script, {"GITHUB_WORKSPACE": str(workspace)}, tmp_path)
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""


# --- the Gradle jobs' steps ---------------------------------------------------


@pytest.mark.parametrize(
    ("event", "code", "ref", "skip", "build", "reason"),
    [
        ("pull_request", "true", "refs/pull/1/merge", "", "true", "a file outside paths-ignore changed"),
        ("pull_request", "false", "refs/pull/1/merge", "", "false", "every changed file matches paths-ignore"),
        ("pull_request_target", "false", "refs/heads/main", "", "false", "every changed file matches paths-ignore"),
        ("push", "true", "refs/heads/develop", "", "true", "a file outside paths-ignore changed"),
        ("push", "false", "refs/heads/develop", "", "false", "every changed file matches paths-ignore"),
        # The filter did not run: no comparison is no reason to skip.
        ("workflow_dispatch", "", "refs/heads/develop", "develop", "true", "a workflow_dispatch run always builds"),
        ("schedule", "", "refs/heads/main", "", "true", "a schedule run always builds"),
        ("push", "true", "refs/heads/develop", "develop", "false", "skipped on a push to develop"),
        ("push", "true", "refs/heads/develop", "main, develop", "false", "skipped on a push to develop"),
        ("push", "true", "refs/heads/develop", "main,release", "true", "a file outside paths-ignore changed"),
        ("push", "true", "refs/heads/develop-2", "develop", "true", "a file outside paths-ignore changed"),
        ("push", "true", "refs/tags/develop", "develop", "true", "a file outside paths-ignore changed"),
        ("push", "true", "refs/heads/develop", "*", "true", "a file outside paths-ignore changed"),
        ("pull_request", "true", "refs/heads/develop", "develop", "true", "a file outside paths-ignore changed"),
    ],
)
def test_plan(jobs, tmp_path, event, code, ref, skip, build, reason):
    output = tmp_path / "output"
    env = {
        "EVENT_NAME": event,
        "CODE_CHANGED": code,
        "REF": ref,
        "SKIP_PUSH_BRANCHES": skip,
        "GITHUB_OUTPUT": str(output),
    }
    result = run_script(step(jobs["release-build"], "Decide whether to build")["run"], env, tmp_path)
    assert result.returncode == 0, result.stderr
    assert output.read_text() == f"build={build}\n"
    assert reason in result.stdout


def test_only_the_release_build_has_a_push_skip_list(jobs):
    assert step(jobs["verify"], "Decide whether to build")["env"]["SKIP_PUSH_BRANCHES"] == ""
    assert (
        step(jobs["release-build"], "Decide whether to build")["env"]["SKIP_PUSH_BRANCHES"]
        == "${{ inputs.release-build-skip-push-branches }}"
    )


def filters(jobs, tmp_path, paths_ignore: str) -> dict:
    output = tmp_path / "output"
    env = {"PATHS_IGNORE": paths_ignore, "GITHUB_OUTPUT": str(output)}
    result = run_script(step(jobs["verify"], "Prepare the path filter")["run"], env, tmp_path)
    assert result.returncode == 0, result.stderr
    lines = output.read_text().splitlines()
    assert lines[0] == "filters<<ANDROID_PR_CHECK_FILTERS"
    assert lines[-1] == "ANDROID_PR_CHECK_FILTERS"
    return yaml.safe_load("\n".join(lines[1:-1]))


def test_path_filter_negates_each_ignored_pattern(jobs, tmp_path):
    parsed = filters(jobs, tmp_path, "*.md\r\n  docs/**  \n\n.github/ISSUE_TEMPLATE/**\nit's here/**")
    assert parsed == {"code": ["**", "!*.md", "!docs/**", "!.github/ISSUE_TEMPLATE/**", "!it's here/**"]}


def test_path_filter_without_patterns_matches_every_file(jobs, tmp_path):
    assert filters(jobs, tmp_path, "") == {"code": ["**"]}


def test_path_filter_of_the_default_input(entry, jobs, tmp_path):
    default = trigger(entry)["inputs"]["paths-ignore"]["default"]
    parsed = filters(jobs, tmp_path, default)
    assert parsed["code"][0] == "**"
    assert "!*.md" in parsed["code"] and "!docs/**" in parsed["code"]
    assert len(parsed["code"]) == 1 + len(default.split())
    # Every pattern has to match for a file to count as code.
    assert step(jobs["verify"], "Detect code changes")["with"]["predicate-quantifier"] == "every"


KEYTOOL_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$KEYTOOL_ARGS"
while [ "$#" -gt 0 ]; do
  if [ "$1" = -keystore ]; then echo stub > "$2"; fi
  shift
done
"""


def write_local_properties(jobs, tmp_path, local_properties: str, real_keytool: bool = False):
    workspace = tmp_path / "workspace"
    temp = tmp_path / "temp"
    bin_directory = tmp_path / "bin"
    for directory in (workspace, temp, bin_directory):
        directory.mkdir()
    path = os.environ["PATH"]
    if not real_keytool:
        executable(bin_directory / "keytool", KEYTOOL_STUB)
        path = f"{bin_directory}{os.pathsep}{path}"
    env = {
        "PATH": path,
        "RUNNER_TEMP": str(temp),
        "LOCAL_PROPERTIES": local_properties,
        "KEYTOOL_ARGS": str(tmp_path / "keytool-args"),
    }
    script = step(jobs["verify"], "Write local.properties with a throwaway signing key")["run"]
    return run_script(script, env, workspace), workspace, temp


def test_local_properties_holds_the_signing_keys_and_the_caller_lines(jobs, tmp_path):
    caller = "# placeholders\nAPI_KEY=ci-placeholder\n\nEMPTY=\r\nBASE_URL=https://ci.placeholder.invalid/?a=b c\n"
    result, workspace, temp = write_local_properties(jobs, tmp_path, caller)
    assert result.returncode == 0, result.stderr
    keystore = temp / "android-pr-check.jks"
    assert keystore.is_file()
    assert (workspace / "local.properties").read_text().splitlines() == [
        f"KEYSTORE_FILE={keystore}",
        "KEYSTORE_PASSWORD=android-pr-check",
        "KEY_ALIAS=android-pr-check",
        "KEY_PASSWORD=android-pr-check",
        "API_KEY=ci-placeholder",
        "EMPTY=",
        "BASE_URL=https://ci.placeholder.invalid/?a=b c",
    ]
    arguments = (tmp_path / "keytool-args").read_text().splitlines()
    assert arguments[0] == "-genkeypair"
    assert arguments[arguments.index("-keystore") + 1] == str(keystore)
    assert arguments[arguments.index("-alias") + 1] == "android-pr-check"
    # PKCS12 has one password for the store and the key.
    assert arguments[arguments.index("-storepass") + 1] == arguments[arguments.index("-keypass") + 1]


def test_local_properties_without_caller_lines(jobs, tmp_path):
    result, workspace, _ = write_local_properties(jobs, tmp_path, "")
    assert result.returncode == 0, result.stderr
    names = [line.split("=", 1)[0] for line in (workspace / "local.properties").read_text().splitlines()]
    assert names == ["KEYSTORE_FILE", "KEYSTORE_PASSWORD", "KEY_ALIAS", "KEY_PASSWORD"]


@pytest.mark.parametrize(
    ("local_properties", "message"),
    [
        ("API_KEY=x\nJUST_A_NAME\n", "Line 2 is not NAME=value."),
        ("1BAD=x\n", "Line 1 is not NAME=value."),
        ("API KEY=x\n", "Line 1 is not NAME=value."),
        ("A=1\n\nKEY_ALIAS=mine\n", "Line 3 sets KEY_ALIAS, which this workflow writes itself."),
        ("KEYSTORE_FILE=/tmp/other.jks\n", "Line 1 sets KEYSTORE_FILE, which this workflow writes itself."),
    ],
)
def test_local_properties_rejects_other_lines_without_printing_them(jobs, tmp_path, local_properties, message):
    result, workspace, temp = write_local_properties(jobs, tmp_path, local_properties)
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert f"::error title=Invalid local-properties::{message}" in output
    for line in local_properties.splitlines():
        if line and line not in ("API_KEY=x", "A=1"):
            assert line not in output
    assert not (workspace / "local.properties").exists()
    assert not (temp / "android-pr-check.jks").exists()


def test_the_keytool_command_makes_a_key_the_written_properties_open(jobs, tmp_path):
    # ANDROID_CI_KEYTOOL=1 (CI) turns a missing JDK into a failure instead of a skip.
    required = os.environ.get("ANDROID_CI_KEYTOOL") == "1"
    if shutil.which("keytool") is None and not required:
        pytest.skip("no JDK on PATH")
    result, workspace, temp = write_local_properties(jobs, tmp_path, "", real_keytool=True)
    if "Unable to locate a Java Runtime" in result.stderr and not required:
        pytest.skip("keytool is the macOS placeholder without a JDK")
    assert result.returncode == 0, result.stderr
    properties = dict(line.split("=", 1) for line in (workspace / "local.properties").read_text().splitlines())
    assert properties["KEYSTORE_FILE"] == str(temp / "android-pr-check.jks")
    command = ["keytool", "-list", "-keystore", properties["KEYSTORE_FILE"]]
    command += ["-storepass", properties["KEYSTORE_PASSWORD"], "-alias", properties["KEY_ALIAS"]]
    listing = subprocess.run(command, text=True, capture_output=True, check=False)
    assert listing.returncode == 0, listing.stdout + listing.stderr
    assert "PrivateKeyEntry" in listing.stdout


GRADLEW_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$GRADLEW_ARGS"
echo "token=${GITHUB_TOKEN:-}"
echo "BUILD ${GRADLEW_OUTCOME}"
[ "$GRADLEW_OUTCOME" = SUCCESSFUL ]
"""


def run_gradle(jobs, tmp_path, job: str, runner: str, outcome: str = "SUCCESSFUL", **overrides):
    workspace = tmp_path / "workspace"
    temp = tmp_path / "temp"
    workspace.mkdir()
    temp.mkdir()
    # A file the task pattern below would match if the shell expanded globs.
    (workspace / "a.Foo").write_text("")
    executable(workspace / "gradlew", GRADLEW_STUB)
    build = step(jobs[job], "Run Gradle")
    task_input = "verify" if job == "verify" else "release-build"
    assert build["env"]["GRADLE_TASK"] == f"${{{{ inputs.{task_input}-task }}}}"
    arguments = build["env"]["GRADLE_ARGS"].replace(f"${{{{ inputs.{task_input}-args }}}}", overrides.pop("args", ""))
    assert "${{" not in arguments
    env = {
        "GRADLE_TASK": overrides.pop("task", "ciVerify" if job == "verify" else "ciReleaseBuild"),
        "GRADLE_ARGS": arguments,
        "GITHUB_ACTOR": "octocat",
        "GITHUB_TOKEN": "token-for-packages",
        "TOKEN_SOURCE": "GIT_TOKEN",
        "RUNNER_KIND": runner,
        "MAX_WORKERS": "32",
        "GRADLE_JVMARGS": "-Xmx16g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8",
        "KOTLIN_DAEMON_JVMARGS": "-Xmx6g",
        "RUNNER_TEMP": str(temp),
        "GRADLEW_ARGS": str(tmp_path / "gradlew-args"),
        "GRADLEW_OUTCOME": outcome,
        **overrides,
    }
    result = run_script(build["run"], env, workspace)
    return result, (tmp_path / "gradlew-args").read_text().splitlines(), temp / "gradle.log"


def test_gradle_on_a_github_hosted_runner_adds_nothing(jobs, tmp_path):
    result, arguments, log = run_gradle(jobs, tmp_path, "verify", "github-hosted")
    assert result.returncode == 0, result.stderr
    assert arguments == ["ciVerify", "--continue"]
    # gradle-log-verdict reads the copy; the job log shows the same lines after the step's own.
    assert result.stdout.endswith(log.read_text())
    assert "BUILD SUCCESSFUL" in log.read_text()
    assert "token=token-for-packages" in log.read_text()


@pytest.mark.parametrize("job", ["verify", "release-build"])
def test_gradle_gets_git_token_and_the_log_names_the_token_source(jobs, tmp_path, job):
    env = step(jobs[job], "Run Gradle")["env"]
    # Gradle reads GITHUB_ACTOR and GITHUB_TOKEN as GitHub Packages credentials.
    assert env["GITHUB_TOKEN"] == "${{ secrets.GIT_TOKEN || github.token }}"
    assert env["TOKEN_SOURCE"].startswith("${{ secrets.GIT_TOKEN != '' && 'GIT_TOKEN' || 'the job token")
    result, _, log = run_gradle(jobs, tmp_path, job, "github-hosted")
    line = "GitHub Packages credentials for Gradle: GITHUB_ACTOR=octocat, GITHUB_TOKEN from GIT_TOKEN.\n"
    assert result.stdout.startswith(line)
    # The token itself is printed only by the gradlew stand-in.
    assert "token-for-packages" not in line and line not in log.read_text()


def test_gradle_on_a_self_hosted_runner_limits_workers_and_memory(jobs, tmp_path):
    result, arguments, _ = run_gradle(jobs, tmp_path, "verify", "self-hosted", args="--init-script scripts/x.gradle.kts")
    assert result.returncode == 0, result.stderr
    assert arguments == [
        "ciVerify",
        "--continue",
        "--init-script",
        "scripts/x.gradle.kts",
        "--no-daemon",
        "--max-workers=32",
        "-Dorg.gradle.jvmargs=-Xmx16g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8",
        "-Pkotlin.daemon.jvmargs=-Xmx6g",
    ]


def test_gradle_on_a_self_hosted_runner_with_empty_memory_inputs(jobs, tmp_path):
    result, arguments, _ = run_gradle(
        jobs, tmp_path, "release-build", "self-hosted", MAX_WORKERS="", GRADLE_JVMARGS="", KOTLIN_DAEMON_JVMARGS=""
    )
    assert result.returncode == 0, result.stderr
    assert arguments == ["ciReleaseBuild", "--no-daemon"]


def test_release_build_does_not_continue_after_a_failure(jobs, tmp_path):
    result, arguments, _ = run_gradle(jobs, tmp_path, "release-build", "github-hosted", args="-Pname=value")
    assert result.returncode == 0, result.stderr
    assert arguments == ["ciReleaseBuild", "-Pname=value"]


def test_gradle_arguments_are_split_but_not_expanded(jobs, tmp_path):
    result, arguments, _ = run_gradle(
        jobs, tmp_path, "verify", "github-hosted", task="ciVerify  :app:lintDebug", args="--tests *.Foo"
    )
    assert result.returncode == 0, result.stderr
    assert arguments == ["ciVerify", ":app:lintDebug", "--continue", "--tests", "*.Foo"]


def test_a_failed_gradle_run_fails_the_step_despite_the_pipe(jobs, tmp_path):
    result, _, log = run_gradle(jobs, tmp_path, "verify", "self-hosted", outcome="FAILED")
    assert result.returncode != 0
    assert "BUILD FAILED" in log.read_text()


# --- wiring -------------------------------------------------------------------


def test_verify_and_release_build_differ_only_where_they_must(jobs):
    verify, release = jobs["verify"]["steps"], jobs["release-build"]["steps"]
    assert [item.get("name") for item in verify] == [item.get("name") for item in release]
    differing = {}
    for ours, theirs in zip(verify, release):
        if ours != theirs:
            differing[ours["name"]] = {key for key in ours.keys() | theirs.keys() if ours.get(key) != theirs.get(key)}
    assert differing == {
        "Decide whether to build": {"env"},
        "Run Gradle": {"env"},
        "Upload the test and lint reports": {"with"},
    }
    build_env = step(jobs["verify"], "Run Gradle")["env"], step(jobs["release-build"], "Run Gradle")["env"]
    assert {key for key in build_env[0] if build_env[0][key] != build_env[1][key]} == {"GRADLE_TASK", "GRADLE_ARGS"}
    uploads = [step(jobs[job], "Upload the test and lint reports")["with"] for job in ("verify", "release-build")]
    assert {key for key in uploads[0] if uploads[0][key] != uploads[1][key]} == {"name"}
    # Both runs upload in one workflow run, so the names must not collide.
    assert uploads[0]["name"] == "android-pr-check-verify-reports-${{ inputs.attempt }}"
    assert uploads[1]["name"] == "android-pr-check-release-build-reports-${{ inputs.attempt }}"


@pytest.mark.parametrize("job", ["verify", "release-build"])
def test_gradle_job_reports_a_verdict_for_every_ending(jobs, job):
    steps = jobs[job]["steps"]
    verdict = steps[-1]
    assert verdict["id"] == "verdict" and verdict["if"] == "${{ always() }}"
    assert jobs[job]["outputs"] == {"verdict": "${{ steps.verdict.outputs.verdict }}"}
    assert verdict["with"] == {
        "log-file": "${{ runner.temp }}/gradle.log",
        "setup-outcome": "${{ steps.checkout.outcome }}",
        "build-outcome": "${{ steps.build.outcome }}",
        # Covers the steps between checkout and the build, which are skipped on a docs-only change.
        "job-status": "${{ job.status }}",
    }
    ids = [item.get("id") for item in steps]
    assert ids.index("checkout") < ids.index("plan") < ids.index("build") < ids.index("verdict")
    for item in steps[ids.index("plan") + 1 : ids.index("build") + 1]:
        assert item["if"] == "${{ steps.plan.outputs.build == 'true' }}", item["name"]
    upload = step(jobs[job], "Upload the test and lint reports")
    assert upload["continue-on-error"] is True, "a failed upload would turn a code failure into infra"


def test_code_style_reports_a_verdict_for_every_ending(jobs, tmp_path):
    job = jobs["code-style"]
    verdict = job["steps"][-1]
    assert verdict["if"] == "${{ always() }}"
    assert verdict["env"] == {
        "SETUP": "${{ steps.checkout.outcome }} ${{ steps.mise.outcome }}",
        "CHECK": "${{ steps.pre-commit.outcome }}",
    }
    assert [item.get("id") for item in job["steps"][1:]] == ["checkout", "mise", "pre-commit", "verdict"]
    expected = {
        ("success success", "success"): "pass",
        ("success success", "failure"): "code",
        ("success success", "cancelled"): "infra",
        ("success success", "skipped"): "infra",
        ("success failure", "skipped"): "infra",
        ("failure skipped", "skipped"): "infra",
        ("success cancelled", "skipped"): "infra",
    }
    for (setup, check), wanted in expected.items():
        output = tmp_path / f"output-{setup}-{check}".replace(" ", "-")
        result = run_script(verdict["run"], {"SETUP": setup, "CHECK": check, "GITHUB_OUTPUT": str(output)}, tmp_path)
        assert result.returncode == 0, result.stderr
        assert output.read_text() == f"verdict={wanted}\n", (setup, check)


def test_full_scan_paths_default_is_the_one_of_pre_commit_changed(entry, jobs):
    # The workflow always passes the input, so the action's own default never applies.
    action = yaml.safe_load((ROOT / "pre-commit-changed" / "action.yml").read_text())
    assert trigger(entry)["inputs"]["full-scan-paths"]["default"] == action["inputs"]["full-scan-paths"]["default"]
    pre_commit = step(jobs["code-style"], "Run pre-commit on the changed files")
    assert pre_commit["with"]["full-scan-paths"] == "${{ inputs.full-scan-paths }}"


def test_primary_and_fallback_pass_the_same_inputs(entry):
    inputs = trigger(entry)["inputs"]
    inner = trigger(load(JOBS))
    primary, fallback = entry["jobs"]["primary"], entry["jobs"]["fallback"]
    assert primary["uses"] == fallback["uses"] == f"./.github/workflows/{JOBS}"
    assert primary["secrets"] == fallback["secrets"] == {"GIT_TOKEN": "${{ secrets.GIT_TOKEN }}"}
    assert set(inner["secrets"]) == {"GIT_TOKEN"}
    assert set(primary["with"]) == set(fallback["with"]) == set(inner["inputs"])
    assert primary["with"]["attempt"] == "primary" and fallback["with"]["attempt"] == "fallback"
    forwarded = set(inputs) - {"runner-labels", "fallback-runs-on"}
    assert forwarded == set(inner["inputs"]) - {"runs-on", "attempt"}
    for name in forwarded:
        assert primary["with"][name] == fallback["with"][name] == f"${{{{ inputs.{name} }}}}"
        assert inputs[name]["type"] == inner["inputs"][name]["type"], name
        assert "default" in inputs[name], f"{name}: the caller-facing workflow owns the defaults"
        # An input that may be empty cannot be required in the called workflow.
        if inputs[name]["default"] == "":
            assert inner["inputs"][name]["required"] is False, name


def test_fallback_runs_only_after_a_self_hosted_infrastructure_failure(entry):
    condition = " ".join(entry["jobs"]["fallback"]["if"].split())
    for clause in (
        "!cancelled()",
        "inputs.runner-labels != ''",
        "needs.pick-runner.outputs.self-hosted != 'false'",
        "(needs.primary.result == 'failure' || needs.primary.result == 'cancelled')",
        "needs.primary.outputs.code-style-verdict != 'code'",
        "needs.primary.outputs.verify-verdict != 'code'",
        "needs.primary.outputs.release-build-verdict != 'code'",
    ):
        assert clause in condition
    assert "||" not in condition.replace("(needs.primary.result == 'failure' || needs.primary.result == 'cancelled')", "")
    assert entry["jobs"]["fallback"]["needs"] == ["pick-runner", "primary"]
    assert entry["jobs"]["primary"]["if"] == "${{ !cancelled() }}"
    assert entry["jobs"]["pick-runner"]["if"] == "${{ inputs.runner-labels != '' }}"
    outputs = trigger(load(JOBS))["outputs"]
    assert {name: value["value"] for name, value in outputs.items()} == {
        f"{job}-verdict": f"${{{{ jobs.{job}.outputs.verdict }}}}" for job in ("code-style", "verify", "release-build")
    }


def own_action_refs() -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {}
    for name in (ENTRY, JOBS):
        for job in load(name)["jobs"].values():
            for item in job.get("steps", []):
                match = OWN_ACTION.match(item.get("uses", ""))
                if match:
                    refs.setdefault(match["name"], set()).add(match["ref"])
    return refs


def test_actions_of_this_repository_are_referenced_by_full_path_and_one_ref():
    refs = own_action_refs()
    assert set(refs) == {"pick-runner", "pre-commit-changed", "gradle-log-verdict"}
    for name in refs:
        assert (ROOT / name / "action.yml").is_file()
    # A `./` action path would be looked up in the calling repository's checkout.
    for name in (ENTRY, JOBS):
        for job in load(name)["jobs"].values():
            for item in job.get("steps", []):
                assert not item.get("uses", "").startswith("./"), item
    assert refs["pick-runner"] == {"pick-runner/v1"}
    # One ref for the actions released with the workflows: a half-done retag fails here.
    assert len(refs["pre-commit-changed"] | refs["gradle-log-verdict"]) == 1


def test_secrets_are_optional_and_reach_only_the_steps_that_need_them(entry, jobs):
    secrets = trigger(entry)["secrets"]
    assert set(secrets) == {"RUNNER_READ_TOKEN", "GIT_TOKEN"}
    assert all(secret["required"] is False for secret in secrets.values())
    text = (WORKFLOWS / JOBS).read_text()
    assert "RUNNER_READ_TOKEN" not in text
    for name, job in jobs.items():
        for item in job["steps"]:
            if "secrets.GIT_TOKEN" not in str(item):
                continue
            assert name in ("verify", "release-build")
            assert item.get("id") in ("checkout", "build"), item
    for job in ("verify", "release-build"):
        checkout = checkout_step(jobs[job])["with"]
        # paths-filter needs git credentials on a push only; a pull request's code must not find the token.
        assert checkout["persist-credentials"] == "${{ github.event_name == 'push' }}"
        assert checkout["token"] == "${{ secrets.GIT_TOKEN || github.token }}"
    assert checkout_step(jobs["code-style"])["with"]["persist-credentials"] is False
    assert "secrets." not in str(jobs["code-style"])


def test_the_example_passes_only_inputs_the_workflow_has(entry):
    example = yaml.safe_load(EXAMPLE.read_text())
    (job,) = example["jobs"].values()
    assert job["uses"].startswith(f"asleep-ai/actions/.github/workflows/{ENTRY}@")
    assert set(job["with"]) <= set(trigger(entry)["inputs"])
    assert job["secrets"] == "inherit"
    # The called workflow cannot be granted more than the caller has.
    assert example["permissions"] == entry["permissions"] == load(JOBS)["permissions"]


def test_readme_lists_every_input_with_its_default(entry):
    readme = (ROOT / "android-ci" / "README.md").read_text()
    for name, spec in trigger(entry)["inputs"].items():
        row = re.search(rf"^\| `{re.escape(name)}` \| (?P<default>.*?) \| ", readme, re.MULTILINE)
        assert row, f"{name} is missing from the inputs table"
        default = spec["default"]
        if name == "paths-ignore":
            for pattern in default.split():
                assert f"\n{pattern}\n" in readme, pattern
        elif name == "full-scan-paths":
            assert "pre-commit-changed" in row["default"]
        elif default == "":
            assert row["default"] == "`''`", name
        else:
            assert row["default"] == f"`{default}`", name
