"""Tests of android-qa.yml.

As in test_release.py, the run steps are executed as the shell GitHub would
run, here against stand-ins for gh and the check-runs API, and the wiring tests
pin what cannot be executed: job conditions, the calls of the build workflow
it shares with android-release.yml, and the copy of the checks script.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from test_pr_check import OWN_ACTION, ROOT, WORKFLOWS, commit, executable, git, load, run_script, step, trigger
from test_version_bump import SHA, check_run, check_runs_api

sys.path.insert(0, str(ROOT / "release-version"))

import release_version  # noqa: E402

ENTRY = "android-qa.yml"
BUILD = "android-release-build.yml"
RELEASE = "android-release.yml"
EXAMPLE = ROOT / "android-ci" / "qa.yml.example"
README = ROOT / "android-ci" / "README.md"

# How the jobs put the version name together from the plan's outputs and the label input.
VERSION_NAME = "${{ format('{0}-{1}.{2}', needs.plan.outputs.version, inputs.label || 'qa', needs.plan.outputs.number) }}"
KIND = "${{ inputs.label != '' && 'label' || 'qa' }}"


@pytest.fixture(scope="module")
def entry() -> dict:
    return load(ENTRY)


@pytest.fixture(scope="module")
def publish(entry) -> dict:
    return entry["jobs"]["publish"]


@pytest.fixture(scope="module")
def checks(entry) -> dict:
    return entry["jobs"]["checks"]


# --- plan --------------------------------------------------------------------------------------


def test_the_plan_is_the_qa_operation_of_release_version_for_the_ref_of_the_run(entry):
    plan = entry["jobs"]["plan"]
    names = [item.get("name") or item.get("uses") for item in plan["steps"]]
    assert names == [
        "Check the wait for the checks", "Check qa-gradle-home", "actions/checkout@v7", "Plan the QA build",
        "Write the plan to the summary", "Cancel the runs of earlier commits", "Pick the runner",
    ]
    # Every tag and the whole history of the commit the run started for, and no token left behind.
    assert plan["steps"][2]["with"] == {"ref": "${{ github.sha }}", "fetch-depth": 0, "persist-credentials": False}
    qa = step(plan, "Plan the QA build")
    assert qa["uses"] == "asleep-ai/actions/release-version@android-ci/v1"
    assert qa["with"] == {
        "operation": "qa",
        # The ref and the commit of the run, never an input: a manual run builds the branch it was
        # started on, and the action refuses every ref but the source branch and release/<major>.x.
        "ref": "${{ github.ref }}",
        "sha": "${{ github.sha }}",
        "source-branch": "${{ inputs.source-branch || github.event.repository.default_branch }}",
        "version": "${{ inputs.version }}",
        "next-version": "${{ inputs.next-version }}",
        "label": "${{ inputs.label }}",
        # A later run is a higher versionCode; a run that is started again keeps its number.
        "qa-sequence": "${{ github.run_number }}",
        "qa-sequence-offset": "${{ inputs.sequence-offset }}",
    }
    assert "if" not in qa
    # It reads the repository and cancels runs, and no signing secret is near: the build needs the plan.
    assert plan["permissions"] == {"contents": "read", "actions": "write"}
    assert "environment" not in plan
    assert entry["jobs"]["primary"]["needs"] == "plan"
    pick = step(plan, "Pick the runner")
    assert pick["if"] == "${{ inputs.runner-labels != '' }}"
    assert pick["with"]["token"] == "${{ secrets.RUNNER_READ_TOKEN }}"


def test_no_output_of_the_plan_holds_a_name_a_person_chose(entry):
    # The runner drops a job output that contains the value of a secret, and says so only in the
    # log of the job that set it. A branch (release/1.x), a line, a tag (qa-release-1.x) or a label
    # may hold such a value: "release" is a likely key alias. What crosses to the other jobs is a
    # commit id, numbers and a release tag (v and numbers); the names are put together there.
    outputs = entry["jobs"]["plan"]["outputs"]
    assert outputs == {
        "sha": "${{ steps.qa.outputs.target-sha }}",
        "major": "${{ steps.qa.outputs.major }}",
        "version": "${{ steps.qa.outputs.version }}",
        "number": "${{ steps.qa.outputs.number }}",
        "version-code": "${{ steps.qa.outputs.version-code }}",
        "previous-tag": "${{ steps.qa.outputs.previous-tag }}",
        "runs-on": "${{ steps.pick.outputs.runs-on }}",
        "self-hosted": "${{ steps.pick.outputs.self-hosted }}",
    }
    text = (WORKFLOWS / ENTRY).read_text()
    read = set(re.findall(r"(?:needs|jobs)\.plan\.outputs\.([a-z-]+)", text))
    assert read == set(outputs)
    # The same in android-release.yml, whose plan knows whether the tag is a release or a preview.
    release = load(RELEASE)["jobs"]
    assert "kind" not in release["plan"]["outputs"]
    assert release["plan"]["outputs"]["preview"] == "${{ steps.tag.outputs.kind == 'preview' }}"
    assert "plan.outputs.kind" not in (WORKFLOWS / RELEASE).read_text()


def test_every_output_the_plan_reads_is_one_the_action_has(entry):
    action = yaml.safe_load((ROOT / "release-version" / "action.yml").read_text())
    text = (WORKFLOWS / ENTRY).read_text()
    read = set(re.findall(r"steps\.(?:qa|changes)\.outputs\.([a-z-]+)", text))
    assert read and read <= set(action["outputs"])
    for name in ("Plan the QA build",):
        assert set(step(entry["jobs"]["plan"], name)["with"]) <= set(action["inputs"])
    changes = step(entry["jobs"]["publish"], "List the changes since the build before")
    assert set(changes["with"]) <= set(action["inputs"])


@pytest.mark.parametrize(
    ("minutes", "passes"),
    [("0", True), ("60", True), ("100", True), ("101", False), ("1000", False), ("60.5", False), ("", False), ("soon", False), ("-1", False)],
)
def test_a_wait_the_job_has_no_room_for_is_refused_by_the_plan(entry, tmp_path, minutes, passes):
    # The job that waits ends after 120 minutes. A longer wait would end in a cancelled job, which
    # deletes no artifact and names no reason.
    check = step(entry["jobs"]["plan"], "Check the wait for the checks")
    assert check["env"] == {"CHECKS_WAIT_MINUTES": "${{ inputs.checks-timeout-minutes }}"}
    result = run_script(check["run"], {"CHECKS_WAIT_MINUTES": minutes}, tmp_path)
    assert (result.returncode == 0) == passes, result.stdout + result.stderr
    if not passes:
        assert f"::error title=Invalid input::checks-timeout-minutes is {minutes}. It is a whole number of minutes from 0 to 100" in result.stdout
    assert entry["jobs"]["plan"]["steps"][0] is check
    assert entry["jobs"]["checks"]["timeout-minutes"] == 120


@pytest.mark.parametrize(
    ("value", "passes"),
    [("warm", True), ("cold", True), ("reset", True), ("", True), ("Warm", False), ("hot", False), ("true", False), ("warm cold", False)],
)
def test_a_qa_gradle_home_that_is_no_known_word_is_refused_by_the_plan(entry, tmp_path, value, passes):
    # Empty is the default, cold: a calling file passes the input of its manual run, which a push
    # leaves empty. Another word would give cold builds with a warning in the build job, in a
    # repository that meant to ask for the home.
    check = step(entry["jobs"]["plan"], "Check qa-gradle-home")
    assert check["env"] == {"QA_GRADLE_HOME": "${{ inputs.qa-gradle-home }}"}
    result = run_script(check["run"], {"QA_GRADLE_HOME": value}, tmp_path)
    assert (result.returncode == 0) == passes, result.stdout + result.stderr
    if not passes:
        assert f"::error title=Invalid input::qa-gradle-home is '{value}'. It is one of cold, warm and reset." in result.stdout
    # Before the checkout and before any job that builds.
    assert entry["jobs"]["plan"]["steps"][1] is check


def plan_summary(entry, tmp_path, **overrides) -> str:
    summary = tmp_path / "summary"
    summary.write_text("")
    env = {
        "BRANCH": "develop",
        "SHA": "3ff9b1a3" + "0" * 32,
        "KIND": "qa",
        "VERSION": "2.0.0",
        "VERSION_NAME": "2.0.0-qa.1057",
        "VERSION_CODE": "200001057",
        "PREVIOUS_TAG": "",
        "RELEASE_TAG": "qa-develop",
        "VERSION_GIVEN": "",
        "GITHUB_SERVER_URL": "https://github.invalid",
        "GITHUB_REPOSITORY": "acme/app",
        "GITHUB_STEP_SUMMARY": str(summary),
        **overrides,
    }
    result = run_script(step(entry["jobs"]["plan"], "Write the plan to the summary")["run"], env, tmp_path)
    assert result.returncode == 0, result.stderr
    return summary.read_text()


def test_the_plan_is_written_to_the_summary(entry, tmp_path):
    text = plan_summary(entry, tmp_path)
    assert text.startswith("### QA build 2.0.0-qa.1057\n")
    assert "- Branch: `develop`, commit [`3ff9b1a`](https://github.invalid/acme/app/commit/3ff9b1a3" in text
    assert "- Leads up to `2.0.0`, no release yet\n" in text
    assert "- It installs over the earlier QA builds of 2.0. Every preview and every release of 2.0 installs over it, and it over none of them.\n" in text
    assert "- versionCode: 200001057\n" in text
    assert "- Published as the pre-release `qa-develop` when the build and the commit's checks pass\n" in text
    assert "was given" not in text
    text = plan_summary(
        entry, tmp_path, BRANCH="release/1.x", VERSION="1.14.0", VERSION_NAME="1.14.0-qa.58",
        PREVIOUS_TAG="v1.13.1", RELEASE_TAG="qa-release-1.x-1.14.0", VERSION_GIVEN="1.14.0",
    )
    assert "- Leads up to `1.14.0`, after `v1.13.1`\n" in text
    # A version for one run: its build is kept apart from the rolling build of the branch.
    assert "- The version was given (`version`) for this run. The build is published apart from the rolling build of `release/1.x`" in text
    assert "- Published as the pre-release `qa-release-1.x-1.14.0` " in text


def test_the_plan_of_a_label_build_says_what_it_installs_over(entry, tmp_path):
    text = plan_summary(
        entry, tmp_path, KIND="label", VERSION="1.13.1", VERSION_NAME="1.13.1-ble-test.2", VERSION_CODE="101350002",
        PREVIOUS_TAG="v1.13.0", RELEASE_TAG="qa-develop-ble-test",
    )
    assert text.startswith("### Label build 1.13.1-ble-test.2\n")
    assert "- A named test build made after `v1.13.0`: it installs over that release, and `1.13.1` installs over it.\n" in text
    assert "Leads up to" not in text
    assert "- versionCode: 101350002\n" in text


# --- plan: the runs of earlier commits are cancelled ------------------------------------------------

OWN_RUN = "9300"
PUSH_QUERY = {"branch": ["develop"], "event": ["push"], "per_page": ["100"]}


def qa_run(number: int, sha: str, status: str = "in_progress", event: str = "push", branch: str = "develop") -> dict:
    """A run of the calling workflow as the list of its runs has it."""
    return {
        "id": 9000 + number, "run_number": number, "head_sha": sha, "status": status, "event": event,
        "head_branch": branch, "html_url": f"https://github.invalid/acme/app/actions/runs/{9000 + number}",
    }


def job_of(name: str, status: str = "in_progress") -> dict:
    return {"name": name, "status": status, "conclusion": "success" if status == "completed" else None}


# The jobs of a run under a calling job `qa`, at three moments: while it builds, while it waits for
# its checks, and while it publishes.
BUILDING = [job_of("qa / plan", "completed"), job_of("qa / primary / build")]
WAITING = [job_of("qa / plan", "completed"), job_of("qa / primary / build", "completed"), job_of("qa / built", "completed"), job_of("qa / checks")]
PUBLISHING = [*WAITING[:3], job_of("qa / checks", "completed"), job_of("qa / publish")]


@contextlib.contextmanager
def runs_api():
    """A local GitHub API with the runs of qa.yml, their jobs and their artifacts.

    It answers the list of runs with all of `runs`, whatever branch and event the request names,
    so a test can hand the step runs it did not ask for. `failing` maps (method, start of the
    route) to the status such a request is answered with.
    """

    class Handler(BaseHTTPRequestHandler):
        def answer(self, status, document=None):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if document is not None:
                self.wfile.write(json.dumps(document).encode())

        def serve(self):
            url = urllib.parse.urlsplit(self.path)
            route = url.path.removeprefix("/repos/acme/app/")
            server.requests.append((self.command, route, self.headers.get("Authorization")))
            for (method, start), status in server.failing.items():
                if self.command == method and route.startswith(start):
                    return self.answer(status, {"message": "Resource not accessible by integration"})
            run = re.fullmatch(r"actions/runs/(\d+)/(jobs|cancel|artifacts)", route)
            if self.command == "GET" and route == "actions/workflows/qa.yml/runs":
                server.queries.append(urllib.parse.parse_qs(url.query))
                return self.answer(200, {"total_count": len(server.runs), "workflow_runs": server.runs})
            if self.command == "GET" and run and run[2] == "jobs":
                jobs = server.jobs.get(int(run[1]), [])
                return self.answer(200, {"total_count": len(jobs), "jobs": jobs})
            if self.command == "POST" and run and run[2] == "cancel":
                server.cancelled.append(int(run[1]))
                return self.answer(202, {})
            if self.command == "GET" and run and run[2] == "artifacts":
                artifacts = server.artifacts.get(int(run[1]), [])
                return self.answer(200, {"total_count": len(artifacts), "artifacts": artifacts})
            if self.command == "DELETE" and re.fullmatch(r"actions/artifacts/\d+", route):
                server.deleted.append(int(route.rsplit("/", 1)[1]))
                # As GitHub answers: 204, and no body.
                return self.answer(204)
            return self.answer(404, {"message": "Not Found"})

        do_GET = do_POST = do_DELETE = serve  # noqa: N815

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.runs, server.jobs, server.artifacts, server.failing = [], {}, {}, {}
    server.requests, server.queries, server.cancelled, server.deleted = [], [], [], []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()
        # Closed, so that a request made afterwards is refused at once instead of timing out.
        server.server_close()


@pytest.fixture
def pushes(tmp_path) -> tuple[Path, dict]:
    """develop as four pushes left it, a, b, c and d, and a commit x that is on another history."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "develop")
    shas = {name: commit(repo, {"file": f"{name}\n"}, f"Change {name}") for name in ("a", "b", "c", "d")}
    git(repo, "checkout", "-q", "-b", "side", shas["a"])
    shas["x"] = commit(repo, {"side": "x\n"}, "On another history")
    git(repo, "checkout", "-q", "develop")
    return repo, shas


def cancel_earlier(entry, repo: Path, api, sha: str, **overrides):
    """Run the step of the plan that cancels earlier runs, in the checkout of the plan job."""
    summary = repo.parent / "cancel-summary"
    summary.write_text("")
    env = {
        "TOKEN": "job-token",
        "TARGET_SHA": sha,
        "WORKFLOW_REF": "acme/app/.github/workflows/qa.yml@refs/heads/develop",
        "EVENT_NAME": "push",
        "BRANCH": "develop",
        "PLAN_JOB": "plan",
        "GITHUB_API_URL": f"http://127.0.0.1:{api.server_port}",
        "GITHUB_REPOSITORY": "acme/app",
        "GITHUB_RUN_ID": OWN_RUN,
        "GITHUB_STEP_SUMMARY": str(summary),
        **overrides,
    }
    result = run_script(step(entry["jobs"]["plan"], "Cancel the runs of earlier commits")["run"], env, repo)
    # Whatever happens, the plan goes on: a build that is not cancelled is only a build too many.
    assert result.returncode == 0, result.stdout + result.stderr
    return result, summary.read_text()


def test_the_run_of_a_push_cancels_the_runs_of_earlier_pushes_that_are_still_building(entry, pushes):
    # Three merges in a row. The run of c is planned while the run of a builds and the run of b
    # has not got further than its plan: both are cancelled, and c alone is built to the end.
    repo, shas = pushes
    with runs_api() as api:
        api.runs = [qa_run(53, shas["c"]), qa_run(52, shas["b"], status="queued"), qa_run(51, shas["a"])]
        api.runs[0]["id"] = int(OWN_RUN)
        api.jobs = {9051: BUILDING, 9052: [job_of("qa / plan")]}
        result, summary = cancel_earlier(entry, repo, api, shas["c"])
        assert api.cancelled == [9052, 9051]
        assert api.queries == [PUSH_QUERY]
        assert all(authorization == "Bearer job-token" for _, _, authorization in api.requests)
        # Its own run is not asked about, and nothing is asked twice.
        assert [route for _, route, _ in api.requests] == [
            "actions/workflows/qa.yml/runs",
            "actions/runs/9052/jobs", "actions/runs/9052/cancel", "actions/runs/9052/artifacts",
            "actions/runs/9051/jobs", "actions/runs/9051/cancel", "actions/runs/9051/artifacts",
        ]
    notices = [line for line in result.stdout.splitlines() if line.startswith("::")]
    assert notices == [
        f"::notice title=Earlier build cancelled::Run {number} was building {shas[name][:7]}, an earlier commit of develop "
        f"than {shas['c'][:7]}, and had not started to publish. It is cancelled: only the newest commit is built to the "
        "end. To release that commit, start its run again."
        for number, name in ((52, "b"), (51, "a"))
    ]
    assert summary == (
        "#### Earlier builds cancelled\n\n"
        f"- [run 52](https://github.invalid/acme/app/actions/runs/9052) of `{shas['b'][:7]}`: this run builds a later commit of `develop`.\n"
        f"- [run 51](https://github.invalid/acme/app/actions/runs/9051) of `{shas['a'][:7]}`: this run builds a later commit of `develop`.\n\n"
    )


def test_a_run_that_waits_for_its_checks_is_cancelled_and_its_artifact_deleted(entry, pushes):
    # The run of b was built and waits for a pull request check that the next push replaced. Its
    # `built` check succeeded and stays: the commit was built. Its APK is of no use any more, and
    # no job of a cancelled run deletes it.
    repo, shas = pushes
    with runs_api() as api:
        api.runs = [qa_run(52, shas["b"])]
        api.jobs = {9052: WAITING}
        api.artifacts = {9052: [{"id": 71, "name": "android-qa-apk-primary"}, {"id": 72, "name": "android-qa-apk-fallback"}, {"id": 73, "name": "reports"}]}
        result, _ = cancel_earlier(entry, repo, api, shas["c"])
        assert api.cancelled == [9052] and api.deleted == [71, 72]
    assert "Deleted artifact 71 of run 52." in result.stdout and "::warning" not in result.stdout
    # A publish job that waits for its turn has not started either.
    with runs_api() as api:
        api.runs = [qa_run(52, shas["b"])]
        api.jobs = {9052: [*WAITING[:3], job_of("qa / checks", "completed"), job_of("qa / publish", "queued")]}
        cancel_earlier(entry, repo, api, shas["c"])
        assert api.cancelled == [9052]


@pytest.mark.parametrize("publish_job", ["qa / publish", "publish", "android / qa / publish"])
@pytest.mark.parametrize("status", ["in_progress", "completed"])
def test_a_run_that_publishes_is_never_cancelled(entry, publish, pushes, publish_job, status):
    # The rolling tag and the APKs of the pre-release are changed by the publish job alone, in
    # several calls. A run that is stopped between them would leave the tag at its commit and the
    # APKs half replaced. So a run whose publish job has started is left to finish, whatever the
    # calling job is named: the newer build is published after it, in its turn.
    repo, shas = pushes
    with runs_api() as api:
        api.runs = [qa_run(52, shas["b"]), qa_run(51, shas["a"])]
        api.jobs = {9052: BUILDING, 9051: [*PUBLISHING[:4], job_of(publish_job, status)]}
        result, summary = cancel_earlier(entry, repo, api, shas["c"])
        assert api.cancelled == [9052]
        assert ("POST", "actions/runs/9051/cancel", "Bearer job-token") not in api.requests
        assert [route for method, route, _ in api.requests if method == "DELETE"] == []
    assert f"Run 51 ({shas['a'][:7]}) is publishing: left to finish." in result.stdout
    assert "run 51" not in summary
    # And GitHub cancels nothing by itself: no group of this workflow cancels what is in progress.
    assert publish["concurrency"]["cancel-in-progress"] is False
    assert "cancel-in-progress: true" not in (WORKFLOWS / ENTRY).read_text()


@pytest.mark.parametrize(
    "other",
    [
        # The same commit: another run of it, or this one started again.
        lambda shas: qa_run(52, shas["c"]),
        # A later commit. This run is one of an earlier commit that was started again, to get that
        # commit its `built` check, and it must not stop the build of the newest one.
        lambda shas: qa_run(54, shas["d"]),
        # Another history: the branch was rewritten.
        lambda shas: qa_run(52, shas["x"]),
        # A commit this checkout does not have, and no commit at all.
        lambda shas: qa_run(52, "f" * 40),
        lambda shas: qa_run(52, ""),
        lambda shas: {**qa_run(52, shas["b"]), "head_sha": None},
        # A run that is over.
        lambda shas: qa_run(52, shas["b"], status="completed"),
        # Not what was asked for: another branch, and a run that was not started by a push.
        lambda shas: qa_run(52, shas["b"], branch="release/1.x"),
        lambda shas: qa_run(52, shas["b"], event="workflow_dispatch"),
        lambda shas: qa_run(52, shas["b"], event="schedule"),
    ],
)
def test_no_other_run_is_cancelled(entry, pushes, other):
    repo, shas = pushes
    with runs_api() as api:
        api.runs = [other(shas)]
        api.jobs = {9052: BUILDING, 9054: BUILDING}
        result, summary = cancel_earlier(entry, repo, api, shas["c"])
        assert api.cancelled == [] and api.deleted == []
        assert [method for method, _, _ in api.requests] == ["GET"], "its jobs are not even asked for"
    assert "No run of an earlier commit of develop is building." in result.stdout
    assert "::" not in result.stdout and summary == ""


# The name of the plan job, which is how a manual run says which line of builds it is.
PLAN_NAME = "${{ github.event_name == 'push' && 'plan' || format('plan ({0})', inputs.label || inputs.version || 'manual run') }}"


def plan_name(event: str, label: str = "", version: str = "") -> str:
    """PLAN_NAME as GitHub evaluates it."""
    return "plan" if event == "push" else f"plan ({label or version or 'manual run'})"


@pytest.mark.parametrize(("label", "version"), [("ble-test", ""), ("", "1.14.0"), ("", "")])
def test_a_manual_run_cancels_only_the_earlier_manual_runs_of_its_own_label_or_version(entry, pushes, label, version):
    # A label build is run again for a fix before the first run has finished. The first run is
    # cancelled: its APK would be replaced, and published under the number both were planned with.
    # The runs of other labels, of a version, of neither, and the runs of pushes go on. Which is
    # which is in the name of each run's plan job, because the inputs of a run are not in the API.
    repo, shas = pushes
    mine = plan_name("workflow_dispatch", label, version)
    others = [name for name in ("plan (ble-test)", "plan (knox)", "plan (1.14.0)", "plan (manual run)", "plan") if name != mine]
    with runs_api() as api:
        api.runs = [qa_run(61, shas["a"], event="workflow_dispatch")]
        api.jobs = {9061: [job_of(f"qa / {mine}", "completed"), job_of("qa / primary / build")]}
        for number, name in enumerate(others, start=62):
            api.runs.append(qa_run(number, shas["a"], event="workflow_dispatch"))
            api.jobs[9000 + number] = [job_of(f"qa / {name}", "completed"), job_of("qa / primary / build")]
        # A run whose jobs are not listed yet is not known to be of this line.
        api.runs.append(qa_run(70, shas["a"], event="workflow_dispatch", status="queued"))
        # The run of the push of a, which builds the rolling pre-release of the branch.
        api.runs.append(qa_run(71, shas["a"]))
        api.jobs[9071] = BUILDING
        result, _ = cancel_earlier(entry, repo, api, shas["c"], EVENT_NAME="workflow_dispatch", PLAN_JOB=mine)
        assert api.cancelled == [9061]
        assert api.queries == [{"branch": ["develop"], "event": ["workflow_dispatch"], "per_page": ["100"]}]
        assert "actions/runs/9071/jobs" not in [route for _, route, _ in api.requests]
    assert f"has no job '{mine}': another manual build, left alone." in result.stdout
    # And the other way round: the run of a push cancels no manual run, whatever it builds.
    with runs_api() as api:
        api.runs = [qa_run(61, shas["a"], event="workflow_dispatch")]
        api.jobs = {9061: [job_of("qa / plan"), job_of("qa / plan (ble-test)")]}
        cancel_earlier(entry, repo, api, shas["c"])
        assert api.cancelled == []
    # The job is named by the expression the step is handed, so the two cannot differ.
    plan = entry["jobs"]["plan"]
    assert plan["name"] == PLAN_NAME == step(plan, "Cancel the runs of earlier commits")["env"]["PLAN_JOB"]
    # A label holds lowercase letters, digits and hyphens, and a version holds dots: neither can be
    # the other, and neither can be the words of a run that names none.
    assert release_version.LABEL.fullmatch("ble-test") and not release_version.LABEL.fullmatch("1.14.0")
    assert not release_version.LABEL.fullmatch("manual run")


@pytest.mark.parametrize(
    ("failing", "cancelled", "warning"),
    [
        # The runs cannot be listed: a token without the permission, or a server error.
        ({("GET", "actions/workflows/"): 403}, [], "::warning title=Earlier builds not cancelled::Cannot list the runs of qa.yml on develop (HTTP 403: "),
        ({("GET", "actions/workflows/"): 502}, [], "::warning title=Earlier builds not cancelled::Cannot list the runs of qa.yml on develop (HTTP 502: "),
        # Whether one run publishes cannot be told: that run is left alone, and the other is cancelled.
        ({("GET", "actions/runs/9052/jobs"): 502}, [9051], "::warning title=Earlier build not cancelled::Run 52 "),
        # GitHub refuses to cancel one run: it had just ended.
        ({("POST", "actions/runs/9052/cancel"): 409}, [9051], "::warning title=Earlier build not cancelled::Run 52 "),
        # The artifact of a cancelled run cannot be listed or deleted: it goes after a day.
        ({("GET", "actions/runs/9052/artifacts"): 502}, [9052, 9051], "::warning::Could not delete the build artifact of run 52 (HTTP 502: "),
        ({("DELETE", "actions/artifacts/"): 403}, [9052, 9051], "::warning::Could not delete the build artifact of run 52 (HTTP 403: "),
    ],
)
def test_what_cannot_be_asked_or_cancelled_is_a_warning_and_the_plan_goes_on(entry, pushes, failing, cancelled, warning):
    repo, shas = pushes
    with runs_api() as api:
        api.runs = [qa_run(52, shas["b"]), qa_run(51, shas["a"])]
        api.jobs = {9052: WAITING, 9051: BUILDING}
        api.artifacts = {9052: [{"id": 71, "name": "android-qa-apk-primary"}]}
        api.failing = failing
        result, _ = cancel_earlier(entry, repo, api, shas["c"])
        assert api.cancelled == cancelled
    assert warning in result.stdout
    # An earlier run that goes on is built and may be published; the newer build replaces it, and
    # is not replaced by it.
    assert "::error" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "overrides",
    [
        {"WORKFLOW_REF": ""}, {"WORKFLOW_REF": "acme/app/.github/workflows/qa.yml"}, {"WORKFLOW_REF": "qa.yml@refs/heads/develop"},
        {"BRANCH": ""}, {"EVENT_NAME": ""}, {"TARGET_SHA": ""}, {"TARGET_SHA": "develop"},
    ],
)
def test_a_run_that_does_not_say_what_it_is_cancels_nothing(entry, pushes, overrides):
    repo, shas = pushes
    with runs_api() as api:
        api.runs = [qa_run(52, shas["b"])]
        api.jobs = {9052: BUILDING}
        result, _ = cancel_earlier(entry, repo, api, shas["c"], **overrides)
        assert api.requests == []
    assert "::warning title=Earlier builds not cancelled::The run does not say which workflow, branch and commit it is for" in result.stdout


def test_an_unreachable_api_cancels_nothing_and_fails_nothing(entry, pushes):
    repo, shas = pushes
    with runs_api() as api:
        port = api.server_port
    result = run_script(
        step(entry["jobs"]["plan"], "Cancel the runs of earlier commits")["run"],
        {
            "TOKEN": "job-token", "TARGET_SHA": shas["c"], "WORKFLOW_REF": "acme/app/.github/workflows/qa.yml@refs/heads/develop",
            "EVENT_NAME": "push", "BRANCH": "develop", "PLAN_JOB": "plan", "GITHUB_API_URL": f"http://127.0.0.1:{port}",
            "GITHUB_REPOSITORY": "acme/app", "GITHUB_RUN_ID": OWN_RUN, "GITHUB_STEP_SUMMARY": str(repo.parent / "summary"),
        },
        repo,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::warning title=Earlier builds not cancelled::Cannot list the runs of qa.yml on develop (" in result.stdout


def test_the_runs_are_cancelled_by_the_plan_once_it_has_planned_its_own(entry):
    plan = entry["jobs"]["plan"]
    names = [item.get("name") or item.get("uses") for item in plan["steps"]]
    cancel = step(plan, "Cancel the runs of earlier commits")
    # After the plan of this run: a run the plan refuses (another ref, a version that cannot be
    # built) replaces nothing. And in the plan job, which is the first of the run: the build of
    # the earlier commit gives up its runner before this build asks for one.
    assert names.index("Plan the QA build") < names.index("Cancel the runs of earlier commits") < names.index("Pick the runner")
    assert "if" not in cancel and "continue-on-error" not in cancel
    assert cancel["env"] == {
        "TOKEN": "${{ github.token }}",
        "TARGET_SHA": "${{ steps.qa.outputs.target-sha }}",
        # The calling workflow's file and the branch of the run, as the run has them: no input.
        "WORKFLOW_REF": "${{ github.workflow_ref }}",
        "EVENT_NAME": "${{ github.event_name }}",
        "BRANCH": "${{ github.ref_name }}",
        "PLAN_JOB": PLAN_NAME,
    }
    assert "python3 -I - <<'CANCEL'" in cancel["run"]
    # Earlier means in the history of this commit, which the full checkout of the plan job can tell.
    assert '["git", "merge-base", "--is-ancestor", commit, sha]' in cancel["run"]
    assert plan["steps"][2]["with"]["fetch-depth"] == 0
    # The one job that may cancel runs; the two that delete artifacts had the permission before.
    assert plan["permissions"]["actions"] == "write"
    assert "/cancel" not in json.dumps({name: job for name, job in entry["jobs"].items() if name != "plan"})


# --- the build -----------------------------------------------------------------------------------


def test_the_build_is_the_build_of_a_release_with_ci_qa(entry):
    inputs = trigger(entry)["inputs"]
    inner = trigger(load(BUILD))
    primary, fallback = entry["jobs"]["primary"], entry["jobs"]["fallback"]
    # One build workflow for both: the emptied work folder, no build cache and no daemon, the version
    # read back from the APK and the signature check are the release's. The Gradle user home is not.
    release = load(RELEASE)["jobs"]
    assert primary["uses"] == fallback["uses"] == release["primary"]["uses"] == f"./.github/workflows/{BUILD}"
    assert primary["secrets"] == fallback["secrets"] == "inherit"
    assert set(primary["with"]) == set(fallback["with"]) == set(inner["inputs"])
    assert primary["with"]["attempt"] == "primary" and fallback["with"]["attempt"] == "fallback"
    # A QA build, or a label build when the run names a label: two words, never the label itself.
    assert primary["with"]["kind"] == fallback["with"]["kind"] == KIND
    assert primary["with"]["version-name"] == fallback["with"]["version-name"] == VERSION_NAME
    # The commit that was planned, by its id: a branch would be another commit after the next push.
    planned = {"ref": "sha", "sha": "sha", "version-code": "version-code"}
    for name, output in planned.items():
        assert primary["with"][name] == fallback["with"][name] == f"${{{{ needs.plan.outputs.{output} }}}}"
    forwarded = set(inner["inputs"]) - {"runs-on", "attempt", "kind", "version-name", "gradle-home", *planned}
    # source-branch stays with the plan. The build job is not told which branch the calling file
    # releases from: whether a run may use the QA Gradle home is decided from the run alone.
    assert forwarded == set(inputs) - {
        "runner-labels", "fallback-runs-on", "source-branch", "version", "next-version", "label", "sequence-offset",
        "required-checks", "checks-timeout-minutes", "qa-gradle-home",
    }
    for name in forwarded:
        assert primary["with"][name] == fallback["with"][name] == f"${{{{ inputs.{name} }}}}"
        assert inputs[name]["type"] == inner["inputs"][name]["type"], name
        if inputs[name]["default"] == "":
            # An empty value cannot be passed to an input that is required.
            assert inner["inputs"][name]["required"] is False, name
    # The runner choice and the re-run rule are the release's, word for word.
    assert primary["with"]["runs-on"] == release["primary"]["with"]["runs-on"]
    assert fallback["with"]["runs-on"] == release["fallback"]["with"]["runs-on"]
    assert fallback["if"] == release["fallback"]["if"]
    assert fallback["needs"] == ["plan", "primary"]
    assert "if" not in primary


def test_the_qa_gradle_home_is_used_only_when_the_calling_file_asks_for_it(entry):
    inputs = trigger(entry)["inputs"]
    primary, fallback = entry["jobs"]["primary"], entry["jobs"]["fallback"]
    # Cold unless the calling file says otherwise: a repository that changes nothing builds as
    # before, whatever version of the workflow its tag names. An empty value is cold as well.
    assert inputs["qa-gradle-home"] == {**inputs["qa-gradle-home"], "type": "string", "default": "cold", "required": False}
    assert primary["with"]["gradle-home"] == "${{ inputs.qa-gradle-home || 'cold' }}"
    # The build that follows a failed one is cold, on whatever runner fallback-runs-on names.
    assert fallback["with"]["gradle-home"] == "cold"
    # Whether the first build really gets the home is decided in the build job, from the run: the
    # workflow hands it no branch. The gate is executed in test_release.py; here, that a release
    # never asks.
    for name in ("primary", "fallback"):
        assert "source-branch" not in entry["jobs"][name]["with"]
    release = load(RELEASE)["jobs"]
    for name in ("primary", "fallback"):
        assert "gradle-home" not in release[name]["with"]
    assert trigger(load(BUILD))["inputs"]["gradle-home"]["default"] == "cold"
    # A failure of the code is final in the QA home as anywhere: the verdict does not ask which
    # home the build had, and the rule of the release, word for word, starts no second build.
    assert "needs.primary.outputs.verdict != 'code'" in fallback["if"]
    assert "gradle-home" not in load(BUILD)["jobs"]["build"]["outputs"]["verdict"]
    # No other job of the workflow runs on the self-hosted runner, and none names a Gradle home.
    for name, job in entry["jobs"].items():
        if name not in ("primary", "fallback"):
            assert job["runs-on"] == "ubuntu-latest", name
            assert "GRADLE_USER_HOME" not in json.dumps(job) and "android-qa-gradle" not in json.dumps(job), name


def test_only_the_first_qa_build_may_read_the_runs_of_the_repository(entry):
    # A build that finds the QA Gradle home taken asks GitHub whether the job that took it is over.
    # The build workflow declares no permissions, so its job has those of the job that calls it: a
    # declaration there would have to hold for a release as well, whose calling files grant no
    # `actions`, and a called workflow that asks for more than its caller has does not start.
    build = load(BUILD)
    assert "permissions" not in build and "permissions" not in build["jobs"]["build"]
    primary, fallback = entry["jobs"]["primary"], entry["jobs"]["fallback"]
    assert primary["permissions"] == {"contents": "read", "packages": "read", "actions": "read"}
    # The second build is cold and on a GitHub-hosted runner: it has what the workflow gives every job.
    assert "permissions" not in fallback and entry["permissions"] == {"contents": "read", "packages": "read"}
    # A release gets what it got while the build workflow named its permissions itself.
    release = load(RELEASE)
    assert release["permissions"] == {"contents": "read", "packages": "read"}
    for name in ("primary", "fallback"):
        assert "permissions" not in release["jobs"][name], name
    released = yaml.safe_load((ROOT / "android-ci" / "release.yml.example").read_text())["permissions"]
    assert "actions" not in released
    # The token reaches the one step that asks, by name.
    asking = [item.get("name") for item in build["jobs"]["build"]["steps"] if "github.token }}" in json.dumps(item.get("env", {}))]
    assert "Choose the Gradle user home" in asking
    assert step(build["jobs"]["build"], "Choose the Gradle user home")["env"]["TOKEN"] == "${{ github.token }}"


def test_a_qa_build_runs_in_no_environment_and_uploads_nothing_to_sentry_unless_asked(entry):
    inputs = trigger(entry)["inputs"]
    # No environment by default: the signing secrets are the repository's. The release's default
    # names one, for a repository that keeps the key there.
    assert inputs["environment"]["default"] == ""
    assert inputs["sentry-upload"] == {**inputs["sentry-upload"], "type": "boolean", "default": False, "required": False}
    # The token is named in the declaration only: it reaches a step through the build workflow, and
    # there only the Gradle step.
    assert "SENTRY_AUTH_TOKEN" not in json.dumps(entry["jobs"])
    assert "toJSON(secrets)" not in (WORKFLOWS / ENTRY).read_text()


def test_no_secret_is_required_by_the_declaration(entry, publish):
    secrets = trigger(entry)["secrets"]
    assert list(secrets) == [
        "KEYSTORE_BASE64", "KEYSTORE_PASSWORD", "KEY_ALIAS", "KEY_PASSWORD", "RUNNER_READ_TOKEN", "GIT_TOKEN",
        "SENTRY_AUTH_TOKEN", "RELEASE_PUSH_TOKEN",
    ]
    assert all(secret["required"] is False for secret in secrets.values())
    assert set(re.findall(r"secrets\.([A-Z0-9_]+)", (WORKFLOWS / ENTRY).read_text())) == {"RUNNER_READ_TOKEN", "RELEASE_PUSH_TOKEN"}
    # The token that may move a tag the job token may not: read by the one step that moves it,
    # and by no other.
    uses = [item.get("name") for job in entry["jobs"].values() for item in job.get("steps", []) if "RELEASE_PUSH_TOKEN" in json.dumps(item)]
    assert uses == ["Publish the rolling pre-release"]
    assert step(publish, "Publish the rolling pre-release")["env"]["TAG_TOKEN"] == "${{ secrets.RELEASE_PUSH_TOKEN || github.token }}"


# --- the names of a build --------------------------------------------------------------------------


def name_build(publish, tmp_path, **overrides):
    output = tmp_path / "names-output"
    output.write_text("")
    env = {
        "SHA": BUILT,
        "MAJOR": "",
        "VERSION": "1.13.1",
        "NUMBER": "57",
        "VERSION_CODE": "101300057",
        "SOURCE_BRANCH": "develop",
        "LABEL": "",
        "VERSION_GIVEN": "",
        "GITHUB_OUTPUT": str(output),
        **overrides,
    }
    result = run_script(step(publish, "Name the build")["run"], env, tmp_path)
    return result, dict(line.split("=", 1) for line in output.read_text().splitlines())


@pytest.fixture
def lines(tmp_path, monkeypatch) -> dict:
    """develop after v1.13.0, and release/1.x after v1.13.1, as the checkout of the plan job has them."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "develop")
    commit(repo, {"a": "1\n"}, "Released (#10)")
    git(repo, "tag", "v1.13.0")
    git(repo, "checkout", "-q", "-b", "release/1.x")
    commit(repo, {"a": "2\n"}, "Hotfix (#21)")
    git(repo, "tag", "v1.13.1")
    heads = {"release/1.x": commit(repo, {"a": "3\n"}, "Hotfix two (#22)")}
    git(repo, "checkout", "-q", "develop")
    git(repo, "tag", "v2.0.0", commit(repo, {"b": "1\n"}, "Redesign (#30)"))
    heads["develop"] = commit(repo, {"b": "2\n"}, "Fix (#31)")
    for branch, head in heads.items():
        git(repo, "update-ref", f"refs/remotes/origin/{branch}", head)
    monkeypatch.chdir(repo)
    return heads


@pytest.mark.parametrize("branch", ["develop", "release/1.x"])
@pytest.mark.parametrize(
    ("label", "version"), [("", ""), ("ble-test", ""), ("sleephub-sweetsleep-x", ""), ("", "next minor")]
)
def test_the_jobs_name_a_build_as_the_plan_does(entry, publish, lines, tmp_path, branch, label, version):
    # The plan's names do not cross to the other jobs (a job output with a name in it can be
    # dropped), so the build jobs and the publish job put them together again. What they arrive at
    # is what release-version planned: the version the APK is checked against, and the tag the
    # plan's summary names.
    if version:
        version = {"develop": "2.1.0", "release/1.x": "1.14.0"}[branch]
    plan = release_version.plan_qa({
        "REF": f"refs/heads/{branch}", "SHA": lines[branch], "TAG_PREFIX": "v", "SOURCE_BRANCH": "develop",
        "LABEL": label, "VERSION": version, "QA_SEQUENCE": "57",
    })
    result, names = name_build(
        publish, tmp_path, SHA=plan.target_sha, MAJOR=plan.major, VERSION=plan.version, NUMBER=str(plan.number),
        VERSION_CODE=str(plan.version_code), LABEL=label, VERSION_GIVEN=version,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert names == {
        "branch": plan.branch,
        "line": plan.line,
        "kind": plan.kind,
        "apart": label or version,
        "version-name": plan.version_name,
        "tag": plan.release_tag,
    }
    assert plan.branch == branch and plan.kind == ("label" if label else "qa")
    # The expressions of the build jobs and of the workflow's output, read as GitHub evaluates them.
    assert f"{plan.version}-{label or 'qa'}.{plan.number}" == plan.version_name
    assert release_version.QA_SUFFIX == "qa"
    assert ("label" if label != "" else "qa") == plan.kind
    outputs = trigger(entry)["outputs"]
    assert outputs["version-name"]["value"] == VERSION_NAME.replace("needs.plan", "jobs.plan")
    assert outputs["version-code"]["value"] == "${{ jobs.plan.outputs.version-code }}"
    assert "release-tag" not in outputs


@pytest.mark.parametrize("empty", ["SHA", "VERSION", "NUMBER", "VERSION_CODE"])
def test_an_output_the_runner_dropped_ends_the_publish_job_with_the_reason(publish, checks, tmp_path, empty):
    # A secret whose value is a word of the plan (a version, a number) empties the output without
    # failing the plan. The first step of the job that publishes says so.
    result, names = name_build(publish, tmp_path, **{empty: ""})
    assert result.returncode == 1
    assert f"::error title=The plan is incomplete::Empty: {empty}. The plan job passed, so the runner dropped the output" in result.stdout
    assert names == {}
    assert publish["steps"][0] is step(publish, "Name the build")
    # The job that waits for the checks needs no name: it reads the checks of the commit, by its id.
    assert "Name the build" not in [item.get("name") for item in checks["steps"]]
    assert "steps.names" not in json.dumps(checks)
    env = step(publish, "Name the build")["env"]
    assert env["SOURCE_BRANCH"] == "${{ inputs.source-branch || github.event.repository.default_branch }}"
    assert env["LABEL"] == "${{ inputs.label }}" and env["VERSION_GIVEN"] == "${{ inputs.version }}"
    for name, output in {"SHA": "sha", "MAJOR": "major", "VERSION": "version", "NUMBER": "number", "VERSION_CODE": "version-code"}.items():
        assert env[name] == f"${{{{ needs.plan.outputs.{output} }}}}"


# --- built: the check a release requires ----------------------------------------------------------


def state_built(entry, tmp_path, **env):
    script = step(entry["jobs"]["built"], "State whether the commit was built")["run"]
    return run_script(script, {"PLAN": "success", "PRIMARY": "success", "FALLBACK": "skipped", "SHA": BUILT, **env}, tmp_path)


@pytest.mark.parametrize(
    ("results", "built"),
    [
        ({"PRIMARY": "success", "FALLBACK": "skipped"}, True),
        # The self-hosted build did not pass for a reason outside the code, and the second one did.
        ({"PRIMARY": "failure", "FALLBACK": "success"}, True),
        ({"PRIMARY": "cancelled", "FALLBACK": "success"}, True),
        ({"PRIMARY": "failure", "FALLBACK": "skipped"}, False),
        ({"PRIMARY": "failure", "FALLBACK": "failure"}, False),
        ({"PRIMARY": "cancelled", "FALLBACK": "cancelled"}, False),
        # The plan refused the run, or the run was cancelled before anything was built.
        ({"PLAN": "failure", "PRIMARY": "skipped", "FALLBACK": "skipped"}, False),
        ({"PLAN": "cancelled", "PRIMARY": "skipped", "FALLBACK": "skipped"}, False),
        ({"PLAN": "", "PRIMARY": "", "FALLBACK": ""}, False),
    ],
)
def test_built_succeeds_only_when_a_build_of_the_commit_passed(entry, tmp_path, results, built):
    result = state_built(entry, tmp_path, **results)
    assert (result.returncode == 0) == built, result.stdout + result.stderr
    if built:
        assert "3ff9b1a was built with ciQa on " in result.stdout and "::error" not in result.stdout
    else:
        assert "::error title=The commit was not built::3ff9b1a: plan " in result.stdout
        assert "A release of this commit requires this check" in result.stdout
        # A run that was stopped, with nothing failed, says what stops a run and what to do.
        stopped = "cancelled" in results.values() and "failure" not in results.values()
        assert ("by the run of a later commit of the branch" in result.stdout) == stopped
        assert ("to release this commit, run this run again." in result.stdout) == stopped


def test_a_run_that_a_later_push_cancelled_while_it_built_has_no_successful_check(entry, tmp_path):
    # A1: a commit that was not built cannot be tagged. The run of the next push cancels this one
    # while Gradle runs. `built` runs all the same (always()), reads the build as cancelled and
    # fails; the second build is not started for a cancelled run; nothing waits and nothing publishes.
    result = state_built(entry, tmp_path, PRIMARY="cancelled", FALLBACK="skipped")
    assert result.returncode == 1
    assert "plan success, build cancelled, build on the GitHub-hosted runner skipped." in result.stdout
    jobs = entry["jobs"]
    assert jobs["built"]["if"] == "${{ always() }}"
    for name in ("fallback", "checks", "publish"):
        assert "!cancelled()" in jobs[name]["if"], name
    # Cancelled after the build passed, while the run waited for its checks: `built` had succeeded
    # by then, and that check stays. The commit was built, and can be released once its other
    # checks are there.
    assert jobs["built"]["needs"] == ["plan", "primary", "fallback"] and "checks" not in jobs["built"]["needs"]
    assert state_built(entry, tmp_path, PRIMARY="success", FALLBACK="skipped").returncode == 0


def test_built_is_the_check_of_a_push_and_states_the_build_whatever_publish_does(entry):
    built = entry["jobs"]["built"]
    # The name of the check under a calling job `qa`: "qa / built". A manual run builds what its
    # inputs say (a label, a version, another flavor), and its check has another name: the newest
    # run of a name is what a release reads, and a manual build must neither stand in for the
    # build of the push nor hide it.
    assert built["name"] == "${{ github.event_name == 'push' && 'built' || 'built (manual run)' }}"
    # A plan that failed and a cancelled run end in a failed check, not in a skipped job.
    assert built["if"] == "${{ always() }}"
    # The build, not the publication: a commit whose APK a newer build kept out was still built.
    assert built["needs"] == ["plan", "primary", "fallback"]
    assert "publish" not in json.dumps(built)
    assert built["runs-on"] == "ubuntu-latest" and built["permissions"] == {}
    env = step(built, "State whether the commit was built")["env"]
    assert env == {
        "PLAN": "${{ needs.plan.result }}",
        "PRIMARY": "${{ needs.primary.result }}",
        "FALLBACK": "${{ needs.fallback.result }}",
        "SHA": "${{ needs.plan.outputs.sha || github.sha }}",
    }
    # What the two workflows that release require by default, next to the pull request check.
    for name in ("android-version-bump.yml", RELEASE):
        assert "qa / built" in trigger(load(name))["inputs"]["required-checks"]["default"].splitlines()
    # The plan no longer refuses a commit the pre-release is past: it is built, for its check.
    assert "An older build does not replace a newer one" not in (ROOT / "release-version" / "release_version.py").read_text()


# --- publish: the checks of the commit ------------------------------------------------------------


def test_the_checks_are_waited_for_after_the_build_and_outside_the_queue_of_the_pre_release(entry, checks):
    # The job starts for the build that passed, as the publish job of a release does.
    assert checks["needs"] == ["plan", "primary", "fallback"]
    assert checks["if"] == load(RELEASE)["jobs"]["publish"]["if"]
    assert checks["runs-on"] == "ubuntu-latest"
    # The wait of one commit holds up no other: a commit whose pull request check was replaced
    # while it waited never gets the check, and inside the group of the pre-release its wait
    # would keep every later build from being published for as long as it lasts. And a job that
    # only waits can be cancelled for a later commit, which a job in that group cannot.
    assert "concurrency" not in checks
    # It reads the checks, and deletes the artifact of a build that is not published. It cannot
    # write a release or a tag.
    assert checks["permissions"] == {"contents": "read", "checks": "read", "actions": "write"}
    assert [item["name"] for item in checks["steps"]] == ["Check the commit's checks", "Delete the build artifact"]
    assert "if" not in checks["steps"][0]
    # The job waits, so its own timeout has to leave room for the longest wait.
    assert checks["timeout-minutes"] == 120
    assert "At most 100" in trigger(entry)["inputs"]["checks-timeout-minutes"]["description"]
    # Nothing crosses to the publish job but the result of this one. The job no longer decides
    # that a build was overtaken: the run of the later commit cancels this run instead, so there
    # is no word that says "do not publish", and none that could get lost on the way.
    assert "outputs" not in checks
    text = (WORKFLOWS / ENTRY).read_text()
    for gone in ("superseded", "ROLLING_TAG", "commits_past", "/compare/"):
        assert gone not in text, gone


def test_publish_follows_the_checks_and_is_the_only_job_that_writes(entry, publish, checks):
    assert publish["needs"] == ["plan", "primary", "fallback", "checks"]
    # Only for a commit whose checks passed: a commit that failed them and one the job could not
    # read them for end `checks` in a failure. The condition reads the result of that job and no
    # output of it. A result cannot be dropped the way an output can, so whatever goes wrong
    # between the two jobs ends in nothing published.
    assert publish["if"] == "${{ !cancelled() && needs.checks.result == 'success' }}"
    assert publish["runs-on"] == "ubuntu-latest"
    # The release and its tag, and the artifact it deletes.
    assert publish["permissions"] == {"contents": "write", "actions": "write"}
    assert entry["permissions"] == {"contents": "read", "packages": "read"}
    for name, job in entry["jobs"].items():
        if name != "publish":
            assert job.get("permissions", {}).get("contents", "read") == "read", name
    names = [item.get("name") or item.get("uses") for item in publish["steps"]]
    assert names == [
        "Name the build",
        "Download the signed APKs",
        "actions/checkout@v7",
        "List the changes since the build before",
        "Publish the rolling pre-release",
        "Delete the build artifact",
    ]
    # No step runs with always() but the one that deletes the artifact.
    for item in publish["steps"][:5]:
        assert "if" not in item, item
    # The tags are read after the download, right before the tag is moved: what they say about the
    # other pre-releases is then as fresh as it can be.
    assert publish["steps"][2]["with"] == {
        "ref": "${{ needs.plan.outputs.sha }}", "fetch-depth": 0, "persist-credentials": False,
    }
    download = step(publish, "Download the signed APKs")["with"]
    assert download["name"] == "android-qa-apk-${{ needs.primary.result == 'success' && 'primary' || 'fallback' }}"
    # The name the build workflow uploads under for the two kinds of this workflow.
    upload = step(load(BUILD)["jobs"]["build"], "Upload the signed APKs")["with"]
    assert upload["name"] == "android-${{ (inputs.kind == 'qa' || inputs.kind == 'label') && 'qa' || 'release' }}-apk-${{ inputs.attempt }}"
    changes = step(publish, "List the changes since the build before")
    assert changes["with"] == {
        "operation": "changes",
        "sha": "${{ needs.plan.outputs.sha }}",
        "since": "${{ steps.names.outputs.tag }}",
        "version-code": "${{ needs.plan.outputs.version-code }}",
    }
    # Nothing in the job waits for anything but its own uploads.
    assert "Check the commit's checks" not in names
    assert publish["timeout-minutes"] == 30


def test_one_publish_job_at_a_time_per_pre_release_and_none_is_cancelled(entry, publish):
    # Two builds that finish together would each delete the APK of the other. The group is the
    # pre-release: the line, and what keeps a build apart from the line's rolling one.
    pre_release = (
        "format('android-qa-publish-{0}-{1}', needs.plan.outputs.major || 'source', "
        "inputs.label || (inputs.version != '' && needs.plan.outputs.version) || 'rolling')"
    )
    # Only a job that will publish is in that group. GitHub keeps one waiting job per group, and
    # should it put a job into its group before it looks at the job's `if`, a job that is about to
    # be skipped would replace the one that waits. Such a job is in a group of its own run. The
    # condition is that of `if`, without the cancelling.
    alone = "format('android-qa-no-publish-{0}-{1}', github.run_id, github.run_attempt)"
    assert publish["concurrency"] == {
        "group": "${{ needs.checks.result == 'success' && " + pre_release + " || " + alone + " }}",
        "cancel-in-progress": False,
    }
    assert "needs.checks.result == 'success'" in publish["if"]
    # Nothing else is in a group. No run is cancelled by GitHub for a later one, which would stop
    # a publish job half way and could not tell an earlier commit from a later one: the plan job
    # of the later commit's run cancels, and it leaves a run that publishes alone.
    assert "concurrency" not in entry
    for name, job in entry["jobs"].items():
        if name != "publish":
            assert "concurrency" not in job, name


def test_the_checks_are_decided_by_the_script_of_the_version_bump(entry, checks):
    # One rule in three workflows: an APK is handed out for a commit that passed what a pull
    # request has to pass. Only the wait is this workflow's.
    ours = step(checks, "Check the commit's checks")
    bump = step(load("android-version-bump.yml")["jobs"]["plan"], "Check the commit's checks")
    release = step(load(RELEASE)["jobs"]["plan"], "Check the commit's checks")
    assert ours["run"] == bump["run"] == release["run"]
    assert "python3 -I - <<'CHECKS'" in ours["run"]
    assert ours["id"] == "checks"
    assert ours["env"] == {
        "TOKEN": "${{ github.token }}",
        "TARGET_SHA": "${{ needs.plan.outputs.sha }}",
        "REQUIRED_CHECKS": "${{ inputs.required-checks }}",
        "CHECKS_WAIT_MINUTES": "${{ inputs.checks-timeout-minutes }}",
    }
    # A release is decided from the checks as they are. And nothing in the script passes over a
    # check: it has one way to end without an error, which is that every required check succeeded.
    for other in (bump, release):
        assert "CHECKS_WAIT_MINUTES" not in other["env"]
    assert "sys.exit(0)" not in ours["run"] and ours["run"].count("sys.exit(") == 3
    inputs = trigger(entry)["inputs"]
    # The APK waits for the pull request check alone: the other check a release requires is this
    # very workflow's.
    assert inputs["required-checks"]["default"] == "pr-check / result"
    assert inputs["checks-timeout-minutes"]["default"] == 60


class Answers:
    """The check runs of a commit as they change: each time the API is asked, the next answer."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = 0

    def __iter__(self):
        answer = self.answers[min(self.asked, len(self.answers) - 1)]
        self.asked += 1
        return iter(answer)


def wait_for_checks(checks, tmp_path, api, minutes="1", required="pr-check / result"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    summary, output = tmp_path / "summary", tmp_path / "checks-output"
    summary.write_text("")
    output.write_text("")
    env = {
        "TOKEN": "job-token",
        "TARGET_SHA": SHA,
        "REQUIRED_CHECKS": required,
        "CHECKS_WAIT_MINUTES": minutes,
        "CHECKS_POLL_SECONDS": "0.01",
        "GITHUB_API_URL": f"http://127.0.0.1:{api.server_port}",
        "GITHUB_REPOSITORY": "acme/app",
        "GITHUB_STEP_SUMMARY": str(summary),
        "GITHUB_OUTPUT": str(output),
    }
    result = run_script(step(checks, "Check the commit's checks")["run"], env, tmp_path)
    # What the step tells the step after it: refused=true or nothing.
    result.refused = output.read_text()
    return result, summary.read_text()


def test_a_commit_whose_check_never_comes_does_not_hold_up_the_build_after_it(entry, checks, publish, tmp_path):
    # Three merges in a row, A, B and C, while a pull request check is running. That workflow
    # keeps one waiting run per branch, so the run of B is replaced by the run of C: B never gets
    # `pr-check / result`.
    #
    # Were the wait part of the job that publishes, B would hold the turn of the pre-release for
    # the whole of checks-timeout-minutes, and C behind it. It is a job of its own instead, and
    # what ends it is the run of C: that run cancels the run of B, which is waiting and not
    # publishing. The job itself asks about nothing but the checks of its commit.
    assert "concurrency" not in checks and "concurrency" in publish
    assert "checks" in publish["needs"] and "Check the commit's checks" not in [item.get("name") for item in publish["steps"]]
    with check_runs_api() as api:
        api.runs = []
        result, summary = wait_for_checks(checks, tmp_path, api, minutes="0.002")
        assert len(api.requests) >= 2
        assert {path.split("?")[0] for path, _ in api.requests} == {f"/repos/acme/app/commits/{SHA}/check-runs"}
        assert all(authorization == "Bearer job-token" for _, authorization in api.requests)
    # Without a later push nothing ends the wait but its time, and then the commit is not published.
    assert result.returncode == 1 and result.refused == "refused=true\n"
    assert "| pr-check / result | not run |" in summary
    # The cancelling is the plan's, of the run of the later commit: its tests are further up.
    assert "Cancel the runs of earlier commits" in [item.get("name") for item in entry["jobs"]["plan"]["steps"]]
    assert "cancel" not in json.dumps(checks).replace("cancelled", "")


def test_publish_waits_for_a_check_that_has_not_finished(checks, tmp_path):
    # The push started the checks and this build together. First the check does not exist on the
    # commit, then it is queued, then it runs, then it has passed.
    with check_runs_api() as api:
        api.runs = Answers(
            [],
            [check_run(conclusion=None, status="queued")],
            [check_run(conclusion=None, status="in_progress")],
            [check_run()],
        )
        result, summary = wait_for_checks(checks, tmp_path, api)
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(api.requests) == 4
    assert "Waiting for pr-check / result ('pr-check / result' did not run on the commit)." in result.stdout
    assert "Waiting for pr-check / result ('pr-check / result' is in_progress)." in result.stdout
    assert "| pr-check / result | [success](https://github.invalid/runs/1) |" in summary
    assert "::error" not in result.stdout
    assert result.refused == ""


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped"])
def test_a_check_that_fails_ends_the_wait_and_nothing_is_published(checks, tmp_path, conclusion):
    with check_runs_api() as api:
        api.runs = Answers([check_run(conclusion=None, status="in_progress")], [check_run(conclusion=conclusion)])
        result, summary = wait_for_checks(checks,tmp_path, api, minutes="100")
        assert len(api.requests) == 2, "the wait ends with the first answer that is final"
    assert result.returncode == 1
    assert f"::error title=The commit has not passed its checks::{SHA[:7]}: 'pr-check / result' is {conclusion}. " in result.stdout
    assert "after waiting" not in result.stdout
    assert f"[{conclusion}]" in summary
    # The checks answered, and the answer is no: the artifact of this build is of no use.
    assert result.refused == "refused=true\n"


def test_one_failed_check_is_final_while_another_still_runs(checks, tmp_path):
    with check_runs_api() as api:
        api.runs = [check_run(conclusion="failure"), check_run(name="lint", conclusion=None, status="in_progress")]
        result, _ = wait_for_checks(checks,tmp_path, api, minutes="100", required="pr-check / result\nlint")
        assert len(api.requests) == 2, "one request for each check, and no second round"
    assert result.returncode == 1
    assert "'pr-check / result' is failure; 'lint' is in_progress." in result.stdout


@pytest.mark.parametrize(
    ("runs", "state"),
    [
        ([], "'pr-check / result' did not run on the commit"),
        ([check_run(conclusion=None, status="in_progress")], "'pr-check / result' is in_progress"),
    ],
)
def test_a_check_that_never_finishes_fails_the_job_after_the_wait(checks, tmp_path, runs, state):
    with check_runs_api() as api:
        api.runs = runs
        # A fraction of a minute, so that the test does not take one.
        result, _ = wait_for_checks(checks,tmp_path, api, minutes="0.002")
        assert len(api.requests) >= 2
    assert result.returncode == 1
    assert f"::error title=The commit has not passed its checks::{SHA[:7]}: {state} after waiting 0.002 minutes. " in result.stdout
    assert result.refused == "refused=true\n"


def test_a_check_that_already_passed_is_not_waited_for(checks, tmp_path):
    with check_runs_api() as api:
        api.runs = [check_run()]
        result, _ = wait_for_checks(checks,tmp_path, api)
        assert (result.returncode, len(api.requests)) == (0, 1)
    assert "Waiting" not in result.stdout


def test_a_request_that_fails_during_the_wait_is_made_again(checks, tmp_path):
    # An hour of waiting is some 120 requests. One that meets a server error or a rate limit is
    # not the checks' answer: the step asks again, and the build is published when they pass.
    with check_runs_api() as api:
        api.statuses = [502, 429, 503]
        api.runs = Answers([check_run(conclusion=None, status="in_progress")], [check_run()])
        result, summary = wait_for_checks(checks,tmp_path, api)
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(api.requests) == 5
    assert result.stdout.count(f"Cannot read the checks of {SHA} (HTTP 50") == 2
    assert f"Cannot read the checks of {SHA} (HTTP 429: " in result.stdout
    assert "Asking again." in result.stdout
    assert "[success]" in summary and "::error" not in result.stdout + result.stderr


def test_checks_that_cannot_be_read_fail_the_job_without_being_a_refusal(checks, tmp_path):
    # The server never answers: the job fails when the wait is over. That is not the checks
    # refusing the commit, so the artifact stays and the job can be run again.
    with check_runs_api() as api:
        api.status = 503
        result, _ = wait_for_checks(checks,tmp_path, api, minutes="0.002")
        assert len(api.requests) >= 2
    assert result.returncode == 1
    assert f"::error::Cannot read the checks of {SHA}: HTTP 503" in result.stderr
    assert result.refused == ""
    # A token that may not read the checks will not be let in by asking again.
    with check_runs_api() as api:
        api.status = 403
        result, _ = wait_for_checks(checks,tmp_path, api, minutes="100")
        assert len(api.requests) == 1
    assert result.returncode == 1
    assert "HTTP 403" in result.stderr and result.refused == ""


def test_the_wait_has_to_be_a_number(checks, tmp_path):
    with check_runs_api() as api:
        result, _ = wait_for_checks(checks,tmp_path, api, minutes="soon")
        assert api.requests == []
    assert result.returncode == 1
    assert "The time to wait for the checks is not a number." in result.stderr


# --- publish: the rolling pre-release ------------------------------------------------------------

# A stand-in for gh that keeps the state of one release: its tag, its assets, its drafts. Every
# call is recorded with the token it was made with, and after every change of the assets their
# names are recorded too, so a test can ask what a download would have found at each moment.
GH_STUB = r'''#!/usr/bin/env python3
import json
import os
import sys

directory = os.environ["GH_DIR"]
arguments = sys.argv[1:]
with open(os.path.join(directory, "calls"), "a") as calls:
    calls.write(" ".join(arguments) + "\n")
with open(os.path.join(directory, "tokens"), "a") as tokens:
    tokens.write(os.environ.get("GH_TOKEN", "") + " " + " ".join(arguments[:4]) + "\n")
path = os.path.join(directory, "state.json")
with open(path) as file:
    state = json.load(file)


def done(text=""):
    with open(path, "w") as file:
        json.dump(state, file)
    if text:
        print(text)
    sys.exit(0)


def fail(message, code=1):
    with open(path, "w") as file:
        json.dump(state, file)
    print(message, file=sys.stderr)
    sys.exit(code)


def seen():
    state["history"].append([asset["name"] for asset in state["assets"]])


def add(name):
    state["next"] += 1
    state["assets"].append({"id": state["next"], "name": name})
    seen()


def fields(flag):
    return dict(arguments[i + 1].split("=", 1) for i, value in enumerate(arguments) if value == flag)


if arguments[0] == "release":
    command, tag, rest = arguments[1], arguments[2], arguments[3:]
    files = [value for value in rest if value.endswith(".apk")]
    if command == "upload":
        for file in files:
            name = os.path.basename(file)
            if state["fail_upload"] and state["fail_upload"] in name:
                fail("gh: Server Error (HTTP 502)")
            held = [asset for asset in state["assets"] if asset["name"] == name]
            if held and "--clobber" not in rest:
                fail("gh: Validation Failed: already_exists (HTTP 422)")
            if held:
                # As gh does it: the asset of that name is deleted, and the upload starts then.
                state["assets"].remove(held[0])
                seen()
            add(name)
        done("gh output that must not reach the summary")
    if command == "edit":
        if state["fail_edit"]:
            fail("gh: Server Error (HTTP 502)")
        state["title"] = rest[rest.index("--title") + 1]
        state["notes"] = open(rest[rest.index("--notes-file") + 1]).read()
        state["marks"] = [value for value in rest if value in ("--prerelease", "--latest=false")]
        done("gh output that must not reach the summary")
    if command == "create":
        if state["tag"] is None or "--verify-tag" not in rest:
            fail("gh: tag " + tag + " does not exist")
        if state["fail_create"]:
            fail("gh: Server Error (HTTP 502)")
        state["release"] = True
        state["title"] = rest[rest.index("--title") + 1]
        state["notes"] = open(rest[rest.index("--notes-file") + 1]).read()
        state["marks"] = [value for value in rest if value in ("--prerelease", "--latest=false")]
        for file in files:
            add(os.path.basename(file))
        done("https://github.invalid/acme/app/releases/tag/" + tag)
    fail("unexpected gh call: " + " ".join(arguments), 97)

assert arguments[0] == "api", arguments
method = arguments[arguments.index("--method") + 1] if "--method" in arguments else "GET"
jq = arguments[arguments.index("--jq") + 1] if "--jq" in arguments else ""
route = next(value for value in arguments[1:] if value.startswith("repos/acme/app/"))[len("repos/acme/app/"):]

if method == "GET" and route.startswith("releases/tags/"):
    if state["release_error"]:
        fail(state["release_error"])
    if not state["release"]:
        fail("gh: Not Found (HTTP 404)")
    if jq == ".tag_name":
        done(route[len("releases/tags/"):])
    if jq == '.assets[] | "\\(.id) \\(.name)"':
        # fail_assets: true fails every listing, a number the listing of that count.
        state["listings"] += 1
        if state["fail_assets"] is True or state["fail_assets"] == state["listings"]:
            fail("gh: Server Error (HTTP 502)")
        done("\n".join(f"{asset['id']} {asset['name']}" for asset in state["assets"]))
if method == "GET" and route.startswith("git/ref/tags/"):
    if state["tag_error"]:
        fail(state["tag_error"])
    if state["tag"] is None:
        fail("gh: Not Found (HTTP 404)")
    done(state["tag"])
if method == "POST" and route == "git/tags":
    given = fields("-f")
    state["objects"] += 1
    name = "%040x" % (0x7A6 * 16 ** 36 + state["objects"])
    state["tags"][name] = {"tag": given["tag"], "message": given["message"], "object": given["object"], "type": given["type"]}
    done(name)
if method == "PATCH" and route.startswith("git/refs/tags/"):
    given = {**fields("-f"), **fields("-F")}
    if state["refuse_tag"] and given["sha"] in state["tags"]:
        fail("gh: Repository rule violations found (HTTP 422)")
    if state["tag"] is None or given.get("force") != "true":
        fail("gh: Reference does not exist (HTTP 422)")
    state["tag"] = given["sha"]
    done("{}")
if method == "POST" and route == "git/refs":
    given = fields("-f")
    if state["refuse_tag"]:
        fail("gh: Repository rule violations found (HTTP 422)")
    if state["tag"] is not None:
        fail("gh: Reference already exists (HTTP 422)")
    assert given["ref"].startswith("refs/tags/"), given
    state["tag"] = given["sha"]
    done("{}")
if method == "DELETE" and route.startswith("git/refs/tags/"):
    state["tag"] = None
    done()
if method == "DELETE" and route.startswith("releases/assets/"):
    asset = [asset for asset in state["assets"] if str(asset["id"]) == route.rsplit("/", 1)[1]]
    if not asset:
        fail("gh: Not Found (HTTP 404)")
    if state["fail_delete"] and state["fail_delete"] in asset[0]["name"]:
        fail("gh: Server Error (HTTP 502)")
    state["assets"].remove(asset[0])
    seen()
    done()
if method == "PATCH" and route.startswith("releases/assets/"):
    name = fields("-f")["name"]
    if state["fail_rename"]:
        fail("gh: Server Error (HTTP 502)")
    if any(asset["name"] == name for asset in state["assets"]):
        fail("gh: Validation Failed: already_exists (HTTP 422)")
    for asset in state["assets"]:
        if str(asset["id"]) == route.rsplit("/", 1)[1]:
            asset["name"] = name
    seen()
    done("{}")
if method == "GET" and route == "releases":
    assert "--paginate" in arguments and ".draft" in jq, arguments
    done("\n".join(str(draft) for draft in state["drafts"]))
if method == "DELETE" and route.startswith("releases/"):
    state["drafts"].remove(int(route.rsplit("/", 1)[1]))
    done()
if method == "GET" and route.startswith("actions/runs/"):
    if state["artifacts"] is None:
        fail("gh: Resource not accessible by integration (HTTP 403)")
    done("\n".join(str(artifact) for artifact in state["artifacts"]))
if method == "DELETE" and route.startswith("actions/artifacts/"):
    if int(route.rsplit("/", 1)[1]) in state["undeletable"]:
        fail("gh: Forbidden (HTTP 403)")
    done()
fail("unexpected gh call: " + " ".join(arguments), 97)
'''
BUILT = "3ff9b1a3" + "0" * 32
BEFORE = "e8760a1c" + "1" * 32
# The tag object the rolling tag named before this build: what the ref of an annotated tag holds.
BEFORE_TAG = "7a60000b" + "2" * 32
FIRST_OBJECT = "%040x" % (0x7A6 * 16 ** 36 + 1)
APK = "HeySleep_qa_2.0.0-qa.1057_200001057.apk"


def gh_directory(tmp_path: Path, release=False, tag=None, assets=(), **state) -> Path:
    """A stand-in for gh: whether the rolling pre-release and its tag exist, and the APKs it holds."""
    directory = tmp_path / "gh"
    directory.mkdir(parents=True)
    executable(directory / "gh", GH_STUB)
    (directory / "calls").write_text("")
    (directory / "tokens").write_text("")
    held = [{"id": number, "name": name} for number, name in enumerate(assets, start=1)]
    initial = {
        "release": release, "tag": tag, "assets": held, "next": len(held), "history": [], "objects": 0, "tags": {},
        "drafts": [], "artifacts": None, "undeletable": [], "title": "", "notes": "", "marks": [],
        "release_error": "", "tag_error": "", "refuse_tag": False, "fail_upload": "", "fail_assets": False,
        "listings": 0, "fail_delete": "", "fail_rename": False, "fail_edit": False, "fail_create": False, **state,
    }
    (directory / "state.json").write_text(json.dumps(initial))
    return directory


def state_of(directory: Path) -> dict:
    return json.loads((directory / "state.json").read_text())


def publish_build(publish, tmp_path, directory: Path, apks=(APK,), **overrides):
    apk_directory, temp = tmp_path / "apk", tmp_path / "temp"
    temp.mkdir(exist_ok=True)
    for name in apks:
        (apk_directory / name).parent.mkdir(parents=True, exist_ok=True)
        (apk_directory / name).write_text("apk")
    apk_directory.mkdir(exist_ok=True)
    summary = tmp_path / "summary"
    summary.write_text("")
    env = {
        "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}",
        "GH_DIR": str(directory),
        "GH_REPO": "acme/app",
        "GH_TOKEN": "token",
        "TAG_TOKEN": "tag-token",
        "TAG": "qa-develop",
        "SHA": BUILT,
        "BRANCH": "develop",
        "LINE": "develop",
        "KIND": "qa",
        "APART": "",
        "VERSION": "2.0.0",
        "VERSION_NAME": "2.0.0-qa.1057",
        "VERSION_CODE": "200001057",
        "PREVIOUS_TAG": "",
        "SINCE": BEFORE,
        # The tag as the checkout of this job had it: where the stand-in has it, unless a test moves it.
        "SINCE_OBJECT": state_of(directory)["tag"] or "",
        "PRS": "- #340 fix: Close the session\n- #341 -n 100% `code` $(not run) %s",
        "STALE": "false",
        "UNCHANGED": "false",
        "CODE_TAKEN_BY": "",
        "APK_DIRECTORY": str(apk_directory),
        "STAGING_DIRECTORY": str(temp / "android-qa-upload"),
        "NOTES_FILE": str(temp / "android-qa-notes.md"),
        "BUILT_ON": "primary",
        "SELF_HOSTED": "true",
        "GITHUB_SERVER_URL": "https://github.invalid",
        "GITHUB_RUN_NUMBER": "57",
        "GITHUB_RUN_ID": "9001",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_STEP_SUMMARY": str(summary),
        **overrides,
    }
    result = run_script(step(publish, "Publish the rolling pre-release")["run"], env, tmp_path)
    calls = (directory / "calls").read_text().splitlines()
    return result, calls, state_of(directory), summary.read_text()


def writes(calls: list) -> list:
    """The calls that change something, in order."""
    return [call for call in calls if call.startswith("release ") or "--method" in call]


def test_the_first_build_of_a_line_creates_its_pre_release(publish, tmp_path):
    directory = gh_directory(tmp_path)
    result, calls, state, summary = publish_build(publish, tmp_path, directory, SINCE="", PRS="- #1 Feature")
    assert result.returncode == 0, result.stdout + result.stderr
    apk = tmp_path / "apk" / APK
    notes_file = tmp_path / "temp" / "android-qa-notes.md"
    assert calls == [
        "api repos/acme/app/releases/tags/qa-develop --jq .tag_name",
        "api repos/acme/app/git/ref/tags/qa-develop --jq .object.sha",
        # The tag first, at the commit that was built and naming the build; then the release of
        # that tag and no other.
        "api --method POST repos/acme/app/git/tags -f tag=qa-develop -f message=2.0.0-qa.1057 (200001057) "
        f"-f object={BUILT} -f type=commit --jq .sha",
        f"api --method POST repos/acme/app/git/refs -f ref=refs/tags/qa-develop -f sha={FIRST_OBJECT}",
        'api --paginate repos/acme/app/releases --jq .[] | select(.draft and .tag_name == "qa-develop") | .id',
        "release create qa-develop --verify-tag --title QA develop: 2.0.0-qa.1057 "
        f"--notes-file {notes_file} --prerelease --latest=false {apk}",
    ]
    assert state["tag"] == FIRST_OBJECT and state["tags"][FIRST_OBJECT]["object"] == BUILT
    assert [asset["name"] for asset in state["assets"]] == [APK]
    # A pre-release, and never the latest release of the repository.
    assert state["marks"] == ["--prerelease", "--latest=false"]
    assert f"Created the pre-release with {APK}: <https://github.invalid/acme/app/releases/tag/qa-develop>" in summary
    assert "| 2.0.0-qa.1057 | 200001057 | qa-develop | a self-hosted runner |" in summary
    assert "gh output" not in summary
    # No build and no release before it: the whole history of the repository is not listed.
    assert state["notes"].endswith("#### Changes\n\nThe first build of this line, with no release before it to compare with.\n")
    assert "#1 Feature" not in state["notes"]


def test_every_later_build_moves_the_tag_then_replaces_the_apk(publish, tmp_path):
    old = ("HeySleep_qa_2.0.0-qa.1056_200001056.apk", "HeySleep_qa_2.0.0-qa.1049_200001049.apk")
    directory = gh_directory(tmp_path, release=True, tag=BEFORE_TAG, assets=old)
    result, calls, state, summary = publish_build(publish, tmp_path, directory)
    assert result.returncode == 0, result.stdout + result.stderr
    apk = tmp_path / "apk" / APK
    notes_file = tmp_path / "temp" / "android-qa-notes.md"
    listing = 'api repos/acme/app/releases/tags/qa-develop --jq .assets[] | "\\(.id) \\(.name)"'
    assert calls == [
        "api repos/acme/app/releases/tags/qa-develop --jq .tag_name",
        "api repos/acme/app/git/ref/tags/qa-develop --jq .object.sha",
        # The tag moves before anything is uploaded: a repository that refuses the move is left as
        # it was.
        "api --method POST repos/acme/app/git/tags -f tag=qa-develop -f message=2.0.0-qa.1057 (200001057) "
        f"-f object={BUILT} -f type=commit --jq .sha",
        f"api --method PATCH repos/acme/app/git/refs/tags/qa-develop -f sha={FIRST_OBJECT} -F force=true",
        listing,
        # The new APK is there before anything is removed: the release is never without an APK.
        f"release upload qa-develop {apk}",
        f"release edit qa-develop --title QA develop: 2.0.0-qa.1057 --notes-file {notes_file} --prerelease --latest=false",
        listing,
        # Only the newest build is kept, so the releases do not grow with every push.
        "api --method DELETE repos/acme/app/releases/assets/1",
        "api --method DELETE repos/acme/app/releases/assets/2",
    ]
    assert state["tag"] == FIRST_OBJECT
    assert [asset["name"] for asset in state["assets"]] == [APK]
    assert all(names for names in state["history"]), "the release was never without an APK"
    assert f"Replaced the APK of the pre-release with {APK}" in summary
    assert f"Deleted {old[0]} of the build before." in result.stdout
    # The tag is moved with the token for it, everything else with the job token.
    tokens = (directory / "tokens").read_text().splitlines()
    assert [line.split(" ", 1)[0] for line in tokens if "git/tags" in line or "git/refs" in line] == ["tag-token", "tag-token"]
    assert {line.split(" ", 1)[0] for line in tokens if "git/tags" not in line and "git/refs" not in line} == {"token"}


@pytest.mark.parametrize(
    ("held", "built"),
    [
        # A build that does not name its APK after the version: every build is app-release.apk.
        ("app-release.apk", "app-release.apk"),
        # The same run again: the same version, so the same name.
        (APK, APK),
    ],
)
def test_an_apk_of_a_name_the_release_holds_is_replaced_without_a_gap(publish, tmp_path, held, built):
    # `gh release upload --clobber` deletes the asset of the same name and uploads then: for as
    # long as a 360 MB upload takes, and for good when it fails or the run is cancelled, the
    # pre-release has no APK. So the new file goes up next to the old one under another name, the
    # old one is deleted, and the new one takes its name.
    directory = gh_directory(tmp_path, release=True, tag=BEFORE_TAG, assets=(held,))
    result, calls, state, summary = publish_build(publish, tmp_path, directory, apks=(built,))
    assert result.returncode == 0, result.stdout + result.stderr
    staged = built[: -len(".apk")] + ".run-9001-1.apk"
    assert writes(calls)[2:] == [
        f"release upload qa-develop {tmp_path / 'temp' / 'android-qa-upload' / staged} --clobber",
        "api --method DELETE repos/acme/app/releases/assets/1",
        f"api --method PATCH repos/acme/app/releases/assets/2 -f name={built}",
        f"release edit qa-develop --title QA develop: 2.0.0-qa.1057 --notes-file {tmp_path / 'temp' / 'android-qa-notes.md'} --prerelease --latest=false",
    ]
    # Never without an APK. Between the two calls that replace it, the release holds the new APK
    # under the name it was uploaded with, and none under its own: a moment, not a gap.
    assert state["history"] == [[held, staged], [staged], [built]]
    assert state["assets"] == [{"id": 2, "name": built}]
    assert f"Replaced {built}: uploaded next to the one the release held, which was then deleted." in result.stdout
    assert f"Replaced the APK of the pre-release with {built}" in summary
    # What the stand-in would have recorded for --clobber on the name itself: a moment without it.
    assert [] not in state["history"]
    assert f"{tmp_path / 'apk' / built} --clobber" not in "\n".join(calls)


def test_what_an_upload_that_stopped_half_way_left_is_cleared_by_the_next_build(publish, tmp_path):
    leftover = "HeySleep_qa_2.0.0-qa.1056_200001056.run-8990-1.apk"
    directory = gh_directory(tmp_path, release=True, tag=BEFORE_TAG, assets=("HeySleep_qa_2.0.0-qa.1056_200001056.apk", leftover))
    result, _, state, _ = publish_build(publish, tmp_path, directory)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [asset["name"] for asset in state["assets"]] == [APK]
    assert f"Deleted {leftover} of the build before." in result.stdout


def test_a_tag_that_cannot_be_moved_leaves_the_pre_release_as_it_was(publish, tmp_path):
    # A tag ruleset, or a token that may not move the tag: the step fails before it has changed
    # anything, and says what to do. Had the APK been uploaded first, every later build would add
    # another one next to a tag and notes that never follow.
    old = ("HeySleep_qa_2.0.0-qa.1056_200001056.apk",)
    directory = gh_directory(tmp_path, release=True, tag=BEFORE_TAG, assets=old, refuse_tag=True)
    result, calls, state, summary = publish_build(publish, tmp_path, directory)
    assert result.returncode == 1
    assert "::error title=The tag could not be moved::qa-develop was not moved to 3ff9b1a" in result.stdout
    assert "Nothing is published, and the pre-release is as it was." in result.stdout
    assert "pass a token that may as the secret RELEASE_PUSH_TOKEN" in result.stdout
    assert "git tag -f -a qa-develop -m '2.0.0-qa.1057 (200001057)' 3ff9b1a && git push -f origin qa-develop" in result.stdout
    assert "Repository rule violations found (HTTP 422)" in result.stderr
    assert state["tag"] == BEFORE_TAG and [asset["name"] for asset in state["assets"]] == list(old)
    assert state["history"] == [] and state["title"] == "" and summary == ""
    assert not [call for call in calls if call.startswith("release ")]
    # The same for the first build of a line, whose tag cannot be created.
    directory = gh_directory(tmp_path / "first", refuse_tag=True)
    result, calls, state, _ = publish_build(publish, tmp_path / "first", directory, SINCE="")
    assert result.returncode == 1
    assert "::error title=The tag could not be moved::" in result.stdout
    assert state["tag"] is None and not state["release"]


def test_an_upload_that_fails_puts_the_tag_back(publish, tmp_path):
    # The tag moved and the APK did not arrive. The tag goes back to the build the release still
    # holds, so the pre-release is whole and the next build lists its changes from there.
    old = ("HeySleep_qa_2.0.0-qa.1056_200001056.apk",)
    directory = gh_directory(tmp_path, release=True, tag=BEFORE_TAG, assets=old, fail_upload="1057")
    result, calls, state, summary = publish_build(publish, tmp_path, directory)
    assert result.returncode == 1
    assert "::error title=Not published::An APK could not be uploaded. qa-develop is back where it was, and the pre-release holds the build before." in result.stdout
    assert writes(calls)[-1] == f"api --method PATCH repos/acme/app/git/refs/tags/qa-develop -f sha={BEFORE_TAG} -F force=true"
    assert state["tag"] == BEFORE_TAG and [asset["name"] for asset in state["assets"]] == list(old)
    assert state["title"] == "" and summary == ""
    # The same when the release cannot be listed, before any upload.
    directory = gh_directory(tmp_path / "list", release=True, tag=BEFORE_TAG, assets=old, fail_assets=True)
    result, _, state, _ = publish_build(publish, tmp_path / "list", directory)
    assert result.returncode == 1
    assert "::error title=Not published::Cannot list the APKs of the pre-release. qa-develop is back where it was" in result.stdout
    assert state["tag"] == BEFORE_TAG and state["history"] == []


def test_what_was_uploaded_before_an_upload_failed_is_removed_again(publish, tmp_path):
    # Two APKs, and the second upload fails. The first one is in the release by then; left there, the
    # pre-release would offer an APK of this build next to a tag, a title and an APK of the one
    # before. Nothing the release held was touched yet, so the step can put everything back.
    old = ("SleepHub_qa_1.13.1-qa.56_101300056.apk", "SweetSleep_qa_1.13.1-qa.56_101300056.apk")
    apks = ("sleephub/release/SleepHub_qa_1.13.1-qa.57_101300057.apk", "sweetsleep/release/SweetSleep_qa_1.13.1-qa.57_101300057.apk")
    directory = gh_directory(tmp_path, release=True, tag=BEFORE_TAG, assets=old, fail_upload="SweetSleep_qa_1.13.1-qa.57")
    result, calls, state, summary = publish_build(publish, tmp_path, directory, apks=apks)
    assert result.returncode == 1
    assert "::error title=Not published::An APK could not be uploaded. qa-develop is back where it was, and the pre-release holds the build before." in result.stdout
    assert writes(calls)[-2:] == [
        "api --method DELETE repos/acme/app/releases/assets/3",
        f"api --method PATCH repos/acme/app/git/refs/tags/qa-develop -f sha={BEFORE_TAG} -F force=true",
    ]
    assert state["tag"] == BEFORE_TAG and [asset["name"] for asset in state["assets"]] == list(old)
    assert state["title"] == "" and summary == ""


def test_a_replacement_that_cannot_be_looked_up_is_undone_before_anything_is_deleted(publish, tmp_path):
    # The APK went up under another name, and the listing that would give its id fails. The APK of
    # the build before is still there: the upload is removed and the tag goes back.
    directory = gh_directory(tmp_path, release=True, tag=BEFORE_TAG, assets=("app-release.apk",), fail_assets=2)
    result, calls, state, _ = publish_build(publish, tmp_path, directory, apks=("app-release.apk",))
    assert result.returncode == 1
    assert "::error title=Not published::Cannot list the APKs of the pre-release after the upload. qa-develop is back where it was" in result.stdout
    assert state["tag"] == BEFORE_TAG and state["assets"] == [{"id": 1, "name": "app-release.apk"}]
    assert state["history"] == [["app-release.apk", "app-release.run-9001-1.apk"], ["app-release.apk"]]


def test_an_apk_that_cannot_take_its_name_stays_published_and_the_tag_stays_with_it(publish, tmp_path):
    # The old APK is deleted and the rename of the new one fails. The release holds this build,
    # under the name it was uploaded with. Putting the tag back now would leave a tag and a title
    # of the build before on a release that holds this one: the tag stays, title and notes follow,
    # and the job fails with what is left to do.
    directory = gh_directory(tmp_path, release=True, tag=BEFORE_TAG, assets=("app-release.apk", "leftover.apk"), fail_rename=True)
    result, calls, state, summary = publish_build(publish, tmp_path, directory, apks=("app-release.apk",))
    assert result.returncode == 1
    assert (
        "::error title=Published, but not completely::qa-develop is at 3ff9b1a and the pre-release holds 2.0.0-qa.1057, but "
        "app-release.apk is there as app-release.run-9001-1.apk, which could not be given its name. Run this job again, or "
        "leave it to the next build that is published: either puts it right."
    ) in result.stdout
    assert "Not published" not in result.stdout
    assert state["tag"] == FIRST_OBJECT and state["tags"][FIRST_OBJECT]["object"] == BUILT
    assert state["title"] == "QA develop: 2.0.0-qa.1057"
    # The APK of this build is kept under the name it has; what is older goes.
    assert [asset["name"] for asset in state["assets"]] == ["app-release.run-9001-1.apk"]
    assert [] not in state["history"]
    assert summary == ""

    # The job is run again (the artifact is still there after a failure): the name is free now.
    again = json.loads((directory / "state.json").read_text())
    again.update(fail_rename=False, history=[])
    (directory / "state.json").write_text(json.dumps(again))
    result, _, state, summary = publish_build(publish, tmp_path, directory, apks=("app-release.apk",), GITHUB_RUN_ATTEMPT="2", SINCE=BUILT, UNCHANGED="true")
    assert result.returncode == 0, result.stdout + result.stderr
    assert [asset["name"] for asset in state["assets"]] == ["app-release.apk"]
    assert [] not in state["history"]
    assert "Replaced the APK of the pre-release with app-release.apk" in summary


def test_an_old_apk_that_cannot_be_deleted_is_named_and_the_rest_is_done(publish, tmp_path):
    old = ("HeySleep_qa_2.0.0-qa.1056_200001056.apk", "HeySleep_qa_2.0.0-qa.1049_200001049.apk")
    directory = gh_directory(tmp_path, release=True, tag=BEFORE_TAG, assets=old, fail_delete="qa.1056")
    result, _, state, summary = publish_build(publish, tmp_path, directory)
    assert result.returncode == 1
    assert f"::error title=Published, but not completely::qa-develop is at 3ff9b1a and the pre-release holds 2.0.0-qa.1057, but {old[0]} of the build before could not be deleted. " in result.stdout
    assert state["tag"] == FIRST_OBJECT and state["title"] == "QA develop: 2.0.0-qa.1057"
    assert [asset["name"] for asset in state["assets"]] == [old[0], APK]
    assert f"Deleted {old[1]} of the build before." in result.stdout
    assert summary == ""
    # The same APK name, and the one the release holds cannot be deleted: both stay, the new one
    # under the name it was uploaded with, and the error says which is which.
    directory = gh_directory(tmp_path / "same", release=True, tag=BEFORE_TAG, assets=("app-release.apk",), fail_delete="app-release.apk")
    result, _, state, _ = publish_build(publish, tmp_path / "same", directory, apks=("app-release.apk",))
    assert result.returncode == 1
    assert "app-release.apk of the build before could not be deleted, and the new one is there as app-release.run-9001-1.apk" in result.stdout
    assert [asset["name"] for asset in state["assets"]] == ["app-release.apk", "app-release.run-9001-1.apk"]
    assert state["tag"] == FIRST_OBJECT


def test_a_build_whose_versioncode_another_pre_release_holds_is_not_published(publish, tmp_path):
    # Two label builds were planned while neither was published, and both were numbered 1 after the
    # release. The other one was published while this one was built. Devices tell builds apart by
    # the versionCode, so this one is not published under it; a new run takes the next number.
    labelled = {
        "TAG": "qa-develop-knox", "KIND": "label", "APART": "knox", "VERSION": "1.13.1", "VERSION_NAME": "1.13.1-knox.1",
        "VERSION_CODE": "101350001", "PREVIOUS_TAG": "v1.13.0", "SINCE": "v1.13.0",
    }
    directory = gh_directory(tmp_path)
    result, calls, state, summary = publish_build(publish, tmp_path, directory, CODE_TAKEN_BY="qa-develop-ble-test", **labelled)
    assert result.returncode == 1
    assert (
        "::error title=The versionCode is taken::1.13.1-knox.1 has the versionCode 101350001, and the pre-release "
        "qa-develop-ble-test holds a build of that versionCode: it was published while this one was built. Nothing is published. "
        "Start the workflow again with the same inputs (a new run, not a re-run of this one): it takes the next number."
    ) in result.stdout
    assert calls == [] and state["tag"] is None and summary == ""
    env = step(publish, "Publish the rolling pre-release")["env"]
    assert env["CODE_TAKEN_BY"] == "${{ steps.changes.outputs.code-taken-by }}"
    # Asked with the tags as the checkout of this job has them, which is made after the download.
    names = [item.get("name") or item.get("uses") for item in publish["steps"]]
    assert names.index("Download the signed APKs") < names.index("actions/checkout@v7") < names.index("List the changes since the build before")


def test_a_pre_release_that_cannot_be_created_leaves_no_tag_behind(publish, tmp_path):
    directory = gh_directory(tmp_path, fail_create=True)
    result, calls, state, _ = publish_build(publish, tmp_path, directory, SINCE="")
    assert result.returncode == 1
    assert "::error title=Not published::The pre-release could not be created. qa-develop is back where it was" in result.stdout
    assert writes(calls)[-1] == "api --method DELETE repos/acme/app/git/refs/tags/qa-develop"
    assert state["tag"] is None and not state["release"]


def test_a_tag_that_was_moved_since_the_changes_were_listed_is_not_moved_again(publish, tmp_path):
    # Only this job moves the tag, one run at a time. A tag that is elsewhere than the checkout of
    # this job had it was moved by hand or outside that order: the notes would list the wrong
    # changes, and the build might be an older one.
    directory = gh_directory(tmp_path, release=True, tag=BEFORE_TAG, assets=("old.apk",))
    result, calls, state, _ = publish_build(publish, tmp_path, directory, SINCE_OBJECT="1" * 40)
    assert result.returncode == 1
    assert f"::error title=The tag was moved meanwhile::qa-develop named {'1' * 40} when this job listed the changes and names {BEFORE_TAG} now." in result.stdout
    assert writes(calls) == [] and state["tag"] == BEFORE_TAG
    # A tag that appeared, and one that is gone.
    result, calls, _, _ = publish_build(publish, tmp_path / "new", gh_directory(tmp_path / "new", tag=BEFORE_TAG), SINCE_OBJECT="")
    assert result.returncode == 1 and "named nothing when this job listed the changes" in result.stdout and writes(calls) == []
    result, calls, _, _ = publish_build(publish, tmp_path / "gone", gh_directory(tmp_path / "gone"), SINCE_OBJECT=BEFORE_TAG)
    assert result.returncode == 1 and "and names nothing now" in result.stdout and writes(calls) == []
    env = step(publish, "Publish the rolling pre-release")["env"]
    assert env["SINCE_OBJECT"] == "${{ steps.changes.outputs.since-object }}"


def test_the_notes_state_the_build_and_what_it_adds(publish, tmp_path):
    directory = gh_directory(tmp_path, release=True, tag=BEFORE_TAG)
    result, _, state, _ = publish_build(publish, tmp_path, directory, PREVIOUS_TAG="v1.13.0")
    assert result.returncode == 0, result.stdout + result.stderr
    notes = state["notes"]
    assert notes.startswith("Rolling QA build of `develop`. Every build of it replaces the APK here. This is not a release.\n")
    row = re.search(r"^\| `2\.0\.0-qa\.1057` \| 200001057 \| (.*) \| (.*) \|$", notes, re.MULTILINE)
    assert row, notes
    assert row[1] == f"[`3ff9b1a`](https://github.invalid/acme/app/commit/{BUILT})"
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d UTC, \[run 57\]\(https://github.invalid/acme/app/actions/runs/9001\)", row[2])
    assert (
        "Leads up to `2.0.0`, after `v1.13.0`. It installs over the earlier QA builds of 2.0. Every preview and "
        "every release of 2.0 installs over it, and it over none of them." in notes
    )
    assert "#### Changes since the build before (`e8760a1`)\n\n" in notes
    # The titles are written as they are.
    assert notes.endswith("- #340 fix: Close the session\n- #341 -n 100% `code` $(not run) %s\n")


def test_the_notes_of_a_first_build_start_at_the_last_release_and_a_rebuild_says_so(publish, tmp_path):
    _, _, state, _ = publish_build(publish, tmp_path / "first", gh_directory(tmp_path / "first"), SINCE="v1.13.0", PRS="")
    assert "#### Changes since `v1.13.0`\n\nNothing listed.\n" in state["notes"]
    _, _, state, _ = publish_build(
        publish, tmp_path / "again", gh_directory(tmp_path / "again", release=True, tag=BEFORE_TAG), SINCE=BUILT, PRS="", UNCHANGED="true"
    )
    assert state["notes"].endswith("#### Changes\n\nThe same commit as the build before.\n")


def test_a_label_build_of_a_maintenance_branch_has_its_own_pre_release(publish, tmp_path):
    directory = gh_directory(tmp_path)
    labelled = {
        "TAG": "qa-release-1.x-ble-test", "BRANCH": "release/1.x", "LINE": "release-1.x", "KIND": "label", "APART": "ble-test",
        "VERSION": "1.13.2", "VERSION_NAME": "1.13.2-ble-test.3", "VERSION_CODE": "101350103", "PREVIOUS_TAG": "v1.13.1",
        "SINCE": "v1.13.1",
    }
    apk = "SweetSleep_label_1.13.2-ble-test.3_101350103.apk"
    result, calls, state, summary = publish_build(publish, tmp_path, directory, apks=(apk,), **labelled)
    assert result.returncode == 0, result.stdout + result.stderr
    assert writes(calls)[0] == (
        "api --method POST repos/acme/app/git/tags -f tag=qa-release-1.x-ble-test -f message=1.13.2-ble-test.3 (101350103) "
        f"-f object={BUILT} -f type=commit --jq .sha"
    )
    assert writes(calls)[1] == f"api --method POST repos/acme/app/git/refs -f ref=refs/tags/qa-release-1.x-ble-test -f sha={FIRST_OBJECT}"
    assert writes(calls)[2].startswith("release create qa-release-1.x-ble-test --verify-tag --title QA release-1.x ble-test: 1.13.2-ble-test.3 ")
    assert state["notes"].startswith("Label build `ble-test` of `release/1.x`: a named test build. Every build with this label replaces the APK here. ")
    assert "Made after `v1.13.1`. It installs over `v1.13.1` and over the label builds made after it before this one, and `1.13.2` installs over it." in state["notes"]
    assert summary.startswith("### Label build 1.13.2-ble-test.3\n")
    # What the tag says is what release-version counts the label builds from.
    message = state["tags"][FIRST_OBJECT]["message"]
    counted = release_version.ROLLING_MESSAGE.fullmatch(message)
    assert (counted["name"], counted["code"]) == ("1.13.2-ble-test.3", "101350103")


def test_a_build_of_a_version_that_was_given_is_kept_apart_under_that_version(publish, tmp_path):
    given = {"TAG": "qa-develop-1.14.0", "APART": "1.14.0", "VERSION": "1.14.0", "VERSION_NAME": "1.14.0-qa.57", "VERSION_CODE": "101400057"}
    result, calls, state, _ = publish_build(publish, tmp_path, gh_directory(tmp_path), SINCE="v1.13.0", **given)
    assert result.returncode == 0, result.stdout + result.stderr
    assert writes(calls)[2].startswith("release create qa-develop-1.14.0 --verify-tag --title QA develop 1.14.0: 1.14.0-qa.57 ")
    assert state["notes"].startswith("Rolling QA build of `develop`, of the version `1.14.0` that was given for it. ")


def test_every_apk_of_the_build_is_published(publish, tmp_path):
    apks = ("sleephub/release/SleepHub_qa_1.13.1-qa.57_101300057.apk", "sweetsleep/release/SweetSleep_qa_1.13.1-qa.57_101300057.apk")
    directory = gh_directory(tmp_path, release=True, tag=BEFORE_TAG, assets=("SweetSleep_qa_1.13.1-qa.57_101300057.apk", "old.apk"))
    result, calls, state, summary = publish_build(publish, tmp_path, directory, apks=apks)
    assert result.returncode == 0, result.stdout + result.stderr
    uploads = [call for call in calls if call.startswith("release upload")]
    assert uploads == [
        f"release upload qa-develop {tmp_path / 'apk' / apks[0]}",
        f"release upload qa-develop {tmp_path / 'temp' / 'android-qa-upload' / 'SweetSleep_qa_1.13.1-qa.57_101300057.run-9001-1.apk'} --clobber",
    ]
    assert sorted(asset["name"] for asset in state["assets"]) == ["SleepHub_qa_1.13.1-qa.57_101300057.apk", "SweetSleep_qa_1.13.1-qa.57_101300057.apk"]
    assert all(any(name.startswith("SweetSleep_qa_") for name in names) for names in state["history"])
    assert "with SleepHub_qa_1.13.1-qa.57_101300057.apk, SweetSleep_qa_1.13.1-qa.57_101300057.apk" in summary


def test_a_tag_whose_release_was_deleted_gets_its_release_again(publish, tmp_path):
    directory = gh_directory(tmp_path, tag=BEFORE_TAG)
    result, calls, state, _ = publish_build(publish, tmp_path, directory)
    assert result.returncode == 0, result.stdout + result.stderr
    assert writes(calls)[1] == f"api --method PATCH repos/acme/app/git/refs/tags/qa-develop -f sha={FIRST_OBJECT} -F force=true"
    assert writes(calls)[2].startswith("release create qa-develop --verify-tag ")
    assert state["release"] and state["tag"] == FIRST_OBJECT


def test_the_draft_a_deleted_tag_left_behind_is_removed(publish, tmp_path):
    # GitHub turns the release of a deleted tag into a draft, which is not found by its tag: the
    # next build creates the pre-release anew, and the draft with the old APK would stay.
    directory = gh_directory(tmp_path, drafts=[41, 42])
    result, calls, state, _ = publish_build(publish, tmp_path, directory, SINCE="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert [call for call in calls if "--method DELETE" in call] == [
        "api --method DELETE repos/acme/app/releases/41", "api --method DELETE repos/acme/app/releases/42",
    ]
    assert state["drafts"] == [] and state["release"]
    assert "Deleted the draft release 41 that qa-develop left behind when the tag was deleted." in result.stdout


def test_a_build_the_pre_release_is_past_is_not_published_and_does_not_fail(publish, tmp_path):
    # Every pushed commit is built, and two builds may finish in the other order, or an old run is
    # started again. The commit was built, which is what its check says; nothing is wrong, and the
    # artifact is deleted like that of a published build.
    directory = gh_directory(tmp_path, release=True, tag=BEFORE_TAG, assets=("newer.apk",))
    result, calls, state, summary = publish_build(publish, tmp_path, directory, STALE="true")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::notice title=A newer build is published::qa-develop is at e8760a1, a later commit that contains 3ff9b1a." in result.stdout
    assert "::error" not in result.stdout
    assert calls == [] and state["tag"] == BEFORE_TAG
    assert summary.startswith("### 2.0.0-qa.1057 is not published\n")
    assert "holds the build of `e8760a1`, a later commit that contains `3ff9b1a`" in summary


def test_publish_needs_an_apk(publish, tmp_path):
    result, calls, _, _ = publish_build(publish, tmp_path, gh_directory(tmp_path), apks=())
    assert result.returncode == 1
    assert "::error::The build artifact holds no APK." in result.stdout
    assert calls == []


def test_an_api_failure_is_not_read_as_no_release(publish, tmp_path):
    directory = gh_directory(tmp_path, release=True, tag=BEFORE_TAG, release_error="gh: Server Error (HTTP 502)")
    result, calls, _, _ = publish_build(publish, tmp_path, directory)
    assert result.returncode == 1
    assert "::error::Cannot tell whether release qa-develop exists: gh: Server Error (HTTP 502)" in result.stdout
    assert len(calls) == 1
    directory = gh_directory(tmp_path / "tag", release=True, tag=BEFORE_TAG, tag_error="gh: Bad credentials (HTTP 401)")
    result, calls, _, _ = publish_build(publish, tmp_path / "tag", directory)
    assert result.returncode == 1
    assert "::error::Cannot tell whether tag qa-develop exists" in result.stdout
    assert len(calls) == 2, "nothing is written"


def test_a_failed_edit_fails_the_job_after_the_old_apk_is_gone(publish, tmp_path):
    directory = gh_directory(tmp_path, release=True, tag=BEFORE_TAG, assets=("old.apk",), fail_edit=True)
    result, _, state, summary = publish_build(publish, tmp_path, directory)
    assert result.returncode == 1
    assert "::error title=Published, but not completely::qa-develop is at 3ff9b1a and the pre-release holds 2.0.0-qa.1057, but its title and notes are still those of the build before. " in result.stdout
    assert summary == ""
    # The rest is done all the same: the release holds this build alone, and the tag names its
    # commit. Only the text is behind, and the job can be run again.
    assert [asset["name"] for asset in state["assets"]] == [APK]
    assert state["tag"] == FIRST_OBJECT
    # The APKs of the build before cannot be listed: they stay, and the error says so.
    directory = gh_directory(tmp_path / "list", release=True, tag=BEFORE_TAG, assets=("old.apk",), fail_assets=2)
    result, _, state, _ = publish_build(publish, tmp_path / "list", directory)
    assert result.returncode == 1
    assert "but the APKs of the build before could not be listed and are still there. " in result.stdout
    assert [asset["name"] for asset in state["assets"]] == ["old.apk", APK]
    assert state["tag"] == FIRST_OBJECT and state["title"] == "QA develop: 2.0.0-qa.1057"


def test_a_build_on_the_fallback_runner_says_so(publish, tmp_path):
    result, _, _, summary = publish_build(publish, tmp_path, gh_directory(tmp_path), BUILT_ON="fallback", SINCE="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::warning title=Self-hosted runner problem::" in result.stdout
    assert "the GitHub-hosted runner, after the self-hosted build did not pass" in summary


def test_the_rolling_tag_is_never_a_release_tag(publish):
    # What the plan names and what publish writes: qa-..., which neither the release workflow's
    # trigger nor release-version's tag rule reads as a version.
    env = step(publish, "Publish the rolling pre-release")["env"]
    assert env["TAG"] == "${{ steps.names.outputs.tag }}"
    assert env["SHA"] == "${{ needs.plan.outputs.sha }}"
    for name, output in {"SINCE": "since", "PRS": "prs", "STALE": "stale", "UNCHANGED": "unchanged"}.items():
        assert env[name] == f"${{{{ steps.changes.outputs.{output} }}}}"
    assert 'tag="qa-$line${apart:+-$apart}"' in step(publish, "Name the build")["run"]
    source = (ROOT / "release-version" / "release_version.py").read_text()
    assert 'release_tag = f"qa-{line}' in source
    assert release_version.ROLLING_TAGS == "refs/tags/qa-*"
    example = yaml.safe_load((ROOT / "android-ci" / "release.yml.example").read_text())
    for pattern in example[True]["push"]["tags"]:
        assert pattern.startswith("v")


# --- publish: the artifact -------------------------------------------------------------------------


def delete_artifacts(publish, tmp_path, directory: Path, **overrides):
    env = {
        "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}",
        "GH_DIR": str(directory),
        "GH_REPO": "acme/app",
        "GH_TOKEN": "token",
        "RUN_ID": "9001",
        **overrides,
    }
    result = run_script(step(publish, "Delete the build artifact")["run"], env, tmp_path)
    return result, (directory / "calls").read_text().splitlines()


def test_the_artifact_is_deleted_once_the_apk_is_published(publish, tmp_path):
    directory = gh_directory(tmp_path, artifacts=[71, 72])
    result, calls = delete_artifacts(publish, tmp_path, directory)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == [
        "api --paginate repos/acme/app/actions/runs/9001/artifacts --jq "
        '.artifacts[] | select(.name | startswith("android-qa-apk-")) | .id',
        "api --method DELETE repos/acme/app/actions/artifacts/71",
        "api --method DELETE repos/acme/app/actions/artifacts/72",
    ]
    assert "Deleted artifact 71." in result.stdout and "Deleted artifact 72." in result.stdout


def test_an_artifact_that_cannot_be_deleted_is_a_warning_not_a_failure(publish, tmp_path):
    # The APK is published by then; the artifact goes after a day at the latest.
    directory = gh_directory(tmp_path, artifacts=[71, 72], undeletable=[71])
    result, _ = delete_artifacts(publish, tmp_path, directory)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::warning::Could not delete artifact 71. It is kept for one day." in result.stdout
    assert "Deleted artifact 72." in result.stdout
    result, calls = delete_artifacts(publish, tmp_path / "unlisted", gh_directory(tmp_path / "unlisted"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::warning::Cannot list the artifacts of this run." in result.stdout
    assert len(calls) == 1


def test_the_artifact_is_kept_only_for_a_run_that_can_be_finished(publish, checks):
    # Deleted by the job that is done with it. The publish job: the APK is published, or a newer
    # build is. The job that waits for the checks: they refused the commit, and the publish job
    # does not start for it. After any other failure, checks that could not be read included, the
    # artifact stays for the day GitHub keeps it, so the job can run again. (The artifact of a run
    # that is cancelled for a later commit is deleted by the run of that commit.)
    delete = step(publish, "Delete the build artifact")
    assert delete["if"] == "${{ always() && steps.publish.outcome == 'success' }}"
    assert step(publish, "Publish the rolling pre-release")["id"] == "publish"
    early = step(checks, "Delete the build artifact")
    assert early["if"] == "${{ always() && steps.checks.outputs.refused == 'true' }}"
    assert step(checks, "Check the commit's checks")["id"] == "checks"
    assert 'output.write("refused=true\\n")' in step(checks, "Check the commit's checks")["run"]
    # One script and one set of values in both jobs; both may delete an artifact.
    assert (early["run"], early["env"]) == (delete["run"], delete["env"])
    assert checks["permissions"]["actions"] == publish["permissions"]["actions"] == "write"
    upload = step(load(BUILD)["jobs"]["build"], "Upload the signed APKs")["with"]
    assert upload["retention-days"] == "${{ (inputs.kind == 'qa' || inputs.kind == 'label') && 1 || 90 }}"


# --- wiring ------------------------------------------------------------------------------------


def test_inputs_and_outputs(entry):
    call = trigger(entry)
    assert list(call["inputs"]) == [
        "runner-labels", "fallback-runs-on", "environment", "java-version", "java-distribution", "checkout-submodules",
        "local-properties-secrets", "required-secrets", "gradle-args", "apk-directory", "expected-cert-sha256",
        "build-tools-version", "sentry-upload", "source-branch", "version", "next-version", "label", "sequence-offset",
        "required-checks", "checks-timeout-minutes", "build-timeout-minutes", "qa-gradle-home",
        "self-hosted-max-workers", "self-hosted-gradle-jvmargs", "self-hosted-kotlin-daemon-jvmargs",
    ]
    for name in ("version", "next-version", "label", "source-branch"):
        assert call["inputs"][name]["default"] == "" and call["inputs"][name]["required"] is False, name
    assert call["inputs"]["sequence-offset"] == {**call["inputs"]["sequence-offset"], "type": "number", "default": 0}
    # What the build inputs share with the release workflow, they share to the letter.
    release = trigger(load(RELEASE))["inputs"]
    for name in set(call["inputs"]) & set(release):
        if name in ("environment", "source-branch", "required-checks"):
            continue
        assert call["inputs"][name]["default"] == release[name]["default"], name
        assert call["inputs"][name]["type"] == release[name]["type"], name
    assert list(call["outputs"]) == ["version-name", "version-code"]


def test_actions_are_referenced_at_the_workflows_tag():
    own = {}
    for line in re.findall(r"^\s*(?:- )?uses: (.+)$", (WORKFLOWS / ENTRY).read_text(), re.MULTILINE):
        match = OWN_ACTION.match(line)
        if match:
            assert (ROOT / match["name"] / "action.yml").is_file()
            own.setdefault(match["name"], set()).add(match["ref"])
        else:
            assert line.startswith(("actions/", "./.github/workflows/")), line
    assert own == {"release-version": {"android-ci/v1"}, "pick-runner": {"pick-runner/v1"}}


def test_the_example_passes_only_what_the_workflow_declares(entry):
    example = yaml.safe_load(EXAMPLE.read_text())
    # The job is named qa: the check a release requires by default is "qa / built".
    assert list(example["jobs"]) == ["qa"]
    job = example["jobs"]["qa"]
    assert job["uses"] == f"asleep-ai/actions/.github/workflows/{ENTRY}@android-ci/v1"
    assert set(job["with"]) <= set(trigger(entry)["inputs"])
    assert job["secrets"] == "inherit"
    # The called workflow cannot be given more than the caller has.
    assert example["permissions"] == {"contents": "write", "packages": "read", "checks": "read", "actions": "write"}
    levels = {"read": 1, "write": 2}
    for called in entry["jobs"].values():
        for scope, level in called.get("permissions", {}).items():
            assert levels[example["permissions"][scope]] >= levels[level], scope
    # Every push to a branch that is released from, and to nothing the plan would refuse: the
    # branches are the source branch and release/<major>.x. No path is left out, because a release
    # of the pushed commit requires the check of its build.
    assert example[True]["push"] == {"branches": ["develop", "release/*.x"]}
    dispatch = example[True]["workflow_dispatch"]["inputs"]
    assert list(dispatch) == ["label", "version"]
    for name in dispatch:
        assert job["with"][name] == f"${{{{ inputs.{name} }}}}"
        assert dispatch[name]["required"] is False
    # The QA Gradle home is off in a file that is copied as it is. The line that turns it on is
    # there, commented out, with where to read first.
    assert "qa-gradle-home" not in job["with"]
    assert "      # qa-gradle-home: warm\n" in EXAMPLE.read_text()
    # The calling file names no concurrency group. The called workflow cancels the runs of earlier
    # commits itself and keeps the order of what is published; a group here would cancel a run
    # that is publishing, or make the newest commit wait behind an older one.
    assert "concurrency" not in example
    usage = yaml.safe_load(README.read_text().split("## android-qa.yml", 1)[1].split("```yaml", 1)[1].split("```", 1)[0])
    assert "concurrency" not in usage
    assert usage["permissions"] == example["permissions"]
    assert usage[True]["push"] == example[True]["push"]
    assert list(usage["jobs"]) == ["qa"]
    shown = usage["jobs"]["qa"]
    assert shown["uses"] == job["uses"] and set(shown["with"]) <= set(trigger(entry)["inputs"])


def test_readme_lists_every_input_with_its_default(entry):
    section = README.read_text().split("## android-qa.yml", 1)[1].split("\n## ", 1)[0]
    inputs = trigger(entry)["inputs"]
    rows = re.findall(r"^\| `([a-z0-9-]+)` \| (.*?) \| ", section.split("### Inputs", 1)[1].split("###", 1)[0], re.MULTILINE)
    assert [name for name, _ in rows] == list(inputs), "the table follows the order of the workflow file"
    for name, shown in rows:
        default = inputs[name]["default"]
        if default == "":
            assert shown == "`''`", name
        elif isinstance(default, bool):
            assert shown == f"`{str(default).lower()}`", name
        else:
            assert shown == f"`{default}`", name
    for name in trigger(entry)["secrets"]:
        assert f"`{name}`" in section, name
