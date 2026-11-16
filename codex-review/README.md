# Automatic Codex agent PR review

Reusable workflow: `.github/workflows/code-review.yml`. This uses the official [openai/codex-action](https://github.com/openai/codex-action), not a single diff-to-Responses request. Codex CLI runs a multi-step agent loop: inspect the diff, search/read related repository code, and produce advisory Korean review. Its official Responses proxy uses your OpenAI API project key. The agent can invoke read-only shell tools; it is not given a build/test environment.

## Adopt

1. Copy `workflow.yml.example` into the consumer's `.github/workflows/`.
2. Replace `REVIEWED_FULL_COMMIT_SHA` with the full 40-character reviewed commit of this repository. Do not use a floating `main`/major tag for privileged execution.
3. Set repository variable `CODEX_REVIEW_MODEL` to an exact Codex-compatible API model ID available to the approved project. UI nicknames are not API model IDs. Set secret `OPENAI_API_KEY`; do not use `secrets: inherit`.
4. Configure project usage controls/alerts and approve source-code processing by OpenAI. Protect consumer workflow files and this shared workflow's publishing refs.

The caller's `pull_request` events (`opened`, `synchronize`, `reopened`, `ready_for_review`) trigger automatically. Non-draft same-repository PRs by human actors with write/maintain/admin access are eligible. Both original and rerun actors are checked. Forks, bots, read-only outsiders, `pull_request_target`, manual dispatch and label triggers are refused. No wildcard actor allowlist or secret-bearing fork execution is provided. Fork support would require a separate explicit trust/cost policy.

`post-comment` defaults true. Set false to omit PR publication; feedback remains available in the workflow job summary. One bot-owned marked comment is updated, rather than appended on every push. Publishing rechecks head and base SHA and skips stale/closed PRs. Mentions are neutralized and output size is capped; review content remains untrusted advice.

## Trust boundaries

- Three separate fresh `ubuntu-latest` jobs: read-only authorization gate; read-only agent; minimum-permission PR publisher. No self-hosted or release runner support, writable caches, release secrets or signing keys.
- Checkout exact validated PR head, no persisted credentials, no submodules/LFS, no dependency installation or project scripts before the agent. Full history supports merge-base diff inspection; this makes repository history available to the agent.
- Official Codex action pinned at `13e9c17417b29e9a276f73ec6ed7fb574137d3c3`; CLI and Responses proxy pinned to `0.167.2`. Review both when updating. Registry version pinning is not a claim of verified package provenance or hash locking.
- `permission-profile: ':read-only'` plus **`safety-strategy: drop-sudo`**, not the weaker similarly named `safety-strategy: read-only`. Codex home is freshly created outside the checkout, with `project_doc_max_bytes = 0` and explicit project `trust_level = "untrusted"`: no automatic AGENTS instructions or project-local `.codex` configuration/hooks. Source can still be read as evidence. Official actor gate remains active; bots are not allowed.
- After Codex, the read-only agent job performs only fixed local text processing without new credentials or API access: uses isolated Python (`-I`) outside the checkout, rejects symlinks/non-regular files, reads at most 48,001 bytes, clips UTF-8 safely, and writes a bounded output and fenced summary. This intentionally deviates from the official general last-step recommendation, relying on the read-only sandbox to prevent host mutation. Never widen the sandbox or add privileged steps. A fresh publisher receives only bounded generated text/revision metadata, no checkout and no OpenAI key. Text is passed through environment variables and GitHub API data, never evaluated as shell/JavaScript.
- Official drop-sudo setup changes privileges/socket access and can configure Linux user-namespace/AppArmor settings on the disposable hosted VM. Do not copy this into persistent runners. These changes do not alter repository security settings.

Repository content and instructions can still prompt-inject the review; read-only execution and privilege separation limit consequences, not model persuasion. The agent can read repository source/history, so unlike filename-filtered diff review there is **no sensitive-file exclusion guarantee**. Never use a checkout containing secrets; use existing secret scanning and an approved data policy. No zero-retention claim, hard-dollar budget, guaranteed bug detection, test completion or guaranteed prevention of all attacks is made. Fifteen-minute timeout and cancellation on newer runs bound time, not API dollars.

Repository/organization branch protection, fork approval rules, runner-group access and billing are not configured here. Existing release/signing jobs remain separate work. Installing this workflow does not automatically enable reviews in consuming repos.

## Validation

`node --test codex-review/test-workflow.mjs && python3 codex-review/test-bound.py`

Tests execute the actual inline gate/publisher scripts against mocked GitHub APIs and assert trust-boundary properties. They do not call OpenAI, execute Codex, spend credits or post real comments. Live integration should be checked on an approved internal PR after adoption; no such run has been performed in this PR.

Official references: [usage and automatic review example](https://github.com/openai/codex-action/blob/13e9c17417b29e9a276f73ec6ed7fb574137d3c3/README.md), [security](https://github.com/openai/codex-action/blob/13e9c17417b29e9a276f73ec6ed7fb574137d3c3/docs/security.md).
