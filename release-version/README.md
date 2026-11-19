# release-version

Composite action for repositories whose versions are git tags. One input, `operation`, selects what a step does:

| operation | what it does |
|-----------|--------------|
| `next` (default) | Resolves the commit to release, finds the last release it builds on, computes the next version, checks that it can be released and lists the pull requests it adds. With `prerelease`, plans a preview of that version instead. |
| `code` | Computes the Android versionCode of a release, of a preview or of a QA build, or reserves its [build number](#build-numbers). |
| `qa` | Plans a QA build of a branch head, or a label build (a named test build made after a release): the version it leads up to, its version name and versionCode, and the tag of the rolling pre-release it is published as. |
| `changes` | Lists what a commit adds to the commit a tag names, such as the QA build before it. |

The action reads and computes, with one exception. It creates no release tag, moves no rolling tag and pushes nothing: [`android-version-bump.yml`](../android-ci/README.md#android-version-bumpyml) and [`android-qa.yml`](../android-ci/README.md#android-qayml) are the workflows that do, with this action's outputs. The exception is a build number: with `version-code-scheme: build-number`, `qa` and `code` reserve one through the GitHub API, as a tag object (no tag ref) and the ref `refs/build-numbers/<major>/<n>` that names it ([Build numbers](#build-numbers)).

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
| `operation` | all | `next` | `next`, `code`, `qa` or `changes`. |
| `pr` | `next` | `''` | Number of a merged pull request; its merge commit is released. Exactly one of `pr` and `sha` is given. |
| `sha` | `next`, `qa`, `changes`, `code` | `''` | For `next`, the exception to `pr`: id of the commit to release, 7 or more hex digits. For `qa` and `changes`: id of the commit that is built. For `code` with `version-code-scheme: build-number`: the commit that is built, which the reservation names. |
| `bump` | `next` | `patch` | Which part of the last version to raise: `patch`, `minor` or `major`. The parts below it become 0. Not read when `version` is given. |
| `prerelease` | `next` | `''` | Optional. Empty plans the release. `preview` plans a preview of the same version, which has to be a new minor or major version (`X.Y.0`). See [A preview](#a-preview). |
| `source-branch` | `next`, `qa` | `''` | For `next`, optional: the branch releases are normally cut from. When given, the commit has to be on the branch of its release line. See [Release lines](#release-lines). For `qa`, required. |
| `tag-prefix` | all | `v` | What a release tag starts with, before `X.Y.Z`. Letters, digits and `. _ / -`, not starting with `-`; may be empty. |
| `token` | `next` with `pr`; `next`, `code`, `qa` | `github.token` | Token that can read the pull request (`pull-requests: read`). Also the token that reads the build numbers of the major (`contents: read`; with the formula once, to [refuse a major that has them](#the-formula-next-to-build-numbers)), and with `version-code-scheme: build-number` reads the releases (`contents: read`, for `next`) and reserves a build number (`contents: write`, for `qa` and `code`). |
| `version` | `code`, `next`, `qa` | `''` | For `code`: the version, as `X.Y.Z` or as its tag; with `kind: preview`, as `X.Y.0-preview.N`. For `next`, optional: the exact version to release instead of a bump. See [An exact version](#an-exact-version). For `qa`, optional: the version of this one build, the next minor or the next major version and later than the version the build has without it; not with `label`. See [qa](#qa). |
| `kind` | `code` | `release` | `release`, `preview`, or `qa` for a QA build of the minor version of `version`. |
| `qa-sequence` | `code` with `qa`, `qa` | `''` | Number of the QA build, 1 or higher. A later build has a higher number. Not read for a label build. |
| `qa-sequence-offset` | `qa` | `''` | Optional whole number added to `qa-sequence`; may be negative. |
| `ref` | `qa` | `''` | The full ref the build was started for, `refs/heads/<branch>`. |
| `next-version` | `qa` | `''` | Optional. The version the branch is heading for, used while it is higher than the last release, no higher than the next major version after it, and not released. |
| `label` | `qa` | `''` | Optional. Makes the build a label build: a named test build numbered right after the last release of its line. |
| `since` | `changes` | `''` | Name of the tag that marks the earlier build. It need not exist. |
| `version-code` | `changes` | `''` | Optional. The versionCode of the build that is about to be published as the `since` tag; `code-taken-by` then says whether a rolling tag holds it already, and `since-code` which versionCode the build of the `since` tag has. |
| `release-exists` | `code` with `build-number` | `''` | Optional. `true` when the tag has a GitHub Release already: the build then keeps the number an earlier attempt of the same run reserved, and is refused when there is none ([Reserving](#reserving)). `false` or empty: a build that has not been released. The formula does not read it. |
| `asset-pattern` | `code` with `build-number` and `release-exists: true`; `qa`, `changes` | `''` | Optional. For a repository that builds several apps (applicationIds) into one GitHub Release, one app per run: the file name pattern of the APKs the build makes (`*`, `?`, `[…]`, compared case by case). The tag then counts as released for this build only when an asset of its release matches, and a release without one gets the app's first build, unless a later release or preview of the major holds the app ([Several apps in one release](#several-apps-in-one-release)). The formula does not read it. For `qa` and `changes`, the same pattern for a rolling pre-release with a build per app: `qa` refuses one with other characters than letters, digits, `. _ -` and `* ? [ ] !`, and `changes` reads the build before from that app's line ([changes](#changes)). |
| `version-code-scheme` | `next`, `code`, `qa` | `minor-blocks` | How a build gets its versionCode. `minor-blocks`, the default (empty is the same): [the formula](#code), from the version. `build-number`: the next [build number](#build-numbers) of the major, which `qa` and `code` reserve and `next` only looks up. The version names are the same in both. |
| `version-code-floor` | `next`, `code`, `qa` with `build-number` | `''` | Optional. The highest versionCode of the app that is out where the repository's releases do not show it. Every build of its major is numbered above it. A build of a lower major is refused while that major has no build, and passed over with a notice once it has ([Where a major starts](#where-a-major-starts)). With `minor-blocks` it is not read, and a warning says so. |

Inputs reach the script as environment variables, never through shell interpolation.

## Outputs

| name | set by | description |
|------|--------|-------------|
| `version` | `next`, `code`, `qa` | The version as `X.Y.Z`. For `next`, the next version, or `X.Y.0-preview.N` with `prerelease`. For `qa`, the version the build leads up to. |
| `tag` | `next` | The tag of the next version, `<tag-prefix>X.Y.Z`, or of the preview, `<tag-prefix>X.Y.0-preview.N`. |
| `previous-tag` | `next`, `qa` | The last release the commit contains. Empty when it contains none. |
| `previous-prerelease-tag` | `next` with `prerelease` | The preview of the same version before this one. Empty for the first. |
| `notice` | `next` | A sentence for the person who releases, when the plan is allowed and has a consequence: a release whose commit does not contain the last preview of its version. Empty otherwise. |
| `target-sha` | `next`, `qa`, `changes` | Full id of the commit. |
| `branch` | `next` with `source-branch`, `qa` | The branch of the release line: the source branch or `release/<major>.x`. |
| `prs` | `next`, `changes` | Markdown list of what the commit adds, oldest first. |
| `version-code` | `next`, `code`, `qa` | The Android versionCode of the version or build. With `build-number`: for `qa` and `code`, the code of the number reserved for the build; for `next`, the code the release would get if it were built now. |
| `build-number` | `next`, `code`, `qa` with `build-number` | The build number in `version-code`: reserved for `qa` and `code`, looked up for `next`. Not written with the formula. |
| `version-code-scheme` | `next`, `code`, `qa` | The scheme the versionCode was computed with, `minor-blocks` or `build-number`: the input without the whitespace around it, `minor-blocks` for an empty one. A later step or job compares this output, never the input. |
| `version-name` | `qa` | The versionName of the build: `<version>-qa.<n>`, or `<version>-<label>.<k>` for a label build. |
| `kind` | `qa` | `qa`, or `label` for a label build. |
| `number` | `qa` | The number in the version name: `qa-sequence` plus `qa-sequence-offset`, or the `k` of a label build. |
| `line` | `qa` | The branch as it appears in names, slashes as hyphens. |
| `major` | `qa` | The major version of the maintenance branch the build is of. Empty on the source branch. |
| `release-tag` | `qa` | The tag of the rolling pre-release: `qa-<line>`, `qa-<line>-<label>` for a label build, `qa-<line>-<version>` when `version` was given. |
| `since` | `changes` | What the list starts at: a commit id, a release tag, or empty for the whole history. |
| `since-object` | `changes` | The id of the object the `since` tag names in the checkout: the tag object of an annotated tag. Empty when the tag does not exist. A caller that moves the tag afterwards compares it with the tag as it is then. |
| `stale` | `changes` | `true` when the `since` tag names a later commit that contains this one; with `asset-pattern`, when its line of that app does. |
| `unchanged` | `changes` | `true` when the `since` tag names this very commit; with `asset-pattern`, when its line of that app does. |
| `code-taken-by` | `changes` with `version-code` | The rolling tag that holds a build of that versionCode; `since` itself counts unless it names `sha` (with `asset-pattern`, the build of that app). Empty when there is none. |
| `since-code` | `changes` with `version-code` | The versionCode of the build the `since` tag holds, as its message names it; with `asset-pattern`, of that app's build. Empty when it names none. |
| `kept-builds` | `changes` with `asset-pattern` | The lines of the `since` tag's message that name the other apps' builds, `<pattern> <version name> (<versionCode>) <commit>`, one per line and sorted: what the tag keeps when this app's build is published. Empty when there are none. |

## next

### The last release

- The last release is the highest `<tag-prefix>X.Y.Z` among the tags **the commit contains**, compared as numbers: `v1.10.0` is above `v1.9.3`. It is not the newest tag, not the nearest one in the history, and not the highest tag of the repository: a commit on a maintenance branch does not contain the releases the main line made after the two parted.
- Only tags of exactly that form are releases. A tag with a suffix (`v1.2.3-preview.1`, `v1.11.2-test.84`), a floating tag (`v1`), the rolling tag of a QA build (`qa-develop`), a number with a leading zero and a tag with another prefix are ignored. Previews and QA builds therefore never change which version comes next.
- A repository without any release tag counts from `0.0.0`: `patch` gives `0.0.1`, `minor` `0.1.0`, `major` `1.0.0`.

### The commit to release

- `pr` is resolved through the GitHub API to the commit the merge left on the base branch. A pull request that is not merged is refused: an open one has a merge commit too, the test merge, which is on no branch.
- `sha` has to be a commit id. A branch or tag name is refused, because it would name another commit on the next run.
- The commit has to be in the checkout.

### An exact version

`version` names the next version outright, as `X.Y.Z` or as its tag, and `bump` is then not read; the log says so. It is for what a bump cannot reach: the first release of a repository that does not start at `0.x` (`version: 2.0.0` while no release tag exists), or a version that skips some.

- The version has to be higher than the last release the commit contains, and its tag must not exist.
- Every other rule applies as to a computed version: the release line, the branch, the commit that has to add something, and the versionCode limits. On `release/1.x` only a 1.x version passes.

### A preview

`prerelease: preview` plans a preview of the next version instead of its release. The version is computed and checked exactly as without it, by `bump` or `version` and under every rule below. Then:

- The version has to be a new minor or major version, `X.Y.0`. A patch release has no preview: `bump: patch`, or a `version` whose patch is not 0, is refused, and the error says to choose `minor` or `major`. The codes of the previews are below every release of the minor version ([code](#code)), so a preview of a patch could not install over the release that is out. What is tried on a device before a patch release is a label build ([qa](#qa)).
- The tag is `<tag-prefix>X.Y.0-preview.N`, and `version` is `X.Y.0-preview.N`. `N` is one above the highest preview tag of `X.Y.0` in the repository, and 1 for the first; at most 999.
- The commit has to contain the last preview of that version and must not be its commit: a preview builds on the one before it. `previous-prerelease-tag` names it, and `prs` then lists what the commit adds to it. `previous-tag` stays the last release.
- `version-code` is the code of a preview, between the QA builds and the releases of the minor version.
- `preview` is the only value. Any other is refused.

The release of the version is planned without `prerelease`, as if its previews did not exist, with one exception: when the commit does not contain the last preview of the version, `notice` says so, and the action prints the sentence as a warning. The release installs over that preview, and it would do so with code that lacks what the preview had. The plan is still allowed.

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
- "On the branch" means in its first-parent history: a commit the branch itself pointed at, which is what a squash merge, a rebase merge and the merge commit of a pull request are. The head commit of a pull request that was merged with a merge commit is in the branch's history and still refused: the check on it is the pull request's, which built the merge with the base branch, not that commit.
- A commit of an open pull request or of a branch that was never merged is on neither, and is refused.
- The branch must not have released a higher version after the commit. The rule on the line's last release covers this within a major; this one covers `major`, whose line has no release yet: `v2.0.0` is not cut from a commit that precedes `v1.14.0` on the same branch.

The `branch` output names the line's branch. Without `source-branch`, only the tag rules above apply.

### The checks

The step fails, with an `::error::` annotation that names the rule, when:

- the commit is the commit of the last release: there is nothing new;
- the last release of the next version's major is not the one the commit contains, or the commit contains no release tag while the repository has some;
- `version` is not of the form `X.Y.Z`, is tagged already, or is not higher than the last release the commit contains;
- with `source-branch`: the commit is not in the first-parent history of the branch of its release line, or that branch has released a higher version after the commit;
- the checkout is shallow, so tags or history may be missing;
- the next version has no versionCode under the formula below;
- with `prerelease`: the value is not `preview`, the version is a patch release, the commit is the commit of the last preview of the version, or it does not contain that preview.

### The pull request list

One entry per commit of the first-parent history between the last release and the commit, oldest first:

- A squash merge names its pull request at the end of the subject: `Fix the timer (#12)` becomes `- #12 Fix the timer`. When the subject ends in two numbers, the last one is the pull request.
- A merge commit `Merge pull request #12 from …` becomes `- #12` with the first line of its body, which GitHub fills with the pull request title.
- Any other commit was pushed directly and is listed by its id: ``- `1a2b3c4` subject``.
- A pull request is listed once. More than 200 entries are cut, with the number that was left out.

The list is read from git alone. It is what the history says, not what the GitHub API says about the pull requests.

## code

The formula lives in [`version_code.py`](./version_code.py), and nothing else computes a versionCode. A code is `X × 100000000 + Y × 100000 + R`, and `R`, the place within the minor version `X.Y`, says what kind of build it is:

| build | `R` | range | computed by |
|-------|-----|-------|-------------|
| QA build `n` of the minor version `X.Y` | `n` | `n` 1 to 48999 | `code` with `kind: qa` and `qa-sequence: n`; `qa` |
| preview `N` of `X.Y.0` | `49000 + N` | `N` 1 to 999 | `code` with `kind: preview` and `version: X.Y.0-preview.N`; `next` with `prerelease` |
| release `X.Y.Z` | `50000 + Z × 100` | `Z` 0 to 499 | `code` with `kind: release`; `next` |
| label build `k` after the release `X.Y.Z` | `50000 + Z × 100 + k` | `k` 1 to 99 | `qa` with `label` |

Examples: `1.13.0` is 101350000, `1.13.1` 101350100, `1.14.0` 101450000, `2.0.0` 200050000; `2.0.0-preview.3` is 200049003; QA build 57 of 1.13 is 101300057; the first label build after `1.13.0` is 101350001.

Android installs an APK over an installed one only when its versionCode is not lower. Within one minor version the formula gives this order:

```
QA build  <  preview  <  release X.Y.Z  <  label build after X.Y.Z  <  release X.Y.(Z+1)
```

- a release is higher than every earlier release;
- every QA build is lower than every preview, and every preview lower than every release. A preview installs over a QA build and a release over both, never the other way round;
- a label build installs over the release it was made after, and the next patch release installs over the label build;
- a later QA build is higher than an earlier one, and the same holds for the previews of a version and for the label builds after a release.

A QA build is numbered in the block of the version it leads up to, the next release of its line. While that is a patch release, the QA build is in the block of the last release and below it: it does not install over that release. A preview exists only of `X.Y.0`, a version whose block has no release yet; a patch release has none, because its preview would be below the release that is installed.

Across [release lines](#release-lines), each major version has a block of 100000000 codes. A release is higher than the earlier releases of its own line, and every code of a line, QA builds, previews and label builds included, is below every code of the next major. A maintenance release made after the next major exists has a lower code than that major, so it never installs over it.

Limits: `X` up to 20, `Y` up to 999, and the ranges of the table, so that no code exceeds 2100000000 and no kind reaches the range of the next. A value outside them fails the step.

To change the formula, change `version_code.py` and `test_version_code.py`. The tests state the order the codes have to keep and the codes the calling apps have already shipped.

A calling repository can give its builds [build numbers](#build-numbers) instead: `version-code-scheme: build-number`.

## qa

Plans a QA build for [`android-qa.yml`](../android-ci/README.md#android-qayml), whose README describes the rules from the caller's side.

```yaml
      - id: qa
        uses: asleep-ai/actions/release-version@release-version/v1
        with:
          operation: qa
          ref: ${{ github.ref }}
          sha: ${{ github.sha }}
          source-branch: develop
          qa-sequence: ${{ github.run_number }}
```

- **The branch.** `ref` has to be `refs/heads/<source-branch>` or `refs/heads/release/<major>.x`. Any other ref, a tag or a pull request ref included, is refused with an error that names it. The commit has to be in the first-parent history of that branch.
- **The version** is the next release of the branch's line:
  - on the source branch, the last release the commit contains raised by patch, or the next major once `release/<major>.x` exists for the major of that release;
  - on `release/<major>.x`, the last release of that major raised by patch, or `<major>.0.0` when the commit contains none;
  - `0.0.1` when the commit contains no release at all.
- **`next-version`** replaces that version while it can be the next release of the branch: higher than the last release the commit contains, no higher than the next major version after it, not tagged, and of the branch's line. Otherwise it is not used, and the log says why. It never fails the step, so a value that a release has overtaken can stay in the calling file.
- **`version`** replaces it for one build, and is refused when it cannot be. It is typed into a form, and a build of it is signed with the release key, so it is bounded on both sides:
  - it is the next minor or the next major version after the last release of the line, and nothing else (`1.14.0` or `2.0.0` after `v1.13.0`);
  - it is later than the version the build has without it, `next-version` included. That version itself would be a second build under the name and the versionCode of the rolling one; the error says to run without `version`;
  - before the first release, none passes: the branch builds the version it is heading for (`next-version`, or `0.0.1`), and anything above it is refused as well;
  - it is not tagged, and it is a version of the branch's line.
  The build is then published apart from the rolling build of the branch: `release-tag` ends in the version.
- **The number** of a QA build is `qa-sequence` plus `qa-sequence-offset`, 1 to 48999. With `build-number` it is only part of the name, 1 or higher, and the versionCode is a [build number](#build-numbers); a label build is then counted from the version names of the rolling tags, without the limit of 99.
- **A label build.** With `label`, the build is a named test build made after the last release of its line:
  - the version is the patch after that release, and `version-name` is `<version>-<label>.<k>`;
  - `version-code` is the release's code plus `k`: above the release, below its next patch;
  - `k` is one above the label builds that were published after the same release, whatever their label and branch. They are read from the rolling tags: each `qa-*` tag is an annotated tag whose message names the build it holds, `<version name> (<versionCode>)`, as the publish job of `android-qa.yml` writes it, or with `asset-pattern` the build of each app in a line of its own, every one of which counts. A build that was planned and not published took no number, so two builds that are planned before either is published get the same one: the caller asks [`changes`](#changes) with `version-code` when it is about to publish. After 99 the step fails: the next code is the next patch release's;
  - the step fails when the commit contains no release of the line (there is nothing to sit above), when the next patch is tagged already on a commit this one does not contain, and when `version` is given too;
  - a `label` is lowercase letters, digits and single hyphens, at most 40 characters, and not the name of the line or of another kind of build (`qa`, `preview`, `release`).
- **The outputs**: `version-name` is `<version>-qa.<n>`, or `<version>-<label>.<k>`; `kind` is `qa` or `label`, and `number` the `n` or `k`; `release-tag` is `qa-<line>`, `qa-<line>-<label>` for a label build, or `qa-<line>-<version>` when `version` was given; `major` is the major version of a maintenance branch, and empty on the source branch.
- **An older commit is planned like any other.** Whether its build replaces what is published is the caller's decision, from the `stale` output of [`changes`](#changes).
- **`asset-pattern`** is checked and named in the log, and changes nothing of the plan: the caller writes it into a line of the tag's message and into the name of a job, so whitespace and other characters than letters, digits, `. _ -` and `* ? [ ] !` are refused.

## changes

Lists what the commit `sha` adds to the commit the tag `since` names, in the form of [the pull request list](#the-pull-request-list).

- When the tag exists and its commit is in the history of `sha`, the list starts there, and `since` is that commit's id.
- When the tag names `sha` itself, `unchanged` is `true` and the list is empty.
- When the tag names a later commit that contains `sha`, `stale` is `true` and the list is empty: the caller decides what an older build means.
- Otherwise (no such tag, or a tag on a history the commit does not have) the list starts at the last release the commit contains, and `since` is that tag, or empty when there is none and the list covers the whole history.
- With `version-code`, the versionCode of the build that is about to be published as `since`: `code-taken-by` is the name of a rolling tag (`qa-*`) whose message names a build of that versionCode, and empty when there is none. The `since` tag itself counts unless it names `sha` (the same build, published again). A versionCode is published once; the number of a label build was read from the same tags when it was planned, and may have been taken since, by a build of another label or by a run of the same label on another commit.
- With `asset-pattern`, the `since` tag holds a build per app ([android-ci](../android-ci/README.md#several-apps-in-one-pre-release)): its message has, below the first line, a line per app, `<pattern> <version name> (<versionCode>) <commit>`. The build before is the one of the line whose pattern is `asset-pattern`, written the same way, and `since`, `stale`, `unchanged`, `since-code` and the list are read from its commit, not from the commit the tag names. A tag without such a line, one from before the pattern among them, holds no build of the app: the list starts at the last release. `kept-builds` hands the other lines back, for the caller to write next to this build's. `code-taken-by` counts every line of every rolling tag but this app's own when it names `sha`. The pattern is refused as by `qa`.

## Build numbers

`version-code-scheme: build-number` replaces the versionCodes of the formula with build numbers ([`build_numbers.py`](./build_numbers.py), [`version_code.py`](./version_code.py)):

```
versionCode = X × 100000000 + n        n: the build number of the major version X, 1 to 99999999
```

`n` counts the builds of major `X`, of every kind, in the order they are planned: QA builds, label builds, previews and releases draw from one counter. Every build installs over every build of its major planned before it; a QA build planned after the release `2.0.0` is above it, whatever the formula gave `2.0.0`. The version names, and every rule of `next` and `qa` about versions, are the same as with the formula. What changes:

| | formula (`minor-blocks`) | `build-number` |
|---|---|---|
| `version-code` of `qa` and `code` | from the version and the number | the build number reserved for the build |
| `version-code` of `next` | the release's or preview's | the code the release would get now; nothing reserved |
| limits | the ranges of [code](#code) | `X` 0 to 20; `n` up to 99999999 |
| the number `N` of a QA build | 1 to 48999, part of the code | 1 or higher, part of the name only |
| label builds after a release | at most 99, counted from the codes of the rolling tags | no limit, counted from the version names of the rolling tags (`<next patch>-<label>.<k>`) |
| previews of a version | at most 999 | no limit |

### Reserving

`qa` reserves once the plan has passed every rule, and `code` once its version is read; a plan that is refused takes no number.

1. The step creates a tag object through the GitHub API: the commit that is built (`sha`), the message `The build number of run <GITHUB_RUN_ID> of <repository>`, and the commit's time as the tagger's. The same run makes the same object in every attempt.
2. It lists `refs/build-numbers/<X>/`. When one of the refs names that object, an earlier attempt of the run reserved it: the run keeps that number, and nothing is written. That holds for a plan that reserved and then failed, as for a run whose plan passed and whose build failed.
3. With `release-exists: true` (`code` of a tag whose GitHub Release exists), a run that found no number of its own is refused here, before it reserves: a new run of a released tag would take a number above every build since, later releases included, and a device that took it would refuse those. The release is built again by running its own run again. With `asset-pattern`, that is a release that holds an APK of the app, and a release without one is refused only for a later version ([below](#several-apps-in-one-release)).
4. Otherwise it takes the next number ([below](#where-a-major-starts)), checks [the safety rule](#the-safety-rule), and creates `refs/build-numbers/<X>/<n>` naming the object. GitHub creates a ref only when it does not exist: when another build took `n` in between, the step tries `n + 1`, up to 100 times. Any other refusal (a ruleset, the token's permission) fails the step at once, with what to do.

The token needs `contents: write` for that, `contents: read` to look (`next`). Every other run, the same commit's included, gets a new number. A number whose build never happens is a gap, which costs nothing. The object stands for the run, not for the call: a workflow that runs the step twice in one run for one commit gets one number for both when the calls follow each other, and two when they run at once. Call it once per run.

- A server error, or a request that got no answer, is made again up to three times (after 1, 2 and 4 seconds). Every request may be: a read, a tag object that is the same object again, and a ref that exists by then, which counts as taken.
- The pages of a long answer are followed where the `Link` header points, as long as that is on the API of the token (`GITHUB_API_URL`: scheme, host and path). GitHub names the repository there by its id, `…/repositories/<id>/releases?page=2`.
- The refs grow by one per build, and each plan reads those of its major. When that gets long, delete the lower ones and keep the highest few: the highest is the counter. A run whose ref was deleted takes a new number when it is run again.

### Several apps in one release

A repository may build apps with applicationIds of their own into one GitHub Release, one app per run: [android-ci](../android-ci/README.md#several-apps-in-one-release) has how a calling repository does it. Their versionCodes keep no order with each other, so with `asset-pattern`, the file name pattern of the build's APKs, `code` with `release-exists: true` reads the releases and pre-releases (drafts left out) before it reserves:

- When an asset of the tag's release matches the pattern, the release holds the app, and the build is refused as step 3 above refuses a released tag, unless an earlier attempt of the run reserved a number.
- When none does, the build is the app's first for the tag, and takes the next number. It is refused, unless an earlier attempt of the run reserved a number, when a release or a preview of the same major whose version is higher than the tag's has an asset that matches: the new build would be above that one, and a device that took the new build would refuse it. `X.Y.0-preview.N` is below `X.Y.0` and above every version before it, and previews of one version are in the order of `N`. Other tags, a rolling `qa-*` pre-release among them, are not versions here.
- `release-exists: false` reads nothing more: the release is new to every app.

The pattern is compared with `fnmatch.fnmatchcase`, and the step strips it as every input. The caller checks that every APK it publishes matches it: a pattern that is not the app the run built would have made the reading above wrong.

### Where a major starts

- A major with refs goes on one above the highest.
- A major without refs starts one above the highest versionCode of that major that is out:
  - in the file names of the `.apk` and `.aab` assets of the repository's GitHub Releases and pre-releases, drafts left out. A versionCode is a part of the name of digits only between `_`, `-` and the ends of the name: `App_qa_2.0.0-qa.6_200000006.apk` holds `200000006`, `app-release.apk` none. Any such part counts, so the `64` of `x86_64` is read as a code, which matters only in major 0;
  - in the messages of the rolling tags `qa-*`, `<version name> (<versionCode>)`, or each app's line of a tag with a build per app, whatever the files are called.
- Without any, the first build of major `X` is `X × 100000000 + 1`: `1` for major 0.
- `version-code-floor` is a code that is out where neither shows it. Within its major, the next number is above it, refs or not. Below the major it does nothing. Above every code of a major that has no build yet (`101300002` for the first build of major 0) it refuses the build. Above every code of a major that has builds already, a reserved number or a code out, it is the floor of a later line (one calling file builds the maintenance line too): it is not used, and a notice says so.

### The safety rule

Before it reserves, the step refuses the build, with the reason, when:

- the build's major is outside 0 to 20;
- `version-code-floor` is above every code of the major, and the major has no build yet;
- the code the build would get is not above the highest code of its major that is out, as listed above. That happens when the refs are behind what was published: a build made with the formula after the switch, or one made by hand. The error names the code and where it was found, and says to pass it as `version-code-floor`.

A number kept from an earlier attempt of the same run is not checked again: what was published since is newer than that build, and the caller decides what an older build means (`stale` of [changes](#changes)).

### The formula next to build numbers

The scheme is an input, and every calling file passes its own: a branch that did not get the switch, or a tag on a commit from before it, would compute the formula for a major that has build numbers, and the two keep no order (`2.0.1` would be `200050100`, above every build number of 2.x so far). So with `minor-blocks`, `next`, `code` and `qa` read the refs of their major once, after every other rule, and refuse the plan when there are any. A repository that never switched has none, and plans as it always did. When the API cannot be asked (no answer, a refusal), the plan goes on with a warning: the question is new, and a repository that never switched should not depend on it.

A `version-code-floor` given with `minor-blocks` is not read, and a warning says so.

### A new repository

A repository that uses `build-number` from its first build has no versionCode out, and starts at `X × 100000000 + 1`. `X` is the major of the version name: `0` before any release (`0.0.1-qa.N`, versionCodes `1`, `2`, …), `1` with `next-version: 1.0.0` (`100000001`). A repository that takes over an app released elsewhere passes that app's highest versionCode as `version-code-floor`, or starts at a higher major. How a calling repository switches is in [android-ci](../android-ci/README.md#build-numbers).

## Requirements

- `python3` 3.9 or newer and `git` on `PATH`. GitHub-hosted runners provide both.
- For `next`, `qa` and `changes`: a checkout with `fetch-depth: 0`.
- With `build-number`: for `code`, a checkout that has `sha`; a token with `contents: write` for `qa` and `code`, `contents: read` for `next`.
- With `minor-blocks`: a token with `contents: read` for `next`, `code` and `qa`, which read the refs of the major once. Without it the plan goes on with a warning.

## Development

```bash
uv run --with pytest python -m pytest release-version -q
```

The tests run the script against throwaway git repositories and a stand-in for the pull request API. [`test_build_numbers.py`](./test_build_numbers.py) runs the build numbers against a stand-in for the GitHub API that keeps refs, tag objects and releases in memory and creates a ref only once: builds that reserve at once, a new repository, the floor, major 0 and major 21, and a QA build after a release. Whether GitHub itself creates refs under `refs/build-numbers/` for the job token and answers for the same tag object in a run that is run again is seen only in a calling repository.
