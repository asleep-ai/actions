# android-ci

Reusable workflows for Android app repositories. The calling repository keeps a short workflow file per workflow and a few Gradle tasks; the jobs, the runner choice and the re-run policy live here.

| workflow | description |
|----------|-------------|
| [`android-pr-check.yml`](../.github/workflows/android-pr-check.yml) | Pull request check: code style, a verification build and a release build, with a GitHub-hosted re-run after an infrastructure failure on self-hosted runners. |
| [`android-version-bump.yml`](../.github/workflows/android-version-bump.yml) | Release cut, started by hand: computes the next version from the tags and checks the commit. A dry run by default; with `dry_run` off and the planned version in `confirm`, it tags the commit `vX.Y.Z`, or `vX.Y.0-preview.N` for a preview of a new minor or major version. |
| [`android-release.yml`](../.github/workflows/android-release.yml) | Release of a tag `vX.Y.Z`: a check of the tagged commit, a cold, signed build, a check of the APK, and a GitHub Release with the APK attached. A tag `vX.Y.0-preview.N` is built the same way and published as a pre-release. |
| [`android-qa.yml`](../.github/workflows/android-qa.yml) | QA build of every push to a branch that is released from: the same signed build with a QA version, published as the rolling pre-release of its line once the commit's checks passed. Its check is what a release requires of the commit's release variant. A manual run can make a label build: a named test build that installs over the last release. |

A version exists only as a git tag. No file in the calling repository states it, and no branch marks it:

```
pull request merged ──▶ pr-check.yml on the push ──▶ `pr-check / result` on the merge commit
        │               qa.yml on the push ──▶ build ──▶ `qa / built` on the merge commit
        │                                        └──▶ (pr-check passed) ──▶ pre-release qa-<line>
        ▼
version-bump.yml (by hand: pr, bump)    ──▶ plan                    a dry run, the default
version-bump.yml (dry_run off, confirm) ──▶ plan ──▶ tag vX.Y.Z     the release
                                                      │             (prerelease: tag vX.Y.0-preview.N)
                                                      ▼
release.yml (for the tag) ──▶ plan ──▶ build with -PciVersionName, -PciVersionCode ──▶ GitHub Release
```

No step waits for a person. The second run tags as soon as its plan passes, and the release is built and published from the tag ([Dry run and confirmation](#dry-run-and-confirmation), [Who can release](#who-can-release)). Both plans require the two checks of the merge commit: the pull request check of the push, and the QA build of the push, which builds the release variant in its place.

Four kinds of build come out of this, all signed with the same key and numbered so that it is known which installs over which ([Version and versionCode](#version-and-versioncode)):

| build | version name | made by | published as | kept |
|-------|--------------|---------|--------------|------|
| QA build | `X.Y.Z-qa.N` | every push to a release branch, or a manual run on one | the rolling pre-release `qa-<line>` | the newest one of each line |
| label build | `X.Y.Z-<label>.k` | a manual QA run with `label` | the rolling pre-release `qa-<line>-<label>` | the newest one of each label |
| preview | `X.Y.0-preview.N` | the version bump with `prerelease: preview` | the pre-release `vX.Y.0-preview.N` | every one |
| release | `X.Y.Z` | the version bump | the release `vX.Y.Z` | every one |

Within a minor version: QA build < preview < release < label build made after that release < the next patch release.

How the workflows fit together is in [Branches, tags and releases](#branches-tags-and-releases).

## android-pr-check.yml

### Usage

```yaml
name: PR Check

on:
  pull_request:
    branches: [main, develop, 'release/**']
  push:
    branches: [main, develop, 'release/**']
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

[`pr-check.yml.example`](./pr-check.yml.example) is a complete file.

- `permissions` must grant at least the two scopes above. A called workflow cannot be given more than its caller has, and GitHub refuses to start the run when one is missing.
- Pass the two secrets by name. `secrets: inherit` works too, but it hands every secret of the calling repository to this workflow, signing keys included, when two are needed.
- Triggers and `concurrency` belong to the calling file. Call the workflow on `pull_request`, `push` and manual or scheduled events; `pull_request_target` is refused (see [Events](#events)).
- Include every branch that is released from in the `push` branches: `android-version-bump.yml` and `android-release.yml` release a commit only when this check succeeded on it, and a merge commit gets the check from the `push` run. `release/**` covers maintenance branches ([Release lines](#release-lines)); a repository without them loses nothing by listing it.
- On a push to those branches the release build is left to [`android-qa.yml`](#android-qayml), which the repository then has to call on the same pushes ([Skipped builds](#skipped-builds)). A repository without it sets `release-build-on-push: true`.
- Do not let a newer push cancel the `push` run of those branches. With `cancel-in-progress: true` for every event, the second of two merges in a row cancels the check of the first, and the first can then not be released. The example cancels only pull request runs (`cancel-in-progress: ${{ github.event_name == 'pull_request' }}`). GitHub still keeps only one waiting run per group: of several merges made while one run is in progress, the ones in between get no check, and a release of one of them is refused until its run is started again.

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
| `primary / release-build` | picked runner | `./gradlew ciReleaseBuild`, signed with a throwaway key. Not built on a push to a branch that is released from ([Skipped builds](#skipped-builds)). |
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
| `full-scan-paths` | the list of [`pre-commit-changed`](../pre-commit-changed/) | Glob patterns, one per line. A change to a matching path makes `code-style` run pre-commit on every file. Empty: a changed path never switches the job to every file. pre-commit-changed still checks every file on a push whose previous commit it cannot find (a new branch, a force push). |
| `verify-args` | `''` | Extra Gradle arguments for the `verify` job. |
| `verify-signing-key` | `true` | Whether `verify` gets the throwaway signing key. See [local.properties](#localproperties). |
| `release-build-args` | `''` | Extra Gradle arguments for the `release-build` job. |
| `release-build-on-push` | `false` | Whether a push to the source branch or to a maintenance branch `release/<major>.x` runs the release build too. `false`, the default: it is skipped there, because [`android-qa.yml`](#the-check-a-release-requires) builds the release variant of every such push and a release requires its check next to `result`. Set `true` in a repository that does not call `android-qa.yml`, and take `qa / built` out of the `required-checks` of the version bump and of the release. See [Skipped builds](#skipped-builds). |
| `source-branch` | `''` | The branch releases are cut from. Empty means the repository's default branch. Read only to tell which pushes `android-qa.yml` builds, for `release-build-on-push`. |
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
- `GIT_TOKEN` is handed on to the build jobs by name and used in two steps: `actions/checkout` and the step that runs Gradle. The checkout leaves no token in the clone, on any event, so the code that is built finds none there. The `code-style` job gets no secret.
- The Gradle step prints which token it was given (`GITHUB_TOKEN from GIT_TOKEN` or `from the job token`), which is the first thing to read when a private package does not resolve.
- No signing secret is used.

Pull requests from forks and from Dependabot receive no secrets. The build itself is the same for them, but a repository that needs `GIT_TOKEN` (a private submodule, private packages) fails without it, and on self-hosted runners it fails twice: a failed checkout and an unauthorized package request both read as infrastructure failures, so the fallback runs and fails the same way. Giving Dependabot its own `GIT_TOKEN` secret avoids both failures.

### Events

| event | what the Gradle jobs build |
|-------|----------------------------|
| `pull_request` | The merge commit of the pull request, when a file outside `paths-ignore` changed. |
| `push` | The pushed commit, always, whatever the push changed: `verify` on every branch, and `release-build` on every branch but the ones that are released from, where the QA build of the same push builds the release variant. See [Skipped builds](#skipped-builds). |
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
- A job that hits its timeout on the GitHub-hosted runner fails `result`. Size the two build timeouts for that run: at least one and a half times what a build without any cache takes there.
- The cost of a generous timeout is that a hung self-hosted job is noticed that much later.

Measured cold builds on the GitHub-hosted runner (runs with `runner-labels` left out, November 2026): one calling repository took 40 minutes for `verify` and 28 for `release-build`, the other 16 and 21 (dependencies refreshed and the build cache off). The defaults are 60 minutes for `verify` and 45 for `release-build`. Measure again with your own repository before relying on them.

### Skipped builds

`paths-ignore` lists files that need no build. When every file a pull request changed matches one of the patterns, `verify` and `release-build` skip Gradle and report `pass`, so `result` still reports on a documentation-only pull request. A workflow-level `paths-ignore` would skip the run instead and leave a required check pending.

**No push is skipped for what it changed. A `push` run always builds**, on every branch and with every `paths-ignore`:

- `result` of the push run is a check that [`android-version-bump.yml`](#what-is-released) and [`android-release.yml`](#the-tag-1) require of the commit they release. It has to mean "this commit was built and tested".
- A skip decides from what the push changed, not from the state of the commit before it. When the previous commit's run failed, was cancelled or was replaced while it waited, a documentation-only merge on top of it would be green without any build, and a release of it would ship code that never passed. A merge that changes no file at all, such as a merge of an already identical branch, would be skipped the same way.
- This holds without anything in the calling file: there is no input that turns it off. The cost is one build for each documentation-only merge.

**One build of a push is left to another workflow: the release build of a push to a branch that is released from**, the source branch and `release/<major>.x`.

- [`android-qa.yml`](#android-qayml) builds the release variant of that very commit: `ciQa`, cold, with the release key, the version and the signature of every APK checked. A second release build of the same commit in this workflow, with a throwaway key, would add nothing but time on the runners.
- `release-build` still runs on such a push and reports `pass` without building, so `result` is decided by `code-style` and `verify`. `verify` builds and tests every push, as above.
- **`result` of a push to those branches therefore no longer says that the release variant builds. The check of the QA build says it**, and a release requires both: `pr-check / result` and `qa / built` are the default `required-checks` of the version bump and of the release ([The check a release requires](#the-check-a-release-requires)). The two defaults change together, so a calling repository that takes them cannot tag a commit whose release variant no job built: without a QA build of the push, the release is refused.
- A pull request builds the release variant as before, so R8 and keep-rule breakage still surface before the merge, and so does a push to any other branch.
- A repository that does not call `android-qa.yml` sets `release-build-on-push: true` here and `required-checks: pr-check / result` in the version bump and the release. Change both or neither: with `qa / built` taken out of `required-checks` while `release-build-on-push` is still `false`, a release would be cut from a commit whose release variant no job built.

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
- The changed files come from git in the job's own checkout, with no API call: the merge commit of the pull request against its first parent. A deleted or renamed file counts with its old path.
- Whenever the change cannot be listed (a git error), the jobs build.
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

## android-version-bump.yml

### Usage

```yaml
name: Version Bump

on:
  workflow_dispatch:
    inputs:
      pr:
        description: 'Number of the merged pull request to release (its merge commit is tagged)'
        type: string
      sha:
        description: 'Exception: the commit to release, instead of a pull request'
        type: string
      bump:
        type: choice
        options: [patch, minor, major]
        default: patch
      version:
        description: 'Exception: the exact version X.Y.Z to release (bump is then ignored)'
        type: string
      prerelease:
        description: 'none: the release vX.Y.Z. preview: a preview of a new minor or major version, vX.Y.0-preview.N (not with bump patch)'
        type: choice
        options: [none, preview]
        default: none
      dry_run:
        description: 'Dry run: plan and check only. Untick to release, and fill in confirm'
        type: boolean
        default: true
      confirm:
        description: 'To release (dry run off): the version the dry run planned, e.g. v1.13.1 or v2.0.0-preview.1'
        type: string

concurrency:
  group: ${{ inputs.dry_run && format('version-bump-dry-{0}', github.run_id) || 'version-bump' }}
  cancel-in-progress: false

permissions:
  contents: write
  actions: write
  checks: read
  pull-requests: read

jobs:
  version-bump:
    uses: asleep-ai/actions/.github/workflows/android-version-bump.yml@android-ci/v1
    with:
      pr: ${{ inputs.pr }}
      sha: ${{ inputs.sha }}
      bump: ${{ inputs.bump }}
      version: ${{ inputs.version }}
      prerelease: ${{ inputs.prerelease != 'none' && inputs.prerelease || '' }}
      dry_run: ${{ inputs.dry_run }}
      confirm: ${{ inputs.confirm }}
```

[`version-bump.yml.example`](./version-bump.yml.example) is a complete file.

- `permissions` must grant the four scopes above: the tag (`contents: write`), starting the release workflow (`actions: write`), reading the commit's checks and the pull request.
- No secret is needed. See [Secrets](#secrets-1) for the one optional token.
- `prerelease` is a list in the form, so that another kind of prerelease is one more entry. Its first entry stands for the release; the workflow takes an empty value for that ([Previews](#previews)).
- Keep `dry_run` on by default in the calling file, as above, and pass `confirm`. The two are what stands between a mistaken run and a published release: nothing in the workflow waits for a person ([Dry run and confirmation](#dry-run-and-confirmation)).
- Give the calling file one `concurrency` group without cancellation, so two releases never run at once. GitHub keeps one waiting run per group: a third run started while one runs and one waits replaces the waiting one. The group above leaves dry runs out of it: a dry run writes nothing, so it starts at once and never replaces a release that waits.

### Jobs

| job | runs on | what it does |
|-----|---------|--------------|
| `plan` | GitHub-hosted | Read-only. Computes the next version with [`release-version`](../release-version/), checks the commit, writes the plan to the job summary and, when the run is not a dry run, checks `confirm` against the planned version. |
| `tag` | GitHub-hosted | Follows `plan` at once: tags the commit and starts `release-workflow`. Skipped on a dry run, and when `plan` refused. Runs in `environment` when the caller names one. |

### What is released

- **The commit** is the merge commit of the pull request named by `pr`: the commit the merge left on its base branch. `sha` names a commit directly and is the exception, for a commit that no pull request produced.
- **The version** is the last release raised by `bump`. The last release is the highest `vX.Y.Z` among the tags the commit contains, compared as numbers. Tags with a suffix (`v1.2.3-preview.1`, `v1.2.3-rc.1`) or of another form (`qa-develop`) are not releases: previews and QA builds never change which version comes next. A repository without a release tag counts from `0.0.0`, so `minor` gives `v0.1.0`.
- **An exact version** can be given instead, with `version` (`2.0.0` or `v2.0.0`). `bump` is then not read, and the log and the summary say so. It is for what a bump cannot reach: a first release that does not start at `0.x`, or a version that skips some. The version has to be higher than the last release the commit contains, must not be tagged yet, and is held to every rule below like a version that was computed.
- **The branch** the commit has to be on is `source-branch`, by default the repository's default branch, or the maintenance branch of the version's major ([Release lines](#release-lines)). "On the branch" means in its first-parent history: a commit the branch itself pointed at.
- **The summary** shows `vA → vB`, the commit, its release line and versionCode, and the pull requests the release adds (number and title), read from the first-parent history between the two versions. A commit that names no pull request is listed by its id.

The plan refuses the release, with an error that says which rule it was, when:

- the pull request is not merged, or the commit is not in the repository;
- the commit is not on the branch of its release line: a commit of an open pull request, of a branch that was never merged, or of another line's branch. The head commit of a pull request that was merged with a merge commit is refused as well: it is in the branch's history, and its green check is the pull request's, which built the merge with the base branch and not that commit. Release the merge commit;
- the commit is the commit of the last release (nothing new);
- the last release of the version's major is not the one the commit contains: the commit contains no release tag at all while the repository has some, or the line went on elsewhere;
- the branch has released a higher version after the commit, which matters when `major` opens a line that has no release yet;
- `version` is not `X.Y.Z`, is tagged already, or is not higher than the last release the commit contains;
- the commit does not hold `release-workflow` as a workflow that calls `android-release.yml` and can be started by hand ([Starting the release](#starting-the-release)): a commit from before the repository adopted these workflows;
- a check named in `required-checks` has not succeeded on the commit. The newest run of each check counts, and a check that never ran on the commit is a refusal;
- the version has no versionCode under [the formula](#version-and-versioncode);
- with `prerelease`: the value is not `preview`, the version is a patch release, or the commit is the commit of the last preview of the version or does not contain it ([Previews](#previews));
- the run is not a dry run and `confirm` is not the planned version ([Dry run and confirmation](#dry-run-and-confirmation)).

These refusals happen in `plan`, and all but the last are the same on a dry run, so a dry run shows whether the release can happen. The `tag` job can still fail on what only shows while writing: the version was taken by another release meanwhile ([Running again](#running-again)), or the repository's rules refuse the push.

`required-checks` defaults to two checks of the commit:

| check | from | what it says |
|-------|------|--------------|
| `pr-check / result` | [`android-pr-check.yml`](#android-pr-checkyml), called by a job named `pr-check`, on the `push` run | The commit passed code style, the verification build, the tests and lint. That run always builds ([Skipped builds](#skipped-builds)). |
| `qa / built` | [`android-qa.yml`](#the-check-a-release-requires), called by a job named `qa`, on the `push` run | The release variant of the commit was built, cold and signed, and its APKs were checked. The pull request check leaves that build to this workflow on a push. |

- The merge commit gets both from the runs its push started, so the calling repository has to run both workflows on pushes to the branches that are released from, without a path filter and without cancelling a run for a later push.
- A run that was cancelled leaves its check `cancelled`, and a commit that was pushed together with later ones has neither check. Run the failed or cancelled jobs of that commit's runs again, or release a later commit.
- A commit from before the repository called `android-qa.yml` has no `qa / built`, and is refused. The first commit that can be released is the merge commit of the pull request that brought the calling `qa.yml` in.
- A release whose commit does not contain the last preview of its version is allowed, with a warning in the summary: the release installs over that preview, and would do so with code that lacks what the preview had.

### Release lines

A major version is a release line. A repository can keep an older major alive on a maintenance branch named `release/<major>.x` while its source branch moves on:

```
develop      ── v1.13.0 ──────── v2.0.0 ── v2.0.1 ── …      the 2.x line
release/1.x     └── v1.13.1 ── v1.13.2 ── v1.14.0 ── …      the 1.x line
```

- Each line continues from the last release its commit contains. A hotfix merged into `release/1.x` after `v2.0.1` exists is planned as `v1.13.2`, not from `v2.0.1`.
- A major that has a `release/<major>.x` branch is released from that branch only, and that branch releases its own major only. `major` on `release/1.x` is refused, and so is `patch` or `minor` on the source branch while it still contains only 1.x releases and `release/1.x` exists: the source branch then has to raise the major.
- A version is never repeated and never falls behind its line. When the last release of the computed major is tagged on another branch, the plan refuses and names it.
- Every versionCode of a line is below every versionCode of the next major ([Version and versionCode](#version-and-versioncode)), so a 1.x release made after `v2.0.0` never installs over a 2.x build.

Nothing has to be configured for this: a repository without `release/<major>.x` branches has one line, and the rules above reduce to "the commit is on the source branch and contains the last release".

To start a maintenance line, create `release/<major>.x` at the last release of that major (or at a later commit that belongs to it) before the source branch releases the next major. Add `release/**` to the pull request check's branches and `release/*.x` to the QA build's, and, in a repository that has created the `release` environment, `release/*` to its allowed branches when a job that signs runs on those branches.

### Previews

A preview is a build of a version that is not released yet, for people outside the team who use it for a while: a demo site, a guest room. It behaves like the release and is not one. With `prerelease: preview` the run plans **the same version** as without it, holds the commit to every rule above, and tags it `vX.Y.0-preview.N` instead of `vX.Y.0`:

```bash
gh workflow run version-bump.yml -f pr=412 -f version=2.0.0 -f prerelease=preview
gh workflow run version-bump.yml -f pr=412 -f version=2.0.0 -f prerelease=preview -f dry_run=false -f confirm=v2.0.0-preview.1
```

- **Only a new minor or major version has previews**: `bump: minor`, `bump: major`, or a `version` whose patch is 0. With `bump: patch` the plan is refused and says to choose `minor` or `major`. The previews of a minor version are numbered below all of its releases, so a preview of `2.0.1` could not install over the `2.0.0` that is out. A fix that has to be tried on a device before the patch is released is a [label build](#label-builds), which installs over the release.
- **`N` counts the previews of that version**: one above the highest `vX.Y.0-preview.N` tag of the repository, starting at 1 and at most 999. A fix found at the demo site is merged and previewed again, as `preview.2`.
- **A preview builds on the one before it.** The commit has to contain the last preview of the same version and add something to it; a commit from before it would carry a higher versionCode and older code. The summary lists what the preview adds to the one before it, or, for the first, to the last release.
- **`confirm` is the whole name**, `v2.0.0-preview.1` or `2.0.0-preview.1`. The version alone does not confirm a preview, and the name of a preview does not confirm a release. A preview someone else cut in between changes the number, and the run then fails before it tags.
- **The release workflow builds it like a release**: the same checks of the commit, the same key, `ciRelease` with `-PciVersionName=X.Y.0-preview.N` and `-PciBuildKind=preview`, and a GitHub **pre-release** that is never the latest release, with notes ([android-release.yml](#the-tag-1)). Every preview keeps its tag and its pre-release.
- **Its versionCode is between the QA builds and the release** of its minor version ([Version and versionCode](#version-and-versioncode)): a preview installs over a QA build, a later preview over an earlier one, and the release over all of them. So a site that runs `2.0.0-preview.3` takes `2.0.0` as an update.
- **The release is not affected.** `vX.Y.0` is later cut with the same inputs and without `prerelease`; it is planned as if the previews did not exist, from any commit that passes the rules, and its notes cover everything since the release before it. One thing is said: when that commit does not contain the last preview of the version, the summary carries a warning, because the release installs over the preview with code that lacks what the preview had.
- `main-branch`, when a caller uses it, is not moved by a preview.
- `preview` is the only value. Another kind, such as a release candidate, would be a second entry of the same list with a range of versionCodes of its own ([`release-version`](../release-version/)).

### The tag

- The tag is an annotated tag on the commit itself. No commit is created: the version is in no file.
- Nothing else is written. No branch is moved: the tag and the GitHub Release that `android-release.yml` publishes for it are the whole record of a release ([Branches, tags and releases](#branches-tags-and-releases)).
- The tag is pushed as soon as the plan of a run that is not a dry run has passed. Nothing waits in between ([Dry run and confirmation](#dry-run-and-confirmation)).
- The push uses the job token. A repository whose rules restrict who may create `v*` tags provides `RELEASE_PUSH_TOKEN` ([Who can release](#who-can-release)).

`main-branch` is an option for a repository that wants a branch to follow its releases; it is empty by default. When it is set, a release from the source branch moves that branch to the tagged commit as a fast-forward, in one atomic push with the tag: when the branch cannot move, no tag is left behind. The plan refuses a release when the branch holds a commit the release lacks, a release from a maintenance branch does not move it, and the source branch itself is never the branch that is moved. The branch cannot be named `release`: git cannot keep a branch `release` next to the maintenance branches `release/<major>.x`.

### Starting the release

GitHub starts no workflow for a push made with the job token, so the new tag alone would not start the release. The `tag` job therefore starts `release-workflow` for the tag itself, as a manual run: starting a workflow by hand is one of the two events the job token may cause.

- GitHub runs the workflow file of the tagged commit, not the one of the branch the version bump was started from. The plan therefore reads `.github/workflows/<release-workflow>` at the commit that is released and refuses the release when the file is missing, does not call `android-release.yml`, or has no `workflow_dispatch` trigger.
- That refusal matters right after a repository adopts these workflows. A commit from before the adoption can pass every other rule: it is on the branch, contains the last release and has a green check. Its release workflow is the earlier one, which would build the new tag its own way, with the version its own files state and without the checks of `android-release.yml`. The first commit that can be released is the merge commit of the pull request that brought the calling `release.yml` in.
- The release runs as a run of its own, on the tag. It can be run again without another version bump, and the rules of its environment, when it has one, see the tag as the ref.
- With `RELEASE_PUSH_TOKEN`, the tag push itself starts every workflow that triggers on tags. Set `release-workflow: ''` then, or the release runs twice. The plan then checks no release workflow: which commits may be tagged is for the calling repository to watch.

### Dry run and confirmation

**No person approves a release, and no job waits.** A run that is not a dry run tags the commit as soon as its plan passes, and the tag is built, signed and published as a GitHub Release without another question. Two inputs take the place of an approval:

- **`dry_run` is on by default**, here and in the calling file. A run started without thinking, from the form or with `gh workflow run version-bump.yml -f pr=…`, plans and checks and writes nothing. Its summary shows `vA → vB`, the commit, the pull requests the release adds and the result of every check, and ends with the value to pass as `confirm`.
- **`confirm` has to name the planned version** (`v1.13.1` or `1.13.1`; for a preview its whole name, `v2.0.0-preview.1`) when `dry_run` is off. The plan compares it with the version it computed and refuses the run, before anything is tagged, when it is empty or another version; the error names the version that was computed. A wrong `bump` or a mistyped `version` therefore ends in a failed run and not in a release, as long as the person passes the version they mean to release and not one copied from the failed run. A wrong `pr` is not caught: every pull request merged after the last release plans the same version, so check the commit in the dry run's summary and pass the same `pr` again. On a dry run `confirm` is not read.

So a release is two runs: a dry run, read by the person who releases, and the same inputs again with `dry_run` off and `confirm`:

```bash
gh workflow run version-bump.yml -f pr=857 -f bump=patch
gh workflow run version-bump.yml -f pr=857 -f bump=patch -f dry_run=false -f confirm=v1.13.1
```

What this is and is not:

- It guards against mistakes: a run started by accident (`confirm` empty), a bump that gives another version than intended, a mistyped `version`, a release someone else cut in between (the planned version is then another one). It does not catch a wrong pull request number when that pull request was merged after the last release.
- It is not a second person. The one who starts the run can read the plan and confirm it alone. Who that can be is in [Who can release](#who-can-release).
- A `vX.Y.Z` tag that a person pushes by hand passes neither input; `android-release.yml` checks its branch and its checks and builds it.

**`environment` is empty by default**, and the `tag` job then runs in no environment. GitHub's required reviewers on an environment would be an approval before the tag, but they exist only for public repositories and on the Enterprise plan: a private repository on the Team plan cannot set them (nor a wait timer). An environment without reviewers stops nothing, and one that a workflow names without it existing is created by GitHub without any rule, so naming one "for approval" there only records a deployment for each release. That an empty `environment` runs the job without one is GitHub's documented behaviour, not yet seen in a run; were a run to fail on it, the job fails before its first step and nothing is tagged, and the calling file can pass any name as `environment` (GitHub creates it without rules; it only records a deployment). A repository that does have required reviewers creates an environment with them, allows the branch the version bump is started from, and passes its name as `environment`; the `tag` job then waits for a reviewer with the plan in the summary. Keep that environment apart from the one of the signing key ([Environment](#environment)): reviewers on the key's environment would be asked again for the build and for every other job that signs.

### Running again

- The `tag` job plans the version again from the repository as it is when the job runs, and stops when the result differs from the plan: another release happened meanwhile. That covers a job that waited, for a runner or for an environment's reviewers, and a re-run of a failed job, which keeps the earlier plan.
- A run whose `tag` job failed can be run again with **Re-run failed jobs**: the plan is kept, and the job finds out by itself how far it got. When the tag is on the commit already, it does only what is left: starting the release.
- **Re-run all jobs**, or a new run with the same inputs, plans again. After the tag was pushed that plan fails, because the commit is now the last release and has nothing new. What is left can then be done by hand: start the release workflow for the tag.
- A re-run keeps the inputs of the run, `dry_run` and `confirm` included: re-running a release that failed before it tagged is that release again, with no dry run in between.

### Inputs

| name | default | description |
|------|---------|-------------|
| `pr` | `''` | Number of the merged pull request to release. Exactly one of `pr` and `sha` is given. |
| `sha` | `''` | The exception to `pr`: id of the commit to release, 7 or more hex digits. A branch name is refused. |
| `bump` | `patch` | Which part of the last version to raise: `patch`, `minor` or `major`. Not read when `version` is given. |
| `version` | `''` | Optional. The exact version to release, `X.Y.Z`, instead of a bump. Higher than the last release the commit contains, and not tagged yet. |
| `prerelease` | `''` | Optional. Empty plans the release `vX.Y.Z`. `preview` plans a preview of that version instead, `vX.Y.0-preview.N`, and only of a new minor or major version: with a patch release the plan is refused ([Previews](#previews)). Any other value is refused. |
| `dry_run` | `true` | Plan and check only: nothing is tagged, pushed or started. On by default; a release turns it off and gives `confirm`. |
| `confirm` | `''` | The version the plan is expected to compute, `X.Y.Z` or `vX.Y.Z`; for a preview the whole name, `vX.Y.0-preview.N`. Required when `dry_run` is `false`: the run is refused before the tag when the plan computes another version. Not read on a dry run. |
| `source-branch` | `''` | The branch releases are cut from. Empty means the repository's default branch. The commit has to be in its first-parent history, or in that of `release/<major>.x` for a major that has that branch. |
| `required-checks` | `pr-check / result`<br>`qa / built` | Names of the checks that must have succeeded on the commit, one per line, as the commit's list of checks shows them. The default is the pull request check and the QA build of the push ([What is released](#what-is-released)). Empty requires none. |
| `main-branch` | `''` | Optional. A branch that is moved to the commit of a release from the source branch. Empty, the default, moves no branch. |
| `release-workflow` | `release.yml` | File name, in `.github/workflows`, of the calling repository's workflow that is started for the new tag. The commit that is released has to hold it, calling `android-release.yml`. Empty starts nothing and checks nothing. |
| `environment` | `''` | Optional. An environment for the `tag` job. Empty, the default: no environment, and the job follows the plan at once. Only where required reviewers exist does naming one add an approval. Not the environment of the signing key. |

### Secrets

| name | used for |
|------|----------|
| `RELEASE_PUSH_TOKEN` | Optional. Pushes the tag instead of the job token, for a repository whose rules restrict who may create `v*` tags. Needs write access to the repository's contents and a place on the bypass list of that rule. |

## android-release.yml

### Usage

```yaml
name: Release

on:
  push:
    tags:
      - 'v[0-9]+.[0-9]+.[0-9]+'
      - 'v[0-9]+.[0-9]+.[0-9]+-preview.[0-9]+'
  workflow_dispatch:

concurrency:
  group: release-${{ github.ref }}
  cancel-in-progress: false

permissions:
  contents: write
  packages: read
  checks: read
  pull-requests: read

jobs:
  release:
    uses: asleep-ai/actions/.github/workflows/android-release.yml@android-ci/v1
    with:
      runner-labels: '["self-hosted","Linux","X64"]'
      local-properties-secrets: |
        EXAMPLE_API_KEY
        EXAMPLE_BASE_URL
      required-secrets: SENTRY_AUTH_TOKEN
    secrets: inherit
```

[`release.yml.example`](./release.yml.example) is a complete file.

- `workflow_dispatch` is how [`android-version-bump.yml`](#starting-the-release) starts the release. The tag trigger covers a tag pushed by a person; such a tag is built only when it passes the same checks of branch and commit ([The tag](#the-tag-1)), and without the dry run and the `confirm` of the version bump ([Who can release](#who-can-release)). Leave the trigger out to build a tag only when someone starts the workflow for it.
- The two tag patterns are the release tags and the preview tags ([Previews](#previews)). A pattern that matched every tag starting with `v` would do no harm, since the workflow refuses a tag of any other form, and neither pattern matches the rolling tags of the QA builds (`qa-…`).
- `permissions` must grant the four scopes above: the GitHub Release, private packages, the checks of the tagged commit, and pull request descriptions for the release notes.
- `secrets: inherit` is required here, unlike in the pull request check. See [Secrets and variables](#secrets-and-variables).

### Jobs

```
plan ─▶ primary / build ─▶ fallback / build ─▶ publish
```

| job | runs on | what it does |
|-----|---------|--------------|
| `plan` | GitHub-hosted | Checks the tag, the branch and the checks of its commit, computes the versionCode, and picks the runner with [`pick-runner`](../pick-runner/). |
| `primary / build` | picked runner, in `environment` | Builds the tag with `./gradlew ciRelease`, checks the APKs and uploads them as an artifact. |
| `fallback / build` | `fallback-runs-on`, in `environment` | The same build again, only after an infrastructure failure of a self-hosted primary build. |
| `publish` | GitHub-hosted | Creates the GitHub Release with the APKs of the build that passed, or adds them to an existing release. |

### What the calling repository provides

| what | contract |
|------|----------|
| `ciRelease` | Gradle task in the root project that builds the release APKs, signed, and runs the uploads that belong to a release, such as mapping files. One APK, or one per flavor: which variants those are is decided in the calling repository's build, and `gradle-args` can pass it a property. A preview tag is built by the same task. |
| APK location | After `ciRelease`, the APKs of the release, and no other `.apk` file, are below `apk-directory`, by default `app/build/outputs/apk`, where the Android Gradle plugin puts the APKs of the `app` module (`release/` without flavors, `<flavor>/release/` with them). Every `.apk` file found there is checked, verified and attached. Their file names differ, because they become assets of one release, and hold letters, digits and `. _ -` only: GitHub changes other characters in the name of an asset. |
| Version | The build takes `versionName` from `-PciVersionName` and `versionCode` from `-PciVersionCode`, for every APK. The workflow reads both back from each APK and fails when one differs. |
| Kind | The build also gets `-PciBuildKind`: `release` or `preview` here, `qa` or `label` from the QA workflow. It is for a build that names its APK after the kind, such as `<App>_<kind>_<versionName>_<versionCode>.apk`; a build that does not read it is not affected. |
| Signing | The release variant's `signingConfig` reads `KEYSTORE_FILE`, `KEYSTORE_PASSWORD`, `KEY_ALIAS` and `KEY_PASSWORD` from `local.properties` in the repository root, as for the pull request check. |
| Other `local.properties` values | Named in `local-properties-secrets`. Each is a secret or a variable, written under its own name. |
| `SENTRY_AUTH_TOKEN` | Read by the build from the environment. Only the Gradle step has it. |
| Runners | bash, git, `python3` 3.9 or newer, and an Android SDK with a build-tools package (`apksigner`, `aapt2`) that is found through `ANDROID_HOME`. GitHub-hosted Ubuntu runners have all of it. |

### Inputs

| name | default | description |
|------|---------|-------------|
| `tag` | `''` | The tag to build: `vX.Y.Z`, or `vX.Y.0-preview.N` for a preview. Empty means the ref the run started for: the tag on a tag push, and on a manual run started for a tag. |
| `runner-labels` | `''` | Self-hosted `runs-on` labels as a JSON array of strings. Empty builds on `fallback-runs-on`, with no runner check and no re-run. |
| `fallback-runs-on` | `ubuntu-latest` | GitHub-hosted `runs-on`: a bare label or a JSON array of labels, as for the pull request check. |
| `environment` | `release` | The environment of the build job. See [Environment](#environment). The QA workflow's default is none; the difference is meant. |
| `java-version` | `17` | JDK version for the build. |
| `java-distribution` | `temurin` | JDK distribution for `actions/setup-java`. |
| `checkout-submodules` | `false` | `submodules` of `actions/checkout` in the build: `false`, `true` or `recursive`. Anything but `false` checks out with `GIT_TOKEN`. |
| `local-properties-secrets` | `''` | Names of secrets and variables, separated by whitespace or commas, written to `local.properties` under their own names. |
| `required-secrets` | `''` | Names of secrets and variables that must not be empty and are not written to `local.properties`, such as `SENTRY_AUTH_TOKEN`. |
| `gradle-args` | `''` | Extra Gradle arguments for `./gradlew ciRelease`, split on whitespace without glob expansion. The build already gets `-PciVersionName`, `-PciVersionCode` and `-PciBuildKind`. |
| `apk-directory` | `app/build/outputs/apk` | The directory `ciRelease` leaves the release APKs in, relative to the repository root. Every `.apk` file below it is released. |
| `expected-cert-sha256` | `''` | Optional SHA-256 digest of the signing certificate every APK must carry, as [`android-signing`](../android-signing/#comparing-the-certificate) takes it. Empty checks only that the signatures are valid. |
| `build-tools-version` | `''` | Optional build-tools version whose `apksigner` and `aapt2` check the APKs. Empty uses the highest installed version. |
| `source-branch` | `''` | The branch releases are cut from. Empty means the repository's default branch. The tagged commit has to be in its first-parent history, or in that of `release/<major>.x`, the maintenance branch of the tag's major. |
| `required-checks` | `pr-check / result`<br>`qa / built` | Names of the checks that must have succeeded on the tagged commit, one per line: by default the pull request check and the QA build of the push. Checked for every tag, whoever pushed it. Empty requires none. |
| `release-notes-prompt` | `''` | System prompt for [`release-notes`](../release-notes/), replacing its built-in one. |
| `build-timeout-minutes` | `60` | Timeout of the build job, on either kind of runner. The build never uses a cache, so size it for a cold build. |
| `self-hosted-max-workers` | `32` | Gradle `--max-workers` on a self-hosted runner. Empty leaves Gradle's default. |
| `self-hosted-gradle-jvmargs` | `-Xmx16g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8` | `org.gradle.jvmargs` on a self-hosted runner. Empty keeps the caller's `gradle.properties` value. |
| `self-hosted-kotlin-daemon-jvmargs` | `-Xmx6g` | `kotlin.daemon.jvmargs` on a self-hosted runner. Empty keeps the caller's value. |

### Secrets and variables

The calling job passes `secrets: inherit`. The names in `local-properties-secrets` and `required-secrets` are known only to the calling repository, so the workflow cannot declare them, and a secret that is not declared can only be handed over with `inherit`. Variables need nothing: a called workflow reads the caller's.

The workflow reads these by name:

| name | required | used for |
|------|----------|----------|
| `KEYSTORE_BASE64` | yes | The signing keystore, base64-encoded (`base64 -i release.jks`). |
| `KEYSTORE_PASSWORD`, `KEY_ALIAS`, `KEY_PASSWORD` | yes | Written to `local.properties` with the path of the decoded keystore. |
| `RUNNER_READ_TOKEN` | no | `pick-runner`'s token. Without it the self-hosted labels are used unchecked, with a warning. |
| `GIT_TOKEN` | no | Checkout when `checkout-submodules` is not `false`, and `GITHUB_TOKEN` of the Gradle step, so private GitHub Packages of other repositories resolve. Without it both use the job token. |
| `SENTRY_AUTH_TOKEN` | no | An environment variable of the Gradle step, and of no other step. Name it in `required-secrets` when a release must not be built without it. |
| `OPENAI_API_KEY` | no | `release-notes`. Without it the notes are a list of commits. |

How the values are read:

- Before anything is checked out, the build job checks the four signing values and every name in `local-properties-secrets` and `required-secrets`. All empty names are printed in one error. A value is never printed.
- A name is looked up among the secrets, then among the variables. For each, GitHub gives the job the closest value: the environment's, then the repository's, then the organization's. Where a value is kept is therefore the calling repository's choice, the signing secrets included ([Environment](#environment)).
- That step is the only one that is handed every secret. It runs after the work folder was emptied, and its script imports nothing from the work folder. It writes the values for `local.properties` to a file in the job's temp directory, readable by the runner's user only, and a later step moves the file into place with the keystore path as `KEYSTORE_FILE`.
- Values are written with the escapes of `java.util.Properties`, so a backslash, a leading space, a line break or a character outside ASCII in a secret reaches the build unchanged.
- The keystore, `local.properties` and the prepared file are deleted at the end of the job, whatever happened before.

### Environment

The build job runs in `environment`, by default `release`. What that means depends on whether the calling repository has created an environment of that name, and both are supported:

**Not created (the signing secrets are repository or organization secrets).**

- GitHub creates the environment, without any rule and without secrets, the first time a job names it. The job reads the four signing secrets from the repository or the organization, like every other value.
- Nothing limits who reads the key. Every workflow of the repository, on every branch, can read a repository secret, so anyone who can push a branch with a workflow file can sign with the release key. Pull requests from forks and from Dependabot get no secrets.
- The only trace of the environment is a deployment record named `release` for each build.
- Which refs the calling repository's other signing jobs accept (a QA build, a prerelease) is then a policy of those workflows, stated and checked in their own files. It is not a protection of the key: a branch that changes the workflow file changes the check.

**Created, with the signing secrets in it and a rule for deployment branches and tags.**

- Secrets of an environment are read only by jobs that name it, and only in runs on a ref that the rule allows. With the signing secrets there, and deleted from the repository's secrets, pull request runs and runs on any other branch cannot read the key.
- Set the rule to selected branches and tags: the release tags (`v*`) for this workflow, whatever line they belong to, and the branches of the calling repository's other jobs that sign, such as a QA build of the default branch (and `release/*` for one on maintenance branches). A run on any other ref cannot start a job in the environment. Deployment branch rules and environment secrets are available to private repositories on the Team plan; required reviewers are not.
- **No required reviewers here**, where they exist. They would make every job that names the environment wait: the build, the `fallback` build again, and those other jobs on every push.
- The rule goes by ref names. What it does not do is in [Who can release](#who-can-release).

Either way:

- `plan` and `publish` name no environment and read no signing secret.
- `android-version-bump.yml` does not use this environment, so the branch it is started from does not have to be allowed here.
- The default differs from `android-qa.yml`, whose build job names no environment unless the caller passes one, and the difference is meant. A release or a preview is rare, and its deployment record under `release` is a list of what was released. A QA build is made on every push, and the same default there would add a deployment to that list for each one. A repository that keeps its signing key in an environment passes its name to both workflows; one that wants no environment for releases either passes `environment: ''`.

### The tag

`plan` stops the release before any build, and before any job reads the signing key, when:

- the tag is not of the form `vX.Y.Z` or `vX.Y.0-preview.N`, or does not exist. A branch, a tag with any other suffix, or the rolling tag of a QA build is never built, and neither is a preview tag of a patch release (`v2.0.1-preview.1`), which the version bump does not create;
- the tag is below the highest release tag of its major and has no GitHub Release yet. A new release with a lower version would carry a lower versionCode than the devices of that line already have. Building an older tag again for its existing release is allowed, and so is a new release of a lower major while a higher one exists ([Release lines](#release-lines));
- the tag is a preview without a GitHub Release yet, and its version is not above the highest release tag of its major (a preview comes before the release of its version), or a higher preview of the same version exists;
- the tagged commit is in the first-parent history of neither `source-branch` nor `release/<major>.x`, the maintenance branch of the tag's major: a commit of an open pull request, of a branch that was never merged or of another line, or the head commit of a pull request that was merged with a merge commit;
- a check named in `required-checks` has not succeeded on the tagged commit. The newest run of each check counts, and a check that never ran on the commit is a refusal. The default is `pr-check / result` and `qa / built`, which the commit gets from the two runs its push started: the pull request check, which always builds and tests ([Skipped builds](#skipped-builds)), and the QA build, which builds the release variant ([The check a release requires](#the-check-a-release-requires)).

The last two are the rules of [`android-version-bump.yml`](#what-is-released), applied again to the tag. They hold for every run of this workflow: a tag the version bump created passes them a second time, and a tag that a person pushed, or a run that a person started for a tag, is held to them all the same. What such a tag does skip is the version bump's plan: the computed version, the summary, the dry run and `confirm` ([Who can release](#who-can-release)).

A tag from before the calling repository had these two workflows on its pushes has no such checks on its commit, and is refused.

A preview tag is held to the same rules, with the branch of its version's line as its branch. What differs is the version the build gets, `X.Y.0-preview.N` with the versionCode of a preview and `-PciBuildKind=preview`, and how it is published ([The GitHub Release](#the-github-release)).

### The build

The build trusts nothing an earlier job left behind:

- **Work folder.** On a self-hosted runner the folder is emptied in the job's first step, `.git` included, before any secret is read and before the checkout, which is then a fresh clone: files, git hooks and git configuration of earlier jobs are gone. It is emptied again at the end, because the build outputs hold the values of `local.properties`.
- **Gradle user home.** `GRADLE_USER_HOME` is a new directory in the job's temp directory, which the runner empties before and after every job. The build starts without the init scripts, dependency cache, build cache and daemons of the shared home, and what it leaves there does not outlive the job.
- **No caches.** `setup-gradle` runs with the Actions cache off, and Gradle with `--no-daemon --no-build-cache`. Every dependency is downloaded and every task runs. The build is therefore about as slow on a self-hosted runner as on a GitHub-hosted one.
- **The commit.** The checkout is the tag, and the job fails when the tag no longer names the commit the release was planned for.

What a self-hosted host still shares with this job: the JDK and other tools in the runner's tool cache, the Android SDK, and everything else the runner's user can write, its home directory included (`~/.gitconfig`, which the checkout reads, and the configuration files of tools the build calls). A pull request build on the same host can change those. The workflow's own Python steps do not read the user's site-packages or `PYTHON*` variables. A repository that cannot accept the rest leaves `runner-labels` empty, and the release is built on a GitHub-hosted runner.

On a self-hosted runner the Gradle step passes `--max-workers`, `org.gradle.jvmargs` and `kotlin.daemon.jvmargs` from the three `self-hosted-*` inputs, as the pull request check does.

### The APKs

After the build, every `.apk` file below `apk-directory` is an APK of the release. The work folder was empty before the checkout, so each was built by this job.

- At least one APK has to be there, no two may share a file name, and each name holds letters, digits and `. _ -` only. The names become the names of the release's assets, and GitHub would change other characters.
- `aapt2 dump badging` reads each APK's `versionName` and `versionCode`, and both have to be the planned ones. A build that ignores `-PciVersionName` or `-PciVersionCode`, for one flavor or for all, fails here instead of being released under another version.
- [`android-signing`](../android-signing/) verifies the signature of each APK and, with `expected-cert-sha256`, its signing certificate.
- The APKs are uploaded together as the artifact `android-release-apk-<primary|fallback>`, kept for 90 days.

### The GitHub Release

- A new release gets the tag as its title, notes from [`release-notes`](../release-notes/) and every APK of the build.
- The notes cover what the release adds to the previous release of its own line: the highest lower release tag the tagged commit contains that has a published release. `v1.13.2`, cut from `release/1.x` after `v2.0.0` exists, starts at `v1.13.1`; `v2.0.0` starts at the 1.x release the source branch grew from. A lower tag that was never published, because its build failed and the fix got the next version, is not a start, so its changes are in these notes. When there is no such release, as for the first one, the notes cover the whole history; a tag with a suffix (`vX.Y.Z-label.N`) is never a start.
- Only the highest version of the repository is created as the latest release (`--latest=true`). A release of a lower major is created with `--latest=false`, whenever it is made.
- A preview is created as a pre-release (`--prerelease --latest=false`), with its tag as the title. Its notes start at the published preview of the same version before it, so they say what changed since the build the site has, and the first preview of a version starts where the release would. A release never starts at a preview.
- When the release of the tag exists already, its title, notes and latest mark are kept, because they may have been edited, and the APKs are uploaded again. An asset of the same name is replaced; an APK with another name is added next to it. Replacing means that `gh` deletes the asset and uploads the new one: for the length of that upload, and after one that failed, the release lacks that APK. Unlike the QA pre-release, which is replaced on every push and is protected against that gap, this happens only when a person builds a published tag again.
- Running the release of a tag again is therefore safe. An APK the first run did not build, such as another flavor selected with a `gradle-args` property, is a second run for the same tag.

### How a run is decided

The build job reports a verdict like the Gradle jobs of the pull request check: `pass`, `code` or `infra`, from [`gradle-log-verdict`](../gradle-log-verdict/).

- A missing secret or variable is `code`: no other runner has it either.
- A plan that arrives incomplete is `code` too. The runner drops a job output that contains the value of a secret, so a version or a commit id can arrive empty; the build job checks what it was handed in its second step and says so. The plans pass no word a secret is likely to equal: numbers, a commit id, a tag, and `true` or `false` for whether a tag is a preview.
- APKs that cannot be released are `code` as well: none under `apk-directory`, two with one file name, a name with other characters than letters, digits and `. _ -`, or one whose version is not the planned one. The build left them, and a second cold build on another runner would leave the same.
- Any other failed step after a successful Gradle run (an SDK without `aapt2`, the signatures, the upload) is `infra`, as the verdict of every doubtful case is.

The `fallback` build starts when the primary build ran on a self-hosted runner, its result is `failure` or `cancelled` (a timeout), its verdict is not `code`, and the run was not cancelled. `publish` runs when either build passed and takes that build's APKs. It adds a warning annotation when they came from the fallback build.

After a failed primary build the run stays red even when the fallback build passed and the release was published. The summary of `publish` states the outcome.

## android-qa.yml

### Usage

```yaml
name: QA Build

on:
  push:
    branches: [develop, 'release/*.x']
  workflow_dispatch:
    inputs:
      label:
        description: 'Optional: a label (ble-test) for a named test build made after the last release'
        type: string
      version:
        description: 'Optional: the next minor or major version X.Y.Z, for one QA build of it'
        type: string

permissions:
  contents: write
  packages: read
  checks: read
  actions: write

jobs:
  qa:
    uses: asleep-ai/actions/.github/workflows/android-qa.yml@android-ci/v1
    with:
      runner-labels: '["self-hosted","Linux","X64"]'
      label: ${{ inputs.label }}
      version: ${{ inputs.version }}
      local-properties-secrets: |
        EXAMPLE_API_KEY
        EXAMPLE_BASE_URL
    secrets: inherit
```

[`qa.yml.example`](./qa.yml.example) is a complete file.

- **Name the calling job `qa`.** The check a release requires by default is `qa / built` ([The check a release requires](#the-check-a-release-requires)); with another job name, name that check in the `required-checks` of the version bump and of the release.
- Call it on `push` to the branches that are released from, the default branch and `release/*.x`, and on `workflow_dispatch`. Whatever starts the run, only those branches are built ([Which commits are built](#which-commits-are-built)). `release/*.x` and not `release/**`: a push to `release/1.x/fix` would start a run that the plan refuses.
- **Every push to those branches has to start a run, and no run is cancelled for a later one.** A release of a commit requires the check of its QA build, so:
  - no `paths` or `paths-ignore` filter on the `push` trigger. A push that changed documentation only is built like any other; without its run the commit could not be released;
  - no `concurrency` in the calling file. With `cancel-in-progress`, the second of two merges in a row would cancel the build of the first, and the first could then not be released. The workflow keeps the order of what is published itself ([The rolling pre-release](#the-rolling-pre-release)).
- `permissions` must grant the four scopes above: the pre-release and its tag (`contents: write`), private packages, the checks of the commit, and deleting the build's artifact (`actions: write`).
- `secrets: inherit` is required, as for the release: the names in `local-properties-secrets` are known only to the calling repository.

### Jobs

```
plan ─▶ primary / build ─▶ fallback / build ─┬▶ built
                                             └▶ publish
```

| job | runs on | what it does |
|-----|---------|--------------|
| `plan` | GitHub-hosted | Refuses a ref that is not built, computes the version name and versionCode with [`release-version`](../release-version/), and picks the runner. |
| `primary / build` | picked runner | Builds the commit with `./gradlew ciQa`, checks the APKs and uploads them as an artifact. |
| `fallback / build` | `fallback-runs-on` | The same build again, only after an infrastructure failure of a self-hosted primary build. |
| `built` | GitHub-hosted | States whether the commit was built: the check a release requires. |
| `publish` | GitHub-hosted | Waits for the commit's checks, then replaces the APKs of the rolling pre-release and deletes the artifact. |

The build job is the one of the release ([The build](#the-build)): the work folder emptied before and after, a Gradle user home of its own, no cache and no daemon, the version read back from every APK and every signature verified. A QA build is signed with the release key, so it is trusted with no more than a release is. It is therefore a cold build on every push; size `build-timeout-minutes` for that.

### What the calling repository provides

| what | contract |
|------|----------|
| `ciQa` | Gradle task in the root project that builds the QA APKs, signed with the same `signingConfig` as the release. One APK, or one per flavor, as for `ciRelease`; `gradle-args` can pass it a property. What a QA build differs in (a variant, a flag, an upload it leaves out) is decided in this task, not in the workflow. The name is fixed. It builds the release variant: its check stands for the release build that the pull request check leaves out on a push. |
| APK location, version, signing, `local.properties` | As for the release ([What the calling repository provides](#what-the-calling-repository-provides-1)): every `.apk` below `apk-directory` is checked and published, the build takes `-PciVersionName` and `-PciVersionCode` as they are, and the four signing keys are written to `local.properties`. `-PciBuildKind` is `qa`, or `label` for a label build. |
| The pull request check on pushes | The APK is published only when `required-checks` succeeded on the commit, and the commit gets `pr-check / result` from the `push` run of the pull request check on its branch. |

### Which commits are built

- **The head of the source branch or of a maintenance branch `release/<major>.x`, and nothing else.** A push run builds the pushed commit; a manual run builds the head of the branch it was started on.
- **A manual run on any other ref fails in `plan`**, before a job reads the signing key, and the error says which ref it was and where a QA build is built from. A branch that is not merged yet is not built: a QA build is signed with the release key and handed to people, so it is built from merged code. Merge the change, or test it as a local build.
- The commit has to be one the branch itself pointed at. A run that waited while the branch moved on still builds its commit; a run for a commit the branch no longer has (a force push) fails.
- **A commit the pre-release is already past is built too.** Its build is what its check stands for; `publish` then leaves the pre-release as it is ([The rolling pre-release](#the-rolling-pre-release)).
- This is a rule of the workflow, checked from the run's own ref. It is not a protection of the signing key ([Who can release](#who-can-release)).

### The check a release requires

`built` succeeds when the build of the commit passed, on the picked runner or on the re-run, and fails otherwise: a plan that refused the run, a build that failed, a run that was cancelled. It does not wait for `publish` and is not changed by it.

- **Its name is `<calling job> / built`, `qa / built` in the example, and only for a run that a push started.** That is the name in the default `required-checks` of [`android-version-bump.yml`](#what-is-released) and [`android-release.yml`](#the-tag-1), next to `pr-check / result`.
- **It stands for the release build of the pushed commit.** [`android-pr-check.yml`](#skipped-builds) does not build the release variant on a push to a branch that is released from, because this workflow builds it there: cold, with the release key, the version and signature of every APK checked. The two checks together say what `pr-check / result` alone said before: the commit was built, tested, and built for release.
- **A manual run reports `<calling job> / built (manual run)` instead.** It builds what its inputs say (a label, a version, another flavor through `gradle-args`). A release reads the newest check run of a name, so under one name a manual build could stand in for the build of the push, or a failed one could hide it.
- A commit whose push run failed for a reason outside the code is built again with **Re-run failed jobs** on that run; the check then succeeds, even when the pre-release has moved on.
- A commit that no push run built has no such check and cannot be released: a commit that was pushed together with later ones, or one from before the repository called this workflow. Release a later commit.

### The version of a QA build

A QA build is named after the version it leads up to, the next release of its line:

```
<version>-qa.<N>                2.0.0-qa.57      1.13.2-qa.58
```

The name is the same on every branch. The version tells the lines apart: a maintenance branch builds its own major.

**`<version>`** is computed from the release tags the commit contains ([What is released](#what-is-released)):

| branch | version |
|--------|---------|
| the source branch | The last release raised by patch: `1.13.1` after `v1.13.0`. |
| the source branch, once `release/<major>.x` exists for the major of its last release | The next major, `2.0.0`: by the rule of [release lines](#release-lines), that is the only version the branch can release. |
| `release/<major>.x` | The last release of that major raised by patch, or `<major>.0.0` when the branch has none yet. |
| no release tag at all | `0.0.1`. |

Two inputs change it:

- **`next-version`**, in the calling file, names the version the branch is heading for when that is not the patch after its last release: `2.0.0` before a first release, or the next minor or major while it is developed. It is used while it is higher than the last release the commit contains, no higher than the next major version after it, belongs to the branch's line and is not released. After that release it is ignored, and the builds go on from the tags: a calling file that still says `2.0.0` after `v2.0.0` builds `2.0.1-qa.N`. The log says when it is not used and why.
- **`version`**, for one manual run, names the version of that one build ([A build of the next minor or major version](#a-build-of-the-next-minor-or-major-version)).

**`<N>`** is the run number of the calling workflow plus `sequence-offset`. A later run is a higher number, on whichever branch; a run that is started again keeps its number. A new calling file counts from 1, so a repository whose installed QA builds have higher numbers sets `sequence-offset` to at least the last number that was used.

**The versionCode** is `N` in the block of the version's minor version, below its previews and releases ([Version and versionCode](#version-and-versioncode)):

- A QA build installs over an earlier QA build of the same minor version, and a preview or a release of that minor version installs over it.
- A QA build does not install over a release of its own minor version. `1.13.1-qa.57` is `101300057`, below `1.13.0` at `101350000`. What goes onto a device that runs the release is a [label build](#label-builds), or a QA build of the next minor version.
- `N` has to stay below 49000, where the previews start. A build that would reach it fails in `plan`. `sequence-offset` may be negative.

### Label builds

A label build is a named test build: a fix to be confirmed on the device that showed the bug, a build for one question (`ble-test`). A manual run with `label` makes one.

```
<next patch>-<label>.<k>        1.13.1-ble-test.2        101350002
```

- **It is numbered right after the last release of its line**, the last release the commit contains of the major the branch builds. Its versionCode is that release's plus `k`: `1.13.0` (`101350000`) < `1.13.1-ble-test.2` (`101350002`) < `1.13.1` (`101350100`). So it **installs over the release** a device runs, and **the next patch release installs over it**. Its name is the patch it leads up to.
- **`k` counts the label builds published after that release**, whatever their label and branch: one above the highest, 1 for the first. A later label build therefore installs over an earlier one. The count is read from the rolling tags ([The rolling pre-release](#the-rolling-pre-release)): each names the build it holds. A build that was planned and never published took no number. Two runs that are planned in the same minute can get the same `k`; they are then two builds of one versionCode, and either installs over the other.
- **At most 99 after one release.** The hundredth is refused: the code after it is the next patch release's. Release the patch; the count starts at 1 again above it.
- **A line without a release has no label builds.** There is nothing for the build to sit above, and the run is refused with that reason. Before a first release, test with the QA builds or with a preview. The same holds for the source branch after `release/<major>.x` took over its last major, until the next major is released.
- **It is published as a pre-release of its own**, `qa-<line>-<label>`. The line's rolling pre-release is not touched, and a later build with the same label replaces the labelled one. It stays until someone deletes it (`gh release delete qa-develop-ble-test --cleanup-tag`).
- A label is lowercase letters, digits and single hyphens, at most 40 characters, and not the name of the line or of another kind of build (`qa`, `preview`, `release`). It cannot be combined with `version`.
- Everything else is the build of a QA build: the same branches only, the same checks of the commit before the APK is published. `-PciBuildKind` is `label`.

A label is a name for a build, not a switch in the app. Behaviour that only one customer or one site gets belongs in the source branch behind a flag or a flavor; a label build of a branch that carries such a change is still that branch's code for everyone.

### A build of the next minor or major version

`version` gives one manual run the version of its build: the next minor or the next major version after the last release, whose QA builds are numbered above every build of the current minor version. `1.14.0-qa.59` is `101400059`: it installs over `1.13.x`, releases included, and `1.14.0` installs over it.

- **It names one of two versions and nothing else**: after `v1.13.0`, `1.14.0` or `2.0.0`. A value typed into a form is signed with the release key and installed; a device that takes `11.3.1-qa.60` refuses every build of 1.x and 2.x afterwards. Before a first release, `version` is no higher than the version the branch is heading for (`next-version`).
- It is refused when it is released already, or when the branch cannot release it: a maintenance branch builds its own major, and the source branch no major that has a maintenance branch.
- **The build is published apart**, as `qa-<line>-<version>` (`qa-develop-1.14.0`). The rolling pre-release of the branch stays a build that every device with an earlier one can take: a device that installed `1.14.0-qa.59` does not take `1.13.1-qa.60` afterwards.
- For a branch, as opposed to one build, use `next-version` in the calling file.

### The checks of the commit

`publish` hands an APK out only for a commit that passed `required-checks`, by default `pr-check / result`, decided by the script a release is decided with ([What is released](#what-is-released)).

- The push that starts the QA build starts the pull request check too, so the check is usually still running when the build is done. `publish` waits for it, asking every 30 seconds, for at most `checks-timeout-minutes`.
- A check that failed, was cancelled or was skipped ends the wait at once. Nothing is published, the job fails with the check's state, and the build's artifact is deleted: the APK of a commit that did not pass is kept nowhere.
- A check that is still missing or running when the time is up fails the job the same way. The usual reason for a check that never appears is a pull request check that does not run on pushes to the branch, or whose run for this commit was replaced while it waited.
- A request that fails while the job waits (a server error, a rate limit, a timeout) is made again until the time is up. Checks that could not be read at all fail the job without being a refusal: the artifact stays, and **Re-run failed jobs** publishes the build once the API answers.
- `checks-timeout-minutes` is a whole number of at most 100; the plan refuses more, because `publish` ends after 120 minutes.
- The wait is the only time this workflow spends on a GitHub-hosted runner without working. It starts after the build, so it is the difference between the two, not the length of the check.
- A run whose checks refused the commit is started again as a whole (**Re-run all jobs**) once the check has passed: the artifact is gone.
- `required-checks: ''` publishes without any check.

### The rolling pre-release

Each line has one GitHub Release, a pre-release that is never the latest release, named by a tag that is not a release tag:

| build | tag and release |
|-------|-----------------|
| the source branch `develop` | `qa-develop` |
| `release/1.x` | `qa-release-1.x` |
| a label build of `develop` with `label: ble-test` | `qa-develop-ble-test` |
| a build of `develop` with `version: 1.14.0` | `qa-develop-1.14.0` |

- **Every build replaces the APK**, in an order that leaves the pre-release whole at every moment:
  1. The tag is moved to the commit that was built. When the repository refuses that, nothing has changed, and the job fails with what to do.
  2. The new APKs are uploaded next to the old ones. An APK whose name the release already holds (a build that names every APK alike, or the same run again) is uploaded under another name, the old one is deleted, and the new one takes its name. `gh release upload --clobber` would delete first, and the pre-release would have no APK for as long as the upload takes, or for good when it fails. When an upload fails, the tag is put back.
  3. Title and notes are rewritten, and the APKs of the build before are deleted.
  The release holds the newest build only, however many pushes there were, and a run that stops half way is put right by the next one.
- **The tag is an annotated tag that moves**, and its message names the build the pre-release holds: `1.13.1-qa.57 (101300057)`. The label builds after a release are counted from these messages. It never starts with `v`, so neither the version bump nor the release workflow reads it as a version, and a release trigger on `v…` tags does not start for it. Do not base anything on it.
- **The job token moves the tag** unless the repository's rules say otherwise: a tag ruleset, or immutable releases. Then give the workflow a token that may, as the secret `RELEASE_PUSH_TOKEN`, the one the version bump takes for release tags; it is used for the tag and nothing else.
- **The notes** state the version name, the versionCode, the commit and the run, and list what the commit adds to the build before it: the pull requests between the commit the tag named and this one, as the version bump lists them. The first build of a line lists what it adds to the last release, and nothing when there is no release yet.
- **One `publish` job at a time per pre-release**, and none is cancelled half way. Every pushed commit is built, so two builds can finish close together, or in the other order.
  - **An older build does not replace a newer one.** When the pre-release is already at a later commit, `publish` says so, publishes nothing and succeeds: the commit was built, and that is all its check claims.
  - GitHub keeps one waiting job per group, the one that arrived last. A build whose `publish` was replaced while it waited shows that job as cancelled; the newer build is published, and nothing is lost.
  - The case that needs a hand: two `publish` jobs wait behind a third, and the one that arrived last is of the older commit, such as a run that was started again. The newer one is cancelled, and the pre-release stays behind until the next push. Run the cancelled `publish` job again (**Re-run failed jobs**); its artifact is kept for a day.
- **A re-run of an old run** builds its commit again and reports its check. It does not move the pre-release back.
- **Why a release and not an artifact.** Artifacts count against the organization's Actions storage for as long as they are kept, and a QA APK of every push fills it; assets of a GitHub Release do not count against it. The artifact that carries the APKs from the build job to `publish` is kept for one day, the shortest GitHub allows, and `publish` deletes it once it is done with it: the APK is published, or a newer build is. After a failure of `publish` it stays for that day, so the job can be run again.
- The download link of a line is its release page, `…/releases/tag/qa-develop`. The file name of the APK is the build's. A build that puts the version name into it gives every build its own name; nothing in the workflow depends on that.

### Sentry

`sentry-upload` decides whether the Gradle step gets `SENTRY_AUTH_TOKEN`, whose presence is what turns the mapping upload on in the calling repository's build. It is off by default: no step of a QA build then has the token, and the build uploads nothing to Sentry. With `sentry-upload: true` the Gradle step, and no other step, gets the secret; name it in `required-secrets` when a QA build must not be made without it. An upload that needs no token is for `ciQa` to leave out.

### Inputs

| name | default | description |
|------|---------|-------------|
| `runner-labels` | `''` | Self-hosted `runs-on` labels as a JSON array of strings. Empty builds on `fallback-runs-on`, with no runner check and no re-run. |
| `fallback-runs-on` | `ubuntu-latest` | GitHub-hosted `runs-on`: a bare label or a JSON array of labels, as for the pull request check. |
| `environment` | `''` | Optional. An environment for the build job. Empty, the default: none, and the job reads the signing secrets from the repository or the organization. A repository that keeps its signing key in an environment names it here and allows the release branches in its rule ([Environment](#environment)). |
| `java-version` | `17` | JDK version for the build. |
| `java-distribution` | `temurin` | JDK distribution for `actions/setup-java`. |
| `checkout-submodules` | `false` | `submodules` of `actions/checkout` in the build: `false`, `true` or `recursive`. Anything but `false` checks out with `GIT_TOKEN`. |
| `local-properties-secrets` | `''` | Names of secrets and variables, separated by whitespace or commas, written to `local.properties` under their own names. |
| `required-secrets` | `''` | Names of secrets and variables that must not be empty and are not written to `local.properties`. |
| `gradle-args` | `''` | Extra Gradle arguments for `./gradlew ciQa`, split on whitespace without glob expansion. The build already gets `-PciVersionName`, `-PciVersionCode` and `-PciBuildKind`. |
| `apk-directory` | `app/build/outputs/apk` | The directory `ciQa` leaves the APKs in, relative to the repository root. Every `.apk` file below it is published. |
| `expected-cert-sha256` | `''` | Optional SHA-256 digest of the signing certificate every APK must carry. Empty checks only that the signatures are valid. |
| `build-tools-version` | `''` | Optional build-tools version whose `apksigner` and `aapt2` check the APKs. Empty uses the highest installed version. |
| `sentry-upload` | `false` | Whether the Gradle step gets `SENTRY_AUTH_TOKEN` ([Sentry](#sentry)). |
| `source-branch` | `''` | The branch releases are cut from. Empty means the repository's default branch. A QA build is built from it or from `release/<major>.x`. |
| `version` | `''` | Optional, for one run: the next minor or the next major version `X.Y.Z`, for one QA build of it that is published apart ([A build of the next minor or major version](#a-build-of-the-next-minor-or-major-version)). Not with `label`. |
| `next-version` | `''` | Optional, for every run of the calling file: the version the branch is heading for. Used while it is higher than the last release, no higher than the next major version after it, and not released. |
| `label` | `''` | Optional, for one run: makes the build a label build, a named test build numbered right after the last release of its line ([Label builds](#label-builds)). |
| `sequence-offset` | `0` | A whole number added to the run number of the calling workflow to give the `N` of a QA build. |
| `required-checks` | `pr-check / result` | Names of the checks that must have succeeded on the commit before its APK is published, one per line. Empty requires none. |
| `checks-timeout-minutes` | `60` | How long `publish` waits, after the build, for a required check that has not finished. A whole number, at most 100. |
| `build-timeout-minutes` | `60` | Timeout of the build job, on either kind of runner. The build never uses a cache, so size it for a cold build. |
| `self-hosted-max-workers` | `32` | Gradle `--max-workers` on a self-hosted runner. Empty leaves Gradle's default. |
| `self-hosted-gradle-jvmargs` | `-Xmx16g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8` | `org.gradle.jvmargs` on a self-hosted runner. Empty keeps the caller's `gradle.properties` value. |
| `self-hosted-kotlin-daemon-jvmargs` | `-Xmx6g` | `kotlin.daemon.jvmargs` on a self-hosted runner. Empty keeps the caller's value. |

### Outputs

`version-name` and `version-code`: what the APK was built with.

### Secrets and variables

As for the release ([Secrets and variables](#secrets-and-variables)): the calling job passes `secrets: inherit`, and the build job checks the four signing values and every name in `local-properties-secrets` and `required-secrets` before anything is checked out.

| name | required | used for |
|------|----------|----------|
| `KEYSTORE_BASE64`, `KEYSTORE_PASSWORD`, `KEY_ALIAS`, `KEY_PASSWORD` | yes | The signing key, the release's. |
| `RUNNER_READ_TOKEN` | no | `pick-runner`'s token. |
| `GIT_TOKEN` | no | Checkout of submodules, and `GITHUB_TOKEN` of the Gradle step. |
| `SENTRY_AUTH_TOKEN` | no | An environment variable of the Gradle step, only with `sentry-upload: true`. |
| `RELEASE_PUSH_TOKEN` | no | Moves the tag of the rolling pre-release instead of the job token, where the repository's rules keep the job token from moving it. Read by that one step. |

The runner drops a job output that contains the value of a secret, without failing the job that set it. The plan therefore passes on numbers, a commit id and a release tag only, and the other jobs put the names of a build together themselves: a key alias such as `release` does not empty the name of a maintenance line (`release/1.x`, `qa-release-1.x`). A job that still receives an empty value says so before it does anything else.

## Branches, tags and releases

The workflows share one picture of the calling repository:

| ref | role |
|-----|------|
| the default branch (`source-branch`) | The line of the current major version. Pull requests are merged here, and its releases are cut here. |
| `release/<major>.x` | The line of an older major that is still maintained ([Release lines](#release-lines)). |
| tags `vX.Y.Z` | The releases. A tag is created by the version bump and never moved. |
| tags `vX.Y.0-preview.N` | The previews of a new minor or major version that is not released yet ([Previews](#previews)). Created by the version bump and never moved. |
| tags `qa-<line>`, `qa-<line>-<label>`, `qa-<line>-<version>` | The newest QA build of a line, the newest label build of a label, and the newest QA build of a version that was given ([The rolling pre-release](#the-rolling-pre-release)). Annotated tags that name the build they hold, moved by every build. |
| GitHub Releases | The published APKs and notes of each tag: releases, and pre-releases for previews, QA builds and label builds. |

- **A release is its tag and its GitHub Release, and nothing else records it.** The newest release is the one GitHub marks **Latest**, the highest `vX.Y.Z` tag that has a release; the last release of an older line is the highest such tag of that major. Previews, QA builds and label builds are pre-releases, which GitHub never marks as the latest.
- **Code moves one way: source branch → QA build → preview → release.** Every push to a release branch is a QA build for the team. A commit that should be seen outside the team before a new minor or major version is released is tagged as a preview. The release is cut when the version is done. All three are signed with the same key and numbered so that each installs over the one before it ([Version and versionCode](#version-and-versioncode)).
- **A label build is the side step**: a named test build of a release branch, numbered right after the last release, for the device that runs that release. The next patch release installs over it. It is a name for a build, not a variant of the app: what only one customer gets is a flag or a flavor in the source branch.
- **No branch follows the releases.** The workflows write no branch. A `main` branch that an earlier flow moved or merged into with every release is frozen: nothing here reads it or writes it. Block pushes to it with a ruleset, so that it cannot drift into a second, unchecked history. That is a setting of the repository, which a person makes; the workflows cannot. Take the branch out of the pull request check's triggers as well.
- **A hotfix starts from a tag or from a maintenance branch.** For the current major, branch from the released tag (`git switch -c fix/crash v2.0.1`) and merge the fix into the default branch through a pull request; the release of that pull request contains everything the default branch had before it. For an older major, branch from `release/<major>.x` and merge the pull request there.
- **No branch is named `release`.** Git cannot keep a branch `release` next to the maintenance branches `release/<major>.x`: a ref cannot be both a name and the directory of other names.
- A repository whose earlier releases were merge commits on `main` that the default branch never received merges `main` into the default branch once, with a merge commit and not a squash. Until then the default branch does not contain the last release tag, and the version bump refuses. That merge commit is itself not released when it was made before the calling `release.yml` was merged: the plan refuses a commit whose release workflow is still the earlier one ([Starting the release](#starting-the-release)).

### Who can release

**Everyone with write access to the repository can release, alone.** That is the whole answer for a repository without the rulesets below, and it is how GitHub works, not an option of these workflows:

- **Starting the version bump.** Write access is what `workflow_dispatch` asks for. One person starts the dry run, reads the plan and starts the release with `confirm`; no second person is involved ([Dry run and confirmation](#dry-run-and-confirmation)). Required reviewers on an environment would add one, and a private repository on the Team plan cannot have them.
- **Pushing a tag by hand.** Everyone with write access can push a tag `vX.Y.Z` or `vX.Y.0-preview.N`, or start the release workflow for one, without the version bump.
- **Starting a QA build or a label build.** Every push to a release branch makes a QA build, and everyone with write access can start one by hand on those branches, with a label or with the next minor or major version. The workflow builds no other ref and no other version, which is a rule of its own file like the checks below: a branch that changes its calling file is not held by it.
- **Reading the signing key.** With the signing secrets among the repository's or the organization's secrets, every workflow on every branch can read them. With a `release` environment that holds them, the rule for branches and tags keeps the key from pull request runs and from every other branch, but it goes by ref names: someone with write access can change a workflow on a branch, tag that commit `v0.0.1-x` or push it as `release/x`, and the run of that ref may name the environment and read the key.

What the workflows do enforce, for the version bump and for a hand-pushed tag alike, is **what** is released, not who releases it:

- the commit is in the first-parent history of the source branch or of its maintenance branch, so it went through whatever the repository requires of a merge there (a reviewed pull request, when the branch is protected so);
- `required-checks` succeeded on that very commit: by default the pull request check and the QA build of its push;
- the version is the next one of its line, and the APKs carry it.

The version bump adds the dry-run default and `confirm`. They catch a mistake; they do not stop a person who means to release.

Rulesets narrow the "who", and without them write access to the repository is the right to release and to use the signing key:

- Require pull requests, with a review, on the default branch and on `release/**` branches, so that code and workflow files change only through review. The rules above lean on this one.
- Restrict who may create, update and delete tags matching `v*`.
- Restrict who may create `release/**` branches.
- A rule on `v*` tags also refuses the version bump's own tag push, unless the actor behind its token is let through. Give the workflow a token of an actor on the rule's bypass list, such as a GitHub App, as `RELEASE_PUSH_TOKEN`, and set `release-workflow: ''`, because a tag pushed with that token starts the release through its tag trigger.

These are settings of the calling repository. The workflows can neither make them nor see whether they are made.

## Version and versionCode

Four kinds of build, and how each is named:

| build | version name | made by |
|-------|--------------|---------|
| QA build | `X.Y.Z-qa.N` | every push to a release branch, or a manual run |
| label build | `X.Y.Z-<label>.k` | a manual QA run with `label` |
| preview | `X.Y.0-preview.N` | the version bump with `prerelease: preview`, for a new minor or major version |
| release | `X.Y.Z` | the version bump |

The version of a release or a preview is its tag, and the version of a QA or label build is computed for the commit ([The version of a QA build](#the-version-of-a-qa-build), [Label builds](#label-builds)). The workflows pass it to Gradle as `-PciVersionName` and `-PciVersionCode`, with the kind as `-PciBuildKind` (`qa`, `label`, `preview` or `release`), and the calling repository's build uses the first two as they are. The kind is there for a build that names its APK after it; a build that does not read the property is not affected. A build without the properties is a local build, and what version it shows is the calling repository's choice.

The versionCode is computed in one place, [`release-version/version_code.py`](../release-version/version_code.py). A code is `X × 100000000 + Y × 100000 + R`, and `R` says where in the minor version `X.Y` the build stands:

| build | `R` | range | example |
|-------|-----|-------|---------|
| QA build `N` leading up to `X.Y.Z` | `N` | 1 to 48999 | `1.13.1-qa.57` is `101300057` |
| preview `N` of `X.Y.0` | `49000 + N` | `N` 1 to 999 | `2.0.0-preview.3` is `200049003` |
| release `X.Y.Z` | `50000 + Z × 100` | `Z` 0 to 499 | `1.13.0` is `101350000`, `1.13.1` is `101350100`, `2.0.0` is `200050000` |
| label build `k` after the release `X.Y.Z` | `50000 + Z × 100 + k` | `k` 1 to 99 | `1.13.1-ble-test.2`, made after `1.13.0`, is `101350002` |

- Each minor version has a block of 100000 codes: QA builds at the bottom, the previews of `X.Y.0` above them, and the releases in the upper half, one hundred codes apart. The ninety-nine codes after a release are the label builds made after it.
- Within a minor version the order is **QA build < preview < release `X.Y.Z` < label build after `X.Y.Z` < release `X.Y.(Z+1)`**. Android installs an APK over an installed one only when its versionCode is not lower, so:
  - a preview installs over every QA build, and a release over every preview and QA build, never the other way round;
  - a label build installs over the release it was made after, and the next patch release installs over the label build;
  - a QA build does not install over a preview or a release of its minor version. That includes the release it was built after: `1.13.1-qa.57` does not go onto a device that runs `1.13.0`.
- Within each kind, a later build is higher: QA builds in the order they are built, previews of a version in the order of their number, releases in the order of their patch number, label builds after a release in the order they are published.
- **A patch release has no preview.** The previews are numbered below every release of the minor version, so a preview of `2.0.1` could not install over `2.0.0`. The version bump refuses it and names the choices: a preview of the next minor or major version, or, to try a fix on a device before the patch is released, a label build.
- Each major version, a [release line](#release-lines), has a block of 100000000 codes. A release is higher than the earlier releases of its own line, and every code of a line, QA builds, previews and label builds included, is below every code of the next major. A 1.x release made after `2.0.0` therefore has a lower code than `2.0.0`, as it should.
- `X` goes up to 20 and `Y` up to 999, with the ranges of the table. A value outside these is refused when the build is planned: no kind takes a code of another.

A change of the formula has to keep every new code above the codes that devices already have; the tests next to the formula state the order it has to keep and the codes the calling apps have shipped. A release that shipped under an earlier formula keeps the code it has: a calling repository whose `1.13.0` is `101300000` finds the QA builds of 1.13 above that one release, and from `1.13.1` on the order above holds.

## Versioning

The workflows are released together under `android-ci/vX.Y.Z`, with `android-ci/v1` as the floating major tag.

Inside the workflows:

- Actions of this repository are referenced by full path at the workflows' own major tag (`asleep-ai/actions/<name>@android-ci/v1`), so an `android-ci` release carries the actions it was tested with. `pick-runner` and `release-notes`, released before the workflows, are referenced at `pick-runner/v1` and `release-notes/v1`. A `./` action path would be looked up in the calling repository's checkout.
- `android-pr-check.yml` calls `android-pr-check-jobs.yml`, and `android-release.yml` and `android-qa.yml` call `android-release-build.yml`, as `./.github/workflows/…`, which GitHub resolves in this repository at the same commit as the calling workflow file. The files therefore always match.
- Third-party actions are pinned to a commit. GitHub's own (`actions/*`) follow their major tags.

Pinning a workflow to a full version or a commit SHA fixes its workflow files, not everything they run: the actions of this repository are referenced at the floating `android-ci/v1`, `pick-runner/v1` and `release-notes/v1`, so a rule change in `gradle-log-verdict`, for instance, reaches a caller pinned to an older `android-ci` version with the next release.

`android-pr-check-jobs.yml` and `android-release-build.yml` are internal: their inputs change without notice, so call `android-pr-check.yml`, `android-release.yml` and `android-qa.yml`.

## Development

```bash
uv run --with pytest --with pyyaml python -m pytest android-ci -q
```

The tests read the workflow files. They run the shell of each `run` step against throwaway git repositories and stand-ins for `gradlew`, `keytool`, `gh`, `aapt2` and the GitHub API, check every combination of results and verdicts that `result` has to decide, and fail when the copies that have to match drift apart: the `verify` and `release-build` jobs, the `primary` and `fallback` calls of the build workflows, and the script that reads a commit's checks in the version bump, the release and the QA build.

They cannot evaluate GitHub's expressions or its behaviour: job conditions, job names and the expressions that put a version name together are pinned as text, and how secrets of an environment reach a called workflow, whether a push or the move of a tag is accepted under the repository's rules, under which name the check of a job appears on a commit, whether a started workflow runs, and how GitHub shows a pre-release whose tag was moved are verified only by running the workflows from a calling repository. The stand-in for `gh` in the QA tests keeps the assets of a release and records them after every change, so the tests do see whether a release was ever without its APK; they do not see what the real API answers.
