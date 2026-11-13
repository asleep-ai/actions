"""Scenario tests for release_version.py, each against a throwaway git repository.

Run: uv run --with pytest python -m pytest release-version -q
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import release_version  # noqa: E402

API = "https://api.github.invalid"


def git(repo: Path, *args: str) -> str:
    command = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]
    command += ["-c", "tag.gpgsign=false"]
    result = subprocess.run([*command, *args], cwd=repo, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def commit(repo: Path, subject: str, body: str = "") -> str:
    message = ["-m", subject] + (["-m", body] if body else [])
    git(repo, "commit", "-q", "--allow-empty", *message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "develop")
    commit(path, "Initial commit")
    monkeypatch.chdir(path)
    return path


def pulls(**by_number):
    """A fetch stub answering for the given pull requests, each a dict of the API's fields."""
    calls = []

    def fetch(url, token):
        calls.append((url, token))
        number = url.rsplit("/", 1)[1]
        if number not in by_number:
            raise release_version.Failure(f"HTTP 404 from {url}: Not Found")
        document = by_number[number]
        return document if isinstance(document, bytes) else json.dumps(document).encode()

    fetch.calls = calls
    return fetch


def merged(sha: str) -> dict:
    return {"state": "closed", "merged": True, "merge_commit_sha": sha}


def run(tmp_path, fetch=None, **inputs):
    output = tmp_path / "github-output"
    output.write_text("")
    env = {
        "OPERATION": "next",
        "BUMP": "patch",
        "TAG_PREFIX": "v",
        "TOKEN": "t0ken\n",
        "GITHUB_REPOSITORY": "acme/app",
        "GITHUB_API_URL": API,
        "GITHUB_OUTPUT": str(output),
    }
    env.update({name.upper(): value for name, value in inputs.items()})
    code = release_version.main(env, fetch or pulls())
    return code, parse_outputs(output.read_text())


def parse_outputs(text: str) -> dict:
    """Read a GITHUB_OUTPUT file, heredoc values included."""
    outputs = {}
    lines = iter(text.splitlines())
    for line in lines:
        name, separator, value = line.partition("=")
        if "<<" in name and not separator:
            name, delimiter = line.split("<<", 1)
            collected = []
            for inner in lines:
                if inner == delimiter:
                    break
                collected.append(inner)
            outputs[name] = "\n".join(collected)
        else:
            outputs[name] = value
    return outputs


def error_of(capsys) -> str:
    out = capsys.readouterr().out
    assert out.count("::error title=release-version::") == 1, out
    return out.split("::error title=release-version::", 1)[1]


# --- the last release tag and the next version ---------------------------------


@pytest.mark.parametrize(("bump", "version"), [("patch", "0.0.1"), ("minor", "0.1.0"), ("major", "1.0.0")])
def test_a_repository_without_release_tags_counts_from_zero(repo, tmp_path, bump, version):
    target = commit(repo, "Add the first feature (#1)")
    code, out = run(tmp_path, sha=target, bump=bump)
    assert code == 0
    assert out == {
        "version": version,
        "tag": "v" + version,
        "previous-tag": "",
        "target-sha": target,
        "prs": "- `" + git(repo, "rev-parse", "HEAD~1")[:7] + "` Initial commit\n- #1 Add the first feature",
        "version-code": str({"0.0.1": 50_001, "0.1.0": 150_000, "1.0.0": 100_050_000}[version]),
        "branch": "",
    }


@pytest.mark.parametrize(
    ("bump", "version", "code"),
    [("patch", "1.13.1", "101350001"), ("minor", "1.14.0", "101450000"), ("major", "2.0.0", "200050000")],
)
def test_bump_raises_one_part_and_resets_the_lower_ones(repo, tmp_path, bump, version, code):
    git(repo, "tag", "v1.13.0")
    target = commit(repo, "Fix it (#2)")
    status, out = run(tmp_path, sha=target, bump=bump)
    assert status == 0
    assert (out["version"], out["tag"], out["previous-tag"], out["version-code"]) == (
        version, "v" + version, "v1.13.0", code,
    )


def test_the_last_release_is_the_highest_version_not_the_newest_tag(repo, tmp_path):
    # 1.10.0 is above 1.9.3 as numbers and below it as text; 1.9.3 is tagged last.
    git(repo, "tag", "v1.10.0")
    commit(repo, "Later (#3)")
    git(repo, "tag", "v1.2.0", "HEAD~1")
    git(repo, "tag", "v1.9.3", "HEAD~1")
    target = commit(repo, "Newest (#4)")
    _, out = run(tmp_path, sha=target)
    assert (out["previous-tag"], out["tag"]) == ("v1.10.0", "v1.10.1")


def test_only_exact_release_tags_count(repo, tmp_path):
    git(repo, "tag", "v1.2.3")
    commit(repo, "More (#5)")
    for name in (
        "v1.11.2-handok-reset-off.84",  # a test build of an earlier flow
        "v2.0.0-rc.1",
        "v3",  # a floating major tag
        "v3.1",
        "v9.9.9.9",
        "v04.0.0",  # a leading zero is not a number
        "android-ci/v8.0.0",  # another component's tag
        "8.0.0",
        "V7.0.0",
        "skeleton-v0.1",
    ):
        git(repo, "tag", name)
    target = commit(repo, "Newest (#6)")
    _, out = run(tmp_path, sha=target)
    assert (out["previous-tag"], out["tag"]) == ("v1.2.3", "v1.2.4")


def test_annotated_tags_are_followed_to_their_commit(repo, tmp_path):
    git(repo, "tag", "-a", "v1.0.0", "-m", "1.0.0 release")
    target = commit(repo, "Fix (#7)")
    code, out = run(tmp_path, sha=target)
    assert code == 0
    assert out["prs"] == "- #7 Fix"


@pytest.mark.parametrize(("prefix", "tag"), [("app-v", "app-v2.0.1"), ("android/v", "android/v0.3.1"), ("", "0.0.1")])
def test_tag_prefix_selects_the_release_line(repo, tmp_path, prefix, tag):
    for name in ("v5.0.0", "app-v2.0.0", "android/v0.3.0"):
        git(repo, "tag", name)
    target = commit(repo, "Fix (#8)")
    code, out = run(tmp_path, sha=target, tag_prefix=prefix)
    assert code == 0
    assert out["tag"] == tag
    assert out["version"] == tag[len(prefix):]


# --- the commit to release ---------------------------------------------------------


def test_pr_is_resolved_to_its_merge_commit(repo, tmp_path):
    git(repo, "tag", "v1.0.0")
    target = commit(repo, "Add a setting (#12)")
    commit(repo, "Not released yet (#13)")
    fetch = pulls(**{"12": merged(target)})
    code, out = run(tmp_path, fetch, pr="12")
    assert code == 0
    assert out["target-sha"] == target
    assert out["prs"] == "- #12 Add a setting"
    # The token is sent without the newline a pasted secret carries.
    assert fetch.calls == [(f"{API}/repos/acme/app/pulls/12", "t0ken")]


def test_pr_may_be_written_with_a_hash_sign(repo, tmp_path):
    target = commit(repo, "Add a setting (#12)")
    code, out = run(tmp_path, pulls(**{"12": merged(target)}), pr=" #12 ")
    assert (code, out["target-sha"]) == (0, target)


@pytest.mark.parametrize(
    ("pull", "message"),
    [
        ({"state": "open", "merged": False, "merge_commit_sha": "a" * 40}, "pull request #12 is not merged (state: 'open')"),
        ({"state": "closed", "merged": False, "merge_commit_sha": None}, "is not merged (state: 'closed')"),
        ({"state": "closed", "merged": True, "merge_commit_sha": None}, "pull request #12 has no merge commit"),
        ({"state": "closed", "merged": True, "merge_commit_sha": "b" * 40}, "is not in this checkout"),
        (b"<html>", "is not JSON"),
        ([], "is not an object"),
    ],
)
def test_a_pull_request_that_cannot_be_released(repo, tmp_path, capsys, pull, message):
    code, out = run(tmp_path, pulls(**{"12": pull}), pr="12")
    assert (code, out) == (1, {})
    assert message in error_of(capsys)


def test_an_unknown_pull_request_fails(repo, tmp_path, capsys):
    code, _ = run(tmp_path, pulls(), pr="99")
    assert code == 1
    assert "HTTP 404" in error_of(capsys)


@pytest.mark.parametrize("pr", ["abc", "12; rm -rf /", "0", "-1", "1.5", "../issues/3"])
def test_pr_must_be_a_number(repo, tmp_path, capsys, pr):
    fetch = pulls()
    code, _ = run(tmp_path, fetch, pr=pr)
    assert code == 1
    assert "pr must be a pull request number" in error_of(capsys)
    assert fetch.calls == []


def test_sha_may_be_abbreviated(repo, tmp_path):
    target = commit(repo, "Fix (#3)")
    code, out = run(tmp_path, sha=target[:8])
    assert (code, out["target-sha"]) == (0, target)


@pytest.mark.parametrize(
    ("sha", "message"),
    [
        ("develop", "sha must be a commit id"),
        ("HEAD", "sha must be a commit id"),
        ("--all", "sha must be a commit id"),
        ("abc12", "sha must be a commit id"),
        ("f" * 40, "is not in this checkout"),
    ],
)
def test_sha_must_name_a_commit_of_the_checkout(repo, tmp_path, capsys, sha, message):
    code, _ = run(tmp_path, sha=sha)
    assert code == 1
    assert message in error_of(capsys)


def test_a_tree_or_blob_id_is_not_a_commit(repo, tmp_path, capsys):
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    code, _ = run(tmp_path, sha=tree)
    assert code == 1
    assert "is not in this checkout" in error_of(capsys)


@pytest.mark.parametrize("inputs", [{}, {"pr": "1", "sha": "abcdef1"}])
def test_exactly_one_of_pr_and_sha(repo, tmp_path, capsys, inputs):
    code, _ = run(tmp_path, **inputs)
    assert code == 1
    assert "give exactly one of pr and sha" in error_of(capsys)


# --- the commit against the last release ------------------------------------------------


def test_the_commit_of_the_last_release_has_nothing_new(repo, tmp_path, capsys):
    target = commit(repo, "Released (#1)")
    git(repo, "tag", "-a", "v1.0.0", "-m", "1.0.0")
    code, out = run(tmp_path, sha=target)
    assert (code, out) == (1, {})
    assert "is the commit of v1.0.0: there is nothing new to release" in error_of(capsys)


def test_a_commit_before_the_first_release_is_refused(repo, tmp_path, capsys):
    older = commit(repo, "Older (#1)")
    commit(repo, "Released (#2)")
    git(repo, "tag", "v1.0.0")
    code, _ = run(tmp_path, sha=older)
    assert code == 1
    assert "contains no release tag, and the repository has releases up to v1.0.0" in error_of(capsys)


@pytest.mark.parametrize("bump", ["patch", "minor"])
def test_a_commit_before_the_last_release_of_its_line_is_refused(repo, tmp_path, capsys, bump):
    git(repo, "tag", "v1.2.0")
    older = commit(repo, "Older (#1)")
    commit(repo, "Released (#2)")
    git(repo, "tag", "v1.3.0")
    code, _ = run(tmp_path, sha=older, bump=bump)
    assert code == 1
    message = error_of(capsys)
    assert "builds on v1.2.0" in message
    assert "the last release of major 1 is v1.3.0, which the commit does not contain" in message


def test_a_commit_on_a_branch_that_lacks_the_last_release_is_refused(repo, tmp_path, capsys):
    # The earlier flow of one caller: the release is a merge commit on main that develop never received.
    git(repo, "checkout", "-q", "-b", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "1.13.0 release", "develop")
    commit(repo, "Release commit on main only")
    git(repo, "tag", "-a", "v1.13.0", "-m", "1.13.0")
    git(repo, "checkout", "-q", "develop")
    target = commit(repo, "Fix (#857)")
    fetch = pulls(**{"857": merged(target)})
    code, _ = run(tmp_path, fetch, pr="857")
    assert code == 1
    message = error_of(capsys)
    assert "the merge commit of pull request #857" in message
    assert "contains no release tag, and the repository has releases up to v1.13.0" in message
    # Once develop has received main, the same pull request can be released.
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'main' into develop", "main")
    target = commit(repo, "Another fix (#858)")
    code, out = run(tmp_path, pulls(**{"858": merged(target)}), pr="858")
    assert code == 0
    assert out["tag"] == "v1.13.1"


# --- release lines ----------------------------------------------------------------------


def origin(repo: Path, branch: str, revision: str = "HEAD") -> None:
    """Record a branch of the remote, as a checkout with fetch-depth: 0 has it."""
    git(repo, "update-ref", f"refs/remotes/origin/{branch}", git(repo, "rev-parse", revision))


@pytest.fixture
def lines(repo) -> dict:
    """Two lines: 1.x maintained on release/1.x, and develop, which went on to 2.x.

        develop:      base(v1.13.0) ── a ── b(v2.0.0) ── c
        release/1.x:    └── h1(v1.13.1) ── h2
    """
    commits = {"base": commit(repo, "Last change of 1.13.0 (#10)")}
    git(repo, "tag", "-a", "v1.13.0", "-m", "v1.13.0")
    git(repo, "checkout", "-q", "-b", "release/1.x")
    commits["h1"] = commit(repo, "Hotfix one (#21)")
    git(repo, "tag", "v1.13.1")
    commits["h2"] = commit(repo, "Hotfix two (#22)")
    git(repo, "checkout", "-q", "develop")
    commits["a"] = commit(repo, "Redesign (#30)")
    commits["b"] = commit(repo, "Finish the redesign (#31)")
    git(repo, "tag", "v2.0.0")
    commits["c"] = commit(repo, "Fix after 2.0 (#32)")
    origin(repo, "develop", "develop")
    origin(repo, "release/1.x", "release/1.x")
    return commits


def test_each_line_continues_from_the_last_release_its_commit_contains(lines, tmp_path):
    # 2.0.0 is the highest tag of the repository, and the maintenance branch still goes on from 1.13.1.
    code, out = run(tmp_path, sha=lines["h2"], source_branch="develop")
    assert code == 0
    assert (out["previous-tag"], out["tag"], out["branch"]) == ("v1.13.1", "v1.13.2", "release/1.x")
    assert out["prs"] == "- #22 Hotfix two"
    assert out["version-code"] == "101350002"
    code, out = run(tmp_path, sha=lines["c"], source_branch="develop")
    assert code == 0
    assert (out["previous-tag"], out["tag"], out["branch"]) == ("v2.0.0", "v2.0.1", "develop")
    assert out["prs"] == "- #32 Fix after 2.0"


def test_a_minor_release_on_the_maintenance_branch_stays_in_its_line(lines, tmp_path):
    code, out = run(tmp_path, sha=lines["h2"], bump="minor", source_branch="develop")
    assert (code, out["tag"], out["branch"]) == (0, "v1.14.0", "release/1.x")


def test_a_maintenance_branch_cannot_release_another_major(lines, repo, tmp_path, capsys):
    # 2.0.0 exists, so major from 1.13.1 would repeat it.
    code, _ = run(tmp_path, sha=lines["h2"], bump="major", source_branch="develop")
    assert code == 1
    assert "the last release of major 2 is v2.0.0, which the commit does not contain" in error_of(capsys)
    # Also when the next major has no release yet.
    git(repo, "tag", "-d", "v2.0.0")
    code, _ = run(tmp_path, sha=lines["h2"], bump="major", source_branch="develop")
    assert code == 1
    message = error_of(capsys)
    assert "is on release/1.x, and v2.0.0 is a release of major 2" in message
    assert "A maintenance branch releases its own major version only" in message


@pytest.mark.parametrize("bump", ["patch", "minor"])
def test_the_main_line_cannot_take_a_version_of_a_maintained_major(repo, tmp_path, capsys, bump):
    # develop has not moved on to the next major yet, and 1.x already has its branch.
    git(repo, "tag", "v1.13.0")
    origin(repo, "release/1.x")
    target = commit(repo, "New work (#30)")
    origin(repo, "develop")
    code, _ = run(tmp_path, sha=target, bump=bump, source_branch="develop")
    assert code == 1
    message = error_of(capsys)
    assert "is a release of major 1, which is released from release/1.x" in message
    assert "On develop, raise the major version" in message
    code, out = run(tmp_path, sha=target, bump="major", source_branch="develop")
    assert (code, out["tag"], out["branch"]) == (0, "v2.0.0", "develop")


def test_the_main_line_cannot_repeat_a_version_the_maintenance_branch_released(lines, repo, tmp_path, capsys):
    # A commit of develop from before 2.0.0: it contains v1.13.0 only, and v1.13.1 is on release/1.x.
    code, _ = run(tmp_path, sha=lines["a"])
    assert code == 1
    assert "the last release of major 1 is v1.13.1, which the commit does not contain" in error_of(capsys)


def test_a_commit_shared_by_both_lines_is_released_on_the_branch_of_its_major(repo, tmp_path):
    git(repo, "tag", "v1.13.0")
    shared = commit(repo, "Before the lines split (#11)")
    origin(repo, "release/1.x")
    commit(repo, "Redesign (#30)")
    origin(repo, "develop")
    code, out = run(tmp_path, sha=shared, source_branch="develop")
    assert (code, out["tag"], out["branch"]) == (0, "v1.13.1", "release/1.x")


def test_a_repository_without_maintenance_branches_releases_from_the_source_branch(repo, tmp_path):
    git(repo, "tag", "v1.13.0")
    target = commit(repo, "Fix (#12)")
    origin(repo, "develop")
    origin(repo, "main", "HEAD~1")
    for bump, tag in (("patch", "v1.13.1"), ("minor", "v1.14.0"), ("major", "v2.0.0")):
        code, out = run(tmp_path, sha=target, bump=bump, source_branch="develop")
        assert (code, out["tag"], out["branch"]) == (0, tag, "develop")


def test_a_commit_that_was_not_merged_is_refused(repo, tmp_path, capsys):
    git(repo, "tag", "v1.13.0")
    commit(repo, "Merged (#12)")
    origin(repo, "develop")
    git(repo, "checkout", "-q", "-b", "feature")
    head = commit(repo, "Head of an open pull request")
    code, _ = run(tmp_path, sha=head, source_branch="develop")
    assert code == 1
    assert "is not on develop. A release is cut from a commit that was merged into develop" in error_of(capsys)
    # Without source-branch the action checks tags only.
    code, out = run(tmp_path, sha=head)
    assert (code, out["tag"], out["branch"]) == (0, "v1.13.1", "")


def test_a_branch_that_only_looks_like_a_maintenance_branch_is_not_one(repo, tmp_path, capsys):
    git(repo, "tag", "v1.13.0")
    origin(repo, "develop")
    git(repo, "checkout", "-q", "-b", "release/next")
    target = commit(repo, "On another release branch")
    origin(repo, "release/next")
    code, _ = run(tmp_path, sha=target, source_branch="develop")
    assert code == 1
    assert "is not on develop" in error_of(capsys)


@pytest.mark.parametrize("branch", ["develop", "release/1.x"])
def test_the_head_of_a_pull_request_merged_with_a_merge_commit_is_refused(repo, tmp_path, capsys, branch):
    # The head is in the history of the branch, and its own check, the pull request's, is green. But
    # that check built the merge with the base branch, and the branch never pointed at the head.
    git(repo, "tag", "v1.13.0")
    if branch != "develop":
        git(repo, "checkout", "-q", "-b", branch)
    git(repo, "checkout", "-q", "-b", "feature")
    head = commit(repo, "Work on the feature")
    git(repo, "checkout", "-q", branch)
    commit(repo, "The base branch moved on (#11)")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge pull request #12 from acme/feature", "feature")
    merge = git(repo, "rev-parse", "HEAD")
    origin(repo, "develop", "develop")
    if branch != "develop":
        origin(repo, branch, branch)
    code, _ = run(tmp_path, sha=head, source_branch="develop")
    assert code == 1
    message = error_of(capsys)
    assert f"reached {branch} only through a merge commit" in message
    assert "Release the merge commit instead" in message
    code, out = run(tmp_path, pulls(**{"12": merged(merge)}), pr="12", source_branch="develop")
    assert (code, out["tag"], out["branch"]) == (0, "v1.13.1", branch)


def test_a_commit_from_before_the_last_release_of_its_branch_is_refused(repo, tmp_path, capsys):
    # major opens a line that has no release yet, so the rule "the commit contains the last release
    # of its line" has nothing to compare with. The branch itself has: it released v1.14.0 later.
    git(repo, "tag", "v1.13.0")
    older = commit(repo, "Older (#11)")
    commit(repo, "Released as 1.14.0 (#12)")
    git(repo, "tag", "v1.14.0")
    newest = commit(repo, "Newest (#13)")
    origin(repo, "develop")
    code, _ = run(tmp_path, sha=older, bump="major", source_branch="develop")
    assert code == 1
    message = error_of(capsys)
    assert "develop has released v1.14.0 after the commit" in message
    assert "which builds on v1.13.0. Release a commit that contains v1.14.0" in message
    code, out = run(tmp_path, sha=newest, bump="major", source_branch="develop")
    assert (code, out["tag"], out["previous-tag"]) == (0, "v2.0.0", "v1.14.0")


# --- an exact version instead of a bump -----------------------------------------------------


def test_the_first_release_can_be_given_as_an_exact_version(repo, tmp_path, capsys):
    # No release tag yet: a bump counts from 0.0.0 and cannot give 2.0.0.
    target = commit(repo, "Ready for the first release (#339)")
    origin(repo, "develop")
    code, out = run(tmp_path, sha=target, version="2.0.0", bump="patch", source_branch="develop")
    assert code == 0
    assert (out["tag"], out["version"], out["previous-tag"], out["branch"]) == ("v2.0.0", "2.0.0", "", "develop")
    assert out["version-code"] == "200050000"
    assert "version 2.0.0 was given: bump (patch) is ignored." in capsys.readouterr().out


@pytest.mark.parametrize(
    ("version", "tag"),
    [("1.13.1", "v1.13.1"), ("v1.13.1", "v1.13.1"), ("1.15.0", "v1.15.0"), ("1.13.7", "v1.13.7"), ("2.0.0", "v2.0.0")],
)
def test_an_exact_version_replaces_the_bump(repo, tmp_path, version, tag):
    git(repo, "tag", "v1.13.0")
    target = commit(repo, "Fix (#12)")
    origin(repo, "develop")
    # The bump is not read at all, so even a value that is no bump does not matter.
    code, out = run(tmp_path, sha=target, version=version, bump="", source_branch="develop")
    assert (code, out["tag"], out["previous-tag"], out["prs"]) == (0, tag, "v1.13.0", "- #12 Fix")


@pytest.mark.parametrize(
    ("version", "message"),
    [
        ("1.13.0", "version 1.13.0 was given, and tag v1.13.0 exists already"),
        ("1.12.0", "version 1.12.0 was given, and tag v1.12.0 exists already"),
        ("1.12.5", "version 1.12.5 was given, and the commit"),
        ("0.9.0", "A release has to be higher than the last release its commit contains"),
        ("1.13", "'1.13' is not a version of the form X.Y.Z"),
        ("1.14.0-rc.1", "is not a version of the form X.Y.Z"),
        ("01.14.0", "is not a version of the form X.Y.Z"),
        ("latest", "is not a version of the form X.Y.Z"),
        ("21.0.0", "above the limit of 2100000000"),
    ],
)
def test_an_exact_version_that_cannot_be_released(repo, tmp_path, capsys, version, message):
    git(repo, "tag", "v1.12.0")
    commit(repo, "Released (#11)")
    git(repo, "tag", "v1.13.0")
    target = commit(repo, "Fix (#12)")
    origin(repo, "develop")
    code, out = run(tmp_path, sha=target, version=version, source_branch="develop")
    assert (code, out) == (1, {})
    assert message in error_of(capsys)


def test_an_exact_version_keeps_to_its_line(lines, repo, tmp_path, capsys):
    # On release/1.x only a 1.x version: 2.x is released from develop, and 2.0.0 exists there.
    code, _ = run(tmp_path, sha=lines["h2"], version="2.1.0", source_branch="develop")
    assert code == 1
    assert "the version given is v2.1.0, but the last release of major 2 is v2.0.0" in error_of(capsys)
    # Also for a major that has no release yet.
    code, _ = run(tmp_path, sha=lines["h2"], version="3.0.0", source_branch="develop")
    assert code == 1
    assert "is on release/1.x, and v3.0.0 is a release of major 3" in error_of(capsys)
    code, out = run(tmp_path, sha=lines["h2"], version="1.14.0", source_branch="develop")
    assert (code, out["tag"], out["previous-tag"], out["branch"]) == (0, "v1.14.0", "v1.13.1", "release/1.x")
    # And develop cannot take a 1.x version: the commit contains v2.0.0 already.
    code, _ = run(tmp_path, sha=lines["c"], version="1.14.0", source_branch="develop")
    assert code == 1
    assert "version 1.14.0 was given, and the commit" in error_of(capsys)
    # A commit of develop from before 2.0.0 cannot repeat what the maintenance branch released.
    code, _ = run(tmp_path, sha=lines["a"], version="1.13.5", source_branch="develop")
    assert code == 1
    assert "the last release of major 1 is v1.13.1, which the commit does not contain" in error_of(capsys)


def test_an_exact_version_still_needs_something_new(repo, tmp_path, capsys):
    target = commit(repo, "Released (#1)")
    git(repo, "tag", "v1.0.0")
    origin(repo, "develop")
    code, _ = run(tmp_path, sha=target, version="2.0.0", source_branch="develop")
    assert code == 1
    assert "is the commit of v1.0.0: there is nothing new to release" in error_of(capsys)


@pytest.mark.parametrize(
    ("branch", "message"),
    [("trunk", "branch trunk is not in this checkout"), ("-x", "source-branch is not a branch name"),
     ("a b", "source-branch is not a branch name")],
)
def test_the_source_branch_has_to_exist(repo, tmp_path, capsys, branch, message):
    target = commit(repo, "Fix (#1)")
    origin(repo, "develop")
    code, _ = run(tmp_path, sha=target, source_branch=branch)
    assert code == 1
    assert message in error_of(capsys)


def test_a_shallow_checkout_is_refused(repo, tmp_path, capsys, monkeypatch):
    git(repo, "tag", "v1.0.0")
    commit(repo, "Fix (#2)")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--depth", "1", repo.as_uri(), str(clone))
    monkeypatch.chdir(clone)
    code, _ = run(tmp_path, sha=git(clone, "rev-parse", "HEAD"))
    assert code == 1
    assert "fetch-depth: 0" in error_of(capsys)


# --- the pull request list ----------------------------------------------------------------


def test_pull_requests_are_read_from_the_first_parent_history(repo, tmp_path):
    git(repo, "tag", "v1.0.0")
    commit(repo, "fix: Close the session (#823)", "A long description.\n\nCo-Authored-By: Someone")
    # A merge commit: its pull request title is the first line of the body.
    git(repo, "checkout", "-q", "-b", "feature")
    commit(repo, "Work in progress")
    commit(repo, "More work in progress (#999)")
    git(repo, "checkout", "-q", "develop")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge pull request #827 from acme/feature", "-m", "Add kiosk context", "feature")
    # A pull request that closes an issue has two numbers; the last one is the pull request.
    commit(repo, "fix(designsystem): Close the dropdown (#230) (#333)")
    commit(repo, "Hotfix pushed   directly")
    target = commit(repo, "한글 제목 (#334)")
    code, out = run(tmp_path, sha=target)
    assert code == 0
    assert out["prs"].splitlines() == [
        "- #823 fix: Close the session",
        "- #827 Add kiosk context",
        "- #333 fix(designsystem): Close the dropdown (#230)",
        "- `" + git(repo, "rev-parse", "HEAD~1")[:7] + "` Hotfix pushed directly",
        "- #334 한글 제목",
    ]


def test_a_merge_commit_without_a_body_keeps_its_subject(repo, tmp_path):
    git(repo, "checkout", "-q", "-b", "feature")
    commit(repo, "Work")
    git(repo, "checkout", "-q", "develop")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge pull request #5 from acme/feature", "feature")
    _, out = run(tmp_path, sha=git(repo, "rev-parse", "HEAD"))
    assert out["prs"].splitlines()[-1] == "- #5 Merge pull request #5 from acme/feature"


def test_a_pull_request_is_listed_once(repo, tmp_path):
    git(repo, "tag", "v1.0.0")
    commit(repo, "Add it (#7)")
    target = commit(repo, 'Revert "Add it (#7)" (#7)')
    _, out = run(tmp_path, sha=target)
    assert out["prs"] == "- #7 Add it"


def test_a_long_list_is_cut(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(release_version, "MAX_LISTED", 3)
    git(repo, "tag", "v1.0.0")
    for number in range(1, 6):
        target = commit(repo, f"Change {number} (#{number})")
    _, out = run(tmp_path, sha=target)
    assert out["prs"].splitlines() == ["- #1 Change 1", "- #2 Change 2", "- #3 Change 3", "- … and 2 more"]


def test_a_commit_subject_cannot_forge_an_output(repo, tmp_path):
    git(repo, "tag", "v1.0.0")
    target = commit(repo, "Innocent (#1)", "tag=v9.9.9")
    commit_two = commit(repo, "Merge pull request #2 from acme/x", "EOF\ntag=v9.9.9")
    code, out = run(tmp_path, sha=commit_two)
    assert code == 0
    assert out["tag"] == "v1.0.1"
    assert out["prs"] == "- #1 Innocent\n- #2 EOF"
    assert target != commit_two


# --- inputs ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        ({"bump": "Patch"}, "bump must be one of patch, minor, major"),
        ({"bump": ""}, "bump must be one of patch, minor, major"),
        ({"tag_prefix": "v v"}, "tag-prefix may hold only"),
        ({"tag_prefix": "-v"}, "tag-prefix may hold only"),
        ({"tag_prefix": "v*"}, "tag-prefix may hold only"),
        ({"operation": "bump"}, "operation must be next or code"),
    ],
)
def test_invalid_inputs_fail(repo, tmp_path, capsys, inputs, message):
    code, out = run(tmp_path, sha=git(repo, "rev-parse", "HEAD"), **inputs)
    assert (code, out) == (1, {})
    assert message in error_of(capsys)


def test_a_version_the_formula_cannot_hold_is_refused(repo, tmp_path, capsys):
    git(repo, "tag", "v1.999.0")
    target = commit(repo, "Fix (#1)")
    code, _ = run(tmp_path, sha=target, bump="minor")
    assert code == 1
    assert "minor version 1000 is above 999" in error_of(capsys)


# --- operation: code ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("inputs", "outputs"),
    [
        ({"version": "v1.13.1"}, {"version": "1.13.1", "version-code": "101350001"}),
        ({"version": "1.13.1", "kind": "release"}, {"version": "1.13.1", "version-code": "101350001"}),
        ({"version": "v0.1.0", "kind": "qa", "qa_sequence": "412"}, {"version": "0.1.0", "version-code": "100412"}),
        ({"version": "app-v2.0.0", "tag_prefix": "app-v"}, {"version": "2.0.0", "version-code": "200050000"}),
    ],
)
def test_code(tmp_path, inputs, outputs):
    code, out = run(tmp_path, operation="code", **{"kind": "release", **inputs})
    assert (code, out) == (0, outputs)


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        ({"version": "v1.13"}, "is not a version of the form X.Y.Z"),
        ({"version": "v1.13.1-qa.3"}, "is not a version of the form X.Y.Z"),
        ({"version": ""}, "is not a version of the form X.Y.Z"),
        ({"version": "1.0.0", "kind": "debug"}, "kind must be one of release, qa"),
        ({"version": "1.0.0", "kind": "qa"}, "qa-sequence must be a whole number"),
        ({"version": "1.0.0", "kind": "qa", "qa_sequence": "-3"}, "qa-sequence must be a whole number"),
        ({"version": "1.0.0", "kind": "qa", "qa_sequence": "0"}, "QA sequence number 0 is outside 1..49999"),
        ({"version": "1.0.0", "kind": "qa", "qa_sequence": "50000"}, "QA sequence number 50000 is outside 1..49999"),
        ({"version": "1.0.50000"}, "patch version 50000 is outside 0..49999"),
        ({"version": "21.0.0"}, "above the limit of 2100000000"),
    ],
)
def test_code_refuses_what_it_cannot_compute(tmp_path, capsys, inputs, message):
    code, out = run(tmp_path, operation="code", **{"kind": "release", **inputs})
    assert (code, out) == (1, {})
    assert message in error_of(capsys)


def test_the_action_passes_every_input_to_the_script():
    import re

    action = (Path(__file__).parent / "action.yml").read_text()
    read = set(re.findall(r'env\.get\("([A-Z_]+)"', (Path(__file__).parent / "release_version.py").read_text()))
    passed = set(re.findall(r"^        ([A-Z_]+): \$\{\{ inputs\.", action, re.MULTILINE))
    assert passed == read - {"GITHUB_REPOSITORY", "GITHUB_API_URL", "GITHUB_OUTPUT"}
