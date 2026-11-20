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
      version_code.py. With version-code-from, the build number another
      app's APK of the same GitHub Release was built with, reused for this
      app's APK (reused_number).

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
        the last release. The number is qa-sequence plus qa-sequence-offset;
        with build-number it counts the QA builds of the version instead, from
        1 and of each app apart (build_numbers.reserve_qa).
      - version names the version of this one build: the next minor or the
        next major version, whose QA builds install over the releases that
        are out, and a later one than the builds of the branch are of without
        it. Such a build is published apart from the rolling build of its
        branch.
      - With label, the build is a label build, <version>-<label>.<number>: a
        named test build made after the last release of its line. The version
        is the patch after that release, and the versionCode is the release's
        plus the number, which counts the label builds published after that
        release. It installs over the release, and the next patch release
        installs over it. A line without a release has no label builds.
      - asset-pattern names the APKs of one app, for a pre-release that holds
        a build of several apps; it is checked here and read by changes, and
        with build-number the app's QA builds are numbered apart.

changes
      What a commit adds to the commit a tag names (the previous build of a
      rolling QA release), as the pull request list of next. When the tag
      does not exist or is not part of the commit's history, the list starts
      at the last release the commit contains. With version-code, also
      whether another rolling tag holds a build of that versionCode. With
      asset-pattern, the build before is the one of that app in the tag's
      message, and the builds of the other apps are handed back to be kept.

version-code-scheme says which versionCode a build gets. minor-blocks, the
default, is the formula of version_code.py, computed from the version and the
numbers above. build-number gives every build the next build number of its
major instead (build_numbers.py): qa and code reserve it in the repository,
and next says which number the release would get now, without reserving it.
The version names are of the same form in both; only the number of a QA build
is counted another way.

Anything that cannot be decided fails the step: a release is not planned on a
guess.
"""
from __future__ import annotations

import fnmatch
import http.client
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional

import build_numbers
from build_numbers import BuildNumber, BuildNumberError, QaNumber, Shipped
from version_code import MAJOR_BLOCK, major_codes
from version_code import MAX_LABEL as MAX_LABEL_BUILDS
from version_code import VersionCodeError, check_major, label_code, preview_code, qa_code, release_code

BUMPS = ("patch", "minor", "major")
# How a build gets its versionCode: the formula of version_code.py, whose codes follow from the
# version, or the next build number of the major (build_numbers.py). The first is the default.
MINOR_BLOCKS = "minor-blocks"
BUILD_NUMBER = "build-number"
SCHEMES = (MINOR_BLOCKS, BUILD_NUMBER)
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
# A line per app below the first when the pre-release holds several apps' builds, so each app's stays known.
ROLLING_BUILD = re.compile(r"(?P<pattern>\S+) (?P<name>\S+) \((?P<code>[1-9][0-9]*)\) (?P<commit>[0-9a-f]{40})")
# The characters of an APK's name and *, ? and [...]: the pattern is written into that line and a job name.
ASSET_PATTERN = re.compile(r"[A-Za-z0-9._*?!\[\]-]+")
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
ReleaseTag = str
AssetAsTheApiListsIt = dict
VersionCodeTheApkWasBuiltWith = int
ReadCode = Callable[[ReleaseTag, AssetAsTheApiListsIt], VersionCodeTheApkWasBuiltWith]


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
    build_number: Optional[BuildNumber] = None  # build-number: the number the release would get now


@dataclass(frozen=True)
class QaPlan:
    version: str  # the version the build leads up to, X.Y.Z
    version_name: str  # <version>-qa.<number>, or <version>-<label>.<number> for a label build
    version_code: int
    kind: str  # qa or label
    number: int  # of a QA build, the sequence number or its QA number; of a label build, its place after the release
    line: str  # the branch as it appears in names: develop, release-1.x
    major: str  # the major version of a maintenance branch; empty on the source branch
    release_tag: str  # the tag of the rolling pre-release the build is published as
    previous_tag: str  # the last release the commit contains
    branch: str
    target_sha: str
    why: str  # where the version comes from, for the log
    build_number: Optional[BuildNumber] = None  # build-number: the number reserved for the build
    qa_number: Optional[QaNumber] = None  # build-number: the number reserved for the name of a QA build


@dataclass(frozen=True)
class Changes:
    since: str  # what the list starts at: a commit id, a release tag, or empty for the whole history
    prs: str
    stale: bool  # the tag names a later commit that contains this one
    unchanged: bool  # the tag names this very commit


@dataclass(frozen=True)
class Rolling:
    name: str  # the version name of the build the message names first, the one published last
    code: int
    builds: tuple = ()  # (pattern, version name, versionCode, commit) per app, when the tag names one per app


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


def http_call(method: str, url: str, token: str, body: Optional[dict]) -> tuple:
    """One request of build_numbers: the status, the headers and the body, whatever the status.

    The caller decides what a status means: a ref that exists already is an
    answer, not a failure.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "asleep-ai-actions-release-version",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(request, timeout=20) as response:
            return response.status, {name.lower(): value for name, value in response.headers.items()}, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {name.lower(): value for name, value in (exc.headers or {}).items()}, exc.read(2000)
    # No answer: build_numbers asks again before it gives up.
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise build_numbers.Unreachable(f"cannot reach {url}: {str(exc) or type(exc).__name__}") from None


def version_code_scheme(env: Mapping[str, str]) -> str:
    # Empty is the default, so that a calling file can pass an input of its own that is empty.
    scheme = env.get("VERSION_CODE_SCHEME", "").strip() or MINOR_BLOCKS
    if scheme not in SCHEMES:
        raise Failure(f"version-code-scheme must be one of {', '.join(SCHEMES)}, got {scheme!r}")
    return scheme


def checked_major(major: int) -> None:
    """Refuse a build of a major that has no versionCodes in the build-number scheme."""
    try:
        check_major(major)
    except VersionCodeError as exc:
        raise Failure(f"the build is of major {major}, and {exc}. Nothing is built") from None


def repository_api(env: Mapping[str, str], call: build_numbers.Call) -> build_numbers.Repository:
    repository = env.get("GITHUB_REPOSITORY", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repository):
        raise Failure("GITHUB_REPOSITORY is not set to owner/name")
    return build_numbers.Repository(
        env.get("GITHUB_API_URL") or "https://api.github.com", repository, env.get("TOKEN", "").strip(), call
    )


def build_number(
    env: Mapping[str, str], call: build_numbers.Call, major: int, target: str, keep: bool, kept_only: str = ""
) -> BuildNumber:
    """The build number of major `major` for a build of `target`: reserved, or with keep false only looked at.

    The codes the rolling tags name count as out next to the release assets:
    a QA build is published as the asset of a rolling pre-release, and its
    tag says which code it has whatever the file is called. With `kept_only`,
    only a number an earlier attempt of the run reserved is taken; otherwise
    the plan is refused with that reason.
    """
    api = repository_api(env, call)
    seen = [Shipped(code, f"the build {name} that {tag} holds") for tag, name, code in rolling_builds()]
    try:
        floor = build_numbers.parse_floor(env.get("VERSION_CODE_FLOOR", ""))
        if not keep:
            return build_numbers.look(api, major, seen, floor)
        marker, date = run_marker(env, target)
        return build_numbers.reserve(api, major, target, marker, date, seen, floor, kept_only)
    except BuildNumberError as exc:
        raise Failure(str(exc)) from None


def run_marker(env: Mapping[str, str], target: str) -> tuple:
    """What stands for the run in the object of a reservation, and the time the object carries.

    A run that is run again creates the same object and finds its numbers.
    Without an id, nothing is found again.
    """
    repository = env.get("GITHUB_REPOSITORY", "").strip()
    run = env.get("GITHUB_RUN_ID", "").strip()
    marker = f"The build number of run {run} of {repository}" if run else f"A build of {repository} ({secrets.token_hex(8)})"
    seconds = int(git("show", "-s", "--format=%ct", target).stdout.strip())
    return marker, datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def qa_numbers_out(version: tuple, pattern: str) -> list:
    """(k, where) of each QA build <version>-qa.<k> that the rolling tags hold.

    With `pattern`, the builds of that app: its lines, and the one build of a
    tag from before the pattern, which may be this app's. Without it, every
    build of every tag: the version has one counter.
    """
    name = re.compile(rf"{re.escape(format_version(version))}-{QA_SUFFIX}\.(?P<number>[1-9][0-9]*)")
    found = []
    for tag, held in rolling_tags().items():
        if not held.builds:
            builds = [held.name]
        elif pattern:
            builds = [built for app, built, _, _ in held.builds if app == pattern]
        else:
            builds = [held.name, *(built for _, built, _, _ in held.builds)]
        for built in builds:
            match = name.fullmatch(built)
            if match:
                found.append((int(match["number"]), f"the build {built} that {tag} holds"))
    return found


def qa_number(env: Mapping[str, str], call: build_numbers.Call, version: tuple, target: str, pattern: str) -> QaNumber:
    """N of the QA build <version>-qa.<N> with build numbers, reserved for this run.

    It counts the QA builds of the version from 1, those of the app of
    asset-pattern apart, above the ones that are out. qa-sequence is not read:
    the run number goes on across versions, so a version's first build would
    not be 1.
    """
    api = repository_api(env, call)
    marker, date = run_marker(env, target)
    try:
        return build_numbers.reserve_qa(api, format_version(version), pattern, target, marker, date, qa_numbers_out(version, pattern))
    except BuildNumberError as exc:
        raise Failure(str(exc)) from None


def release_order(text: str) -> Optional[tuple]:
    """Where a release or preview version stands among the others: X.Y.0-preview.N below X.Y.0.

    None for anything else.
    """
    match = VERSION.fullmatch(text)
    if match:
        return (*(int(part) for part in match.groups()), 1, 0)
    match = PRERELEASE.fullmatch(text)
    if match and match["kind"] in PRERELEASES:
        return (*parse_version(match["version"]), 0, int(match["number"]))
    return None


def kept_only_for_the_app(
    env: Mapping[str, str], call: build_numbers.Call, prefix: str, name: str, major: int, pattern: str
) -> str:
    """Why a build of a released tag may only keep a number, for the app of asset-pattern; empty when it may take one.

    A repository may build several apps, each with an applicationId of its own, into one GitHub
    Release, one app per run. Their versionCodes keep no order with each other, so the tag is out
    for this app only when an asset of its release matches the pattern. Otherwise the build is the
    app's first for the tag and takes the next number of the major, unless a later release or
    preview of the major holds the app: the new build would be above it, and a device that took the
    new build would refuse that later version.
    """
    tag = prefix + name
    order = release_order(name)
    try:
        releases = repository_api(env, call).published()
    except BuildNumberError as exc:
        raise Failure(str(exc)) from None
    own, later = [], []
    for release in releases:
        tag_name = str(release.get("tag_name") or "")
        names = [asset.get("name") for asset in release.get("assets") or [] if isinstance(asset, dict)]
        matching = sorted(item for item in names if isinstance(item, str) and fnmatch.fnmatchcase(item, pattern))
        if not matching:
            continue
        theirs = release_order(tag_name[len(prefix):]) if tag_name.startswith(prefix) else None
        if tag_name == tag:
            own = matching
        elif theirs and theirs[0] == major and theirs > order:
            later.append((theirs, tag_name, matching[0]))
    if own:
        return (
            f"{tag} has a GitHub Release with {own[0]} already, an APK that asset-pattern {pattern} matches, and no "
            f"attempt of this run reserved a build number for it. With {BUILD_NUMBER} an APK that is out is built again "
            "only by running its own run again (Re-run failed jobs or Re-run all jobs on the run that built it): a new "
            f"run would take the next number of major {major}, above every build since, a later release included, and "
            "a device that took it would refuse those. Nothing is built"
        )
    if later:
        _, later_tag, asset = min(later)
        return (
            f"{tag} has a GitHub Release without an APK that asset-pattern {pattern} matches, and {later_tag}, a later "
            f"version of major {major}, holds {asset}. The first build of that app for {tag} would take the next "
            f"number of major {major}, above {asset}, and a device that took it would refuse {later_tag}. Nothing is "
            "built. An app is added to the GitHub Release of a version only while no later release or preview of its "
            "major holds it"
        )
    print(
        f"{tag} has a GitHub Release, and none of its assets matches asset-pattern {pattern}: this build is the "
        "first of that app for the tag, and takes a build number of its own."
    )
    return ""


def apk_reader(env: Mapping[str, str]) -> ReadCode:
    """Read the versionCode of a release asset from the APK itself: download it, and ask aapt2.

    The name of an asset need not hold the versionCode (sleephub-android's do
    not), and nothing else GitHub keeps says which build an asset is. The build
    job read the same value with the same tool before the APK was published,
    and refused an APK without the planned one (android-release-build.yml). The
    token goes with the request to the API only: GitHub answers with the
    address of the file, on another host, which is asked without it.
    """
    api = (env.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    repository = env.get("GITHUB_REPOSITORY", "").strip()
    token = env.get("TOKEN", "").strip()
    sdk = env.get("ANDROID_HOME", "").strip() or env.get("ANDROID_SDK_ROOT", "").strip()

    def read(tag: str, asset: dict) -> int:
        name = str(asset.get("name"))
        identifier = asset.get("id")
        if not isinstance(identifier, int) or isinstance(identifier, bool):
            raise Failure(f"{name} of {tag} is listed without an id, and cannot be downloaded to read its versionCode")
        found = [path for path in Path(sdk, "build-tools").glob("*/aapt2") if path.is_file() and os.access(path, os.X_OK)] if sdk else []
        if not found:
            raise Failure(
                "aapt2 was not found under the build-tools of ANDROID_HOME or ANDROID_SDK_ROOT: the versionCode of an "
                "APK of the release is read with it. GitHub-hosted Ubuntu runners have it"
            )
        aapt2 = max(found, key=lambda path: [int(part) for part in re.findall(r"[0-9]+", path.parent.name)])
        request = urllib.request.Request(
            f"{api}/repos/{repository}/releases/assets/{identifier}",
            headers={
                "Accept": "application/octet-stream",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "asleep-ai-actions-release-version",
            },
        )
        if token:
            request.add_unredirected_header("Authorization", f"Bearer {token}")
        with tempfile.TemporaryDirectory(prefix="release-version-") as directory:
            path = os.path.join(directory, "asset.apk")
            try:
                with urllib.request.urlopen(request, timeout=60) as response, open(path, "wb") as out:
                    shutil.copyfileobj(response, out, 1 << 20)
            except urllib.error.HTTPError as exc:
                raise Failure(f"cannot download {name} of {tag} to read its versionCode: HTTP {exc.code}") from None
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                raise Failure(f"cannot download {name} of {tag} to read its versionCode: {str(exc) or type(exc).__name__}") from None
            badging = subprocess.run([str(aapt2), "dump", "badging", path], capture_output=True, text=True, check=False)
        line = next((line for line in badging.stdout.splitlines() if line.startswith("package: ")), "")
        match = re.search(r"\bversionCode='([1-9][0-9]{0,9})'", line)
        if badging.returncode != 0 or not match:
            said = (badging.stderr.strip() or line or "no package line")[:300]
            raise Failure(f"aapt2 of build-tools {aapt2.parent.name} cannot read the versionCode of {name} of {tag}: {said}")
        print(f"{name} of {tag}: versionCode {match[1]}, read from the APK with aapt2 of build-tools {aapt2.parent.name}.")
        return int(match[1])

    return read


def checked_reuse(scheme: str, kind: str, pattern: str, source: str) -> None:
    """Refuse version-code-from in a run it does not fit, before anything is asked or reserved."""
    if scheme != BUILD_NUMBER:
        raise Failure(
            f"version-code-from is {source}, and version-code-scheme is {scheme}: with the formula the versionCode "
            "follows from the tag, the same in every run and for every app, and there is no build number to reuse. "
            f"version-code-from is for version-code-scheme: {BUILD_NUMBER} only. Nothing is built"
        )
    if kind not in ("release", "preview"):
        raise Failure(f"version-code-from is for the build of a release or a preview tag, and kind is {kind}")
    if not pattern:
        raise Failure(
            f"version-code-from is {source}, and asset-pattern is empty: the number of another app's APK is reused "
            "by a run that adds one app to a release of several, and asset-pattern names the APKs this run builds. "
            "Pass it. Nothing is built"
        )
    for name, value in (("version-code-from", source), ("asset-pattern", pattern)):
        if not ASSET_PATTERN.fullmatch(value):
            raise Failure(
                f"{name} is {value!r}. It is the file name pattern of the APKs of one app, such as SleepHub_*.apk: "
                "letters, digits, . _ - and the wildcards * ? [ ] !, without whitespace"
            )
    if source == pattern:
        raise Failure(
            f"version-code-from and asset-pattern are both {source}: the number is reused from the APK of another app "
            "of the release, not from the app this run builds. Nothing is built"
        )


def reused_number(
    env: Mapping[str, str],
    call: build_numbers.Call,
    read: ReadCode,
    prefix: str,
    name: str,
    major: int,
    target: str,
    pattern: str,
    source: str,
    exists: str = "",
) -> Optional[BuildNumber]:
    """The build number of the APKs of another app of the release (`source`), for the first APK of this app (`pattern`).

    A release that went out with one app gets another later with the same
    versionCode. The number is the one in the APKs of the release that
    `source` matches, read from the files themselves, and it has to be a
    number of the major that the build of this very commit reserved:
    refs/build-numbers/<major>/<n> names the tagged commit. Nothing is
    reserved, because that ref exists already and every later build is above
    it. The run records that it reused the number
    (build_numbers.record_reuse), so that the run, run again, is told apart
    from a new run of the app.

    A run that is run again keeps what its first attempt took, and that is
    looked at first, before what the release holds. An earlier attempt that
    reused a number: the run builds with that number again or not at all. It
    is checked against the APKs of `source` the release holds now and against
    the APKs of the app that are out, whichever run published them; with
    neither, or another code in either, the run is refused, with how to go on.
    A number of its own would give the release two versionCodes once the APK
    of `source` is back, and an APK of the app with another code, which a new
    run published while the APK of `source` was gone, would be replaced with
    a lower one. An earlier attempt that reserved a number of its own
    (the run that lent its number to the other app, or one that published
    nothing before the other app was released): None, and the run keeps that
    number on the path without version-code-from.

    Otherwise None when there is no number to reuse, and the run takes a build
    number of its own as without version-code-from: the tag has no published
    release (`exists` false, or none in the list), or no APK of the release
    matches `source`. Once an APK of `source` is found, the run gets its
    number or is refused, never a number of its own. It is refused when the
    release holds an APK of the app already (as with kept_only_for_the_app),
    before any APK is downloaded, and when the number is not one to reuse: the
    APKs of `source` disagree, or their code is of the formula, of another
    major, or of a ref that is gone or names another commit.

    What else of the app is out is not compared. The app's builds that are
    above the number came after the release that lends it: a QA build of the
    next version (X.Y.Z+1-qa.N after vX.Y.Z), or a later release or preview.
    Each of them is of a later version than this one, and a device that has
    one is not meant to take this release. So the run, and the run run again,
    builds with the number whatever went out since.
    """
    tag = prefix + name
    low, high = major_codes(major)
    try:
        api = repository_api(env, call)
        marker, date = run_marker(env, target)
        own = api.tag_object(target, marker, date)
        runs = api.reuses(major)
        reserved = api.reserved(major)
        reused_before, record = build_numbers.reuse_of_the_run(runs, major, pattern, own)
        the_plan_found_no_published_release = exists == "false"
        releases = [] if the_plan_found_no_published_release else api.published()
        own_release = next((release for release in releases if str(release.get("tag_name") or "") == tag), None)

        def assets_of(which: str) -> list:
            return [
                asset for asset in (own_release or {}).get("assets") or []
                if isinstance(asset, dict) and isinstance(asset.get("name"), str) and fnmatch.fnmatchcase(asset["name"], which)
            ]

        def held() -> str:
            return ", ".join(sorted(str(asset.get("name")) for asset in (own_release or {}).get("assets") or [] if isinstance(asset, dict))) or "none"

        def read_codes(assets: list) -> dict:
            return {asset["name"]: read(tag, asset) for asset in assets}

        def checked(code: int, names: str) -> int:
            """The build number of `code`, a number of the major that the build of the tagged commit reserved."""
            if not low <= code <= high:
                raise Failure(
                    f"{names} of {tag} has versionCode {code}, which is no build number of major {major} ({low} to {high}): "
                    f"it was built with the formula, or for another major. Only a number of {build_numbers.REFS}/{major}/ is "
                    "reused. Nothing is built"
                )
            number = code - major * MAJOR_BLOCK
            ref = f"{build_numbers.REFS}/{major}/{number}"
            if number not in reserved:
                raise Failure(
                    f"{names} of {tag} has versionCode {code}, build number {number} of major {major}, and {ref} does "
                    "not exist: no build of this repository reserved it, or the ref was deleted. A number is reused only "
                    "while its ref keeps every later build above it. Nothing is built"
                )
            reserved_for = api.tag_target(reserved[number], ref)
            if reserved_for != target:
                raise Failure(
                    f"{names} of {tag} has versionCode {code}, and {ref} was reserved for the commit "
                    f"{reserved_for[:7]}, not for {target[:7]}, the commit of {tag}. Nothing is built"
                )
            print(f"{names} of {tag}: versionCode {code}, build number {number} of major {major}, reserved as {ref} for {target[:7]}.")
            return number

        lending = assets_of(source)
        overlap = sorted(asset["name"] for asset in lending if fnmatch.fnmatchcase(asset["name"], pattern))
        if overlap:
            raise Failure(
                f"{', '.join(overlap)} of {tag} matches both version-code-from {source} and asset-pattern {pattern}. "
                "Give each app a pattern that matches its own APKs only. Nothing is built"
            )
        of_the_app = assets_of(pattern)

        def the_number_this_run_reused_again() -> BuildNumber:
            code = major * MAJOR_BLOCK + reused_before
            said = (
                f"An earlier attempt of this run reused build number {reused_before} of major {major} (versionCode "
                f"{code}) for {pattern}, recorded as {record}"
            )
            restore = (
                f"The run builds with that number or not at all: with a number of its own, {tag} would hold two "
                f"versionCodes once an APK of {source} is back. Nothing is built. Put the APK of {source} with "
                f"versionCode {code} back on the GitHub Release of {tag} by running again the run that built it "
                "(Re-run failed jobs or Re-run all jobs), then run this run again. Do that before any new run of "
                f"{pattern} for {tag}: while no APK of {source} is there, a new run has no number to reuse and takes "
                "one of its own"
            )
            if own_release is None:
                raise Failure(f"{said}, and {tag} has no published GitHub Release now (a draft is not out). {restore}")
            if not lending and not of_the_app:
                raise Failure(
                    f"{said}, and the GitHub Release of {tag} holds no APK that version-code-from {source} or "
                    f"asset-pattern {pattern} matches now (its assets: {held()}). {restore}"
                )
            codes = read_codes(lending + of_the_app)
            names = ", ".join(sorted(codes))

            def with_another_code(assets: list) -> str:
                return ", ".join(f"{name} ({codes[name]})" for name in sorted(asset["name"] for asset in assets) if codes[name] != code)

            replaced = with_another_code(of_the_app)
            if replaced:
                raise Failure(
                    f"{said}, and {replaced} of {tag}, an APK that asset-pattern {pattern} matches, is out with another "
                    f"versionCode: another run published it. Built with {code}, it would be replaced (gh release upload "
                    "--clobber), and a device that took it does not take a lower versionCode. Nothing is built. To build "
                    f"{pattern} of {tag} again, run again the run that built it (Re-run failed jobs or Re-run all jobs on "
                    "that run), not this one"
                )
            lent_another = with_another_code(lending)
            if lent_another:
                again = (
                    f"{pattern} of {tag} is out with {code} already: this run is not needed, and a new run is refused "
                    "while that APK is there"
                    if of_the_app else
                    f"To build {pattern} with the number the release holds now, start a new run for {tag} instead of "
                    "running this one again: a new run reuses it"
                )
                raise Failure(
                    f"{said}, and {lent_another} of {tag} has another versionCode now. The run builds with its number "
                    f"or not at all, and with it the release would hold two versionCodes. Nothing is built. {again}"
                )
            if not lending:
                print(
                    f"{tag} holds no APK that version-code-from {source} matches now, and {names}, which this run "
                    f"published, has versionCode {code}: the run builds with that number again. Put the APK of "
                    f"{source} back on the release with versionCode {code}, by running again the run that built it."
                )
            checked(code, names)
            why = f"the number {names} of {tag} was built with ({build_numbers.REFS}/{major}/{reused_before}), reused for {pattern}"
            return BuildNumber(major, reused_before, code, why, record, True)

        if reused_before:
            return the_number_this_run_reused_again()

        reserved_by_an_earlier_attempt = sorted(number for number, sha in reserved.items() if sha == own)
        if reserved_by_an_earlier_attempt:
            print(
                f"An earlier attempt of this run reserved {build_numbers.REFS}/{major}/{reserved_by_an_earlier_attempt[-1]} "
                f"for {pattern}: the run keeps that number, and does not reuse the one of {source}."
            )
            return None
        if own_release is None:
            print(
                f"{tag} has no published GitHub Release (a draft is not out), so no APK of {source} lends its build "
                f"number: {pattern} takes a build number of its own."
            )
            return None
        if not lending:
            print(
                f"{tag} has a GitHub Release, and none of its assets matches version-code-from {source} (its assets: "
                f"{held()}): {pattern} takes a build number of its own."
            )
            return None
        if of_the_app:
            raise Failure(
                f"{tag} has a GitHub Release with {sorted(asset['name'] for asset in of_the_app)[0]} already, an APK that "
                f"asset-pattern {pattern} matches, and no attempt of this run reused a build number for it. An APK that "
                "is out is built again only by running its own run again (Re-run failed jobs or Re-run all jobs on the "
                "run that built it). Nothing is built"
            )
        codes = read_codes(lending)
        names = ", ".join(sorted(codes))
        if len(set(codes.values())) > 1:
            listed = ", ".join(f"{item} ({codes[item]})" for item in sorted(codes))
            raise Failure(
                f"the APKs of {tag} that version-code-from {source} matches were built with different versionCodes: "
                f"{listed}. There is no one number to reuse. Nothing is built"
            )
        code = next(iter(codes.values()))
        number = checked(code, names)
        ref = build_numbers.record_reuse(api, runs, major, number, pattern, own)
    except BuildNumberError as exc:
        raise Failure(str(exc)) from None
    why = f"the number {names} of {tag} was built with ({build_numbers.REFS}/{major}/{number}), reused for {pattern}"
    return BuildNumber(major, number, code, why, ref, False)


def formula_guard(env: Mapping[str, str], call: build_numbers.Call, major: int) -> None:
    """Refuse a versionCode of the formula for a major that has build numbers.

    The scheme is the calling file's, and the calling file is the one of the
    commit that is built: qa.yml of the branch that was pushed, release.yml
    of the tagged commit. A branch or a commit from before the switch would
    still build with the formula, and its codes do not keep one order with
    build numbers. One read: a repository that never switched has no ref, and
    builds as before. When GitHub cannot be asked, the plan goes on with a
    warning, as it did before the question existed.
    """
    if env.get("VERSION_CODE_FLOOR", "").strip():
        print(
            "::warning title=release-version::version-code-floor is given, and version-code-scheme is "
            f"{MINOR_BLOCKS}: the floor is for build numbers, and is not read. Set version-code-scheme: "
            f"{BUILD_NUMBER} in the calling file, or take the floor out"
        )
    try:
        reserved = repository_api(env, call).reserved(major)
    except (Failure, BuildNumberError) as exc:
        print(
            f"::warning title=release-version::Could not ask whether major {major} has build numbers ({exc}). "
            "The plan goes on with the formula"
        )
        return
    if reserved:
        raise Failure(
            f"version-code-scheme is {MINOR_BLOCKS} here, and {build_numbers.REFS}/{major}/ holds build numbers "
            f"(up to {max(reserved)}): the builds of major {major} are numbered with {BUILD_NUMBER}, and a code of "
            "the formula keeps no order with them. Nothing is built. The scheme comes from the calling file of the "
            f"commit that is built: pass version-code-scheme: {BUILD_NUMBER} in qa.yml, release.yml and "
            "version-bump.yml of every branch that is built, and do not release a commit from before the switch"
        )


def print_qa_number(number: QaNumber) -> None:
    of = f"{number.version} of {number.pattern}" if number.pattern else number.version
    if number.kept:
        print(f"QA number {number.number} of {of} ({number.ref}): {number.why}.")
    else:
        print(f"QA number {number.number} of {of}, reserved as {number.ref}: {number.why}.")


def print_reused_number(number: BuildNumber, pattern: str) -> None:
    if number.notice:
        print(f"::notice title=release-version::{escape(number.notice)}")
    if number.kept:
        print(f"Build number {number.number} of major {number.major} ({number.ref}): reused for {pattern} by an earlier attempt of this run, and kept.")
    else:
        print(
            f"Build number {number.number} of major {number.major}, reused and recorded as {number.ref}: {number.why}. "
            "No number is reserved."
        )


def print_build_number(number: BuildNumber) -> None:
    if number.notice:
        print(f"::notice title=release-version::{escape(number.notice)}")
    if number.kept:
        print(f"Build number {number.number} of major {number.major} ({number.ref}): {number.why}.")
    elif number.ref:
        print(f"Build number {number.number} of major {number.major}, reserved as {number.ref}: {number.why}.")
    else:
        print(
            f"Build number {number.number} of major {number.major} if the build were planned now: {number.why}. "
            "Nothing is reserved: the build reserves its own."
        )


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


def plan_next(env: Mapping[str, str], fetch: Fetch, call: build_numbers.Call = http_call) -> Plan:
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
    scheme = version_code_scheme(env)

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
    # The formula holds a version only within its ranges. A build number holds any version of a
    # major that has codes; its number is looked up once the plan has passed every rule.
    code = 0
    if scheme == MINOR_BLOCKS:
        try:
            code = release_code(*version)
        except VersionCodeError as exc:
            raise Failure(str(exc)) from None
    else:
        checked_major(version[0])

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
        # Not reserved: the release workflow reserves the number when it builds the tag. What the
        # plan shows is the number it would get now, and the plan is refused where the build would be.
        looked = build_number(env, call, version[0], target, keep=False) if scheme == BUILD_NUMBER else None
        if scheme == MINOR_BLOCKS:
            formula_guard(env, call, version[0])
        return Plan(
            version=format_version(version),
            tag=tag,
            previous_tag=previous_tag,
            target_sha=target,
            prs=pull_requests(previous_commit, target),
            version_code=looked.code if looked else code,
            branch=branch,
            notice=notice,
            build_number=looked,
        )

    # The version passed every rule of a release. Its prerelease is numbered after the ones of the
    # same version, and builds on the last of them the way a release builds on the one before it.
    existing = prerelease_numbers(prefix, version, prerelease)
    number = max(existing, default=0) + 1
    name = f"{format_version(version)}-{prerelease}.{number}"
    if scheme == MINOR_BLOCKS:
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
    looked = build_number(env, call, version[0], target, keep=False) if scheme == BUILD_NUMBER else None
    if scheme == MINOR_BLOCKS:
        formula_guard(env, call, version[0])
    return Plan(
        version=name,
        tag=prefix + name,
        previous_tag=previous_tag,
        target_sha=target,
        prs=pull_requests(since, target),
        version_code=looked.code if looked else code,
        branch=branch,
        previous_prerelease_tag=last_tag,
        build_number=looked,
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
    #
    # And nothing at or below the version the builds of the branch are of without it. The same
    # version would be a second build under the name and the versionCode of the rolling one, in
    # another pre-release; a lower one is in a block of codes the branch has left behind.
    version = parsed(exact)
    problem = usable(version)
    if not problem and version <= default:
        heading = "that very version" if version == default else f"{format_version(default)}, a later version,"
        problem = f"the QA builds of {branch} lead up to {heading} without it ({why}). Run without version"
    elif not problem and last:
        steps = (bump_version(last, "minor"), bump_version(last, "major"))
        if version not in steps:
            problem = (
                f"it is neither {format_version(steps[0])} nor {format_version(steps[1])}, the next minor and the "
                f"next major version after {prefix}{format_version(last)}. Without version, the build is one of "
                f"{format_version(default)}"
            )
    elif not problem:
        # Before a first release there is one version to build: the one the branch is heading for.
        problem = (
            f"it is above {format_version(default)}, the version this branch is heading for before its first "
            "release. A later version is named in next-version, in the calling file"
        )
    if problem:
        raise Failure(f"version {format_version(version)} was given for this QA build, and {problem}")
    return version, last, own, "given as version"


def rolling_tags(refs: str = ROLLING_TAGS) -> dict:
    """What the messages of the rolling tags among `refs` say, by tag.

    A rolling pre-release holds one build, and its tag says which: the publish
    job writes "<version name> (<versionCode>)" into the annotated tag it moves.
    One that holds a build per app (asset-pattern) has a line per app below,
    "<pattern> <version name> (<versionCode>) <commit>". A tag without such a
    message names no build.
    """
    listing = git(
        "for-each-ref", "--format=%(refname:strip=2)%1f%(objecttype)%1f%(contents:subject)%1f%(contents:body)%1e", refs
    )
    tags = {}
    for record in listing.stdout.split("\x1e"):
        tag, kind, subject, body = (record.lstrip("\n").split("\x1f") + ["", "", ""])[:4]
        match = ROLLING_MESSAGE.fullmatch(subject.strip()) if tag and kind == "tag" else None
        if not match:
            continue
        lines = (ROLLING_BUILD.fullmatch(line.strip()) for line in body.splitlines())
        builds = tuple((line["pattern"], line["name"], int(line["code"]), line["commit"]) for line in lines if line)
        tags[tag] = Rolling(match["name"], int(match["code"]), builds)
    return tags


def rolling_builds() -> list:
    """The builds the rolling pre-releases hold, as (tag, version name, versionCode): every app's."""
    builds = []
    for tag, held in rolling_tags().items():
        builds.extend((tag, name, code) for _, name, code, _ in held.builds or [("", held.name, held.code, "")])
    return builds


def checked_asset_pattern(env: Mapping[str, str]) -> str:
    """asset-pattern as it was given, or a Failure: it is written as it is into a tag message and a job name."""
    pattern = env.get("ASSET_PATTERN", "")
    if pattern and not ASSET_PATTERN.fullmatch(pattern):
        raise Failure(
            f"asset-pattern is {pattern!r}. It is the file name pattern of the APKs of one app, such as "
            "SweetSleep_*.apk: letters, digits, . _ - and the wildcards * ? [ ] !, without whitespace"
        )
    return pattern


def label_numbers(base: tuple, scheme: str = MINOR_BLOCKS) -> list:
    """The numbers of the label builds published after the release `base`.

    They are the rolling tags of the label builds after that release, whatever
    their label and branch. A build that was planned and never published took
    no number. In the formula these are the tags whose versionCode is in the
    ninety-nine codes after that release's. A build number says nothing about
    the release, so in that scheme they are the tags whose version name is a
    label build's of the patch after it, <next patch>-<label>.<k>: the name of
    a label build made under the formula as well.
    """
    if scheme == MINOR_BLOCKS:
        low = release_code(*base)
        return [code - low for _, _, code in rolling_builds() if low < code <= low + MAX_LABEL_BUILDS]
    name = re.compile(rf"{re.escape(format_version(bump_version(base, 'patch')))}-(?P<label>[a-z0-9-]+)\.(?P<number>[1-9][0-9]*)")
    numbers = []
    for _, built, _ in rolling_builds():
        match = name.fullmatch(built)
        if match and LABEL.fullmatch(match["label"]) and match["label"] not in RESERVED_LABELS:
            numbers.append(int(match["number"]))
    return numbers


def changes_since(target: str, earlier: Optional[str], prefix: str) -> Changes:
    """What `target` adds to `earlier`, the commit of the build before, or to its last release."""
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


def plan_qa(env: Mapping[str, str], call: build_numbers.Call = http_call) -> QaPlan:
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
    scheme = version_code_scheme(env)
    pattern = checked_asset_pattern(env)

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
            number = max(label_numbers(own, scheme), default=0) + 1
            # A build number has no room to run out of after a release.
            if scheme == MINOR_BLOCKS and number > MAX_LABEL_BUILDS:
                raise Failure(
                    f"{MAX_LABEL_BUILDS} label builds were published after {released}, and the versionCode after "
                    f"them is the one of {prefix}{format_version(version)}. Release {format_version(version)}: "
                    "the label builds after it are counted from 1 again"
                )
            kind = "label"
            code = label_code(*own, number) if scheme == MINOR_BLOCKS else 0
            name = f"{format_version(version)}-{label}.{number}"
            release_tag = f"qa-{line}-{label}"
            why = f"label build {number} after {released}"
        else:
            kind = "qa"
            if scheme == MINOR_BLOCKS:
                number = whole_number(env.get("QA_SEQUENCE", "").strip(), "qa-sequence")
                number += whole_number(env.get("QA_SEQUENCE_OFFSET", "").strip() or "0", "qa-sequence-offset", signed=True)
                code = qa_code(version[0], version[1], number)
            else:
                # Reserved below, once the plan has passed every rule.
                number, code = 0, 0
                offset = env.get("QA_SEQUENCE_OFFSET", "").strip()
                if offset and not re.fullmatch(r"-?0+", offset):
                    print(
                        f"::warning title=release-version::qa-sequence-offset is {escape(offset)}, and version-code-scheme "
                        f"is {BUILD_NUMBER}: the number of a QA build counts the QA builds of its version, and neither "
                        "qa-sequence nor the offset is read. Take sequence-offset out of the calling file"
                    )
            # A build of a version that was given is in another block of versionCodes than the
            # builds of the branch. It gets a pre-release of its own, so the rolling build of the
            # branch stays one that every device with an earlier one can take.
            release_tag = f"qa-{line}" + (f"-{format_version(version)}" if exact else "")
    except VersionCodeError as exc:
        raise Failure(str(exc)) from None

    # Reserved last, once the plan has passed every rule: a run that is refused takes no number.
    reserved = build_number(env, call, version[0], target, keep=True) if scheme == BUILD_NUMBER else None
    # After the build number, which may still refuse the build: a refused build leaves no gap in the names.
    counted = qa_number(env, call, version, target, pattern) if reserved and kind == "qa" else None
    if counted:
        number = counted.number
    if kind == "qa":
        name = f"{format_version(version)}-{QA_SUFFIX}.{number}"
    if scheme == MINOR_BLOCKS:
        formula_guard(env, call, version[0])
    return QaPlan(
        version=format_version(version),
        version_name=name,
        version_code=reserved.code if reserved else code,
        kind=kind,
        number=number,
        line=line,
        major="" if major is None else str(major),
        release_tag=release_tag,
        previous_tag=prefix + format_version(last) if last else "",
        branch=branch,
        target_sha=target,
        why=why,
        build_number=reserved,
        qa_number=counted,
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


def scheme_outputs(env: Mapping[str, str], number: Optional[BuildNumber]) -> dict:
    """The scheme as the plan read it, and the build number when there is one.

    The scheme is the word, whatever whitespace the input had: a job after
    the plan compares this output, never the input.
    """
    return {"version-code-scheme": version_code_scheme(env), **({"build-number": str(number.number)} if number else {})}


def run_next(env: Mapping[str, str], fetch: Fetch, call: build_numbers.Call = http_call) -> None:
    plan = plan_next(env, fetch, call)
    if env.get("VERSION", "").strip():
        print(f"version {plan.version} was given: bump ({env.get('BUMP', '').strip() or 'none'}) is ignored.")
    print(f"Last release the commit contains: {plan.previous_tag or 'none'}")
    if plan.previous_prerelease_tag:
        print(f"Prerelease before this one: {plan.previous_prerelease_tag}")
    kind = "prerelease" if env.get("PRERELEASE", "").strip() else "release"
    print(f"Next {kind}: {plan.tag} (versionCode {plan.version_code}) at {plan.target_sha}")
    if plan.build_number:
        print_build_number(plan.build_number)
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
        **scheme_outputs(env, plan.build_number),
    })


def run_code(env: Mapping[str, str], call: build_numbers.Call = http_call, read: Optional[ReadCode] = None) -> None:
    text = env.get("VERSION", "").strip()
    prefix = env.get("TAG_PREFIX", "")
    kind = env.get("KIND", "").strip()
    if kind not in KINDS:
        raise Failure(f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
    scheme = version_code_scheme(env)
    exists = env.get("RELEASE_EXISTS", "").strip()
    if exists not in ("", "true", "false"):
        raise Failure(f"release-exists must be true or false, got {exists!r}")
    source = env.get("VERSION_CODE_FROM", "").strip()
    if source:
        checked_reuse(scheme, kind, env.get("ASSET_PATTERN", "").strip(), source)
    read_source_said_on_every_path = {"version-code-from": source} if source else {}
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
            if scheme == MINOR_BLOCKS:
                code = preview_code(version[0], version[1], int(match["number"]))
        else:
            version = parse_version(text)
            name = format_version(version)
            if scheme == MINOR_BLOCKS and kind == "release":
                code = release_code(*version)
            elif scheme == MINOR_BLOCKS:
                sequence = whole_number(env.get("QA_SEQUENCE", "").strip(), "qa-sequence")
                code = qa_code(version[0], version[1], sequence)
    except VersionCodeError as exc:
        raise Failure(str(exc)) from None
    reserved = None
    if scheme == BUILD_NUMBER:
        # The number of a build, whatever its kind, of the commit that is built.
        checked_major(version[0])
        sha = env.get("SHA", "").strip()
        if not sha:
            raise Failure("sha is the commit that is built, and the build-number scheme needs it to reserve a number")
        target = named_commit(sha)
        # The formula gave a tag the same code in every run, so building a released tag again was
        # safe. A new run takes the next number of the major instead: above every build since,
        # a later release included, and a device that took it would refuse those. Only the run
        # that built the release, run again, keeps its number.
        kept_only = ""
        # Stripped as the publish job of android-release.yml strips it to check the APKs it publishes.
        pattern = env.get("ASSET_PATTERN", "").strip()
        if source:
            reused = reused_number(env, call, read or apk_reader(env), prefix, name, version[0], target, pattern, source, exists)
            if reused:
                print_reused_number(reused, pattern)
                print(f"versionCode of {kind} {name}: {reused.code}, as {source}")
                write_outputs(env, {
                    "version": name, "version-code": str(reused.code), **scheme_outputs(env, reused),
                    **read_source_said_on_every_path, "version-code-reused": "true",
                })
                return
            read_source_said_on_every_path["version-code-reused"] = "false"
        if exists == "true" and pattern:
            kept_only = kept_only_for_the_app(env, call, prefix, name, version[0], pattern)
        elif exists == "true":
            kept_only = (
                f"{prefix}{name} has a GitHub Release already, and no attempt of this run reserved a build number "
                f"for it. With {BUILD_NUMBER} a release that is out is built again only by running its own run "
                "again (Re-run failed jobs or Re-run all jobs on the run that built it): a new run would take the "
                f"next number of major {version[0]}, above every build since, a later release included, and a "
                "device that took it would refuse those. Nothing is built"
            )
        reserved = build_number(env, call, version[0], target, keep=True, kept_only=kept_only)
        code = reserved.code
        print_build_number(reserved)
    else:
        formula_guard(env, call, version[0])
    print(f"versionCode of {kind} {name}: {code}")
    write_outputs(env, {"version": name, "version-code": str(code), **scheme_outputs(env, reserved), **read_source_said_on_every_path})


def run_qa(env: Mapping[str, str], call: build_numbers.Call = http_call) -> None:
    plan = plan_qa(env, call)
    print(f"Last release the commit contains: {plan.previous_tag or 'none'}")
    print(f"QA build of {plan.branch} at {plan.target_sha}")
    print(f"Version: {plan.version} ({plan.why})")
    what = "label build" if plan.kind == "label" else "QA build"
    if plan.build_number:
        print_build_number(plan.build_number)
    if plan.qa_number:
        print_qa_number(plan.qa_number)
    print(f"Version name: {plan.version_name} (versionCode {plan.version_code}, {what} {plan.number})")
    pattern = env.get("ASSET_PATTERN", "")
    apart = f", the APKs that {pattern} matches next to the builds of the other apps" if pattern else ""
    print(f"Published as: {plan.release_tag}{apart}")
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
        **scheme_outputs(env, plan.build_number),
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
    pattern = checked_asset_pattern(env)
    target = named_commit(env.get("SHA", "").strip())
    tags = rolling_tags()
    held = tags.get(since_tag)
    # The tag names the commit of whichever app's build was published last, so this app's is read from its line.
    own = next((build for build in held.builds if build[0] == pattern), None) if pattern and held else None
    if pattern:
        earlier = commit_of(own[3]) if own else None
        before = f"{own[1]} ({own[2]}) of {own[3][:7]}" if own else "none, so the list starts at the last release"
        print(f"The build of {pattern} that {since_tag or 'no tag'} holds: {before}.")
    else:
        earlier = commit_of(f"refs/tags/{since_tag}") if since_tag else None
    changes = changes_since(target, earlier, prefix)
    # What the tag names in this checkout, unpeeled: the tag object of an annotated tag. A caller
    # that moves the tag afterwards can tell whether it is still where this list was read from.
    named = git("rev-parse", "--verify", "--quiet", f"refs/tags/{since_tag}", check=False) if since_tag else None
    since_object = named.stdout.strip() if named is not None and named.returncode == 0 else ""
    # A versionCode is published once. The number of a label build was taken from these tags when
    # the build was planned, and another build may have been published under it since: the caller
    # asks again here, with the tags as they are when it is about to publish.
    #
    # The since tag itself counts unless it names this very commit: the same build published again
    # is no second build, but two runs of one label on two commits were planned with one number,
    # and the one that publishes second finds its own tag holding the code for another commit.
    # With asset-pattern that is the build of this app in the since tag; the other apps' count.
    taken_by = ""
    given = {}
    given_code = env.get("VERSION_CODE", "").strip()
    if given_code:
        code = whole_number(given_code, "version-code")
        builds = [
            (tag, line_pattern, line_code)
            for tag, rolling in tags.items()
            for line_pattern, _, line_code, _ in rolling.builds or [("", rolling.name, rolling.code, "")]
        ]

        def replaced(tag: str, line_pattern: str) -> bool:
            return tag == since_tag and (not pattern or line_pattern == pattern)

        taken_by = next(
            (tag for tag, line, number in builds if number == code and not (replaced(tag, line) and changes.unchanged)), ""
        )
        if taken_by:
            print(f"{taken_by} holds a build of the versionCode {code}.")
        # The code of the build the since tag holds now (this app's with asset-pattern). In the
        # build-number scheme a build is not published over one with a higher code: the caller compares.
        if pattern:
            given["since-code"] = str(own[2]) if own else ""
        else:
            given["since-code"] = str(held.code) if held else ""
    if pattern:
        # The builds of the other apps, which the caller writes into the tag again next to this one.
        apps = held.builds if held else ()
        kept = sorted(f"{app} {name} ({number}) {commit}" for app, name, number, commit in apps if app != pattern)
        given["kept-builds"] = "\n".join(kept)
    holds = f"{since_tag} holds the build of {pattern} of" if pattern else f"{since_tag} is at"
    if changes.stale:
        print(f"{holds} {changes.since[:7]}, a later commit that contains {target[:7]}.")
    elif changes.unchanged:
        print(f"{holds} {target[:7]} already.")
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
        "code-taken-by": taken_by,
        **given,
    })


OPERATIONS = ("next", "code", "qa", "changes")


def main(
    env: Mapping[str, str] = os.environ,
    fetch: Fetch = http_get,
    call: build_numbers.Call = http_call,
    read: Optional[ReadCode] = None,
) -> int:
    operation = env.get("OPERATION", "").strip()
    try:
        if operation in OPERATIONS and operation != "code" and env.get("VERSION_CODE_FROM", "").strip():
            raise Failure(
                f"version-code-from is for the operation code, the versionCode of a release or a preview tag, and the "
                f"operation is {operation}"
            )
        if operation == "next":
            run_next(env, fetch, call)
        elif operation == "code":
            run_code(env, call, read)
        elif operation == "qa":
            run_qa(env, call)
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
