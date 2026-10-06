# ruff

Template for running [Ruff](https://docs.astral.sh/ruff/) lint and format checks in CI with the same Ruff version developers use locally. This directory has no `action.yml`. Callers copy the example and run native `uv`/`ruff` commands or the official [`astral-sh/ruff-action`](https://github.com/astral-sh/ruff-action); nothing references a tag from this repository.

## Contract

- **One version source.** Local runs and CI read the Ruff version from the same file (`uv.lock` by default).
- **Caller configuration as-is.** CI passes no paths, `--config`, or rule flags; `pyproject.toml`, `ruff.toml`, or `.ruff.toml` decides rules and file selection.
- **Both checks always run.** `ruff check` and `ruff format --check` each run even if the other fails, and the job fails if either fails.
- **Full scan.** Every run checks the whole repository, so a change to only the configuration or the Ruff version is still covered.
- **Caller-owned policy.** Each repository chooses its Ruff version policy, rules, and `requires-python`. There is no organization-wide Ruff version.

## uv projects (preferred)

For repositories that already have `uv.lock`, add Ruff to a dependency group:

```sh
uv add --group lint 'ruff==0.16.10'
```

This adds a `lint` group to `[dependency-groups]` in `pyproject.toml` and locks it in `uv.lock`. Unlike `[project.optional-dependencies]`, dependency groups are not published in package metadata.

Run locally with the same commands CI uses:

```sh
uv run --locked --only-group lint ruff check --no-fix
uv run --locked --only-group lint ruff format --check
```

- `--only-group lint` installs only that group. The project and its dependencies are omitted, so linting does not install runtime dependencies.
- `--locked` exits with an error when `uv.lock` is missing or stale, so CI cannot resolve a different Ruff than the lock.
- `uv run` makes minimal changes by default, so an existing development environment keeps its other packages.

For CI, copy [`ruff.yml.example`](./ruff.yml.example) to `.github/workflows/ruff.yml`.

## Official ruff-action

For repositories without `uv.lock`, or that prefer the official action, use the action to install Ruff and keep the aggregated checks:

```yaml
- uses: astral-sh/ruff-action@v4.1.0
  with:
    version-file: uv.lock # or pyproject.toml / requirements.txt; or version: "0.16.10"
    args: "--version" # install only; the next step runs the checks
- name: Ruff check and format check
  run: |
    lint=0
    ruff check --no-fix || lint=$?
    format=0
    ruff format --check || format=$?
    echo "ruff check exit status: $lint"
    echo "ruff format --check exit status: $format"
    if [ "$lint" -ne 0 ] || [ "$format" -ne 0 ]; then
      exit 1
    fi
```

Version resolution in ruff-action v4.1.0, per its docs and source:

- Precedence is `version`, then `version-file`, then the nearest `pyproject.toml` above `src`, then `latest`. Setting both `version` and `version-file` is an error.
- In `pyproject.toml`, the action reads Ruff only from `project.dependencies`, `project.optional-dependencies`, `dependency-groups`, and Poetry dependency tables. It does not read `[tool.ruff] required-version`.
- `version-file: uv.lock` uses the exact locked version. A range in `pyproject.toml`, such as `ruff>=0.16`, resolves to the newest matching release on each run and can drift from `uv.lock`.
- If the version file is missing or unparseable, the action only warns, then falls back to `pyproject.toml` discovery and finally `latest`. For a hard pin, use `version:` or check the `ruff-version` output.

Locally, install Ruff from the same file, for example `uv pip install -r requirements-dev.txt`.

## Version policy

| Policy | uv dependency group | ruff-action |
| --- | --- | --- |
| Exact (recommended) | `ruff==0.16.10` | `version: "0.16.10"` |
| Range | `ruff>=0.16,<0.17`, or bare `ruff` | `version: ">=0.16,<0.17"` |
| Latest on every run | `uvx ruff@latest check` (no lock) | `version: latest` |

With uv, `uv.lock` pins the version even for a range or a bare `ruff`. The specifier only bounds how far `uv lock --upgrade-package ruff` can move it, so local and CI stay identical. With ruff-action, a range is resolved again on each run. "Latest on every run" moves local and CI to the newest PyPI release, so results can change without a commit. Choose it deliberately.

An upgrade can change results with no code change. Ruff 0.16.0 grew the default rule set from 59 to 413 rules and began formatting Python code blocks in Markdown files. Bump Ruff in its own pull request so the full scan shows new findings, and keep an explicit `select` if the rule set should not follow Ruff's defaults.

Optionally, `[tool.ruff] required-version = ">=0.16"` makes any other Ruff, such as an editor's bundled binary, exit with an error. It is a runtime guard only and does not choose what uv or ruff-action installs.

## Configuration baseline (optional)

Merge into the existing `pyproject.toml`. Do not replace the file or remove other `[tool.ruff]` settings:

```toml
[tool.ruff]
line-length = 120

[tool.ruff.lint]
select = ["E", "F", "B", "SIM", "I"]
```

In `ruff.toml`, drop the `tool.ruff` prefix: put `line-length` at the top level and the rest under `[lint]`. `requires-python` stays owned by the repository, and Ruff infers `target-version` from it, so the baseline sets neither.

## Narrowing scope

The template has no changed-files mode and no `paths:` filter. If you add either:

- Run a full scan whenever `pyproject.toml`, `ruff.toml`, or `.ruff.toml` changes in any directory. The same applies to files they `extend`, to `uv.lock` or your version file, and to the workflow itself.
- Pass `--force-exclude` so configured excludes still apply to files named on the command line.

`--no-fix` keeps CI read-only even when the configuration sets `fix = true`, without changing which rules run.

## pre-commit and action pins

- **pre-commit `rev` must be immutable.** pre-commit caches each hook environment by `rev` and does not refresh a moved tag or a branch. `ruff-pre-commit` tags match Ruff versions (`rev: v0.16.10`), so the hook is a second version source. Bump it with `pre-commit autoupdate` in the same change as `uv.lock`. To keep `uv.lock` as the only source, use a `language: system` local hook whose entry is `uv run --locked --only-group lint ruff ...` instead.
- **Action refs are resolved on every run.** `actions/checkout@v7` is a mutable major tag, so a moved tag takes effect on the next run. Current `astral-sh/setup-uv` and `astral-sh/ruff-action` releases have no floating major tag (`@v10` and `@v4` do not exist) and are immutable, so pin the full version as above. Pin commit SHAs for strict reproducibility and let Dependabot propose bumps. The action version is a separate choice from the Ruff version, which `uv.lock` or the `version` input controls.
