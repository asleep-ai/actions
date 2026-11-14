# asleep-ai/actions

Shared GitHub Actions, reusable workflows, local lint hooks, and adoption templates for `asleep-ai` org repositories. Composite actions and hook scripts are versioned independently via path-prefixed tags, the reusable workflows as one set; templates are copied into the consuming repository.

## Actions

| path | description |
|------|-------------|
| [`release-notes/`](./release-notes/) | AI-drafted markdown release notes from a git tag range, with a deterministic commit-list fallback. |
| [`pre-commit-changed/`](./pre-commit-changed/) | Run the caller's pre-commit hooks against event-specific changes, with full scans for lint configuration changes. |
| [`android-signing/`](./android-signing/) | Keystore and signature steps of a signed Android build: decode the keystore, verify the APK with `apksigner`, delete the keystore and secret files. |
| [`gradle-log-verdict/`](./gradle-log-verdict/) | Report how a Gradle job ended (`pass`, `code` or `infra`) from step outcomes and the Gradle log, so only infrastructure failures are re-run elsewhere. |
| [`pick-runner/`](./pick-runner/) | Decide `runs-on` before jobs are queued: self-hosted labels when a matching runner is online, else a GitHub-hosted fallback. |
| [`release-version/`](./release-version/) | Plan the next release of a repository whose versions are git tags: the last release tag, the next version, the commit to release and the pull requests it adds. Also plans a preview of a new minor or major version, a QA build of a branch and a label build (a named test build made after a release), and computes the Android versionCode. |

## Reusable workflows

| path | description |
|------|-------------|
| [`android-pr-check.yml`](./.github/workflows/android-pr-check.yml) | Pull request check for Android app repositories: code style, a verification build and a release build on self-hosted runners, re-run on a GitHub-hosted runner only after an infrastructure failure, with one aggregate check. On a push to a release branch the release build is left to the QA build of that push. |
| [`android-version-bump.yml`](./.github/workflows/android-version-bump.yml) | Release cut, started by hand for a merged pull request: the next version from the tags (or an exact version), and a check of the commit. A dry run by default; with `dry_run` off and the planned version repeated in `confirm`, the tag `vX.Y.Z` on that commit, at once and without an approval. No branch is moved. Maintenance branches (`release/<major>.x`) are release lines of their own. |
| [`android-release.yml`](./.github/workflows/android-release.yml) | Release of a tag `vX.Y.Z`: a check that the tagged commit is on a release branch and passed the pull request check and the QA build of its push, a cold build signed with the caller's keystore, a check of every APK's version and signature, and a GitHub Release with the APKs and release notes. A tag `vX.Y.0-preview.N`, which the version bump creates with `prerelease: preview` for a new minor or major version, is built the same way and published as a pre-release. |
| [`android-qa.yml`](./.github/workflows/android-qa.yml) | QA build of every push to a release branch (the default branch, `release/<major>.x`) and of a manual run on one: the same signed build with a QA version (`X.Y.Z-qa.N`) below every preview and release, published as the rolling pre-release of its line (`qa-develop`) once the commit passed the pull request check. Each build replaces the APK of the one before. On a self-hosted runner the build keeps its Gradle user home for the QA builds after it, in a directory no other build uses; a release is built cold. Its check, `qa / built`, is what a release requires of the commit's release variant. A manual run with a label makes a label build (`X.Y.Z-<label>.k`): a named test build that installs over the last release, and the next patch release over it. |

All four are documented in [`android-ci/`](./android-ci/). The calling repository keeps a short workflow file per workflow and the Gradle tasks they run (`ciVerify`, `ciReleaseBuild`, `ciRelease`, `ciQa`). The workflows are released together under `android-ci/vX.Y.Z`.

## Local hooks

[`ktfmt/`](./ktfmt/) provides a shared pre-commit hook using KotlinLang style and the caller's `.editorconfig`. Its optional content-hash baseline supports gradual adoption: untouched legacy files stay exempt, edited files must pass, and routine refreshes cannot add exemptions.

[`detekt/`](./detekt/) provides a shared pre-commit hook that runs `detekt-cli` on the selected Kotlin files with the caller's configuration, baseline and plugin jars.

The CI action runs the same `.pre-commit-config.yaml` used locally. Formatting rules, tool versions, detekt configuration, and Gradle task selection remain caller-owned. See each component's README for setup and migration examples.

[`ruff/`](./ruff/) contains native Ruff adoption examples rather than another installer action, including independent lint and format checks with a combined failure result.

## Tool versions

Shared hooks use the caller's mise configuration rather than imposing an organization-wide tool version. Explicit versions are recommended for reproducible formatting, but callers may deliberately use floating versions and accept formatter changes on a later run. Tool upgrades should include a full scan to expose new findings, including changes to baseline eligibility.

For floating tools, run the native `pre-commit run --all-files` periodically after provisioning the tools, since an upstream tool update does not change a configuration file and therefore cannot trigger the changed-file action's configuration detection.

The shared component revision and the formatter version are separate choices. GitHub Actions callers can choose a floating major tag as described below. Pre-commit caches hook revisions, so moving a tag does not reliably update an existing local installation; use an immutable release revision and update it deliberately.

## Versioning

Path-prefixed tags so each action releases on its own cadence inside this monorepo:

```
release-notes/v1.0.0
release-notes/v1.1.0
<future-action>/v1.0.0
```

Consumers pin to the major (`@release-notes/v1`) for floating bug-fix updates, or to a full version (`@release-notes/v1.0.0`) for reproducibility. The repo-level tag (`v1`) is intentionally unused — actions are versioned individually.

## Adding a new action

1. New subdirectory at the repo root (`<name>/`).
2. `<name>/action.yml` (composite action).
3. `<name>/README.md` documenting inputs/outputs and the caller contract.
4. Tag the release: `<name>/v1.0.0`.
5. Create a GitHub Release scoped to that tag.

## Visibility

This repo is **public**. The actions have no proprietary content and the OPENAI_API_KEY (or equivalent) is always caller-injected, so making them inspectable carries no real cost. External adoption is not a goal -- issues filed by non-org users may not get prompt triage.
