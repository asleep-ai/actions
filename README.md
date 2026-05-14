# asleep-ai/actions

Shared GitHub Actions for `asleep-ai` org repositories. Each subdirectory is one composite action, versioned independently via path-prefixed tags.

## Actions

| path | description |
|------|-------------|
| [`release-notes/`](./release-notes/) | AI-drafted markdown release notes from a git tag range, with a deterministic commit-list fallback. |

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

## Visibility policy

This repo is **private**. The actions inside have no proprietary content, but keeping the surface private avoids accidental external-user support burden. Spin out an action to its own public repo only when external adoption is the actual goal.
