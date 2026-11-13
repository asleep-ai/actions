# gradle-log-verdict

Composite action that reports how a Gradle job ended: `pass`, `code` or `infra`.

- `code`: Gradle reached a verdict and the failure belongs to the change: a compile error, a failing test, a lint finding, a dependency no repository has.
- `infra`: the run did not reach a verdict for a reason outside the change: a failed setup step, memory, disk, network, a missing tool or SDK, or a Gradle run that never finished.

A workflow that runs on self-hosted runners can use the verdict to run a job again on a GitHub-hosted runner only after `infra`. A doubtful case therefore leans to `infra`: a wrong `infra` costs one extra run that fails the same way, while a wrong `code` is never re-run.

## Usage

```yaml
jobs:
  check:
    runs-on: ubuntu-latest
    outputs:
      verdict: ${{ steps.verdict.outputs.verdict }}
    steps:
      - uses: actions/checkout@v7
        id: checkout

      - uses: actions/setup-java@v5
        id: java
        with:
          distribution: temurin
          java-version: '17'

      - name: Build
        id: build
        shell: bash  # with an explicit bash, a failing gradlew fails the step despite the pipe (pipefail)
        run: ./gradlew ciVerify 2>&1 | tee "$RUNNER_TEMP/gradle.log"

      - name: Report the verdict
        id: verdict
        if: ${{ always() }}
        uses: asleep-ai/actions/gradle-log-verdict@gradle-log-verdict/v1
        with:
          log-file: ${{ runner.temp }}/gradle.log
          setup-outcome: ${{ steps.checkout.outcome }} ${{ steps.java.outcome }}
          build-outcome: ${{ steps.build.outcome }}
```

- Run the step with `if: ${{ always() }}`. Without it the step is skipped exactly when there is a failure to report.
- Pass `outcome`, not `conclusion`: a step with `continue-on-error` has the conclusion `success` even when it failed.
- The job output is empty when the job never got as far as this step, for instance when its runner went away. The calling workflow decides what an empty verdict means; treating it as `infra` matches the rules here.
- `python3` 3.9 or newer must be on `PATH`. GitHub-hosted runners provide it.

The `gradle-log-verdict/v1` tag does not exist until the first release; pin a full version or commit SHA once it does.

## Inputs

| name | required | description |
|------|----------|-------------|
| `log-file` | yes | Path of the file the Gradle output was copied to. A relative path is resolved against the step's working directory, normally the workspace. It is read only when the build step failed. |
| `setup-outcome` | yes | `steps.<id>.outcome` of the steps that prepare the build. Several outcomes may be given, separated by whitespace or commas. |
| `build-outcome` | yes | `steps.<id>.outcome` of the step that ran Gradle. |

## Outputs

| name | description |
|------|-------------|
| `verdict` | `pass`, `code` or `infra`. |

## Rules

The first row that applies decides.

| state | verdict |
|-------|---------|
| Any `setup-outcome` is not `success` | `infra` |
| `build-outcome` is `success` or `skipped` | `pass` |
| `log-file` does not exist | `infra` |
| Otherwise | decided from the log, as below |

A build that was `cancelled`, for instance by the job timeout, is decided from the log like a failed one: a build cut off mid-run has no verdict line and is `infra`.

The log of a failed build, again first match first:

| the log contains | verdict |
|------------------|---------|
| Memory exhaustion (`OutOfMemoryError`, `Java heap space`, `GC overhead limit exceeded`), a Gradle daemon that disappeared, a worker process killed with exit value 137 | `infra` |
| `No space left on device` | `infra` |
| A missing Android SDK, platform or build-tools revision, or SDK licences that have not been accepted | `infra` |
| `Could not find <group>:<name>:<version>`: a dependency no repository has | `code` |
| No failed test, and a dependency resolution failure (`Could not resolve all`), a failed download (`Could not GET`, `HEAD` or `download`), a timeout, a reset connection, an unknown host, an HTTP 5xx status, or `command not found` | `infra` |
| A line starting with `BUILD FAILED` or `BUILD SUCCESSFUL` | `code` |
| None of the above: Gradle never finished | `infra` |

- Network and tool messages count only when no test failed, because a failing unit test can print them too: a test may exercise network errors, and Gradle prints a failed test's exception class. Memory, disk and SDK messages are `infra` even next to a failed test.
- `BUILD SUCCESSFUL` in the log of a failed step is `code`: Gradle finished on this host, and the step failed in a check that ran after it.
- A dependency resolution failure that names no missing coordinate is `infra`. It is an outage or a damaged or locked dependency cache far more often than the change; when it is the change (a variant no dependency offers, a version conflict), the cost is one extra run.

The step prints the verdict and the rule that decided it, for example `verdict=infra (setup: success success, build: failure): the log matches 'No space left on device'`.

An outcome that is not `success`, `failure`, `cancelled` or `skipped` fails the step with an `::error::` annotation instead of producing a verdict, and so do an empty outcome, more than one `build-outcome` and an empty `log-file`. An empty outcome is most often a misspelt step id: `steps.<id>.outcome` of a step that does not exist is an empty string, which would otherwise read as a permanent `infra` or send every run to the log.

## Out of scope

- Running the job again. The verdict is an output; the re-run policy belongs to the calling workflow.
- Checks that do not run Gradle. Their verdict follows from step outcomes alone and needs no log.

## Development

```bash
uv run --with pytest python -m pytest gradle-log-verdict -q
```
