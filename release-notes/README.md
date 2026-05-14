# release-notes

Composite action that drafts sectioned markdown release notes from a git tag range via OpenAI, with a deterministic commit-list fallback for offline / no-key runs.

## Usage

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0  # required: action reads tags + full commit history

- uses: asleep-ai/actions/release-notes@release-notes/v1
  id: notes
  with:
    openai-api-key: ${{ secrets.OPENAI_API_KEY }}

- name: Create GitHub Release
  env:
    GH_TOKEN: ${{ github.token }}
  run: |
    gh release create "${{ github.ref_name }}" \
      --title "${{ github.ref_name }}" \
      --notes-file "${{ steps.notes.outputs.path }}"
```

The action **only writes the markdown file**. Release creation, asset attachment, draft/prerelease policy, and idempotency are caller concerns.

## Inputs

| name | required | default | description |
|------|----------|---------|-------------|
| `current-tag` | no | `github.ref_name` | Current tag, e.g. `v0.5.0`. Defaults to the tag that triggered the run. |
| `previous-tag` | no | _auto_ | Previous tag bounding the commit range. Auto-derived via `git describe --tags --match=<pattern> --abbrev=0 $CUR^` when empty. |
| `tag-pattern` | no | _auto_ | Pattern passed to `git describe --match` when auto-deriving the previous tag. Defaults to `<prefix>/v*` for path-prefixed tags, otherwise `v*`. See [Multi-track repos](#multi-track-repos). |
| `openai-api-key` | no | _empty_ | OpenAI API key. **If unset, fallback emits a plain commit-list summary.** |
| `openai-model` | no | `gpt-5.4` | OpenAI model name. |
| `system-prompt` | no | _built-in_ | Override the default prompt. Useful when the caller wants Korean output, different sections, or a domain-specific tone. |
| `output-file` | no | `release-notes.md` | Path where the markdown is written. |

## Outputs

| name | description |
|------|-------------|
| `path` | Path to the generated markdown file. |
| `previous-tag` | The resolved previous tag (useful when auto-derived). |

## Multi-track repos

Repos that ship more than one independent line of tags (e.g. this monorepo's
`release-notes/v*` alongside a hypothetical `i18n-audit/v*`) need the previous-tag
search filtered to the same track. Resolution precedence:

1. **`previous-tag` set** — used verbatim, no `git describe` call.
2. **`tag-pattern` set** — passed as-is to `git describe --match`.
3. **Auto-derived** — if `current-tag` matches `<prefix>/vN...`, pattern is
   `<prefix>/v*`. Otherwise `v*`.

Most callers need nothing: tags following the `<action>/vMAJOR.MINOR.PATCH`
convention auto-derive the right pattern. Set `tag-pattern` explicitly only when
your tag scheme doesn't fit either default.

```yaml
# Non-default scheme: filter to "rel-*" tags
- uses: asleep-ai/actions/release-notes@release-notes/v1
  with:
    tag-pattern: 'rel-*'
    openai-api-key: ${{ secrets.OPENAI_API_KEY }}
```

## Caller contract

- **Checkout must use `fetch-depth: 0`.** The action runs `git log` and `git describe` locally; a shallow checkout will miss tags and historical commits.
- **Caller owns release creation.** The action is intentionally split from `gh release create` so the caller can decide on assets, drafts, prerelease, idempotency, and title.
- **AI failure is non-fatal.** If `openai-api-key` is empty or the API call fails, the action emits a `## Changes` heading followed by a bullet list of commit subjects. Tagging proceeds normally.

## Fallback output shape

```
## Changes

- Subject of commit 1 (#123)
- Subject of commit 2 (#124)
```

One bullet per commit, first non-blank line of each commit message. Squash-merge subjects with `(#NNN)` suffixes are preserved.

## AI output shape

The built-in prompt asks the model for:

```
## Highlights
## Features
## Bug fixes
## Internal
```

Empty sections are dropped. Override `system-prompt` for different sectioning, language, or audience.

## Custom prompt example

```yaml
- uses: asleep-ai/actions/release-notes@release-notes/v1
  id: notes
  with:
    openai-api-key: ${{ secrets.OPENAI_API_KEY }}
    system-prompt: |
      Android 앱 릴리즈 노트. 한국어 마크다운.
      섹션: ### 신규 기능, ### 개선, ### 버그 수정, ### 내부 변경.
      비는 섹션 생략. 사용자 관점으로 재작성.
```

## Versioning

Tags use the `release-notes/vX[.Y[.Z]]` prefix so siblings in this monorepo can release independently. Pin to the major (`@release-notes/v1`) for floating updates, or to a full version (`@release-notes/v1.0.0`) for reproducibility.
