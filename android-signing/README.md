# android-signing

Composite action for the keystore and signature steps of a signed Android build. One input, `operation`, selects what a step does:

| operation | what it does |
|-----------|--------------|
| `decode` | Writes the base64-encoded keystore to `release.jks` in the runner's temp directory and reports its path. |
| `verify` | Runs `apksigner verify` on a signed APK and, optionally, compares the signer certificate with an expected SHA-256 digest. |
| `cleanup` | Deletes the keystore and any other files the caller names, such as `local.properties`. |

The action does not build or sign anything. The build signs the APK with the decoded keystore; how the keystore path and passwords reach the build is the caller's choice.

## Usage

```yaml
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7

      - name: Decode the keystore
        id: keystore
        uses: asleep-ai/actions/android-signing@android-signing/v1
        with:
          operation: decode
          keystore-base64: ${{ secrets.KEYSTORE_BASE64 }}

      - name: Write local.properties
        env:
          KEYSTORE_FILE: ${{ steps.keystore.outputs.keystore-file }}
          KEYSTORE_PASSWORD: ${{ secrets.KEYSTORE_PASSWORD }}
          KEY_ALIAS: ${{ secrets.KEY_ALIAS }}
          KEY_PASSWORD: ${{ secrets.KEY_PASSWORD }}
        run: |
          {
            echo "KEYSTORE_FILE=$KEYSTORE_FILE"
            echo "KEYSTORE_PASSWORD=$KEYSTORE_PASSWORD"
            echo "KEY_ALIAS=$KEY_ALIAS"
            echo "KEY_PASSWORD=$KEY_PASSWORD"
          } > local.properties

      - name: Build the release APK
        run: ./gradlew assembleRelease

      - name: Verify the APK signature
        uses: asleep-ai/actions/android-signing@android-signing/v1
        with:
          operation: verify
          apk-file: app/build/outputs/apk/release/app-release.apk
          expected-cert-sha256: ${{ vars.RELEASE_CERT_SHA256 }}  # optional

      - name: Delete the keystore and secret files
        if: ${{ always() }}
        uses: asleep-ai/actions/android-signing@android-signing/v1
        with:
          operation: cleanup
          keystore-file: ${{ steps.keystore.outputs.keystore-file }}
          paths: |
            local.properties
```

**Run `cleanup` with `if: ${{ always() }}`.** Without it the step is skipped when the build or the verification fails, which is exactly when the keystore and `local.properties` would be left behind.

The `android-signing/v1` tag does not exist until the first release; pin a full version or commit SHA once it does.

## Inputs

| name | used by | default | description |
|------|---------|---------|-------------|
| `operation` | all | required | `decode`, `verify` or `cleanup`. |
| `keystore-base64` | `decode` | `''` | The keystore, base64-encoded (`base64 -i release.jks`). Line breaks are ignored. |
| `apk-file` | `verify` | `''` | Path of the signed APK. Several paths may be given, one per line, for a build with one APK per flavor. |
| `expected-cert-sha256` | `verify` | `''` | Optional SHA-256 digest of the expected signer certificate. See [Comparing the certificate](#comparing-the-certificate). |
| `build-tools-version` | `verify` | `''` | Optional build-tools version whose `apksigner` is used, as the directory is named under `build-tools` (`36.0.0`). When empty, the highest installed version is used. See [Which apksigner runs](#which-apksigner-runs). |
| `keystore-file` | `cleanup` | `''` | The keystore to delete, normally the `keystore-file` output of the `decode` step. When empty, the path `decode` writes to is used. |
| `paths` | `cleanup` | `''` | Newline-separated files to delete besides the keystore. |

Relative paths are resolved against the step's working directory, normally the workspace. Inputs reach the script as environment variables, never through shell interpolation.

## Outputs

| name | set by | description |
|------|--------|-------------|
| `keystore-file` | `decode` | Absolute path of the decoded keystore. |

## decode

- The keystore is written to `$RUNNER_TEMP/release.jks`, not to the workspace. The runner empties its temp directory at the start and at the end of every job, so a runner that is killed mid-job keeps the keystore only until its next job.
- The file is readable and writable by the runner's user only (mode `0600`). Whatever is at that path, a symlink included, is removed first and never written through.
- An empty `keystore-base64` fails the step with an `::error::` annotation. Secrets are not passed to workflows triggered from forks or by Dependabot, so in those runs the value is empty; a pull request check that has to produce a signed APK should generate a throwaway key instead of calling `decode`.
- A value that is not valid base64, or that decodes to nothing, fails the step as well. The error never quotes the value.
- The file name is fixed, so a second `decode` in the same job replaces the first keystore. Copy the first one elsewhere in the temp directory if a job needs two, and list the copy in `paths` of `cleanup`.

## verify

`verify` needs an Android SDK with a build-tools package and a Java runtime:

- The SDK is read from `ANDROID_HOME`, or from `ANDROID_SDK_ROOT` when `ANDROID_HOME` is not set.
- `apksigner` is taken from `<sdk>/build-tools`: the highest installed version, or the one named by `build-tools-version`.
- `apksigner` runs on the JVM and finds it through `JAVA_HOME` or `PATH`.

A missing SDK variable, a missing `apksigner`, a `build-tools-version` that is not installed and a missing APK each fail the step with an `::error::` annotation that says which one it is.

The step runs `apksigner verify --verbose <apk-file>` and fails when apksigner reports the APK as not verified.

With several paths in `apk-file`, the step first checks that every file exists, then verifies them in the order given, each against the same `expected-cert-sha256`, and fails at the first APK that does not pass.

### Which apksigner runs

By default the highest installed build-tools version is used; a preview such as `36.0.0-rc1` sorts below its release. On a GitHub-hosted runner that version changes when the runner image is updated, without any change in the calling repository. The validity check does not depend on the version. The certificate comparison reads apksigner's output, and that output has changed before: build-tools 37.0.0 renamed every signer (see below). A version the action cannot read fails the step, it never passes unchecked, but the failure arrives at release time.

Set `build-tools-version` to keep the comparison on a version that is known to work, and install that version if the runner does not have it. The step fails when the named version is not installed, and lists the ones that are.

### Comparing the certificate

Without `expected-cert-sha256`, only the validity of the signature is checked. That is the right setting when the project has one key and no independent record of its certificate: comparing the APK with a digest taken from the same keystore proves nothing the build has not already proved.

With `expected-cert-sha256`, the step runs `apksigner verify --verbose --print-certs` and also compares the signer certificate. This is worth setting when the digest is recorded apart from the keystore, for example in a repository variable, so that a keystore secret replaced by mistake fails the release instead of shipping an APK that cannot update the installed one.

- An unset variable or secret is an empty value, so nothing is compared and the step still succeeds. It then leaves a `::notice::` annotation on the run saying that the signer certificate was not compared; a run that should compare must not show that notice.
- The digest is hex, with or without colons, in upper or lower case. `keytool -list -v -keystore release.jks` prints it as `SHA256:`, and `apksigner verify --print-certs app.apk` prints it as `certificate SHA-256 digest`.
- Several digests may be given, separated by whitespace or commas, for example while a signing key is being rotated.
- Every signer certificate of the APK has to be one of the expected digests. One match among several signers is not enough. An APK signed with a rotated key has two signers, the new key and the old one.
- The digests are read from the lines that name a signer and continue with ` certificate SHA-256 digest: <64 hex digits>`. The name is not matched, because it depends on the build-tools version and on how the APK is signed:

  | build-tools | default signing | rotated key |
  |-------------|-----------------|-------------|
  | 34.0.0 to 36.1.0 | `Signer #1 certificate SHA-256 digest: …` | `Signer (minSdkVersion=33, maxSdkVersion=2147483647) certificate SHA-256 digest: …` |
  | 37.0.0 | `V3.0 Signer: certificate SHA-256 digest: …` (`V2 Signer:`, `V1 Signer:` for older schemes) | `V3.1 Signer: (minSdkVersion=33, maxSdkVersion=2147483647) certificate SHA-256 digest: …` |

- Public key digests are another value and are ignored. So is the `Source Stamp Signer`, which is the stamp of a distributing store and not a signer of the APK.
- When no signer certificate can be read from the output, for instance because a new build-tools version prints it differently again, the step fails instead of passing unchecked, and the output is in the log.

## cleanup

- Deletes `keystore-file` and each file in `paths`. A file that does not exist is ignored, so the step is safe to run after any earlier failure.
- With an empty `keystore-file`, the path `decode` writes to is deleted. A `decode` step that failed or was skipped has an empty output, and the keystore still has to go.
- Only files are deleted. A directory in `paths` fails the step, after the remaining files have been deleted; a symlink is removed without touching its target.
- Patterns are not expanded. A path that does not exist and contains `*`, `?` or `[` fails the step, after the remaining files have been deleted: ignoring it would report success while the files it was meant to match are still there. A file whose name really contains such a character is deleted like any other.
- A misspelt path cannot be told from a file that is simply not there, so it is ignored like one. The step prints `Deleted:` or `Not present:` for every path.
- A file that cannot be deleted fails the step with an `::error::` annotation naming it.

On a self-hosted runner the workspace is kept between jobs, so every file in it that received a secret belongs in `paths`. Build outputs that embed secrets (generated sources, the signed APK) are directories or many files; removing those is left to the calling workflow.

## Requirements

- `python3` 3.9 or newer on `PATH`. GitHub-hosted runners provide it.
- For `verify`: an Android SDK with build-tools and a Java runtime, as described above. GitHub-hosted Ubuntu runners provide both.
- Linux or macOS runners.

## Development

```bash
uv run --with pytest python -m pytest android-signing -q
```

The unit tests replace apksigner with a stub and read certificates from [`testdata/`](./testdata/), which holds the unedited output of `apksigner verify --verbose --print-certs` from build-tools 36.1.0 (the same as 34.0.0 to 36.0.0) and 37.0.0, for five kinds of APK signed with throwaway keys: default signing, v1 and v2, v1 only, a rotated key, and a source stamp. When a build-tools version prints something new, add its output there.

One test builds a small APK with `aapt2`, signs it in those five ways through the decoded keystore, and runs `verify` with the real `apksigner` of every installed build-tools version, then `cleanup`. It runs when `ANDROID_HOME` (or `ANDROID_SDK_ROOT`), `java` and `keytool` are available and is skipped otherwise; `ANDROID_SIGNING_INTEGRATION=1` turns the skip into a failure, which is how CI runs it. CI also runs it every week, because the runner image gains build-tools versions without any change in this repository.
