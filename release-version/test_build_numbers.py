"""Tests of the build-number scheme: reserving, where a major starts, and the safety rule.

Run: uv run --with pytest python -m pytest release-version -q

The GitHub API is a stand-in kept in memory, with the one property the
reservation rests on: a ref is created only when it does not exist. The plans
run against throwaway git repositories, as in test_release_version.py.
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import build_numbers  # noqa: E402
import release_version  # noqa: E402
from build_numbers import BuildNumberError, Shipped, codes_in_name, next_number  # noqa: E402
from test_release_version import (  # noqa: E402,F401
    API,
    commit,
    error_of,
    git,
    lines,
    origin,
    parse_outputs,
    pulls,
    repo,
    rolling,
)

REPOSITORY = API + "/repos/acme/app/"
# How GitHub names the repository in the Link header of a page: by its id, not by its name.
BY_ID = API + "/repositories/123456789/"


class FakeGitHub:
    """The refs, the tag objects and the releases of acme/app, as the API answers for them."""

    def __init__(self, releases=(), page_size=100):
        self.refs = {}
        self.objects = {}
        self.releases = list(releases)
        self.page_size = page_size
        self.requests = []
        self.lock = threading.Lock()
        # Called with the name of a ref right before it is created: another build that is quicker.
        self.before_create = None
        # Called after the refs of a major were listed.
        self.after_list = None
        # (method, path) -> (status, message): what the API refuses.
        self.refuse = {}
        # (start of a ref's name, status, message): the refs whose creation the API refuses.
        self.refuse_ref = None
        # The next pages are linked as GitHub links them, /repositories/<id>/..., or under the name.
        self.link_by_id = True
        # matching-refs/build-numbers/2/ answering for every ref that starts with build-numbers/2,
        # those of major 20 included, as a prefix without its slash would.
        self.loose_prefix = False
        # (method, path prefix, status or "unreachable"), answered once each before anything else.
        self.trouble = []

    def release(self, tag, *assets, prerelease=False, draft=False):
        self.releases.append({
            "tag_name": tag, "prerelease": prerelease, "draft": draft, "assets": [{"name": name} for name in assets],
        })

    def reserve_by_hand(self, major, number, sha="0" * 40):
        self.refs[f"refs/build-numbers/{major}/{number}"] = sha

    def numbers(self, major):
        prefix = f"refs/build-numbers/{major}/"
        return sorted(int(name[len(prefix):]) for name in self.refs if name.startswith(prefix))

    def qa_numbers(self):
        return sorted(name for name in self.refs if name.startswith("refs/qa-numbers/"))

    def writes(self):
        return [request for request in self.requests if request[0] != "GET"]

    def _answer(self, status, document, headers=None):
        return status, headers or {}, json.dumps(document).encode()

    def _page(self, path, items):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        page = int(query.get("page", ["1"])[0])
        chunk = items[(page - 1) * self.page_size:page * self.page_size]
        headers = {}
        if page * self.page_size < len(items):
            following = urllib.parse.urlencode({**{k: v[0] for k, v in query.items()}, "page": page + 1})
            root = BY_ID if self.link_by_id else REPOSITORY
            headers["link"] = f'<{root}{path.split("?")[0]}?{following}>; rel="next", <{root}{path.split("?")[0]}?page=99>; rel="last"'
        return self._answer(200, chunk, headers)

    def __call__(self, method, url, token, body):
        assert url.startswith((REPOSITORY, BY_ID)), url
        assert token == "job-token"
        path = url[len(REPOSITORY if url.startswith(REPOSITORY) else BY_ID):]
        self.requests.append((method, path.split("?")[0]))
        for index, (troubled_method, troubled_path, what) in enumerate(self.trouble):
            if method == troubled_method and path.startswith(troubled_path):
                del self.trouble[index]
                if what == "unreachable":
                    raise build_numbers.Unreachable(f"cannot reach {url}: timed out")
                return self._answer(what, {"message": "Server Error"})
        for (refused_method, refused_path), (status, message) in self.refuse.items():
            if method == refused_method and path.startswith(refused_path):
                return self._answer(status, {"message": message})
        if method == "GET" and path.startswith("releases"):
            return self._page(path, self.releases)
        if method == "GET" and path.startswith("git/matching-refs/"):
            prefix = "refs/" + path.split("?")[0][len("git/matching-refs/"):]
            if self.loose_prefix:
                prefix = prefix.rstrip("/")
            with self.lock:
                listed = [{"ref": name, "object": {"sha": sha, "type": "tag"}} for name, sha in sorted(self.refs.items()) if name.startswith(prefix)]
            answer = self._page(path, listed)
            if self.after_list:
                self.after_list()
            return answer
        if method == "POST" and path == "git/tags":
            # A git object is its content: the same content is the same object.
            sha = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()
            self.objects[sha] = body
            return self._answer(201, {"sha": sha})
        if method == "GET" and path.startswith("git/tags/"):
            body = self.objects.get(path[len("git/tags/"):])
            if body is None:
                return self._answer(404, {"message": "Not Found"})
            return self._answer(200, {"sha": path[len("git/tags/"):], "object": {"sha": body["object"], "type": body["type"]}})
        if method == "POST" and path == "git/refs":
            if self.before_create:
                hook, self.before_create = self.before_create, None
                hook(body["ref"])
            if self.refuse_ref and body["ref"].startswith(self.refuse_ref[0]):
                return self._answer(self.refuse_ref[1], {"message": self.refuse_ref[2]})
            with self.lock:
                if body["ref"] in self.refs:
                    return self._answer(422, {"message": "Reference already exists"})
                self.refs[body["ref"]] = body["sha"]
            return self._answer(201, {"ref": body["ref"], "object": {"sha": body["sha"]}})
        raise AssertionError(f"unexpected request {method} {url}")


def no_api(method, url, token, body):
    raise AssertionError(f"nothing is asked before this refusal, and {method} {url} was asked")


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    # A request that is made again waits first; not in the tests.
    monkeypatch.setattr(build_numbers, "RETRY_DELAYS", (0, 0, 0))


def run(tmp_path, gh, run_id="7001", read=None, **inputs):
    output = tmp_path / "github-output"
    output.write_text("")
    env = {
        "BUMP": "patch",
        "TAG_PREFIX": "v",
        "TOKEN": "job-token",
        "GITHUB_REPOSITORY": "acme/app",
        "GITHUB_API_URL": API,
        "GITHUB_OUTPUT": str(output),
        "GITHUB_RUN_ID": run_id,
        "VERSION_CODE_SCHEME": "build-number",
    }
    env.update({name.upper(): value for name, value in inputs.items()})
    code = release_version.main(env, pulls(), gh, read)
    return code, parse_outputs(output.read_text())


def qa(tmp_path, gh, sha, ref="refs/heads/develop", sequence="57", **inputs):
    """Plan a QA build the way android-qa.yml does, with the build-number scheme."""
    return run(tmp_path, gh, operation="qa", sha=sha, ref=ref, qa_sequence=sequence, source_branch="develop", **inputs)


def release(tmp_path, gh, tag, sha, kind="release", **inputs):
    """Compute the versionCode of a tag the way android-release.yml does, with the build-number scheme."""
    return run(tmp_path, gh, operation="code", kind=kind, version=tag, sha=sha, **inputs)


# --- the file names of the assets ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "codes"),
    [
        ("SleepHub_qa_2.0.0-qa.6_200000006.apk", [200_000_006]),
        # As sleephub-android names its assets (2026-11-18): a QA build, and a test build of an
        # earlier flow whose label follows the code.
        ("SleepHub_qa_2.0.0-qa.7_200000007.apk", [200_000_007]),
        ("SleepHub_release_1.11.2_101150086-handok-reset-off.84.apk", [101_150_086]),
        ("HeySleep-2.0.0-preview.1-200049001.apk", [200_049_001]),
        ("SweetSleep_release_1.12.0_101200000.aab", [101_200_000]),
        ("App_qa_0.0.1-qa.580_37.apk", [37]),
        # No part of digits only: the version name's digits are joined by dots.
        ("app-release.apk", []),
        ("app-2.0.0.apk", []),
        ("App.200000006.apk", []),
        # Not an APK or an app bundle: a mapping file, a checksum list.
        ("mapping-200000006.txt", []),
        ("checksums_2026.txt", []),
        # A leading zero is no number.
        ("App_qa_007.apk", []),
        # A part of digits that is not a versionCode is read as one: an ABI of a split APK.
        ("app-x86_64-release-200000006.apk", [64, 200_000_006]),
    ],
)
def test_the_versioncode_of_an_asset_is_a_part_of_its_name_of_digits_only(name, codes):
    assert codes_in_name(name) == codes


# --- where a major starts, and the safety rule ---------------------------------------------------


def test_a_major_without_reserved_numbers_starts_above_what_is_out():
    out = [Shipped(200_000_006, "the build 2.0.0-qa.6 that qa-develop holds"), Shipped(101_300_002, "a 1.x build")]
    assert next_number(2, [], out, None) == (7, "one above 200000006, the build 2.0.0-qa.6 that qa-develop holds: no number of major 2 was reserved before")
    # The codes of other majors do not count for this one.
    assert next_number(1, [], out, None)[0] == 1_300_003
    assert next_number(3, [], out, None) == (1, "the first build of major 3: no number of it was reserved before, and none of its codes is out")


def test_reserved_numbers_are_the_counter_once_there_are_any():
    out = [Shipped(200_000_006, "qa-develop")]
    assert next_number(2, [7, 8, 12], out, None) == (13, "one above refs/build-numbers/2/12, the last number reserved")


@pytest.mark.parametrize("major", [0, 1, 2, 20])
def test_the_first_build_of_a_major_is_its_block_plus_one(major):
    number, _ = next_number(major, [], [], None)
    assert release_version.build_numbers.build_code(major, number) == major * 100_000_000 + 1


def test_the_floor_lifts_the_next_number_and_only_within_its_major():
    out = [Shipped(101_300_002, "qa-release-1.x")]
    # The app in Google Play is at 101400000, which no release of this repository shows.
    assert next_number(1, [], out, 101_400_000) == (1_400_001, "one above version-code-floor 101400000")
    # A floor below what is out, or below the major, changes nothing.
    assert next_number(1, [], out, 101_000_000)[0] == 1_300_003
    assert next_number(2, [], out, 101_400_000)[0] == 1
    # Once numbers are reserved, it still lifts them; above it, it is left behind like next-version.
    assert next_number(1, [1_300_003], out, 101_400_000)[0] == 1_400_001
    assert next_number(1, [1_400_005], out, 101_400_000)[0] == 1_400_006


@pytest.mark.parametrize("floor", [101_300_002, 99_999_999])
def test_a_floor_above_every_code_of_a_major_without_builds_is_refused(floor):
    # A new repository that takes over an app at 101300002 and builds 0.0.1: no build of major 0
    # installs over it.
    with pytest.raises(BuildNumberError) as refused:
        next_number(0, [], [], floor)
    assert f"version-code-floor is {floor}, and no versionCode of major 0 is above it (the highest is 99999999), and major 0 has no build yet" in str(refused.value)
    assert "next-version" in str(refused.value)
    assert build_numbers.floor_of_a_later_line(0, [], [], floor) == ""


def test_a_floor_above_a_major_that_has_builds_is_a_later_lines_and_passed_over():
    # One qa.yml builds develop (2.x) and release/1.x. A floor for 2.x is above every code of 1.x,
    # which has builds of its own: it is the floor of the later line, and not this one's.
    out = [Shipped(101_300_002, "the build 1.13.2-qa.2 that qa-release-1.x holds")]
    assert next_number(1, [], out, 200_000_006)[0] == 1_300_003
    assert next_number(1, [1_300_005], [], 200_000_006)[0] == 1_300_006
    said = "version-code-floor 200000006 is above every versionCode of major 1, which has builds of its own already"
    assert build_numbers.floor_of_a_later_line(1, [], out, 200_000_006).startswith(said)
    assert build_numbers.floor_of_a_later_line(1, [1_300_005], [], 200_000_006).startswith(said)
    # Within the major, or below it, the floor is the floor.
    assert build_numbers.floor_of_a_later_line(2, [], out, 200_000_006) == ""
    assert build_numbers.floor_of_a_later_line(3, [5], out, 200_000_006) == ""


@pytest.mark.parametrize(
    ("reserved", "shipped"),
    [
        # Builds of major 2 known by a reserved number only, or by an APK that is out only.
        ([5], []),
        ([], [Shipped(200_000_005, "in the name of App_qa_2.0.1-qa.5_200000005.apk, an asset of the pre-release qa-develop")]),
    ],
)
def test_a_floor_at_the_last_code_of_a_major_with_builds_is_its_own_and_refused(reserved, shipped):
    # 299999999 is the highest code of major 2: no build of 2 can be above it. It is not above
    # every code of 2, so it is not a later line's floor to pass over; the major is used up.
    high = 299_999_999
    assert build_numbers.floor_of_a_later_line(2, reserved, shipped, high) == ""
    with pytest.raises(BuildNumberError) as refused:
        next_number(2, reserved, shipped, high)
    message = str(refused.value)
    assert message.startswith(
        "version-code-floor is 299999999, and no versionCode of major 2 is above it (the highest is 299999999), and "
        "the floor is the last code of major 2 itself, not the floor of a later major's line, so no build of major 2 "
        "would install over the app that has the floor. Nothing is built."
    )
    assert "has no build yet" not in message
    # One above it is the next major's block: a later line's floor, passed over with a notice.
    assert next_number(2, reserved, shipped, high + 1)[0] == 6
    assert build_numbers.floor_of_a_later_line(2, reserved, shipped, high + 1).startswith(
        "version-code-floor 300000000 is above every versionCode of major 2, which has builds of its own already"
    )
    gh = FakeGitHub()
    for number in reserved:
        gh.reserve_by_hand(2, number)
    seen = list(shipped)
    with pytest.raises(BuildNumberError):
        build_numbers.reserve(repository(gh), 2, "a" * 40, "run 1", "2026-11-19T00:00:00Z", seen, high)
    assert gh.numbers(2) == reserved
    number = build_numbers.reserve(repository(gh), 2, "a" * 40, "run 1", "2026-11-19T00:00:00Z", seen, high + 1)
    assert (number.number, number.notice.startswith("version-code-floor 300000000 is above every versionCode of major 2")) == (6, True)


def test_a_floor_at_the_last_code_of_a_major_without_builds_is_refused():
    with pytest.raises(BuildNumberError) as refused:
        next_number(2, [], [], 299_999_999)
    assert "(the highest is 299999999), and major 2 has no build yet, so no build of major 2 would install" in str(refused.value)
    assert build_numbers.floor_of_a_later_line(2, [], [], 299_999_999) == ""


def test_a_maintenance_line_builds_with_the_floor_of_the_later_line_given(lines, repo, tmp_path, capsys):
    #   develop:      base(v1.13.0) ── a ── b(v2.0.0) ── c
    #   release/1.x:    └── h1(v1.13.1) ── h2
    rolling(repo, "qa-release-1.x", "1.13.2-qa.2", 101_300_002, lines["h2"])
    gh = FakeGitHub()
    code, out = qa(tmp_path, gh, lines["h2"], ref="refs/heads/release/1.x", version_code_floor="200000006")
    assert (code, out["version-code"]) == (0, "101300003")
    log = capsys.readouterr().out
    assert "::notice title=release-version::version-code-floor 200000006 is above every versionCode of major 1, which has builds of its own already" in log
    # The later line itself is lifted by it.
    code, out = qa(tmp_path, gh, lines["c"], version_code_floor="200000006", run_id="7002")
    assert (code, out["version-code"]) == (0, "200000007")
    assert "::notice" not in capsys.readouterr().out


def test_numbers_behind_what_is_out_are_refused():
    # 2.0.0 was released under the formula after the numbers of major 2 had started.
    out = [Shipped(200_050_000, "in the name of HeySleep_release_2.0.0_200050000.apk, an asset of the release v2.0.0")]
    with pytest.raises(BuildNumberError) as refused:
        next_number(2, [7], out, None)
    message = str(refused.value)
    assert "the next build number of major 2 is 8 (one above refs/build-numbers/2/7, the last number reserved), which is versionCode 200000008" in message
    assert "200050000 is out already: in the name of HeySleep_release_2.0.0_200050000.apk, an asset of the release v2.0.0" in message
    assert "Nothing is built. To go on above it, set version-code-floor: 200050000 in the calling files" in message
    # The floor the message names is the way on.
    assert next_number(2, [7], out, 200_050_000)[0] == 50_001


@pytest.mark.parametrize("major", [21, 99, -1])
def test_a_major_above_twenty_has_no_codes(major):
    with pytest.raises(BuildNumberError) as refused:
        next_number(major, [], [], None)
    assert f"the build is of major {major}, and major version {major} is outside 0..20" in str(refused.value)


def test_the_last_number_of_a_major_is_the_last_one():
    with pytest.raises(BuildNumberError) as refused:
        next_number(20, [99_999_999], [], None)
    assert "build number 100000000 is outside 1..99999999" in str(refused.value)
    assert next_number(20, [99_999_998], [], None)[0] == 99_999_999


# --- reserving ------------------------------------------------------------------------------------


def repository(gh):
    return build_numbers.Repository(API, "acme/app", "job-token", gh)


def test_two_builds_that_reserve_at_once_get_two_numbers():
    gh = FakeGitHub()
    gh.reserve_by_hand(2, 6)
    # Both list the refs before either creates one: both start at 7.
    barrier = threading.Barrier(2, timeout=10)
    gh.after_list = barrier.wait
    results = {}

    def build(name):
        results[name] = build_numbers.reserve(repository(gh), 2, "a" * 40, f"run {name}", "2026-11-18T00:00:00Z", [], None)

    threads = [threading.Thread(target=build, args=(name,)) for name in ("first", "second")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(result.number for result in results.values()) == [7, 8]
    assert gh.numbers(2) == [6, 7, 8]
    later = max(results.values(), key=lambda result: result.number)
    assert later.why == "one above refs/build-numbers/2/6, the last number reserved; 7 taken meanwhile by another build"


def test_a_number_taken_meanwhile_moves_the_build_up_until_one_is_free():
    gh = FakeGitHub()

    def quicker(name):
        # Two other builds were quicker.
        gh.reserve_by_hand(0, 1)
        gh.reserve_by_hand(0, 2)

    gh.before_create = quicker
    number = build_numbers.reserve(repository(gh), 0, "a" * 40, "run 1", "2026-11-18T00:00:00Z", [], None)
    assert (number.number, number.code, number.ref) == (3, 3, "refs/build-numbers/0/3")
    assert gh.numbers(0) == [1, 2, 3]


def test_a_ref_that_cannot_be_created_fails_with_what_the_token_needs():
    gh = FakeGitHub()
    gh.refuse[("POST", "git/refs")] = (403, "Resource not accessible by integration")
    with pytest.raises(BuildNumberError) as refused:
        build_numbers.reserve(repository(gh), 2, "a" * 40, "run 1", "2026-11-18T00:00:00Z", [], None)
    assert str(refused.value) == (
        "cannot create refs/build-numbers/2/1: HTTP 403: Resource not accessible by integration. "
        "The job's token needs contents: write."
    )
    gh.refuse = {("GET", "releases"): (404, "Not Found")}
    with pytest.raises(BuildNumberError) as refused:
        build_numbers.look(repository(gh), 2, [], None)
    assert "cannot list the releases: HTTP 404: Not Found. The job's token needs contents: read." == str(refused.value)


@pytest.mark.parametrize("by_id", [True, False])
def test_every_page_of_the_answer_is_read(by_id):
    # GitHub links the next page by the repository's id: /repositories/<id>/releases?page=2, as
    # sleephub-android's releases do. A link under the name is followed as well.
    gh = FakeGitHub(page_size=2)
    gh.link_by_id = by_id
    for patch in range(5):
        gh.release(f"v2.0.{patch}", f"App_release_2.0.{patch}_2000000{patch + 10}.apk")
    for number in range(1, 6):
        gh.reserve_by_hand(1, number)
    assert max(item.code for item in repository(gh).shipped()) == 200_000_014
    assert sorted(repository(gh).reserved(1)) == [1, 2, 3, 4, 5]
    assert gh.requests.count(("GET", "releases")) == 3 and gh.requests.count(("GET", "git/matching-refs/build-numbers/1/")) == 3


@pytest.mark.parametrize(
    ("api", "link", "followed"),
    [
        ("https://api.github.invalid", "https://api.github.invalid/repositories/1/releases?page=2", True),
        ("https://ghe.invalid/api/v3", "https://ghe.invalid/api/v3/repositories/1/releases?page=2", True),
        # The token goes to the API it is for, and nowhere else.
        ("https://api.github.invalid", "https://api.github.invalid.example/repositories/1/releases?page=2", False),
        ("https://api.github.invalid", "http://api.github.invalid/repositories/1/releases?page=2", False),
        ("https://ghe.invalid/api/v3", "https://ghe.invalid/other/repositories/1/releases?page=2", False),
    ],
)
def test_a_next_page_is_followed_only_on_the_api_of_the_token(api, link, followed):
    asked = []

    def call(method, url, token, body):
        asked.append(url)
        if len(asked) == 1:
            return 200, {"link": f'<{link}>; rel="next"'}, b"[]"
        return 200, {}, b"[]"

    listing = build_numbers.Repository(api, "acme/app", "job-token", call)
    if followed:
        assert listing.shipped() == [] and asked == [f"{api}/repos/acme/app/releases?per_page=100", link]
    else:
        with pytest.raises(BuildNumberError) as refused:
            listing.shipped()
        assert str(refused.value) == f"the next page of the answer to list the releases is not on {api}/: {link}"
        assert asked == [f"{api}/repos/acme/app/releases?per_page=100"]


def test_a_prefix_that_also_matches_a_later_major_does_not_count_its_refs():
    # build-numbers/2 is the start of build-numbers/20 as well. Should GitHub match the prefix
    # without its slash, the refs of major 20 are not numbers of major 2.
    gh = FakeGitHub()
    gh.loose_prefix = True
    gh.reserve_by_hand(2, 5)
    gh.reserve_by_hand(20, 900)
    gh.reserve_by_hand(21, 3)
    assert sorted(repository(gh).reserved(2)) == [5]
    number = build_numbers.reserve(repository(gh), 2, "a" * 40, "run 1", "2026-11-18T00:00:00Z", [], None)
    assert number.number == 6


def test_a_server_error_or_no_answer_is_asked_again_before_it_is_the_answer():
    gh = FakeGitHub()
    gh.trouble = [("GET", "releases", 502), ("GET", "releases", "unreachable"), ("POST", "git/tags", 500), ("POST", "git/refs", 503)]
    number = build_numbers.reserve(repository(gh), 2, "a" * 40, "run 1", "2026-11-18T00:00:00Z", [], None)
    assert (number.number, gh.trouble) == (1, [])
    assert gh.requests.count(("GET", "releases")) == 3 and gh.requests.count(("POST", "git/refs")) == 2
    # Four tries in all; then the answer is the answer.
    gh.trouble = [("GET", "releases", 502)] * 4
    with pytest.raises(BuildNumberError) as refused:
        build_numbers.look(repository(gh), 2, [], None)
    assert str(refused.value) == (
        "cannot list the releases: HTTP 502: Server Error. GitHub answered with a server error, also when asked "
        "again: run the job again later."
    )
    gh.trouble = [("GET", "git/matching-refs/", "unreachable")] * 4
    with pytest.raises(build_numbers.Unreachable):
        build_numbers.look(repository(gh), 2, [], None)
    # A refusal is not asked again.
    gh.refuse[("GET", "releases")] = (404, "Not Found")
    before = gh.requests.count(("GET", "releases"))
    with pytest.raises(BuildNumberError):
        build_numbers.look(repository(gh), 2, [], None)
    assert gh.requests.count(("GET", "releases")) == before + 1


def test_a_build_that_finds_every_number_taken_gives_up(monkeypatch):
    monkeypatch.setattr(build_numbers, "MAX_TRIES", 3)
    gh = FakeGitHub()
    gh.refuse[("POST", "git/refs")] = (422, "Reference already exists")
    with pytest.raises(BuildNumberError) as refused:
        build_numbers.reserve(repository(gh), 2, "a" * 40, "run 1", "2026-11-18T00:00:00Z", [], None)
    assert str(refused.value) == "3 build numbers of major 2 in a row were taken while this build tried them, up to 3. Run the plan again"
    assert gh.requests.count(("POST", "git/refs")) == 3


@pytest.mark.parametrize(
    ("status", "message", "hint"),
    [
        # A ruleset refuses the ref: no number is taken, and no other number is tried.
        (422, "Repository rule violations found\n\nCannot create ref due to creations being restricted.",
         "A ruleset of the repository refuses it: let the job token create refs/build-numbers/*, or keep those refs out of the ruleset."),
        (403, "You have exceeded a secondary rate limit.", "GitHub limits the requests of the token for now: run the job again later."),
        (403, "Resource not accessible by integration", "The job's token needs contents: write."),
        (422, "Object does not exist", ""),
    ],
)
def test_a_ref_that_is_refused_for_another_reason_than_being_taken_fails_at_once(status, message, hint):
    gh = FakeGitHub()
    gh.refuse[("POST", "git/refs")] = (status, message)
    with pytest.raises(BuildNumberError) as refused:
        build_numbers.reserve(repository(gh), 2, "a" * 40, "run 1", "2026-11-18T00:00:00Z", [], None)
    assert str(refused.value) == f"cannot create refs/build-numbers/2/1: HTTP {status}: {message}.{' ' + hint if hint else ''}"
    assert gh.requests.count(("POST", "git/refs")) == 1 and gh.refs == {}


def test_a_tag_object_that_cannot_be_created_reserves_nothing():
    gh = FakeGitHub()
    gh.refuse[("POST", "git/tags")] = (403, "Resource not accessible by integration")
    with pytest.raises(BuildNumberError) as refused:
        build_numbers.reserve(repository(gh), 2, "a" * 40, "run 1", "2026-11-18T00:00:00Z", [], None)
    assert str(refused.value) == (
        "cannot create the object of a build number: HTTP 403: Resource not accessible by integration. "
        "The job's token needs contents: write."
    )
    assert gh.refs == {} and ("POST", "git/refs") not in gh.requests


def test_a_draft_is_not_out():
    gh = FakeGitHub()
    gh.release("v2.0.0", "App_release_2.0.0_200000009.apk", draft=True)
    gh.release("qa-develop", "App_qa_2.0.0-qa.3_200000003.apk", prerelease=True)
    assert [(item.code, item.where) for item in repository(gh).shipped()] == [
        (200_000_003, "in the name of App_qa_2.0.0-qa.3_200000003.apk, an asset of the pre-release qa-develop"),
    ]


# --- the QA build and the release with build numbers ------------------------------------------------


def test_a_new_repository_counts_from_one(repo, tmp_path, capsys):
    # No release, no asset, no ref: the builds of major 0 are 1, 2, 3.
    target = commit(repo, "Feature (#1)")
    origin(repo, "develop")
    gh = FakeGitHub()
    codes = [qa(tmp_path, gh, target, run_id=str(run), sequence=str(run))[1]["version-code"] for run in (1, 2, 3)]
    assert codes == ["1", "2", "3"]
    code, out = qa(tmp_path, gh, target, run_id="4", sequence="4")
    assert code == 0
    # The name is the one of the formula; only the versionCode is the build number.
    assert (out["version-name"], out["version-code"], out["build-number"]) == ("0.0.1-qa.4", "4", "4")
    assert gh.numbers(0) == [1, 2, 3, 4]
    log = capsys.readouterr().out
    assert "Build number 4 of major 0, reserved as refs/build-numbers/0/4: one above refs/build-numbers/0/3, the last number reserved." in log
    assert "Version name: 0.0.1-qa.4 (versionCode 4, QA build 4)" in log


def test_a_new_repository_heading_for_1_0_0_starts_at_its_block(repo, tmp_path):
    target = commit(repo, "Feature (#1)")
    origin(repo, "develop")
    gh = FakeGitHub()
    _, out = qa(tmp_path, gh, target, next_version="1.0.0")
    assert (out["version-name"], out["version-code"]) == ("1.0.0-qa.1", "100000001")


def test_major_zero_goes_on_into_major_one_above_all_of_it(repo, tmp_path):
    git(repo, "tag", "v0.3.0")
    target = commit(repo, "Feature (#1)")
    origin(repo, "develop")
    gh = FakeGitHub()
    gh.release("v0.3.0", "App_release_0.3.0_41.apk")
    _, out = qa(tmp_path, gh, target)
    assert (out["version-name"], out["version-code"]) == ("0.3.1-qa.1", "42")
    gh.reserve_by_hand(0, 99_999_999)
    _, out = qa(tmp_path, gh, target, next_version="1.0.0", run_id="7002")
    # Above every code major 0 can have.
    assert (out["version-name"], out["version-code"]) == ("1.0.0-qa.1", "100000001")


def test_a_floor_above_the_major_stops_the_build_of_a_repository_that_took_over_an_app(repo, tmp_path, capsys):
    # A new repository that replaces the app at 101300002 in Google Play: its releases do not show it.
    target = commit(repo, "Feature (#1)")
    origin(repo, "develop")
    gh = FakeGitHub()
    code, out = qa(tmp_path, gh, target, version_code_floor="101300002")
    assert (code, out) == (1, {})
    assert "version-code-floor is 101300002, and no versionCode of major 0 is above it" in error_of(capsys)
    assert gh.refs == {}
    # Heading for 2.0.0, it is above it; within major 1, the floor lifts the start.
    _, out = qa(tmp_path, gh, target, version_code_floor="101300002", next_version="2.0.0")
    assert out["version-code"] == "200000001"
    _, out = qa(tmp_path, gh, target, version_code_floor="101300002", next_version="1.0.0", run_id="7002")
    assert out["version-code"] == "101300003"


@pytest.mark.parametrize("floor", ["0", "-1", "1e9", "2100000001", "abc"])
def test_a_floor_that_is_no_versioncode_is_refused(repo, tmp_path, capsys, floor):
    target = commit(repo, "Feature (#1)")
    origin(repo, "develop")
    code, _ = qa(tmp_path, FakeGitHub(), target, version_code_floor=floor)
    assert code == 1
    assert f"version-code-floor must be a versionCode, a whole number from 1 to 2100000000, got {floor!r}" in error_of(capsys)


def test_a_major_above_twenty_is_refused_before_anything_is_reserved(repo, tmp_path, capsys):
    target = commit(repo, "Feature (#1)")
    origin(repo, "develop")
    gh = FakeGitHub()
    code, out = qa(tmp_path, gh, target, next_version="21.0.0")
    assert (code, out) == (1, {})
    assert "the build is of major 21, and major version 21 is outside 0..20" in error_of(capsys)
    git(repo, "tag", "v21.0.0")
    code, _ = release(tmp_path, gh, "v21.0.0", target)
    assert code == 1
    assert "the build is of major 21, and major version 21 is outside 0..20" in error_of(capsys)
    code, _ = run(tmp_path, gh, operation="next", sha=commit(repo, "More (#2)"), version="22.0.0")
    assert code == 1
    assert "the build is of major 22" in error_of(capsys)
    # Nothing was asked, let alone written.
    assert gh.requests == []
    # The formula refuses it too, in its own words.
    code, _ = run(tmp_path, no_api, operation="code", kind="release", version="21.0.0", version_code_scheme="")
    assert code == 1
    assert "above the limit of 2100000000" in error_of(capsys)


def test_the_first_build_after_the_switch_is_one_above_what_each_app_has_out(lines, repo, tmp_path):
    #   develop:      base(v1.13.0) ── a ── b(v2.0.0) ── c
    #   release/1.x:    └── h1(v1.13.1) ── h2
    # The codes that were out on 2026-11-18. sleephub: 2.0.0-qa.6 on qa-develop, 101300002 on
    # qa-release-1.x, and SweetSleep 1.12.0 as a release asset.
    rolling(repo, "qa-develop", "2.0.1-qa.6", 200_000_006, lines["c"])
    rolling(repo, "qa-release-1.x", "1.13.2-qa.2", 101_300_002, lines["h2"])
    gh = FakeGitHub()
    gh.release("v1.12.0", "SweetSleep_release_1.12.0_101200000.apk")
    gh.release("qa-release-1.x", "SleepHub_qa_1.13.2-qa.2_101300002.apk", prerelease=True)
    _, develop = qa(tmp_path, gh, lines["c"], run_id="1")
    _, maintenance = qa(tmp_path, gh, lines["h2"], ref="refs/heads/release/1.x", run_id="2")
    assert (develop["version-code"], maintenance["version-code"]) == ("200000007", "101300003")
    # The names go on from the QA builds of the same version that are out.
    assert (develop["version-name"], maintenance["version-name"]) == ("2.0.1-qa.7", "1.13.2-qa.3")


def test_the_first_heysleep_build_after_the_switch_is_above_its_preview(repo, tmp_path):
    # heysleep: no release yet, 2.0.0-preview.1 is 200049001 and its QA builds are at 200001012.
    target = commit(repo, "Feature (#1)")
    git(repo, "tag", "v2.0.0-preview.1")
    after = commit(repo, "Fix (#2)")
    origin(repo, "develop")
    rolling(repo, "qa-develop", "2.0.0-qa.1012", 200_001_012, target)
    gh = FakeGitHub()
    gh.release("v2.0.0-preview.1", "HeySleep_preview_2.0.0-preview.1_200049001.apk", prerelease=True)
    _, out = qa(tmp_path, gh, after, next_version="2.0.0")
    assert (out["version-name"], out["version-code"]) == ("2.0.0-qa.1013", "200049002")


def test_a_qa_build_after_a_release_is_above_the_release(repo, tmp_path):
    # The reason for the scheme: heysleep releases 2.0.0 under the formula as 200050000, and the
    # QA builds after it, 2.0.1-qa.N, were 200000000 + N under the formula, below the release.
    # Sleepless takes a build only when its code is higher than the one it has.
    target = commit(repo, "Feature (#1)")
    git(repo, "tag", "v2.0.0")
    rolling(repo, "qa-develop", "2.0.0-qa.1012", 200_001_012, target)
    gh = FakeGitHub()
    gh.release("v2.0.0", "HeySleep_release_2.0.0_200050000.apk")
    assert release_version.release_code(2, 0, 0) == 200_050_000
    in_order = []
    fix = commit(repo, "Fix after 2.0.0 (#2)")
    origin(repo, "develop")
    _, out = qa(tmp_path, gh, fix, run_id="1", sequence="1013")
    assert (out["version-name"], out["version-code"]) == ("2.0.1-qa.1", "200050001")
    in_order.append(int(out["version-code"]))
    # A label build, a release, a preview and QA builds, from one counter, in the order they are made.
    _, out = qa(tmp_path, gh, fix, run_id="2", label="ble-test")
    assert out["version-name"] == "2.0.1-ble-test.1"
    in_order.append(int(out["version-code"]))
    git(repo, "tag", "v2.0.1", fix)
    _, out = release(tmp_path, gh, "v2.0.1", fix, run_id="3")
    assert (out["version"], out["version-code"]) == ("2.0.1", "200050003")
    in_order.append(int(out["version-code"]))
    later = commit(repo, "Feature for 2.1 (#3)")
    origin(repo, "develop")
    _, out = qa(tmp_path, gh, later, run_id="4", sequence="1014")
    assert out["version-name"] == "2.0.2-qa.1"
    in_order.append(int(out["version-code"]))
    git(repo, "tag", "v2.1.0-preview.1", later)
    _, out = release(tmp_path, gh, "v2.1.0-preview.1", later, kind="preview", run_id="5")
    assert out["version"] == "2.1.0-preview.1"
    in_order.append(int(out["version-code"]))
    _, out = qa(tmp_path, gh, later, run_id="6", sequence="1015")
    in_order.append(int(out["version-code"]))
    # Every build installs over every build before it.
    assert in_order == [200_050_001, 200_050_002, 200_050_003, 200_050_004, 200_050_005, 200_050_006]
    assert in_order[3] > in_order[2] == 200_050_003


def test_a_run_that_is_run_again_keeps_its_number_and_another_run_gets_a_new_one(repo, tmp_path, capsys):
    git(repo, "tag", "v2.0.0")
    target = commit(repo, "Fix (#1)")
    origin(repo, "develop")
    gh = FakeGitHub()
    _, first = qa(tmp_path, gh, target, run_id="9001")
    # The same run again, whether its failed jobs or all of them: the same object, the same number.
    capsys.readouterr()
    _, again = qa(tmp_path, gh, target, run_id="9001")
    assert first["version-code"] == again["version-code"] == "200000001"
    assert "Build number 1 of major 2 (refs/build-numbers/2/1): reserved by an earlier attempt of this run, and kept." in capsys.readouterr().out
    # Another run of the same commit is another build.
    _, other = qa(tmp_path, gh, target, run_id="9002")
    assert other["version-code"] == "200000002"
    # The earlier run, run again after it, still keeps its own.
    _, earlier = qa(tmp_path, gh, target, run_id="9001")
    assert earlier["version-code"] == "200000001"
    assert gh.numbers(2) == [1, 2]
    # The release of the tag, run again, as well.
    git(repo, "tag", "v2.0.1", target)
    codes = {release(tmp_path, gh, "v2.0.1", target, run_id=run)[1]["version-code"] for run in ("9003", "9003")}
    assert codes == {"200000003"}


def test_a_release_that_is_out_is_built_again_only_by_its_own_run(lines, repo, tmp_path, capsys):
    # With the formula a tag has one code, so a new run of a released tag was safe. A new run takes
    # the next build number: v2.0.1 built again after v2.0.2 would be above v2.0.2, and a device
    # that took it would refuse v2.0.2 and every build after it.
    gh = FakeGitHub()
    git(repo, "tag", "v2.0.1", lines["c"])
    _, first = release(tmp_path, gh, "v2.0.1", lines["c"], run_id="8001", release_exists="false")
    assert first["version-code"] == "200000001"
    _, later = qa(tmp_path, gh, lines["c"], run_id="8002")
    assert later["version-code"] == "200000002"
    # The run that built it, run again (the release is out by then): it keeps its number.
    code, again = release(tmp_path, gh, "v2.0.1", lines["c"], run_id="8001", release_exists="true")
    assert (code, again["version-code"]) == (0, "200000001")
    # Another run: refused before anything is reserved.
    capsys.readouterr()
    code, out = release(tmp_path, gh, "v2.0.1", lines["c"], run_id="8003", release_exists="true")
    assert (code, out) == (1, {})
    message = error_of(capsys)
    assert message.startswith("v2.0.1 has a GitHub Release already, and no attempt of this run reserved a build number for it.")
    assert "Re-run failed jobs or Re-run all jobs on the run that built it" in message
    assert gh.numbers(2) == [1, 2]
    # A release that is not out yet (its first run failed before it published) is built by a new run.
    code, out = release(tmp_path, gh, "v2.0.1", lines["c"], run_id="8004", release_exists="false")
    assert (code, out["version-code"]) == (0, "200000003")
    # The formula gives the tag its one code in every run, and does not read it.
    code, out = release(tmp_path, FakeGitHub(), "v2.0.1", lines["c"], run_id="8005", release_exists="true", version_code_scheme="")
    assert (code, out["version-code"]) == (0, "200050100")
    code, _ = release(tmp_path, FakeGitHub(), "v2.0.1", lines["c"], release_exists="yes")
    assert code == 1
    assert "release-exists must be true or false, got 'yes'" in error_of(capsys)


def test_a_run_whose_reservation_was_made_but_not_answered_finds_it(repo, tmp_path):
    # The ref was created and the answer got lost: the plan failed, and its job is run again.
    git(repo, "tag", "v2.0.0")
    target = commit(repo, "Fix (#1)")
    origin(repo, "develop")
    gh = FakeGitHub()
    gh.refuse[("POST", "git/refs")] = (502, "Bad Gateway")
    code, _ = qa(tmp_path, gh, target, run_id="9001")
    assert code == 1
    own = next(iter(gh.objects))
    gh.refuse = {}
    gh.refs["refs/build-numbers/2/1"] = own
    _, again = qa(tmp_path, gh, target, run_id="9001")
    assert again["version-code"] == "200000001" and gh.numbers(2) == [1]


def test_the_reservation_names_the_commit_and_the_run(repo, tmp_path):
    git(repo, "tag", "v2.0.0")
    target = commit(repo, "Fix (#1)")
    origin(repo, "develop")
    gh = FakeGitHub()
    qa(tmp_path, gh, target, run_id="9001")
    [(sha, body)] = gh.objects.items()
    # One object for the run: its build number and the number in its name.
    assert gh.refs == {"refs/build-numbers/2/1": sha, "refs/qa-numbers/2.0.1/1": sha}
    seconds = int(git(repo, "show", "-s", "--format=%ct", target))
    assert body == {
        "tag": "build-number",
        "message": "The build number of run 9001 of acme/app",
        "object": target,
        "type": "commit",
        "tagger": {
            "name": "github-actions[bot]",
            "email": "41898282+github-actions[bot]@users.noreply.github.com",
            # The time of the commit: the same in every attempt of the run.
            "date": datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }


def test_numbers_behind_the_published_builds_stop_the_build_before_it_reserves(repo, tmp_path, capsys):
    git(repo, "tag", "v2.0.0")
    target = commit(repo, "Fix (#1)")
    origin(repo, "develop")
    gh = FakeGitHub()
    gh.reserve_by_hand(2, 7)
    # A release made with the formula after the switch: above every number reserved.
    gh.release("v2.0.0", "HeySleep_release_2.0.0_200050000.apk")
    code, out = qa(tmp_path, gh, target)
    assert (code, out) == (1, {})
    message = error_of(capsys)
    assert "which is versionCode 200000008, and 200050000 is out already: in the name of HeySleep_release_2.0.0_200050000.apk, an asset of the release v2.0.0" in message
    assert gh.numbers(2) == [7] and ("POST", "git/refs") not in gh.requests
    # The same for a rolling build, whatever its file is called.
    gh.releases = []
    rolling(repo, "qa-develop-ble-test", "2.0.1-ble-test.1", 200_050_001, target)
    code, _ = qa(tmp_path, gh, target, run_id="7002")
    assert code == 1
    assert "200050001 is out already: the build 2.0.1-ble-test.1 that qa-develop-ble-test holds" in error_of(capsys)
    # The floor the message names lets the builds go on above it.
    code, out = qa(tmp_path, gh, target, run_id="7003", version_code_floor="200050001")
    assert (code, out["version-code"]) == (0, "200050002")


def test_label_builds_are_counted_from_the_names_of_the_rolling_builds(lines, repo, tmp_path):
    #   develop:      base(v1.13.0) ── a ── b(v2.0.0) ── c
    gh = FakeGitHub()
    # One label build after v2.0.0 under the formula, and a QA build of the same version.
    rolling(repo, "qa-develop-knox", "2.0.1-knox.1", 200_050_001, lines["c"])
    rolling(repo, "qa-develop", "2.0.1-qa.60", 200_000_060, lines["c"])
    _, out = qa(tmp_path, gh, lines["c"], label="ble-test")
    assert (out["version-name"], out["version-code"], out["kind"], out["number"]) == ("2.0.1-ble-test.2", "200050002", "label", "2")
    # Past 99 label builds after a release: the formula has no code for the hundredth, a build number has.
    rolling(repo, "qa-develop-sweet", "2.0.1-sweet.99", 200_050_002, lines["c"])
    code, out = qa(tmp_path, gh, lines["c"], label="ble-test", run_id="7002")
    assert (code, out["version-name"], out["version-code"]) == (0, "2.0.1-ble-test.100", "200050003")
    # A label of another line, after another release, is not counted.
    rolling(repo, "qa-release-1.x-knox", "1.13.2-knox.5", 101_350_105, lines["h2"])
    _, out = qa(tmp_path, gh, lines["h2"], ref="refs/heads/release/1.x", label="knox", run_id="7003")
    assert out["version-name"] == "1.13.2-knox.6"
    # A label build reserves no QA number: its k comes from the tags, as before.
    assert gh.qa_numbers() == []


def test_qa_sequence_is_not_read_with_build_numbers(repo, tmp_path, capsys):
    git(repo, "tag", "v2.0.0")
    target = commit(repo, "Fix (#1)")
    origin(repo, "develop")
    gh = FakeGitHub()
    # The run number goes on across versions, and the name counts the QA builds of its version: the
    # formula's ceiling of 48999, or a sequence that is no number, is nothing to it.
    names = [qa(tmp_path, gh, target, sequence=sequence, run_id=run_id)[1]["version-name"] for run_id, sequence in (("7001", "50000"), ("7002", ""), ("7003", "abc"))]
    assert names == ["2.0.1-qa.1", "2.0.1-qa.2", "2.0.1-qa.3"]
    assert "::warning" not in capsys.readouterr().out
    # An offset is not read either, and says so; 0 is what android-qa.yml passes by default.
    code, out = qa(tmp_path, gh, target, sequence="5", qa_sequence_offset="-5", run_id="7004")
    assert (code, out["version-name"], out["number"]) == (0, "2.0.1-qa.4", "4")
    assert (
        "::warning title=release-version::qa-sequence-offset is -5, and version-code-scheme is build-number: the number "
        "of a QA build counts the QA builds of its version, and neither qa-sequence nor the offset is read. Take "
        "sequence-offset out of the calling file"
    ) in capsys.readouterr().out
    _, out = qa(tmp_path, gh, target, qa_sequence_offset="0", run_id="7005")
    assert out["version-name"] == "2.0.1-qa.5"
    assert "::warning" not in capsys.readouterr().out


def test_the_release_needs_the_commit_it_builds(repo, tmp_path, capsys):
    git(repo, "tag", "v2.0.0")
    code, _ = run(tmp_path, FakeGitHub(), operation="code", kind="release", version="v2.0.0")
    assert code == 1
    assert "sha is the commit that is built, and the build-number scheme needs it to reserve a number" in error_of(capsys)


# --- several apps in one release --------------------------------------------------------------------

# sleephub-android builds two apps from one tag, one per run: SleepHub, and SweetSleep with an
# applicationId of its own (ai.asleep.sweetsleep). Their codes keep no order with each other.
SLEEPHUB = "SleepHub_*.apk"
SWEETSLEEP = "SweetSleep_*.apk"


def add_asset(gh, tag, name):
    """Attach an APK to a release that exists, as gh release upload does."""
    (published,) = [item for item in gh.releases if item["tag_name"] == tag]
    published["assets"].append({"name": name})


def test_an_app_new_to_a_published_release_takes_a_number_of_its_own(repo, tmp_path, capsys):
    target = commit(repo, "Feature (#1)")
    gh = FakeGitHub()
    # The SleepHub run publishes the release; it was not out when its plan ran.
    _, first = release(tmp_path, gh, "v1.12.0", target, run_id="6001", release_exists="false", asset_pattern=SLEEPHUB)
    assert first["version-code"] == "100000001"
    gh.release("v1.12.0", "SleepHub_release_1.12.0_100000001.apk")
    # The SweetSleep run of the same tag: the release holds no APK of it.
    capsys.readouterr()
    code, sweet = release(tmp_path, gh, "v1.12.0", target, run_id="6002", release_exists="true", asset_pattern=SWEETSLEEP)
    assert (code, sweet["version-code"]) == (0, "100000002")
    assert "v1.12.0 has a GitHub Release, and none of its assets matches asset-pattern SweetSleep_*.apk" in capsys.readouterr().out
    add_asset(gh, "v1.12.0", "SweetSleep_release_1.12.0_100000002.apk")
    # That run, run again once its APK is out: the same number.
    code, again = release(tmp_path, gh, "v1.12.0", target, run_id="6002", release_exists="true", asset_pattern=SWEETSLEEP)
    assert (code, again["version-code"]) == (0, "100000002")
    # A new run of either app: refused before it reserves, as a released tag is without the pattern.
    capsys.readouterr()
    for run_id, pattern, apk in (
        ("6003", SWEETSLEEP, "SweetSleep_release_1.12.0_100000002.apk"),
        ("6004", SLEEPHUB, "SleepHub_release_1.12.0_100000001.apk"),
    ):
        code, out = release(tmp_path, gh, "v1.12.0", target, run_id=run_id, release_exists="true", asset_pattern=pattern)
        assert (code, out) == (1, {})
        message = error_of(capsys)
        assert message.startswith(
            f"v1.12.0 has a GitHub Release with {apk} already, an APK that asset-pattern {pattern} matches, and no "
            "attempt of this run reserved a build number for it."
        ), message
        assert "Re-run failed jobs or Re-run all jobs on the run that built it" in message
    assert gh.numbers(1) == [1, 2]
    # Without the pattern, or with one of whitespace only, the release is one build, as before.
    for pattern in ("", " \n"):
        code, out = release(tmp_path, gh, "v1.12.0", target, run_id="6005", release_exists="true", asset_pattern=pattern)
        assert (code, out) == (1, {})
        assert error_of(capsys).startswith("v1.12.0 has a GitHub Release already, and no attempt of this run reserved")


@pytest.mark.parametrize(
    ("tag", "others", "later"),
    [
        # A later release or preview of the major holds the app.
        ("v1.12.0", [("v1.13.0", "SweetSleep_release_1.13.0_100000003.apk", "release")], "v1.13.0"),
        ("v1.12.0", [("v1.13.0-preview.1", "SweetSleep_preview_1.13.0-preview.1_100000003.apk", "pre-release")], "v1.13.0-preview.1"),
        ("v1.12.0", [("v1.12.1", "SweetSleep_release_1.12.1_100000003.apk", "release")], "v1.12.1"),
        # A preview is below the release of its version and below the later previews of it.
        ("v1.13.0-preview.1", [("v1.13.0", "SweetSleep_release_1.13.0_100000003.apk", "release")], "v1.13.0"),
        ("v1.13.0-preview.1", [("v1.13.0-preview.2", "SweetSleep_preview_1.13.0-preview.2_100000003.apk", "pre-release")], "v1.13.0-preview.2"),
        ("v1.13.0-preview.9", [("v1.13.0-preview.10", "SweetSleep_preview_1.13.0-preview.10_100000003.apk", "pre-release")], "v1.13.0-preview.10"),
        # Of several, the nearest is named.
        (
            "v1.12.0",
            [("v1.14.0", "SweetSleep_release_1.14.0_100000004.apk", "release"), ("v1.13.0", "SweetSleep_release_1.13.0_100000003.apk", "release")],
            "v1.13.0",
        ),
        # Nothing later holds the app: an earlier version, the previews of the release's own version,
        # another major, a draft, a rolling QA build, another app, a name in another case.
        ("v1.12.0", [("v1.11.0", "SweetSleep_release_1.11.0_100000003.apk", "release")], ""),
        ("v1.13.0", [("v1.13.0-preview.2", "SweetSleep_preview_1.13.0-preview.2_100000003.apk", "pre-release")], ""),
        ("v1.13.0-preview.2", [("v1.13.0-preview.1", "SweetSleep_preview_1.13.0-preview.1_100000003.apk", "pre-release")], ""),
        ("v1.12.0", [("v2.0.0", "SweetSleep_release_2.0.0_200000001.apk", "release")], ""),
        ("v1.12.0", [("v1.13.0", "SweetSleep_release_1.13.0_100000003.apk", "draft")], ""),
        ("v1.12.0", [("qa-develop", "SweetSleep_qa_1.13.1-qa.4_100000004.apk", "pre-release")], ""),
        ("v1.12.0", [("v1.13.0", "SleepHub_release_1.13.0_100000003.apk", "release")], ""),
        ("v1.12.0", [("v1.13.0", "sweetsleep_release_1.13.0_100000003.apk", "release")], ""),
    ],
)
def test_an_app_is_added_to_a_release_only_while_no_later_version_of_its_major_holds_it(repo, tmp_path, capsys, tag, others, later):
    # A first build of SweetSleep for the tag takes the next number of the major: above every build
    # so far, also above a later version of SweetSleep that is out, which would then be the lower.
    target = commit(repo, "Feature (#1)")
    gh = FakeGitHub()
    kind = "preview" if "-preview." in tag else "release"
    gh.release(tag, f"SleepHub_{kind}_{tag[1:]}_100000001.apk", prerelease=kind == "preview")
    for other, asset, what in others:
        gh.release(other, asset, prerelease=what == "pre-release", draft=what == "draft")
    gh.reserve_by_hand(1, 9)
    capsys.readouterr()
    code, out = release(tmp_path, gh, tag, target, kind=kind, release_exists="true", asset_pattern=SWEETSLEEP)
    if later:
        assert (code, out) == (1, {})
        message = error_of(capsys)
        assert message.startswith(
            f"{tag} has a GitHub Release without an APK that asset-pattern SweetSleep_*.apk matches, and {later}, a "
            "later version of major 1, holds "
        ), message
        assert "Nothing is built." in message
        assert gh.numbers(1) == [9] and ("POST", "git/refs") not in gh.requests
    else:
        assert (code, out["version-code"]) == (0, "100000010"), capsys.readouterr().out


def test_a_run_that_reserved_before_a_later_version_got_the_app_keeps_its_number(repo, tmp_path, capsys):
    target = commit(repo, "Feature (#1)")
    gh = FakeGitHub()
    gh.release("v1.12.0", "SleepHub_release_1.12.0_100000001.apk")
    gh.reserve_by_hand(1, 1)
    _, first = release(tmp_path, gh, "v1.12.0", target, run_id="6101", release_exists="true", asset_pattern=SWEETSLEEP)
    assert first["version-code"] == "100000002"
    # Its publish failed, and SweetSleep 1.13.0 came out meanwhile, with a higher number.
    gh.release("v1.13.0", "SweetSleep_release_1.13.0_100000003.apk")
    gh.reserve_by_hand(1, 3)
    # The run, run again, keeps a number below that release; a new run is refused.
    code, again = release(tmp_path, gh, "v1.12.0", target, run_id="6101", release_exists="true", asset_pattern=SWEETSLEEP)
    assert (code, again["version-code"]) == (0, "100000002")
    code, out = release(tmp_path, gh, "v1.12.0", target, run_id="6102", release_exists="true", asset_pattern=SWEETSLEEP)
    assert (code, out) == (1, {})
    assert "and v1.13.0, a later version of major 1, holds SweetSleep_release_1.13.0_100000003.apk" in error_of(capsys)
    assert gh.numbers(1) == [1, 2, 3]


def test_the_assets_are_read_only_for_a_released_tag_and_with_build_numbers(lines, repo, tmp_path, capsys):
    git(repo, "tag", "v2.0.1", lines["c"])
    gh = FakeGitHub()
    gh.release("v2.0.1", "SleepHub_release_2.0.1_200050100.apk")
    # A tag without a release yet: the first run of any app, as without the pattern.
    code, out = release(tmp_path, gh, "v2.0.1", lines["c"], release_exists="false", asset_pattern=SWEETSLEEP)
    assert (code, out["version-code"]) == (0, "200050101")
    assert gh.requests.count(("GET", "releases")) == 1, "only the safety rule reads the releases"
    # The formula gives the tag its one code in every run, and reads neither.
    gh = FakeGitHub()
    code, out = release(tmp_path, gh, "v2.0.1", lines["c"], release_exists="true", asset_pattern=SWEETSLEEP, version_code_scheme="")
    assert (code, out["version-code"]) == (0, "200050100")
    assert ("GET", "releases") not in gh.requests
    # A token that may not read the releases stops the plan before it reserves.
    gh = FakeGitHub()
    gh.refuse[("GET", "releases")] = (403, "Resource not accessible by integration")
    capsys.readouterr()
    code, out = release(tmp_path, gh, "v2.0.1", lines["c"], release_exists="true", asset_pattern=SWEETSLEEP)
    assert (code, out) == (1, {})
    assert error_of(capsys).startswith("cannot list the releases: HTTP 403: Resource not accessible by integration.")
    assert gh.refs == {}


def apks(codes):
    """A stand-in for reading the versionCode of a release asset from its APK: (tag, name) -> code."""

    def reader(tag, asset):
        reader.read.append((tag, asset["name"]))
        return codes[(tag, asset["name"])]

    reader.read = []
    return reader


def set_assets(gh, tag, *names):
    """Replace every asset of a release, as the publish job of a QA build without asset-pattern does."""
    (published,) = [item for item in gh.releases if item["tag_name"] == tag]
    published["assets"] = [{"name": name} for name in names]


def reused_refs(gh):
    return sorted(name for name in gh.refs if name.startswith("refs/reused-numbers/"))


SWEET_REUSED = "refs/reused-numbers/2/30/app-SweetSleep_-2a-2eapk"
SLEEPHUB_REUSED = "refs/reused-numbers/2/30/app-SleepHub_-2a-2eapk"
SLEEPHUB_APK = ("v2.1.0", "SleepHub_release_2.1.0.apk")
SWEETSLEEP_APK = ("v2.1.0", "SweetSleep_release_2.1.0.apk")


def released_with_sleephub(repo, tmp_path, gh, number=30):
    """v2.1.0 released with SleepHub alone by its own run, as build number `number` of major 2."""
    target = commit(repo, "Feature (#1)")
    gh.reserve_by_hand(2, number - 1)
    code, first = release(tmp_path, gh, "v2.1.0", target, run_id="8001", release_exists="false", asset_pattern=SLEEPHUB)
    assert (code, first["version-code"]) == (0, str(200_000_000 + number))
    gh.release("v2.1.0", "SleepHub_release_2.1.0.apk")
    return target


def add_later(tmp_path, gh, target, read, run_id="8101", tag="v2.1.0", **inputs):
    """The run of release.yml that adds SweetSleep to the release of a tag, with SleepHub's number."""
    inputs = {"release_exists": "true", "asset_pattern": SWEETSLEEP, "version_code_from": SLEEPHUB, **inputs}
    return release(tmp_path, gh, tag, target, run_id=run_id, read=read, **inputs)


def qa_build_of_sleephub_alone_after_the_release(repo, gh):
    gh.reserve_by_hand(2, 33)
    rolling(repo, "qa-develop", "2.1.1-qa.3", 200_000_033)
    gh.release("qa-develop", "SleepHub_2.1.1-qa.3.apk", prerelease=True)


def other_releases_of_both_apps(gh):
    gh.release("v2.0.0", "SleepHub_release_2.0.0.apk", "SweetSleep_release_2.0.0.apk")
    gh.release("v1.13.2", "SweetSleep_release_1.13.2.apk")
    gh.release("v2.0.1", "SweetSleep_release_2.0.1.apk", draft=True)


def test_an_app_added_later_gets_the_number_of_the_other_apps_apk_and_keeps_it_when_run_again(repo, tmp_path, capsys):
    gh = FakeGitHub()
    target = released_with_sleephub(repo, tmp_path, gh)
    qa_build_of_sleephub_alone_after_the_release(repo, gh)
    other_releases_of_both_apps(gh)
    reader = apks({SLEEPHUB_APK: 200_000_030, SWEETSLEEP_APK: 200_000_030})
    capsys.readouterr()
    code, out = add_later(tmp_path, gh, target, reader)
    log = capsys.readouterr().out
    assert (code, out["version-code"], out["build-number"], out["version-code-from"]) == (0, "200000030", "30", SLEEPHUB), log
    assert (out["version-code-scheme"], out["version-code-reused"]) == ("build-number", "true")
    assert (
        "SleepHub_release_2.1.0.apk of v2.1.0: versionCode 200000030, build number 30 of major 2, reserved as "
        f"refs/build-numbers/2/30 for {target[:7]}."
    ) in log
    assert f"Build number 30 of major 2, reused and recorded as {SWEET_REUSED}/1: the number SleepHub_release_2.1.0.apk" in log
    assert "No number is reserved." in log
    assert reader.read == [SLEEPHUB_APK]
    reserved_by_the_release_and_the_qa_build = [29, 30, 33]
    assert gh.numbers(2) == reserved_by_the_release_and_the_qa_build
    assert reused_refs(gh) == [f"{SWEET_REUSED}/1"]
    code, run_again_before_it_published = add_later(tmp_path, gh, target, reader)
    assert (code, run_again_before_it_published["version-code"]) == (0, "200000030")
    assert f"Build number 30 of major 2 ({SWEET_REUSED}/1): reused for SweetSleep_*.apk by an earlier attempt of this run, and kept." in capsys.readouterr().out
    add_asset(gh, *SWEETSLEEP_APK)
    code, run_again_once_its_apk_is_out = add_later(tmp_path, gh, target, reader)
    assert (code, run_again_once_its_apk_is_out["version-code"]) == (0, "200000030")
    capsys.readouterr()
    downloaded_before_the_new_run = [SLEEPHUB_APK, SLEEPHUB_APK, SLEEPHUB_APK, SWEETSLEEP_APK]
    assert reader.read == downloaded_before_the_new_run
    code, new_run = add_later(tmp_path, gh, target, reader, run_id="8102")
    assert (code, new_run) == (1, {})
    message = error_of(capsys)
    assert message.startswith(
        "v2.1.0 has a GitHub Release with SweetSleep_release_2.1.0.apk already, an APK that asset-pattern "
        "SweetSleep_*.apk matches, and no attempt of this run reused a build number for it."
    ), message
    assert "Re-run failed jobs or Re-run all jobs on the run that built it" in message
    assert reader.read == downloaded_before_the_new_run
    assert gh.numbers(2) == reserved_by_the_release_and_the_qa_build and reused_refs(gh) == [f"{SWEET_REUSED}/1"]


def test_a_new_run_after_one_that_did_not_publish_reuses_the_same_number(repo, tmp_path):
    gh = FakeGitHub()
    target = released_with_sleephub(repo, tmp_path, gh)
    reader = apks({SLEEPHUB_APK: 200_000_030})
    run_that_did_not_publish, new_run = "8201", "8202"
    assert add_later(tmp_path, gh, target, reader, run_id=run_that_did_not_publish)[1]["version-code"] == "200000030"
    assert add_later(tmp_path, gh, target, reader, run_id=new_run)[1]["version-code"] == "200000030"
    one_record_per_run = [f"{SWEET_REUSED}/1", f"{SWEET_REUSED}/2"]
    assert reused_refs(gh) == one_record_per_run
    assert gh.numbers(2) == [29, 30]
    assert add_later(tmp_path, gh, target, reader, run_id=run_that_did_not_publish)[1]["version-code"] == "200000030"
    assert reused_refs(gh) == one_record_per_run


def qa_build_of_both_brands_after_the_release(repo, gh):
    gh.reserve_by_hand(2, 35)
    rolling(repo, "qa-develop", "2.1.1-qa.5", 200_000_035)
    gh.release("qa-develop", "SleepHub_2.1.1-qa.5.apk", "SweetSleep_2.1.1-qa.5.apk", prerelease=True)


def qa_tag_with_a_line_per_app_above_the_number(repo, gh, target):
    gh.reserve_by_hand(2, 36)
    rolling_apps(
        repo, "qa-develop", "2.1.1-qa.6 (200000036)",
        f"{SLEEPHUB} 2.1.1-qa.6 (200000036) {target}", f"{SWEETSLEEP} 2.1.1-qa.2 (200000034) {target}",
    )
    set_assets(gh, "qa-develop", "SleepHub_2.1.1-qa.6.apk", "SweetSleep_2.1.1-qa.2.apk")


def test_an_app_added_later_gets_the_number_whatever_qa_build_of_the_next_version_a_rolling_tag_holds(repo, tmp_path):
    gh = FakeGitHub()
    target = released_with_sleephub(repo, tmp_path, gh)
    qa_build_of_both_brands_after_the_release(repo, gh)
    reader = apks({SLEEPHUB_APK: 200_000_030})
    code, out = add_later(tmp_path, gh, target, reader)
    assert (code, out["version-code"], out["version-code-reused"]) == (0, "200000030", "true")
    assert reused_refs(gh) == [f"{SWEET_REUSED}/1"] and gh.numbers(2) == [29, 30, 35]
    qa_tag_with_a_line_per_app_above_the_number(repo, gh, target)
    a_floor_above_the_number = "200000040"
    code, out = add_later(tmp_path, gh, target, reader, run_id="8102", version_code_floor=a_floor_above_the_number)
    assert (code, out["version-code"]) == (0, "200000030")
    assert reader.read == [SLEEPHUB_APK, SLEEPHUB_APK]


def test_a_run_that_reused_a_number_builds_with_it_again_after_a_higher_qa_build(repo, tmp_path, capsys):
    gh = FakeGitHub()
    target = released_with_sleephub(repo, tmp_path, gh)
    reader = apks({SLEEPHUB_APK: 200_000_030, SWEETSLEEP_APK: 200_000_030})
    assert add_later(tmp_path, gh, target, reader)[1]["version-code"] == "200000030"
    qa_build_of_both_brands_after_the_release(repo, gh)
    add_asset(gh, *SWEETSLEEP_APK)
    capsys.readouterr()
    code, out = add_later(tmp_path, gh, target, reader)
    assert (code, out["version-code"]) == (0, "200000030")
    assert f"Build number 30 of major 2 ({SWEET_REUSED}/1): reused for SweetSleep_*.apk by an earlier attempt of this run, and kept." in capsys.readouterr().out
    assert reused_refs(gh) == [f"{SWEET_REUSED}/1"] and gh.numbers(2) == [29, 30, 35]


def alone(tmp_path, gh, target, pattern, other, read, run_id, release_exists="true", **inputs):
    """A run of one brand, as sleephub-android's release.yml starts it: the other brand's APK lends its number."""
    return release(
        tmp_path, gh, "v2.1.0", target, run_id=run_id, read=read, release_exists=release_exists,
        asset_pattern=pattern, version_code_from=other, **inputs,
    )


@pytest.mark.parametrize("exists", ["false", ""])
def test_the_first_app_of_a_tag_without_a_release_takes_a_number_of_its_own_with_version_code_from(repo, tmp_path, capsys, exists):
    gh = FakeGitHub()
    target = commit(repo, "Feature (#1)")
    gh.reserve_by_hand(2, 29)
    reader = apks({})
    capsys.readouterr()
    code, out = alone(tmp_path, gh, target, SWEETSLEEP, SLEEPHUB, reader, "8001", release_exists=exists)
    log = capsys.readouterr().out
    assert (code, out["version-code"], out["version-code-from"], out["version-code-reused"]) == (0, "200000030", SLEEPHUB, "false"), log
    assert "v2.1.0 has no " in log and "SweetSleep_*.apk takes a build number of its own." in log
    assert "Build number 30 of major 2, reserved as refs/build-numbers/2/30" in log
    assert reader.read == [] and reused_refs(gh) == [] and gh.numbers(2) == [29, 30]
    gh.release("v2.1.0", "SweetSleep_release_2.1.0.apk")
    code, again = alone(tmp_path, gh, target, SWEETSLEEP, SLEEPHUB, reader, "8001")
    assert (code, again["version-code"], again["version-code-reused"]) == (0, "200000030", "false")
    assert reader.read == [] and gh.numbers(2) == [29, 30]


def test_a_release_without_an_apk_of_version_code_from_gives_the_app_a_number_of_its_own(repo, tmp_path, capsys):
    gh = FakeGitHub()
    target = released_with_sleephub(repo, tmp_path, gh)
    reader = apks({})
    capsys.readouterr()
    code, out = alone(tmp_path, gh, target, SWEETSLEEP, "Sleep_*.apk", reader, "8101")
    log = capsys.readouterr().out
    assert (code, out["version-code"], out["version-code-reused"]) == (0, "200000031", "false"), log
    assert (
        "v2.1.0 has a GitHub Release, and none of its assets matches version-code-from Sleep_*.apk (its assets: "
        "SleepHub_release_2.1.0.apk): SweetSleep_*.apk takes a build number of its own."
    ) in log
    gh.releases = [item for item in gh.releases if item["tag_name"] != "v2.1.0"]
    gh.release("v2.1.0", "SleepHub_release_2.1.0.apk", draft=True)
    code, with_only_a_draft = alone(tmp_path, gh, target, SWEETSLEEP, SLEEPHUB, reader, "8102")
    assert (code, with_only_a_draft["version-code"], with_only_a_draft["version-code-reused"]) == (0, "200000032", "false")
    assert "v2.1.0 has no published GitHub Release (a draft is not out), so no APK of SleepHub_*.apk lends its build number" in capsys.readouterr().out
    assert reader.read == [] and reused_refs(gh) == []


def test_sleephub_added_to_a_release_of_sweetsleep_alone_gets_its_number(repo, tmp_path, capsys):
    gh = FakeGitHub()
    target = commit(repo, "Feature (#1)")
    gh.reserve_by_hand(2, 29)
    assert alone(tmp_path, gh, target, SWEETSLEEP, SLEEPHUB, apks({}), "8001", release_exists="false")[1]["version-code"] == "200000030"
    gh.release("v2.1.0", "SweetSleep_release_2.1.0.apk")
    reader = apks({SWEETSLEEP_APK: 200_000_030})
    capsys.readouterr()
    code, out = alone(tmp_path, gh, target, SLEEPHUB, SWEETSLEEP, reader, "8101")
    log = capsys.readouterr().out
    assert (code, out["version-code"], out["version-code-from"], out["version-code-reused"]) == (0, "200000030", SWEETSLEEP, "true"), log
    assert reader.read == [SWEETSLEEP_APK]
    assert reused_refs(gh) == [f"{SLEEPHUB_REUSED}/1"] and gh.numbers(2) == [29, 30]


def test_sleephub_added_to_a_release_of_sweetsleep_alone_after_qa_builds_of_the_next_version_gets_its_number_also_run_again(repo, tmp_path):
    gh = FakeGitHub()
    target = commit(repo, "Feature (#1)")
    gh.reserve_by_hand(2, 29)
    assert alone(tmp_path, gh, target, SWEETSLEEP, SLEEPHUB, apks({}), "8001", release_exists="false")[1]["version-code"] == "200000030"
    gh.release("v2.1.0", "SweetSleep_release_2.1.0.apk")
    gh.reserve_by_hand(2, 31)
    rolling(repo, "qa-develop", "2.1.1-qa.1", 200_000_031)
    gh.release("qa-develop", "SleepHub_2.1.1-qa.1.apk", prerelease=True)
    reader = apks({SWEETSLEEP_APK: 200_000_030, SLEEPHUB_APK: 200_000_030})
    code, out = alone(tmp_path, gh, target, SLEEPHUB, SWEETSLEEP, reader, "8101")
    assert (code, out["version-code"], out["version-code-reused"]) == (0, "200000030", "true")
    assert reused_refs(gh) == [f"{SLEEPHUB_REUSED}/1"] and gh.numbers(2) == [29, 30, 31]
    gh.reserve_by_hand(2, 32)
    rolling(repo, "qa-develop", "2.1.1-qa.2", 200_000_032)
    set_assets(gh, "qa-develop", "SleepHub_2.1.1-qa.2.apk")
    add_asset(gh, "v2.1.0", "SleepHub_release_2.1.0.apk")
    code, again = alone(tmp_path, gh, target, SLEEPHUB, SWEETSLEEP, reader, "8101")
    assert (code, again["version-code"]) == (0, "200000030")
    assert reused_refs(gh) == [f"{SLEEPHUB_REUSED}/1"] and gh.numbers(2) == [29, 30, 31, 32]


def test_the_run_that_lent_its_number_keeps_it_when_it_is_run_again(repo, tmp_path, capsys):
    gh = FakeGitHub()
    target = commit(repo, "Feature (#1)")
    gh.reserve_by_hand(2, 29)
    assert alone(tmp_path, gh, target, SLEEPHUB, SWEETSLEEP, apks({}), "8001", release_exists="false")[1]["version-code"] == "200000030"
    gh.release("v2.1.0", "SleepHub_release_2.1.0.apk")
    assert alone(tmp_path, gh, target, SWEETSLEEP, SLEEPHUB, apks({SLEEPHUB_APK: 200_000_030}), "8101")[1]["version-code"] == "200000030"
    add_asset(gh, "v2.1.0", "SweetSleep_release_2.1.0.apk")
    reader = apks({SWEETSLEEP_APK: 200_000_030})
    capsys.readouterr()
    code, out = alone(tmp_path, gh, target, SLEEPHUB, SWEETSLEEP, reader, "8001")
    log = capsys.readouterr().out
    assert (code, out["version-code"], out["version-code-reused"]) == (0, "200000030", "false"), log
    assert (
        "An earlier attempt of this run reserved refs/build-numbers/2/30 for SleepHub_*.apk: the run keeps that number, "
        "and does not reuse the one of SweetSleep_*.apk."
    ) in log
    assert "Build number 30 of major 2 (refs/build-numbers/2/30): reserved by an earlier attempt of this run, and kept." in log
    assert reader.read == [] and gh.numbers(2) == [29, 30] and reused_refs(gh) == [f"{SWEET_REUSED}/1"]
    code, new_run = alone(tmp_path, gh, target, SLEEPHUB, SWEETSLEEP, reader, "8002")
    assert (code, new_run) == (1, {})
    assert error_of(capsys).startswith(
        "v2.1.0 has a GitHub Release with SleepHub_release_2.1.0.apk already, an APK that asset-pattern SleepHub_*.apk "
        "matches, and no attempt of this run reused a build number for it."
    )


def test_a_run_that_reserved_before_the_other_app_was_released_keeps_its_number_and_a_new_run_gets_the_other_apps(repo, tmp_path, capsys):
    gh = FakeGitHub()
    target = commit(repo, "Feature (#1)")
    gh.reserve_by_hand(2, 29)
    assert alone(tmp_path, gh, target, SLEEPHUB, SWEETSLEEP, apks({}), "8001", release_exists="false")[1]["version-code"] == "200000030"
    assert alone(tmp_path, gh, target, SWEETSLEEP, SLEEPHUB, apks({}), "8101", release_exists="false")[1]["version-code"] == "200000031"
    gh.release("v2.1.0", "SweetSleep_release_2.1.0.apk")
    reader = apks({SWEETSLEEP_APK: 200_000_031})
    code, run_again_with_two_versioncodes_in_the_release = alone(tmp_path, gh, target, SLEEPHUB, SWEETSLEEP, reader, "8001")
    assert (code, run_again_with_two_versioncodes_in_the_release["version-code"]) == (0, "200000030")
    assert run_again_with_two_versioncodes_in_the_release["version-code-reused"] == "false"
    assert reader.read == [] and gh.numbers(2) == [29, 30, 31] and reused_refs(gh) == []
    code, new_run_with_one_versioncode = alone(tmp_path, gh, target, SLEEPHUB, SWEETSLEEP, reader, "8002")
    assert (code, new_run_with_one_versioncode["version-code"], new_run_with_one_versioncode["version-code-reused"]) == (0, "200000031", "true")
    assert gh.numbers(2) == [29, 30, 31]


def sweetsleep_reused_the_number_and_failed_then_sleephub_lost_its_apk(repo, tmp_path, gh):
    target = released_with_sleephub(repo, tmp_path, gh)
    assert add_later(tmp_path, gh, target, apks({SLEEPHUB_APK: 200_000_030}))[1]["version-code"] == "200000030"
    set_assets(gh, "v2.1.0")
    return target


def sleephub_run_again_puts_its_apk_back(tmp_path, gh, target):
    code, lender = alone(tmp_path, gh, target, SLEEPHUB, SWEETSLEEP, apks({}), "8001")
    assert (code, lender["version-code"], lender["version-code-reused"]) == (0, "200000030", "false")
    add_asset(gh, *SLEEPHUB_APK)


def test_a_run_that_reused_a_number_takes_none_of_its_own_while_the_apk_that_lent_it_is_gone_and_builds_with_it_once_back(repo, tmp_path, capsys):
    gh = FakeGitHub()
    target = sweetsleep_reused_the_number_and_failed_then_sleephub_lost_its_apk(repo, tmp_path, gh)
    reader = apks({SLEEPHUB_APK: 200_000_030})
    capsys.readouterr()
    code, out = add_later(tmp_path, gh, target, reader)
    assert (code, out) == (1, {})
    message = error_of(capsys)
    assert message.startswith(
        "An earlier attempt of this run reused build number 30 of major 2 (versionCode 200000030) for "
        f"SweetSleep_*.apk, recorded as {SWEET_REUSED}/1, and the GitHub Release of v2.1.0 holds no APK that "
        "version-code-from SleepHub_*.apk or asset-pattern SweetSleep_*.apk matches now (its assets: none)."
    ), message
    assert (
        "Put the APK of SleepHub_*.apk with versionCode 200000030 back on the GitHub Release of v2.1.0 by running "
        "again the run that built it (Re-run failed jobs or Re-run all jobs), then run this run again. Do that before "
        "any new run of SweetSleep_*.apk for v2.1.0: while no APK of SleepHub_*.apk is there, a new run has no number "
        "to reuse and takes one of its own\n"
    ) in message
    assert gh.numbers(2) == [29, 30] and reused_refs(gh) == [f"{SWEET_REUSED}/1"]
    sleephub_run_again_puts_its_apk_back(tmp_path, gh, target)
    capsys.readouterr()
    code, out = add_later(tmp_path, gh, target, reader)
    assert (code, out["version-code"], out["version-code-reused"]) == (0, "200000030", "true")
    assert f"Build number 30 of major 2 ({SWEET_REUSED}/1): reused for SweetSleep_*.apk by an earlier attempt of this run, and kept." in capsys.readouterr().out
    assert gh.numbers(2) == [29, 30] and reused_refs(gh) == [f"{SWEET_REUSED}/1"]


def test_a_new_run_while_the_apk_that_would_lend_its_number_is_gone_takes_a_number_of_its_own(repo, tmp_path, capsys):
    gh = FakeGitHub()
    target = sweetsleep_reused_the_number_and_failed_then_sleephub_lost_its_apk(repo, tmp_path, gh)
    reader = apks({})
    capsys.readouterr()
    code, new_run = add_later(tmp_path, gh, target, reader, run_id="8102")
    log = capsys.readouterr().out
    assert (code, new_run["version-code"], new_run["version-code-reused"]) == (0, "200000031", "false"), log
    assert (
        "v2.1.0 has a GitHub Release, and none of its assets matches version-code-from SleepHub_*.apk (its assets: "
        "none): SweetSleep_*.apk takes a build number of its own."
    ) in log
    assert reader.read == [] and gh.numbers(2) == [29, 30, 31] and reused_refs(gh) == [f"{SWEET_REUSED}/1"]


def sweetsleep_published_by_a_new_run_with_a_number_of_its_own_while_sleephub_was_gone(repo, tmp_path, gh):
    target = sweetsleep_reused_the_number_and_failed_then_sleephub_lost_its_apk(repo, tmp_path, gh)
    assert add_later(tmp_path, gh, target, apks({}), run_id="8102")[1]["version-code"] == "200000031"
    add_asset(gh, *SWEETSLEEP_APK)
    return target


def test_a_run_that_reused_a_number_is_refused_once_a_new_run_published_its_app_with_another_number(repo, tmp_path, capsys):
    gh = FakeGitHub()
    target = sweetsleep_published_by_a_new_run_with_a_number_of_its_own_while_sleephub_was_gone(repo, tmp_path, gh)
    sleephub_run_again_puts_its_apk_back(tmp_path, gh, target)
    reader = apks({SLEEPHUB_APK: 200_000_030, SWEETSLEEP_APK: 200_000_031})
    capsys.readouterr()
    code, out = add_later(tmp_path, gh, target, reader)
    assert (code, out) == (1, {})
    message = error_of(capsys)
    assert message.startswith(
        "An earlier attempt of this run reused build number 30 of major 2 (versionCode 200000030) for "
        f"SweetSleep_*.apk, recorded as {SWEET_REUSED}/1, and SweetSleep_release_2.1.0.apk (200000031) of v2.1.0, an "
        "APK that asset-pattern SweetSleep_*.apk matches, is out with another versionCode: another run published it. "
        "Built with 200000030, it would be replaced (gh release upload --clobber), and a device that took it does not "
        "take a lower versionCode. Nothing is built."
    ), message
    assert (
        "To build SweetSleep_*.apk of v2.1.0 again, run again the run that built it (Re-run failed jobs or Re-run all "
        "jobs on that run), not this one\n"
    ) in message
    assert reader.read == [SLEEPHUB_APK, SWEETSLEEP_APK]
    assert gh.numbers(2) == [29, 30, 31] and reused_refs(gh) == [f"{SWEET_REUSED}/1"]


def test_a_run_that_reused_a_number_is_refused_while_its_app_is_out_with_another_number_and_the_lending_apk_is_gone(repo, tmp_path, capsys):
    gh = FakeGitHub()
    target = sweetsleep_published_by_a_new_run_with_a_number_of_its_own_while_sleephub_was_gone(repo, tmp_path, gh)
    reader = apks({SWEETSLEEP_APK: 200_000_031})
    capsys.readouterr()
    code, out = add_later(tmp_path, gh, target, reader)
    assert (code, out) == (1, {})
    assert (
        "and SweetSleep_release_2.1.0.apk (200000031) of v2.1.0, an APK that asset-pattern SweetSleep_*.apk matches, "
        "is out with another versionCode: another run published it."
    ) in error_of(capsys)
    assert reader.read == [SWEETSLEEP_APK]
    assert gh.numbers(2) == [29, 30, 31] and reused_refs(gh) == [f"{SWEET_REUSED}/1"]


OTHER_OBJECT = "e" * 40


def without_release(gh, repo, target):
    gh.releases = [item for item in gh.releases if item["tag_name"] != "v2.1.0"]
    return {"release_exists": "false"}, {}, "and v2.1.0 has no published GitHub Release now (a draft is not out)."


def with_a_draft(gh, repo, target):
    gh.releases = [item for item in gh.releases if item["tag_name"] != "v2.1.0"]
    gh.release("v2.1.0", "SleepHub_release_2.1.0.apk", draft=True)
    return {"release_exists": ""}, {}, "and v2.1.0 has no published GitHub Release now (a draft is not out)."


def with_neither_apk(gh, repo, target):
    set_assets(gh, "v2.1.0", "SleepHub_release_2.1.0.aab", "notes.txt")
    return {}, {}, "matches now (its assets: SleepHub_release_2.1.0.aab, notes.txt)."


def republished_by_a_new_run_with_another_code(gh, repo, target):
    return {}, {SLEEPHUB_APK: 200_000_031}, (
        "and SleepHub_release_2.1.0.apk (200000031) of v2.1.0 has another versionCode now. The run builds with its "
        "number or not at all, and with it the release would hold two versionCodes. Nothing is built. To build "
        "SweetSleep_*.apk with the number the release holds now, start a new run for v2.1.0 instead of running this "
        "one again: a new run reuses it"
    )


def without_its_ref(gh, repo, target):
    del gh.refs["refs/build-numbers/2/30"]
    return {}, {SLEEPHUB_APK: 200_000_030}, "refs/build-numbers/2/30 does not exist"


def with_a_ref_of_another_commit(gh, repo, target):
    other = commit(repo, "Other (#2)")
    gh.objects[OTHER_OBJECT] = {"object": other, "type": "commit"}
    gh.refs["refs/build-numbers/2/30"] = OTHER_OBJECT
    return {}, {SLEEPHUB_APK: 200_000_030}, f"was reserved for the commit {other[:7]}, not for {target[:7]}"


def with_two_numbers(gh, repo, target):
    gh.refs["refs/reused-numbers/2/31/app-SweetSleep_-2a-2eapk/1"] = gh.refs[f"{SWEET_REUSED}/1"]
    return {}, {SLEEPHUB_APK: 200_000_030}, (
        "earlier attempts of this run reused more than one build number of major 2 for SweetSleep_*.apk: "
        f"{SWEET_REUSED}/1, refs/reused-numbers/2/31/app-SweetSleep_-2a-2eapk/1."
    )


@pytest.mark.parametrize(
    "lost",
    [
        without_release, with_a_draft, with_neither_apk, republished_by_a_new_run_with_another_code, without_its_ref,
        with_a_ref_of_another_commit, with_two_numbers,
    ],
)
def test_a_run_that_reused_a_number_builds_with_it_or_not_at_all(repo, tmp_path, capsys, lost):
    gh = FakeGitHub()
    target = released_with_sleephub(repo, tmp_path, gh)
    assert add_later(tmp_path, gh, target, apks({SLEEPHUB_APK: 200_000_030}))[1]["version-code"] == "200000030"
    inputs, codes, said = lost(gh, repo, target)
    numbers_before, records_before = gh.numbers(2), reused_refs(gh)
    capsys.readouterr()
    code, out = add_later(tmp_path, gh, target, apks(codes), **inputs)
    assert (code, out) == (1, {})
    message = error_of(capsys)
    assert said in message, message
    assert gh.numbers(2) == numbers_before and reused_refs(gh) == records_before


def sweetsleep_out_with_the_number_then_sleephub_lost_its_apk(repo, tmp_path, gh):
    target = released_with_sleephub(repo, tmp_path, gh)
    assert add_later(tmp_path, gh, target, apks({SLEEPHUB_APK: 200_000_030}))[1]["version-code"] == "200000030"
    set_assets(gh, *SWEETSLEEP_APK)
    return target


def test_a_run_whose_apk_is_out_builds_with_its_number_again_while_the_apk_that_lent_it_is_gone(repo, tmp_path, capsys):
    gh = FakeGitHub()
    target = sweetsleep_out_with_the_number_then_sleephub_lost_its_apk(repo, tmp_path, gh)
    reader = apks({SWEETSLEEP_APK: 200_000_030})
    capsys.readouterr()
    code, out = add_later(tmp_path, gh, target, reader)
    log = capsys.readouterr().out
    assert (code, out["version-code"], out["version-code-reused"]) == (0, "200000030", "true"), log
    assert (
        "v2.1.0 holds no APK that version-code-from SleepHub_*.apk matches now, and SweetSleep_release_2.1.0.apk, "
        "which this run published, has versionCode 200000030: the run builds with that number again."
    ) in log
    assert f"Build number 30 of major 2 ({SWEET_REUSED}/1): reused for SweetSleep_*.apk by an earlier attempt of this run, and kept." in log
    assert reader.read == [SWEETSLEEP_APK]
    assert gh.numbers(2) == [29, 30] and reused_refs(gh) == [f"{SWEET_REUSED}/1"]


def test_a_new_run_is_refused_while_the_release_holds_the_apps_apk_and_the_apk_that_lent_its_number_is_gone(repo, tmp_path, capsys):
    gh = FakeGitHub()
    target = sweetsleep_out_with_the_number_then_sleephub_lost_its_apk(repo, tmp_path, gh)
    reader = apks({})
    capsys.readouterr()
    code, new_run = add_later(tmp_path, gh, target, reader, run_id="8102")
    assert (code, new_run) == (1, {})
    assert "v2.1.0 has a GitHub Release with SweetSleep_release_2.1.0.apk already" in error_of(capsys)
    assert reader.read == [] and gh.numbers(2) == [29, 30] and reused_refs(gh) == [f"{SWEET_REUSED}/1"]


def test_an_app_added_later_to_a_preview_gets_the_number_of_the_other_apps_apk(repo, tmp_path, capsys):
    gh = FakeGitHub()
    target = commit(repo, "Feature (#1)")
    gh.reserve_by_hand(2, 29)
    tag, apk = "v2.2.0-preview.1", ("v2.2.0-preview.1", "SleepHub_2.2.0-preview.1.apk")
    code, first = release(
        tmp_path, gh, tag, target, kind="preview", run_id="8001", read=apks({}), release_exists="false",
        asset_pattern=SLEEPHUB, version_code_from=SWEETSLEEP,
    )
    assert (code, first["version"], first["version-code"], first["version-code-reused"]) == (0, "2.2.0-preview.1", "200000030", "false")
    gh.release(tag, apk[1], prerelease=True)
    reader = apks({apk: 200_000_030})
    code, out = release(
        tmp_path, gh, tag, target, kind="preview", run_id="8101", read=reader, release_exists="true",
        asset_pattern=SWEETSLEEP, version_code_from=SLEEPHUB,
    )
    assert (code, out["version"], out["version-code"], out["version-code-reused"]) == (0, "2.2.0-preview.1", "200000030", "true")
    assert reader.read == [apk] and gh.numbers(2) == [29, 30] and reused_refs(gh) == [f"{SWEET_REUSED}/1"]


@pytest.mark.parametrize(
    ("status", "said", "hint"),
    [
        (422, "Repository rule violations found", "A ruleset of the repository refuses it: let the job token create refs/reused-numbers/*, or keep those refs out of the ruleset."),
        (403, "Resource not accessible by integration", "The job's token needs contents: write."),
    ],
)
def test_a_reuse_that_cannot_be_recorded_is_refused_and_reserves_nothing(repo, tmp_path, capsys, status, said, hint):
    gh = FakeGitHub()
    target = released_with_sleephub(repo, tmp_path, gh)
    gh.refuse_ref = ("refs/reused-numbers/", status, said)
    capsys.readouterr()
    code, out = add_later(tmp_path, gh, target, apks({SLEEPHUB_APK: 200_000_030}))
    assert (code, out) == (1, {})
    message = error_of(capsys)
    assert message.startswith(f"cannot create {SWEET_REUSED}/1: HTTP {status}: {said}. {hint}"), message
    assert gh.numbers(2) == [29, 30] and reused_refs(gh) == []


def test_a_number_whose_ref_was_made_by_hand_with_the_id_of_the_commit_is_not_reused(repo, tmp_path, capsys):
    gh = FakeGitHub()
    target = released_with_sleephub(repo, tmp_path, gh)
    the_id_of_the_commit_instead_of_a_tag_object = target
    gh.refs["refs/build-numbers/2/30"] = the_id_of_the_commit_instead_of_a_tag_object
    capsys.readouterr()
    code, out = add_later(tmp_path, gh, target, apks({SLEEPHUB_APK: 200_000_030}))
    assert (code, out) == (1, {})
    message = error_of(capsys)
    assert message.startswith(
        f"refs/build-numbers/2/30, which names {target}, does not name a tag object (HTTP 404 for git/tags/{target}): "
        "the reservation of a build names the tag object of its run, and a ref made by hand, or by another tool, "
        "names a commit."
    ), message
    assert "contents: read" not in message and reused_refs(gh) == []


@pytest.mark.parametrize(
    ("another_release_of_the_app", "its_asset_with_a_higher_code_in_its_name_or_not"),
    [
        ("v2.0.0", "SweetSleep_release_2.0.0_200000031.apk"),
        ("v2.0.0", "SweetSleep_release_2.0.0.apk"),
        ("v2.1.0-preview.1", "SweetSleep_2.1.0-preview.1.apk"),
        ("v2.2.0-preview.1", "SweetSleep_2.2.0-preview.1.apk"),
        ("v2.1.1", "SweetSleep_release_2.1.1.apk"),
        ("v2.2.0", "SweetSleep_release_2.2.0.apk"),
    ],
)
def test_an_app_added_later_gets_the_number_whatever_other_releases_of_it_hold_and_downloads_only_the_lending_apk(
    repo, tmp_path, another_release_of_the_app, its_asset_with_a_higher_code_in_its_name_or_not
):
    gh = FakeGitHub()
    target = released_with_sleephub(repo, tmp_path, gh)
    gh.reserve_by_hand(2, 31)
    tag = another_release_of_the_app
    gh.release(tag, its_asset_with_a_higher_code_in_its_name_or_not, prerelease="-preview." in tag)
    reader = apks({SLEEPHUB_APK: 200_000_030})
    code, out = add_later(tmp_path, gh, target, reader)
    assert (code, out["version-code"], out["version-code-reused"]) == (0, "200000030", "true")
    assert reader.read == [SLEEPHUB_APK] and reused_refs(gh) == [f"{SWEET_REUSED}/1"]


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        ({"version_code_scheme": "minor-blocks"}, "version-code-from is SleepHub_*.apk, and version-code-scheme is minor-blocks: with the formula"),
        ({"version_code_scheme": ""}, "version-code-from is SleepHub_*.apk, and version-code-scheme is minor-blocks: with the formula"),
        ({"asset_pattern": ""}, "version-code-from is SleepHub_*.apk, and asset-pattern is empty"),
        ({"asset_pattern": " \n"}, "version-code-from is SleepHub_*.apk, and asset-pattern is empty"),
        ({"asset_pattern": SLEEPHUB}, "version-code-from and asset-pattern are both SleepHub_*.apk"),
        ({"version_code_from": " SleepHub_*.apk\n", "asset_pattern": SLEEPHUB}, "version-code-from and asset-pattern are both SleepHub_*.apk"),
        ({"version_code_from": "SleepHub *.apk"}, "version-code-from is 'SleepHub *.apk'. It is the file name pattern"),
        ({"asset_pattern": "Sweet/Sleep_*.apk"}, "asset-pattern is 'Sweet/Sleep_*.apk'. It is the file name pattern"),
        ({"kind": "qa"}, "version-code-from is for the build of a release or a preview tag, and kind is qa"),
    ],
)
def test_version_code_from_is_refused_where_it_does_not_fit_before_anything_is_asked(repo, tmp_path, capsys, inputs, message):
    target = commit(repo, "Feature (#1)")
    code, out = add_later(tmp_path, no_api, target, apks({}), **inputs)
    assert (code, out) == (1, {})
    assert error_of(capsys).startswith(message)


@pytest.mark.parametrize("operation", ["next", "qa", "changes"])
def test_version_code_from_is_for_the_code_of_a_tag_only(repo, tmp_path, capsys, operation):
    target = commit(repo, "Feature (#1)")
    code, out = run(tmp_path, no_api, operation=operation, sha=target, version_code_from=SLEEPHUB)
    assert (code, out) == (1, {})
    assert error_of(capsys) == f"version-code-from is for the operation code, the versionCode of a release or a preview tag, and the operation is {operation}\n"


def test_without_version_code_from_a_run_downloads_no_apk_and_writes_no_record_as_before(repo, tmp_path):
    gh = FakeGitHub()
    target = released_with_sleephub(repo, tmp_path, gh)
    reader = apks({})
    code, out = release(tmp_path, gh, "v2.1.0", target, run_id="8301", read=reader, release_exists="true", asset_pattern=SWEETSLEEP)
    assert (code, out["version-code"]) == (0, "200000031")
    assert "version-code-from" not in out and reader.read == [] and reused_refs(gh) == []
    assert ("GET", "git/tags/") not in [(method, path[:9]) for method, path in gh.requests]


def test_the_number_is_reused_only_from_one_apk_of_the_release_reserved_for_the_tagged_commit(repo, tmp_path, capsys):
    gh = FakeGitHub()
    target = released_with_sleephub(repo, tmp_path, gh)

    def refused(read, **inputs):
        capsys.readouterr()
        code, out = add_later(tmp_path, gh, target, read, **inputs)
        assert (code, out) == (1, {})
        assert reused_refs(gh) == []
        return error_of(capsys)

    add_asset(gh, *SWEETSLEEP_APK)
    a_pattern_of_both_apps = refused(apks({}), version_code_from="*_release_2.1.0.apk")
    assert a_pattern_of_both_apps.startswith("SweetSleep_release_2.1.0.apk of v2.1.0 matches both version-code-from *_release_2.1.0.apk and asset-pattern SweetSleep_*.apk.")
    set_assets(gh, "v2.1.0", "SleepHub_release_2.1.0.apk", "SleepHub_mini_release_2.1.0.apk")
    two_lending_apks_with_two_codes = refused(apks({SLEEPHUB_APK: 200_000_030, ("v2.1.0", "SleepHub_mini_release_2.1.0.apk"): 200_000_031}))
    assert two_lending_apks_with_two_codes.startswith(
        "the APKs of v2.1.0 that version-code-from SleepHub_*.apk matches were built with different versionCodes: "
        "SleepHub_mini_release_2.1.0.apk (200000031), SleepHub_release_2.1.0.apk (200000030)."
    ), two_lending_apks_with_two_codes
    set_assets(gh, *SLEEPHUB_APK)
    a_code_of_another_major = refused(apks({SLEEPHUB_APK: 100_000_030}))
    assert a_code_of_another_major.startswith("SleepHub_release_2.1.0.apk of v2.1.0 has versionCode 100000030, which is no build number of major 2 (200000001 to 299999999)")
    a_code_without_its_ref = refused(apks({SLEEPHUB_APK: 200_150_000}))
    assert a_code_without_its_ref.startswith(
        "SleepHub_release_2.1.0.apk of v2.1.0 has versionCode 200150000, build number 150000 of major 2, and "
        "refs/build-numbers/2/150000 does not exist"
    ), a_code_without_its_ref
    another_commit = commit(repo, "Other (#2)")
    assert release(tmp_path, gh, "v2.1.1", another_commit, run_id="8401", release_exists="false")[1]["version-code"] == "200000031"
    a_number_of_another_commit = refused(apks({SLEEPHUB_APK: 200_000_031}))
    assert a_number_of_another_commit.startswith(
        f"SleepHub_release_2.1.0.apk of v2.1.0 has versionCode 200000031, and refs/build-numbers/2/31 was reserved for "
        f"the commit {another_commit[:7]}, not for {target[:7]}, the commit of v2.1.0."
    ), a_number_of_another_commit


def test_version_code_floor_is_for_a_new_number_and_does_not_hold_back_one_the_release_was_built_with(repo, tmp_path, capsys):
    gh = FakeGitHub()
    target = released_with_sleephub(repo, tmp_path, gh)
    reader = apks({SLEEPHUB_APK: 200_000_030})
    for floor in ("200000030", "200000029", "300000005"):
        capsys.readouterr()
        code, out = add_later(tmp_path, gh, target, reader, version_code_floor=floor)
        assert (code, out["version-code"]) == (0, "200000030")
        assert "::notice" not in capsys.readouterr().out


def test_the_ref_of_a_reuse_names_the_number_and_the_app_as_a_qa_counter_does():
    assert build_numbers.reuse_counter(2, 30, SWEETSLEEP) == "refs/reused-numbers/2/30/app-SweetSleep_-2a-2eapk"
    assert build_numbers.app_key("SleepHub_*.apk") == "app-SleepHub_-2a-2eapk"
    assert build_numbers.qa_counter("2.1.1", "SleepHub_*.apk") == "refs/qa-numbers/2.1.1/app-SleepHub_-2a-2eapk"


def test_the_refs_of_reuses_are_outside_the_directory_of_number_refs_the_counter_reads():
    assert not build_numbers.REUSED_REFS.startswith(build_numbers.REFS + "/")


def test_the_versioncode_of_an_asset_is_read_from_the_downloaded_apk_with_aapt2(tmp_path):
    import http.server

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append((self.headers.get("Host"), self.path, self.headers.get("Authorization"), self.headers.get("Accept")))
            if self.path == "/repos/acme/app/releases/assets/7":
                self.redirect_to_the_file_on_another_host()
                return
            body = {"/file/7": b"APK 200000030", "/repos/acme/app/releases/assets/9": b"not an APK"}.get(self.path)
            self.send_response(200 if body else 404)
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

        def redirect_to_the_file_on_another_host(self):
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{server.server_port}/file/7")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sdk = tmp_path / "sdk"
    an_older_aapt2_that_answers_another_code = "echo \"package: name='x' versionCode='1' versionName='0'\"\n"
    the_highest_aapt2_that_reads_the_file = (
        '[ "$1 $2" = "dump badging" ] || exit 2\n'
        'read -r word code < "$3"\n'
        '[ "$word" = APK ] || { echo "ERROR: $3: not a zip" >&2; exit 1; }\n'
        "echo \"package: name='ai.asleep.sleephub' versionCode='$code' versionName='2.1.0' platformBuildVersionName=''\"\n"
    )
    for version, script in (("35.0.0", an_older_aapt2_that_answers_another_code), ("36.1.0", the_highest_aapt2_that_reads_the_file)):
        tool = sdk / "build-tools" / version / "aapt2"
        tool.parent.mkdir(parents=True)
        tool.write_text("#!/bin/sh\n" + script)
        tool.chmod(0o755)
    env = {"GITHUB_API_URL": f"http://127.0.0.1:{server.server_port}", "GITHUB_REPOSITORY": "acme/app", "TOKEN": "job-token\n"}
    try:
        read = release_version.apk_reader({**env, "ANDROID_HOME": str(sdk)})
        assert read("v2.1.0", {"name": "SleepHub_release_2.1.0.apk", "id": 7}) == 200_000_030
        port = server.server_port
        request_to_the_api_with_the_token, request_to_the_file_without_it = seen
        assert request_to_the_api_with_the_token == (f"127.0.0.1:{port}", "/repos/acme/app/releases/assets/7", "Bearer job-token", "application/octet-stream")
        assert request_to_the_file_without_it == (f"localhost:{port}", "/file/7", None, "application/octet-stream")
        with pytest.raises(release_version.Failure, match=r"aapt2 of build-tools 36\.1\.0 cannot read the versionCode of x\.apk of v2\.1\.0: ERROR: .* not a zip"):
            read("v2.1.0", {"name": "x.apk", "id": 9})
        with pytest.raises(release_version.Failure, match=r"cannot download y\.apk of v2\.1\.0 to read its versionCode: HTTP 404"):
            read("v2.1.0", {"name": "y.apk", "id": 8})
        with pytest.raises(release_version.Failure, match="z.apk of v2.1.0 is listed without an id"):
            read("v2.1.0", {"name": "z.apk"})
        read_with_android_sdk_root = release_version.apk_reader({**env, "ANDROID_SDK_ROOT": str(sdk)})
        assert read_with_android_sdk_root("v2.1.0", {"name": "a.apk", "id": 7}) == 200_000_030
        downloads_before = len(seen)
        read_without_an_sdk = release_version.apk_reader({**env, "ANDROID_HOME": str(tmp_path / "none")})
        with pytest.raises(release_version.Failure, match="aapt2 was not found under the build-tools of ANDROID_HOME or ANDROID_SDK_ROOT"):
            read_without_an_sdk("v2.1.0", {"name": "a.apk", "id": 7})
        assert len(seen) == downloads_before
    finally:
        server.shutdown()


# --- the number in the name of a QA build ---------------------------------------------------------

DATE = "2026-11-19T00:00:00Z"


def rolling_apps(repo, tag, first, *builds, revision="HEAD"):
    """Move a rolling tag that holds a build per app, as the publish job of android-qa.yml does with asset-pattern."""
    git(repo, "tag", "-f", "-a", tag, "-m", first, "-m", "\n".join(builds), revision)


def test_the_qa_builds_of_a_version_are_numbered_from_one(repo, tmp_path):
    # The run number goes on across versions: 1.14.0's first QA build was 1.14.0-qa.12.
    git(repo, "tag", "v1.13.0")
    target = commit(repo, "Fix (#1)")
    origin(repo, "develop")
    gh = FakeGitHub()
    names = [qa(tmp_path, gh, target, run_id=str(run), sequence=str(run))[1]["version-name"] for run in (11, 12, 13)]
    assert names == ["1.13.1-qa.1", "1.13.1-qa.2", "1.13.1-qa.3"]
    # A build of the next minor for one run counts that version, and the branch goes on from there
    # once it builds that version itself.
    _, given = qa(tmp_path, gh, target, run_id="14", sequence="14", version="1.14.0")
    assert (given["version-name"], given["number"], given["release-tag"]) == ("1.14.0-qa.1", "1", "qa-develop-1.14.0")
    _, heading = qa(tmp_path, gh, target, run_id="15", sequence="15", next_version="1.14.0")
    assert (heading["version-name"], heading["release-tag"]) == ("1.14.0-qa.2", "qa-develop")
    # After the release, the next patch starts at 1 again.
    git(repo, "tag", "v1.13.1", target)
    fix = commit(repo, "Fix after 1.13.1 (#2)")
    origin(repo, "develop")
    _, after = qa(tmp_path, gh, fix, run_id="16", sequence="16")
    assert after["version-name"] == "1.13.2-qa.1"
    # The versionCodes are still one counter of the major, whatever the version.
    assert (given["version-code"], heading["version-code"], after["version-code"]) == ("100000004", "100000005", "100000006")
    assert gh.qa_numbers() == [
        "refs/qa-numbers/1.13.1/1", "refs/qa-numbers/1.13.1/2", "refs/qa-numbers/1.13.1/3",
        "refs/qa-numbers/1.13.2/1", "refs/qa-numbers/1.14.0/1", "refs/qa-numbers/1.14.0/2",
    ]


def test_a_run_that_is_run_again_keeps_the_number_in_its_name(repo, tmp_path, capsys):
    git(repo, "tag", "v2.0.0")
    target = commit(repo, "Fix (#1)")
    origin(repo, "develop")
    gh = FakeGitHub()
    _, first = qa(tmp_path, gh, target, run_id="9001")
    _, other = qa(tmp_path, gh, target, run_id="9002")
    capsys.readouterr()
    _, again = qa(tmp_path, gh, target, run_id="9001")
    assert [first["version-name"], other["version-name"], again["version-name"]] == ["2.0.1-qa.1", "2.0.1-qa.2", "2.0.1-qa.1"]
    assert first["version-code"] == again["version-code"] == "200000001"
    assert "QA number 1 of 2.0.1 (refs/qa-numbers/2.0.1/1): reserved by an earlier attempt of this run, and kept." in capsys.readouterr().out
    # A plan that reserved its build number and then could not reserve the name: run again, it
    # keeps the build number and takes the name.
    gh.refuse_ref = ("refs/qa-numbers/", 422, "Repository rule violations found\n\nCannot create ref due to creations being restricted.")
    code, out = qa(tmp_path, gh, target, run_id="9003")
    assert (code, out) == (1, {})
    assert error_of(capsys).startswith(
        "cannot create refs/qa-numbers/2.0.1/3: HTTP 422: Repository rule violations found%0A%0ACannot create ref due to "
        "creations being restricted.. A ruleset of the repository refuses it: let the job token create refs/qa-numbers/*, "
        "or keep those refs out of the ruleset."
    )
    assert gh.numbers(2) == [1, 2, 3]
    gh.refuse_ref = None
    _, again = qa(tmp_path, gh, target, run_id="9003")
    assert (again["version-name"], again["version-code"]) == ("2.0.1-qa.3", "200000003")
    assert gh.numbers(2) == [1, 2, 3]
    assert gh.qa_numbers() == ["refs/qa-numbers/2.0.1/1", "refs/qa-numbers/2.0.1/2", "refs/qa-numbers/2.0.1/3"]


def test_two_builds_that_reserve_a_qa_number_at_once_get_two():
    # Pushes in a row are not cancelled before they plan, and each build takes minutes: two plans
    # can read the counter at the same moment. The ref, created only once, keeps their names apart.
    gh = FakeGitHub()
    gh.refs["refs/qa-numbers/1.14.0/1"] = "0" * 40
    barrier = threading.Barrier(2, timeout=10)
    gh.after_list = barrier.wait
    results = {}

    def build(name):
        results[name] = build_numbers.reserve_qa(repository(gh), "1.14.0", "", "a" * 40, f"run {name}", DATE, [])

    threads = [threading.Thread(target=build, args=(name,)) for name in ("first", "second")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(result.number for result in results.values()) == [2, 3]
    later = max(results.values(), key=lambda result: result.number)
    assert later.why == "one above refs/qa-numbers/1.14.0/1, the last number reserved; 2 taken meanwhile by another build"


def test_the_first_qa_number_of_a_version_is_above_its_qa_builds_that_are_out(repo, tmp_path, capsys):
    git(repo, "tag", "v1.13.0")
    target = commit(repo, "Fix (#1)")
    origin(repo, "develop")
    # Published before the switch, named by the run number.
    rolling(repo, "qa-develop-1.14.0", "1.14.0-qa.12", 101_400_012, target)
    rolling(repo, "qa-develop", "1.13.1-qa.40", 101_300_040, target)
    # A label build is no QA build of its version.
    rolling(repo, "qa-develop-ble-test", "1.13.1-ble-test.70", 101_350_070, target)
    gh = FakeGitHub()
    capsys.readouterr()
    _, out = qa(tmp_path, gh, target, version="1.14.0")
    assert out["version-name"] == "1.14.0-qa.13"
    assert (
        "QA number 13 of 1.14.0, reserved as refs/qa-numbers/1.14.0/13: one above 12, the build 1.14.0-qa.12 that "
        "qa-develop-1.14.0 holds: no QA number of 1.14.0 was reserved before."
    ) in capsys.readouterr().out
    _, out = qa(tmp_path, gh, target, run_id="7002")
    assert out["version-name"] == "1.13.1-qa.41"
    # With numbers reserved, a name published above them (by a release-version from before) is
    # still not given twice.
    published = [(20, "the build 1.14.0-qa.20 that qa-develop-1.14.0 holds"), (12, "an earlier one")]
    number = build_numbers.reserve_qa(repository(gh), "1.14.0", "", "b" * 40, "run 3", DATE, published)
    assert (number.number, number.why) == (
        21, "one above 20, the build 1.14.0-qa.20 that qa-develop-1.14.0 holds, above refs/qa-numbers/1.14.0/13, the last number reserved"
    )
    number = build_numbers.reserve_qa(repository(gh), "1.14.0", "", "b" * 40, "run 4", DATE, [(5, "below them")])
    assert number.number == 22


def test_each_app_numbers_its_qa_builds_of_a_version_apart(repo, tmp_path, capsys):
    # sleephub-android: SleepHub on every push and SweetSleep by hand, into the one qa-<line>.
    git(repo, "tag", "v1.13.0")
    target = commit(repo, "Fix (#1)")
    origin(repo, "develop")
    gh = FakeGitHub()
    hub = [qa(tmp_path, gh, target, run_id=str(run), asset_pattern=SLEEPHUB)[1]["version-name"] for run in (1, 2, 3)]
    capsys.readouterr()
    _, sweet = qa(tmp_path, gh, target, run_id="4", asset_pattern=SWEETSLEEP)
    assert (hub, sweet["version-name"]) == (["1.13.1-qa.1", "1.13.1-qa.2", "1.13.1-qa.3"], "1.13.1-qa.1")
    # One counter of build numbers for both apps.
    assert sweet["version-code"] == "100000004"
    assert gh.qa_numbers() == [
        "refs/qa-numbers/1.13.1/app-SleepHub_-2a-2eapk/1", "refs/qa-numbers/1.13.1/app-SleepHub_-2a-2eapk/2",
        "refs/qa-numbers/1.13.1/app-SleepHub_-2a-2eapk/3", "refs/qa-numbers/1.13.1/app-SweetSleep_-2a-2eapk/1",
    ]
    assert (
        "QA number 1 of 1.13.1 of SweetSleep_*.apk, reserved as refs/qa-numbers/1.13.1/app-SweetSleep_-2a-2eapk/1: the "
        "first QA build of 1.13.1 of SweetSleep_*.apk"
    ) in capsys.readouterr().out
    # The first numbers after the switch: each app above its own line of the tag.
    rolling_apps(
        repo, "qa-develop", "1.13.1-qa.60 (100000126)",
        f"{SLEEPHUB} 1.13.1-qa.57 (100000123) {target}", f"{SWEETSLEEP} 1.13.1-qa.60 (100000126) {target}",
    )
    gh = FakeGitHub()
    _, hub = qa(tmp_path, gh, target, run_id="5", asset_pattern=SLEEPHUB)
    _, sweet = qa(tmp_path, gh, target, run_id="6", asset_pattern=SWEETSLEEP)
    _, other = qa(tmp_path, gh, target, run_id="7", asset_pattern="HeySleep_*.apk")
    assert (hub["version-name"], sweet["version-name"], other["version-name"]) == ("1.13.1-qa.58", "1.13.1-qa.61", "1.13.1-qa.1")
    # Without the pattern the version has one counter, above every app's builds.
    _, whole = qa(tmp_path, gh, target, run_id="8")
    assert whole["version-name"] == "1.13.1-qa.61"
    # A tag from before the pattern names one build of no app, which may be either's.
    rolling(repo, "qa-develop", "1.13.1-qa.70", 100_000_140, target)
    gh = FakeGitHub()
    _, hub = qa(tmp_path, gh, target, run_id="9", asset_pattern=SLEEPHUB)
    _, sweet = qa(tmp_path, gh, target, run_id="10", asset_pattern=SWEETSLEEP)
    assert (hub["version-name"], sweet["version-name"]) == ("1.13.1-qa.71", "1.13.1-qa.71")


@pytest.mark.parametrize(
    ("pattern", "counter"),
    [
        ("", "refs/qa-numbers/1.13.1"),
        (SLEEPHUB, "refs/qa-numbers/1.13.1/app-SleepHub_-2a-2eapk"),
        (SWEETSLEEP, "refs/qa-numbers/1.13.1/app-SweetSleep_-2a-2eapk"),
        # What git refuses in a ref: * ? [ .., a part that starts with . or ends in .lock.
        ("[!a-z]?..lock", "refs/qa-numbers/1.13.1/app--5b-21a-2dz-5d-3f-2e-2elock"),
        (".apk", "refs/qa-numbers/1.13.1/app--2eapk"),
        # Digits only: app- keeps it apart from a number of the version's own counter.
        ("123", "refs/qa-numbers/1.13.1/app-123"),
    ],
)
def test_the_counter_of_an_app_is_a_ref_git_takes(repo, pattern, counter):
    assert build_numbers.qa_counter("1.13.1", pattern) == counter
    git(repo, "check-ref-format", f"{counter}/1")


def test_no_two_patterns_share_a_counter():
    patterns = ["a-b", "a_2db", "a-2db", "a.b", "a-2eb", "a", "A", "*", "-2a", ".apk", "-apk", SLEEPHUB, "SleepHub_?.apk", "SleepHub_[!x].apk"]
    assert len({build_numbers.qa_counter("1.0.0", pattern) for pattern in patterns}) == len(patterns)


# --- the version bump looks and reserves nothing --------------------------------------------------


def test_the_version_bump_shows_the_number_the_release_would_get_and_reserves_nothing(repo, tmp_path, capsys):
    git(repo, "tag", "v2.0.0")
    target = commit(repo, "Fix (#1)")
    gh = FakeGitHub()
    gh.release("v2.0.0", "HeySleep_release_2.0.0_200050000.apk")
    capsys.readouterr()
    code, out = run(tmp_path, gh, operation="next", sha=target)
    assert code == 0
    assert (out["tag"], out["version-code"], out["build-number"]) == ("v2.0.1", "200050001", "50001")
    assert gh.writes() == [] and gh.refs == {}
    log = capsys.readouterr().out
    assert "Build number 50001 of major 2 if the build were planned now: one above 200050000" in log
    assert "Nothing is reserved: the build reserves its own." in log
    # A preview too. The formula has no code for a thousandth preview; a build number has.
    git(repo, "tag", "v2.1.0-preview.999", "HEAD~1")
    code, out = run(tmp_path, gh, operation="next", sha=target, bump="minor", prerelease="preview")
    assert (code, out["tag"], out["version-code"]) == (0, "v2.1.0-preview.1000", "200050001")


def test_the_version_bump_refuses_what_the_release_would_refuse(repo, tmp_path, capsys):
    git(repo, "tag", "v2.0.0")
    target = commit(repo, "Fix (#1)")
    gh = FakeGitHub()
    gh.reserve_by_hand(2, 3)
    gh.release("v2.0.0", "HeySleep_release_2.0.0_200050000.apk")
    code, out = run(tmp_path, gh, operation="next", sha=target)
    assert (code, out) == (1, {})
    assert "200050000 is out already" in error_of(capsys)
    # A release beyond the formula's patch numbers is no reason to refuse one.
    git(repo, "tag", "v2.0.499", target)
    later = commit(repo, "More (#2)")
    code, out = run(tmp_path, FakeGitHub(), operation="next", sha=later)
    assert (code, out["tag"]) == (0, "v2.0.500")
    code, _ = run(tmp_path, no_api, operation="next", sha=later, version_code_scheme="minor-blocks")
    assert code == 1
    assert "patch version 500 is outside 0..499" in error_of(capsys)


# --- the formula, as before -----------------------------------------------------------------------


def test_the_formula_reads_the_build_numbers_of_its_major_once_and_writes_nothing(lines, repo, tmp_path):
    # A repository that never switched has no build number: the formula computes what it always
    # did, after one read of the refs of the major.
    for scheme in ("", "minor-blocks", " minor-blocks\n"):
        gh = FakeGitHub()
        code, out = qa(tmp_path, gh, lines["c"], version_code_scheme=scheme)
        assert (code, out["version-code"], out["version-code-scheme"]) == (0, "200000057", "minor-blocks")
        assert "build-number" not in out
        code, out = run(tmp_path, gh, operation="next", sha=lines["c"], bump="minor", version_code_scheme=scheme)
        assert (code, out["version-code"]) == (0, "200150000")
        git(repo, "tag", "-f", "v2.0.1", lines["c"])
        code, out = release(tmp_path, gh, "v2.0.1", lines["c"], version_code_scheme=scheme)
        assert (code, out) == (0, {"version": "2.0.1", "version-code": "200050100", "version-code-scheme": "minor-blocks"})
        git(repo, "tag", "-d", "v2.0.1")
        assert gh.requests == [("GET", "git/matching-refs/build-numbers/2/")] * 3


@pytest.mark.parametrize("operation", ["qa", "code", "next"])
def test_the_formula_is_refused_for_a_major_that_has_build_numbers(lines, repo, tmp_path, capsys, operation):
    # The scheme is the calling file's, and the calling file is the commit's: a branch that did not
    # get the switch, or a tag on a commit from before it, would build 2.x with the formula after
    # 2.x has build numbers. 2.0.1 would be 200050100, above every build number of 2.x so far.
    gh = FakeGitHub()
    gh.reserve_by_hand(2, 7)
    git(repo, "tag", "v2.0.1", lines["c"])
    later = commit(repo, "Fix (#40)")
    origin(repo, "develop")
    inputs = {
        "qa": {"operation": "qa", "sha": later, "ref": "refs/heads/develop", "qa_sequence": "57", "source_branch": "develop"},
        "code": {"operation": "code", "kind": "release", "version": "v2.0.1", "sha": lines["c"]},
        "next": {"operation": "next", "sha": later},
    }[operation]
    code, out = run(tmp_path, gh, version_code_scheme="minor-blocks", **inputs)
    assert (code, out) == (1, {})
    message = error_of(capsys)
    assert "version-code-scheme is minor-blocks here, and refs/build-numbers/2/ holds build numbers (up to 7)" in message
    assert "do not release a commit from before the switch" in message
    assert gh.writes() == []
    # A major without build numbers is not held to those of another one: 1.x still builds with the formula.
    code, out = run(tmp_path, gh, version_code_scheme="minor-blocks", operation="code", kind="release", version="v1.13.1")
    assert (code, out["version-code"]) == (0, "101350100")


def test_the_formula_goes_on_when_github_cannot_say(lines, tmp_path, capsys):
    # The question is new for the formula: a repository that never switched builds as it did when
    # GitHub does not answer, with a warning.
    gh = FakeGitHub()
    gh.refuse[("GET", "git/matching-refs/")] = (503, "Service Unavailable")
    code, out = qa(tmp_path, gh, lines["c"], version_code_scheme="")
    assert (code, out["version-code"]) == (0, "200000057")
    log = capsys.readouterr().out
    assert "::warning title=release-version::Could not ask whether major 2 has build numbers (cannot list the build numbers of major 2: HTTP 503: Service Unavailable." in log
    # Asked again before it gave up.
    assert gh.requests.count(("GET", "git/matching-refs/build-numbers/2/")) == 4


def test_a_floor_with_the_formula_is_not_read_and_says_so(lines, tmp_path, capsys):
    for floor in ("200000006", "abc"):
        code, out = qa(tmp_path, FakeGitHub(), lines["c"], version_code_scheme="", version_code_floor=floor)
        assert (code, out["version-code"]) == (0, "200000057")
        assert (
            "::warning title=release-version::version-code-floor is given, and version-code-scheme is minor-blocks: "
            "the floor is for build numbers, and is not read."
        ) in capsys.readouterr().out


@pytest.mark.parametrize("scheme", ["build-number", " build-number ", "build-number\n", "\tbuild-number\r\n"])
def test_the_scheme_is_given_on_as_the_word_the_plan_read(lines, repo, tmp_path, scheme):
    # The jobs after the plan compare this output, never the input: an input with whitespace
    # around the word is the build-number scheme for the plan, and has to be for them as well.
    gh = FakeGitHub()
    code, out = qa(tmp_path, gh, lines["c"], version_code_scheme=scheme)
    assert (code, out["version-code-scheme"], out["version-code"], out["build-number"]) == (0, "build-number", "200000001", "1")
    git(repo, "tag", "v2.0.1", lines["c"])
    code, out = release(tmp_path, gh, "v2.0.1", lines["c"], version_code_scheme=scheme, run_id="7002")
    assert (code, out["version-code-scheme"], out["version-code"]) == (0, "build-number", "200000002")
    code, out = run(tmp_path, gh, operation="next", sha=commit(repo, "More (#9)"), version_code_scheme=scheme)
    assert (code, out["version-code-scheme"], out["version-code"]) == (0, "build-number", "200000003")
    # The word itself, with anything else inside it, is no scheme.
    code, out = qa(tmp_path, gh, lines["c"], version_code_scheme="build- number")
    assert (code, out) == (1, {})


@pytest.mark.parametrize("operation", ["qa", "code", "next"])
def test_a_scheme_that_is_no_known_word_is_refused(lines, tmp_path, capsys, operation):
    inputs = {"qa": {"ref": "refs/heads/develop", "qa_sequence": "1", "source_branch": "develop", "sha": lines["c"]},
              "code": {"kind": "release", "version": "2.0.0"}, "next": {"sha": lines["c"]}}[operation]
    code, out = run(tmp_path, no_api, operation=operation, version_code_scheme="build_number", **inputs)
    assert (code, out) == (1, {})
    assert "version-code-scheme must be one of minor-blocks, build-number, got 'build_number'" in error_of(capsys)


# --- what is published over what --------------------------------------------------------------------


def test_changes_say_which_code_the_rolling_build_has(repo, tmp_path):
    git(repo, "tag", "v2.0.0")
    earlier = commit(repo, "Fix (#1)")
    rolling(repo, "qa-develop", "2.0.1-qa.5", 200_000_009, earlier)
    target = commit(repo, "Fix (#2)")
    _, out = run(tmp_path, no_api, operation="changes", sha=target, since="qa-develop", version_code="200000008")
    assert (out["since-code"], out["code-taken-by"], out["stale"]) == ("200000009", "", "false")
    _, out = run(tmp_path, no_api, operation="changes", sha=target, since="qa-develop-knox", version_code="200000008")
    assert out["since-code"] == ""
    # Only asked with a versionCode, as code-taken-by.
    _, out = run(tmp_path, no_api, operation="changes", sha=target, since="qa-develop")
    assert "since-code" not in out
