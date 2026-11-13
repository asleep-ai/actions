"""Plan the next release of a repository whose versions are git tags.

action.yml passes every input through an environment variable, so no input is
ever interpolated into a shell command.

Operations:

next  Find the last release tag, compute the next version, resolve the commit
      to release and check that it can be released:
      - The last release tag is the highest <prefix>X.Y.Z among all tags of the
        repository, compared as numbers. Tags with a suffix (v1.2.3-rc.1), a
        leading zero or another prefix are not release tags.
      - The commit is the merge commit of a merged pull request (pr) or, as the
        exception, a commit named directly (sha).
      - The commit has to contain the last release tag and at least one commit
        after it. Without a release tag there is nothing to contain, and the
        version is counted from 0.0.0.
      The pull requests between the two are listed from the first-parent
      history, read from the commit subjects.

code  The versionCode of a release or of a QA build, from version_code.py.

Anything that cannot be decided fails the step: a release is not planned on a
guess.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from version_code import VersionCodeError, qa_code, release_code

BUMPS = ("patch", "minor", "major")
KINDS = ("release", "qa")
# A leading zero is not a version number: v01.2.3 and v1.2.3 would be the same release.
NUMBER = r"(0|[1-9][0-9]*)"
VERSION = re.compile(rf"{NUMBER}\.{NUMBER}\.{NUMBER}")
# Characters git allows in a ref name and that need no quoting anywhere a tag is used. Not a leading
# hyphen: git would read such a tag as an option.
TAG_PREFIX = re.compile(r"([A-Za-z0-9._/][A-Za-z0-9._/-]*)?")
SHA = re.compile(r"[0-9a-fA-F]{7,64}")
SQUASH_SUBJECT = re.compile(r"(?P<title>.*\S)\s*\(#(?P<number>[0-9]+)\)")
MERGE_SUBJECT = re.compile(r"Merge pull request #(?P<number>[0-9]+) from \S+")
# The list is read by a person approving a release; a release this large is summarised.
MAX_LISTED = 200
ZERO = (0, 0, 0)

# (url, token) -> body
Fetch = Callable[[str, str], bytes]


class Failure(Exception):
    """A reason the release cannot be planned."""


@dataclass(frozen=True)
class Plan:
    version: str
    tag: str
    previous_tag: str  # empty when the repository has no release tag
    target_sha: str
    prs: str  # markdown list
    version_code: int


def escape(text: str) -> str:
    """Escape workflow-command data so a message cannot end the command early."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise Failure(f"git {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def parse_version(text: str) -> tuple:
    match = VERSION.fullmatch(text)
    if not match:
        raise Failure(f"{text!r} is not a version of the form X.Y.Z")
    return tuple(int(part) for part in match.groups())


def format_version(version: tuple) -> str:
    return ".".join(str(part) for part in version)


def bump_version(version: tuple, bump: str) -> tuple:
    major, minor, patch = version
    if bump == "major":
        return (major + 1, 0, 0)
    if bump == "minor":
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def last_release(prefix: str) -> Optional[tuple]:
    """The highest release version among the tags, or None when there is none."""
    versions = []
    for name in git("tag", "--list").stdout.splitlines():
        if name.startswith(prefix):
            match = VERSION.fullmatch(name[len(prefix):])
            if match:
                versions.append(tuple(int(part) for part in match.groups()))
    return max(versions) if versions else None


def commit_of(revision: str) -> Optional[str]:
    """The commit a revision names (a tag is followed to its commit), or None."""
    result = git("rev-parse", "--verify", "--quiet", revision + "^{commit}", check=False)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


class SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only when it stays on the host that was asked.

    urllib sends the Authorization header along with a redirect, wherever it
    points, so a redirect to another host would hand that host the token.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        asked = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(newurl)
        if (asked.scheme, asked.netloc) != (target.scheme, target.netloc):
            raise Failure(f"redirect points outside {asked.scheme}://{asked.netloc}: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(SameHostRedirects)


def http_get(url: str, token: str) -> bytes:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "asleep-ai-actions-release-version",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with _OPENER.open(urllib.request.Request(url, headers=headers), timeout=20) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read(300).decode(errors="replace").strip()
        raise Failure(f"HTTP {exc.code} from {url}: {detail}") from None
    # http.client's own errors (a truncated body, a reply that is not HTTP) are not OSErrors.
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise Failure(f"cannot reach {url}: {str(exc) or type(exc).__name__}") from None


def merge_commit_of(number: str, env: Mapping[str, str], fetch: Fetch) -> str:
    """The commit a merged pull request left on its base branch."""
    if not re.fullmatch(r"[1-9][0-9]*", number):
        raise Failure(f"pr must be a pull request number, got {number!r}")
    repository = env.get("GITHUB_REPOSITORY", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repository):
        raise Failure("GITHUB_REPOSITORY is not set to owner/name")
    api_url = (env.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    # A secret pasted with a trailing newline is still the same token.
    body = fetch(f"{api_url}/repos/{repository}/pulls/{number}", env.get("TOKEN", "").strip())
    try:
        pull = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise Failure(f"the response for pull request #{number} is not JSON") from None
    if not isinstance(pull, dict):
        raise Failure(f"the response for pull request #{number} is not an object")
    # An open pull request has a merge_commit_sha too: the test merge, which is on no branch.
    if pull.get("merged") is not True:
        raise Failure(f"pull request #{number} is not merged (state: {pull.get('state')!r})")
    sha = pull.get("merge_commit_sha")
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise Failure(f"pull request #{number} has no merge commit")
    commit = commit_of(sha)
    if commit is None:
        raise Failure(
            f"the merge commit {sha} of pull request #{number} is not in this checkout; "
            "check out the repository with fetch-depth: 0"
        )
    return commit


def named_commit(sha: str) -> str:
    # Only a commit id: a branch name would name a different commit on the next run.
    if not SHA.fullmatch(sha):
        raise Failure(f"sha must be a commit id of 7 or more hex digits, got {sha!r}")
    commit = commit_of(sha)
    if commit is None:
        raise Failure(f"commit {sha} is not in this checkout, or the id is ambiguous")
    return commit


def one_line(text: str) -> str:
    return " ".join(text.split())


def pull_requests(previous: Optional[str], target: str) -> str:
    """Markdown list of what target adds to previous, oldest first.

    One entry per first-parent commit: a squash merge names its pull request in
    the subject's trailing (#N), a merge commit in "Merge pull request #N". A
    commit that names none was pushed directly and is listed by its id.
    """
    revisions = f"{previous}..{target}" if previous else target
    log = git("log", "--first-parent", "--reverse", "--format=%H%x1f%s%x1f%b%x1e", revisions, "--")
    entries = []
    seen = set()
    for record in log.stdout.split("\x1e"):
        if not record.strip():
            continue
        sha, subject, body = (record.strip("\n").split("\x1f") + ["", ""])[:3]
        merge = MERGE_SUBJECT.match(subject)
        squash = SQUASH_SUBJECT.fullmatch(subject)
        if merge:
            number = merge["number"]
            lines = [line for line in body.splitlines() if line.strip()]
            title = lines[0] if lines else subject
        elif squash:
            number, title = squash["number"], squash["title"]
        else:
            entries.append(f"- `{sha[:7]}` {one_line(subject)}")
            continue
        if number not in seen:
            seen.add(number)
            entries.append(f"- #{number} {one_line(title)}")
    if len(entries) > MAX_LISTED:
        hidden = len(entries) - MAX_LISTED
        entries = entries[:MAX_LISTED] + [f"- … and {hidden} more"]
    return "\n".join(entries)


def plan_next(env: Mapping[str, str], fetch: Fetch) -> Plan:
    pr = env.get("PR", "").strip().lstrip("#")
    sha = env.get("SHA", "").strip()
    bump = env.get("BUMP", "").strip()
    prefix = env.get("TAG_PREFIX", "")
    if bool(pr) == bool(sha):
        raise Failure("give exactly one of pr and sha")
    if bump not in BUMPS:
        raise Failure(f"bump must be one of {', '.join(BUMPS)}, got {bump!r}")
    if not TAG_PREFIX.fullmatch(prefix):
        raise Failure(f"tag-prefix may hold only letters, digits and . _ / -, and not start with -, got {prefix!r}")

    if git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        raise Failure("the checkout is shallow, so tags and history are incomplete; check out with fetch-depth: 0")

    previous = last_release(prefix)
    version = bump_version(previous or ZERO, bump)
    tag = prefix + format_version(version)
    previous_tag = prefix + format_version(previous) if previous else ""
    try:
        code = release_code(*version)
    except VersionCodeError as exc:
        raise Failure(str(exc)) from None

    target = merge_commit_of(pr, env, fetch) if pr else named_commit(sha)
    what = f"the merge commit of pull request #{pr}" if pr else "the commit"

    previous_commit = None
    if previous_tag:
        previous_commit = commit_of(previous_tag)
        if previous_commit is None:
            raise Failure(f"tag {previous_tag} does not name a commit")
        if previous_commit == target:
            raise Failure(f"{what} ({target[:7]}) is the commit of {previous_tag}: there is nothing new to release")
        if git("merge-base", "--is-ancestor", previous_commit, target, check=False).returncode != 0:
            raise Failure(
                f"{what} ({target[:7]}) does not contain the last release {previous_tag} ({previous_commit[:7]}). "
                f"A release has to build on the one before it: merge the history of {previous_tag} into the "
                "branch that is released first"
            )
    return Plan(
        version=format_version(version),
        tag=tag,
        previous_tag=previous_tag,
        target_sha=target,
        prs=pull_requests(previous_commit, target),
        version_code=code,
    )


def write_outputs(env: Mapping[str, str], outputs: Mapping[str, str]) -> None:
    path = env.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as output:
        for name, value in outputs.items():
            if "\n" in value or "\r" in value:
                # A delimiter no value can contain, so a commit subject cannot end the value early.
                delimiter = f"release-version-{secrets.token_hex(16)}"
                output.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")
            else:
                output.write(f"{name}={value}\n")


def run_next(env: Mapping[str, str], fetch: Fetch) -> None:
    plan = plan_next(env, fetch)
    print(f"Last release: {plan.previous_tag or 'none'}")
    print(f"Next release: {plan.tag} (versionCode {plan.version_code}) at {plan.target_sha}")
    print(plan.prs or "No commits listed.")
    write_outputs(env, {
        "version": plan.version,
        "tag": plan.tag,
        "previous-tag": plan.previous_tag,
        "target-sha": plan.target_sha,
        "prs": plan.prs,
        "version-code": str(plan.version_code),
    })


def run_code(env: Mapping[str, str]) -> None:
    text = env.get("VERSION", "").strip()
    prefix = env.get("TAG_PREFIX", "")
    kind = env.get("KIND", "").strip()
    if kind not in KINDS:
        raise Failure(f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
    # The version may be given as its tag.
    if prefix and text.startswith(prefix):
        text = text[len(prefix):]
    version = parse_version(text)
    try:
        if kind == "release":
            code = release_code(*version)
        else:
            sequence = env.get("QA_SEQUENCE", "").strip()
            if not re.fullmatch(r"[0-9]+", sequence):
                raise Failure(f"qa-sequence must be a whole number, got {sequence!r}")
            code = qa_code(version[0], version[1], int(sequence))
    except VersionCodeError as exc:
        raise Failure(str(exc)) from None
    print(f"versionCode of {kind} {format_version(version)}: {code}")
    write_outputs(env, {"version": format_version(version), "version-code": str(code)})


def main(env: Mapping[str, str] = os.environ, fetch: Fetch = http_get) -> int:
    operation = env.get("OPERATION", "").strip()
    try:
        if operation == "next":
            run_next(env, fetch)
        elif operation == "code":
            run_code(env)
        else:
            raise Failure(f"operation must be next or code, got {operation!r}")
    except Failure as exc:
        print(f"::error title=release-version::{escape(str(exc))}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
