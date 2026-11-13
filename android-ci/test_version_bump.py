"""Tests of android-version-bump.yml.

The run steps are executed as the shell GitHub would run: the checks step
against a local stand-in for the GitHub API, the branch and tag steps against
throwaway git repositories with a bare repository as the remote.
"""

from __future__ import annotations

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
        "VERSION_CODE": "101350001",
        "TARGET_SHA": "3ff9b1a3" + "0" * 32,
        "PRS": prs,
        "PR": "858",
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
    assert "- versionCode: 101350001\n" in text
    # The titles are written as they are.
    assert prs + "\n" in text
    assert "Dry run" not in text

    summary.write_text("")
    env.update({"PREVIOUS_TAG": "", "TAG": "v0.1.0", "PRS": "", "PR": "", "DRY_RUN": "true"})
    result = run_script(script, env, tmp_path)
    assert result.returncode == 0, result.stderr
    text = summary.read_text()
    assert text.startswith("### no release yet → v0.1.0\n")
    assert "**Dry run.** Nothing is tagged, pushed or started." in text
    assert "merge commit of" not in text
    assert "Nothing listed." in text


# --- plan: the checks of the commit --------------------------------------------------------

SHA = "ab" * 20


def check_run(name="pr-check / result", conclusion="success", status="completed", started="2026-11-13T01:00:00Z", **more):
    run = {"name": name, "status": status, "conclusion": conclusion, "started_at": started, "id": 1}
    run["html_url"] = f"https://github.invalid/runs/{run['id']}"
    return {**run, **more}


@pytest.fixture
def api():
    """A local GitHub API that answers the check-runs request with the runs set on it."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            server.requests.append((self.path, self.headers.get("Authorization")))
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)
            if server.status != 200 or url.path != f"/repos/acme/app/commits/{SHA}/check-runs":
                self.send_response(server.status if server.status != 200 else 404)
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
    server.runs, server.requests, server.status, server.body = [], [], 200, None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join()


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


def test_the_default_required_check_is_the_result_of_the_pull_request_check(jobs):
    inputs = trigger(load(ENTRY))["inputs"]
    assert inputs["required-checks"]["default"] == "pr-check / result"
    pr_check = load("android-pr-check.yml")
    assert "result" in pr_check["jobs"]
    example = yaml.safe_load((ROOT / "android-ci" / "pr-check.yml.example").read_text())
    assert list(example["jobs"]) == ["pr-check"]


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


def check_branch(jobs, clone: Path, target: str, branch: str = "main"):
    summary = clone.parent / "summary"
    summary.write_text("")
    env = {"MAIN_BRANCH": branch, "TARGET_SHA": target, "GITHUB_STEP_SUMMARY": str(summary)}
    result = run_script(step(jobs["plan"], "Check that the main branch can move")["run"], env, clone)
    return result, summary.read_text()


def test_main_moves_forward_to_the_commit(jobs, clone):
    commit(clone, {"a.kt": "1"}, "one (#1)")
    target = commit(clone, {"b.kt": "2"}, "two (#2)")
    result, summary = check_branch(jobs, clone, target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "`main` moves forward by 2 commit(s), from `" in summary
    assert "main moves forward by 2 commit(s)" in result.stdout


def test_main_at_the_commit_already(jobs, clone):
    result, summary = check_branch(jobs, clone, git(clone, "rev-parse", "HEAD"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "`main` is at the commit already." in summary


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
    result, summary = check_branch(jobs, clone, git(clone, "rev-parse", "HEAD"), branch="release")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "`release` does not exist and is created at the commit." in summary


def test_an_empty_main_branch_moves_nothing(jobs, clone):
    result, summary = check_branch(jobs, clone, git(clone, "rev-parse", "HEAD"), branch="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "No branch is moved" in summary


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
    assert again["with"] == {"sha": "${{ needs.plan.outputs.target-sha }}", "bump": "${{ inputs.bump }}"}
    env = tag["steps"][ids.index("push")]["env"]
    assert env["TAG"] == "${{ needs.plan.outputs.tag }}" and env["TAG_NOW"] == "${{ steps.version.outputs.tag }}"
    assert env["PREVIOUS_TAG_NOW"] == "${{ steps.version.outputs.previous-tag }}"
    planned = jobs["plan"]["steps"][1]
    assert planned["uses"] == again["uses"]
    assert planned["with"] == {"pr": "${{ inputs.pr }}", "sha": "${{ inputs.sha }}", "bump": "${{ inputs.bump }}"}


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
    assert "environment" not in plan, "the plan is shown before anyone is asked to approve"
    assert plan["steps"][0]["with"] == {"fetch-depth": 0, "persist-credentials": False}
    assert tag["needs"] == "plan"
    assert tag["if"] == "${{ !inputs.dry_run }}"
    assert tag["environment"] == "${{ inputs.environment }}"
    assert tag["permissions"] == {"contents": "write", "actions": "write", "pull-requests": "read"}
    assert tag["steps"][0]["with"] == {
        "fetch-depth": 0,
        "token": "${{ secrets.RELEASE_PUSH_TOKEN || github.token }}",
    }
    # The release workflow is started with the job token, whatever token pushed.
    assert step(tag, "Start the release workflow")["env"]["GH_TOKEN"] == "${{ github.token }}"
    names = [item.get("name") for item in plan["steps"][1:]]
    assert names == [
        "Plan the version",
        "Write the plan to the summary",
        "Check the commit's checks",
        "Check that the main branch can move",
    ]


def test_inputs_secrets_and_outputs(jobs):
    call = trigger(load(ENTRY))
    assert list(call["inputs"]) == [
        "pr", "sha", "bump", "dry_run", "required-checks", "main-branch", "release-workflow", "environment",
    ]
    assert call["inputs"]["dry_run"] == {**call["inputs"]["dry_run"], "type": "boolean", "default": False}
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
    assert list(dispatch) == ["pr", "sha", "bump", "dry_run"]
    assert dispatch["bump"]["options"] == ["patch", "minor", "major"]
    for name in dispatch:
        assert job["with"][name] == f"${{{{ inputs.{name} }}}}"
    # The called workflow cannot be given more than the caller has.
    assert example["permissions"] == {
        "contents": "write", "actions": "write", "checks": "read", "pull-requests": "read",
    }
    assert "secrets" not in job
    assert example["concurrency"]["cancel-in-progress"] is False


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
            assert shown == f"`{default}`", name
