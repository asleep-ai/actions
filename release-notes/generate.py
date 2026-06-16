#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "openai>=1.55,<2",
# ]
# ///
"""Generate markdown release notes from a git tag range via OpenAI.

Env:
  CUR              required -- current tag (e.g., v0.5.0)
  PREV             optional -- previous tag; if empty, full history is used
  OPENAI_API_KEY   required for AI summary; without it the commit list is returned
  OPENAI_MODEL     optional -- default: gpt-5.5
  SYSTEM_PROMPT    optional -- override default prompt

Stdout: markdown. Never exits non-zero for AI failure -- always emits a usable
fallback so release creation isn't blocked by an OpenAI outage.

Caller contract: the workflow that invokes this script must have checked out
the repository with `fetch-depth: 0` so all tags and the full commit graph
are available locally.
"""
from __future__ import annotations

import functools
import os
import re
import subprocess
import sys
import time

from openai import OpenAI, OpenAIError

DEFAULT_SYSTEM_PROMPT = (
    "Generate release notes from a list of merged-PR commits. Output markdown "
    "only -- no preamble, no trailing commentary. Use these sections (omit "
    "empty ones, do not invent content): ## Highlights, ## Features, "
    "## Bug fixes, ## Internal. "
    "Input is a numbered list of commits; each entry contains the PR subject "
    "on the first line and the PR description (rationale, compatibility "
    "notes, verification) on the following lines. Use the description -- not "
    "just the subject -- when summarising user impact. Wrap code identifiers, "
    "UUIDs, paths, and numeric thresholds in backticks. Preserve issue and "
    "PR references (#NNN). Ignore `Co-Authored-By:` trailers and merge "
    "artefacts."
)


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def git_commit_list(prev: str | None, cur: str, limit: int = 500) -> list[str]:
    """Return commits in [prev..cur] as full messages.

    Squash-merged PRs put the PR description in the commit body, where the
    rationale and compatibility notes live. We pull `%B` (full message) and
    delimit entries with `git log -z` so NUL bytes (which cannot appear in
    commit text) separate records -- robust against any token a PR body
    might contain.
    """
    ref = f"{prev}..{cur}" if prev else cur
    try:
        result = subprocess.run(
            ["git", "log", "-z", f"--max-count={limit}", "--pretty=format:%B", ref],
            capture_output=True,
            text=True,
            check=True,
        )
        return [entry for entry in result.stdout.split("\x00") if entry.strip()]
    except subprocess.CalledProcessError as e:
        log(f"::warning::git log failed ({e.stderr.strip() if e.stderr else e}); using empty commit list")
        return []


def commit_subjects_only(commits: list[str]) -> str:
    """First non-blank line of each commit, formatted as a bullet list.

    Used by the fallback path so a no-AI release still produces a tidy
    summary instead of dumping every PR body verbatim.
    """
    subjects: list[str] = []
    for entry in commits:
        for line in entry.splitlines():
            stripped = line.strip()
            if stripped:
                subjects.append(f"- {stripped}")
                break
    return "\n".join(subjects)


def fallback(commits: list[str]) -> str:
    return f"## Changes\n\n{commit_subjects_only(commits)}\n"


# The squash-merge PR ref is the trailing `(#N)` on the subject line. Anchoring
# to the line end avoids refs embedded in the title/body -- e.g. a revert subject
# `Revert "Feature (#42)" (#43)` must enrich with #43, not the reverted #42.
PR_REF_RE = re.compile(r"\(#(\d+)\)\s*$")


def pr_ref(entry: str) -> int | None:
    """Return the squash-merge PR number from a commit's subject line, if any."""
    for line in entry.splitlines():
        if line.strip():  # first non-blank line is the subject
            match = PR_REF_RE.search(line)
            return int(match.group(1)) if match else None
    return None


@functools.cache
def pr_body(number: int) -> str:
    """Fetch a pull request's description via the `gh` CLI.

    Squash merges frequently land with an empty commit body, so the PR
    rationale, compatibility notes, and verification live only on the pull
    request. `gh` is preinstalled on GitHub runners and already used by the
    release workflow; it handles auth (GH_TOKEN/GITHUB_TOKEN), host, and JSON.
    Any failure -- no token, the number is an issue not a PR, an API error,
    `gh` not on PATH, or a hang past the timeout -- yields an empty string,
    so the caller falls back to the commit message. Cached so a PR referenced
    by several commits is fetched at most once.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(number), "--json", "body", "--jq", ".body"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        # gh absent (OSError) or hung past the timeout (TimeoutExpired, a
        # SubprocessError). Enrichment is best-effort -- never block a release.
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


# Wall-clock cap on PR-body enrichment across the whole run. gh calls are
# normally sub-second; this only bites during a sustained gh/API hang, where
# 500 commits * the 10s per-PR timeout could otherwise stall a release for
# ~80 minutes. Once spent, remaining commits fall back to the message alone.
ENRICH_BUDGET_S = 120

# Bounds on PR-body text appended to the prompt so a few huge descriptions, or
# one very long range, cannot push the request past the model's input limit and
# force a fallback to bare commit subjects for the whole release.
PR_BODY_CHAR_LIMIT = 4000  # per PR
PR_BODY_TOTAL_LIMIT = 40000  # across the run


def truncate(text: str, limit: int) -> str:
    """Trim text to `limit` chars, appending a marker when it was cut."""
    if len(text) <= limit:
        return text
    marker = "\n[... truncated]"
    return text[: max(0, limit - len(marker))].rstrip() + marker


def format_commits_for_prompt(commits: list[str]) -> str:
    """Render commits as a numbered list, enriched with referenced PR bodies.

    PR-body lookups share a total wall-clock budget (`ENRICH_BUDGET_S`) so a
    hanging `gh`/API never holds the release for long, and appended text is
    bounded per PR (`PR_BODY_CHAR_LIMIT`) and overall (`PR_BODY_TOTAL_LIMIT`)
    so a verbose range can't push the request past the model's input limit.
    """
    deadline = time.monotonic() + ENRICH_BUDGET_S
    warned = False
    chars_used = 0
    blocks: list[str] = []
    for i, entry in enumerate(commits, start=1):
        block = f"### Commit {i}\n{entry.strip()}"
        number = pr_ref(entry)
        if number is not None and chars_used < PR_BODY_TOTAL_LIMIT:
            if time.monotonic() >= deadline:
                if not warned:
                    log(f"::warning::PR-body enrichment budget ({ENRICH_BUDGET_S}s) exceeded; remaining commits use commit message only")
                    warned = True
            else:
                body = pr_body(number)
                if body:
                    snippet = truncate(body, PR_BODY_CHAR_LIMIT)
                    chars_used += len(snippet)
                    block += f"\n\nPR #{number} description:\n{snippet}"
        blocks.append(block)
    return "\n\n".join(blocks)


def generate_ai_notes(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    version: str,
    commits: list[str],
) -> str | None:
    try:
        client = OpenAI(api_key=api_key, timeout=60.0, max_retries=2)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": f"Version: {version}\n\nCommits:\n{format_commits_for_prompt(commits)}",
                },
            ],
        )
        content = (resp.choices[0].message.content or "").strip()
        return content or None
    except OpenAIError as e:
        log(f"::warning::OpenAI request failed: {e}")
        return None


def main() -> int:
    cur = os.environ.get("CUR")
    if not cur:
        log("::error::CUR (current tag) env var is required")
        return 2

    prev = os.environ.get("PREV") or None
    api_key = os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("OPENAI_MODEL") or "gpt-5.5"
    system_prompt = os.environ.get("SYSTEM_PROMPT") or DEFAULT_SYSTEM_PROMPT

    commits = git_commit_list(prev, cur)

    if not commits:
        log("::warning::No commits found in range, skipping AI call")
        sys.stdout.write(fallback(commits))
        return 0

    if not api_key:
        log("::warning::OPENAI_API_KEY not set, using commit list fallback")
        sys.stdout.write(fallback(commits))
        return 0

    notes = generate_ai_notes(
        api_key=api_key,
        model=model,
        system_prompt=system_prompt,
        version=cur,
        commits=commits,
    )
    if notes is None:
        log("::warning::AI notes generation failed, using commit list fallback")
        sys.stdout.write(fallback(commits))
        return 0

    print(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
