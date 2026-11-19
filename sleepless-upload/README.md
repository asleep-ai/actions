# sleepless-upload

Composite action that uploads a build, an APK or an ipa, to Sleepless (GADS), the QA team's regression system. Sleepless installs the build on its devices, runs the regression and reports the result; this action ends with the upload.

1. `POST <GADS_URL>/oauth/token` with `client_id`, `client_secret` and `tenant` as JSON. The `access_token` of the answer is masked in the log at once.
2. `POST <GADS_URL>/apps` with the token as `Authorization: Bearer`, and the file and a description as `multipart/form-data` (`file`, `description`).

It is the action the medical apps use (asleep-ai/medical-apps#58, #138), moved here for [`android-sleepless.yml`](../android-ci/README.md#android-sleeplessyml), with these differences: `file` is required (the earlier default, the ipa of an iOS export, is gone), the branch and the commit of the description are inputs, and the time an upload may take is an input.

## Usage

```yaml
      - name: Upload to Sleepless
        uses: asleep-ai/actions/sleepless-upload@android-ci/v1
        with:
          file: ${{ steps.apk.outputs.path }}
          description: HeySleep 2.0.1-qa.57 (200000123)
          ref: qa-develop
          sha: ${{ steps.apk.outputs.commit }}
        env:
          GADS_URL: ${{ vars.GADS_URL }}
          GADS_CLIENT_ID: ${{ vars.GADS_CLIENT_ID }}
          GADS_CLIENT_SECRET: ${{ secrets.GADS_CLIENT_SECRET }}
          GADS_TENANT: ${{ vars.GADS_TENANT }}
```

The dashboard then shows `HeySleep 2.0.1-qa.57 (200000123) · qa-develop 3ff9b1a`.

- **The runner has to reach `GADS_URL`**, which is in the office network: a self-hosted runner there. A GitHub-hosted runner gets no answer, and the step fails with `No answer from GADS_URL`.
- The connection is read from the step's environment, as above: three organization variables and one organization secret. A step without one of them fails before any request, and names every one that is empty.
- The action is released with the Android workflows, under `android-ci/v1`.

## Inputs

| name | default | description |
|------|---------|-------------|
| `file` | | Required. Path of the file to upload. |
| `description` | | Required. What the dashboard shows for the build. ` · <ref> <commit>` is added from `ref` and `sha`. |
| `ref` | `${{ github.ref_name }}` | The branch or tag added to the description. Empty adds none. |
| `sha` | `${{ github.sha }}` | The commit added to the description, as its first seven characters. Empty adds none. |
| `max-time` | `900` | Seconds one upload attempt may take, from the first byte sent to the answer (curl's `--max-time`). |

The defaults of `ref` and `sha` are the run's, as the medical apps' description had them. A workflow that uploads the build of another commit, such as the APK of a release that a scheduled run takes, passes that tag and commit.

## Outputs

| name | description |
|------|-------------|
| `http-code` | The HTTP status Sleepless answered the upload with, `000` when there was no answer. Not written when the step stopped before the upload. |

## What fails the step

| case | log |
|------|-----|
| A variable or the secret is empty | `Sleepless settings missing`, with every empty name. Nothing is sent. |
| The file does not exist, or `file` is empty | `No file to upload`. Nothing is sent. |
| The token request is answered with anything but 2xx | `Sleepless token request failed` with the status and up to 500 bytes of the answer. Nothing is uploaded. |
| The token answer has no `access_token` (an empty one, no JSON) | `Sleepless token request failed::The answer … has no access_token`. |
| No answer from `GADS_URL` | `No answer from GADS_URL` with curl's exit status. |
| The upload is answered with anything but 2xx | `Sleepless upload failed` with the status, the file name and its size; up to 2000 bytes of the answer are printed. A 413 says that Sleepless refuses the size. |
| No answer to the upload within `max-time` | `No answer within max-time (… seconds)`, with the size. |

Every 2xx is an upload. What Sleepless answered is printed in the log either way.

## Time and attempts

- The token request has 30 seconds and is made up to three times when there is no connection or a server error.
- **An upload attempt has `max-time` seconds, 900 by default.** The original had 300 for an iOS ipa; HeySleep's QA APK is about 360 MB, so 300 seconds is enough only from about 10 Mbit/s up (360 MB × 8 / 300 s ≈ 9.6 Mbit/s). 900 seconds holds down to about 3.2 Mbit/s, three times the margin. Inside the office network the upload takes seconds, so the input costs time only when Sleepless does not answer.
- **A failed upload is made again only while `max-time` has not passed since the first attempt began** (`--retry 2 --retry-max-time <max-time>`): after no connection or a 408, 429 or 5xx answer that came quickly, once or twice more. An attempt that used up `max-time` is not made again, so a step takes about twice `max-time` at most. An attempt made again after a 5xx may give Sleepless the same build twice; it does not switch to a build it has already ([Sleepless](../android-ci/README.md#sleepless)).
- A 4xx answer is final and is not made again.

Sleepless's size limit for a file is not known yet (a question to the QA team). A 413 answer would be the sign of one.

## Secrets

- The client secret goes from the environment into a JSON file in `$RUNNER_TEMP`, readable by the runner's user only, which curl reads. The token goes into a header file the same way. Neither is on a command line, where another process of the same user could read it on a shared host.
- Both files and both answers are deleted when the step ends, however it ends.
- The token is masked with `::add-mask::` before anything that could hold it is printed. The client secret is never printed.

## Development

```bash
uv run --with pytest --with pyyaml python -m pytest sleepless-upload -q
```

The tests run the step's script against a local stand-in for Sleepless: a refused token request, an answer without `access_token`, uploads answered with 2xx, 4xx, 413 and 5xx, an upload that succeeds when made again, an answer that comes after `max-time`, no server at all, a file that does not exist, a 64 MiB file that arrives whole, a description and a path with the characters `-F` would read, and the mask of the token. How the real Sleepless answers, and how long a 360 MB upload takes from the office network, is seen only there.
