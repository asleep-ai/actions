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

    def release(self, tag, *assets, prerelease=False, draft=False):
        self.releases.append({
            "tag_name": tag, "prerelease": prerelease, "draft": draft, "assets": [{"name": name} for name in assets],
        })

    def reserve_by_hand(self, major, number, sha="0" * 40):
        self.refs[f"refs/build-numbers/{major}/{number}"] = sha

    def numbers(self, major):
        prefix = f"refs/build-numbers/{major}/"
        return sorted(int(name[len(prefix):]) for name in self.refs if name.startswith(prefix))

    def writes(self):
        return [request for request in self.requests if request[0] != "GET"]

    def _answer(self, status, document, headers=None):
        return status, headers or {}, json.dumps(document).encode()

    def _page(self, url, items):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        page = int(query.get("page", ["1"])[0])
        chunk = items[(page - 1) * self.page_size:page * self.page_size]
        headers = {}
        if page * self.page_size < len(items):
            parts = urllib.parse.urlsplit(url)
            following = urllib.parse.urlencode({**{k: v[0] for k, v in query.items()}, "page": page + 1})
            headers["link"] = f'<{urllib.parse.urlunsplit(parts._replace(query=following))}>; rel="next"'
        return self._answer(200, chunk, headers)

    def __call__(self, method, url, token, body):
        assert url.startswith(REPOSITORY), url
        assert token == "job-token"
        path = url[len(REPOSITORY):]
        self.requests.append((method, path.split("?")[0]))
        for (refused_method, refused_path), (status, message) in self.refuse.items():
            if method == refused_method and path.startswith(refused_path):
                return self._answer(status, {"message": message})
        if method == "GET" and path.startswith("releases"):
            return self._page(url, self.releases)
        if method == "GET" and path.startswith("git/matching-refs/"):
            prefix = "refs/" + path.split("?")[0][len("git/matching-refs/"):]
            with self.lock:
                listed = [{"ref": name, "object": {"sha": sha, "type": "tag"}} for name, sha in sorted(self.refs.items()) if name.startswith(prefix)]
            answer = self._page(url, listed)
            if self.after_list:
                self.after_list()
            return answer
        if method == "POST" and path == "git/tags":
            # A git object is its content: the same content is the same object.
            sha = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()
            self.objects[sha] = body
            return self._answer(201, {"sha": sha})
        if method == "POST" and path == "git/refs":
            if self.before_create:
                hook, self.before_create = self.before_create, None
                hook(body["ref"])
            with self.lock:
                if body["ref"] in self.refs:
                    return self._answer(422, {"message": "Reference already exists"})
                self.refs[body["ref"]] = body["sha"]
            return self._answer(201, {"ref": body["ref"], "object": {"sha": body["sha"]}})
        raise AssertionError(f"unexpected request {method} {url}")


def no_api(method, url, token, body):
    raise AssertionError(f"the formula asks GitHub nothing, and {method} {url} was asked")


def run(tmp_path, gh, run_id="7001", **inputs):
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
    code = release_version.main(env, pulls(), gh)
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
def test_a_floor_above_every_code_of_the_major_is_refused(floor):
    # A new repository that takes over an app at 101300002 and builds 0.0.1: no build of major 0
    # installs over it.
    with pytest.raises(BuildNumberError) as refused:
        next_number(0, [], [], floor)
    assert f"version-code-floor is {floor}, and no versionCode of major 0 is above it (the highest is 99999999)" in str(refused.value)
    assert "next-version" in str(refused.value)


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


def test_every_page_of_the_answer_is_read():
    gh = FakeGitHub(page_size=2)
    for patch in range(5):
        gh.release(f"v2.0.{patch}", f"App_release_2.0.{patch}_2000000{patch + 10}.apk")
    for number in range(1, 6):
        gh.reserve_by_hand(1, number)
    assert max(item.code for item in repository(gh).shipped()) == 200_000_014
    assert sorted(repository(gh).reserved(1)) == [1, 2, 3, 4, 5]


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
    assert (out["version-name"], out["version-code"]) == ("1.0.0-qa.57", "100000001")


def test_major_zero_goes_on_into_major_one_above_all_of_it(repo, tmp_path):
    git(repo, "tag", "v0.3.0")
    target = commit(repo, "Feature (#1)")
    origin(repo, "develop")
    gh = FakeGitHub()
    gh.release("v0.3.0", "App_release_0.3.0_41.apk")
    _, out = qa(tmp_path, gh, target)
    assert (out["version-name"], out["version-code"]) == ("0.3.1-qa.57", "42")
    gh.reserve_by_hand(0, 99_999_999)
    _, out = qa(tmp_path, gh, target, next_version="1.0.0", run_id="7002")
    # Above every code major 0 can have.
    assert (out["version-name"], out["version-code"]) == ("1.0.0-qa.57", "100000001")


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
    assert (develop["version-name"], maintenance["version-name"]) == ("2.0.1-qa.57", "1.13.2-qa.57")


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
    assert (out["version-name"], out["version-code"]) == ("2.0.0-qa.57", "200049002")


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
    assert (out["version-name"], out["version-code"]) == ("2.0.1-qa.1013", "200050001")
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
    assert out["version-name"] == "2.0.2-qa.1014"
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
    assert gh.refs == {"refs/build-numbers/2/1": sha}
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


def test_a_qa_number_has_no_ceiling_of_its_own(repo, tmp_path, capsys):
    git(repo, "tag", "v2.0.0")
    target = commit(repo, "Fix (#1)")
    origin(repo, "develop")
    gh = FakeGitHub()
    # The formula stops at 48999, where the previews start; a build number is the versionCode.
    _, out = qa(tmp_path, gh, target, sequence="50000")
    assert (out["version-name"], out["version-code"]) == ("2.0.1-qa.50000", "200000001")
    code, _ = qa(tmp_path, gh, target, sequence="5", qa_sequence_offset="-5", run_id="7002")
    assert code == 1
    assert "qa-sequence plus qa-sequence-offset is 0, and the number of a QA build is 1 or higher" in error_of(capsys)


def test_the_release_needs_the_commit_it_builds(repo, tmp_path, capsys):
    git(repo, "tag", "v2.0.0")
    code, _ = run(tmp_path, FakeGitHub(), operation="code", kind="release", version="v2.0.0")
    assert code == 1
    assert "sha is the commit that is built, and the build-number scheme needs it to reserve a number" in error_of(capsys)


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


def test_the_formula_asks_github_nothing_and_writes_no_build_number(lines, repo, tmp_path):
    for scheme in ("", "minor-blocks"):
        code, out = qa(tmp_path, no_api, lines["c"], version_code_scheme=scheme)
        assert (code, out["version-code"]) == (0, "200000057")
        assert "build-number" not in out
        git(repo, "tag", "-f", "v2.0.1", lines["c"])
        code, out = release(tmp_path, no_api, "v2.0.1", lines["c"], version_code_scheme=scheme)
        assert (code, out) == (0, {"version": "2.0.1", "version-code": "200050100"})
        git(repo, "tag", "-d", "v2.0.1")


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
