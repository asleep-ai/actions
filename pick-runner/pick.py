"""Pick the runs-on value for a workflow's jobs before they are queued.

A job queued for self-hosted labels while every matching runner is offline
waits up to 24 hours instead of failing, so the choice has to be made up front,
on a GitHub-hosted runner that still answers when the self-hosted host is down.

action.yml passes every input through an environment variable, so no input is
ever interpolated into a shell command.

Rules:
- At least one online runner carries every label -> the labels. Busy runners
  count: waiting for one beats moving the run elsewhere. Labels compare
  case-insensitively, as GitHub matches runs-on labels.
- No matching runner is online -> the fallback, with a warning.
- The check itself cannot run (no token, API error, unreadable response, or
  any other error while reading the runners) -> the labels, with a warning. A
  failed check is not evidence that the runners are offline, and the labels are
  what the jobs used before this check.
- Invalid inputs fail the step: they are caller bugs, not runner state. An
  empty fallback or organization is not invalid: it means the default.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Tuple

DEFAULT_FALLBACK = "ubuntu-latest"
PER_PAGE = 100
MAX_PAGES = 50
NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="next"')

# (url, token) -> (body, Link header)
Fetch = Callable[[str, str], Tuple[bytes, Optional[str]]]


class InputError(Exception):
    """An input the caller has to fix."""


class CheckError(Exception):
    """The runner list could not be read."""


@dataclass(frozen=True)
class Result:
    runs_on: str  # JSON
    self_hosted: bool
    online_count: Optional[int]
    warning: Optional[Tuple[str, str]]  # (title, message)


def parse_labels(raw: str, name: str = "labels") -> list:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputError(f"{name} must be a JSON array of strings: {exc}") from None
    if not isinstance(value, list) or not value:
        raise InputError(f"{name} must be a non-empty JSON array of strings")
    for label in value:
        # A label with surrounding whitespace never equals a runner's label.
        if not isinstance(label, str) or not label or label != label.strip():
            raise InputError(
                f"{name} must contain only non-empty strings without surrounding whitespace, got {label!r}"
            )
    return value


def parse_fallback(raw: str) -> str:
    """Return the fallback as JSON: a quoted label or a label array.

    Anything else fails here. A value that is not a real label would queue the
    jobs for a runner that does not exist, the wait this action is there to
    prevent.
    """
    text = raw.strip()
    if not text:
        # action.yml's default only applies when the input is not given at all;
        # `fallback: ${{ vars.X }}` with X unset arrives here as an empty string.
        return json.dumps(DEFAULT_FALLBACK)
    if text.startswith("["):
        return json.dumps(parse_labels(text, "fallback"), separators=(",", ":"))
    shape = InputError(f"fallback must be one label or a JSON array of labels, got {text!r}")
    label = text
    if text.startswith('"'):
        # A JSON string, as a runs-on output is written: the label is inside.
        try:
            label = json.loads(text)
        except json.JSONDecodeError:
            raise shape from None
    # A label that opens like JSON or a quote, or holds a comma or whitespace, is
    # far more likely a list, an object or a quoted value written the wrong way;
    # the array form still takes such a label.
    if not label or label[0] in "[{\"'" or "," in label or any(ch.isspace() for ch in label):
        raise shape
    return json.dumps(label)


def next_link(link: Optional[str], api_url: str) -> Optional[str]:
    if not link:
        return None
    match = NEXT_LINK.search(link)
    if not match:
        return None
    url = match.group(1)
    # The token goes with every request, so only ever follow the API's own host.
    if not url.startswith(api_url.rstrip("/") + "/"):
        raise CheckError(f"pagination link points outside {api_url}: {url}")
    return url


def list_runners(org: str, token: str, api_url: str, fetch: Fetch) -> list:
    base = api_url.rstrip("/")
    url: Optional[str] = f"{base}/orgs/{urllib.parse.quote(org, safe='')}/actions/runners?per_page={PER_PAGE}"
    runners: list = []
    total = None
    pages = 0
    while url:
        pages += 1
        if pages > MAX_PAGES:
            raise CheckError(f"more than {MAX_PAGES} pages of runners")
        body, link = fetch(url, token)
        try:
            document = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise CheckError("the runner list is not JSON") from None
        if not isinstance(document, dict) or not isinstance(document.get("runners"), list):
            raise CheckError("the response has no runners list")
        runners.extend(document["runners"])
        total = document.get("total_count", total)
        url = next_link(link, base)
    # total_count is every runner of the organization. Reading fewer means a
    # page was missed, for instance behind a Link header this parser cannot
    # follow, and the missing page may hold the online runner.
    if isinstance(total, int) and not isinstance(total, bool) and total != len(runners):
        raise CheckError(f"read {len(runners)} runners where the response counts {total}")
    return runners


def count_online(runners: list, labels: list) -> int:
    """Count the online runners carrying every label.

    An entry that does not look as expected is a failed check, never a runner
    that does not match: skipping it could turn an online runner into "offline".
    """
    wanted = {label.casefold() for label in labels}
    online = 0
    for runner in runners:
        if not isinstance(runner, dict):
            raise CheckError("unexpected runner entry in the response")
        status = runner.get("status")
        if status not in ("online", "offline"):
            raise CheckError(f"unexpected runner status in the response: {status!r}")
        carried = runner.get("labels")
        if not isinstance(carried, list) or not all(
            isinstance(label, dict) and isinstance(label.get("name"), str) for label in carried
        ):
            raise CheckError("unexpected runner labels in the response")
        if status == "online" and wanted <= {label["name"].casefold() for label in carried}:
            online += 1
    return online


def decide(labels: list, fallback: str, token: str, org: str, api_url: str, fetch: Fetch) -> Result:
    chosen = json.dumps(labels, separators=(",", ":"))
    if not token:
        return Result(chosen, True, None, (
            "Runner check skipped",
            "No token was given, so the jobs go to the self-hosted labels unchecked.",
        ))
    try:
        online = count_online(list_runners(org, token, api_url, fetch), labels)
    except Exception as exc:
        # Whatever stopped the check, expected or not, it is a failed check and
        # not evidence that the runners are offline. An unexpected exception is
        # named by type only: its message can quote the request, token included.
        reason = str(exc) if isinstance(exc, CheckError) else f"unexpected error ({type(exc).__name__})"
        return Result(chosen, True, None, (
            "Runner check failed",
            f"Could not read the runners of {org}, so the jobs go to the self-hosted labels unchecked: {reason}",
        ))
    if online > 0:
        return Result(chosen, True, online, None)
    return Result(fallback, False, 0, (
        "Self-hosted runners offline",
        f"No online runner carries every label in {chosen}, so this run uses {fallback}.",
    ))


class SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only when it stays on the host that was asked.

    urllib sends the Authorization header along with a redirect, wherever it
    points, so a redirect to another host would hand that host the token.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        asked = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(newurl)
        if (asked.scheme, asked.netloc) != (target.scheme, target.netloc):
            raise CheckError(f"redirect points outside {asked.scheme}://{asked.netloc}: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(SameHostRedirects)


def http_get(url: str, token: str) -> Tuple[bytes, Optional[str]]:
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "asleep-ai-actions-pick-runner",
    })
    try:
        with _OPENER.open(request, timeout=20) as response:
            return response.read(), response.headers.get("Link")
    except urllib.error.HTTPError as exc:
        detail = exc.read(300).decode(errors="replace").strip()
        raise CheckError(f"HTTP {exc.code}: {detail}") from None
    # http.client's own errors (a truncated body, a reply that is not HTTP) are
    # not OSErrors.
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise CheckError(str(exc) or type(exc).__name__) from None


def escape(text: str) -> str:
    """Escape workflow-command data so a message cannot end the command early."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(env: Mapping[str, str] = os.environ, fetch: Fetch = http_get) -> int:
    try:
        labels = parse_labels(env.get("LABELS", ""))
        fallback = parse_fallback(env.get("FALLBACK", ""))
        # As with fallback, an input given as an empty string gets the default.
        org = env.get("ORG", "").strip() or env.get("GITHUB_REPOSITORY_OWNER", "").strip()
        if not org:
            raise InputError("organization must not be empty")
    except InputError as exc:
        print(f"::error title=pick-runner::{escape(str(exc))}")
        return 1

    api_url = env.get("GITHUB_API_URL") or "https://api.github.com"
    # A secret pasted with a trailing newline is still the same token.
    token = env.get("TOKEN", "").strip()
    result = decide(labels, fallback, token, org, api_url, fetch)

    if result.warning:
        title, message = result.warning
        print(f"::warning title={escape(title)}::{escape(message)}")
    count = "" if result.online_count is None else str(result.online_count)
    where = "self-hosted" if result.self_hosted else "fallback"
    print(f"Picked {where}: runs-on={result.runs_on} online={count or 'unknown'}")

    output_path = env.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write(f"runs-on={result.runs_on}\n")
            output.write(f"self-hosted={'true' if result.self_hosted else 'false'}\n")
            output.write(f"online-count={count}\n")
    summary_path = env.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(f"pick-runner: **{where}** (`{result.runs_on}`, online runners: {count or 'unknown'})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
