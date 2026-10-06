# asleep-ai/actions

Shared GitHub Actions, local lint hooks, and adoption templates for `asleep-ai` org repositories. Composite actions and hook scripts are versioned independently via path-prefixed tags; templates are copied into the consuming repository.

## Actions

| path | description |
|------|-------------|
| [`release-notes/`](./release-notes/) | AI-drafted markdown release notes from a git tag range, with a deterministic commit-list fallback. |
| [`pre-commit-changed/`](./pre-commit-changed/) | Run the caller's pre-commit hooks against event-specific changes, with full scans for lint configuration changes. |

## Local hooks

[`ktfmt/`](./ktfmt/) provides a shared pre-commit hook using KotlinLang style and the caller's `.editorconfig`. Its optional content-hash baseline supports gradual adoption: untouched legacy files stay exempt, edited files must pass, and routine refreshes cannot add exemptions.

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
