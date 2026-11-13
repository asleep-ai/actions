# android-ci

Reusable workflows for Android app repositories. The calling repository keeps a short workflow file and two Gradle tasks; the jobs, the runner choice and the re-run policy live here.

| workflow | description |
|----------|-------------|
| [`android-pr-check.yml`](../.github/workflows/android-pr-check.yml) | Pull request check: code style, a verification build and a release build, with a GitHub-hosted re-run after an infrastructure failure on self-hosted runners. |

## android-pr-check.yml

### Usage

```yaml
name: PR Check

on:
  pull_request:
    branches: [main, develop]
  push:
    branches: [main, develop]
  workflow_dispatch:

permissions:
  contents: read
  packages: read
  pull-requests: read

jobs:
  pr-check:
    uses: asleep-ai/actions/.github/workflows/android-pr-check.yml@android-ci/v1
    with:
      runner-labels: '["self-hosted","Linux","X64"]'
    secrets: inherit
```

[`pr-check.yml.example`](./pr-check.yml.example) is a complete file. The `android-ci/v1` tag does not exist until the first release; pin a full version or commit SHA once it does.

- `permissions` must grant at least the three scopes above. A called workflow cannot be given more than its caller has, and GitHub refuses to start the run when one is missing.
- `secrets: inherit` passes the two optional secrets below when the repository has them. Naming them one by one works too.
- Triggers and `concurrency` belong to the calling file.

### Jobs and checks

```
pick-runner ─▶ primary ─────────▶ fallback ─────────▶ result
               ├ code-style       ├ code-style
               ├ verify           ├ verify
               └ release-build    └ release-build
```

| job | runs on | what it does |
|-----|---------|--------------|
| `pick-runner` | GitHub-hosted | [`pick-runner`](../pick-runner/): the self-hosted labels when a matching runner is online, else `fallback-runs-on`. Skipped when `runner-labels` is empty. |
| `primary / code-style` | picked runner | [`pre-commit-changed`](../pre-commit-changed/) with the caller's `.pre-commit-config.yaml`, on the files the pull request or push changed. |
| `primary / verify` | picked runner | `./gradlew ciVerify --continue` |
| `primary / release-build` | picked runner | `./gradlew ciReleaseBuild`, signed with a throwaway key |
| `fallback / …` | `fallback-runs-on` | The same three jobs again, only after an infrastructure failure of a self-hosted primary run. |
| `result` | GitHub-hosted | Combines both runs into one outcome. |

**The check to require is `<calling job> / result`**, `pr-check / result` in the example above. It is the only check that always reports and always means the outcome:

- A primary job that failed for an infrastructure reason stays red after the fallback passes.
- The fallback jobs are skipped on most runs, and a skipped check counts as passing.
- `result` runs with `always()`, so a cancelled run ends in a failed check instead of a skipped one.

### What the calling repository provides

| what | contract |
|------|----------|
| `ciVerify` | Gradle task in the root project: the build, unit tests, lint and repository-specific checks of a pull request. It runs with `--continue`, so one run reports build, test and lint failures together. |
| `ciReleaseBuild` | Gradle task in the root project that assembles the release variant, so R8, resource shrinking and keep-rule breakage surface on the pull request. |
| Signing | The release variant's `signingConfig` reads `KEYSTORE_FILE`, `KEYSTORE_PASSWORD`, `KEY_ALIAS` and `KEY_PASSWORD` from `local.properties` in the repository root. The workflow writes these four keys for a key it generates in the job's temp directory. |
| Other `local.properties` values | Whatever else the build reads at configuration time goes into the `local-properties` input as placeholders. |
| No uploads without credentials | Tasks that upload (crash-reporting mappings, source bundles) must be disabled when their token is absent: no such token reaches these jobs. |
| Code style | `.pre-commit-config.yaml`, and a mise configuration that provides `pre-commit` and every tool the hooks call. |
| Private packages | Gradle reads the environment variables `GITHUB_ACTOR` and `GITHUB_TOKEN` as GitHub Packages credentials. |
| Runners | bash, `python3` 3.9 or newer, and an Android SDK that Gradle finds through `ANDROID_HOME`. GitHub-hosted Ubuntu runners have all three. |

`ciVerify` and `ciReleaseBuild` are names, not definitions: which variants, flavors and extra checks they cover is decided in the calling repository's build, where it can be run locally with the same command.

### Inputs

| name | default | description |
|------|---------|-------------|
| `runner-labels` | `''` | Self-hosted `runs-on` labels as a JSON array of strings. Empty runs everything on `fallback-runs-on`, with no runner check and no re-run. |
| `fallback-runs-on` | `ubuntu-latest` | GitHub-hosted `runs-on`: one label, or a JSON array of labels. Used when no self-hosted runner is online, and for the re-run. |
| `java-version` | `17` | JDK version for the Gradle jobs. |
| `java-distribution` | `temurin` | JDK distribution for `actions/setup-java`. |
| `mise-version` | `2026.9.2` | Version of mise in the `code-style` job. |
| `full-scan-paths` | the list of [`pre-commit-changed`](../pre-commit-changed/) | Glob patterns, one per line. A change to a matching path makes `code-style` run pre-commit on every file. Empty never does. See [Code style](#code-style). |
| `verify-task` | `ciVerify` | Gradle task of the `verify` job. |
| `verify-args` | `''` | Extra Gradle arguments for the `verify` job. |
| `release-build-task` | `ciReleaseBuild` | Gradle task of the `release-build` job. |
| `release-build-args` | `''` | Extra Gradle arguments for the `release-build` job. |
| `release-build-skip-push-branches` | `''` | Branch names, separated by whitespace or commas. A push to one of them skips the release build. For a branch whose pushes another workflow already builds the release variant for. |
| `checkout-submodules` | `false` | `submodules` of `actions/checkout` in the Gradle jobs: `false`, `true` or `recursive`. |
| `local-properties` | `''` | Lines of `NAME=value` appended to `local.properties` in the Gradle jobs. See [local.properties](#localproperties). |
| `paths-ignore` | see below | Glob patterns, one per line. See [Skipped builds](#skipped-builds). |
| `code-style-timeout-minutes` | `10` | Timeout of the `code-style` job. |
| `verify-timeout-minutes` | `45` | Timeout of the `verify` job. |
| `release-build-timeout-minutes` | `30` | Timeout of the `release-build` job. |
| `self-hosted-max-workers` | `32` | Gradle `--max-workers` on a self-hosted runner. Empty leaves Gradle's default. |
| `self-hosted-gradle-jvmargs` | `-Xmx16g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8` | `org.gradle.jvmargs` on a self-hosted runner. Empty keeps the caller's `gradle.properties` value. |
| `self-hosted-kotlin-daemon-jvmargs` | `-Xmx6g` | `kotlin.daemon.jvmargs` on a self-hosted runner. Empty keeps the caller's value. |
| `report-retention-days` | `14` | Days to keep the reports of a failed build. |

The task and argument inputs are split on whitespace into separate Gradle arguments, without glob expansion. An argument that itself contains a space cannot be passed this way; put such a setting in the build or in `gradle.properties`.

### Secrets

Both are optional, and the workflow runs without either.

| name | used for |
|------|----------|
| `RUNNER_READ_TOKEN` | `pick-runner`'s token: the organization permission **Self-hosted runners: Read**. Without it the self-hosted labels are used unchecked, with a warning. |
| `GIT_TOKEN` | Checkout of private submodules, and `GITHUB_TOKEN` for the Gradle step so private GitHub Packages of other repositories resolve. Without it both use the job token, which reads only the calling repository. |

`GIT_TOKEN` is passed to `actions/checkout` in the Gradle jobs and to the step that runs Gradle. No other step sees it. The clone keeps the checkout token only on a `push` event, where the path filter fetches the previous commit with git; on a pull request the token is not left in the clone for the pull request's code to read.

No signing secret is used. Pull requests from forks and from Dependabot receive no secrets, and they build the same way; a repository that needs `GIT_TOKEN` to resolve its dependencies has to provide it as a Dependabot secret as well.

### How a run is decided

Each job reports a verdict:

| verdict | meaning |
|---------|---------|
| `pass` | The job's check passed, or its build was skipped on purpose. |
| `code` | The failure belongs to the change: a hook finding, a compile error, a failing test, a lint finding. |
| `infra` | The job did not reach a verdict for a reason outside the change. |

The Gradle jobs take theirs from [`gradle-log-verdict`](../gradle-log-verdict/), which reads the step outcomes and the Gradle log; its README lists the rules. In `code-style`, a failed checkout or tool installation is `infra` and a failed hook run is `code`. A job that never reported, because its runner went away, has an empty verdict.

The `fallback` run starts when all of these hold:

- `runner-labels` is set, and `pick-runner` picked the self-hosted labels (or failed, in which case the jobs went there unchecked).
- The primary run's result is `failure` or `cancelled`. `cancelled` while the workflow run itself is not cancelled means a job hit its timeout.
- No primary job reported `code`. One code failure is final for the whole run: the other jobs are not re-run to find out more.
- The workflow run was not cancelled, by a person or by a newer push in the caller's `concurrency` group.

`result` then passes when the primary run passed, or else when the fallback run passed. A run passed when its result is `success` and all three verdicts are `pass`: the verdict speaks for the build step and the result for the whole job, and neither is trusted alone. `result` writes the two runs' results and verdicts to the job summary, and adds a warning annotation when the check passed only on the re-run or ran on GitHub-hosted runners because no self-hosted runner was online. Both mean the host needs attention.

### Code style

`code-style` checks out the pull request head with full history, installs the caller's mise tools and runs [`pre-commit-changed`](../pre-commit-changed/), whose README describes which files each event checks.

When a changed path matches `full-scan-paths` (lint configuration, tool versions, hook scripts, workflow files), pre-commit runs on every file instead, so a configuration change cannot leave findings in files the change did not touch. That requires a repository whose files all pass its hooks, with baselines for the legacy ones. A repository that is still adopting its hooks file by file sets `full-scan-paths: ''` until then; otherwise every pull request that touches a workflow file or a lint setting fails on files it did not change.

### Self-hosted runners

Steps that differ on a self-hosted runner (`runner.environment == 'self-hosted'`):

- **Memory.** Gradle starts one worker per core by default, and a `gradle.properties` heap is sized for a small machine. On a host with many cores, that combination runs out of heap. The Gradle jobs therefore pass `--max-workers`, `org.gradle.jvmargs` and `kotlin.daemon.jvmargs` from the three `self-hosted-*` inputs. Size them for the number of runners that share one host: each concurrent build takes the Gradle heap plus the Kotlin daemon heap plus its test workers.
- **Daemon.** `--no-daemon`. Runners on one host share a Gradle user home, so a build could attach to another job's idle daemon, which that job's runner kills when the job ends.
- **Caches.** `setup-gradle` and `mise-action` skip the Actions cache: the runner keeps its Gradle user home and installed tools between jobs, so restoring and saving them only costs time. On GitHub-hosted runners both caches are read and written.

`pick-runner` and `result` always run on `ubuntu-latest`, so the choice and the outcome are reported even when the self-hosted host is down.

The jobs run the pull request's code on the runners named by `runner-labels`. Whether pull requests from forks may reach self-hosted runners is the repository's and the runner group's setting, not this workflow's.

### Skipped builds

`paths-ignore` lists files that need no build. When every file a pull request or push changed matches one of the patterns, `verify` and `release-build` skip Gradle and report `pass`, so `result` still reports on a documentation-only change. A workflow-level `paths-ignore` would skip the run instead and leave a required check pending.

The default:

```
*.md
docs/**
.gitignore
LICENSE
.github/ISSUE_TEMPLATE/**
.github/PULL_REQUEST_TEMPLATE.md
```

- Patterns are [picomatch](https://github.com/micromatch/picomatch) globs, as in `dorny/paths-filter`. `*.md` is Markdown in the repository root only; Markdown elsewhere may be packaged into the app. Use `**/*.md` to ignore it everywhere.
- Setting the input replaces the default list. An empty value builds on every change.
- Events other than `pull_request`, `pull_request_target` and `push` have no change to compare and always build. A manual run is a request for the full check.
- `code-style` always runs: pre-commit decides by itself which hooks apply to the changed files.

### local.properties

In both Gradle jobs the workflow generates a signing key with `keytool` in the job's temp directory and writes `local.properties` in the repository root:

```properties
KEYSTORE_FILE=<temp directory>/android-pr-check.jks
KEYSTORE_PASSWORD=…
KEY_ALIAS=…
KEY_PASSWORD=…
<the lines of the local-properties input>
```

- The APK built with this key is never uploaded. It only proves that the release variant compiles, shrinks, packages and signs.
- `local-properties` takes `NAME=value` lines; blank lines and `#` comments are skipped. A line of any other form, or one that sets a signing key, fails the job without printing the line.
- The values are placeholders for a pull request build, written in the calling workflow file. No secret or variable is read, so the build is the same for every author.
- An existing `local.properties` is overwritten.

### Reports

When the Gradle step of `verify` or `release-build` fails, the job uploads `**/build/reports/tests/**`, `**/build/reports/lint-results-*` and `**/build/reports/configuration-cache/**` as an artifact named `android-pr-check-<job>-reports-<primary|fallback>`, kept for `report-retention-days`. A build that fails before any report exists uploads nothing, and a failed upload does not change the job's outcome.

### Versioning

The workflows are released together under `android-ci/vX.Y.Z`, with `android-ci/v1` as the floating major tag.

Inside the workflows:

- Actions of this repository are referenced by full path and ref (`asleep-ai/actions/<name>@<ref>`). A `./` action path would be looked up in the calling repository's checkout.
- `android-pr-check.yml` calls `android-pr-check-jobs.yml` as `./.github/workflows/…`, which GitHub resolves in this repository at the same commit as the calling workflow file. The two files therefore always match.

`android-pr-check-jobs.yml` is internal: its inputs change without notice, so call `android-pr-check.yml`.

### Development

```bash
uv run --with pytest --with pyyaml python -m pytest android-ci -q
```

The tests read both workflow files. They run the shell of each `run` step against stand-ins for `gradlew` and `keytool`, check every combination of results and verdicts that `result` has to decide, and fail when the `verify` and `release-build` jobs, or the `primary` and `fallback` calls, drift apart. They cannot evaluate GitHub's expressions: job conditions such as the one on `fallback` are verified by running the workflow from a calling repository.
