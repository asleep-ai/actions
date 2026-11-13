# android-ci

Reusable workflows for Android app repositories. The calling repository keeps a short workflow file per workflow and a few Gradle tasks; the jobs, the runner choice and the re-run policy live here.

| workflow | description |
|----------|-------------|
| [`android-pr-check.yml`](../.github/workflows/android-pr-check.yml) | Pull request check: code style, a verification build and a release build, with a GitHub-hosted re-run after an infrastructure failure on self-hosted runners. |
| [`android-version-bump.yml`](../.github/workflows/android-version-bump.yml) | Release cut, started by hand: computes the next version from the tags, checks the commit, and after approval tags it `vX.Y.Z`. |
| [`android-release.yml`](../.github/workflows/android-release.yml) | Release of a tag `vX.Y.Z`: a check of the tagged commit, a cold, signed build, a check of the APK, and a GitHub Release with the APK attached. |

A version exists only as a git tag. No file in the calling repository states it, and no branch marks it:

```
pull request merged ──▶ pr-check.yml on the push ──▶ `pr-check / result` on the merge commit
        │
        ▼
version-bump.yml (by hand: pr, bump) ──▶ plan ──▶ approval ──▶ tag vX.Y.Z
                                                                      │
                                                                      ▼
release.yml (for the tag) ──▶ plan ──▶ build with -PciVersionName, -PciVersionCode ──▶ GitHub Release
```

How the three fit together is in [Branches, tags and releases](#branches-tags-and-releases).

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
| `full-scan-paths` | the list of [`pre-commit-changed`](../pre-commit-changed/) | Glob patterns, one per line. A change to a matching path makes `code-style` run pre-commit on every file. Empty: a changed path never switches the job to every file. pre-commit-changed still checks every file on a push whose previous commit it cannot find (a new branch, a force push). |
| `verify-args` | `''` | Extra Gradle arguments for the `verify` job. |
| `verify-signing-key` | `true` | Whether `verify` gets the throwaway signing key. See [local.properties](#localproperties). |
| `release-build-args` | `''` | Extra Gradle arguments for the `release-build` job. |
| `release-build-skip-push-branches` | `''` | Branch names, separated by whitespace or commas. A push to one of them skips the release build. For a branch whose pushes another workflow already builds the release variant for. `result` of such a push then says nothing about the release variant, and `result` is what a release of that commit requires: on a branch that is released from, list it only when the other workflow's check is in the `required-checks` of the version bump and of the release. |
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
| `push` | The pushed commit, always, whatever the push changed. See [Skipped builds](#skipped-builds). |
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

**Only a pull request skips. A `push` run always builds**, on every branch and with every `paths-ignore`:

- `result` of the push run is the check that [`android-version-bump.yml`](#what-is-released) and [`android-release.yml`](#the-tag-1) require of the commit they release. It has to mean "this commit was built and tested".
- A skip decides from what the push changed, not from the state of the commit before it. When the previous commit's run failed, was cancelled or was replaced while it waited, a documentation-only merge on top of it would be green without any build, and a release of it would ship code that never passed. A merge that changes no file at all, such as a merge of an already identical branch, would be skipped the same way.
- This holds without anything in the calling file: there is no input that turns it off. The cost is one build for each documentation-only merge.

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
      dry_run:
        type: boolean
        default: false

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
      dry_run: ${{ inputs.dry_run }}
```

[`version-bump.yml.example`](./version-bump.yml.example) is a complete file.

- `permissions` must grant the four scopes above: the tag (`contents: write`), starting the release workflow (`actions: write`), reading the commit's checks and the pull request.
- No secret is needed. See [Secrets](#secrets-1) for the one optional token.
- Create the `release-approval` environment with its required reviewers before the first run ([Approval and dry run](#approval-and-dry-run)).
- Give the calling file one `concurrency` group without cancellation, so two releases never run at once. A run that waits for its approval holds the group, and GitHub keeps one waiting run per group: a third run started while one runs and one waits replaces the waiting one. The group above leaves dry runs out of it: a dry run writes nothing, so it starts at once and never replaces a release that waits.

### Jobs

| job | runs on | what it does |
|-----|---------|--------------|
| `plan` | GitHub-hosted | Read-only. Computes the next version with [`release-version`](../release-version/), checks the commit, and writes the plan to the job summary. |
| `tag` | GitHub-hosted, in `environment` | Waits for the environment's approval, tags the commit and starts `release-workflow`. Skipped on a dry run. |

### What is released

- **The commit** is the merge commit of the pull request named by `pr`: the commit the merge left on its base branch. `sha` names a commit directly and is the exception, for a commit that no pull request produced.
- **The version** is the last release raised by `bump`. The last release is the highest `vX.Y.Z` among the tags the commit contains, compared as numbers. Tags with a suffix (`v1.2.3-rc.1`) or of another form are not releases. A repository without a release tag counts from `0.0.0`, so `minor` gives `v0.1.0`.
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
- a check named in `required-checks` has not succeeded on the commit. The newest run of each check counts, and a check that never ran on the commit is a refusal;
- the version has no versionCode under [the formula](#version-and-versioncode).

These refusals happen in `plan`, so a dry run shows them and nobody is asked to approve a release that cannot happen. The `tag` job can still fail on what only shows while writing: the environment does not allow the branch the run was started from, the version was taken by another release while the run waited ([Running again](#running-again)), or the repository's rules refuse the push.

`required-checks` defaults to `pr-check / result`, the check of [`android-pr-check.yml`](#android-pr-checkyml) when the calling job is named `pr-check`. The merge commit gets that check from the `push` run of the pull request check on its branch, so the calling repository has to run it on pushes to that branch, and that run always builds ([Skipped builds](#skipped-builds)). A run that was cancelled leaves the check `cancelled`, and a commit that was pushed together with later ones has no check; run the pull request check again for that commit, or release a later one.

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

To start a maintenance line, create `release/<major>.x` at the last release of that major (or at a later commit that belongs to it) before the source branch releases the next major. Add `release/**` to the pull request check's branches, and `release/*` to the allowed branches of the `release` environment when a job that signs runs on those branches.

### The tag

- The tag is an annotated tag on the commit itself. No commit is created: the version is in no file.
- Nothing else is written. No branch is moved: the tag and the GitHub Release that `android-release.yml` publishes for it are the whole record of a release ([Branches, tags and releases](#branches-tags-and-releases)).
- The push uses the job token. A repository whose rules restrict who may create `v*` tags provides `RELEASE_PUSH_TOKEN` ([Who can release](#who-can-release)).

`main-branch` is an option for a repository that wants a branch to follow its releases; it is empty by default. When it is set, a release from the source branch moves that branch to the tagged commit as a fast-forward, in one atomic push with the tag: when the branch cannot move, no tag is left behind. The plan refuses a release when the branch holds a commit the release lacks, a release from a maintenance branch does not move it, and the source branch itself is never the branch that is moved. The branch cannot be named `release`: git cannot keep a branch `release` next to the maintenance branches `release/<major>.x`.

### Starting the release

GitHub starts no workflow for a push made with the job token, so the new tag alone would not start the release. The `tag` job therefore starts `release-workflow` for the tag itself, as a manual run: starting a workflow by hand is one of the two events the job token may cause.

- The calling repository's release workflow needs the `workflow_dispatch` trigger, and has to exist at the tagged commit.
- The release runs as a run of its own, on the tag. It can be run again without another version bump, and the rules of its environment see the tag as the ref.
- With `RELEASE_PUSH_TOKEN`, the tag push itself starts every workflow that triggers on tags. Set `release-workflow: ''` then, or the release runs twice.

### Approval and dry run

The `tag` job runs in `environment`, by default `release-approval`, and the required reviewers of that environment are the approval: the job waits until one of them approves, with the plan already in the summary of the run.

- **The approval has an environment of its own.** The signing key is in another one, `release`, which the build job of `android-release.yml` names. Reviewers on `release` would be asked once for the tag and again for the build, and again for every other job that signs with that key, such as a QA build of every push. So `release` carries only rules for branches and tags, and `release-approval` carries the reviewers and no secret.
- **Create `release-approval` before the first run**, with required reviewers and with the branch the version bump is started from (the default branch) among its allowed branches. GitHub creates an environment that a workflow names and that does not exist, without any rule: the `tag` job then runs at once, and the release has no approval.
- The approval covers releases that start here. A `vX.Y.Z` tag that a person pushes is built without it ([Who can release](#who-can-release)).

With `dry_run`, only `plan` runs. Nothing is tagged, pushed or started, and no approval is asked for.

### Running again

- Time passes between the plan and the approval. Before it pushes, the `tag` job plans the version again from the repository as it is then, and stops when the result differs: another release happened meanwhile.
- A run whose `tag` job failed can be run again with **Re-run failed jobs**: the plan is kept, and the job finds out by itself how far it got. When the tag is on the commit already, it does only what is left: starting the release.
- **Re-run all jobs**, or a new run with the same inputs, plans again. After the tag was pushed that plan fails, because the commit is now the last release and has nothing new. What is left can then be done by hand: start the release workflow for the tag.

### Inputs

| name | default | description |
|------|---------|-------------|
| `pr` | `''` | Number of the merged pull request to release. Exactly one of `pr` and `sha` is given. |
| `sha` | `''` | The exception to `pr`: id of the commit to release, 7 or more hex digits. A branch name is refused. |
| `bump` | `patch` | Which part of the last version to raise: `patch`, `minor` or `major`. Not read when `version` is given. |
| `version` | `''` | Optional. The exact version to release, `X.Y.Z`, instead of a bump. Higher than the last release the commit contains, and not tagged yet. |
| `dry_run` | `false` | Plan and check only. |
| `source-branch` | `''` | The branch releases are cut from. Empty means the repository's default branch. The commit has to be in its first-parent history, or in that of `release/<major>.x` for a major that has that branch. |
| `required-checks` | `pr-check / result` | Names of the checks that must have succeeded on the commit, one per line, as the commit's list of checks shows them. Empty requires none. |
| `main-branch` | `''` | Optional. A branch that is moved to the commit of a release from the source branch. Empty, the default, moves no branch. |
| `release-workflow` | `release.yml` | File name of the calling repository's workflow that is started for the new tag. Empty starts nothing. |
| `environment` | `release-approval` | The environment of the `tag` job. Its required reviewers are the approval. Not the environment of the signing key. |

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
    tags: ['v[0-9]+.[0-9]+.[0-9]+']
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

- `workflow_dispatch` is how [`android-version-bump.yml`](#starting-the-release) starts the release. The tag trigger covers a tag pushed by a person; such a tag is built only when it passes the same checks of branch and commit ([The tag](#the-tag-1)), and without the approval of the version bump ([Who can release](#who-can-release)). Leave the trigger out to build a tag only when someone starts the workflow for it.
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
| `ciRelease` | Gradle task in the root project that builds the release APKs, signed, and runs the uploads that belong to a release, such as mapping files. One APK, or one per flavor: which variants those are is decided in the calling repository's build, and `gradle-args` can pass it a property. |
| APK location | After `ciRelease`, the APKs of the release, and no other `.apk` file, are below `apk-directory`, by default `app/build/outputs/apk`, where the Android Gradle plugin puts the APKs of the `app` module (`release/` without flavors, `<flavor>/release/` with them). Every `.apk` file found there is checked, verified and attached. Their file names differ, because they become assets of one release. |
| Version | The build takes `versionName` from `-PciVersionName` and `versionCode` from `-PciVersionCode`, for every APK. The workflow reads both back from each APK and fails when one differs. |
| Signing | The release variant's `signingConfig` reads `KEYSTORE_FILE`, `KEYSTORE_PASSWORD`, `KEY_ALIAS` and `KEY_PASSWORD` from `local.properties` in the repository root, as for the pull request check. |
| Other `local.properties` values | Named in `local-properties-secrets`. Each is a secret or a variable, written under its own name. |
| `SENTRY_AUTH_TOKEN` | Read by the build from the environment. Only the Gradle step has it. |
| Runners | bash, git, `python3` 3.9 or newer, and an Android SDK with a build-tools package (`apksigner`, `aapt2`) that is found through `ANDROID_HOME`. GitHub-hosted Ubuntu runners have all of it. |

### Inputs

| name | default | description |
|------|---------|-------------|
| `tag` | `''` | The release tag to build. Empty means the ref the run started for: the tag on a tag push, and on a manual run started for a tag. |
| `runner-labels` | `''` | Self-hosted `runs-on` labels as a JSON array of strings. Empty builds on `fallback-runs-on`, with no runner check and no re-run. |
| `fallback-runs-on` | `ubuntu-latest` | GitHub-hosted `runs-on`: a bare label or a JSON array of labels, as for the pull request check. |
| `environment` | `release` | The environment of the build job. See [Environment](#environment). |
| `java-version` | `17` | JDK version for the build. |
| `java-distribution` | `temurin` | JDK distribution for `actions/setup-java`. |
| `checkout-submodules` | `false` | `submodules` of `actions/checkout` in the build: `false`, `true` or `recursive`. Anything but `false` checks out with `GIT_TOKEN`. |
| `local-properties-secrets` | `''` | Names of secrets and variables, separated by whitespace or commas, written to `local.properties` under their own names. |
| `required-secrets` | `''` | Names of secrets and variables that must not be empty and are not written to `local.properties`, such as `SENTRY_AUTH_TOKEN`. |
| `gradle-args` | `''` | Extra Gradle arguments for `./gradlew ciRelease`, split on whitespace without glob expansion. |
| `apk-directory` | `app/build/outputs/apk` | The directory `ciRelease` leaves the release APKs in, relative to the repository root. Every `.apk` file below it is released. |
| `expected-cert-sha256` | `''` | Optional SHA-256 digest of the signing certificate every APK must carry, as [`android-signing`](../android-signing/#comparing-the-certificate) takes it. Empty checks only that the signatures are valid. |
| `build-tools-version` | `''` | Optional build-tools version whose `apksigner` and `aapt2` check the APKs. Empty uses the highest installed version. |
| `source-branch` | `''` | The branch releases are cut from. Empty means the repository's default branch. The tagged commit has to be in its first-parent history, or in that of `release/<major>.x`, the maintenance branch of the tag's major. |
| `required-checks` | `pr-check / result` | Names of the checks that must have succeeded on the tagged commit, one per line. Checked for every tag, whoever pushed it. Empty requires none. |
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
- A name is looked up among the secrets, then among the variables. For each, GitHub gives the job the closest value: the environment's, then the repository's, then the organization's. Where a value is kept is therefore the calling repository's choice; the signing secrets belong in the environment (see below).
- That step is the only one that is handed every secret. It runs after the work folder was emptied, and its script imports nothing from the work folder. It writes the values for `local.properties` to a file in the job's temp directory, readable by the runner's user only, and a later step moves the file into place with the keystore path as `KEYSTORE_FILE`.
- Values are written with the escapes of `java.util.Properties`, so a backslash, a leading space, a line break or a character outside ASCII in a secret reaches the build unchanged.
- The keystore, `local.properties` and the prepared file are deleted at the end of the job, whatever happened before.

### Environment

The build job runs in `environment`, by default `release`, which holds the four signing secrets:

- Secrets of an environment are read only by jobs that name it, and only in runs on a ref that the environment's rule for deployment branches and tags allows. Keeping the signing secrets there, and not among the repository's secrets, keeps them from pull request runs and from runs on any other branch.
- Set that rule to selected branches and tags: the release tags (`v*`) for this workflow, whatever line they belong to, and the branches of the calling repository's other jobs that sign, such as a QA build of the default branch (and `release/*` for one on maintenance branches). A run on any other ref cannot start a job in the environment.
- **No required reviewers here.** They would make every job that names the environment wait: the build, the `fallback` build again, and those other jobs on every push. The approval of a release is the environment of the version bump's `tag` job ([Approval and dry run](#approval-and-dry-run)).
- **The environment is optional.** A repository can leave `release` uncreated: GitHub creates it without rules on the first run, and the job reads the signing secrets from the repository or the organization. The key is then readable by any workflow of the repository; create the environment with the rule above to limit it to the release tags and the named branches.
- `plan` and `publish` name no environment and read no signing secret.
- `android-version-bump.yml` does not use this environment, so the branch it is started from does not have to be allowed here.

What the rule does not do is in [Who can release](#who-can-release).

### The tag

`plan` stops the release before any build, and before any job reads the signing key, when:

- the tag is not of the form `vX.Y.Z`, or does not exist. A branch, or a tag with a suffix, is never built;
- the tag is below the highest release tag of its major and has no GitHub Release yet. A new release with a lower version would carry a lower versionCode than the devices of that line already have. Building an older tag again for its existing release is allowed, and so is a new release of a lower major while a higher one exists ([Release lines](#release-lines));
- the tagged commit is in the first-parent history of neither `source-branch` nor `release/<major>.x`, the maintenance branch of the tag's major: a commit of an open pull request, of a branch that was never merged or of another line, or the head commit of a pull request that was merged with a merge commit;
- a check named in `required-checks` has not succeeded on the tagged commit. The newest run of each check counts, and a check that never ran on the commit is a refusal. The default is `pr-check / result`, which the commit gets from the `push` run of the pull request check on its branch, a run that always builds ([Skipped builds](#skipped-builds)).

The last two are the rules of [`android-version-bump.yml`](#what-is-released), applied again to the tag. They hold for every run of this workflow: a tag the version bump created passes them a second time, and a tag that a person pushed, or a run that a person started for a tag, is held to them all the same. What such a tag does skip is the approval ([Who can release](#who-can-release)).

A tag from before the calling repository had this pull request check has no such check on its commit, and is refused.

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

- At least one APK has to be there, and no two may share a file name.
- `aapt2 dump badging` reads each APK's `versionName` and `versionCode`, and both have to be the planned ones. A build that ignores `-PciVersionName` or `-PciVersionCode`, for one flavor or for all, fails here instead of being released under another version.
- [`android-signing`](../android-signing/) verifies the signature of each APK and, with `expected-cert-sha256`, its signing certificate.
- The APKs are uploaded together as the artifact `android-release-apk-<primary|fallback>`, kept for 90 days.

### The GitHub Release

- A new release gets the tag as its title, notes from [`release-notes`](../release-notes/) and every APK of the build.
- The notes cover what the release adds to the previous release of its own line: the highest lower release tag the tagged commit contains that has a published release. `v1.13.2`, cut from `release/1.x` after `v2.0.0` exists, starts at `v1.13.1`; `v2.0.0` starts at the 1.x release the source branch grew from. A lower tag that was never published, because its build failed and the fix got the next version, is not a start, so its changes are in these notes. When there is no such release, as for the first one, the notes cover the whole history; a tag with a suffix (`vX.Y.Z-label.N`) is never a start.
- Only the highest version of the repository is created as the latest release (`--latest=true`). A release of a lower major is created with `--latest=false`, whenever it is made.
- When the release of the tag exists already, its title, notes and latest mark are kept, because they may have been edited, and the APKs are uploaded again. An asset of the same name is replaced; an APK with another name is added next to it.
- Running the release of a tag again is therefore safe. An APK the first run did not build, such as another flavor selected with a `gradle-args` property, is a second run for the same tag.

### How a run is decided

The build job reports a verdict like the Gradle jobs of the pull request check: `pass`, `code` or `infra`, from [`gradle-log-verdict`](../gradle-log-verdict/).

- A missing secret or variable is `code`: no other runner has it either.
- APKs that cannot be released are `code` as well: none under `apk-directory`, two with one file name, or one whose version is not the planned one. The build left them, and a second cold build on another runner would leave the same.
- Any other failed step after a successful Gradle run (an SDK without `aapt2`, the signatures, the upload) is `infra`, as the verdict of every doubtful case is.

The `fallback` build starts when the primary build ran on a self-hosted runner, its result is `failure` or `cancelled` (a timeout), its verdict is not `code`, and the run was not cancelled. `publish` runs when either build passed and takes that build's APKs. It adds a warning annotation when they came from the fallback build.

After a failed primary build the run stays red even when the fallback build passed and the release was published. The summary of `publish` states the outcome.

## Branches, tags and releases

The three workflows share one picture of the calling repository:

| ref | role |
|-----|------|
| the default branch (`source-branch`) | The line of the current major version. Pull requests are merged here, and its releases are cut here. |
| `release/<major>.x` | The line of an older major that is still maintained ([Release lines](#release-lines)). |
| tags `vX.Y.Z` | The releases. A tag is created by the version bump and never moved. |
| GitHub Releases | The published APKs and notes of each tag. |

- **A release is its tag and its GitHub Release, and nothing else records it.** The newest release is the one GitHub marks **Latest**, the highest `vX.Y.Z` tag that has a release; the last release of an older line is the highest such tag of that major.
- **No branch follows the releases.** The workflows write no branch. A `main` branch that an earlier flow moved or merged into with every release is frozen: nothing here reads it or writes it. Block pushes to it with a ruleset, so that it cannot drift into a second, unchecked history. That is a setting of the repository, which a person makes; the workflows cannot. Take the branch out of the pull request check's triggers as well.
- **A hotfix starts from a tag or from a maintenance branch.** For the current major, branch from the released tag (`git switch -c fix/crash v2.0.1`) and merge the fix into the default branch through a pull request; the release of that pull request contains everything the default branch had before it. For an older major, branch from `release/<major>.x` and merge the pull request there.
- **No branch is named `release`.** Git cannot keep a branch `release` next to the maintenance branches `release/<major>.x`: a ref cannot be both a name and the directory of other names.
- A repository whose earlier releases were merge commits on `main` that the default branch never received merges `main` into the default branch once, with a merge commit and not a squash. Until then the default branch does not contain the last release tag, and the version bump refuses.

### Who can release

A release through `android-version-bump.yml` is approved by a reviewer of `release-approval`, for a commit whose branch and checks the plan verified. Two things are outside that, and both follow from how GitHub works, not from an option of these workflows:

- **A tag can be pushed by hand.** Everyone with write access can push a tag `vX.Y.Z`, or start the release workflow for one. `android-release.yml` then verifies the branch and the checks of the tagged commit itself ([The tag](#the-tag-1)), so code that was not merged or did not pass is not built. Nobody approves that release, though: the approval is a rule of the version bump's environment, and the release does not run in it.
- **The `release` environment trusts ref names.** Its rule allows `v*` tags and the named branches, whatever workflow file the commit behind such a ref holds. Someone with write access can change a workflow on a branch, tag that commit `v0.0.1-x` or push it as `release/x`, and the run of that ref may name the environment and read the signing key. The environment keeps the key from pull request runs and from every other branch; it does not keep it from people who can create the refs it allows.

Rulesets close both, and without them write access to the repository is the right to release and to use the signing key:

- Restrict who may create, update and delete tags matching `v*`.
- Restrict who may create `release/**` branches, and require pull requests on them and on the default branch, so that a workflow file changes only through review.
- A rule on `v*` tags also refuses the version bump's own tag push, unless the actor behind its token is let through. Give the workflow a token of an actor on the rule's bypass list, such as a GitHub App, as `RELEASE_PUSH_TOKEN`, and set `release-workflow: ''`, because a tag pushed with that token starts the release through its tag trigger.

These are settings of the calling repository. The workflows can neither make them nor see whether they are made.

## Version and versionCode

The version of a build is the tag. The workflows pass it to Gradle as `-PciVersionName` (`X.Y.Z`) and `-PciVersionCode`, and the calling repository's build uses the two values as they are. A build without them is a local build, and what version it shows is the calling repository's choice.

The versionCode is computed in one place, [`release-version/version_code.py`](../release-version/version_code.py):

| build | versionCode |
|-------|-------------|
| release `X.Y.Z` | `X × 100000000 + Y × 100000 + 50000 + Z` |
| QA build `N` after a release of `X.Y` | `X × 100000000 + Y × 100000 + N` |

- Each minor version has a block of 100000 codes: QA builds in the lower half, releases in the upper half.
- Within a minor version, a release installs over every QA build, and a QA build never installs over a release.
- Each major version, a [release line](#release-lines), has a block of 100000000 codes. A release is higher than the earlier releases of its own line, and every code of a line, QA builds included, is below every code of the next major. A 1.x release made after `2.0.0` therefore has a lower code than `2.0.0`, as it should.
- `X` goes up to 20, `Y` up to 999, `Z` and `N` up to 49999. A version outside these is refused when the release is planned.
- `1.13.1` is `101350001`; `0.1.0` is `150000`.

Android installs an APK over an installed one only when its versionCode is not lower. A change of the formula therefore has to keep every new code above the codes that devices already have; the tests next to the formula state the order it has to keep.

## Versioning

The workflows are released together under `android-ci/vX.Y.Z`, with `android-ci/v1` as the floating major tag.

Inside the workflows:

- Actions of this repository are referenced by full path at the workflows' own major tag (`asleep-ai/actions/<name>@android-ci/v1`), so an `android-ci` release carries the actions it was tested with. `pick-runner` and `release-notes`, released before the workflows, are referenced at `pick-runner/v1` and `release-notes/v1`. A `./` action path would be looked up in the calling repository's checkout.
- `android-pr-check.yml` calls `android-pr-check-jobs.yml`, and `android-release.yml` calls `android-release-build.yml`, as `./.github/workflows/…`, which GitHub resolves in this repository at the same commit as the calling workflow file. The two files of a pair therefore always match.
- Third-party actions are pinned to a commit. GitHub's own (`actions/*`) follow their major tags.

Pinning a workflow to a full version or a commit SHA fixes its workflow files, not everything they run: the actions of this repository are referenced at the floating `android-ci/v1`, `pick-runner/v1` and `release-notes/v1`, so a rule change in `gradle-log-verdict`, for instance, reaches a caller pinned to an older `android-ci` version with the next release.

`android-pr-check-jobs.yml` and `android-release-build.yml` are internal: their inputs change without notice, so call `android-pr-check.yml` and `android-release.yml`.

## Development

```bash
uv run --with pytest --with pyyaml python -m pytest android-ci -q
```

The tests read the workflow files. They run the shell of each `run` step against throwaway git repositories and stand-ins for `gradlew`, `keytool`, `gh`, `aapt2` and the GitHub API, check every combination of results and verdicts that `result` has to decide, and fail when the copies that have to match drift apart: the `verify` and `release-build` jobs, and the `primary` and `fallback` calls of both workflow pairs.

They cannot evaluate GitHub's expressions or its behaviour: job conditions are pinned as text, and how secrets of an environment reach a called workflow, whether a push is accepted under the repository's rules, and whether a started workflow runs are verified only by running the workflows from a calling repository.
