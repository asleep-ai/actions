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
      - The next version is the last release raised by bump, or the exact
        version given (version), which then has to be higher than the last
        release and not tagged yet.
      - A major version is a release line. The next version continues its
        line: the last release of that major anywhere in the repository has to
        be the one the commit contains, and the commit has to add something to
        it. A maintenance branch (v1.4.2 on release/1.x) and the main line
        (v2.1.0) are planned independently this way.
      - With source-branch, the commit also has to be on the branch of its
        line, in its first-parent history: release/<major>.x when that branch
        exists, otherwise the source branch. And that branch must not have
        released anything after the commit.
      The pull requests between the two are listed from the first-parent
      history, read from the commit subjects.

      With prerelease (preview), the same version is planned and checked, and
      the tag is <prefix>X.Y.0-preview.N instead, N being one above the
      previews of X.Y.0 that are tagged already. The commit has to contain
      the last of them and add something to it. Only a new minor or major
      version has previews: a patch release is refused.

code  The versionCode of a release, of a preview or of a QA build, from
      version_code.py.

qa    Plan a QA build of a branch head: its version name, its versionCode and
      the rolling pre-release it is published as.
      - Only the source branch and the maintenance branches release/<major>.x
        are built, and only a commit in their first-parent history.
      - A QA build is <version>-qa.<number>. The version is the next release
        of the line: the last release the commit contains, raised by patch.
        On the source branch it is the next major instead once the last
        release has a maintenance branch, and on a maintenance branch without
        a release of its major, <major>.0.0. next-version names a later
        version the branch is heading for; it is used while it is higher than
        the last release. The number is qa-sequence plus qa-sequence-offset.
      - version names the version of this one build: the next minor or the
        next major version, whose QA builds install over the releases that
        are out. Such a build is published apart from the rolling build of
        its branch.
      - With label, the build is a label build, <version>-<label>.<number>: a
        named test build made after the last release of its line. The version
        is the patch after that release, and the versionCode is the release's
        plus the number, which counts the label builds published after that
        release. It installs over the release, and the next patch release
        installs over it. A line without a release has no label builds.

changes
      What a commit adds to the commit a tag names (the previous build of a
      rolling QA release), as the pull request list of next. When the tag
      does not exist or is not part of the commit's history, the list starts
      at the last release the commit contains.

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

from version_code import MAX_LABEL as MAX_LABEL_BUILDS
from version_code import VersionCodeError, label_code, preview_code, qa_code, release_code

BUMPS = ("patch", "minor", "major")
KINDS = ("release", "preview", "qa")
# The kinds of prerelease a version can be tagged as before it is released. Each needs a range of
# versionCodes in version_code.py; preview is the only one that has one.
PRERELEASES = ("preview",)
# A leading zero is not a version number: v01.2.3 and v1.2.3 would be the same release.
NUMBER = r"(0|[1-9][0-9]*)"
VERSION = re.compile(rf"{NUMBER}\.{NUMBER}\.{NUMBER}")
# X.Y.Z-preview.N. The number starts at 1.
PRERELEASE = re.compile(rf"(?P<version>{NUMBER}\.{NUMBER}\.{NUMBER})-(?P<kind>[a-z]+)\.(?P<number>[1-9][0-9]*)")
# The label of a label build: it becomes part of a version name and of a tag name.
LABEL = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
MAX_LABEL = 40
# What stands in a version name where a label build has its label: 1.13.1-qa.57, 2.0.0-preview.3.
# A label build is none of them, and it is not a release.
QA_SUFFIX = "qa"
RESERVED_LABELS = (QA_SUFFIX, "release") + PRERELEASES
# The tags of the rolling pre-releases QA builds are published as. Each is an annotated tag whose
# message names the build it holds, "<version name> (<versionCode>)": the label builds after a
# release are counted from these messages.
ROLLING_TAGS = "refs/tags/qa-*"
ROLLING_MESSAGE = re.compile(r"(?P<name>\S+) \((?P<code>[1-9][0-9]*)\)")
MAINTENANCE = re.compile(r"release/(?P<major>0|[1-9][0-9]*)\.x")
# Characters git allows in a ref name and that need no quoting anywhere a tag is used. Not a leading
# hyphen: git would read such a tag as an option.
TAG_PREFIX = re.compile(r"([A-Za-z0-9._/][A-Za-z0-9._/-]*)?")
BRANCH = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]*")
SHA = re.compile(r"[0-9a-fA-F]{7,64}")
SQUASH_SUBJECT = re.compile(r"(?P<title>.*\S)\s*\(#(?P<number>[0-9]+)\)")
MERGE_SUBJECT = re.compile(r"Merge pull request #(?P<number>[0-9]+) from \S+")
# The list is read by the person who cuts the release; a release this large is summarised.
MAX_LISTED = 200
ZERO = (0, 0, 0)

# (url, token) -> body
Fetch = Callable[[str, str], bytes]


class Failure(Exception):
    """A reason the release cannot be planned."""


@dataclass(frozen=True)
class Plan:
    version: str  # X.Y.Z, or X.Y.0-preview.N for a prerelease
    tag: str
    previous_tag: str  # empty when the repository has no release tag
    target_sha: str
    prs: str  # markdown list
    version_code: int
    branch: str  # the branch of the release line; empty when no source branch was given
    previous_prerelease_tag: str = ""  # the prerelease of the same version before this one, when there is one
    notice: str = ""  # something the person who releases should read; not a refusal


@dataclass(frozen=True)
class QaPlan:
    version: str  # the version the build leads up to, X.Y.Z
    version_name: str  # <version>-qa.<number>, or <version>-<label>.<number> for a label build
    version_code: int
    kind: str  # qa or label
    number: int  # of a QA build, the sequence number; of a label build, its place after the release
    line: str  # the branch as it appears in names: develop, release-1.x
    major: str  # the major version of a maintenance branch; empty on the source branch
    release_tag: str  # the tag of the rolling pre-release the build is published as
    previous_tag: str  # the last release the commit contains
    branch: str
    target_sha: str
    why: str  # where the version comes from, for the log


@dataclass(frozen=True)
class Changes:
    since: str  # what the list starts at: a commit id, a release tag, or empty for the whole history
    prs: str
    stale: bool  # the tag names a later commit that contains this one
    unchanged: bool  # the tag names this very commit


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


def prerelease_numbers(prefix: str, version: tuple, kind: str) -> list:
    """The numbers N of the tags <prefix>X.Y.Z-<kind>.N of one version, anywhere in the repository."""
    numbers = []
    for name in git("tag", "--list").stdout.splitlines():
        match = PRERELEASE.fullmatch(name[len(prefix):]) if name.startswith(prefix) else None
        if match and match["kind"] == kind and parse_version(match["version"]) == version:
            numbers.append(int(match["number"]))
    return numbers


def contains(commit: str, ancestor: str) -> bool:
    """Whether `ancestor` is in the history of `commit`, or is that commit."""
    return git("merge-base", "--is-ancestor", ancestor, commit, check=False).returncode == 0


def is_on(commit: str, tip: str) -> bool:
    """Whether the commit is in the first-parent history of a branch tip.

    The first-parent history is what the branch itself pointed at over time:
    squash and rebase merges, and the merge commits of pull requests. A commit
    that only came in through a merge commit's second parent, the head of a
    pull request merged that way, is not in it. Its green check is the check of
    the pull request, which built the merge with the base branch, not this
    commit.
    """
    return commit in git("rev-list", "--first-parent", tip).stdout.split()


def line_branch(version: tuple, target: str, source_branch: str, what: str, tag: str) -> str:
    """The branch the release line of `version` lives on, after checking that the commit is on it.

    A major version with a branch release/<major>.x is released from that
    branch only. Every other major is released from the source branch. So a
    commit of an open pull request, of an unmerged branch or of another line's
    branch is refused, however green its own checks are, and so is the head
    commit of a pull request that was merged with a merge commit.
    """
    if not BRANCH.fullmatch(source_branch):
        raise Failure(f"source-branch is not a branch name: {source_branch!r}")
    source = commit_of(f"refs/remotes/origin/{source_branch}")
    if source is None:
        raise Failure(f"branch {source_branch} is not in this checkout")
    major = version[0]
    maintenance = f"release/{major}.x"
    maintenance_tip = commit_of(f"refs/remotes/origin/{maintenance}")
    branch, tip = (maintenance, maintenance_tip) if maintenance_tip is not None else (source_branch, source)
    if is_on(target, tip):
        return branch
    if git("merge-base", "--is-ancestor", target, tip, check=False).returncode == 0:
        raise Failure(
            f"{what} ({target[:7]}) reached {branch} only through a merge commit: it is a commit of a merged "
            f"pull request, not a commit {branch} itself pointed at, and no check built it as it is. Release "
            "the merge commit instead"
        )
    if maintenance_tip is not None:
        raise Failure(
            f"{tag} is a release of major {major}, which is released from {maintenance}, and {what} "
            f"({target[:7]}) is not on that branch. On {source_branch}, raise the major version"
        )
    # Say which line the commit belongs to, when it is on a maintenance branch of another major.
    refs = git("for-each-ref", "--format=%(refname:short)", "--contains", target, "refs/remotes/origin/release/")
    others = [name[len("origin/"):] for name in refs.stdout.split() if re.fullmatch(r"origin/release/[0-9]+\.x", name)]
    if others:
        raise Failure(
            f"{what} ({target[:7]}) is on {', '.join(others)}, and {tag} is a release of major {major}. "
            "A maintenance branch releases its own major version only"
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
    # An exact version instead of a bump: the first release of a repository that does not start at
    # 0.x, or a version that skips some. The bump is then not read.
    exact = env.get("VERSION", "").strip()
    if bool(pr) == bool(sha):
        raise Failure("give exactly one of pr and sha")
    if not exact and bump not in BUMPS:
        raise Failure(f"bump must be one of {', '.join(BUMPS)}, got {bump!r}")
    # A prerelease of the version instead of its release: the same plan, another tag.
    prerelease = env.get("PRERELEASE", "").strip()
    if prerelease and prerelease not in PRERELEASES:
        raise Failure(f"prerelease must be empty or one of {', '.join(PRERELEASES)}, got {prerelease!r}")
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
    previous_tag = prefix + format_version(previous) if previous else ""
    if exact:
        # The version may be given as its tag.
        version = parse_version(exact[len(prefix):] if prefix and exact.startswith(prefix) else exact)
        tag = prefix + format_version(version)
        if version in everything:
            raise Failure(f"version {format_version(version)} was given, and tag {tag} exists already")
        if previous and version <= previous:
            raise Failure(
                f"version {format_version(version)} was given, and {what} ({target[:7]}) builds on {previous_tag}. "
                "A release has to be higher than the last release its commit contains"
            )
    else:
        version = bump_version(previous or ZERO, bump)
        tag = prefix + format_version(version)
    if prerelease and version[2] != 0:
        # The codes of a minor version: QA builds, then the previews, then the releases. A preview of
        # a patch release would be below the release that is installed.
        how = f"version {format_version(version)} was given" if exact else f"bump {bump} plans {format_version(version)}"
        raise Failure(
            f"{how}, a patch release, and a {prerelease} is of a new minor or major version (X.Y.0): within a "
            f"minor version every {prerelease} is below every release, so a {prerelease} of a patch would not "
            f"install over the release that is out. Choose bump minor or major for the {prerelease}. To try a "
            "fix on a device before the patch is released, make a label build (the QA workflow, input label)"
        )
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
        how = "the version given is" if exact else f"{bump} gives"
        raise Failure(
            f"{what} ({target[:7]}) builds on {previous_tag or 'no release'}, and {how} {tag}, but the "
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
    if branch:
        # The commit is on the branch, so the branch contains every release the commit contains. When
        # it contains a higher one, a release was cut from this branch after the commit. The line
        # check above does not see that when the bump opens a new major, which has no release yet.
        released = release_versions(prefix, merged_into=f"refs/remotes/origin/{branch}")
        if released and max(released) != previous:
            newer = prefix + format_version(max(released))
            raise Failure(
                f"{branch} has released {newer} after {what} ({target[:7]}), which builds on "
                f"{previous_tag or 'no release'}. Release a commit that contains {newer}"
            )
    if not prerelease:
        # A preview of this version may be on devices outside the team, and the release installs
        # over it. A release cut from a commit before that preview would take away what the preview
        # had. That can be meant (the preview was a dead end), so it is said and not refused.
        notice = ""
        for kind in PRERELEASES:
            numbers = prerelease_numbers(prefix, version, kind)
            last_tag = f"{prefix}{format_version(version)}-{kind}.{max(numbers)}" if numbers else ""
            last_commit = commit_of(last_tag) if last_tag else None
            if last_commit and not contains(target, last_commit):
                notice = (
                    f"{what} ({target[:7]}) does not contain {last_tag}, the last {kind} of "
                    f"{format_version(version)}. {tag} installs over that {kind}, with code that lacks what the "
                    f"{kind} had. Release a commit that contains {last_tag}, unless leaving it out is meant"
                )
        return Plan(
            version=format_version(version),
            tag=tag,
            previous_tag=previous_tag,
            target_sha=target,
            prs=pull_requests(previous_commit, target),
            version_code=code,
            branch=branch,
            notice=notice,
        )

    # The version passed every rule of a release. Its prerelease is numbered after the ones of the
    # same version, and builds on the last of them the way a release builds on the one before it.
    existing = prerelease_numbers(prefix, version, prerelease)
    number = max(existing, default=0) + 1
    name = f"{format_version(version)}-{prerelease}.{number}"
    try:
        code = preview_code(version[0], version[1], number)
    except VersionCodeError as exc:
        raise Failure(str(exc)) from None
    since = previous_commit
    last_tag = ""
    if existing:
        last_tag = f"{prefix}{format_version(version)}-{prerelease}.{max(existing)}"
        last_commit = commit_of(last_tag)
        if last_commit is None:
            raise Failure(f"tag {last_tag} does not name a commit")
        if last_commit == target:
            raise Failure(f"{what} ({target[:7]}) is the commit of {last_tag}: there is nothing new to {prerelease}")
        if not contains(target, last_commit):
            raise Failure(
                f"{what} ({target[:7]}) does not contain {last_tag}, the last {prerelease} of "
                f"{format_version(version)}. A {prerelease} builds on the one before it: choose a commit that contains it"
            )
        since = last_commit
    return Plan(
        version=name,
        tag=prefix + name,
        previous_tag=previous_tag,
        target_sha=target,
        prs=pull_requests(since, target),
        version_code=code,
        branch=branch,
        previous_prerelease_tag=last_tag,
    )


def qa_base(
    branch: str, major: Optional[int], contained: list, everything: list, exact: str, hint: str, prefix: str
) -> tuple:
    """The version a QA build of `branch` leads up to, and where it comes from.

    `major` is the major version of a maintenance branch, None on the source
    branch. The source branch builds every major that has no maintenance
    branch; a maintenance branch builds its own.

    Returns the version, the last release the commit contains, the last
    release of the branch's own line among them (None when the line has no
    release yet) and the reason for the version.
    """
    if major is None:
        last = max(contained) if contained else None
        moved_on = last is not None and commit_of(f"refs/remotes/origin/release/{last[0]}.x") is not None
        if moved_on:
            default = (last[0] + 1, 0, 0)
            own = None
            why = f"the next major: {prefix}{format_version(last)} is the last release, and release/{last[0]}.x has its line"
        else:
            default = bump_version(last or ZERO, "patch")
            own = last
            why = f"the patch after {prefix}{format_version(last)}" if last else "no release yet"

        def refusal(version: tuple) -> str:
            if commit_of(f"refs/remotes/origin/release/{version[0]}.x") is not None:
                return f"major {version[0]} is built from release/{version[0]}.x"
            return ""
    else:
        if contained and max(contained)[0] > major:
            raise Failure(
                f"{branch} contains {prefix}{format_version(max(contained))}, a release of a later major version. "
                f"A maintenance branch holds the releases of major {major} and of the ones before it"
            )
        mine = [version for version in contained if version[0] == major]
        last = max(mine) if mine else None
        own = last
        default = bump_version(last, "patch") if last else (major, 0, 0)
        why = f"the patch after {prefix}{format_version(last)}" if last else f"the first release of major {major}"

        def refusal(version: tuple) -> str:
            return "" if version[0] == major else f"{branch} builds major {major} only"

    def usable(version: tuple) -> str:
        """Why the version cannot be the next release of this branch; empty when it can."""
        if version in everything:
            return f"{prefix}{format_version(version)} is released already"
        if last and version <= last:
            return f"it is not higher than {prefix}{format_version(last)}, the last release the commit contains"
        return refusal(version)

    def parsed(text: str) -> tuple:
        return parse_version(text[len(prefix):] if prefix and text.startswith(prefix) else text)

    if hint:
        version = parsed(hint)
        problem = usable(version)
        # The same ceiling as for version below: a line reaches its next major at most.
        if not problem and last and version > bump_version(last, "major"):
            ceiling = format_version(bump_version(last, "major"))
            problem = f"it is above {ceiling}, the next major version after {prefix}{format_version(last)}"
        if problem:
            why = f"{why}; next-version {format_version(version)} is not used: {problem}"
        else:
            default, why = version, "next-version"
    if not exact:
        return default, last, own, why

    # version is typed into a form for one run, and a build of it is signed with the release key:
    # every device that takes it refuses each lower versionCode afterwards. So it names one of the
    # two versions the line can reach after its next patch, and nothing above them.
    version = parsed(exact)
    problem = usable(version)
    if not problem and last:
        steps = (bump_version(last, "minor"), bump_version(last, "major"))
        if version not in steps:
            problem = (
                f"it is neither {format_version(steps[0])} nor {format_version(steps[1])}, the next minor and the "
                f"next major version after {prefix}{format_version(last)}. Without version, the build is one of "
                f"{format_version(default)}"
            )
    elif not problem and version > default:
        problem = (
            f"it is above {format_version(default)}, the version this branch is heading for before its first "
            "release. A later version is named in next-version, in the calling file"
        )
    if problem:
        raise Failure(f"version {format_version(version)} was given for this QA build, and {problem}")
    return version, last, own, "given as version"


def label_numbers(base: tuple) -> list:
    """The numbers of the label builds published after the release `base`.

    A rolling pre-release holds one build, and its tag says which: the publish
    job writes "<version name> (<versionCode>)" into the annotated tag it moves.
    The label builds after a release are the tags whose versionCode is in the
    ninety-nine codes after that release's, whatever their label and branch.
    A build that was planned and never published took no number.
    """
    low = release_code(*base)
    listing = git("for-each-ref", "--format=%(objecttype)%1f%(contents:subject)", ROLLING_TAGS).stdout
    numbers = []
    for line in listing.splitlines():
        kind, _, subject = line.partition("\x1f")
        match = ROLLING_MESSAGE.fullmatch(subject.strip()) if kind == "tag" else None
        if match and low < int(match["code"]) <= low + MAX_LABEL_BUILDS:
            numbers.append(int(match["code"]) - low)
    return numbers


def changes_since(target: str, since_tag: str, prefix: str) -> Changes:
    """What `target` adds to the commit `since_tag` names, or to its last release."""
    earlier = commit_of(f"refs/tags/{since_tag}") if since_tag else None
    if earlier == target:
        return Changes(since=earlier, prs="", stale=False, unchanged=True)
    if earlier and contains(target, earlier):
        return Changes(since=earlier, prs=pull_requests(earlier, target), stale=False, unchanged=False)
    if earlier and contains(earlier, target):
        return Changes(since=earlier, prs="", stale=True, unchanged=False)
    # No build before this one, or one of a history the branch no longer has.
    contained = release_versions(prefix, merged_into=target)
    if contained:
        release = prefix + format_version(max(contained))
        release_commit = commit_of(release)
        if release_commit is None:
            raise Failure(f"tag {release} does not name a commit")
        return Changes(since=release, prs=pull_requests(release_commit, target), stale=False, unchanged=False)
    return Changes(since="", prs=pull_requests(None, target), stale=False, unchanged=False)


def whole_number(text: str, name: str, signed: bool = False) -> int:
    if not re.fullmatch(r"-?[0-9]+" if signed else r"[0-9]+", text):
        raise Failure(f"{name} must be a whole number, got {text!r}")
    return int(text)


def plan_qa(env: Mapping[str, str]) -> QaPlan:
    ref = env.get("REF", "").strip()
    sha = env.get("SHA", "").strip()
    prefix = env.get("TAG_PREFIX", "")
    source_branch = env.get("SOURCE_BRANCH", "").strip()
    # Both as they were given, not stripped: the caller puts the names of the build together from
    # the same inputs, and a value that is only whitespace has to be refused here, not read as none.
    label = env.get("LABEL", "")
    exact = env.get("VERSION", "")
    if not TAG_PREFIX.fullmatch(prefix):
        raise Failure(f"tag-prefix may hold only letters, digits and . _ / -, and not start with -, got {prefix!r}")
    if not BRANCH.fullmatch(source_branch):
        raise Failure(f"source-branch is not a branch name: {source_branch!r}")

    # Only what was merged is built: the source branch and the maintenance branches.
    branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ""
    maintenance = MAINTENANCE.fullmatch(branch)
    if not branch or (branch != source_branch and not maintenance):
        raise Failure(
            f"a QA build is built from {source_branch} or from a maintenance branch release/<major>.x, and this "
            f"run was started for {ref or 'no ref'}. A QA build is signed with the release key, so it is built "
            f"from code that was merged: merge the change into {source_branch} or the maintenance branch (the "
            "push builds it), or start the run on one of them"
        )
    major = int(maintenance["major"]) if maintenance and branch != source_branch else None
    line = branch.replace("/", "-")

    if label:
        if not LABEL.fullmatch(label) or len(label) > MAX_LABEL:
            raise Failure(
                f"label must be lowercase letters and digits in segments joined by single hyphens, at most "
                f"{MAX_LABEL} characters (ble-test), got {label!r}"
            )
        if label in RESERVED_LABELS or label == line:
            raise Failure(f"label {label!r} is the name of another kind of build; choose another one")
        if exact:
            raise Failure(
                f"version ({exact}) and label ({label}) were both given. A label build is numbered right after "
                "the last release of its line and takes no version; version is for a QA build without a label"
            )

    if git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        raise Failure("the checkout is shallow, so tags and history are incomplete; check out with fetch-depth: 0")
    target = named_commit(sha)
    tip = commit_of(f"refs/remotes/origin/{branch}")
    if tip is None:
        raise Failure(f"branch {branch} is not in this checkout")
    if not is_on(target, tip):
        raise Failure(
            f"the commit ({target[:7]}) is not a commit {branch} itself pointed at: the branch was moved since this "
            "run started, or the commit came in through a merge. Start the run again for the branch as it is now"
        )

    everything = release_versions(prefix)
    contained = release_versions(prefix, merged_into=target)
    version, last, own, why = qa_base(
        branch, major, contained, everything, exact, env.get("NEXT_VERSION", "").strip(), prefix
    )

    try:
        if label:
            # A label build is a named test build of what follows a release: right above that
            # release, below its next patch. Without a release there is nothing to sit above.
            if own is None:
                raise Failure(
                    f"label {label!r} was given, and the commit ({target[:7]}) contains no release of major "
                    f"{version[0]}, the line {branch} builds. A label build is numbered right after the last "
                    f"release of its line, so it installs over that release. Before the first release, test "
                    f"with the QA builds of {branch} or with a preview"
                )
            version = bump_version(own, "patch")
            released = prefix + format_version(own)
            if version in everything:
                raise Failure(
                    f"{prefix}{format_version(version)} is released already, and the commit ({target[:7]}) does "
                    f"not contain it: its last release is {released}. A label build is made after the last "
                    "release of its line. Start the run on the branch as it is now"
                )
            number = max(label_numbers(own), default=0) + 1
            if number > MAX_LABEL_BUILDS:
                raise Failure(
                    f"{MAX_LABEL_BUILDS} label builds were published after {released}, and the versionCode after "
                    f"them is the one of {prefix}{format_version(version)}. Release {format_version(version)}: "
                    "the label builds after it are counted from 1 again"
                )
            kind = "label"
            code = label_code(*own, number)
            name = f"{format_version(version)}-{label}.{number}"
            release_tag = f"qa-{line}-{label}"
            why = f"label build {number} after {released}"
        else:
            number = whole_number(env.get("QA_SEQUENCE", "").strip(), "qa-sequence")
            number += whole_number(env.get("QA_SEQUENCE_OFFSET", "").strip() or "0", "qa-sequence-offset", signed=True)
            kind = "qa"
            code = qa_code(version[0], version[1], number)
            name = f"{format_version(version)}-{QA_SUFFIX}.{number}"
            # A build of a version that was given is in another block of versionCodes than the
            # builds of the branch. It gets a pre-release of its own, so the rolling build of the
            # branch stays one that every device with an earlier one can take.
            release_tag = f"qa-{line}" + (f"-{format_version(version)}" if exact else "")
    except VersionCodeError as exc:
        raise Failure(str(exc)) from None

    return QaPlan(
        version=format_version(version),
        version_name=name,
        version_code=code,
        kind=kind,
        number=number,
        line=line,
        major="" if major is None else str(major),
        release_tag=release_tag,
        previous_tag=prefix + format_version(last) if last else "",
        branch=branch,
        target_sha=target,
        why=why,
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
    if env.get("VERSION", "").strip():
        print(f"version {plan.version} was given: bump ({env.get('BUMP', '').strip() or 'none'}) is ignored.")
    print(f"Last release the commit contains: {plan.previous_tag or 'none'}")
    if plan.previous_prerelease_tag:
        print(f"Prerelease before this one: {plan.previous_prerelease_tag}")
    kind = "prerelease" if env.get("PRERELEASE", "").strip() else "release"
    print(f"Next {kind}: {plan.tag} (versionCode {plan.version_code}) at {plan.target_sha}")
    if plan.branch:
        print(f"Release line: {plan.branch}")
    if plan.notice:
        print(f"::warning title=release-version::{escape(plan.notice)}")
    print(plan.prs or "No commits listed.")
    write_outputs(env, {
        "version": plan.version,
        "tag": plan.tag,
        "previous-tag": plan.previous_tag,
        "previous-prerelease-tag": plan.previous_prerelease_tag,
        "target-sha": plan.target_sha,
        "prs": plan.prs,
        "version-code": str(plan.version_code),
        "branch": plan.branch,
        "notice": plan.notice,
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
    try:
        if kind == "preview":
            # The whole name, X.Y.Z-preview.N: the number is part of it.
            match = PRERELEASE.fullmatch(text)
            if not match or match["kind"] not in PRERELEASES:
                raise Failure(f"{text!r} is not a prerelease of the form X.Y.Z-{PRERELEASES[0]}.N")
            version = parse_version(match["version"])
            if version[2] != 0:
                raise Failure(
                    f"{text!r} is a {match['kind']} of a patch release, and a {match['kind']} is of a new minor or "
                    f"major version: X.Y.0-{match['kind']}.N"
                )
            name = text
            code = preview_code(version[0], version[1], int(match["number"]))
        else:
            version = parse_version(text)
            name = format_version(version)
            if kind == "release":
                code = release_code(*version)
            else:
                sequence = whole_number(env.get("QA_SEQUENCE", "").strip(), "qa-sequence")
                code = qa_code(version[0], version[1], sequence)
    except VersionCodeError as exc:
        raise Failure(str(exc)) from None
    print(f"versionCode of {kind} {name}: {code}")
    write_outputs(env, {"version": name, "version-code": str(code)})


def run_qa(env: Mapping[str, str]) -> None:
    plan = plan_qa(env)
    print(f"Last release the commit contains: {plan.previous_tag or 'none'}")
    print(f"QA build of {plan.branch} at {plan.target_sha}")
    print(f"Version: {plan.version} ({plan.why})")
    what = "label build" if plan.kind == "label" else "QA build"
    print(f"Version name: {plan.version_name} (versionCode {plan.version_code}, {what} {plan.number})")
    print(f"Published as: {plan.release_tag}")
    write_outputs(env, {
        "version": plan.version,
        "version-name": plan.version_name,
        "version-code": str(plan.version_code),
        "kind": plan.kind,
        "number": str(plan.number),
        "line": plan.line,
        "major": plan.major,
        "release-tag": plan.release_tag,
        "previous-tag": plan.previous_tag,
        "branch": plan.branch,
        "target-sha": plan.target_sha,
    })


def run_changes(env: Mapping[str, str]) -> None:
    prefix = env.get("TAG_PREFIX", "")
    since_tag = env.get("SINCE", "").strip()
    if not TAG_PREFIX.fullmatch(prefix):
        raise Failure(f"tag-prefix may hold only letters, digits and . _ / -, and not start with -, got {prefix!r}")
    if since_tag and not BRANCH.fullmatch(since_tag):
        raise Failure(f"since is not a tag name: {since_tag!r}")
    if git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        raise Failure("the checkout is shallow, so tags and history are incomplete; check out with fetch-depth: 0")
    target = named_commit(env.get("SHA", "").strip())
    changes = changes_since(target, since_tag, prefix)
    # What the tag names in this checkout, unpeeled: the tag object of an annotated tag. A caller
    # that moves the tag afterwards can tell whether it is still where this list was read from.
    named = git("rev-parse", "--verify", "--quiet", f"refs/tags/{since_tag}", check=False) if since_tag else None
    since_object = named.stdout.strip() if named is not None and named.returncode == 0 else ""
    if changes.stale:
        print(f"{since_tag} is at {changes.since[:7]}, a later commit that contains {target[:7]}.")
    elif changes.unchanged:
        print(f"{since_tag} is at {target[:7]} already.")
    else:
        print(f"Changes of {target[:7]} since {changes.since or 'the first commit'}:")
        print(changes.prs or "No commits listed.")
    write_outputs(env, {
        "target-sha": target,
        "since": changes.since,
        "since-object": since_object,
        "prs": changes.prs,
        "stale": "true" if changes.stale else "false",
        "unchanged": "true" if changes.unchanged else "false",
    })


OPERATIONS = ("next", "code", "qa", "changes")


def main(env: Mapping[str, str] = os.environ, fetch: Fetch = http_get) -> int:
    operation = env.get("OPERATION", "").strip()
    try:
        if operation == "next":
            run_next(env, fetch)
        elif operation == "code":
            run_code(env)
        elif operation == "qa":
            run_qa(env)
        elif operation == "changes":
            run_changes(env)
        else:
            raise Failure(f"operation must be one of {', '.join(OPERATIONS)}, got {operation!r}")
    except Failure as exc:
        print(f"::error title=release-version::{escape(str(exc))}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
