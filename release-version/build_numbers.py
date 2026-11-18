"""Build numbers: the versionCodes of the build-number scheme, reserved in the repository.

In the build-number scheme (version_code.build_code) the versionCode of a build
of the major version X is X * 100_000_000 + n, and n counts the builds of that
major. QA builds, label builds, previews and releases draw from one counter in
the order they are planned, so every build installs over every earlier build
of its major. The same commit built again by another run gets a new number.

Reserving a number. The plan of a build creates the ref
refs/build-numbers/<X>/<n> through the GitHub API. GitHub creates a ref only
when it does not exist, so of two builds that try the same n one gets it, and
the other is told that the ref exists and tries n + 1. The refs are the
counter: the highest one is the last number reserved. Nothing else is written.

The ref names a tag object whose content stands for the run: the commit that
is built, the id of the run, and the time of the commit as the tagger's. A run
that is run again (its failed jobs, or all of them) creates the very same
object, finds the ref that names it, and keeps its number. Any other run
creates another object.

Where a major starts. A major without any ref starts one above the highest
versionCode of the major that is out: the codes in the file names of the
assets of the repository's GitHub Releases and pre-releases, and the codes the
rolling QA tags name. Without any, it starts at 1. version-code-floor is a code
that is out where none of this shows it (the app built from another
repository, or before its GitHub Releases); the next number is above it too.

The safety rule. A build whose versionCode would not be above every code of its
major that is out is refused with the reason, before any number is reserved: a
device with the higher code would not take it. That is what happens when the
refs are behind what was published (a build of another scheme, one made by
hand), and when the floor is above every code of the major. A major above 20
has no codes at all.

The API is reached through a function the caller passes, so the tests run
against a stand-in.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from version_code import MAJOR_BLOCK, MAX_BUILD_NUMBER, MAX_VERSION_CODE, VersionCodeError, build_code, major_codes

REFS = "refs/build-numbers"
# The name inside the tag object of a reservation. The number is the ref's.
OBJECT_TAG = "build-number"
TAGGER = {"name": "github-actions[bot]", "email": "41898282+github-actions[bot]@users.noreply.github.com"}
# Builds that reserve at the same moment move up one each. This many taken numbers in a row are
# something else.
MAX_TRIES = 100
# The assets whose file names may hold a versionCode: APKs and app bundles.
PACKAGE = re.compile(r".+\.(apk|aab)", re.IGNORECASE)
CODE_PART = re.compile(r"[1-9][0-9]*")
NEXT_PAGE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')
OBJECT_ID = re.compile(r"[0-9a-f]{40}")

# (method, url, token, JSON body or None) -> (status, headers with lower-case names, body)
Call = Callable[[str, str, str, Optional[dict]], tuple]


class BuildNumberError(Exception):
    """No number can be reserved, or the build would not install over what is out."""


@dataclass(frozen=True)
class Shipped:
    """A versionCode that is out, and where it was found."""

    code: int
    where: str


@dataclass(frozen=True)
class BuildNumber:
    major: int
    number: int
    code: int
    why: str  # where the number comes from, for the log
    ref: str  # the ref that holds it; empty when the plan only looked
    kept: bool  # an earlier attempt of the same run reserved it


def codes_in_name(name: str) -> list:
    """The versionCodes the file name of an APK or app bundle holds.

    A versionCode is a part of the name of digits only, between two of `_`, `-`
    and the ends of the name: App_qa_2.0.0-qa.6_200000006.apk holds 200000006.
    The digits of the version name are joined by dots and are no such part.
    """
    if not PACKAGE.fullmatch(name):
        return []
    stem = name.rsplit(".", 1)[0]
    return [int(part) for part in re.split(r"[_-]", stem) if CODE_PART.fullmatch(part)]


def parse_floor(text: str) -> Optional[int]:
    text = text.strip()
    if not text:
        return None
    if not CODE_PART.fullmatch(text) or int(text) > MAX_VERSION_CODE:
        raise BuildNumberError(
            f"version-code-floor must be a versionCode, a whole number from 1 to {MAX_VERSION_CODE}, got {text!r}"
        )
    return int(text)


def codes_of(major: int) -> tuple:
    """The lowest and the highest versionCode of the major; refused when it has none."""
    try:
        return major_codes(major)
    except VersionCodeError as exc:
        raise BuildNumberError(f"the build is of major {major}, and {exc}. Nothing is built") from None


def next_number(major: int, reserved: Iterable[int], shipped: Iterable[Shipped], floor: Optional[int]) -> tuple:
    """The number the next build of the major gets, and why.

    Refused when the major has no codes, and when the build would not be above
    every code of the major that is out.
    """
    low, high = codes_of(major)
    base = major * MAJOR_BLOCK
    reserved = sorted(set(reserved))
    out = [item for item in shipped if low <= item.code <= high]
    highest = max(out, key=lambda item: item.code, default=None)
    if floor is not None and floor >= high:
        raise BuildNumberError(
            f"version-code-floor is {floor}, and no versionCode of major {major} is above it (the highest is {high}), "
            f"so no build of major {major} would install over the app that has it. Nothing is built. Build a later "
            "major version (next-version in the calling file), or check the floor: it is the highest versionCode "
            "of the app that is out where this repository's releases do not show it"
        )
    if reserved:
        number = reserved[-1] + 1
        why = f"one above {REFS}/{major}/{reserved[-1]}, the last number reserved"
    elif highest:
        number = highest.code - base + 1
        why = f"one above {highest.code}, {highest.where}: no number of major {major} was reserved before"
    else:
        number = 1
        why = f"the first build of major {major}: no number of it was reserved before, and none of its codes is out"
    if floor is not None and floor - base >= number:
        number = floor - base + 1
        why = f"one above version-code-floor {floor}"
    try:
        code = build_code(major, number)
    except VersionCodeError as exc:
        raise BuildNumberError(f"{exc}. Nothing is built") from None
    if highest and code <= highest.code:
        raise BuildNumberError(
            f"the next build number of major {major} is {number} ({why}), which is versionCode {code}, and "
            f"{highest.code} is out already: {highest.where}. A device that has it would not take this build. "
            f"The numbers reserved in {REFS}/{major}/ are behind what was published: a build of another "
            "versionCode scheme, or one made by hand. Nothing is built. To go on above it, set "
            f"version-code-floor: {highest.code} in the calling files"
        )
    return number, why


class Repository:
    """The calls to the GitHub API of one repository that build numbers make."""

    def __init__(self, api_url: str, repository: str, token: str, call: Call):
        self.base = f"{api_url.rstrip('/')}/repos/{repository}/"
        self.token = token
        self.call = call

    def _ask(self, method: str, url: str, body: Optional[dict] = None) -> tuple:
        status, headers, raw = self.call(method, url, self.token, body)
        try:
            document = json.loads(raw) if raw and raw.strip() else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            document = None
        return status, headers, document

    @staticmethod
    def _refusal(what: str, status: int, document: object, scope: str) -> BuildNumberError:
        message = document.get("message") if isinstance(document, dict) else None
        said = f": {message}" if isinstance(message, str) and message else ""
        hint = f" The job's token needs {scope}." if status in (401, 403, 404) else ""
        return BuildNumberError(f"cannot {what}: HTTP {status}{said}.{hint}")

    def _listed(self, path: str, what: str) -> list:
        url = self.base + path
        items = []
        while url:
            status, headers, document = self._ask("GET", url)
            if status != 200 or not isinstance(document, list):
                raise self._refusal(what, status, document, "contents: read")
            items.extend(item for item in document if isinstance(item, dict))
            following = NEXT_PAGE.search(headers.get("link", ""))
            url = following[1] if following else ""
            # The token goes with every page: only to the API it was meant for.
            if url and not url.startswith(self.base):
                raise BuildNumberError(f"the next page of the answer to {what} is not on {self.base}: {url}")
        return items

    def reserved(self, major: int) -> dict:
        """The numbers reserved for the major, each with the id of the object its ref names."""
        ref = re.compile(rf"{re.escape(REFS)}/{major}/([1-9][0-9]*)")
        numbers = {}
        for item in self._listed(f"git/matching-refs/build-numbers/{major}/", f"list the build numbers of major {major}"):
            match = ref.fullmatch(str(item.get("ref", "")))
            target = item.get("object")
            if match:
                numbers[int(match[1])] = target.get("sha", "") if isinstance(target, dict) else ""
        return numbers

    def shipped(self) -> list:
        """The versionCodes in the file names of the assets of the releases and pre-releases.

        A draft is not out, and its assets do not count.
        """
        out = []
        for release in self._listed("releases?per_page=100", "list the releases"):
            if release.get("draft"):
                continue
            kind = "pre-release" if release.get("prerelease") else "release"
            for asset in release.get("assets") or []:
                name = asset.get("name") if isinstance(asset, dict) else None
                if isinstance(name, str):
                    for code in codes_in_name(name):
                        out.append(Shipped(code, f"in the name of {name}, an asset of the {kind} {release.get('tag_name')}"))
        return out

    def tag_object(self, commit: str, message: str, date: str) -> str:
        """Create the tag object a reservation names, and return its id.

        The same content gives the same object, created again or not: that is
        how a run that is run again finds its number.
        """
        body = {
            "tag": OBJECT_TAG,
            "message": message,
            "object": commit,
            "type": "commit",
            "tagger": {**TAGGER, "date": date},
        }
        status, _, document = self._ask("POST", self.base + "git/tags", body)
        sha = document.get("sha") if isinstance(document, dict) else None
        if status != 201 or not isinstance(sha, str) or not OBJECT_ID.fullmatch(sha):
            raise self._refusal("create the object of a build number", status, document, "contents: write")
        return sha

    def create_ref(self, name: str, sha: str) -> bool:
        """Create the ref; False when it exists already."""
        status, _, document = self._ask("POST", self.base + "git/refs", {"ref": name, "sha": sha})
        if status == 201:
            return True
        message = document.get("message") if isinstance(document, dict) else ""
        if status == 422 and isinstance(message, str) and "already exists" in message.lower():
            return False
        raise self._refusal(f"create {name}", status, document, "contents: write")


def reserve(
    repository: Repository,
    major: int,
    commit: str,
    marker: str,
    date: str,
    seen: Iterable[Shipped],
    floor: Optional[int],
) -> BuildNumber:
    """Reserve the next number of the major for this run, or find the one an earlier attempt reserved.

    `marker` stands for the run, `date` is the time the tag object carries,
    and `seen` the codes that are out besides the assets: the rolling tags.
    """
    codes_of(major)
    own = repository.tag_object(commit, marker, date)
    reserved = repository.reserved(major)
    kept = [number for number, sha in reserved.items() if sha == own]
    if kept:
        number = max(kept)
        return BuildNumber(
            major, number, build_code(major, number), "reserved by an earlier attempt of this run, and kept",
            f"{REFS}/{major}/{number}", True,
        )
    number, why = next_number(major, reserved, [*repository.shipped(), *seen], floor)
    taken = []
    for _ in range(MAX_TRIES):
        ref = f"{REFS}/{major}/{number}"
        if repository.create_ref(ref, own):
            if taken:
                why = f"{why}; {', '.join(taken)} taken meanwhile by another build"
            return BuildNumber(major, number, build_code(major, number), why, ref, False)
        taken.append(str(number))
        number += 1
        try:
            build_code(major, number)
        except VersionCodeError as exc:
            raise BuildNumberError(f"{exc}. Nothing is built") from None
    raise BuildNumberError(
        f"{MAX_TRIES} build numbers of major {major} in a row were taken while this build tried them, up to "
        f"{number - 1}. Run the plan again"
    )


def look(repository: Repository, major: int, seen: Iterable[Shipped], floor: Optional[int]) -> BuildNumber:
    """The number the next build of the major would get now. Nothing is reserved."""
    codes_of(major)
    number, why = next_number(major, repository.reserved(major), [*repository.shipped(), *seen], floor)
    return BuildNumber(major, number, build_code(major, number), why, "", False)
