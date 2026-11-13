# OpenAI advisory PR review

A reusable composite action using the official **Responses API**, paid with the caller's OpenAI API project key. It is a **single tool-free model review**, not Codex CLI, Agents SDK, or an agent that explores/runs the repository. This deliberately trades autonomous context gathering for a smaller attack surface. No PR code is checked out or executed.

## Adoption

Copy `workflow.yml.example` into a consuming repo's `.github/workflows/openai-review.yml`. Replace `REVIEWED_FULL_COMMIT_SHA` with the reviewed **40-character commit SHA** containing this action. Also replace `REVIEWED_UPLOAD_ARTIFACT_SHA` with a reviewed **40-character commit SHA** of `actions/upload-artifact`. Both placeholders must be replaced before the workflow can run. Configure `OPENAI_API_KEY` in that repository and set project budgets/alerts in OpenAI. Do not paste a key into workflow YAML. Model is an explicit API model ID; account/UI nicknames are not assumed valid API IDs.

Run manually from a **protected default branch** with a PR number. Only users who can dispatch that workflow can trigger spending; code also rejects PR, issue-comment, workflow-run and pull_request_target triggers. Do not broaden triggers without reviewing the trust boundary and cost abuse risk. Repository write collaborators remain trusted operators: GitHub permits them to choose workflow refs, so protect refs and secrets appropriately. No fork approval policy or repository settings are changed by installing this action.

It fetches up to 100 changed-file patches using the caller's read-only `github.token`, checks head/base SHAs before calling OpenAI, and makes one Responses request. GitHub patches may be missing or truncated; the report is partial review, not approval. No automatic comment, approval, merge, checkout, build, shell tool, web tool or MCP tool is performed. Results are uploaded as an artifact by the example, not printed as workflow commands.

## Inputs and outputs

Required inputs: `pr-number`, `model`, `openai-api-key`, `github-token`. Optional `max-output-tokens` defaults to 4000, range 256..16000. Outputs: `path` and `head-sha`.

Runtime: Python 3 on a fresh GitHub-hosted Linux runner. Python standard library only: no pip installer or unpinned SDK runtime dependency. Fixed HTTPS GitHub/OpenAI endpoints; redirects rejected. GHES is not supported. Max model context is approximately 120,000 characters of patches, not tokens; API file responses are capped at 4 MB. Oversized PRs fail closed instead of silently reviewing only their first page.

## Data, permissions and security

- Diff text and file paths go to OpenAI. Do not use if company policy disallows this. `store:false` is requested; this is **not** a claim of zero retention or a substitute for the account's data policy.
- Common `.env`, private-key and keystore filenames are excluded. This is **not comprehensive secret detection**: secrets embedded in ordinary source files may be sent. Run your existing secret scanning and review data eligibility before dispatch.
- PR content is untrusted, including instruction-like text. The model receives no tools or credentials. Prompt injection can still distort the advisory report; validate findings and treat links/instructions in generated output as untrusted.
- No dollar-budget guarantee: output/context caps limit individual requests, but project budget/alerts and access governance remain caller-owned. Read the reported usage; no automatic retries are performed.
- Keep review separate from signing/deployment jobs, keys and writable caches. GitHub-hosted fresh runner is recommended. Do not run this on the release self-hosted pool.
- Pin this action to a full commit SHA; semantic and major tags can move. Review dependency updates. Public code visibility does not itself grant outsiders execution or secret access.

## Agent review alternative

For repository exploration/test-running, use the official `openai/codex-action` or Codex SDK in a **separately reviewed** disposable environment. That executes agent-selected commands and has a different trust boundary. Do not simply hand release credentials or the host Docker socket to it. This action does not claim to provide agentic review.

## Tests

`python3 -m unittest discover -s openai-review -p 'test_*.py' -v`

Tests mock both APIs. They verify event/input rejection, bounded coverage, sensitive-path exclusions, PR revision race rejection and no tool requests. They do not spend credits or post comments. Validate a manual run with an approved API project after adoption; no live API compatibility claim is made by unit tests.
