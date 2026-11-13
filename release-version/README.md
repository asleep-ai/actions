# release-version

Composite action for repositories whose versions are git tags. One input, `operation`, selects what a step does:

| operation | what it does |
|-----------|--------------|
| `next` (default) | Resolves the commit to release, finds the last release it builds on, computes the next version, checks that it can be released and lists the pull requests it adds. |
| `code` | Computes the Android versionCode of a release or of a QA build. |

The action reads and computes. It creates no tag and pushes nothing; [`android-version-bump.yml`](../android-ci/README.md#android-version-bumpyml) is the workflow that does, with this action's outputs.

## Usage

```yaml
jobs:
  plan:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: read
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0  # required: every tag and the whole history

      - name: Plan the version
        id: version
        uses: asleep-ai/actions/release-version@release-version/v1
        with:
          pr: ${{ inputs.pr }}
          bump: minor

      - env:
          TAG: ${{ steps.version.outputs.tag }}
          PREVIOUS_TAG: ${{ steps.version.outputs.previous-tag }}
          TARGET_SHA: ${{ steps.version.outputs.target-sha }}
        run: echo "$PREVIOUS_TAG → $TAG at $TARGET_SHA"
```

The versionCode of a tag:

```yaml
      - id: code
        uses: asleep-ai/actions/release-version@release-version/v1
        with:
          operation: code
          version: v1.13.1  # or 1.13.1
```

The `release-version/v1` tag does not exist until the first release; pin a full version or commit SHA once it does.

## Inputs

| name | used by | default | description |
|------|---------|---------|-------------|
| `operation` | all | `next` | `next` or `code`. |
| `pr` | `next` | `''` | Number of a merged pull request; its merge commit is released. Exactly one of `pr` and `sha` is given. |
| `sha` | `next` | `''` | The exception to `pr`: id of the commit to release, 7 or more hex digits. |
| `bump` | `next` | `patch` | Which part of the last version to raise: `patch`, `minor` or `major`. The parts below it become 0. |
| `source-branch` | `next` | `''` | Optional. The branch releases are normally cut from. When given, the commit has to be on the branch of its release line. See [Release lines](#release-lines). |
| `tag-prefix` | all | `v` | What a release tag starts with, before `X.Y.Z`. Letters, digits and `. _ / -`, not starting with `-`; may be empty. |
| `token` | `next` with `pr` | `github.token` | Token that can read the pull request (`pull-requests: read`). |
| `version` | `code` | `''` | The version, as `X.Y.Z` or as its tag. |
| `kind` | `code` | `release` | `release`, or `qa` for a QA build made after the release named by `version`. |
| `qa-sequence` | `code` with `qa` | `''` | Number of the QA build, 1 or higher. A later build has a higher number. |

Inputs reach the script as environment variables, never through shell interpolation.

## Outputs

| name | set by | description |
|------|--------|-------------|
| `version` | all | The version as `X.Y.Z`. For `next`, the next version. |
| `tag` | `next` | The tag of the next version, `<tag-prefix>X.Y.Z`. |
| `previous-tag` | `next` | The last release the commit contains. Empty when the repository has no release tag. |
| `target-sha` | `next` | Full id of the commit to release. |
| `branch` | `next` with `source-branch` | The branch of the release line: the source branch or `release/<major>.x`. |
| `prs` | `next` | Markdown list of what the release adds, oldest first. |
| `version-code` | all | The Android versionCode of `version`. |

## next

### The last release

- The last release is the highest `<tag-prefix>X.Y.Z` among the tags **the commit contains**, compared as numbers: `v1.10.0` is above `v1.9.3`. It is not the newest tag, not the nearest one in the history, and not the highest tag of the repository: a commit on a maintenance branch does not contain the releases the main line made after the two parted.
- Only tags of exactly that form are releases. A tag with a suffix (`v1.2.3-rc.1`, `v1.11.2-test.84`), a floating tag (`v1`), a number with a leading zero and a tag with another prefix are ignored.
- A repository without any release tag counts from `0.0.0`: `patch` gives `0.0.1`, `minor` `0.1.0`, `major` `1.0.0`.

### The commit to release

- `pr` is resolved through the GitHub API to the commit the merge left on the base branch. A pull request that is not merged is refused: an open one has a merge commit too, the test merge, which is on no branch.
- `sha` has to be a commit id. A branch or tag name is refused, because it would name another commit on the next run.
- The commit has to be in the checkout.

### Release lines

A major version is a release line. The next version continues its line, wherever in the repository that line was last released:

```
develop      ── v1.13.0 ──────── v2.0.0 ── v2.0.1      patch → v2.0.2
release/1.x     └── v1.13.1 ── v1.13.2                 patch → v1.13.3, minor → v1.14.0
```

- The last release of the next version's major, among all tags of the repository, has to be the release the commit contains. Otherwise the version would repeat one of the line or fall behind it: a commit of `develop` from before `v2.0.0` cannot become `v1.13.1` once `release/1.x` has released it.
- A commit that contains no release tag, in a repository that has some, is refused. Counting from `0.0.0` is for a repository without releases.

With `source-branch`, the commit also has to be on the branch of its line. Branches are read from the remote `origin`, as a checkout with `fetch-depth: 0` has them:

- When a branch `release/<major>.x` exists for the major of the next version, the commit has to be on it. On the source branch, `patch` or `minor` of that major is refused: the source branch has to raise the major.
- Otherwise the commit has to be on the source branch. A commit that is only on `release/1.x` cannot become `v2.0.0`: a maintenance branch releases its own major only.
- A commit of an open pull request or of a branch that was never merged is on neither, and is refused.

The `branch` output names the line's branch. Without `source-branch`, only the tag rules above apply.

### The checks

The step fails, with an `::error::` annotation that names the rule, when:

- the commit is the commit of the last release: there is nothing new;
- the last release of the next version's major is not the one the commit contains, or the commit contains no release tag while the repository has some;
- with `source-branch`: the commit is not on the branch of its release line;
- the checkout is shallow, so tags or history may be missing;
- the next version has no versionCode under the formula below.

### The pull request list

One entry per commit of the first-parent history between the last release and the commit, oldest first:

- A squash merge names its pull request at the end of the subject: `Fix the timer (#12)` becomes `- #12 Fix the timer`. When the subject ends in two numbers, the last one is the pull request.
- A merge commit `Merge pull request #12 from …` becomes `- #12` with the first line of its body, which GitHub fills with the pull request title.
- Any other commit was pushed directly and is listed by its id: ``- `1a2b3c4` subject``.
- A pull request is listed once. More than 200 entries are cut, with the number that was left out.

The list is read from git alone. It is what the history says, not what the GitHub API says about the pull requests.

## code

The formula lives in [`version_code.py`](./version_code.py), and nothing else computes a versionCode:

| build | versionCode |
|-------|-------------|
| release `X.Y.Z` | `X × 100000000 + Y × 100000 + 50000 + Z` |
| QA build `N` after a release of `X.Y` | `X × 100000000 + Y × 100000 + N` |

Android installs an APK over an installed one only when its versionCode is not lower. Within one minor version the formula gives this order:

- a release is higher than every earlier release;
- every QA build is lower than every release, so a release installs over a QA build and a QA build never installs over a release;
- a later QA build is higher than an earlier one.

A QA build is numbered from the last release tag. After `X.Y.0` is released, QA builds are in the block of `X.Y` and above the releases of the minor version before it.

Across [release lines](#release-lines), each major version has a block of 100000000 codes. A release is higher than the earlier releases of its own line, and every code of a line, QA builds included, is below every code of the next major. A maintenance release made after the next major exists has a lower code than that major, so it never installs over it.

Limits: `X` up to 20, `Y` up to 999, `Z` and `N` up to 49999, so that no code exceeds 2100000000. A value outside them fails the step.

To change the formula, change `version_code.py` and `test_version_code.py`. The tests state the order the codes have to keep and the codes the calling apps have already shipped.

## Requirements

- `python3` 3.9 or newer and `git` on `PATH`. GitHub-hosted runners provide both.
- For `next`: a checkout with `fetch-depth: 0`.

## Development

```bash
uv run --with pytest python -m pytest release-version -q
```

The tests run the script against throwaway git repositories and a stand-in for the pull request API.
