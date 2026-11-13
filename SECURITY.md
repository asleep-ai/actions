# Security Policy

## Reporting a Vulnerability

If you believe you have found a security issue in any action in this repository, please report it via [GitHub's private security advisory flow](https://github.com/asleep-ai/actions/security/advisories/new) instead of a public issue.

We aim to respond within 5 business days and will coordinate a fix and disclosure timeline with the reporter.

## Scope

Some actions read untrusted commit messages (`%B` from `git log`) and pass them to third-party APIs (OpenAI). Repository text is data, never a source of authorization. Concerns specifically in scope:

- Command injection through commit subject/body content
- Credential leakage in workflow logs (`OPENAI_API_KEY`, `GITHUB_TOKEN`)
- Tag/ref handling that could trick consumers into running the wrong code

AI-generated notes and reviews are untrusted advisory output. Prompt injection, misleading content and accidental disclosure are relevant risks. Callers must add review where needed; this repository’s release workflow currently publishes generated notes automatically, so human review must not be assumed. No action is a sandbox for untrusted code.

## Public contribution boundary

Keep pull-request CI on disposable GitHub-hosted runners with read-only permissions and no secrets. Do not execute PR head code in a privileged `pull_request_target` job. Never share signing workspaces, writable toolchains, Docker sockets or caches with untrusted PR jobs. Pin consumed actions to reviewed full commit SHAs and protect release/tag publishing through repository administration. These are operator requirements; documentation does not configure or verify those settings.

`openai-review` rejects non-manual triggers, does not execute PR code, and produces no comments or approvals. Maintainers must still authorize source-data transmission and API spend and validate model output.
