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

If the `pick-runner` job itself fails (an invalid input, a GitHub-hosted outage, its timeout), the jobs that need it are skipped. A skipped job [counts as passing for a required status check](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/troubleshooting-required-status-checks), so a pull request could then merge without `build` having run. To send the jobs to the self-hosted labels instead, let them run anyway and repeat the labels as the default:

```yaml
  build:
    needs: pick-runner
    if: ${{ !cancelled() }}
    runs-on: ${{ fromJSON(needs.pick-runner.outputs.runs-on || '["self-hosted","Linux","X64"]') }}
```

`!cancelled()` also lets `build` run when any other job it needs has failed. If it needs more than `pick-runner`, check those jobs' `needs.<job>.result` in the same `if`.

## Inputs

| name | required | default | description |
|------|----------|---------|-------------|
| `labels` | yes | | Self-hosted `runs-on` labels as a JSON array of strings. |
| `token` | yes | | Token that can list the organization's self-hosted runners (see [Token](#token)). When empty, the check is skipped (see [Rules](#rules)). |
| `fallback` | no | `ubuntu-latest` | `runs-on` when no matching runner is online: one label (`ubuntu-latest`), or a JSON array of labels. A `runs-on` object (`group:`) is not supported. An empty value means the default. |
| `organization` | no | `github.repository_owner` | Organization whose runners are checked. An empty value means the default. |

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
| No token, API error, unreadable response, or any other error while reading the runners | `labels` | warning |
| Invalid `labels`, `fallback` or `organization` | — (step fails) | error |

- Busy runners count as available: waiting for one beats moving the run elsewhere.
- A runner matches when it carries every label. Labels compare case-insensitively, as GitHub matches `runs-on` labels ([labels are case-insensitive](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/apply-labels)); extra labels on the runner do not matter.
- Only the organization's runner list is read. A runner registered on a repository is not counted, and runner-group access is not checked: a runner in a group the calling repository cannot use may still be counted.
- A failed check is not evidence that the runners are offline, so it keeps the self-hosted labels instead of silently moving every run to GitHub-hosted runners. Watch for the `Runner check failed` warning.
- Every page of the runner list is read. Pagination links and redirects are only followed on the API host the token was issued for.
- A runner entry that does not look as expected (a status other than `online` or `offline`, labels in another shape), or a list shorter than its `total_count`, is an unreadable response, not a runner that does not match.
- A `fallback` that is not shaped like one label or an array of labels fails the step: a quoted JSON string is unwrapped, but an object, a value that still starts with a quote or a bracket, or a single label containing a comma or whitespace is rejected. Only the shape is checked. A misspelt label passes, and no runner has it, so the jobs would wait in the queue.

## Token

`GITHUB_TOKEN` cannot list organization runners. Pass a fine-grained personal access token or a GitHub App installation token with the **organization** permission **Self-hosted runners: Read**, typically as an organization secret. Read access is enough; the action never changes runners.

Secrets are not passed to workflows triggered from forks or by Dependabot, so in those runs the token is empty and the check is skipped with a warning.

## Out of scope

- Re-running a job on GitHub-hosted runners after it failed on a self-hosted runner. That policy belongs to the calling workflow.
- Monitoring or alerting on runner health.
