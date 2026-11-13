"""Tests of android-version-bump.yml.

The run steps are executed as the shell GitHub would run: the checks step
against a local stand-in for the GitHub API, the branch and tag steps against
throwaway git repositories with a bare repository as the remote.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from test_pr_check import OWN_ACTION, ROOT, WORKFLOWS, commit, executable, git, load, run_script, step, trigger

ENTRY = "android-version-bump.yml"
EXAMPLE = ROOT / "android-ci" / "version-bump.yml.example"
README = ROOT / "android-ci" / "README.md"


@pytest.fixture(scope="module")
def jobs() -> dict:
    return load(ENTRY)["jobs"]


# --- plan: the summary ------------------------------------------------------------------


def test_the_summary_states_the_two_versions_and_what_is_included(jobs, tmp_path):
    summary = tmp_path / "summary"
    prs = "- #857 fix: recognize sleep mats\n- #858 -n 100% `code` $(not run) %s\n- `3ff9b1a` Hotfix"
    env = {
        "TAG": "v1.13.1",
        "PREVIOUS_TAG": "v1.13.0",
        "PREVIOUS_PRERELEASE_TAG": "",
        "NOTICE": "",
        "VERSION_CODE": "101350100",
        "TARGET_SHA": "3ff9b1a3" + "0" * 32,
        "LINE_BRANCH": "develop",
        "PRS": prs,
        "PR": "858",
        "BUMP": "patch",
        "VERSION_GIVEN": "",
        "PRERELEASE": "",
        "DRY_RUN": "false",
        "GITHUB_SERVER_URL": "https://github.invalid",
        "GITHUB_REPOSITORY": "acme/app",
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    script = step(jobs["plan"], "Write the plan to the summary")["run"]
    result = run_script(script, env, tmp_path)
    assert result.returncode == 0, result.stderr
    text = summary.read_text()
    assert text.startswith("### v1.13.0 → v1.13.1\n")
    assert "- Commit: [`3ff9b1a`](https://github.invalid/acme/app/commit/3ff9b1a3" in text
    assert ", the merge commit of #858\n" in text
    assert "- Release line: `develop`\n" in text
    assert "- versionCode: 101350100\n" in text
    # The titles are written as they are.
    assert prs + "\n" in text
    # A run that releases says so at the top: nothing waits between the plan and the tag.
    assert "**Dry run.**" not in text
    assert "**Not a dry run.** When every check below passes and `confirm` names `v1.13.1`, the commit is tagged" in text
    assert "is ignored" not in text
    assert "not a release" not in text and "[!WARNING]" not in text

    # A first release whose version was given, as a dry run.
    summary.write_text("")
    env.update({"PREVIOUS_TAG": "", "TAG": "v2.0.0", "PRS": "", "PR": "", "VERSION_GIVEN": "2.0.0", "DRY_RUN": "true"})
    result = run_script(script, env, tmp_path)
    assert result.returncode == 0, result.stderr
    text = summary.read_text()
    assert text.startswith("### no release yet → v2.0.0\n")
    assert "**Dry run.** Nothing is tagged, pushed or started." in text
    assert "Not a dry run" not in text
    assert "merge commit of" not in text
    assert "- The version was given (`version`); `bump` (patch) is ignored.\n" in text
    assert "Nothing listed." in text

    # A preview: the summary says that it is not the release, and what its list starts at.
    summary.write_text("")
    env.update({
        "PREVIOUS_TAG": "v1.13.0", "TAG": "v2.0.0-preview.2", "PREVIOUS_PRERELEASE_TAG": "v2.0.0-preview.1",
        "PRS": "- #31 Fix found at the demo", "PRERELEASE": "preview", "VERSION_CODE": "200049002",
    })
    result = run_script(script, env, tmp_path)
    assert result.returncode == 0, result.stderr
    text = summary.read_text()
    assert text.startswith("### v1.13.0 → v2.0.0-preview.2\n")
    assert "- A preview, not a release: it is published as a GitHub pre-release, and `v2.0.0` is still to be released.\n" in text
    assert "- The preview before this one is `v2.0.0-preview.1`; the list below is what this one adds to it.\n" in text
    assert "- versionCode: 200049002\n" in text
    assert "[!WARNING]" not in text

    # A plan that is allowed and has a consequence: the sentence of release-version, as a warning.
    # A release whose commit leaves out the last preview of its version is one.
    summary.write_text("")
    notice = "the commit (3ff9b1a) does not contain v2.0.0-preview.2, the last preview of 2.0.0 (100% sure, %s)"
    env.update({"TAG": "v2.0.0", "PREVIOUS_PRERELEASE_TAG": "", "PRERELEASE": "", "NOTICE": notice})
    result = run_script(script, env, tmp_path)
    assert result.returncode == 0, result.stderr
    text = summary.read_text()
    assert f"> [!WARNING]\n> {notice}.\n" in text
    assert "The preview before this one" not in text


# --- plan: the release workflow of the commit ----------------------------------------------

SHARED_RELEASE = """\
name: Release
on:
  push:
    tags: ['v[0-9]+.[0-9]+.[0-9]+']
  workflow_dispatch:
jobs:
  release:
    uses: asleep-ai/actions/.github/workflows/android-release.yml@android-ci/v1
    secrets: inherit
"""
# The workflow a repository had before it called the shared one: it builds whatever ref it is
# started for. The shared workflow is named in a comment only.
EARLIER_RELEASE = """\
name: Release Build
# To be replaced by asleep-ai/actions/.github/workflows/android-release.yml@android-ci/v1.
#   uses: asleep-ai/actions/.github/workflows/android-release.yml@android-ci/v1
on:
  workflow_dispatch:
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: ./gradlew assembleRelease
"""


def check_release_workflow(jobs, repository: Path, target: str, workflow: str = "release.yml"):
    env = {"RELEASE_WORKFLOW": workflow, "TARGET_SHA": target}
    return run_script(step(jobs["plan"], "Check the release workflow of the commit")["run"], env, repository)


@pytest.fixture
def repository(tmp_path) -> Path:
    path = tmp_path / "repository"
    path.mkdir()
    git(path, "init", "-q", "-b", "develop")
    commit(path, {"app/Main.kt": "fun main() {}\n"}, "base")
    return path


@pytest.mark.parametrize(
    "content",
    [
        SHARED_RELEASE,
        # Another owner, a commit as the ref, quotes, and the trigger in the short form.
        "on: [push, workflow_dispatch]\njobs:\n  release:\n    uses: 'acme/actions/.github/workflows/android-release.yml@"
        + "0" * 40 + "'\n",
        'on:\n  workflow_dispatch:\njobs:\n  release:\n    if: ${{ !inputs.label }}\n'
        '    uses: "asleep-ai/actions/.github/workflows/android-release.yml@android-ci/v1"\n',
    ],
)
def test_a_commit_whose_release_workflow_calls_the_shared_one_passes(jobs, repository, content):
    target = commit(repository, {".github/workflows/release.yml": content}, "ci: release from tags (#871)")
    result = check_release_workflow(jobs, repository, target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f".github/workflows/release.yml at {target[:7]} calls android-release.yml" in result.stdout


def test_a_commit_from_before_the_shared_release_workflow_is_refused(jobs, repository):
    # One caller's first release: the merge of main into develop contains the last release tag and
    # passed its checks, and its release.yml is still the earlier workflow, which would build the new
    # tag with the version of its own files. The file of the branch tip does not help that commit.
    before = commit(repository, {".github/workflows/release.yml": EARLIER_RELEASE}, "Merge main into develop (#872)")
    after = commit(repository, {".github/workflows/release.yml": SHARED_RELEASE}, "ci: release from tags (#871)")
    result = check_release_workflow(jobs, repository, before)
    assert result.returncode == 1
    assert "::error title=The commit's release workflow is not the shared one::" in result.stdout
    assert f".github/workflows/release.yml at {before[:7]} does not call android-release.yml" in result.stdout
    assert "the merge commit of the pull request that brought the workflow in, or a later one" in result.stdout
    assert check_release_workflow(jobs, repository, after).returncode == 0


def test_a_commit_without_the_release_workflow_is_refused(jobs, repository):
    target = git(repository, "rev-parse", "HEAD")
    result = check_release_workflow(jobs, repository, target)
    assert result.returncode == 1
    assert f"::error title=The commit has no release workflow::{target[:7]} holds no .github/workflows/release.yml" in result.stdout
    # The file under another name is not the one that is started.
    target = commit(repository, {".github/workflows/publish.yml": SHARED_RELEASE}, "ci (#2)")
    assert check_release_workflow(jobs, repository, target).returncode == 1
    assert check_release_workflow(jobs, repository, target, workflow="publish.yml").returncode == 0


def test_a_release_workflow_that_cannot_be_started_is_refused(jobs, repository):
    content = SHARED_RELEASE.replace("  workflow_dispatch:\n", "").replace(
        "name: Release\n", "name: Release\n# workflow_dispatch was removed\n"
    )
    target = commit(repository, {".github/workflows/release.yml": content}, "ci (#2)")
    result = check_release_workflow(jobs, repository, target)
    assert result.returncode == 1
    assert "::error title=The commit's release workflow cannot be started::" in result.stdout
    assert "has no workflow_dispatch trigger" in result.stdout


def test_without_a_release_workflow_nothing_is_checked(jobs, repository):
    result = check_release_workflow(jobs, repository, git(repository, "rev-parse", "HEAD"), workflow="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "nothing to check" in result.stdout


@pytest.mark.parametrize("workflow", ["../../build.gradle.kts", "release", "Release Build", "release.yml:x", "a/release.yml"])
def test_release_workflow_has_to_be_a_file_name(jobs, repository, workflow):
    target = commit(repository, {".github/workflows/release.yml": SHARED_RELEASE}, "ci (#2)")
    result = check_release_workflow(jobs, repository, target, workflow=workflow)
    assert result.returncode == 1
    assert "release-workflow is not the file name of a workflow" in result.stdout


def test_the_examples_release_workflow_passes_the_check(jobs, repository):
    # The file a calling repository copies is one the version bump accepts.
    example = (ROOT / "android-ci" / "release.yml.example").read_text()
    target = commit(repository, {".github/workflows/release.yml": example}, "ci (#2)")
    result = check_release_workflow(jobs, repository, target)
    assert result.returncode == 0, result.stdout + result.stderr


# --- plan: the confirmation -----------------------------------------------------------------


def check_confirmation(jobs, tmp_path, confirm: str, dry_run: str = "false", tag: str = "v1.13.1", previous: str = "v1.13.0"):
    summary = tmp_path / "summary"
    summary.write_text("")
    env = {
        "DRY_RUN": dry_run,
        "CONFIRM": confirm,
        "TAG": tag,
        "VERSION": tag.removeprefix("v"),
        "PREVIOUS_TAG": previous,
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    result = run_script(step(jobs["plan"], "Check the confirmation")["run"], env, tmp_path)
    return result, summary.read_text()


@pytest.mark.parametrize("confirm", ["v1.13.1", "1.13.1", "  v1.13.1\n"])
def test_a_release_is_confirmed_by_the_planned_version(jobs, tmp_path, confirm):
    result, summary = check_confirmation(jobs, tmp_path, confirm)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Confirmed: v1.13.1." in result.stdout
    assert "`confirm` names the planned version, `v1.13.1`." in summary


@pytest.mark.parametrize("confirm", ["", "  ", "\n"])
def test_a_release_without_confirm_is_refused(jobs, tmp_path, confirm):
    result, summary = check_confirmation(jobs, tmp_path, confirm)
    assert result.returncode == 1
    assert "::error title=The release is not confirmed::The plan computed v1.13.1 (v1.13.0 → v1.13.1), and confirm is empty" in result.stdout
    assert "start the run again with confirm=v1.13.1" in result.stdout
    assert "#### Not released: The release is not confirmed" in summary
    assert "Nothing is tagged." in summary


@pytest.mark.parametrize(
    "confirm", ["v1.13.2", "1.14.0", "v1.13.0", "v2.0.0", "1.13.10", "v1.13.01", "v1.13.1-preview.1", "v1.13.1-rc.1"]
)
def test_confirm_of_another_version_is_refused_and_names_the_planned_one(jobs, tmp_path, confirm):
    # A wrong pr or bump: the person expected one version and the plan computed another.
    result, summary = check_confirmation(jobs, tmp_path, confirm)
    assert result.returncode == 1
    assert (
        "::error title=confirm does not name the planned version::The plan computed v1.13.1 (v1.13.0 → v1.13.1), "
        f"and confirm is {confirm}. Nothing is tagged."
    ) in result.stdout
    assert "Confirmed" not in result.stdout + summary


@pytest.mark.parametrize(
    "confirm",
    [
        "yes", "true", "v2.5", "v1.13.1-rc", "v1.13.1-preview", "v1.13.1-Preview.1", "v1.13.1-preview.1.2",
        "v1.13.1 v1.13.1", "v1.13.1\n::notice::x", "$(id)",
    ],
)
def test_confirm_that_is_not_a_version_is_refused_without_being_printed(jobs, tmp_path, confirm):
    result, summary = check_confirmation(jobs, tmp_path, confirm)
    assert result.returncode == 1
    assert "::error title=confirm is not a version::The plan computed v1.13.1" in result.stdout
    assert "::notice::" not in result.stdout
    assert confirm not in result.stdout and confirm not in summary


def test_a_first_release_is_confirmed_like_any_other(jobs, tmp_path):
    result, _ = check_confirmation(jobs, tmp_path, "", tag="v2.0.0", previous="")
    assert result.returncode == 1
    assert "The plan computed v2.0.0 (no release yet → v2.0.0), and confirm is empty" in result.stdout
    assert check_confirmation(jobs, tmp_path, "2.0.0", tag="v2.0.0", previous="")[0].returncode == 0


@pytest.mark.parametrize("confirm", ["v2.0.0-preview.2", "2.0.0-preview.2", " v2.0.0-preview.2 "])
def test_a_preview_is_confirmed_by_its_whole_name(jobs, tmp_path, confirm):
    result, summary = check_confirmation(jobs, tmp_path, confirm, tag="v2.0.0-preview.2")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Confirmed: v2.0.0-preview.2." in result.stdout


@pytest.mark.parametrize("confirm", ["v2.0.0", "2.0.0", "v2.0.0-preview.1", "v2.0.0-preview.3", "v2.0.0-rc.2", "v2.0.1-preview.2"])
def test_a_preview_is_not_confirmed_by_the_version_alone_or_by_another_number(jobs, tmp_path, confirm):
    # The release of the version and a preview of it are two different things to start, and the
    # number says which preview: one that someone else cut in between changes it.
    result, _ = check_confirmation(jobs, tmp_path, confirm, tag="v2.0.0-preview.2")
    assert result.returncode == 1
    assert (
        "::error title=confirm does not name the planned version::The plan computed v2.0.0-preview.2 "
        f"(v1.13.0 → v2.0.0-preview.2), and confirm is {confirm}. Nothing is tagged."
    ) in result.stdout
    assert "start the run again with confirm=v2.0.0-preview.2" in result.stdout
    # And the other way round: a release is not confirmed by the name of a preview of it.
    result, _ = check_confirmation(jobs, tmp_path, "v2.0.0-preview.2", tag="v2.0.0")
    assert result.returncode == 1


def test_a_dry_run_of_a_preview_says_the_whole_name_to_pass(jobs, tmp_path):
    result, summary = check_confirmation(jobs, tmp_path, "", dry_run="true", tag="v2.0.0-preview.2")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "`dry_run` off and `confirm` set to `v2.0.0-preview.2`" in summary


@pytest.mark.parametrize("confirm", ["", "v1.13.1", "v9.9.9", "anything"])
def test_a_dry_run_does_not_read_confirm_and_says_what_to_pass(jobs, tmp_path, confirm):
    result, summary = check_confirmation(jobs, tmp_path, confirm, dry_run="true")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "start the run again with dry_run=false and confirm=v1.13.1" in result.stdout
    assert "#### To release this plan" in summary
    assert "`dry_run` off and `confirm` set to `v1.13.1`" in summary
    assert "Confirmed" not in result.stdout + summary


def test_the_confirmation_is_the_last_step_of_the_plan_and_the_tag_job_needs_the_plan(jobs):
    # A refusal by any earlier step, or by this one, leaves the tag job without the job it needs.
    plan, tag = jobs["plan"], jobs["tag"]
    last = plan["steps"][-1]
    assert last["name"] == "Check the confirmation"
    assert "if" not in last, "the step runs on every run of the plan"
    assert last["env"] == {
        "DRY_RUN": "${{ inputs.dry_run }}",
        "CONFIRM": "${{ inputs.confirm }}",
        "TAG": "${{ steps.version.outputs.tag }}",
        "VERSION": "${{ steps.version.outputs.version }}",
        "PREVIOUS_TAG": "${{ steps.version.outputs.previous-tag }}",
    }
    assert tag["needs"] == "plan"
    # The version the tag job tags is the one that was confirmed.
    assert jobs["plan"]["outputs"]["tag"] == "${{ steps.version.outputs.tag }}"
    assert step(tag, "Push the tag and the main branch")["env"]["TAG"] == "${{ needs.plan.outputs.tag }}"


# --- plan: the checks of the commit --------------------------------------------------------

SHA = "ab" * 20


def check_run(name="pr-check / result", conclusion="success", status="completed", started="2026-11-13T01:00:00Z", **more):
    run = {"name": name, "status": status, "conclusion": conclusion, "started_at": started, "id": 1}
    run["html_url"] = f"https://github.invalid/runs/{run['id']}"
    return {**run, **more}


@contextlib.contextmanager
def check_runs_api():
    """A local GitHub API that answers the check-runs request with the runs set on it.

    `compare` holds the answers to "how does a tag compare with the commit", which only the QA
    build asks: one for each request, the last one from then on. Empty, the tag does not exist.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            server.requests.append((self.path, self.headers.get("Authorization")))
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)
            # `statuses` answers the next requests one by one, `status` every request after them.
            status = server.statuses.pop(0) if server.statuses else server.status
            if status == 200 and url.path.startswith(f"/repos/acme/app/compare/{SHA}...") and server.compare:
                answer = server.compare.pop(0) if len(server.compare) > 1 else server.compare[0]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(answer).encode())
                return
            if status != 200 or url.path != f"/repos/acme/app/commits/{SHA}/check-runs":
                self.send_response(status if status != 200 else 404)
                self.end_headers()
                self.wfile.write(b'{"message": "Resource not accessible by integration"}')
                return
            # As GitHub does: only the runs of the name that was asked for.
            runs = [run for run in server.runs if run["name"] == query["check_name"][0]]
            body = json.dumps(server.body if server.body is not None else {"total_count": len(runs), "check_runs": runs})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.runs, server.requests, server.status, server.statuses, server.body = [], [], 200, [], None
    server.compare = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()


@pytest.fixture
def api():
    with check_runs_api() as server:
        yield server


def check_checks(jobs, tmp_path, api, required="pr-check / result"):
    summary = tmp_path / "summary"
    summary.write_text("")
    env = {
        "TOKEN": "job-token",
        "TARGET_SHA": SHA,
        "REQUIRED_CHECKS": required,
        "GITHUB_API_URL": f"http://127.0.0.1:{api.server_port}",
        "GITHUB_REPOSITORY": "acme/app",
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    result = run_script(step(jobs["plan"], "Check the commit's checks")["run"], env, tmp_path)
    return result, summary.read_text()


def test_a_commit_whose_check_succeeded_passes(jobs, tmp_path, api):
    api.runs = [check_run(), check_run(name="pr-check / primary / verify", conclusion="failure")]
    result, summary = check_checks(jobs, tmp_path, api)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "| pr-check / result | [success](https://github.invalid/runs/1) |" in summary
    (path, authorization), = api.requests
    assert authorization == "Bearer job-token"
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(path).query) == {
        "check_name": ["pr-check / result"], "filter": ["latest"], "per_page": ["100"],
    }


@pytest.mark.parametrize(
    ("runs", "state"),
    [
        ([check_run(conclusion="failure")], "failure"),
        ([check_run(conclusion="cancelled")], "cancelled"),
        ([check_run(conclusion="skipped")], "skipped"),
        ([check_run(conclusion="neutral")], "neutral"),
        ([check_run(conclusion=None, status="in_progress")], "in_progress"),
        ([check_run(conclusion=None, status="queued")], "queued"),
    ],
)
def test_a_check_that_did_not_succeed_refuses_the_release(jobs, tmp_path, api, runs, state):
    api.runs = runs
    result, summary = check_checks(jobs, tmp_path, api)
    assert result.returncode == 1
    assert f"::error title=The commit has not passed its checks::{SHA[:7]}: 'pr-check / result' is {state}." in result.stdout
    assert f"[{state}]" in summary


def test_a_check_that_never_ran_refuses_the_release(jobs, tmp_path, api):
    api.runs = [check_run(name="another check")]
    result, summary = check_checks(jobs, tmp_path, api)
    assert result.returncode == 1
    assert "'pr-check / result' did not run on the commit" in result.stdout
    assert "| pr-check / result | not run |" in summary


def test_the_newest_run_of_a_check_counts(jobs, tmp_path, api):
    early = check_run(conclusion="failure", started="2026-11-13T01:00:00Z", id=1)
    late = check_run(conclusion="success", started="2026-11-13T02:00:00Z", id=2)
    api.runs = [late, early]
    assert check_checks(jobs, tmp_path, api)[0].returncode == 0
    api.runs = [check_run(conclusion="success", started="2026-11-13T01:00:00Z", id=1),
                check_run(conclusion="failure", started="2026-11-13T02:00:00Z", id=2)]
    assert check_checks(jobs, tmp_path, api)[0].returncode == 1


def test_every_required_check_is_reported(jobs, tmp_path, api):
    api.runs = [check_run(), check_run(name="qa / result", conclusion="failure")]
    result, summary = check_checks(jobs, tmp_path, api, required="pr-check / result\n\n  qa / result  \nlint\n")
    assert result.returncode == 1
    assert "'qa / result' is failure; 'lint' did not run on the commit" in result.stdout
    assert summary.count("\n| ") == 4
    assert len(api.requests) == 3


def test_without_required_checks_nothing_is_asked(jobs, tmp_path, api):
    result, summary = check_checks(jobs, tmp_path, api, required="\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::notice::No check is required" in result.stdout
    assert "No check is required" in summary
    assert api.requests == []


def test_an_unreadable_answer_is_not_a_pass(jobs, tmp_path, api):
    api.status = 403
    result, _ = check_checks(jobs, tmp_path, api)
    assert result.returncode == 1
    assert "Cannot read the checks" in result.stderr and "HTTP 403" in result.stderr
    api.status, api.body = 200, {"message": "no list here"}
    result, _ = check_checks(jobs, tmp_path, api)
    assert result.returncode == 1
    assert "the response has no check_runs list" in result.stderr
    # A release is decided from one reading: only android-qa.yml, which waits, asks again.
    api.requests.clear()
    api.status, api.body = 502, None
    result, _ = check_checks(jobs, tmp_path, api)
    assert result.returncode == 1 and "HTTP 502" in result.stderr
    assert len(api.requests) == 1


def test_the_default_required_checks_are_the_pull_request_check_and_the_qa_build_of_the_push(jobs):
    # The pull request check of a pushed commit does not build the release variant on a branch
    # that is released from (android-pr-check.yml leaves it to android-qa.yml), so a release
    # requires the check of that build too. The two defaults belong together.
    inputs = trigger(load(ENTRY))["inputs"]
    assert inputs["required-checks"]["default"].splitlines() == ["pr-check / result", "qa / built"]
    assert inputs["required-checks"]["default"] == trigger(load("android-release.yml"))["inputs"]["required-checks"]["default"]
    pr_check = load("android-pr-check.yml")
    assert "result" in pr_check["jobs"]
    assert trigger(pr_check)["inputs"]["release-build-on-push"]["default"] is False
    example = yaml.safe_load((ROOT / "android-ci" / "pr-check.yml.example").read_text())
    assert list(example["jobs"]) == ["pr-check"]
    assert "built" in load("android-qa.yml")["jobs"]
    example = yaml.safe_load((ROOT / "android-ci" / "qa.yml.example").read_text())
    assert list(example["jobs"]) == ["qa"]


def test_a_commit_without_the_qa_build_of_its_push_is_not_released(jobs, tmp_path, api):
    # What the default requires, read by the script: the pull request check passed, and the QA
    # build never ran on the commit (no push built it), failed, or was cancelled by hand.
    required = trigger(load(ENTRY))["inputs"]["required-checks"]["default"]
    api.runs = [check_run()]
    result, summary = check_checks(jobs, tmp_path, api, required=required)
    assert result.returncode == 1
    assert "'qa / built' did not run on the commit" in result.stdout
    assert "| qa / built | not run |" in summary
    for conclusion in ("failure", "cancelled", "skipped"):
        api.runs = [check_run(), check_run(name="qa / built", conclusion=conclusion)]
        result, _ = check_checks(jobs, tmp_path, api, required=required)
        assert result.returncode == 1 and f"'qa / built' is {conclusion}" in result.stdout
    # The build of a manual run has another name and does not stand in for it.
    api.runs = [check_run(), check_run(name="qa / built (manual run)")]
    assert check_checks(jobs, tmp_path, api, required=required)[0].returncode == 1
    api.runs = [check_run(), check_run(name="qa / built")]
    result, summary = check_checks(jobs, tmp_path, api, required=required)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "| qa / built | [success]" in summary


# --- plan: the main branch ------------------------------------------------------------------


@pytest.fixture
def clone(tmp_path) -> Path:
    """A clone of a bare remote with main and develop at one commit, as after fetch-depth: 0."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "-q", "--bare", "-b", "develop")
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "develop")
    commit(seed, {"app/Main.kt": "fun main() {}\n"}, "base")
    git(seed, "push", "-q", origin.as_uri(), "develop:develop", "develop:main")
    path = tmp_path / "clone"
    git(tmp_path, "clone", "-q", origin.as_uri(), str(path))
    return path


def remote(clone: Path, *args: str) -> str:
    return git(clone.parent / "origin.git", *args)


def check_branch(jobs, clone: Path, target: str, branch: str = "main", line: str = "develop", prerelease: str = ""):
    summary, output = clone.parent / "summary", clone.parent / "main-output"
    summary.write_text("")
    output.write_text("")
    env = {
        "MAIN_BRANCH": branch,
        "LINE_BRANCH": line,
        "SOURCE_BRANCH": "develop",
        "TARGET_SHA": target,
        "PRERELEASE": prerelease,
        "GITHUB_STEP_SUMMARY": str(summary),
        "GITHUB_OUTPUT": str(output),
    }
    result = run_script(step(jobs["plan"], "Check that the main branch can move")["run"], env, clone)
    check_branch.moved = output.read_text()
    return result, summary.read_text()


def test_main_moves_forward_to_the_commit(jobs, clone):
    commit(clone, {"a.kt": "1"}, "one (#1)")
    target = commit(clone, {"b.kt": "2"}, "two (#2)")
    result, summary = check_branch(jobs, clone, target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "`main` moves forward by 2 commit(s), from `" in summary
    assert "main moves forward by 2 commit(s)" in result.stdout
    assert check_branch.moved == "main-branch=main\n"


def test_main_at_the_commit_already(jobs, clone):
    result, summary = check_branch(jobs, clone, git(clone, "rev-parse", "HEAD"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "`main` is at the commit already." in summary
    assert check_branch.moved == "main-branch=main\n"


def test_a_release_from_a_maintenance_branch_leaves_main_where_it_is(jobs, clone):
    # main is ahead on the 2.x line, and the release is a hotfix on release/1.x: main could never
    # move forward to it, and that is no reason to refuse the release.
    base = git(clone, "rev-parse", "HEAD")
    commit(clone, {"a.kt": "2"}, "2.0 work (#30)")
    git(clone, "push", "-q", "origin", "develop:develop", "develop:main")
    git(clone, "checkout", "-q", "-b", "release/1.x", base)
    target = commit(clone, {"hotfix.kt": "1"}, "hotfix (#21)")
    result, summary = check_branch(jobs, clone, target, line="release/1.x")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "`main` is not moved: it follows the releases of `develop`, and this one is cut from `release/1.x`." in summary
    assert check_branch.moved == "main-branch=\n"


def test_a_preview_leaves_main_where_it_is(jobs, clone):
    # The branch follows the releases. A preview is not one, also when the branch could move.
    target = commit(clone, {"a.kt": "1"}, "one (#1)")
    result, summary = check_branch(jobs, clone, target, prerelease="preview")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "`main` is not moved: it follows the releases, and this is a preview." in summary
    assert check_branch.moved == "main-branch=\n"
    # Without a main-branch there is nothing to say about it.
    result, summary = check_branch(jobs, clone, target, branch="", prerelease="preview")
    assert "No branch is moved: the tag is the release." in summary


def test_main_with_a_commit_the_release_lacks_refuses(jobs, clone):
    # The earlier flow of one caller: a release commit made on main that develop never received.
    git(clone, "checkout", "-q", "-b", "main", "origin/main")
    commit(clone, {"version.txt": "1.13.0"}, "1.13.0 release")
    git(clone, "push", "-q", "origin", "main")
    git(clone, "checkout", "-q", "develop")
    target = commit(clone, {"a.kt": "1"}, "fix (#857)")
    result, summary = check_branch(jobs, clone, target)
    assert result.returncode == 1
    assert "::error title=main cannot move forward::main (" in result.stdout
    assert "holds 1 commit(s) that " + target[:7] + " lacks" in result.stdout
    assert "1.13.0 release" in result.stdout
    assert "Branch" not in summary


def test_a_missing_main_branch_is_created(jobs, clone):
    result, summary = check_branch(jobs, clone, git(clone, "rev-parse", "HEAD"), branch="stable")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "`stable` does not exist and is created at the commit." in summary


def test_an_empty_main_branch_moves_nothing(jobs, clone):
    # The default: the tag is the release, and no branch follows it.
    assert trigger(load(ENTRY))["inputs"]["main-branch"]["default"] == ""
    commit(clone, {"a.kt": "1"}, "one (#1)")
    result, summary = check_branch(jobs, clone, git(clone, "rev-parse", "HEAD"), branch="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "No branch is moved: the tag is the release." in summary
    assert check_branch.moved == "main-branch=\n"


def test_the_source_branch_is_never_the_branch_that_is_moved(jobs, clone):
    # A repository that releases from main and names main as main-branch: the commit is on the branch
    # already, and a commit behind its tip is no reason to refuse.
    target = commit(clone, {"a.kt": "1"}, "one (#1)")
    commit(clone, {"b.kt": "2"}, "two (#2)")
    git(clone, "push", "-q", "origin", "develop")
    result, summary = check_branch(jobs, clone, target, branch="develop")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "No branch is moved: `develop` is the branch the release is cut from." in summary
    assert check_branch.moved == "main-branch=\n"


@pytest.mark.parametrize("branch", ["main..develop", "ma in", "-x", "main~1"])
def test_main_branch_has_to_be_a_branch_name(jobs, clone, branch):
    result, _ = check_branch(jobs, clone, git(clone, "rev-parse", "HEAD"), branch=branch)
    assert result.returncode == 1
    assert "main-branch is not a branch name" in result.stdout


# --- tag: the tag and the push ---------------------------------------------------------------


def look_for_tag(jobs, clone: Path, tag: str, target: str):
    output = clone.parent / "state-output"
    output.write_text("")
    env = {"TAG": tag, "TARGET_SHA": target, "GITHUB_OUTPUT": str(output)}
    result = run_script(step(jobs["tag"], "Look for the tag")["run"], env, clone)
    return result, output.read_text()


def test_look_for_the_tag(jobs, clone):
    first = git(clone, "rev-parse", "HEAD")
    target = commit(clone, {"a.kt": "1"}, "fix (#1)")
    result, output = look_for_tag(jobs, clone, "v1.0.0", target)
    assert (result.returncode, output) == (0, "exists=false\n")
    git(clone, "tag", "-a", "v1.0.0", "-m", "v1.0.0", target)
    result, output = look_for_tag(jobs, clone, "v1.0.0", target)
    assert (result.returncode, output) == (0, "exists=true\n")
    assert "an earlier attempt created it" in result.stdout
    result, output = look_for_tag(jobs, clone, "v1.0.0", first)
    assert (result.returncode, output) == (1, "")
    assert "Another release took this version" in result.stdout


def push(jobs, clone: Path, target: str, tag="v1.0.1", previous="v1.0.0", branch="main", exists="false", **overrides):
    output = clone.parent / "push-output"
    output.write_text("")
    env = {
        "TAG": tag,
        "PREVIOUS_TAG": previous,
        "TARGET_SHA": target,
        "TAG_EXISTS": exists,
        "TAG_NOW": tag if exists == "false" else "",
        "PREVIOUS_TAG_NOW": previous if exists == "false" else "",
        "MAIN_BRANCH": branch,
        "GITHUB_OUTPUT": str(output),
        **overrides,
    }
    result = run_script(step(jobs["tag"], "Push the tag and the main branch")["run"], env, clone)
    return result, output.read_text()


def test_the_tag_and_the_branch_are_pushed_together(jobs, clone):
    commit(clone, {"a.kt": "1"}, "fix (#1)")
    target = commit(clone, {"b.kt": "2"}, "fix (#2)")
    commit(clone, {"c.kt": "3"}, "not released (#3)")
    git(clone, "push", "-q", "origin", "develop")
    result, output = push(jobs, clone, target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output == "tagged=true\n"
    assert remote(clone, "rev-parse", "refs/heads/main") == target
    # An annotated tag on the commit, made by the workflow's own identity.
    assert remote(clone, "cat-file", "-t", "refs/tags/v1.0.1") == "tag"
    assert remote(clone, "rev-parse", "refs/tags/v1.0.1^{commit}") == target
    assert remote(clone, "for-each-ref", "--format=%(taggername) %(contents:subject)", "refs/tags/v1.0.1") == (
        "github-actions[bot] v1.0.1"
    )


def test_a_branch_that_cannot_move_forward_leaves_no_tag(jobs, clone):
    git(clone, "checkout", "-q", "-b", "main", "origin/main")
    commit(clone, {"version.txt": "1"}, "a commit on main only")
    git(clone, "push", "-q", "origin", "main")
    main = remote(clone, "rev-parse", "refs/heads/main")
    git(clone, "checkout", "-q", "develop")
    target = commit(clone, {"a.kt": "1"}, "fix (#1)")
    git(clone, "push", "-q", "origin", "develop")
    result, output = push(jobs, clone, target)
    assert result.returncode != 0
    assert output == ""
    assert remote(clone, "rev-parse", "refs/heads/main") == main
    assert remote(clone, "tag", "--list") == "", "the push is atomic: no tag without the branch"


def test_without_a_main_branch_only_the_tag_is_pushed(jobs, clone):
    before = remote(clone, "rev-parse", "refs/heads/main")
    target = commit(clone, {"a.kt": "1"}, "fix (#1)")
    git(clone, "push", "-q", "origin", "develop")
    result, _ = push(jobs, clone, target, branch="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert remote(clone, "rev-parse", "refs/tags/v1.0.1^{commit}") == target
    assert remote(clone, "rev-parse", "refs/heads/main") == before


@pytest.mark.parametrize(
    "now", [{"TAG_NOW": "v1.1.1"}, {"PREVIOUS_TAG_NOW": "v1.0.5"}, {"TAG_NOW": "v1.1.1", "PREVIOUS_TAG_NOW": "v1.1.0"}]
)
def test_a_plan_that_no_longer_holds_is_not_carried_out(jobs, clone, now):
    target = commit(clone, {"a.kt": "1"}, "fix (#1)")
    git(clone, "push", "-q", "origin", "develop")
    result, output = push(jobs, clone, target, **now)
    assert result.returncode == 1
    assert "Another release happened meanwhile" in result.stdout
    assert output == ""
    assert remote(clone, "tag", "--list") == ""
    assert git(clone, "tag", "--list") == ""


def test_a_second_attempt_finishes_what_the_first_left(jobs, clone):
    target = commit(clone, {"a.kt": "1"}, "fix (#1)")
    git(clone, "push", "-q", "origin", "develop")
    git(clone, "tag", "-a", "v1.0.1", "-m", "v1.0.1", target)
    git(clone, "push", "-q", "origin", "refs/tags/v1.0.1")
    tag_object = remote(clone, "rev-parse", "refs/tags/v1.0.1")
    result, output = push(jobs, clone, target, exists="true")
    assert result.returncode == 0, result.stdout + result.stderr
    assert output == "tagged=true\n"
    assert remote(clone, "rev-parse", "refs/heads/main") == target
    assert remote(clone, "rev-parse", "refs/tags/v1.0.1") == tag_object, "the tag is not made again"
    # And once more, with nothing left to do.
    result, output = push(jobs, clone, target, exists="true")
    assert (result.returncode, output) == (0, "tagged=true\n")


def test_the_version_is_planned_again_before_the_push(jobs):
    tag = jobs["tag"]
    ids = [item.get("id") for item in tag["steps"]]
    assert ids.index("state") < ids.index("version") < ids.index("push")
    again = tag["steps"][ids.index("version")]
    assert again["if"] == "${{ steps.state.outputs.exists == 'false' }}"
    source = "${{ inputs.source-branch || github.event.repository.default_branch }}"
    assert again["with"] == {
        "sha": "${{ needs.plan.outputs.target-sha }}",
        "bump": "${{ inputs.bump }}",
        "version": "${{ inputs.version }}",
        # A preview is numbered again too: another preview of the version cut meanwhile changes the tag.
        "prerelease": "${{ inputs.prerelease }}",
        "source-branch": source,
    }
    env = tag["steps"][ids.index("push")]["env"]
    assert env["TAG"] == "${{ needs.plan.outputs.tag }}" and env["TAG_NOW"] == "${{ steps.version.outputs.tag }}"
    assert env["PREVIOUS_TAG_NOW"] == "${{ steps.version.outputs.previous-tag }}"
    # The branch the plan decided to move, which is none for a release from a maintenance branch.
    assert env["MAIN_BRANCH"] == "${{ needs.plan.outputs.main-branch }}"
    assert jobs["plan"]["outputs"]["main-branch"] == "${{ steps.main.outputs.main-branch }}"
    planned = jobs["plan"]["steps"][1]
    assert planned["uses"] == again["uses"]
    # The source branch is always passed: without it the action checks tags only, and a commit of an
    # open pull request could be released.
    assert planned["with"] == {
        "pr": "${{ inputs.pr }}",
        "sha": "${{ inputs.sha }}",
        "bump": "${{ inputs.bump }}",
        # An exact version, when the caller gave one; the action then does not read the bump.
        "version": "${{ inputs.version }}",
        "prerelease": "${{ inputs.prerelease }}",
        "source-branch": source,
    }
    main = step(jobs["plan"], "Check that the main branch can move")
    assert main["env"]["SOURCE_BRANCH"] == source
    assert main["env"]["LINE_BRANCH"] == "${{ steps.version.outputs.branch }}"
    assert main["env"]["PRERELEASE"] == "${{ inputs.prerelease }}"


# --- tag: the release workflow --------------------------------------------------------------

GH_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_CALLS"
echo "gh output that must not reach the summary"
exit "${GH_EXIT:-0}"
"""


def start_release(jobs, tmp_path, workflow="release.yml", gh_exit="0"):
    executable(tmp_path / "gh", GH_STUB)
    calls, summary = tmp_path / "calls", tmp_path / "summary"
    calls.write_text("")
    summary.write_text("")
    env = {
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "GH_CALLS": str(calls),
        "GH_EXIT": gh_exit,
        "GH_REPO": "acme/app",
        "TAG": "v1.0.1",
        "RELEASE_WORKFLOW": workflow,
        "GITHUB_SERVER_URL": "https://github.invalid",
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    result = run_script(step(jobs["tag"], "Start the release workflow")["run"], env, tmp_path)
    return result, calls.read_text().splitlines(), summary.read_text()


def test_the_release_workflow_is_started_for_the_tag(jobs, tmp_path):
    result, calls, summary = start_release(jobs, tmp_path)
    assert result.returncode == 0, result.stderr
    assert calls == ["workflow run release.yml --ref v1.0.1"]
    assert "Started [`release.yml`](https://github.invalid/acme/app/actions/workflows/release.yml) for `v1.0.1`." in summary
    assert "gh output" not in summary


def test_an_empty_release_workflow_starts_nothing(jobs, tmp_path):
    result, calls, summary = start_release(jobs, tmp_path, workflow="")
    assert result.returncode == 0, result.stderr
    assert calls == []
    assert "No release workflow is started" in summary


def test_a_release_workflow_that_cannot_be_started_fails_the_job(jobs, tmp_path):
    result, _, summary = start_release(jobs, tmp_path, gh_exit="1")
    assert result.returncode != 0
    assert "Started" not in summary


# --- wiring -------------------------------------------------------------------------------------


def test_only_the_tag_job_writes_and_only_after_the_plan(jobs):
    workflow = load(ENTRY)
    assert workflow["permissions"] == {"contents": "read"}
    plan, tag = jobs["plan"], jobs["tag"]
    assert plan["permissions"] == {"contents": "read", "checks": "read", "pull-requests": "read"}
    assert "environment" not in plan, "the plan never waits: a dry run shows it at once"
    assert plan["steps"][0]["with"] == {"fetch-depth": 0, "persist-credentials": False}
    assert tag["needs"] == "plan"
    assert tag["if"] == "${{ !inputs.dry_run }}"
    # Empty by default: the job then runs in no environment, right after the plan.
    assert tag["environment"] == "${{ inputs.environment }}"
    assert tag["permissions"] == {"contents": "write", "actions": "write", "pull-requests": "read"}
    assert tag["steps"][0]["with"] == {
        "fetch-depth": 0,
        "token": "${{ secrets.RELEASE_PUSH_TOKEN || github.token }}",
        # Stated, not left to the default: the push needs the token in the clone.
        "persist-credentials": True,
    }
    # The inline script does not import from the checkout.
    assert "python3 -I - <<'CHECKS'" in step(plan, "Check the commit's checks")["run"]
    # The release workflow is started with the job token, whatever token pushed.
    assert step(tag, "Start the release workflow")["env"]["GH_TOKEN"] == "${{ github.token }}"
    names = [item.get("name") for item in plan["steps"][1:]]
    assert names == [
        "Plan the version",
        "Write the plan to the summary",
        "Check the release workflow of the commit",
        "Check the commit's checks",
        "Check that the main branch can move",
        "Check the confirmation",
    ]
    for name in names[2:]:
        assert "if" not in step(plan, name), f"{name}: a dry run shows the same refusals"
    guard = step(plan, "Check the release workflow of the commit")
    assert guard["env"] == {
        "RELEASE_WORKFLOW": "${{ inputs.release-workflow }}",
        "TARGET_SHA": "${{ steps.version.outputs.target-sha }}",
    }
    # The file that is checked is the one the tag job starts.
    assert step(tag, "Start the release workflow")["env"]["RELEASE_WORKFLOW"] == "${{ inputs.release-workflow }}"


def test_inputs_secrets_and_outputs(jobs):
    call = trigger(load(ENTRY))
    assert list(call["inputs"]) == [
        "pr", "sha", "bump", "version", "prerelease", "dry_run", "confirm", "source-branch", "required-checks",
        "main-branch", "release-workflow", "environment",
    ]
    # A release unless the run says otherwise.
    assert call["inputs"]["prerelease"] == {**call["inputs"]["prerelease"], "type": "string", "default": "", "required": False}
    # A run is a dry run unless it says otherwise, and a run that says otherwise has to name the
    # planned version: no job waits for an approval, so these two are what stands before the tag.
    assert call["inputs"]["dry_run"] == {**call["inputs"]["dry_run"], "type": "boolean", "default": True}
    assert call["inputs"]["confirm"] == {**call["inputs"]["confirm"], "type": "string", "default": "", "required": False}
    assert call["inputs"]["version"]["default"] == ""
    # No environment by default: required reviewers, the one rule that would make it an approval,
    # do not exist for a private repository on the Team plan. The build job's default is another
    # matter: it names the environment a repository may keep its signing key in.
    assert call["inputs"]["environment"]["default"] == ""
    assert trigger(load("android-release.yml"))["inputs"]["environment"]["default"] == "release"
    assert call["secrets"]["RELEASE_PUSH_TOKEN"]["required"] is False
    assert set(call["secrets"]) == {"RELEASE_PUSH_TOKEN"}
    assert {name: output["value"] for name, output in call["outputs"].items()} == {
        "tag": "${{ jobs.plan.outputs.tag }}",
        "version": "${{ jobs.plan.outputs.version }}",
        "target-sha": "${{ jobs.plan.outputs.target-sha }}",
        "tagged": "${{ jobs.tag.outputs.tagged }}",
    }


def test_actions_are_referenced_at_the_workflows_tag():
    text = (WORKFLOWS / ENTRY).read_text()
    own = {}
    for line in re.findall(r"^\s*(?:- )?uses: (.+)$", text, re.MULTILINE):
        match = OWN_ACTION.match(line)
        if match:
            own.setdefault(match["name"], set()).add(match["ref"])
        else:
            assert line.startswith("actions/"), line
    assert own == {"release-version": {"android-ci/v1"}}


def test_the_example_passes_only_what_the_workflow_declares():
    call = trigger(load(ENTRY))
    example = yaml.safe_load(EXAMPLE.read_text())
    (job,) = example["jobs"].values()
    assert job["uses"] == f"asleep-ai/actions/.github/workflows/{ENTRY}@android-ci/v1"
    assert set(job["with"]) <= set(call["inputs"])
    dispatch = example[True]["workflow_dispatch"]["inputs"]
    assert list(dispatch) == ["pr", "sha", "bump", "version", "prerelease", "dry_run", "confirm"]
    assert dispatch["bump"]["options"] == ["patch", "minor", "major"]
    # A list, so that another kind of prerelease is one more entry. Its first entry and default is
    # the release, which the called workflow takes as an empty value.
    assert dispatch["prerelease"]["type"] == "choice" and dispatch["prerelease"]["default"] == "none"
    assert dispatch["prerelease"]["options"] == ["none", "preview"]
    assert job["with"]["prerelease"] == "${{ inputs.prerelease != 'none' && inputs.prerelease || '' }}"
    import sys

    sys.path.insert(0, str(ROOT / "release-version"))
    import release_version

    assert tuple(dispatch["prerelease"]["options"][1:]) == release_version.PRERELEASES
    # The form opens as a dry run, like the called workflow: a release is a run that unticks it.
    assert dispatch["dry_run"]["type"] == "boolean" and dispatch["dry_run"]["default"] is True
    assert "default" not in dispatch["confirm"]
    for name in dispatch:
        if name != "prerelease":
            assert job["with"][name] == f"${{{{ inputs.{name} }}}}"
    # Nothing else is passed: no branch is moved and the tag job runs in no environment, by default.
    assert set(job["with"]) == set(dispatch)
    # The called workflow cannot be given more than the caller has.
    assert example["permissions"] == {
        "contents": "write", "actions": "write", "checks": "read", "pull-requests": "read",
    }
    assert "secrets" not in job
    assert example["concurrency"]["cancel-in-progress"] is False
    # A dry run writes nothing, so it neither waits behind a release nor replaces one that waits.
    assert example["concurrency"]["group"] == (
        "${{ inputs.dry_run && format('version-bump-dry-{0}', github.run_id) || 'version-bump' }}"
    )
    usage = yaml.safe_load(
        README.read_text().split("## android-version-bump.yml", 1)[1].split("```yaml", 1)[1].split("```", 1)[0]
    )
    assert usage["concurrency"] == example["concurrency"]
    shown = usage[True]["workflow_dispatch"]["inputs"]
    assert list(shown) == list(dispatch)
    assert shown["dry_run"]["default"] is True
    (shown_job,) = usage["jobs"].values()
    assert shown_job["with"] == job["with"]


def test_readme_lists_every_input_with_its_default():
    section = README.read_text().split("## android-version-bump.yml", 1)[1].split("\n## ", 1)[0]
    inputs = trigger(load(ENTRY))["inputs"]
    rows = re.findall(r"^\| `([a-z_-]+)` \| (.*?) \| ", section.split("### Inputs", 1)[1].split("###", 1)[0], re.MULTILINE)
    assert [name for name, _ in rows] == list(inputs), "the table follows the order of the workflow file"
    for name, shown in rows:
        default = inputs[name]["default"]
        if default == "":
            assert shown == "`''`", name
        elif isinstance(default, bool):
            assert shown == f"`{str(default).lower()}`", name
        else:
            # A default of several lines is shown line by line.
            assert shown == "<br>".join(f"`{line}`" for line in default.splitlines()), name
