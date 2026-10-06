# ktfmt

[pre-commit](https://pre-commit.com) hooks that check Kotlin formatting with
[ktfmt](https://github.com/facebook/ktfmt) (`--kotlinlang-style --enable-editorconfig`),
with an optional content-hash baseline so a repository can adopt ktfmt
without reformatting its legacy files. The same hook runs locally and in CI.

| hook id | stage | what it does |
|---------|-------|--------------|
| `ktfmt` | default | Fails on changed `.kt`/`.kts` files ktfmt would reformat. Read-only. |
| `ktfmt-write` | manual | Formats the selected files in place. |
| `ktfmt-baseline-refresh` | manual | Shrinks the baseline; never adds an entry. |
| `ktfmt-baseline-bootstrap` | manual | Creates the baseline once; refuses to overwrite one. |

Manual hooks run only when asked for (`--hook-stage manual`), so no commit
or CI run ever rewrites a source file or the baseline.

## Caller contract

- pre-commit 4.4.0 or newer (the hooks use `language: unsupported_script`).
- [mise](https://mise.jdx.dev) on `PATH`, with the repository's mise
  configuration declaring java and ktfmt. The hook runs
  `mise exec` from the repository root, so it uses the caller's
  configuration and nothing from this repository. If ktfmt is not declared
  the hook fails instead of falling back to whatever release happens to be
  installed. The hook never downloads anything itself; whether `mise exec`
  installs a declared but missing tool is mise's `exec_auto_install`
  setting (on by default); otherwise run `mise install`. If mise reports
  that the configuration is not trusted, run `mise trust` once in the clone
  (in CI, trust the checkout, for example with `MISE_TRUSTED_CONFIG_PATHS`).
- bash, git, and `iconv` (which validates file names), plus `locale` when a
  file name is non-ASCII. GitHub-hosted Ubuntu and macOS runners have them.

```toml
# mise.toml
[tools]
java = "temurin-17.0.20+8"
"aqua:Kotlin/ktfmt" = "0.64"
```

Exact versions and a committed `mise.lock` (`mise lock`) are recommended,
not required. Floating versions
(`"latest"`, a prefix such as `"0"`) are allowed, but then each machine
formats with whatever matching release mise has installed or installs, so
local runs and CI can disagree and results can change without a commit. The
ktfmt release needs `--enable-editorconfig` (0.64 has it). `.editorconfig`
is optional; when present, ktfmt applies its supported overrides such as
`indent_size` and `max_line_length`.

For a local experiment with another ktfmt release, set
`KTFMT_JAR=/path/to/ktfmt-<version>-with-dependencies.jar`; java still comes
from the mise environment. This override skips the declared-version check,
so keep it out of shared configuration and CI.

## Usage

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/asleep-ai/actions
    rev: ktfmt/v1.0.0  # replace with a published release tag or full commit SHA
    hooks:
      - id: ktfmt
      # Optional manual hooks:
      - id: ktfmt-write
      - id: ktfmt-baseline-refresh
      - id: ktfmt-baseline-bootstrap
```

`rev` versions the hook scripts only; the ktfmt and java versions come from
`mise.toml`, so the two move independently. pre-commit caches each `rev`
forever and never re-fetches it, so use an immutable ref: a release tag such
as `ktfmt/v1.0.0` or a commit SHA (`pre-commit autoupdate --freeze`). A
floating major tag such as `ktfmt/v1` does not update existing installs when
it moves, and pre-commit warns that such mutable refs are unsupported. In this
multi-action repository `pre-commit autoupdate` picks the most recent tag on
the default branch, which may belong to another action; bump `rev`
deliberately and review the diff.

```bash
pre-commit run ktfmt --files path/to/Changed.kt
pre-commit run ktfmt-write --hook-stage manual --files path/to/Changed.kt
pre-commit run ktfmt-baseline-refresh --hook-stage manual --all-files
pre-commit run ktfmt-baseline-bootstrap --hook-stage manual --all-files
```

A file argument that does not exist fails the hook, with or without a
baseline; ktfmt alone would skip it silently.

A manual hook that rewrites a tracked file reports
`files were modified by this hook` and exits non-zero; review and commit the
change. Pass `--all-files` (or `--files`) to the baseline hooks so pre-commit
does not stash unstaged changes around them.

In CI, run pre-commit with the same configuration on the changed files after
installing the mise tools (for example with `jdx/mise-action`). Running the
`ktfmt` hook over all files also works; baselined legacy files stay exempt.

## Baseline

ktfmt has no baseline of its own. The hook reads `config/ktfmt-baseline.txt`
when it exists; choose another path with
`args: [--baseline, path/to/file.txt]` on every ktfmt hook. A repository
whose Kotlin is already formatted needs no baseline and no `config/`
directory.

Each entry is `<git blob hash> <path>`. A file is exempt only while its
content still hashes to the recorded value, so any edit makes the hook check
the whole file again; format it with `ktfmt-write` before committing.

- **Bootstrap** scans every tracked `.kt`/`.kts` file and records the ones
  ktfmt would change. It is a one-time migration step and refuses to
  overwrite an existing baseline.
- **Refresh** is a ratchet: it keeps an entry only while the file is still
  tracked, unchanged, and still reported by ktfmt. It drops edited,
  formatted, and removed files and never adds one. Run it after formatting
  legacy files or upgrading ktfmt.
- If ktfmt fails during either scan (a parse error or a crash), the baseline
  is left exactly as it was; nothing is half-written.
- Never add or edit entries by hand, and review every generated diff.

## Limitations

- Bootstrap scans all tracked Kotlin files, not just those the caller's
  pre-commit `exclude` lets through; a tracked file ktfmt cannot parse aborts
  bootstrap until it is fixed or untracked.
- Paths containing a newline or carriage return are rejected, because ktfmt
  reads its argument file line by line.
- File names must be valid UTF-8, and non-ASCII names need a UTF-8 locale
  (for example `LANG=C.UTF-8`, which GitHub-hosted runners set). The JVM
  converts file names with the locale's charset and ktfmt silently skips a
  file it cannot open, so the hook fails instead. Names are checked before
  `mise exec`, which replaces bytes that are not valid UTF-8.
- ktfmt is a formatter only: naming, imports, and line length beyond what it
  can wrap belong to a linter such as detekt.

## Development

```bash
uv run --with pytest python -m pytest ktfmt -q
KTFMT_INTEGRATION=1 uv run --with pytest python -m pytest ktfmt -q
```

The default run uses stand-ins for mise and java and needs git, bash, and
pre-commit 4.4.0+ on `PATH`; without pre-commit the hook tests skip.
`KTFMT_INTEGRATION=1` repeats the shared scenarios with the real mise, java,
and ktfmt (versions overridable with `KTFMT_INTEGRATION_KTFMT` and
`KTFMT_INTEGRATION_JAVA`), and fails rather than skips when pre-commit is
missing, broken, or older than 4.4.0.
