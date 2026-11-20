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
A floor above every code of a major that has builds already is the floor of a
later major's line (one calling file builds every line), and is passed over
with a notice.

The safety rule. A build whose versionCode would not be above every code of its
major that is out is refused with the reason, before any number is reserved: a
device with the higher code would not take it. That is what happens when the
refs are behind what was published (a build of another scheme, one made by
hand), and when the floor is above every code of a major that has no build
yet. A major above 20 has no codes at all.

QA numbers. The name of a QA build, <version>-qa.<N>, has a number of its own,
which counts the QA builds of that version from 1, of each app apart when the
builds name their APKs by a pattern (asset-pattern). It is reserved the same
way, as refs/qa-numbers/<version>/<N> or refs/qa-numbers/<version>/app-<key>/<N>,
with the same object, so a run that is run again keeps its name too. The first
number of a version is one above the QA builds of it that are out already, by
the names the rolling tags hold, so no name is given twice.

A reused number. A release that holds several apps can get an app's APK later
with the number another app's APK of the same release was built with
(release_version.reused_number). That number's ref exists already, so nothing
is reserved: every later build is above it whatever happens. The run says
that it reused the number with a ref of its own,
refs/reused-numbers/<X>/<n>/app-<key>/<k>, naming the run's object, so that
the run, run again, is told apart from a new run of the same app, and builds
with that number again or not at all, whatever the release holds by then.

The API is reached through a function the caller passes, so the tests run
against a stand-in. A server error, or a request that got no answer, is made
again a few times; every request here may be.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from version_code import MAJOR_BLOCK, MAX_BUILD_NUMBER, MAX_VERSION_CODE, VersionCodeError, build_code, major_codes

REFS = "refs/build-numbers"
QA_REFS = "refs/qa-numbers"
REUSED_REFS = "refs/reused-numbers"
# What an asset pattern keeps of itself in the name of its counter: git and a URL take these as they are.
APP_KEY = re.compile(r"[A-Za-z0-9_]")
# The name inside the tag object of a reservation. The number is the ref's.
OBJECT_TAG = "build-number"
TAGGER = {"name": "github-actions[bot]", "email": "41898282+github-actions[bot]@users.noreply.github.com"}
# Builds that reserve at the same moment move up one each. This many taken numbers in a row are
# something else.
MAX_TRIES = 100
# Seconds before each new try of a request that GitHub answered with a server error, or that did
# not reach it. Every request here may be made again: a read, a tag object that is the same object
# again, and a ref that exists by then, which the reservation takes for a taken number.
RETRY_DELAYS = (1, 2, 4)
# The assets whose file names may hold a versionCode: APKs and app bundles.
PACKAGE = re.compile(r".+\.(apk|aab)", re.IGNORECASE)
CODE_PART = re.compile(r"[1-9][0-9]*")
NEXT_PAGE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')
OBJECT_ID = re.compile(r"[0-9a-f]{40}")

# (method, url, token, JSON body or None) -> (status, headers with lower-case names, body)
Call = Callable[[str, str, str, Optional[dict]], tuple]


class BuildNumberError(Exception):
    """No number can be reserved, or the build would not install over what is out."""


class Unreachable(BuildNumberError):
    """A request did not reach GitHub, or got no answer. It may be made again."""


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
    notice: str = ""  # something to say that did not stop the build


@dataclass(frozen=True)
class QaNumber:
    version: str
    pattern: str  # the asset pattern of the app; empty for the one counter of the version
    number: int
    why: str  # where the number comes from, for the log
    ref: str
    kept: bool  # an earlier attempt of the same run reserved it


def qa_counter(version: str, pattern: str = "") -> str:
    """Where the QA numbers of a version are reserved: refs/qa-numbers/<version>, or a directory of the app below it.

    The pattern becomes one part of the ref's name. Every byte but a letter, a
    digit and _ is written as - and two hex digits, so the part holds nothing
    git refuses in a ref (* ? [ .. .lock) or a URL has to quote, and no two
    patterns give the same part: SleepHub_*.apk is app-SleepHub_-2a-2eapk.
    The app- in front keeps it apart from a number of the version's own counter.
    """
    if not pattern:
        return f"{QA_REFS}/{version}"
    return f"{QA_REFS}/{version}/{app_key(pattern)}"


def app_key(pattern: str) -> str:
    """The part of a ref's name that stands for an asset pattern: app- and the pattern, every byte but a letter, a digit and _ as - and two hex digits."""
    return "app-" + "".join(chr(byte) if APP_KEY.fullmatch(chr(byte)) else f"-{byte:02x}" for byte in pattern.encode())


def reuse_counter(major: int, number: int, pattern: str) -> str:
    """Where the runs that reused build number `number` of the major for the app of `pattern` say so."""
    return f"{REUSED_REFS}/{major}/{number}/{app_key(pattern)}"


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


def shipped_in(releases: Iterable[dict]) -> list:
    """The versionCodes in the file names of the assets of these releases and pre-releases."""
    out = []
    for release in releases:
        kind = "pre-release" if release.get("prerelease") else "release"
        for asset in release.get("assets") or []:
            name = asset.get("name") if isinstance(asset, dict) else None
            if isinstance(name, str):
                for code in codes_in_name(name):
                    out.append(Shipped(code, f"in the name of {name}, an asset of the {kind} {release.get('tag_name')}"))
    return out


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


def floor_of_a_later_line(major: int, reserved: Iterable[int], shipped: Iterable[Shipped], floor: Optional[int]) -> str:
    """Why the floor is not used for a build of the major, or empty when it is.

    A floor above every code of a major that has builds already (a number
    reserved, a code out) is the floor of a later major: the calling file
    builds a maintenance line too. It is not used for that line's builds. A
    major without any build is refused instead (next_number). A floor that is
    the major's last code is its own, used up, and refused as well.
    """
    low, high = codes_of(major)
    if floor is None or floor <= high:
        return ""
    if not list(reserved) and not any(low <= item.code <= high for item in shipped):
        return ""
    return (
        f"version-code-floor {floor} is above every versionCode of major {major}, which has builds of its own "
        "already: it is the floor of a later major's line, and is not used for this build"
    )


def next_number(major: int, reserved: Iterable[int], shipped: Iterable[Shipped], floor: Optional[int]) -> tuple:
    """The number the next build of the major gets, and why.

    Refused when the major has no codes, and when the build would not be above
    every code of the major that is out.
    """
    low, high = codes_of(major)
    base = major * MAJOR_BLOCK
    reserved = sorted(set(reserved))
    shipped = list(shipped)
    out = [item for item in shipped if low <= item.code <= high]
    highest = max(out, key=lambda item: item.code, default=None)
    if floor_of_a_later_line(major, reserved, shipped, floor):
        floor = None
    if floor is not None and floor >= high:
        # A floor above the major is refused only for a major without builds (a later line's is
        # passed over above); one at its last code is the major's own, whatever it has built.
        why = (
            f"major {major} has no build yet"
            if not reserved and not out
            else f"the floor is the last code of major {major} itself, not the floor of a later major's line"
        )
        raise BuildNumberError(
            f"version-code-floor is {floor}, and no versionCode of major {major} is above it (the highest is {high}), "
            f"and {why}, so no build of major {major} would install over the app that has the floor. "
            "Nothing is built. Build a later major version (next-version in the calling file), or check the "
            "floor: it is the highest versionCode of the app that is out where this repository's releases do not "
            "show it"
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
        # The API the token is for: scheme, host and path, /api/v3/ of GitHub Enterprise Server included.
        self.root = f"{api_url.rstrip('/')}/"
        self.base = f"{self.root}repos/{repository}/"
        self.token = token
        self.call = call

    def _ask(self, method: str, url: str, body: Optional[dict] = None) -> tuple:
        # A server error or no answer at all is asked again, a few times, before it is the answer.
        for delay in (*RETRY_DELAYS, None):
            try:
                status, headers, raw = self.call(method, url, self.token, body)
            except Unreachable:
                if delay is None:
                    raise
                time.sleep(delay)
                continue
            if status < 500 or delay is None:
                break
            time.sleep(delay)
        try:
            document = json.loads(raw) if raw and raw.strip() else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            document = None
        return status, headers, document

    @staticmethod
    def _refusal(what: str, status: int, document: object, scope: str, refs: str = REFS) -> BuildNumberError:
        message = document.get("message") if isinstance(document, dict) else None
        message = message if isinstance(message, str) else ""
        said = f": {message}" if message else ""
        # What to do, as far as the answer says it.
        if "rate limit" in message.lower():
            hint = " GitHub limits the requests of the token for now: run the job again later."
        elif status == 422 and "rule" in message.lower():
            hint = f" A ruleset of the repository refuses it: let the job token create {refs}/*, or keep those refs out of the ruleset."
        elif status in (401, 403, 404):
            hint = f" The job's token needs {scope}."
        elif status >= 500:
            hint = " GitHub answered with a server error, also when asked again: run the job again later."
        else:
            hint = ""
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
            # The token goes with every page: only to the API it was meant for. GitHub names the
            # next page by the repository's id, /repositories/<id>/..., not by its name.
            if url and not url.startswith(self.root):
                raise BuildNumberError(f"the next page of the answer to {what} is not on {self.root}: {url}")
        return items

    def numbers(self, counter: str, what: str) -> dict:
        """The numbers reserved as <counter>/<n>, each with the id of the object its ref names."""
        ref = re.compile(rf"{re.escape(counter)}/([1-9][0-9]*)")
        numbers = {}
        for item in self._listed(f"git/matching-refs/{counter[len('refs/'):]}/", what):
            match = ref.fullmatch(str(item.get("ref", "")))
            target = item.get("object")
            if match:
                numbers[int(match[1])] = target.get("sha", "") if isinstance(target, dict) else ""
        return numbers

    def reserved(self, major: int) -> dict:
        """The numbers reserved for the major, each with the id of the object its ref names."""
        return self.numbers(f"{REFS}/{major}", f"list the build numbers of major {major}")

    def published(self) -> list:
        """The releases and pre-releases of the repository. A draft is not out, and is left out."""
        return [release for release in self._listed("releases?per_page=100", "list the releases") if not release.get("draft")]

    def shipped(self) -> list:
        """The versionCodes in the file names of the assets of the releases and pre-releases."""
        return shipped_in(self.published())

    def reuses(self, major: int) -> dict:
        """The runs that reused a build number of the major: {(n, app key): {k: the id of the object its ref names}}."""
        counter = f"{REUSED_REFS}/{major}"
        ref = re.compile(rf"{re.escape(counter)}/([1-9][0-9]*)/(app-[A-Za-z0-9_-]+)/([1-9][0-9]*)")
        runs = {}
        for item in self._listed(f"git/matching-refs/{counter[len('refs/'):]}/", f"list the runs that reused a build number of major {major}"):
            match = ref.fullmatch(str(item.get("ref", "")))
            target = item.get("object")
            if match:
                runs.setdefault((int(match[1]), match[2]), {})[int(match[3])] = target.get("sha", "") if isinstance(target, dict) else ""
        return runs

    def tag_target(self, sha: str, ref: str = "") -> str:
        """The id of the object a tag object names: the commit of a reservation. `ref` names the tag object, for the message."""
        named_by = f"{ref}, which names {sha}," if ref else sha
        if not OBJECT_ID.fullmatch(sha):
            raise BuildNumberError(f"{named_by} is not the id of a tag object")
        status, _, document = self._ask("GET", f"{self.base}git/tags/{sha}")
        no_tag_object_of_that_id_for_a_token_that_listed_the_refs = status == 404
        if no_tag_object_of_that_id_for_a_token_that_listed_the_refs:
            raise BuildNumberError(
                f"{named_by} does not name a tag object (HTTP 404 for git/tags/{sha}): the reservation of a build "
                "names the tag object of its run, and a ref made by hand, or by another tool, names a commit. Which "
                "commit and which run reserved the number cannot be told. Nothing is built"
            )
        target = document.get("object") if isinstance(document, dict) else None
        named = target.get("sha") if isinstance(target, dict) else None
        if status != 200 or not isinstance(named, str) or not OBJECT_ID.fullmatch(named):
            raise self._refusal(f"read the tag object {sha}", status, document, "contents: read")
        return named

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
        raise self._refusal(f"create {name}", status, document, "contents: write", "/".join(name.split("/")[:2]))


def reserve(
    repository: Repository,
    major: int,
    commit: str,
    marker: str,
    date: str,
    seen: Iterable[Shipped],
    floor: Optional[int],
    kept_only: str = "",
) -> BuildNumber:
    """Reserve the next number of the major for this run, or find the one an earlier attempt reserved.

    `marker` stands for the run, `date` is the time the tag object carries,
    and `seen` the codes that are out besides the assets: the rolling tags.
    With `kept_only`, a build that may only keep a number is refused with that
    reason when no earlier attempt of the run reserved one, before anything is
    reserved: a rebuild of a release that is out.
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
    if kept_only:
        raise BuildNumberError(kept_only)
    shipped = [*repository.shipped(), *seen]
    number, why = next_number(major, reserved, shipped, floor)
    notice = floor_of_a_later_line(major, reserved, shipped, floor)

    def check(number: int) -> None:
        try:
            build_code(major, number)
        except VersionCodeError as exc:
            raise BuildNumberError(f"{exc}. Nothing is built") from None

    number, ref, why = take(repository, f"{REFS}/{major}", number, own, why, f"build numbers of major {major}", check)
    return BuildNumber(major, number, build_code(major, number), why, ref, False, notice)


def take(
    repository: Repository,
    counter: str,
    number: int,
    own: str,
    why: str,
    what: str,
    check: Callable[[int], None] = lambda _: None,
) -> tuple:
    """Create <counter>/<number> naming `own`, or the first such ref above it that is free.

    Returns the number, its ref and why, with the numbers other builds took
    meanwhile. `check` refuses a number above the first before it is tried.
    """
    taken = []
    for _ in range(MAX_TRIES):
        ref = f"{counter}/{number}"
        if repository.create_ref(ref, own):
            if taken:
                why = f"{why}; {', '.join(taken)} taken meanwhile by another build"
            return number, ref, why
        taken.append(str(number))
        number += 1
        check(number)
    raise BuildNumberError(
        f"{MAX_TRIES} {what} in a row were taken while this build tried them, up to {number - 1}. Run the plan again"
    )


def reserve_qa(
    repository: Repository,
    version: str,
    pattern: str,
    commit: str,
    marker: str,
    date: str,
    published: Iterable[tuple],
) -> QaNumber:
    """Reserve the next QA number of the version for this run, or find the one an earlier attempt reserved.

    With `pattern` the app of that asset pattern counts apart. `marker` and
    `date` are those of the run's build number, so both refs name one object.
    `published` is (k, where) of each QA build <version>-qa.<k> of the app that
    is out: the next number is above them too, and a name is never given twice.
    """
    counter = qa_counter(version, pattern)
    of = f"{version} of {pattern}" if pattern else version
    own = repository.tag_object(commit, marker, date)
    reserved = repository.numbers(counter, f"list the QA numbers of {of}")
    kept = [number for number, sha in reserved.items() if sha == own]
    if kept:
        number = max(kept)
        return QaNumber(version, pattern, number, "reserved by an earlier attempt of this run, and kept", f"{counter}/{number}", True)
    last = max(reserved, default=0)
    out = max(published, key=lambda item: item[0], default=None)
    if out and out[0] > last:
        # Names the run number gave, published before the counter existed or beside it.
        before = f", above {counter}/{last}, the last number reserved" if last else f": no QA number of {of} was reserved before"
        number, why = out[0] + 1, f"one above {out[0]}, {out[1]}{before}"
    elif last:
        number, why = last + 1, f"one above {counter}/{last}, the last number reserved"
    else:
        number, why = 1, f"the first QA build of {of}: no QA number of it was reserved before, and none of its QA builds is out"
    number, ref, why = take(repository, counter, number, own, why, f"QA numbers of {of}")
    return QaNumber(version, pattern, number, why, ref, False)


def reuse_of_the_run(runs: dict, major: int, pattern: str, own: str) -> tuple:
    """The number an earlier attempt of this run reused for the app, and the ref that says so: (n, ref), or (0, "").

    `runs` is Repository.reuses of the major, and `own` the run's object: the
    one a reservation of the run would name, so a run that is run again finds
    the ref its first attempt created, whatever number that was and whatever
    the release holds now.
    """
    key = app_key(pattern)
    found = sorted((number, k) for (number, app), refs in runs.items() if app == key for k, sha in refs.items() if sha == own)
    if len({number for number, _ in found}) > 1:
        listed = ", ".join(f"{reuse_counter(major, number, pattern)}/{k}" for number, k in found)
        raise BuildNumberError(
            f"earlier attempts of this run reused more than one build number of major {major} for {pattern}: "
            f"{listed}. A run builds with one. Nothing is built"
        )
    if not found:
        return 0, ""
    number, k = found[-1]
    return number, f"{reuse_counter(major, number, pattern)}/{k}"


def record_reuse(repository: Repository, runs: dict, major: int, number: int, pattern: str, own: str) -> str:
    """Say that this run reused the number for the app: the ref <counter>/<k> naming the run's object. Nothing is reserved.

    `runs` is Repository.reuses of the major: k is one above the runs that reused the number for the app before.
    """
    last = max(runs.get((number, app_key(pattern)), {}), default=0)
    _, ref, _ = take(
        repository, reuse_counter(major, number, pattern), last + 1, own, "",
        f"refs of the runs that reused build number {number} of major {major} for {pattern}",
    )
    return ref


def look(repository: Repository, major: int, seen: Iterable[Shipped], floor: Optional[int]) -> BuildNumber:
    """The number the next build of the major would get now. Nothing is reserved."""
    codes_of(major)
    reserved = repository.reserved(major)
    shipped = [*repository.shipped(), *seen]
    number, why = next_number(major, reserved, shipped, floor)
    notice = floor_of_a_later_line(major, reserved, shipped, floor)
    return BuildNumber(major, number, build_code(major, number), why, "", False, notice)
