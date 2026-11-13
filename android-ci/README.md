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

jobs:
  pr-check:
    uses: asleep-ai/actions/.github/workflows/android-pr-check.yml@android-ci/v1
    with:
      runner-labels: '["self-hosted","Linux","X64"]'
    secrets:
      RUNNER_READ_TOKEN: ${{ secrets.RUNNER_READ_TOKEN }}
      GIT_TOKEN: ${{ secrets.GIT_TOKEN }}
```

[`pr-check.yml.example`](./pr-check.yml.example) is a complete file. The `android-ci/v1` tag does not exist until the first release.

- `permissions` must grant at least the two scopes above. A called workflow cannot be given more than its caller has, and GitHub refuses to start the run when one is missing.
- Pass the two secrets by name. `secrets: inherit` works too, but it hands every secret of the calling repository to this workflow, signing keys included, when two are needed.
- Triggers and `concurrency` belong to the calling file. Call the workflow on `pull_request`, `push` and manual or scheduled events; `pull_request_target` is refused (see [Events](#events)).

### Jobs and checks

```
pick-runner ─▶ primary ─────────▶ fallback ─────────▶ result
               ├ code-style       ├ code-style
               ├ verify           ├ verify
               └ release-build    └ release-build
```

| job | runs on | what it does |
|-----|---------|--------------|
| `pick-runner` | GitHub-hosted | [`pick-runner`](../pick-runner/): the self-hosted labels when a matching runner is online, else `fallback-runs-on`. Skipped when `runner-labels` is empty and for pull requests from forks. |
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
| No uploads | `ciReleaseBuild` uploads nothing. That includes uploads that need no credentials: a crash-reporting plugin can upload the mapping file of every minified build with nothing but the configuration file in the repository. The build has to switch such tasks off itself; the absence of a token does not. |
| Code style | `.pre-commit-config.yaml`, and a mise configuration that provides `pre-commit` and every tool the hooks call. |
| Private packages | Gradle reads the environment variables `GITHUB_ACTOR` and `GITHUB_TOKEN` as GitHub Packages credentials. |
| Runners | bash, git, `python3` 3.9 or newer, and an Android SDK that Gradle finds through `ANDROID_HOME`. GitHub-hosted Ubuntu runners have all four. |

`ciVerify` and `ciReleaseBuild` are names, not definitions: which variants, flavors and extra checks they cover is decided in the calling repository's build, where it can be run locally with the same command. The names themselves are fixed; they are not inputs.

### Inputs

| name | default | description |
|------|---------|-------------|
| `runner-labels` | `''` | Self-hosted `runs-on` labels as a JSON array of strings. Empty runs everything on `fallback-runs-on`, with no runner check and no re-run. Pull requests from forks always run on `fallback-runs-on` (see [Self-hosted runners](#self-hosted-runners)). |
| `fallback-runs-on` | `ubuntu-latest` | GitHub-hosted `runs-on`. Exactly one of two forms: a bare label (`ubuntu-latest`, no quotes) or a JSON array of labels. A label written with quotes would be queued with the quotes in its name and wait for a runner that does not exist. |
| `java-version` | `17` | JDK version for the Gradle jobs. |
| `java-distribution` | `temurin` | JDK distribution for `actions/setup-java`. |
| `mise-version` | `2026.9.2` | Version of mise in the `code-style` job. |
| `full-scan-paths` | the list of [`pre-commit-changed`](../pre-commit-changed/) | Glob patterns, one per line. A change to a matching path makes `code-style` run pre-commit on every file. Empty never does. See [Code style](#code-style). |
| `verify-args` | `''` | Extra Gradle arguments for the `verify` job. |
| `verify-signing-key` | `true` | Whether `verify` gets the throwaway signing key. See [local.properties](#localproperties). |
| `release-build-args` | `''` | Extra Gradle arguments for the `release-build` job. |
| `release-build-skip-push-branches` | `''` | Branch names, separated by whitespace or commas. A push to one of them skips the release build. For a branch whose pushes another workflow already builds the release variant for. |
| `checkout-submodules` | `false` | `submodules` of `actions/checkout` in the Gradle jobs: `false`, `true` or `recursive`. Anything but `false` checks out with `GIT_TOKEN`. |
| `local-properties` | `''` | Lines of `NAME=value` appended to `local.properties` in the Gradle jobs. See [local.properties](#localproperties). |
| `paths-ignore` | see below | Glob patterns, one per line. See [Skipped builds](#skipped-builds). |
| `code-style-timeout-minutes` | `10` | Timeout of the `code-style` job. |
| `verify-timeout-minutes` | `60` | Timeout of the `verify` job. See [Timeouts](#timeouts). |
| `release-build-timeout-minutes` | `45` | Timeout of the `release-build` job. See [Timeouts](#timeouts). |
| `self-hosted-max-workers` | `32` | Gradle `--max-workers` on a self-hosted runner. Empty leaves Gradle's default. |
| `self-hosted-gradle-jvmargs` | `-Xmx16g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8` | `org.gradle.jvmargs` on a self-hosted runner. Empty keeps the caller's `gradle.properties` value. |
| `self-hosted-kotlin-daemon-jvmargs` | `-Xmx6g` | `kotlin.daemon.jvmargs` on a self-hosted runner. Empty keeps the caller's value. |

The two `-args` inputs are split on whitespace into separate Gradle arguments, without glob expansion. An argument that itself contains a space cannot be passed this way; put such a setting in the build or in `gradle.properties`.

### Secrets

Both are optional, and the workflow runs without either.

| name | used for |
|------|----------|
| `RUNNER_READ_TOKEN` | `pick-runner`'s token: the organization permission **Self-hosted runners: Read**. Without it the self-hosted labels are used unchecked, with a warning. |
| `GIT_TOKEN` | Checkout when `checkout-submodules` is not `false`, so private submodules can be read, and `GITHUB_TOKEN` for the Gradle step, so private GitHub Packages of other repositories resolve. Without it both use the job token, which reads only the calling repository. |

- `RUNNER_READ_TOKEN` goes to the `pick-runner` job only, on a GitHub-hosted runner.
- `GIT_TOKEN` is handed on to the build jobs by name and used in two steps: `actions/checkout` and the step that runs Gradle. On a `push` event the checkout token stays in the clone, because the next step may have to fetch the push's previous commit, and every later step of that job can then read it from the git configuration. On a pull request nothing is left in the clone for the pull request's code to read. The `code-style` job gets no secret.
- The Gradle step prints which token it was given (`GITHUB_TOKEN from GIT_TOKEN` or `from the job token`), which is the first thing to read when a private package does not resolve.
- No signing secret is used.

Pull requests from forks and from Dependabot receive no secrets. The build itself is the same for them, but a repository that needs `GIT_TOKEN` (a private submodule, private packages) fails without it, and on self-hosted runners it fails twice: a failed checkout and an unauthorized package request both read as infrastructure failures, so the fallback runs and fails the same way. Giving Dependabot its own `GIT_TOKEN` secret avoids both failures.

### Events

| event | what the Gradle jobs build |
|-------|----------------------------|
| `pull_request` | The merge commit of the pull request, when a file outside `paths-ignore` changed. |
| `push` | The pushed commit, when a file outside `paths-ignore` changed since the push's previous commit. |
| anything else (`workflow_dispatch`, `schedule`, …) | The event's commit, always. A manual run is a request for the full check. |
| `pull_request_target` | Nothing: `result` fails with an error. The jobs would check out the base branch and pass without building the pull request, and the event would give a fork's pull request the repository's secrets. |

### How a run is decided

Each job reports a verdict:

| verdict | meaning |
|---------|---------|
| `pass` | The job's check passed, or its build was skipped on purpose. |
| `code` | The failure belongs to the change: a hook finding, a compile error, a failing test, a lint finding. |
| `infra` | The job did not reach a verdict for a reason outside the change. |

The Gradle jobs take theirs from [`gradle-log-verdict`](../gradle-log-verdict/), which reads the step outcomes and the Gradle log; its README lists the rules. In `code-style`, a failed checkout or tool installation is `infra` and a failed pre-commit run is `code`, whatever made it fail: a hook that could not start is not told apart from a hook that found something, and is not re-run. A job that never reported, because its runner went away, has an empty verdict.

The `fallback` run starts when all of these hold:

- `pick-runner` ran, and it picked the self-hosted labels or failed (in which case the jobs went there unchecked).
- The primary run's result is `failure` or `cancelled`. `cancelled` while the workflow run itself is not cancelled means a job hit its timeout.
- No primary job reported `code`. One code failure is final for the whole run: the other jobs are not re-run to find out more.
- The workflow run was not cancelled, by a person or by a newer push in the caller's `concurrency` group.

`result` then passes when the primary run passed, or else when the fallback run passed. A run passed when its result is `success` and all three verdicts are `pass`: the verdict speaks for the build step and the result for the whole job, and neither is trusted alone. `result` writes the two runs' results and verdicts to the job summary, and adds a warning annotation when the check passed only on the re-run or ran on GitHub-hosted runners because no self-hosted runner was online. Both mean the host needs attention; counting those annotations is the way to see how often it happens.

### Code style

`code-style` checks out the pull request head with full history, installs the caller's mise tools and runs [`pre-commit-changed`](../pre-commit-changed/), whose README describes which files each event checks.

When a changed path matches `full-scan-paths` (lint configuration, tool versions, hook scripts, workflow files), pre-commit runs on every file instead, so a configuration change cannot leave findings in files the change did not touch. That requires a repository whose files all pass its hooks, with baselines for the legacy ones. A repository that is still adopting its hooks file by file sets `full-scan-paths: ''` until then; otherwise every pull request that touches a workflow file or a lint setting fails on files it did not change.

### Self-hosted runners

Which runs use them:

- Only runs of the repository's own code. When the event carries a pull request whose head is in another repository, `pick-runner` is skipped and all jobs run on `fallback-runs-on`: a fork's mise configuration, hooks and build scripts are code its author chose, and a self-hosted runner keeps its disk between jobs.
- Pull requests from Dependabot and from branches of the repository itself do run there.

Steps that differ on a self-hosted runner (`runner.environment == 'self-hosted'`):

- **Memory.** Gradle starts one worker per core by default, and a `gradle.properties` heap is sized for a small machine. On a host with many cores, that combination runs out of heap. The Gradle jobs therefore pass `--max-workers`, `org.gradle.jvmargs` and `kotlin.daemon.jvmargs` from the three `self-hosted-*` inputs. Size them for the number of runners that share one host: each concurrent build takes the Gradle heap plus the Kotlin daemon heap plus its test workers.
- **Daemon.** `--no-daemon`. Runners on one host share a Gradle user home, so a build could attach to another job's idle daemon, which that job's runner kills when the job ends.
- **Work folder.** The runner keeps its work folder between jobs, and a job cancelled while git is writing leaves a lock file behind, in the repository or in a submodule. Every later checkout in that folder would fail on it (`index.lock: File exists`). Each job therefore deletes the `*.lock` files under the work folder's `.git` before checkout; a runner runs one job at a time, so a lock found there has no owner. `actions/checkout` itself clears only the repository's own `index.lock` and `shallow.lock`.
- **Caches.** `setup-gradle` and `mise-action` skip the Actions cache: the runner keeps its Gradle user home and installed tools between jobs, so restoring and saving them only costs time. On GitHub-hosted runners both caches are read and written.

`pick-runner` and `result` always run on `ubuntu-latest`, so the choice and the outcome are reported even when the self-hosted host is down.

What a self-hosted host carries from one job to the next is outside this workflow, and matters beyond it:

- Runners on one host share the Gradle user home: `init.d` scripts, the dependency cache and the local build cache. A pull request's build writes there, and the next build on that host reads it. The work folder's `.git/config` and hooks persist the same way.
- For this workflow the consequence is limited to the check's own trustworthiness: its outputs are discarded and it holds no signing key. **A job that signs or publishes must not share a Gradle user home or a work folder with these jobs.** Keep such jobs on GitHub-hosted runners or on runners with their own home.
- The shared local build cache is also why one build can fail on an entry another build removed (`Failed to load cache entry`). `gradle-log-verdict` reads that as `infra`, so each occurrence costs a GitHub-hosted re-run. If the warning annotations show it is frequent, give each runner its own `GRADLE_USER_HOME` or build cache directory.

### Timeouts

The same timeout applies on both kinds of runner, and the two differ widely: a self-hosted runner with a warm Gradle user home, and a GitHub-hosted runner that starts without one. The fallback run is close to a cold build, because the self-hosted runs save nothing to the Actions cache.

- A job that hits its timeout on a self-hosted runner is re-run on the GitHub-hosted one.
- A job that hits its timeout on the GitHub-hosted runner fails `result`. Size the two build timeouts for that run: about twice what a build without any cache takes there.
- The cost of a generous timeout is that a hung self-hosted job is noticed that much later.

Measured cold builds on the GitHub-hosted runner (runs with `runner-labels` left out, November 2026): one calling repository took 40 minutes for `verify` and 28 for `release-build`, the other 6 and 19. The defaults are 60 minutes for `verify` and 45 for `release-build`. Measure again with your own repository before relying on them.

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

- The syntax is that of `full-scan-paths`: a pattern matches a whole path from the repository root, `*` and `?` stay within one path segment, `**/` spans zero or more directories and any other `**` matches anything. `*.md` is Markdown in the repository root only; Markdown elsewhere may be packaged into the app. Use `**/*.md` to ignore it everywhere.
- Setting the input replaces the default list. An empty value builds on every change.
- The changed files come from git in the job's own checkout, with no API call: for a pull request the merge commit against its first parent, for a push the pushed commit against the push's previous commit, which is fetched when the two-commit checkout does not have it. A deleted or renamed file counts with its old path.
- Whenever the change cannot be listed (a new branch, a force push whose previous commit is gone, a git error), the jobs build.
- `code-style` always runs: pre-commit decides by itself which hooks apply to the changed files.

### local.properties

In the Gradle jobs the workflow generates a signing key with `keytool` in the job's temp directory and writes `local.properties` in the repository root:

```properties
KEYSTORE_FILE=<temp directory>/android-pr-check.jks
KEYSTORE_PASSWORD=…
KEY_ALIAS=…
KEY_PASSWORD=…
<the lines of the local-properties input>
```

- The APK built with this key is never uploaded. It only proves that the release variant compiles, shrinks, packages and signs.
- `release-build` always gets the key. `verify` gets it unless `verify-signing-key` is `false`. Leave it on when a debug build reads the four keys. Turn it off when none does and the build derives anything from the keystore (a certificate digest in `BuildConfig`, for instance): the key is new in every job, so such a value changes every run and the tasks that consume it never come from the build cache.
- `local-properties` takes `NAME=value` lines; blank lines and `#` comments are skipped. A line of any other form, or one that sets a signing key, fails the job without printing the line.
- The values are placeholders for a pull request build, written in the calling workflow file. No secret or variable is read, so the build is the same for every author.
- An existing `local.properties` is overwritten. When there is nothing to write (no key and no lines), the file is left alone.

### Reports

When the Gradle step of `verify` or `release-build` fails, the job uploads `**/build/reports/tests/**`, `**/build/reports/lint-results-*` and `**/build/reports/configuration-cache/**` as an artifact named `android-pr-check-<job>-reports-<primary|fallback>`, kept for 14 days. A build that fails before any report exists uploads nothing, and a failed upload does not change the job's outcome.

### Versioning

The workflows are released together under `android-ci/vX.Y.Z`, with `android-ci/v1` as the floating major tag.

Inside the workflows:

- Actions of this repository are referenced by full path at the workflows' own major tag (`asleep-ai/actions/<name>@android-ci/v1`), so an `android-ci` release carries the actions it was tested with. `pick-runner`, released before the workflows, is referenced at `pick-runner/v1`. A `./` action path would be looked up in the calling repository's checkout.
- `android-pr-check.yml` calls `android-pr-check-jobs.yml` as `./.github/workflows/…`, which GitHub resolves in this repository at the same commit as the calling workflow file. The two files therefore always match.
- Third-party actions are pinned to a commit. GitHub's own (`actions/*`) follow their major tags.

Pinning the workflow to a full version or a commit SHA fixes the two workflow files, not everything they run: the actions of this repository are referenced at the floating `android-ci/v1` and `pick-runner/v1`, so a rule change in `gradle-log-verdict`, for instance, reaches a caller pinned to an older `android-ci` version with the next release.

`android-pr-check-jobs.yml` is internal: its inputs change without notice, so call `android-pr-check.yml`.

### Development

```bash
uv run --with pytest --with pyyaml python -m pytest android-ci -q
```

The tests read both workflow files. They run the shell of each `run` step against throwaway git repositories and stand-ins for `gradlew` and `keytool`, check every combination of results and verdicts that `result` has to decide, and fail when the `verify` and `release-build` jobs, or the `primary` and `fallback` calls, drift apart. They cannot evaluate GitHub's expressions: job conditions such as the ones on `pick-runner` and `fallback` are pinned as text and verified only by running the workflow from a calling repository.
