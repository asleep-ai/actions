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
        "version-code": str({"0.0.1": 50_100, "0.1.0": 150_000, "1.0.0": 100_050_000}[version]),
        "branch": "",
        # Only a prerelease has one before it, and only a plan with a consequence has a notice.
        "previous-prerelease-tag": "",
        "notice": "",
    }


@pytest.mark.parametrize(
    ("bump", "version", "code"),
    [("patch", "1.13.1", "101350100"), ("minor", "1.14.0", "101450000"), ("major", "2.0.0", "200050000")],
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
    assert out["version-code"] == "101350200"
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
        ({"operation": "bump"}, "operation must be one of next, code, qa, changes"),
        ({"prerelease": "rc"}, "prerelease must be empty or one of preview"),
        ({"prerelease": "Preview"}, "prerelease must be empty or one of preview"),
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
        ({"version": "v1.13.1"}, {"version": "1.13.1", "version-code": "101350100"}),
        ({"version": "1.13.1", "kind": "release"}, {"version": "1.13.1", "version-code": "101350100"}),
        ({"version": "v0.1.0", "kind": "qa", "qa_sequence": "412"}, {"version": "0.1.0", "version-code": "100412"}),
        ({"version": "app-v2.0.0", "tag_prefix": "app-v"}, {"version": "2.0.0", "version-code": "200050000"}),
        # A preview is named whole: its number is part of the version name and of the code.
        (
            {"version": "v2.0.0-preview.3", "kind": "preview"},
            {"version": "2.0.0-preview.3", "version-code": "200049003"},
        ),
        (
            {"version": "1.14.0-preview.12", "kind": "preview"},
            {"version": "1.14.0-preview.12", "version-code": "101449012"},
        ),
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
        ({"version": "1.0.0", "kind": "debug"}, "kind must be one of release, preview, qa"),
        ({"version": "1.0.0", "kind": "qa"}, "qa-sequence must be a whole number"),
        ({"version": "1.0.0", "kind": "qa", "qa_sequence": "-3"}, "qa-sequence must be a whole number"),
        ({"version": "1.0.0", "kind": "qa", "qa_sequence": "0"}, "QA sequence number 0 is outside 1..48999"),
        # 49000 is where the previews of the minor version start.
        ({"version": "1.0.0", "kind": "qa", "qa_sequence": "49000"}, "QA sequence number 49000 is outside 1..48999"),
        # One hundred codes per patch release: the release and the label builds after it.
        ({"version": "1.0.500"}, "patch version 500 is outside 0..499"),
        ({"version": "21.0.0"}, "above the limit of 2100000000"),
        # A preview tag is not a release, and a release is not a preview.
        ({"version": "v2.0.0-preview.1"}, "is not a version of the form X.Y.Z"),
        ({"version": "2.0.0", "kind": "preview"}, "is not a prerelease of the form X.Y.Z-preview.N"),
        ({"version": "2.0.0-rc.1", "kind": "preview"}, "is not a prerelease of the form X.Y.Z-preview.N"),
        ({"version": "2.0.0-preview.0", "kind": "preview"}, "is not a prerelease of the form X.Y.Z-preview.N"),
        ({"version": "2.0.0-preview.01", "kind": "preview"}, "is not a prerelease of the form X.Y.Z-preview.N"),
        ({"version": "2.0.0-preview.1000", "kind": "preview"}, "preview number 1000 is outside 1..999"),
        # A tag somebody pushed by hand: a patch release has no preview.
        ({"version": "v1.14.2-preview.1", "kind": "preview"}, "is a preview of a patch release"),
    ],
)
def test_code_refuses_what_it_cannot_compute(tmp_path, capsys, inputs, message):
    code, out = run(tmp_path, operation="code", **{"kind": "release", **inputs})
    assert (code, out) == (1, {})
    assert message in error_of(capsys)


# --- a preview of the next version ---------------------------------------------------------


def test_a_preview_is_the_next_version_with_a_number(repo, tmp_path, capsys):
    # No release yet, as in one calling repository: the first release will be 2.0.0.
    commit(repo, "Feature (#1)")
    target = commit(repo, "Ready for the demo (#2)")
    origin(repo, "develop")
    code, out = run(tmp_path, sha=target, version="2.0.0", prerelease="preview", source_branch="develop")
    assert code == 0
    assert out == {
        "version": "2.0.0-preview.1",
        "tag": "v2.0.0-preview.1",
        "previous-tag": "",
        "previous-prerelease-tag": "",
        "target-sha": target,
        "prs": "- `" + git(repo, "rev-parse", "HEAD~2")[:7] + "` Initial commit\n- #1 Feature\n- #2 Ready for the demo",
        # Above every QA build of 2.0 and below the release 2.0.0.
        "version-code": "200049001",
        "branch": "develop",
        "notice": "",
    }
    assert "Next prerelease: v2.0.0-preview.1 (versionCode 200049001)" in capsys.readouterr().out


def test_previews_of_one_version_are_numbered_in_order_and_list_what_each_adds(repo, tmp_path):
    git(repo, "tag", "v1.13.0")
    first = commit(repo, "Redesign (#30)")
    origin(repo, "develop")
    code, out = run(tmp_path, sha=first, bump="major", prerelease="preview", source_branch="develop")
    assert (code, out["tag"], out["previous-tag"], out["prs"]) == (0, "v2.0.0-preview.1", "v1.13.0", "- #30 Redesign")
    git(repo, "tag", "v2.0.0-preview.1", first)
    second = commit(repo, "Fix found at the demo (#31)")
    origin(repo, "develop")
    code, out = run(tmp_path, sha=second, bump="major", prerelease="preview", source_branch="develop")
    assert code == 0
    assert (out["tag"], out["version"], out["version-code"]) == ("v2.0.0-preview.2", "2.0.0-preview.2", "200049002")
    # The release it leads up to still counts from the last release; the list starts at the preview before.
    assert (out["previous-tag"], out["previous-prerelease-tag"]) == ("v1.13.0", "v2.0.0-preview.1")
    assert out["prs"] == "- #31 Fix found at the demo"
    # A gap in the numbers is not filled: the next one is above the highest.
    git(repo, "tag", "v2.0.0-preview.7", second)
    third = commit(repo, "Another fix (#32)")
    origin(repo, "develop")
    _, out = run(tmp_path, sha=third, bump="major", prerelease="preview", source_branch="develop")
    assert (out["tag"], out["previous-prerelease-tag"]) == ("v2.0.0-preview.8", "v2.0.0-preview.7")


def test_previews_do_not_change_what_the_release_is(repo, tmp_path):
    # The release of the version is planned as if its previews did not exist, and lists everything
    # since the last release.
    git(repo, "tag", "v1.13.0")
    commit(repo, "Redesign (#30)")
    git(repo, "tag", "v2.0.0-preview.1")
    git(repo, "tag", "v1.14.0-preview.4")
    git(repo, "tag", "qa-develop")
    target = commit(repo, "Last fix (#31)")
    origin(repo, "develop")
    for bump, tag, code in (("patch", "v1.13.1", "101350100"), ("major", "v2.0.0", "200050000")):
        status, out = run(tmp_path, sha=target, bump=bump, source_branch="develop")
        assert (status, out["tag"], out["previous-tag"], out["version-code"]) == (0, tag, "v1.13.0", code)
        assert out["prs"] == "- #30 Redesign\n- #31 Last fix"
        # The commit contains the previews of the version, so there is nothing to say about them.
        assert (out["previous-prerelease-tag"], out["notice"]) == ("", "")
    # Each version numbers its own previews.
    _, out = run(tmp_path, sha=target, bump="minor", prerelease="preview", source_branch="develop")
    assert out["tag"] == "v1.14.0-preview.5"
    _, out = run(tmp_path, sha=target, bump="major", prerelease="preview", source_branch="develop")
    assert out["tag"] == "v2.0.0-preview.2"


def test_a_preview_has_to_add_something_to_the_one_before_it(repo, tmp_path, capsys):
    git(repo, "tag", "v1.13.0")
    older = commit(repo, "Older (#30)")
    previewed = commit(repo, "Previewed (#31)")
    git(repo, "tag", "v2.0.0-preview.1")
    origin(repo, "develop")
    code, _ = run(tmp_path, sha=previewed, bump="major", prerelease="preview", source_branch="develop")
    assert code == 1
    assert "is the commit of v2.0.0-preview.1: there is nothing new to preview" in error_of(capsys)
    # A commit from before the last preview would carry a higher versionCode and older code.
    code, _ = run(tmp_path, sha=older, bump="major", prerelease="preview", source_branch="develop")
    assert code == 1
    message = error_of(capsys)
    assert "does not contain v2.0.0-preview.1, the last preview of 2.0.0" in message
    assert "choose a commit that contains it" in message


def test_a_preview_is_held_to_the_rules_of_its_release(lines, repo, tmp_path, capsys):
    # The commit of the last release has nothing new; a version that is released has no preview; and
    # the lines keep apart.
    code, _ = run(tmp_path, sha=lines["b"], bump="minor", prerelease="preview", source_branch="develop")
    assert code == 1
    assert "is the commit of v2.0.0: there is nothing new to release" in error_of(capsys)
    code, _ = run(tmp_path, sha=lines["c"], version="2.0.0", prerelease="preview", source_branch="develop")
    assert code == 1
    assert "version 2.0.0 was given, and tag v2.0.0 exists already" in error_of(capsys)
    code, _ = run(tmp_path, sha=lines["h2"], bump="major", prerelease="preview", source_branch="develop")
    assert code == 1
    assert "the last release of major 2 is v2.0.0, which the commit does not contain" in error_of(capsys)
    code, out = run(tmp_path, sha=lines["h2"], bump="minor", prerelease="preview", source_branch="develop")
    assert (code, out["tag"], out["branch"], out["version-code"]) == (0, "v1.14.0-preview.1", "release/1.x", "101449001")
    assert out["notice"] == ""


def test_a_patch_release_has_no_preview(lines, repo, tmp_path, capsys):
    # 2.0.0 is out. A preview of 2.0.1 would be numbered below it, in the block of 2.0, and no
    # device with 2.0.0 could take it: the plan is refused and says what to choose.
    code, out = run(tmp_path, sha=lines["c"], bump="patch", prerelease="preview", source_branch="develop")
    assert (code, out) == (1, {})
    message = error_of(capsys)
    assert "bump patch plans 2.0.1, a patch release, and a preview is of a new minor or major version (X.Y.0)" in message
    assert "Choose bump minor or major for the preview" in message
    assert "make a label build" in message
    # The same for a version that was given, and for the patch of a maintenance line.
    code, _ = run(tmp_path, sha=lines["c"], version="2.0.1", prerelease="preview", source_branch="develop")
    assert code == 1
    assert "version 2.0.1 was given, a patch release" in error_of(capsys)
    code, _ = run(tmp_path, sha=lines["h2"], prerelease="preview", source_branch="develop")
    assert code == 1
    assert "bump patch plans 1.13.2, a patch release" in error_of(capsys)
    # The next minor and the next major version are above the release that is out.
    _, out = run(tmp_path, sha=lines["c"], bump="minor", prerelease="preview", source_branch="develop")
    assert (out["tag"], out["version-code"], out["notice"]) == ("v2.1.0-preview.1", "200149001", "")
    _, out = run(tmp_path, sha=lines["c"], bump="major", prerelease="preview", source_branch="develop")
    assert (out["tag"], out["version-code"]) == ("v3.0.0-preview.1", "300049001")
    # The release of the patch itself is planned as before.
    code, out = run(tmp_path, sha=lines["c"], bump="patch", source_branch="develop")
    assert (code, out["tag"], out["version-code"]) == (0, "v2.0.1", "200050100")


def test_a_release_that_leaves_out_the_last_preview_of_its_version_says_so(repo, tmp_path, capsys):
    # preview.2 is on the demo devices, and the release installs over it. A release cut from the
    # commit before that preview would take its changes away again: allowed, and said.
    git(repo, "tag", "v1.13.0")
    older = commit(repo, "In preview 1 (#30)")
    git(repo, "tag", "v1.14.0-preview.1")
    previewed = commit(repo, "Fix found at the demo (#31)")
    git(repo, "tag", "v1.14.0-preview.2")
    later = commit(repo, "After the demo (#32)")
    origin(repo, "develop")
    code, out = run(tmp_path, sha=older, bump="minor", source_branch="develop")
    assert (code, out["tag"]) == (0, "v1.14.0")
    assert f"({older[:7]}) does not contain v1.14.0-preview.2, the last preview of 1.14.0" in out["notice"]
    assert "Release a commit that contains v1.14.0-preview.2, unless leaving it out is meant" in out["notice"]
    assert "::warning title=release-version::" in capsys.readouterr().out
    for target in (previewed, later):
        code, out = run(tmp_path, sha=target, bump="minor", source_branch="develop")
        assert (code, out["tag"], out["notice"]) == (0, "v1.14.0", "")
    # The previews of another version say nothing about this one.
    code, out = run(tmp_path, sha=older, bump="major", source_branch="develop")
    assert (code, out["tag"], out["notice"]) == (0, "v2.0.0", "")


def test_the_previews_of_a_version_are_limited_by_their_range_of_codes(repo, tmp_path, capsys):
    git(repo, "tag", "v1.13.0")
    commit(repo, "Previewed (#30)")
    git(repo, "tag", "v1.14.0-preview.999")
    target = commit(repo, "One more (#31)")
    origin(repo, "develop")
    code, _ = run(tmp_path, sha=target, bump="minor", prerelease="preview", source_branch="develop")
    assert code == 1
    assert "preview number 1000 is outside 1..999" in error_of(capsys)


# --- operation: qa -------------------------------------------------------------------------


def run_qa(tmp_path, sha: str, ref: str = "refs/heads/develop", sequence: str = "57", **inputs):
    """Plan a QA build the way android-qa.yml does: the event's ref and commit, and the run number."""
    return run(tmp_path, operation="qa", sha=sha, ref=ref, qa_sequence=sequence, source_branch="develop", **inputs)


def rolling(repo: Path, tag: str, name: str, code: int, revision: str = "HEAD") -> None:
    """Move a rolling tag the way the publish job of android-qa.yml does: annotated, naming its build."""
    git(repo, "tag", "-f", "-a", tag, "-m", f"{name} ({code})", revision)


def test_a_qa_build_leads_up_to_the_patch_after_the_last_release(repo, tmp_path, capsys):
    git(repo, "tag", "v1.13.0")
    target = commit(repo, "Fix (#12)")
    origin(repo, "develop")
    code, out = run_qa(tmp_path, target)
    assert code == 0
    assert out == {
        "version": "1.13.1",
        "version-name": "1.13.1-qa.57",
        # The block of 1.13, below its previews and releases.
        "version-code": "101300057",
        "kind": "qa",
        "number": "57",
        "line": "develop",
        "major": "",
        "release-tag": "qa-develop",
        "previous-tag": "v1.13.0",
        "branch": "develop",
        "target-sha": target,
    }
    log = capsys.readouterr().out
    assert "Version: 1.13.1 (the patch after v1.13.0)" in log
    assert "Version name: 1.13.1-qa.57 (versionCode 101300057, QA build 57)" in log


def test_a_qa_build_before_any_release(repo, tmp_path):
    target = commit(repo, "Feature (#1)")
    origin(repo, "develop")
    _, out = run_qa(tmp_path, target)
    assert (out["version-name"], out["version-code"], out["previous-tag"]) == ("0.0.1-qa.57", "57", "")
    # One calling repository's first release is 2.0.0, and its QA builds are named after it.
    _, out = run_qa(tmp_path, target, next_version="2.0.0")
    assert (out["version"], out["version-name"], out["version-code"]) == ("2.0.0", "2.0.0-qa.57", "200000057")


def test_next_version_is_used_until_that_version_is_released(repo, tmp_path, capsys):
    git(repo, "tag", "v1.13.0")
    target = commit(repo, "Redesign (#30)")
    origin(repo, "develop")
    _, out = run_qa(tmp_path, target, next_version="v2.0.0")
    assert (out["version-name"], out["version-code"], out["release-tag"]) == ("2.0.0-qa.57", "200000057", "qa-develop")
    _, out = run_qa(tmp_path, target, next_version="1.14.0")
    assert (out["version-name"], out["version-code"]) == ("1.14.0-qa.57", "101400057")
    # A line reaches its next major version at most: a value above it is a slip of the pen, and
    # builds of it would lock every device out of the versions in between.
    capsys.readouterr()
    code, out = run_qa(tmp_path, target, next_version="3.0.0")
    assert (code, out["version-name"]) == (0, "1.13.1-qa.57")
    assert "next-version 3.0.0 is not used: it is above 2.0.0, the next major version after v1.13.0" in capsys.readouterr().out
    # The file still says 2.0.0 after 2.0.0 was released: the builds go on to the patch after it,
    # in the same block of codes, instead of failing on every push.
    git(repo, "tag", "v2.0.0")
    after = commit(repo, "Fix after the release (#31)")
    origin(repo, "develop")
    code, out = run_qa(tmp_path, after, next_version="2.0.0", sequence="58")
    assert (code, out["version-name"], out["version-code"]) == (0, "2.0.1-qa.58", "200000058")
    assert "next-version 2.0.0 is not used: v2.0.0 is released already" in capsys.readouterr().out
    # A value below the last release is not used either.
    _, out = run_qa(tmp_path, after, next_version="1.99.0", sequence="59")
    assert out["version-name"] == "2.0.1-qa.59"


def test_each_line_builds_its_own_version_and_rolling_tag(lines, tmp_path):
    #   develop:      base(v1.13.0) ── a ── b(v2.0.0) ── c
    #   release/1.x:    └── h1(v1.13.1) ── h2
    _, out = run_qa(tmp_path, lines["c"])
    assert (out["version-name"], out["version-code"], out["release-tag"]) == ("2.0.1-qa.57", "200000057", "qa-develop")
    code, out = run_qa(tmp_path, lines["h2"], ref="refs/heads/release/1.x", sequence="58")
    assert code == 0
    assert out == {
        "version": "1.13.2",
        # No branch in the name: the version tells a build of the maintenance line from one of develop.
        "version-name": "1.13.2-qa.58",
        "version-code": "101300058",
        "kind": "qa",
        "number": "58",
        "line": "release-1.x",
        "major": "1",
        # Not a tag that starts with v: the release tags stay vX.Y.Z only.
        "release-tag": "qa-release-1.x",
        "previous-tag": "v1.13.1",
        "branch": "release/1.x",
        "target-sha": lines["h2"],
    }


def test_the_source_branch_builds_the_next_major_once_its_last_major_has_a_maintenance_branch(repo, tmp_path, capsys):
    # develop has not released 2.0.0 yet, and 1.x has its branch: what develop builds is 2.0.0, as
    # the version bump would require of it.
    git(repo, "tag", "v1.13.0")
    origin(repo, "release/1.x")
    target = commit(repo, "Redesign (#30)")
    origin(repo, "develop")
    code, out = run_qa(tmp_path, target)
    assert (code, out["version-name"], out["version-code"], out["major"]) == (0, "2.0.0-qa.57", "200000057", "")
    assert "the next major: v1.13.0 is the last release, and release/1.x has its line" in capsys.readouterr().out
    # A version of the maintained major is not built here, as a hint or for one build.
    _, out = run_qa(tmp_path, target, next_version="1.14.0")
    assert out["version-name"] == "2.0.0-qa.57"
    code, _ = run_qa(tmp_path, target, version="1.14.0")
    assert code == 1
    assert "version 1.14.0 was given for this QA build, and major 1 is built from release/1.x" in error_of(capsys)
    # Its line, major 2, has no release yet: there is nothing a label build could sit above.
    code, _ = run_qa(tmp_path, target, label="ble-test")
    assert code == 1
    assert "contains no release of major 2, the line develop builds" in error_of(capsys)


def test_a_maintenance_branch_builds_its_own_major_only(lines, repo, tmp_path, capsys):
    ref = "refs/heads/release/1.x"
    _, out = run_qa(tmp_path, lines["h2"], ref=ref, next_version="2.0.0")
    assert out["version-name"] == "1.13.2-qa.57"
    _, out = run_qa(tmp_path, lines["h2"], ref=ref, next_version="1.14.0")
    assert out["version-name"] == "1.14.0-qa.57"
    code, _ = run_qa(tmp_path, lines["h2"], ref=ref, version="2.0.0")
    assert code == 1
    assert "version 2.0.0 was given for this QA build, and v2.0.0 is released already" in error_of(capsys)
    code, _ = run_qa(tmp_path, lines["h2"], ref=ref, version="2.1.0")
    assert code == 1
    assert "version 2.1.0 was given for this QA build, and release/1.x builds major 1 only" in error_of(capsys)
    code, out = run_qa(tmp_path, lines["h2"], ref=ref, version="1.14.0")
    assert (code, out["version-name"], out["release-tag"]) == (0, "1.14.0-qa.57", "qa-release-1.x-1.14.0")
    # A maintenance branch that was cut before its major had a release.
    git(repo, "checkout", "-q", "-b", "release/3.x", lines["c"])
    target = commit(repo, "First change of 3.x (#40)")
    origin(repo, "release/3.x", "release/3.x")
    code, out = run_qa(tmp_path, target, ref="refs/heads/release/3.x")
    assert (code, out["version-name"], out["major"]) == (0, "3.0.0-qa.57", "3")
    code, _ = run_qa(tmp_path, target, ref="refs/heads/release/3.x", label="ble-test")
    assert code == 1
    assert "contains no release of major 3, the line release/3.x builds" in error_of(capsys)
    # And one that holds a release of a later major is not that major's branch.
    git(repo, "checkout", "-q", "-b", "release/0.x", lines["c"])
    origin(repo, "release/0.x", "release/0.x")
    code, _ = run_qa(tmp_path, lines["c"], ref="refs/heads/release/0.x")
    assert code == 1
    assert "release/0.x contains v2.0.0, a release of a later major version" in error_of(capsys)


def test_a_version_for_one_build_is_the_next_minor_or_major_and_is_published_apart(repo, tmp_path, capsys):
    # A build that has to install over the release that is out takes the block of the next minor
    # or major version. It does not replace the rolling build of the branch, whose devices could
    # not take the builds that follow it.
    git(repo, "tag", "v1.13.0")
    target = commit(repo, "Fix (#12)")
    origin(repo, "develop")
    for version, name, code, tag in (
        ("2.0.0", "2.0.0-qa.57", "200000057", "qa-develop-2.0.0"),
        ("v1.14.0", "1.14.0-qa.57", "101400057", "qa-develop-1.14.0"),
    ):
        status, out = run_qa(tmp_path, target, version=version)
        assert (status, out["version-name"], out["version-code"], out["release-tag"]) == (0, name, code, tag)
        assert (out["kind"], out["line"]) == ("qa", "develop")
    assert 101_350_000 < 101_400_057
    neither = "it is neither 1.14.0 nor 2.0.0, the next minor and the next major version after v1.13.0"
    for version, message in (
        # A typing error is not a version the line can reach: 11.3.1 for 1.13.1, 3.0.0 for 2.0.0.
        ("11.3.1", neither),
        ("3.0.0", neither),
        ("1.15.0", neither + ". Without version, the build is one of 1.13.1"),
        ("2.1.0", neither),
        ("1.13.0", "version 1.13.0 was given for this QA build, and v1.13.0 is released already"),
        ("1.12.9", "it is not higher than v1.13.0, the last release the commit contains"),
        ("2.0", "'2.0' is not a version of the form X.Y.Z"),
        ("2.0.0-qa.1", "is not a version of the form X.Y.Z"),
    ):
        status, out = run_qa(tmp_path, target, version=version)
        assert (status, out) == (1, {})
        assert message in error_of(capsys)


def test_a_version_for_one_build_is_later_than_the_version_the_branch_builds_anyway(repo, tmp_path, capsys):
    # The same version as the rolling build would be a second build of one name and one
    # versionCode, in a pre-release of its own (qa-develop-1.13.1 next to qa-develop). A lower one is
    # in a block of codes below the builds that are installed.
    git(repo, "tag", "v1.13.0")
    target = commit(repo, "Fix (#12)")
    origin(repo, "develop")
    without = "was given for this QA build, and the QA builds of develop lead up to"
    for version, hint, message in (
        # The next patch is what the build is without version.
        ("1.13.1", "", f"version 1.13.1 {without} that very version without it (the patch after v1.13.0). Run without version"),
        # The calling file already heads for the next minor, or for the next major.
        ("1.14.0", "1.14.0", f"version 1.14.0 {without} that very version without it (next-version). Run without version"),
        ("2.0.0", "2.0.0", f"version 2.0.0 {without} that very version without it (next-version). Run without version"),
        ("1.14.0", "2.0.0", f"version 1.14.0 {without} 2.0.0, a later version, without it (next-version). Run without version"),
        ("1.13.1", "1.14.0", f"version 1.13.1 {without} 1.14.0, a later version, without it (next-version). Run without version"),
    ):
        status, out = run_qa(tmp_path, target, version=version, next_version=hint)
        assert (status, out) == (1, {}), version
        assert message in error_of(capsys)
    # What is above it and within reach is built, apart.
    status, out = run_qa(tmp_path, target, version="2.0.0", next_version="1.14.0")
    assert (status, out["version-name"], out["release-tag"]) == (0, "2.0.0-qa.57", "qa-develop-2.0.0")
    # Once release/1.x has the line of the last release, the branch builds the next major anyway, and
    # the next minor is the maintenance branch's: nothing is left for version to name.
    origin(repo, "release/1.x")
    status, _ = run_qa(tmp_path, target, version="2.0.0")
    assert status == 1
    assert f"version 2.0.0 {without} that very version without it (the next major: " in error_of(capsys)


def test_no_version_is_given_before_the_first_release(repo, tmp_path, capsys):
    # Before a first release the branch has one version to build, the one it is heading for. The
    # builds are of it without version; a lower one and a higher one are both refused.
    target = commit(repo, "Feature (#1)")
    origin(repo, "develop")
    for version, hint, heading in (("2.0.0", "2.0.0", "that very version"), ("1.0.0", "2.0.0", "2.0.0, a later version,"), ("0.0.1", "", "that very version")):
        code, out = run_qa(tmp_path, target, version=version, next_version=hint)
        assert (code, out) == (1, {})
        message = error_of(capsys)
        assert f"version {version} was given for this QA build, and the QA builds of develop lead up to {heading} without it" in message
        assert message.rstrip().endswith("Run without version")
    for version, hint, ceiling in (("2.1.0", "2.0.0", "2.0.0"), ("3.0.0", "2.0.0", "2.0.0"), ("0.1.0", "", "0.0.1")):
        code, out = run_qa(tmp_path, target, version=version, next_version=hint)
        assert (code, out) == (1, {})
        message = error_of(capsys)
        assert f"version {version} was given for this QA build, and it is above {ceiling}" in message
        assert "A later version is named in next-version" in message


def test_a_label_build_is_numbered_right_after_the_last_release_of_its_line(lines, repo, tmp_path, capsys):
    #   develop:      base(v1.13.0) ── a ── b(v2.0.0) ── c
    #   release/1.x:    └── h1(v1.13.1) ── h2
    code, out = run_qa(tmp_path, lines["c"], label="ble-test")
    assert code == 0
    assert out == {
        # Named after the patch it leads up to, numbered after the release it was made after:
        # 2.0.0 (200050000) < 2.0.1-ble-test.1 < 2.0.1 (200050100).
        "version": "2.0.1",
        "version-name": "2.0.1-ble-test.1",
        "version-code": "200050001",
        "kind": "label",
        "number": "1",
        "line": "develop",
        "major": "",
        "release-tag": "qa-develop-ble-test",
        "previous-tag": "v2.0.0",
        "branch": "develop",
        "target-sha": lines["c"],
    }
    log = capsys.readouterr().out
    assert "Version: 2.0.1 (label build 1 after v2.0.0)" in log
    assert "Version name: 2.0.1-ble-test.1 (versionCode 200050001, label build 1)" in log
    _, out = run_qa(tmp_path, lines["h2"], ref="refs/heads/release/1.x", label="sweetsleep")
    assert (out["version-name"], out["version-code"], out["release-tag"], out["major"]) == (
        "1.13.2-sweetsleep.1", "101350101", "qa-release-1.x-sweetsleep", "1",
    )
    # The run number is not part of a label build.
    code, out = run_qa(tmp_path, lines["c"], label="ble-test", sequence="")
    assert (code, out["version-name"]) == (0, "2.0.1-ble-test.1")


def test_label_builds_after_one_release_are_counted_from_the_rolling_tags(lines, repo, tmp_path, capsys):
    # What was published says which numbers are taken: each rolling tag names the build it holds.
    rolling(repo, "qa-develop-ble-test", "2.0.1-ble-test.1", 200_050_001, lines["c"])
    _, out = run_qa(tmp_path, lines["c"], label="ble-test")
    assert (out["version-name"], out["version-code"]) == ("2.0.1-ble-test.2", "200050002")
    # One count for every label after the release: a later build installs over an earlier one,
    # whatever the two are called.
    rolling(repo, "qa-develop-knox", "2.0.1-knox.5", 200_050_005, lines["c"])
    _, out = run_qa(tmp_path, lines["c"], label="ble-test")
    assert (out["version-name"], out["version-code"]) == ("2.0.1-ble-test.6", "200050006")
    _, out = run_qa(tmp_path, lines["c"], label="third")
    assert out["version-name"] == "2.0.1-third.6"
    # Not counted: the rolling QA build, a build given a version, a tag without a message (it names
    # no build), and the label builds after another release.
    rolling(repo, "qa-develop", "2.0.1-qa.57", 200_000_057, lines["c"])
    rolling(repo, "qa-develop-2.1.0", "2.1.0-qa.58", 200_100_058, lines["c"])
    git(repo, "tag", "qa-develop-plain", lines["c"])
    rolling(repo, "qa-release-1.x-sweetsleep", "1.13.2-sweetsleep.3", 101_350_103, lines["h2"])
    _, out = run_qa(tmp_path, lines["c"], label="ble-test")
    assert out["version-name"] == "2.0.1-ble-test.6"
    _, out = run_qa(tmp_path, lines["h2"], ref="refs/heads/release/1.x", label="ble-test")
    assert (out["version-name"], out["version-code"]) == ("1.13.2-ble-test.4", "101350104")
    # After the next patch release the count starts again, above that release.
    git(repo, "checkout", "-q", "develop")
    git(repo, "tag", "v2.0.1")
    after = commit(repo, "Fix after 2.0.1 (#33)")
    origin(repo, "develop")
    _, out = run_qa(tmp_path, after, label="ble-test")
    assert (out["version-name"], out["version-code"]) == ("2.0.2-ble-test.1", "200050101")
    # Ninety-nine codes lie between a release and its next patch.
    rolling(repo, "qa-develop-knox", "2.0.2-knox.99", 200_050_199, after)
    code, out = run_qa(tmp_path, after, label="ble-test")
    assert (code, out) == (1, {})
    message = error_of(capsys)
    assert "99 label builds were published after v2.0.1" in message
    assert "Release 2.0.2: the label builds after it are counted from 1 again" in message


def test_a_label_build_needs_a_release_to_sit_above(repo, tmp_path, capsys):
    target = commit(repo, "Feature (#1)")
    origin(repo, "develop")
    for hint, major in (("", "0"), ("2.0.0", "2")):
        code, out = run_qa(tmp_path, target, label="ble-test", next_version=hint)
        assert (code, out) == (1, {})
        message = error_of(capsys)
        assert f"label 'ble-test' was given, and the commit ({target[:7]}) contains no release of major {major}" in message
        assert "Before the first release, test with the QA builds of develop or with a preview" in message
    # An earlier commit of the branch, from before a release that is out: the build would be named
    # after a version that exists.
    git(repo, "tag", "v1.13.0")
    older = commit(repo, "Fix (#12)")
    commit(repo, "Released as 1.13.1 (#13)")
    git(repo, "tag", "v1.13.1")
    origin(repo, "develop")
    code, _ = run_qa(tmp_path, older, label="ble-test")
    assert code == 1
    message = error_of(capsys)
    assert f"v1.13.1 is released already, and the commit ({older[:7]}) does not contain it: its last release is v1.13.0" in message
    # A label build takes no version: its place is right after the release.
    code, _ = run_qa(tmp_path, git(repo, "rev-parse", "HEAD"), label="ble-test", version="1.14.0")
    assert code == 1
    assert "version (1.14.0) and label (ble-test) were both given" in error_of(capsys)


@pytest.mark.parametrize(
    "label",
    [
        "BLE", "ble_test", "ble--test", "-ble", "ble-", "ble test", "v1", "a" * 41, "ble\ntest", "1.14.0",
        # The names of the other kinds of build, and of the branch.
        "qa", "preview", "release", "develop",
    ],
)
def test_a_label_that_cannot_be_part_of_a_name_is_refused(repo, tmp_path, capsys, label):
    git(repo, "tag", "v1.13.0")
    target = commit(repo, "Fix (#12)")
    origin(repo, "develop")
    code, out = run_qa(tmp_path, target, label=label)
    if label == "v1":
        # Letters and digits: a label may look like anything but another kind of build.
        assert (code, out["release-tag"], out["version-name"]) == (0, "qa-develop-v1", "1.13.1-v1.1")
        return
    assert (code, out) == (1, {})
    message = error_of(capsys)
    assert "label must be lowercase letters and digits" in message or "is the name of another kind of build" in message


@pytest.mark.parametrize(
    "ref",
    [
        "refs/heads/feature/ble",
        "refs/heads/main",
        "refs/heads/release/next",
        "refs/heads/release/1.x/fix",
        "refs/heads/release/01.x",
        "refs/tags/v1.13.0",
        "refs/pull/12/merge",
        "develop",
        "",
    ],
)
def test_only_the_source_branch_and_maintenance_branches_are_built(repo, tmp_path, capsys, ref):
    git(repo, "tag", "v1.13.0")
    target = commit(repo, "Fix (#12)")
    for branch in ("develop", "main", "feature/ble", "release/next"):
        origin(repo, branch)
    code, out = run_qa(tmp_path, target, ref=ref)
    assert (code, out) == (1, {})
    message = error_of(capsys)
    assert "a QA build is built from develop or from a maintenance branch release/<major>.x" in message
    assert f"this run was started for {ref or 'no ref'}." in message
    assert "merge the change into develop or the maintenance branch" in message


def test_the_commit_has_to_be_one_the_branch_pointed_at(repo, tmp_path, capsys):
    git(repo, "tag", "v1.13.0")
    git(repo, "checkout", "-q", "-b", "feature")
    head = commit(repo, "Work on the feature")
    git(repo, "checkout", "-q", "develop")
    commit(repo, "The base branch moved on (#11)")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge pull request #12 from acme/feature", "feature")
    merge = git(repo, "rev-parse", "HEAD")
    origin(repo, "develop")
    assert run_qa(tmp_path, merge)[0] == 0
    # An earlier commit of the branch is still one of its commits: the branch moved on while the run waited.
    assert run_qa(tmp_path, git(repo, "rev-parse", "HEAD~1"))[0] == 0
    code, _ = run_qa(tmp_path, head)
    assert code == 1
    assert "is not a commit develop itself pointed at" in error_of(capsys)
    code, _ = run_qa(tmp_path, merge, ref="refs/heads/release/1.x")
    assert code == 1
    assert "branch release/1.x is not in this checkout" in error_of(capsys)
    code, _ = run_qa(tmp_path, "develop")
    assert code == 1
    assert "sha must be a commit id" in error_of(capsys)


def test_the_number_of_a_qa_build_is_the_sequence_plus_the_offset(repo, tmp_path, capsys):
    # A new workflow file counts its runs from 1 again, below the builds that are installed.
    target = commit(repo, "Feature (#1)")
    origin(repo, "develop")
    _, out = run_qa(tmp_path, target, sequence="3", qa_sequence_offset="1000", next_version="2.0.0")
    assert (out["number"], out["version-name"], out["version-code"]) == ("1003", "2.0.0-qa.1003", "200001003")
    _, out = run_qa(tmp_path, target, sequence="45000", qa_sequence_offset="-44000")
    assert out["number"] == "1000"
    for sequence, offset, message in (
        # 49000 would be the code of a preview.
        ("48990", "10", "QA sequence number 49000 is outside 1..48999"),
        ("49000", "", "QA sequence number 49000 is outside 1..48999"),
        ("5", "-5", "QA sequence number 0 is outside 1..48999"),
        ("", "", "qa-sequence must be a whole number"),
        ("5", "1.5", "qa-sequence-offset must be a whole number"),
        ("5", "+5", "qa-sequence-offset must be a whole number"),
    ):
        code, out = run_qa(tmp_path, target, sequence=sequence, qa_sequence_offset=offset)
        assert (code, out) == (1, {})
        assert message in error_of(capsys)
    _, out = run_qa(tmp_path, target, sequence="48999")
    assert out["version-code"] == "48999"


def test_a_commit_before_the_published_build_is_still_planned(repo, tmp_path):
    # Every pushed commit is built: the check of its build is what a release of that commit
    # requires. Whether the build is published is the publish job's decision (operation changes
    # says that the rolling tag is ahead), not a reason to refuse the build.
    git(repo, "tag", "v1.13.0")
    older = commit(repo, "Older (#12)")
    newer = commit(repo, "Newer (#13)")
    origin(repo, "develop")
    rolling(repo, "qa-develop", "1.13.1-qa.58", 101_300_058, newer)
    code, out = run_qa(tmp_path, older)
    assert (code, out["version-name"], out["release-tag"]) == (0, "1.13.1-qa.57", "qa-develop")
    _, changes = run(tmp_path, operation="changes", sha=older, since="qa-develop")
    assert changes["stale"] == "true"


def test_qa_and_preview_tags_are_not_releases(repo, tmp_path):
    git(repo, "tag", "v1.13.0")
    commit(repo, "Fix (#12)")
    for tag in ("qa-develop", "qa-release-1.x", "v1.14.0-preview.2", "v2.0.0-preview.1", "v1.13.1-ble-test.90"):
        git(repo, "tag", tag)
    target = commit(repo, "Another fix (#13)")
    origin(repo, "develop")
    _, out = run_qa(tmp_path, target)
    assert (out["version-name"], out["previous-tag"]) == ("1.13.1-qa.57", "v1.13.0")
    _, out = run(tmp_path, sha=target, source_branch="develop")
    assert (out["tag"], out["previous-tag"]) == ("v1.13.1", "v1.13.0")


def test_a_qa_build_needs_the_whole_history(repo, tmp_path, capsys, monkeypatch):
    git(repo, "tag", "v1.0.0")
    commit(repo, "Fix (#2)")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--depth", "1", repo.as_uri(), str(clone))
    monkeypatch.chdir(clone)
    code, _ = run_qa(tmp_path, git(clone, "rev-parse", "HEAD"))
    assert code == 1
    assert "fetch-depth: 0" in error_of(capsys)


# --- operation: changes --------------------------------------------------------------------


def run_changes(tmp_path, sha: str, since: str = "qa-develop"):
    return run(tmp_path, operation="changes", sha=sha, since=since)


def test_changes_since_the_build_before(repo, tmp_path):
    git(repo, "tag", "v1.13.0")
    earlier = commit(repo, "In the build before (#12)")
    git(repo, "tag", "qa-develop")
    commit(repo, "fix: Close the session (#13)")
    target = commit(repo, "Hotfix pushed directly")
    code, out = run_changes(tmp_path, target)
    assert code == 0
    assert out == {
        "target-sha": target,
        "since": earlier,
        # A tag without a message names the commit itself.
        "since-object": earlier,
        "prs": "- #13 fix: Close the session\n- `" + target[:7] + "` Hotfix pushed directly",
        "stale": "false",
        "unchanged": "false",
        "code-taken-by": "",
    }


def test_changes_name_the_object_of_an_annotated_rolling_tag(repo, tmp_path):
    # The publish job moves an annotated tag. The list starts at its commit, and since-object is
    # the tag object: what the ref of the tag holds, which the job compares before it moves the tag.
    git(repo, "tag", "v1.13.0")
    earlier = commit(repo, "In the build before (#12)")
    rolling(repo, "qa-develop", "1.13.1-qa.57", 101_300_057)
    target = commit(repo, "Fix (#13)")
    _, out = run_changes(tmp_path, target)
    tag_object = git(repo, "rev-parse", "refs/tags/qa-develop")
    assert tag_object != earlier
    assert (out["since"], out["since-object"], out["prs"]) == (earlier, tag_object, "- #13 Fix")
    # No tag, no object.
    _, out = run_changes(tmp_path, target, since="qa-develop-ble-test")
    assert (out["since"], out["since-object"]) == ("v1.13.0", "")


def test_changes_say_which_other_pre_release_holds_the_versioncode(lines, repo, tmp_path, capsys):
    #   develop:      base(v1.13.0) ── a ── b(v2.0.0) ── c
    #   release/1.x:    └── h1(v1.13.1) ── h2
    # Two label runs were planned while neither was published: both read the tags, and both got
    # number 1 after v2.0.0.
    plans = [run_qa(tmp_path, lines["c"], label=label)[1] for label in ("ble-test", "knox")]
    assert [plan["version-code"] for plan in plans] == ["200050001", "200050001"]
    assert [plan["release-tag"] for plan in plans] == ["qa-develop-ble-test", "qa-develop-knox"]

    def taken(since: str, code: str) -> str:
        return run(tmp_path, operation="changes", sha=lines["c"], since=since, version_code=code)[1]["code-taken-by"]

    # The first to publish finds the code free and moves its tag.
    assert taken("qa-develop-ble-test", "200050001") == ""
    rolling(repo, "qa-develop-ble-test", "2.0.1-ble-test.1", 200_050_001, lines["c"])
    # The second asks again when it is about to publish, and is told who has the code.
    capsys.readouterr()
    assert taken("qa-develop-knox", "200050001") == "qa-develop-ble-test"
    assert "qa-develop-ble-test holds a build of the versionCode 200050001." in capsys.readouterr().out
    # A new run of it is planned from the tags as they are now, and gets the next number.
    _, again = run_qa(tmp_path, lines["c"], label="knox")
    assert (again["version-name"], again["version-code"]) == ("2.0.1-knox.2", "200050002")
    assert taken("qa-develop-knox", "200050002") == ""
    # The build's own pre-release is not another one while it names this very commit: the same run
    # published again, or run again after "Published, but not completely".
    assert taken("qa-develop-ble-test", "200050001") == ""
    # It is one when it names another commit. Two runs of one label, on b and then on c, were both
    # planned as number 3 while neither was published; the one of c publishes second and finds its
    # own tag holding the code for b. (Planned after that, it would have been number 4.)
    rolling(repo, "qa-develop-sweet", "2.0.1-sweet.3", 200_050_003, lines["b"])
    capsys.readouterr()
    out = run(tmp_path, operation="changes", sha=lines["c"], since="qa-develop-sweet", version_code="200050003")[1]
    assert (out["code-taken-by"], out["stale"], out["unchanged"]) == ("qa-develop-sweet", "false", "false")
    assert "qa-develop-sweet holds a build of the versionCode 200050003." in capsys.readouterr().out
    _, again = run_qa(tmp_path, lines["c"], label="sweet")
    assert again["version-code"] == "200050004"
    # The run of b, started again: its tag names its own commit.
    assert run(tmp_path, operation="changes", sha=lines["b"], since="qa-develop-sweet", version_code="200050003")[1]["code-taken-by"] == ""
    git(repo, "tag", "-d", "qa-develop-sweet")
    # Only a tag that names its build counts, and the label builds of another line have other codes.
    git(repo, "tag", "qa-develop-plain", lines["c"])
    rolling(repo, "qa-release-1.x-ble-test", "1.13.2-ble-test.1", 101_350_101, lines["h2"])
    assert taken("qa-develop-knox", "200050002") == ""
    assert taken("qa-develop-knox", "101350101") == "qa-release-1.x-ble-test"
    # Without a versionCode nothing is asked.
    _, out = run_changes(tmp_path, lines["c"], since="qa-develop-knox")
    assert out["code-taken-by"] == ""
    code, out = run(tmp_path, operation="changes", sha=lines["c"], since="qa-develop-knox", version_code="1e3")
    assert (code, out) == (1, {})
    assert "version-code must be a whole number" in error_of(capsys)


def test_changes_of_the_first_build_start_at_the_last_release(repo, tmp_path):
    commit(repo, "Released (#11)")
    git(repo, "tag", "v1.13.0")
    git(repo, "tag", "v1.13.1-preview.1")
    target = commit(repo, "Fix (#12)")
    _, out = run_changes(tmp_path, target)
    assert (out["since"], out["prs"], out["stale"], out["unchanged"]) == ("v1.13.0", "- #12 Fix", "false", "false")
    # Without a tag to compare with, too.
    _, out = run_changes(tmp_path, target, since="")
    assert (out["since"], out["prs"]) == ("v1.13.0", "- #12 Fix")


def test_changes_without_any_release_cover_the_history(repo, tmp_path):
    target = commit(repo, "Feature (#1)")
    _, out = run_changes(tmp_path, target)
    assert out["since"] == ""
    assert out["prs"].splitlines()[-1] == "- #1 Feature"


def test_changes_of_the_same_commit_and_of_an_older_one(repo, tmp_path):
    git(repo, "tag", "v1.13.0")
    older = commit(repo, "Older (#12)")
    newer = commit(repo, "Newer (#13)")
    git(repo, "tag", "qa-develop", newer)
    _, out = run_changes(tmp_path, newer)
    assert (out["since"], out["prs"], out["stale"], out["unchanged"]) == (newer, "", "false", "true")
    _, out = run_changes(tmp_path, older)
    assert (out["since"], out["prs"], out["stale"], out["unchanged"]) == (newer, "", "true", "false")


def test_changes_after_the_branch_was_rewritten_start_at_the_last_release(repo, tmp_path):
    # The build before is on a history the branch no longer has.
    git(repo, "tag", "v1.13.0")
    git(repo, "checkout", "-q", "-b", "rewritten")
    commit(repo, "Dropped later (#12)")
    git(repo, "tag", "qa-develop")
    git(repo, "checkout", "-q", "develop")
    target = commit(repo, "Kept (#13)")
    _, out = run_changes(tmp_path, target)
    assert (out["since"], out["prs"], out["stale"]) == ("v1.13.0", "- #13 Kept", "false")


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        ({"since": "-x"}, "since is not a tag name"),
        ({"since": "qa develop"}, "since is not a tag name"),
        ({"sha": "develop"}, "sha must be a commit id"),
        ({"sha": ""}, "sha must be a commit id"),
    ],
)
def test_changes_refuses_what_it_cannot_read(repo, tmp_path, capsys, inputs, message):
    arguments = {"sha": git(repo, "rev-parse", "HEAD"), "since": "qa-develop", **inputs}
    code, out = run(tmp_path, operation="changes", **arguments)
    assert (code, out) == (1, {})
    assert message in error_of(capsys)


def test_the_action_passes_every_input_to_the_script():
    import re

    action = (Path(__file__).parent / "action.yml").read_text()
    read = set(re.findall(r'env\.get\("([A-Z_]+)"', (Path(__file__).parent / "release_version.py").read_text()))
    passed = set(re.findall(r"^        ([A-Z_]+): \$\{\{ inputs\.", action, re.MULTILINE))
    assert passed == read - {"GITHUB_REPOSITORY", "GITHUB_API_URL", "GITHUB_OUTPUT"}
