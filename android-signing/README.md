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
| `apk-file` | `verify` | `''` | Path of the signed APK. |
| `expected-cert-sha256` | `verify` | `''` | Optional SHA-256 digest of the expected signer certificate. See [Comparing the certificate](#comparing-the-certificate). |
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

## verify

`verify` needs an Android SDK with a build-tools package and a Java runtime:

- The SDK is read from `ANDROID_HOME`, or from `ANDROID_SDK_ROOT` when `ANDROID_HOME` is not set.
- `apksigner` is taken from the highest version under `<sdk>/build-tools`. A preview such as `36.0.0-rc1` sorts below its release.
- `apksigner` runs on the JVM and finds it through `JAVA_HOME` or `PATH`.

A missing SDK variable, a missing `apksigner` and a missing APK each fail the step with an `::error::` annotation that says which one it is.

The step runs `apksigner verify --verbose <apk-file>` and fails when apksigner reports the APK as not verified.

### Comparing the certificate

Without `expected-cert-sha256`, only the validity of the signature is checked. That is the right setting when the project has one key and no independent record of its certificate: comparing the APK with a digest taken from the same keystore proves nothing the build has not already proved.

With `expected-cert-sha256`, the step also runs `apksigner verify --print-certs` and compares the signer certificate. This is worth setting when the digest is recorded apart from the keystore, for example in a repository variable, so that a keystore secret replaced by mistake fails the release instead of shipping an APK that cannot update the installed one.

- An unset variable or secret is an empty value, so nothing is compared. The log then says that the signer certificate was not compared.
- The digest is hex, with or without colons, in upper or lower case. `keytool -list -v -keystore release.jks` prints it as `SHA256:`, and `apksigner verify --print-certs app.apk` prints it as `certificate SHA-256 digest`.
- Several digests may be given, separated by whitespace or commas, for example while a signing key is being rotated.
- Every signer certificate of the APK has to be one of the expected digests. One match among several signers is not enough.
- The digests are read from the lines `<signer> certificate SHA-256 digest: <hex>`, whatever apksigner calls the signer (`Signer #1`, `Signer (minSdkVersion=24, maxSdkVersion=32)`). Public key digests and the source stamp signer are not signers of the APK and are ignored.
- When no signer certificate can be read from the output, for instance because a new build-tools version prints it differently, the step fails instead of passing unchecked, and the output is in the log.

## cleanup

- Deletes `keystore-file` and each file in `paths`. A file that does not exist is ignored, so the step is safe to run after any earlier failure.
- With an empty `keystore-file`, the path `decode` writes to is deleted. A `decode` step that failed or was skipped has an empty output, and the keystore still has to go.
- Only files are deleted. A directory in `paths` fails the step, after the remaining files have been deleted; a symlink is removed without touching its target. Patterns are not expanded.
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

The unit tests replace apksigner with a stub. One test builds a small APK with `aapt2`, signs it with a throwaway key through the decoded keystore, runs `verify` and `cleanup` with the real `apksigner`, and reads the certificate list printed by every installed build-tools version. It runs when `ANDROID_HOME` (or `ANDROID_SDK_ROOT`), `java` and `keytool` are available and is skipped otherwise; `ANDROID_SIGNING_INTEGRATION=1` turns the skip into a failure, which is how CI runs it.
