# Security Policy

## Reporting a Vulnerability

If you believe you have found a security issue in any action in this repository, please report it via [GitHub's private security advisory flow](https://github.com/asleep-ai/actions/security/advisories/new) instead of a public issue.

We aim to respond within 5 business days and will coordinate a fix and disclosure timeline with the reporter.

## Scope

The actions in this repo execute untrusted commit messages (`%B` from `git log`) and pass them to third-party APIs (OpenAI). Concerns specifically in scope:

- Command injection through commit subject/body content
- Credential leakage in workflow logs (`OPENAI_API_KEY`, `GITHUB_TOKEN`)
- Tag/ref handling that could trick consumers into running the wrong code

Out of scope: prompt-injection of the AI-generated notes themselves — the output is reviewed before release publication, and the action is not a security boundary.
