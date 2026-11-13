# pick-runner

Composite action that decides a workflow's `runs-on` before its jobs are queued: the caller's self-hosted labels when at least one runner carrying all of them is online, otherwise a GitHub-hosted fallback.

A job queued for self-hosted labels while every matching runner is offline does not fail; it waits in the queue for up to 24 hours. The choice therefore has to be made up front, in a job on a GitHub-hosted runner, which still answers when the self-hosted host is down.

## Usage

```yaml
jobs:
  pick-runner:
    runs-on: ubuntu-latest  # must be GitHub-hosted
    timeout-minutes: 3
    outputs:
      runs-on: ${{ steps.pick.outputs.runs-on }}
      self-hosted: ${{ steps.pick.outputs.self-hosted }}
    steps:
      - id: pick
        uses: asleep-ai/actions/pick-runner@pick-runner/v1
        with:
          labels: '["self-hosted","Linux","X64"]'
          token: ${{ secrets.RUNNER_READ_TOKEN }}

  build:
    needs: pick-runner
    runs-on: ${{ fromJSON(needs.pick-runner.outputs.runs-on) }}
    steps:
      - run: echo "self-hosted=${{ needs.pick-runner.outputs.self-hosted }}"
```

No checkout is needed; the action only calls the GitHub API.

## Inputs

| name | required | default | description |
|------|----------|---------|-------------|
| `labels` | yes | | Self-hosted `runs-on` labels as a JSON array of strings. |
| `token` | yes | | Token that can list the organization's self-hosted runners (see [Token](#token)). When empty, the check is skipped (see [Rules](#rules)). |
| `fallback` | no | `ubuntu-latest` | `runs-on` when no matching runner is online: one label, or a JSON array of labels. |
| `organization` | no | `github.repository_owner` | Organization whose runners are checked. |

## Outputs

| name | description |
|------|-------------|
| `runs-on` | JSON for `runs-on: ${{ fromJSON(...) }}`: the `labels` array or the fallback. |
| `self-hosted` | `'true'` when the self-hosted labels were picked, else `'false'`. |
| `online-count` | Online runners carrying every label. Empty when the check could not run. |

## Rules

| state | picks | log |
|-------|-------|-----|
| At least one online runner carries every label | `labels` | |
| No matching runner is online | `fallback` | warning |
| No token, API error, or unreadable response | `labels` | warning |
| Invalid `labels`, `fallback` or `organization` | — (step fails) | error |

- Busy runners count as available: waiting for one beats moving the run elsewhere.
- A runner matches when it carries every label. Labels compare case-insensitively, as GitHub matches `runs-on` labels; extra labels on the runner do not matter.
- A failed check is not evidence that the runners are offline, so it keeps the self-hosted labels instead of silently moving every run to GitHub-hosted runners. Watch for the `Runner check failed` warning.
- Every page of the runner list is read. Pagination links are only followed on the API host the token was issued for.

## Token

`GITHUB_TOKEN` cannot list organization runners. Pass a fine-grained personal access token or a GitHub App installation token with the **organization** permission **Self-hosted runners: Read**, typically as an organization secret. Read access is enough; the action never changes runners.

Secrets are not passed to workflows triggered from forks or by Dependabot, so in those runs the token is empty and the check is skipped with a warning.

## Out of scope

- Re-running a job on GitHub-hosted runners after it failed on a self-hosted runner. That policy belongs to the calling workflow.
- Monitoring or alerting on runner health.
