# pre-commit-changed

Composite action that runs the caller's [pre-commit](https://pre-commit.com)
hooks on the files a pull request or push changed. When shared lint
configuration changed, it runs them on every file instead, through the same
hooks, so baseline-aware hooks such as `ktfmt` keep exempting untouched
legacy files.

The action only selects files and runs `pre-commit run`. It installs no
tools and pins no versions, and it never commits or pushes fixes; a hook
that rewrites files fails the run and `--show-diff-on-failure` prints the
change.

## Caller contract

- The commit to check is checked out with full history
  (`actions/checkout` with `fetch-depth: 0`). For pull requests, check out
  the PR head and pass the same SHA as `head-sha`.
- `pre-commit`, and every tool its hooks need, is already on `PATH`, for
  example provisioned from the repository's `mise.toml` by `jdx/mise-action`.
- `python3` 3.9 or newer, which GitHub-hosted Ubuntu and macOS runners
  provide.

## Inputs

| input | default | description |
|-------|---------|-------------|
| `event-name` | required | The triggering event, normally `github.event_name`. |
| `base-sha` | `''` | PR base (`github.event.pull_request.base.sha`) or push previous tip (`github.event.before`). |
| `head-sha` | `github.sha` | The checked-out commit; empty means `git rev-parse HEAD`. |
| `full-scan-paths` | see [action.yml](./action.yml) | Newline-separated root-relative globs that trigger a full run. |

Inputs reach the runner as environment variables, never through shell
interpolation. `base-sha` and `head-sha` must be full 40- or 64-character
lowercase hex SHAs; anything else fails the run before git sees it. Both must
exist locally, except a push `base-sha` (see below), and `head-sha` must equal
the checked-out commit.

## Which files are checked

| event | compares |
|-------|----------|
| `pull_request`, `pull_request_target` | merge base of `base-sha` and `head-sha` to the head, so commits the base branch gained after the PR branched (also from a fork) are not attributed to it |
| `push` | `base-sha` directly to the head, covering every commit of a multi-commit push and a force push without needing a merge base; a zero `base-sha` (new branch) checks all files, and so does a `base-sha` missing from the local history, with a warning |
| anything else (`workflow_dispatch`, ...) | `base-sha` if given, otherwise the head commit's first parent; a root commit checks all files |

Hooks execute code from the checked-out commit, so do not run this on a
`pull_request_target` checkout of an untrusted PR head.

Within that range:

1. If any changed path matches `full-scan-paths`, counting every status
   including deletions and both sides of a rename, it runs
   `pre-commit run --all-files`.
2. Otherwise it passes the added, copied, modified, renamed and type-changed
   files (`git diff --name-only --diff-filter=ACMRT -z`; a symlink replaced
   by a regular file is a type change) as separate `pre-commit run --files`
   arguments. File names with spaces or a leading `-` are safe.
3. If only deletions remain, it skips pre-commit and succeeds.

`full-scan-paths` supports `*` and `?` within one path segment, `**/` for
zero or more directories, and a trailing `/**` for everything below a
directory. An empty value disables configuration-triggered full runs.

An invalid SHA, a `head-sha` that is missing or not checked out, a pull
request base that is missing or has no merge base, an explicit `base-sha` on
other events that is missing, and any git error fail the run with an
`::error::` annotation instead of checking less.

One gap is closed by checking more instead. After a force push, no ref
reaches the old tip, so a fresh clone never contains it, even with
`fetch-depth: 0`. A push whose `base-sha` is a valid SHA missing from the
local history therefore runs `pre-commit run --all-files` with a
`::warning::` annotation. The action never fetches the missing commit, so it
needs no credentials or network access; when the old tip is present, the
direct tree diff applies as usual.

pre-commit's exit status is the step's exit status.

## Usage

```yaml
on:
  pull_request:
  push:
    branches: [main]
  workflow_dispatch:

# A push run checks only before..head. If a newer push cancelled it, the
# commits it covered would never be checked, so push runs get one group per
# SHA and are never cancelled; pull request runs re-check the whole PR.
concurrency:
  group: ${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.event.pull_request.number || github.sha }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

permissions:
  contents: read

jobs:
  pre-commit:  # the job id and name are yours; required status checks key on them
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          ref: ${{ github.event.pull_request.head.sha || github.sha }}
          fetch-depth: 0
          persist-credentials: false
      - uses: jdx/mise-action@c2a87611a18de5b3828c5652fe268e992400cb5c # v4.3.0
      - uses: ./pre-commit-changed
        with:
          event-name: ${{ github.event_name }}
          base-sha: ${{ github.event.pull_request.base.sha || github.event.before }}
          head-sha: ${{ github.event.pull_request.head.sha || github.sha }}
```

`./pre-commit-changed` is a local reference for a checkout containing this
action; the caller must still supply its own pre-commit and mise configuration.
Other repositories will reference the release instead, once it is published (the
`pre-commit-changed/v1` tag does not exist yet; pin a full version or commit
SHA when it does):

```yaml
      - uses: asleep-ai/actions/pre-commit-changed@pre-commit-changed/v1
```

The action contributes a step, not a job: the job id, its name and any
required status check built on it stay in the caller's workflow.

## Development

```bash
uv run --with pytest python -m pytest pre-commit-changed -q
```

The tests build throwaway git repositories and replace pre-commit with a
stub that records its arguments; one test also runs the real pre-commit when
it is on `PATH`.
