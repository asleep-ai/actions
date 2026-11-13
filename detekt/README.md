# detekt

[pre-commit](https://pre-commit.com) hook that checks Kotlin files with
[detekt](https://detekt.dev)'s command-line runner, `detekt-cli`. It passes
the files pre-commit selected as detekt's `--input`, together with the
caller's configuration, baseline and plugin jars. The same hook runs locally
and in CI.

| hook id | stage | what it does |
|---------|-------|--------------|
| `detekt` | default | Fails on changed `.kt`/`.kts` files with findings the baseline does not record. Read-only. |

The rules, the baseline, the plugin jars and the detekt version are the
caller's. This repository only holds the script that puts them together.

## Caller contract

- pre-commit 4.4.0 or newer (the hook uses `language: unsupported_script`).
- [mise](https://mise.jdx.dev) on `PATH`, with the repository's mise
  configuration providing java and `detekt-cli`. The hook runs `mise exec`
  from the repository root, so it uses the caller's configuration and nothing
  from this repository, and it runs whichever `detekt-cli` is first on `PATH`
  inside that environment. The hook never downloads anything itself; whether
  `mise exec` installs a declared but missing tool is mise's
  `exec_auto_install` setting (on by default); otherwise run `mise install`.
  If mise reports that the configuration is not trusted, run `mise trust` once
  in the clone (in CI, trust the checkout, for example with
  `MISE_TRUSTED_CONFIG_PATHS`).
- A detekt configuration at `config/detekt.yml`, or elsewhere with `--config`.
- bash and git.

Any mise tool that puts `detekt-cli` on `PATH` works, for example:

```toml
# mise.toml
[tools]
java = "temurin-17.0.20+8"
"aqua:detekt/detekt" = "1.23.8"
```

Exact versions and a committed `mise.lock` (`mise lock`) are recommended, so
local runs and CI report the same findings.

## Usage

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/asleep-ai/actions
    rev: detekt/v1.0.0  # replace with a published release tag or full commit SHA
    hooks:
      - id: detekt
        args: [--jvm-target, '17', --language-version, '2.0']
```

`rev` versions the hook script only; the detekt and java versions come from
`mise.toml`, so the two move independently. pre-commit caches each `rev`
forever and never re-fetches it, so use an immutable ref: a release tag such
as `detekt/v1.0.0` or a commit SHA. In this multi-action repository
`pre-commit autoupdate` picks the most recent tag on the default branch, which
may belong to another component; bump `rev` deliberately and review the diff.

```bash
pre-commit run detekt --files path/to/Changed.kt
pre-commit run detekt --all-files
```

In CI, run pre-commit with the same configuration on the changed files after
installing the mise tools, for example with
[`pre-commit-changed`](../pre-commit-changed/). Its default `full-scan-paths`
include `config/**`, so a change to the detekt configuration or baseline
checks every file.

## Options

Options go in the hook's `args`, each followed by its value as a separate
item. There is no `--option=value` form.

| option | default | passed to detekt-cli as |
|--------|---------|-------------------------|
| `--config PATHS` | `config/detekt.yml` | `--config PATHS`. Several files may be given, separated by commas; each must exist. |
| `--baseline PATH` | `config/detekt-baseline.xml`, when that file exists | `--baseline PATH`. A baseline named here must exist. |
| `--no-build-upon-default-config` | not set | Leaves out `--build-upon-default-config`, which the hook passes otherwise. |
| `--plugins JARS` | none | `--plugins JARS`. Plugin jar paths, separated by commas; each must exist. |
| `--jvm-target VERSION` | detekt's own | `--jvm-target VERSION` |
| `--language-version VERSION` | detekt's own | `--language-version VERSION` |

All paths are relative to the repository root. An option given twice keeps the
last value. Any other option fails the hook with exit status 2; detekt options
that write files (`--create-baseline`, `--auto-correct`, reports) are not part
of a check and are run directly, as below.

With `--build-upon-default-config`, the caller's configuration only needs the
rules it changes; everything else keeps detekt's defaults.

The hook's exit status is detekt-cli's: 0 when there is nothing to report;
detekt 1.23 exits with 2 when there are findings.

## Baseline

The hook checks whole changed files. Without a baseline, a one-line edit to a
legacy file would have to fix every finding that file already had. The
baseline records those findings, so only new ones fail the hook.

The baseline is optional: a repository without legacy findings needs none, and
the hook then passes no `--baseline`. Create or regenerate it with detekt
itself, using the same options as the hook, and review the diff:

```bash
mise exec -- detekt-cli --build-upon-default-config --config config/detekt.yml \
  --baseline config/detekt-baseline.xml --create-baseline --input app/src
```

## Plugins

`--plugins` takes jar files that are already in the working tree, for example
jars committed under `config/detekt-plugins/`. The hook does not download
plugins, so it works offline and the bytes it runs are the ones under review.

```yaml
      - id: detekt
        args:
          - --plugins
          - config/detekt-plugins/rules-a.jar,config/detekt-plugins/rules-b.jar
```

## Limitations

- Paths containing a comma or a semicolon are rejected: detekt separates the
  entries of `--input` with them.
- A file argument that does not exist fails the hook.
- With no file arguments the hook does nothing. It never runs detekt without
  `--input`, which would scan the whole repository.
- The hook passes no classpath, so rules that need type resolution do not run.
  Those belong to the Gradle build.

## Development

```bash
uv run --with pytest python -m pytest detekt -q
DETEKT_INTEGRATION=1 uv run --with pytest python -m pytest detekt -q
```

The default run uses stand-ins for mise and detekt-cli and needs git, bash,
and pre-commit 4.4.0+ on `PATH`; without pre-commit the hook tests skip.
`DETEKT_INTEGRATION=1` repeats the shared scenarios with the real `detekt-cli`
found on `PATH` (it needs java), and fails rather than skips when detekt-cli
or pre-commit is missing or pre-commit is older than 4.4.0. mise stays a
stand-in in both runs.
