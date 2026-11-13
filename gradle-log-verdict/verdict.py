"""Report how a Gradle job ended: pass, code or infra.

`code`: Gradle reached a verdict and the failure belongs to the change: a
compile error, a failing test, a lint finding, a dependency no repository has.
`infra`: the run did not reach a verdict for a reason outside the change: a
failed setup step, no log (the job failed before Gradle wrote anything),
memory, disk, network, a missing tool or SDK, or a Gradle run that never
finished.

The caller re-runs a job elsewhere only after `infra`, so a doubtful case leans
to `infra`: a wrong `infra` costs one extra run that fails the same way, while
a wrong `code` is never re-run.

action.yml passes every input through an environment variable, so no input is
ever interpolated into a shell command.

Order:
- Any setup outcome other than success -> infra.
- The build outcome is success or skipped -> pass.
- Otherwise the Gradle log decides (see classify).
- Invalid inputs fail the step: they are caller bugs, not a verdict. An outcome
  that is not one GitHub reports is most often a misspelt step id, which reads
  as an empty string.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Mapping

# What steps.<id>.outcome can be.
OUTCOMES = ("success", "failure", "cancelled", "skipped")

INFRA_PATTERNS = [
    # Memory
    r"OutOfMemoryError",
    r"Java heap space",
    r"GC overhead limit exceeded",
    r"Gradle build daemon disappeared unexpectedly",
    r"The daemon has terminated unexpectedly",
    # SIGKILL, usually the kernel OOM killer, on a worker JVM (test executor, lint, D8): it prints no OutOfMemoryError.
    r"finished with non-zero exit value 137",
    # Disk
    r"No space left on device",
    # Host SDK
    r"SDK location not found",
    r"Failed to find (target with hash string|Build Tools revision)",
    r"have not been accepted",
]

# A coordinate that every repository answered "not found" for: a misspelled or nonexistent dependency the change
# added. An outage prints "Could not GET", a status code or a timeout instead, so this is code even next to those.
MISSING_DEPENDENCY = re.compile(r"Could not find \S+:\S+:\S+")

# Messages that a failing unit test can also print (a test may exercise network errors, and Gradle prints a failed
# test's exception class). They count as infra only when no test failed.
NETWORK_OR_TOOL_PATTERNS = [
    # The heading of every dependency resolution failure. The missing dependency above is already decided; what is
    # left is an outage or a damaged or locked dependency cache far more often than the change.
    r"Could not resolve all",
    r"Could not (GET|HEAD|download)",
    r"Connect(ion)? timed out",
    r"Read timed out",
    r"Connection reset",
    r"UnknownHostException",
    r"Received status code 5\d\d",
    r"command not found",
]

# Gradle's console lines for failed tests: "FooTest > bar FAILED", "3 tests completed, 1 failed", a failed test task.
FAILED_TESTS = re.compile(
    r"^\S.* > .+ FAILED$|^\d+ tests? completed, \d+ failed|^> Task \S*[Tt]est\S* FAILED$",
    re.MULTILINE,
)

VERDICT = re.compile(r"^BUILD (FAILED|SUCCESSFUL)\b", re.MULTILINE)


class InputError(Exception):
    """An input the caller has to fix."""


def first_match(patterns: list[str], text: str) -> str | None:
    return next((pattern for pattern in patterns if re.search(pattern, text)), None)


def classify(text: str) -> tuple[str, str]:
    """Return `code` or `infra` for the log of a failed Gradle run, and the rule that decided."""
    pattern = first_match(INFRA_PATTERNS, text)
    if pattern:
        return "infra", f"the log matches {pattern!r}"
    if MISSING_DEPENDENCY.search(text):
        return "code", "a dependency was not found in any repository"
    if not FAILED_TESTS.search(text):
        pattern = first_match(NETWORK_OR_TOOL_PATTERNS, text)
        if pattern:
            return "infra", f"the log matches {pattern!r} and no test failed"
    # A Gradle verdict means Gradle finished on this host, so the failure is the change's. That holds for
    # BUILD SUCCESSFUL too: the step failed in a check that ran after Gradle.
    if VERDICT.search(text):
        return "code", "Gradle finished with a verdict"
    return "infra", "Gradle did not finish: the log has no BUILD verdict"


def parse_outcomes(raw: str, name: str) -> list[str]:
    values = raw.replace(",", " ").split()
    if not values:
        raise InputError(f"{name} is empty; pass steps.<id>.outcome and check the step id")
    for value in values:
        if value not in OUTCOMES:
            raise InputError(f"{name} must be one of {', '.join(OUTCOMES)}, got {value!r}")
    return values


def decide(setup: list[str], build: str, log: Path) -> tuple[str, str]:
    if any(outcome != "success" for outcome in setup):
        return "infra", "a setup step did not succeed"
    if build in ("success", "skipped"):
        return "pass", f"the build step was {build}"
    if not log.is_file():
        return "infra", "there is no Gradle log: the job failed before Gradle wrote anything"
    return classify(log.read_text(errors="replace"))


def escape(text: str) -> str:
    """Escape workflow-command data so a message cannot end the command early."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(env: Mapping[str, str] = os.environ) -> int:
    try:
        setup = parse_outcomes(env.get("SETUP_OUTCOME", ""), "setup-outcome")
        build = parse_outcomes(env.get("BUILD_OUTCOME", ""), "build-outcome")
        if len(build) != 1:
            raise InputError(f"build-outcome must be a single outcome, got {len(build)}")
        log_file = env.get("LOG_FILE", "")
        if not log_file.strip():
            raise InputError("log-file must not be empty")
    except InputError as exc:
        print(f"::error title=gradle-log-verdict::{escape(str(exc))}")
        return 1

    verdict, reason = decide(setup, build[0], Path(log_file))
    print(f"verdict={verdict} (setup: {' '.join(setup)}, build: {build[0]}): {reason}")

    output_path = env.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write(f"verdict={verdict}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
