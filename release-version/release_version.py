"""Plan the next release of a repository whose versions are git tags.

action.yml passes every input through an environment variable, so no input is
ever interpolated into a shell command.

Operations:

next  Resolve the commit to release, find the last release it builds on,
      compute the next version and check that it can be released:
      - The commit is the merge commit of a merged pull request (pr) or, as the
        exception, a commit named directly (sha).
      - The last release is the highest <prefix>X.Y.Z among the tags the commit
        contains, compared as numbers. Tags with a suffix (v1.2.3-rc.1), a
        leading zero or another prefix are not release tags. A repository
        without any release tag counts from 0.0.0.
      - A major version is a release line. The next version continues its
        line: the last release of that major anywhere in the repository has to
        be the one the commit contains, and the commit has to add something to
        it. A maintenance branch (v1.4.2 on release/1.x) and the main line
        (v2.1.0) are planned independently this way.
      - With source-branch, the commit also has to be on the branch of its
        line: release/<major>.x when that branch exists, otherwise the source
        branch.
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
BRANCH = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]*")
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
    branch: str  # the branch of the release line; empty when no source branch was given


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


def release_versions(prefix: str, merged_into: Optional[str] = None) -> list:
    """The versions of the release tags: all of them, or those a commit contains."""
    arguments = ["tag", "--list"] + (["--merged", merged_into] if merged_into else [])
    versions = []
    for name in git(*arguments).stdout.splitlines():
        if name.startswith(prefix):
            match = VERSION.fullmatch(name[len(prefix):])
            if match:
                versions.append(tuple(int(part) for part in match.groups()))
    return versions


def is_ancestor(commit: str, of: str) -> bool:
    return git("merge-base", "--is-ancestor", commit, of, check=False).returncode == 0


def line_branch(version: tuple, target: str, source_branch: str, what: str, tag: str) -> str:
    """The branch the release line of `version` lives on, after checking that the commit is on it.

    A major version with a branch release/<major>.x is released from that
    branch only. Every other major is released from the source branch. So a
    commit of an open pull request, of an unmerged branch or of another line's
    branch is refused, however green its own checks are.
    """
    if not BRANCH.fullmatch(source_branch):
        raise Failure(f"source-branch is not a branch name: {source_branch!r}")
    source = commit_of(f"refs/remotes/origin/{source_branch}")
    if source is None:
        raise Failure(f"branch {source_branch} is not in this checkout")
    major = version[0]
    maintenance = f"release/{major}.x"
    maintenance_tip = commit_of(f"refs/remotes/origin/{maintenance}")
    if maintenance_tip is not None:
        if not is_ancestor(target, maintenance_tip):
            raise Failure(
                f"{tag} is a release of major {major}, which is released from {maintenance}, and {what} "
                f"({target[:7]}) is not on that branch. On {source_branch}, raise the major version"
            )
        return maintenance
    if is_ancestor(target, source):
        return source_branch
    # Say which line the commit belongs to, when it is on a maintenance branch of another major.
    refs = git("for-each-ref", "--format=%(refname:short)", "--contains", target, "refs/remotes/origin/release/")
    others = [name[len("origin/"):] for name in refs.stdout.split() if re.fullmatch(r"origin/release/[0-9]+\.x", name)]
    if others:
        raise Failure(
            f"{what} ({target[:7]}) is on {', '.join(others)}, and the bump gives {tag}, a release of major "
            f"{major}. A maintenance branch releases its own major version only"
        )
    raise Failure(
        f"{what} ({target[:7]}) is not on {source_branch}. A release is cut from a commit that was merged "
        f"into {source_branch} or into the release/<major>.x branch of its line"
    )


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

    source_branch = env.get("SOURCE_BRANCH", "").strip()

    target = merge_commit_of(pr, env, fetch) if pr else named_commit(sha)
    what = f"the merge commit of pull request #{pr}" if pr else "the commit"

    everything = release_versions(prefix)
    contained = release_versions(prefix, merged_into=target)
    if everything and not contained:
        highest = prefix + format_version(max(everything))
        raise Failure(
            f"{what} ({target[:7]}) contains no release tag, and the repository has releases up to {highest}. "
            "A release has to build on the one before it: merge the history of the last release into the "
            "branch that is released first"
        )
    previous = max(contained) if contained else None
    version = bump_version(previous or ZERO, bump)
    tag = prefix + format_version(version)
    previous_tag = prefix + format_version(previous) if previous else ""
    try:
        code = release_code(*version)
    except VersionCodeError as exc:
        raise Failure(str(exc)) from None

    # The line of the next version is its major. Its last release, wherever in the repository it is
    # tagged, has to be the release this commit builds on; otherwise the version would repeat one of
    # the line or fall behind it.
    line = [candidate for candidate in everything if candidate[0] == version[0]]
    if line and max(line) != previous:
        line_last = prefix + format_version(max(line))
        raise Failure(
            f"{what} ({target[:7]}) builds on {previous_tag or 'no release'}, and {bump} gives {tag}, but the "
            f"last release of major {version[0]} is {line_last}, which the commit does not contain. Release "
            f"from the branch that carries {line_last}, merge it into this branch first, or raise the major version"
        )

    previous_commit = None
    if previous_tag:
        previous_commit = commit_of(previous_tag)
        if previous_commit is None:
            raise Failure(f"tag {previous_tag} does not name a commit")
        if previous_commit == target:
            raise Failure(f"{what} ({target[:7]}) is the commit of {previous_tag}: there is nothing new to release")

    branch = line_branch(version, target, source_branch, what, tag) if source_branch else ""
    return Plan(
        version=format_version(version),
        tag=tag,
        previous_tag=previous_tag,
        target_sha=target,
        prs=pull_requests(previous_commit, target),
        version_code=code,
        branch=branch,
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
    print(f"Last release the commit contains: {plan.previous_tag or 'none'}")
    print(f"Next release: {plan.tag} (versionCode {plan.version_code}) at {plan.target_sha}")
    if plan.branch:
        print(f"Release line: {plan.branch}")
    print(plan.prs or "No commits listed.")
    write_outputs(env, {
        "version": plan.version,
        "tag": plan.tag,
        "previous-tag": plan.previous_tag,
        "target-sha": plan.target_sha,
        "prs": plan.prs,
        "version-code": str(plan.version_code),
        "branch": plan.branch,
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
