"""Unit tests for verdict.py.

The caller re-runs a job elsewhere only after `infra`, so a code failure
classified as `infra` costs one extra run, and an infrastructure failure
classified as `code` is never re-run.

Run: uv run --with pytest python -m pytest gradle-log-verdict -q
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("verdict.py")
_spec = importlib.util.spec_from_file_location("verdict", SCRIPT)
assert _spec is not None and _spec.loader is not None
verdict = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = verdict
_spec.loader.exec_module(verdict)

COMPILE_ERROR = "> Task :feature:home:compileDebugKotlin FAILED\ne: Unresolved reference: foo\n\nBUILD FAILED in 2m 3s\n"
OUT_OF_MEMORY = "e: [ksp] java.lang.OutOfMemoryError: Java heap space\n\nBUILD FAILED in 5m 14s\n"


def classify(text):
    return verdict.classify(text)[0]


def run_main(tmp_path, log_text=None, setup="success success", build="failure", **overrides):
    log = tmp_path / "gradle.log"
    if log_text is not None:
        log.write_text(log_text)
    output = tmp_path / "output"
    env = {"LOG_FILE": str(log), "SETUP_OUTCOME": setup, "BUILD_OUTCOME": build, "GITHUB_OUTPUT": str(output)}
    env.update(overrides)
    code = verdict.main(env)
    outputs = {}
    if output.exists():
        for line in output.read_text().splitlines():
            key, _, value = line.partition("=")
            outputs[key] = value
    return code, outputs


# The log of a failed build.


def test_compile_error_is_code():
    assert classify(COMPILE_ERROR) == "code"


def test_failing_test_is_code():
    log = "> Task :domain:testDebugUnitTest FAILED\n3 tests completed, 1 failed\n\nBUILD FAILED in 4m\n"
    assert classify(log) == "code"


def test_successful_build_with_a_failed_later_check_is_code():
    # Gradle finished and passed, and the step failed in a check that ran after it.
    assert classify("BUILD SUCCESSFUL in 5m 33s\n2454 actionable tasks\n") == "code"


def test_heap_exhaustion_is_infra():
    assert classify(OUT_OF_MEMORY) == "infra"


def test_vanished_daemon_is_infra():
    log = "Gradle build daemon disappeared unexpectedly (it may have been killed or may have crashed)\n"
    assert classify(log) == "infra"


def test_killed_worker_is_infra():
    # The kernel OOM killer sends SIGKILL to a worker JVM, which dies without printing OutOfMemoryError.
    log = ("> Task :feature:home:testDebugUnitTest FAILED\n"
           "Process 'Gradle Test Executor 7' finished with non-zero exit value 137\n\nBUILD FAILED in 6m\n")
    assert classify(log) == "infra"


def test_network_failure_is_infra():
    log = ("> Could not resolve all files for configuration ':app:debugRuntimeClasspath'.\n"
           "   > Could not GET 'https://maven.pkg.github.com/...'. Received status code 502 from server\n"
           "\nBUILD FAILED in 40s\n")
    assert classify(log) == "infra"


def test_network_failure_outside_dependency_resolution_is_infra():
    log = ("> Task :app:downloadAssets FAILED\n"
           "java.net.SocketTimeoutException: Read timed out\n\nBUILD FAILED in 1m\n")
    assert classify(log) == "infra"


def test_missing_dependency_is_code():
    # A misspelled or nonexistent coordinate: every repository answered, none has it. A re-run fails the same way.
    log = ("> Could not resolve all files for configuration ':app:debugRuntimeClasspath'.\n"
           "   > Could not find com.example:no-such-lib:1.0.0.\n"
           "     Searched in the following locations:\n"
           "       - https://dl.google.com/dl/android/maven2/com/example/no-such-lib/1.0.0/no-such-lib-1.0.0.pom\n"
           "\nBUILD FAILED in 40s\n")
    assert classify(log) == "code"


def test_other_resolution_failure_is_infra():
    # No missing coordinate and no network message: the dependency cache of the host, not the change.
    log = ("> Could not resolve all files for configuration ':app:debugRuntimeClasspath'.\n"
           "   > Could not resolve com.example:lib:1.0.0.\n"
           "      > Timeout waiting to lock artifact cache. It is currently in use by another Gradle instance.\n"
           "\nBUILD FAILED in 2m\n")
    assert classify(log) == "infra"


def test_failing_test_that_prints_a_network_exception_is_code():
    # A test may exercise network errors; a regression that lets one escape prints its class, not a host problem.
    log = ("> Task :feature:home:testDebugUnitTest FAILED\n\n"
           "RetryPolicyTest > maps unknown host FAILED\n"
           "    java.net.UnknownHostException at RetryPolicyTest.kt:42\n\n"
           "12 tests completed, 1 failed\n\nBUILD FAILED in 3m\n")
    assert classify(log) == "code"


def test_failing_test_with_heap_exhaustion_is_infra():
    # Memory, disk and SDK messages stay infra even next to a failed test.
    log = "FooTest > bar FAILED\n    java.lang.OutOfMemoryError: Java heap space\n\nBUILD FAILED in 4m\n"
    assert classify(log) == "infra"


def test_full_disk_is_infra():
    assert classify("java.io.IOException: No space left on device\n\nBUILD FAILED in 3m\n") == "infra"


def test_missing_sdk_is_infra():
    assert classify("SDK location not found. Define a valid SDK location\n\nBUILD FAILED\n") == "infra"


def test_unaccepted_sdk_licence_is_infra():
    log = ("> Failed to install the following Android SDK packages as some licences have not been accepted.\n"
           "\nBUILD FAILED in 12s\n")
    assert classify(log) == "infra"


def test_missing_tool_is_infra():
    assert classify("scripts/check.sh: line 3: jq: command not found\n\nBUILD FAILED in 9s\n") == "infra"


def test_unfinished_run_is_infra():
    # The run stopped before Gradle printed its verdict: killed, timed out, or the runner went away.
    assert classify("> Task :app:lintAnalyzeDebug\n") == "infra"


def test_verdict_must_start_a_line():
    assert classify("> Task :app:check\nthe words BUILD FAILED inside a test name\n") == "infra"


def test_classify_names_the_rule_that_decided():
    assert "OutOfMemoryError" in verdict.classify(OUT_OF_MEMORY)[1]
    assert "no BUILD verdict" in verdict.classify("> Task :app:check\n")[1]


# The order: setup outcomes, then the build outcome, then the log.


@pytest.mark.parametrize("setup", [
    "failure", "cancelled", "skipped", "success failure", "failure skipped", "success cancelled",
])
@pytest.mark.parametrize("build", ["success", "skipped", "failure", "cancelled"])
def test_setup_that_did_not_succeed_is_infra_whatever_the_build_did(tmp_path, setup, build):
    # The log says code, and is not read.
    code, out = run_main(tmp_path, COMPILE_ERROR, setup=setup, build=build)
    assert code == 0
    assert out == {"verdict": "infra"}


@pytest.mark.parametrize("build", ["success", "skipped"])
def test_build_that_passed_or_was_skipped_is_pass(tmp_path, build):
    # The log says infra, and is not read: a build that passed needs no explanation.
    code, out = run_main(tmp_path, OUT_OF_MEMORY, build=build)
    assert code == 0
    assert out == {"verdict": "pass"}


def test_pass_needs_no_log(tmp_path):
    assert run_main(tmp_path, None, setup="success", build="skipped") == (0, {"verdict": "pass"})


@pytest.mark.parametrize(("log_text", "expected"), [(COMPILE_ERROR, "code"), (OUT_OF_MEMORY, "infra")])
def test_failed_build_is_decided_from_the_log(tmp_path, log_text, expected):
    assert run_main(tmp_path, log_text) == (0, {"verdict": expected})


def test_missing_log_is_infra(tmp_path, capsys):
    # The step failed before Gradle wrote anything: JDK or Gradle setup, a file the build needs.
    assert run_main(tmp_path, None) == (0, {"verdict": "infra"})
    assert "there is no Gradle log" in capsys.readouterr().out


def test_cancelled_build_is_decided_from_the_log(tmp_path):
    # A build cut off by the job timeout has no verdict line; one that failed first has.
    assert run_main(tmp_path, "> Task :app:lintAnalyzeDebug\n", build="cancelled")[1] == {"verdict": "infra"}
    (tmp_path / "output").unlink()
    assert run_main(tmp_path, COMPILE_ERROR, build="cancelled")[1] == {"verdict": "code"}


@pytest.mark.parametrize("setup", ["success", "success success", "success,success", " success\nsuccess \n"])
def test_setup_outcomes_may_be_one_or_several(tmp_path, setup):
    assert run_main(tmp_path, COMPILE_ERROR, setup=setup) == (0, {"verdict": "code"})


def test_log_that_is_not_utf8_is_still_read(tmp_path):
    (tmp_path / "gradle.log").write_bytes(b"\xff\xfe caf\xe9\n" + COMPILE_ERROR.encode())
    assert run_main(tmp_path) == (0, {"verdict": "code"})


def test_the_decision_is_printed(tmp_path, capsys):
    run_main(tmp_path, COMPILE_ERROR)
    assert "verdict=code (setup: success success, build: failure): Gradle finished" in capsys.readouterr().out


# Inputs the caller has to fix.


@pytest.mark.parametrize(("overrides", "message"), [
    # What a misspelt step id passes: steps.<id>.outcome of a step that does not exist is empty.
    ({"SETUP_OUTCOME": ""}, "setup-outcome is empty"),
    ({"SETUP_OUTCOME": " \n"}, "setup-outcome is empty"),
    ({"BUILD_OUTCOME": ""}, "build-outcome is empty"),
    ({"SETUP_OUTCOME": "success Success"}, "setup-outcome must be one of"),
    ({"SETUP_OUTCOME": "true"}, "setup-outcome must be one of"),
    ({"BUILD_OUTCOME": "failed"}, "build-outcome must be one of"),
    ({"BUILD_OUTCOME": "success failure"}, "build-outcome must be a single outcome"),
    ({"LOG_FILE": ""}, "log-file must not be empty"),
    ({"LOG_FILE": "  "}, "log-file must not be empty"),
])
def test_invalid_inputs_fail_the_step(tmp_path, capsys, overrides, message):
    code, out = run_main(tmp_path, COMPILE_ERROR, **overrides)
    assert code == 1
    assert out == {}
    assert f"::error title=gradle-log-verdict::{message}" in capsys.readouterr().out


def test_error_text_cannot_break_out_of_the_command(tmp_path, capsys):
    run_main(tmp_path, COMPILE_ERROR, BUILD_OUTCOME="x%0A::warning::injected")
    printed = capsys.readouterr().out
    assert "%250A" in printed
    assert verdict.escape("a\nb%c\r") == "a%0Ab%25c%0D"
    assert [line for line in printed.splitlines() if line.startswith("::")] == [printed.strip()]


# The action as GitHub runs it.


def test_script_runs_as_the_action_runs_it(tmp_path):
    log = tmp_path / "gradle.log"
    log.write_text(OUT_OF_MEMORY)
    output = tmp_path / "output"
    env = {**os.environ, "LOG_FILE": str(log), "SETUP_OUTCOME": "success", "BUILD_OUTCOME": "failure",
           "GITHUB_OUTPUT": str(output)}
    result = subprocess.run([sys.executable, str(SCRIPT)], env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_text() == "verdict=infra\n"


def test_action_yml_expressions_only_where_evaluated_on_purpose():
    # GitHub evaluates ${{ }} anywhere in action.yml, descriptions included, and
    # refuses to load the action when one does not parse.
    allowed = re.compile(r"^\s*(default|value|[A-Z_]+):\s*\$\{\{")
    text = Path(__file__).with_name("action.yml").read_text()
    offending = [line for line in text.splitlines() if "${{" in line and not allowed.match(line)]
    assert offending == []
