#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "openai>=1.58,<2",
# ]
# ///
"""Generate markdown release notes from a git tag range via OpenAI.

Env:
  CUR              required -- current tag (e.g., v0.5.0)
  PREV             optional -- previous tag; if empty, full history is used
  OPENAI_API_KEY   required for AI summary; without it the commit list is returned
  OPENAI_MODEL     optional -- default: gpt-6-astra
  REASONING_EFFORT optional -- default: low; empty omits the parameter
  SYSTEM_PROMPT    optional -- override default prompt

Stdout: markdown. Never exits non-zero for AI failure -- always emits a usable
fallback so release creation isn't blocked by an OpenAI outage.

Run report: every run publishes what happened (status, model, tokens, estimated
cost) to the job -- a table in $GITHUB_STEP_SUMMARY, key=value pairs in
$GITHUB_OUTPUT, and one `::notice::` annotation carrying the record as JSON so
it stays queryable after the run via the check-run annotations API.

Caller contract: the workflow that invokes this script must have checked out
the repository with `fetch-depth: 0` so all tags and the full commit graph
are available locally.
"""
from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

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


# USD per 1M tokens (input, cached input, output) for the run report's cost
# estimate, keyed by exact model name; a dated snapshot suffix
# (`gpt-5.5-2026-04-23`) is stripped before lookup. Anything else -- including
# unlisted variants that merely share a prefix -- reports tokens but no cost
# rather than a guessed price.
# Source: https://developers.openai.com/api/docs/pricing (2026-09-04)
MODEL_PRICES_USD_PER_1M: dict[str, tuple[float, float, float]] = {
    "gpt-6-astra": (10.00, 1.00, 50.00),
    "gpt-5.6-sol": (4.00, 0.40, 20.00),
    "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5.6": (4.00, 0.40, 20.00),  # alias of gpt-5.6-sol
    "gpt-5.5": (5.00, 0.50, 30.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.4-nano": (0.20, 0.02, 1.25),
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5-nano": (0.05, 0.005, 0.40),
}


@dataclass(frozen=True)
class Usage:
    """Token counts from a Chat Completions response; zeros when no call was made."""

    prompt_tokens: int = 0
    cached_tokens: int = 0  # the part of prompt_tokens billed at the cached rate
    completion_tokens: int = 0

    @classmethod
    def from_response(cls, resp: object) -> Usage:
        usage = getattr(resp, "usage", None)
        details = getattr(usage, "prompt_tokens_details", None)
        return cls(
            prompt_tokens=getattr(usage, "prompt_tokens", None) or 0,
            cached_tokens=getattr(details, "cached_tokens", None) or 0,
            completion_tokens=getattr(usage, "completion_tokens", None) or 0,
        )


SNAPSHOT_SUFFIX_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def estimate_cost_usd(model: str, usage: Usage) -> float | None:
    """Estimate spend from the price table; None when the model is not listed."""
    prices = MODEL_PRICES_USD_PER_1M.get(SNAPSHOT_SUFFIX_RE.sub("", model))
    if prices is None:
        return None
    in_price, cached_price, out_price = prices
    uncached = max(usage.prompt_tokens - usage.cached_tokens, 0)
    total = uncached * in_price + usage.cached_tokens * cached_price + usage.completion_tokens * out_price
    return total / 1_000_000


@dataclass
class RunReport:
    """What this run did, published for humans and for later aggregation."""

    tag: str
    model: str
    status: str = "fallback"  # "ai" once model output was used
    reason: str = ""  # why the fallback was used: no-commits | no-api-key | openai-error | empty-response
    commits: int = 0
    usage: Usage = field(default_factory=Usage)
    duration_s: float = 0.0  # wall-clock of the model call only

    def as_record(self) -> dict[str, object]:
        cost = estimate_cost_usd(self.model, self.usage)
        return {
            "tag": self.tag,
            "action_ref": os.environ.get("GITHUB_ACTION_REF", ""),
            "status": self.status,
            "reason": self.reason,
            "model": self.model,
            "commits": self.commits,
            "prompt_tokens": self.usage.prompt_tokens,
            "cached_tokens": self.usage.cached_tokens,
            "completion_tokens": self.usage.completion_tokens,
            "estimated_cost_usd": None if cost is None else round(cost, 6),
            "duration_s": round(self.duration_s, 2),
        }


def annotation_escape(text: str) -> str:
    """Escape a workflow-command value (`::notice::...`) per the runner's rules."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def summary_markdown(record: dict[str, object]) -> str:
    cost = record["estimated_cost_usd"]
    cost_text = "n/a" if cost is None else f"${cost:.4f}"
    status = f"{record['status']} ({record['reason']})" if record["reason"] else str(record["status"])
    return (
        "### Release notes report\n\n"
        "| Tag | Status | Model | Commits | Prompt tokens (cached) | Completion tokens | Est. cost | Model call |\n"
        "|---|---|---|---|---|---|---|---|\n"
        f"| `{record['tag']}` | {status} | `{record['model']}` | {record['commits']} "
        f"| {record['prompt_tokens']:,} ({record['cached_tokens']:,}) | {record['completion_tokens']:,} "
        f"| {cost_text} | {record['duration_s']}s |\n"
    )


def append_to(path_env: str, text: str) -> None:
    """Append to the file a runner-provided env var names; no-op outside Actions."""
    path = os.environ.get(path_env)
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)


def publish_report(report: RunReport) -> None:
    """Publish the run record to the job.

    Three sinks: `$GITHUB_STEP_SUMMARY` (a table for humans), `$GITHUB_OUTPUT`
    (values for workflow logic, mapped to action outputs), and a `::notice::`
    annotation holding the JSON record. Summaries and outputs cannot be read
    back through the API once the run ends, but annotations can (check-run
    annotations endpoint), so the notice is what cross-repo reporting consumes.
    The runner parses workflow commands from stderr as well as stdout, so the
    notice goes through `log()` and never touches the markdown on stdout.
    Best-effort: a reporting failure must never fail a release.
    """
    record = report.as_record()
    cost = record["estimated_cost_usd"]
    outputs = {
        "status": record["status"],
        "reason": record["reason"],
        "model": record["model"],
        "prompt-tokens": record["prompt_tokens"],
        "cached-tokens": record["cached_tokens"],
        "completion-tokens": record["completion_tokens"],
        "estimated-cost-usd": "" if cost is None else cost,
    }
    try:
        log(f"::notice title=release-notes report::{annotation_escape(json.dumps(record))}")
        # One `key=value` per line. A value with a line break (say, a stray
        # newline in the model input) would make the runner reject the whole
        # file and fail the step, so values are flattened to a single line.
        append_to("GITHUB_OUTPUT", "".join(f"{k}={' '.join(str(v).split())}\n" for k, v in outputs.items()))
        append_to("GITHUB_STEP_SUMMARY", summary_markdown(record))
    except OSError as e:
        log(f"::warning::Could not publish run report: {e}")


def generate_ai_notes(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    version: str,
    commits: list[str],
    report: RunReport,
    reasoning_effort: str = "",
) -> str | None:
    """Ask the model for notes; record tokens, timing, and any failure on `report`.

    `reasoning_effort` is only sent when non-empty: reasoning tokens bill at
    the output rate, and summarising commits does not need deep reasoning, so
    the action defaults to `low`. Empty keeps the request valid for models
    that reject the parameter.
    """
    started = time.monotonic()
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
            **({"reasoning_effort": reasoning_effort} if reasoning_effort else {}),
        )
        report.usage = Usage.from_response(resp)
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            report.reason = "empty-response"
        return content or None
    except OpenAIError as e:
        log(f"::warning::OpenAI request failed: {e}")
        report.reason = "openai-error"
        return None
    finally:
        report.duration_s = time.monotonic() - started


def render_notes(cur: str, model: str, report: RunReport) -> str:
    """Return the markdown for stdout, recording on `report` how it was produced."""
    prev = os.environ.get("PREV") or None
    api_key = os.environ.get("OPENAI_API_KEY")
    system_prompt = os.environ.get("SYSTEM_PROMPT") or DEFAULT_SYSTEM_PROMPT
    reasoning_effort = os.environ.get("REASONING_EFFORT", "low")

    commits = git_commit_list(prev, cur)
    report.commits = len(commits)

    if not commits:
        log("::warning::No commits found in range, skipping AI call")
        report.reason = "no-commits"
        return fallback(commits)

    if not api_key:
        log("::warning::OPENAI_API_KEY not set, using commit list fallback")
        report.reason = "no-api-key"
        return fallback(commits)

    notes = generate_ai_notes(
        api_key=api_key,
        model=model,
        system_prompt=system_prompt,
        version=cur,
        commits=commits,
        report=report,
        reasoning_effort=reasoning_effort,
    )
    if notes is None:
        log("::warning::AI notes generation failed, using commit list fallback")
        return fallback(commits)

    report.status = "ai"
    return f"{notes}\n"


def main() -> int:
    cur = os.environ.get("CUR")
    if not cur:
        log("::error::CUR (current tag) env var is required")
        return 2

    model = os.environ.get("OPENAI_MODEL") or "gpt-6-astra"
    report = RunReport(tag=cur, model=model)
    try:
        sys.stdout.write(render_notes(cur, model, report))
    finally:
        publish_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
