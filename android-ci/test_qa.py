"""Tests of android-qa.yml.

As in test_release.py, the run steps are executed as the shell GitHub would
run, here against stand-ins for gh and the check-runs API, and the wiring tests
pin what cannot be executed: job conditions, the calls of the build workflow
it shares with android-release.yml, and the copy of the checks script.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
import yaml

from test_pr_check import OWN_ACTION, ROOT, WORKFLOWS, executable, load, run_script, step, trigger
from test_version_bump import SHA, check_run, check_runs_api

ENTRY = "android-qa.yml"
BUILD = "android-release-build.yml"
RELEASE = "android-release.yml"
EXAMPLE = ROOT / "android-ci" / "qa.yml.example"
README = ROOT / "android-ci" / "README.md"


@pytest.fixture(scope="module")
def entry() -> dict:
    return load(ENTRY)


@pytest.fixture(scope="module")
def publish(entry) -> dict:
    return entry["jobs"]["publish"]


# --- plan --------------------------------------------------------------------------------------


def test_the_plan_is_the_qa_operation_of_release_version_for_the_ref_of_the_run(entry):
    plan = entry["jobs"]["plan"]
    names = [item.get("name") or item.get("uses") for item in plan["steps"]]
    assert names == ["actions/checkout@v7", "Plan the QA build", "Write the plan to the summary", "Pick the runner"]
    # Every tag and the whole history of the commit the run started for, and no token left behind.
    assert plan["steps"][0]["with"] == {"ref": "${{ github.sha }}", "fetch-depth": 0, "persist-credentials": False}
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
    # Read-only, and no signing secret is near: the build needs the plan.
    assert plan["permissions"] == {"contents": "read"}
    assert "environment" not in plan
    assert entry["jobs"]["primary"]["needs"] == "plan"
    outputs = plan["outputs"]
    for name, output in {
        "sha": "target-sha", "branch": "branch", "line": "line", "version": "version", "version-name": "version-name",
        "version-code": "version-code", "previous-tag": "previous-tag", "release-tag": "release-tag",
    }.items():
        assert outputs[name] == f"${{{{ steps.qa.outputs.{output} }}}}"
    pick = step(plan, "Pick the runner")
    assert pick["if"] == "${{ inputs.runner-labels != '' }}"
    assert pick["with"]["token"] == "${{ secrets.RUNNER_READ_TOKEN }}"


def test_every_output_the_plan_reads_is_one_the_action_has(entry):
    action = yaml.safe_load((ROOT / "release-version" / "action.yml").read_text())
    text = (WORKFLOWS / ENTRY).read_text()
    read = set(re.findall(r"steps\.(?:qa|changes)\.outputs\.([a-z-]+)", text))
    assert read and read <= set(action["outputs"])
    for name in ("Plan the QA build",):
        assert set(step(entry["jobs"]["plan"], name)["with"]) <= set(action["inputs"])
    changes = step(entry["jobs"]["publish"], "List the changes since the build before")
    assert set(changes["with"]) <= set(action["inputs"])


def plan_summary(entry, tmp_path, **overrides) -> str:
    summary = tmp_path / "summary"
    summary.write_text("")
    env = {
        "BRANCH": "develop",
        "SHA": "3ff9b1a3" + "0" * 32,
        "VERSION": "2.0.0",
        "VERSION_NAME": "2.0.0-develop.1057",
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
    assert text.startswith("### QA build 2.0.0-develop.1057\n")
    assert "- Branch: `develop`, commit [`3ff9b1a`](https://github.invalid/acme/app/commit/3ff9b1a3" in text
    assert "- Leads up to `2.0.0`, no release yet\n" in text
    assert "- versionCode: 200001057\n" in text
    assert "- Published as the pre-release `qa-develop` when the build and the commit's checks pass\n" in text
    assert "was given" not in text
    text = plan_summary(
        entry, tmp_path, BRANCH="release/1.x", VERSION="1.14.0", VERSION_NAME="1.14.0-ble-test.58",
        PREVIOUS_TAG="v1.13.1", RELEASE_TAG="qa-release-1.x-ble-test", VERSION_GIVEN="1.14.0",
    )
    assert "- Leads up to `1.14.0`, after `v1.13.1`\n" in text
    # A version for one run: the next build of the branch is numbered from the tags again.
    assert "- The version was given (`version`) for this run." in text and "may be a lower versionCode" in text


# --- the build -----------------------------------------------------------------------------------


def test_the_build_is_the_build_of_a_release_with_ci_qa(entry):
    inputs = trigger(entry)["inputs"]
    inner = trigger(load(BUILD))
    primary, fallback = entry["jobs"]["primary"], entry["jobs"]["fallback"]
    # One build workflow for both: the emptied work folder, the Gradle user home of the job, no cache,
    # the version read back from the APK and the signature check are the release's.
    release = load(RELEASE)["jobs"]
    assert primary["uses"] == fallback["uses"] == release["primary"]["uses"] == f"./.github/workflows/{BUILD}"
    assert primary["secrets"] == fallback["secrets"] == "inherit"
    assert set(primary["with"]) == set(fallback["with"]) == set(inner["inputs"])
    assert primary["with"]["attempt"] == "primary" and fallback["with"]["attempt"] == "fallback"
    assert primary["with"]["kind"] == fallback["with"]["kind"] == "qa"
    # The commit that was planned, by its id: a branch would be another commit after the next push.
    planned = {"ref": "sha", "sha": "sha", "version-name": "version-name", "version-code": "version-code"}
    for name, output in planned.items():
        assert primary["with"][name] == fallback["with"][name] == f"${{{{ needs.plan.outputs.{output} }}}}"
    forwarded = set(inner["inputs"]) - {"runs-on", "attempt", "kind", *planned}
    assert forwarded == set(inputs) - {
        "runner-labels", "fallback-runs-on", "source-branch", "version", "next-version", "label", "sequence-offset",
        "required-checks", "checks-timeout-minutes",
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


def test_no_secret_is_required_by_the_declaration(entry):
    secrets = trigger(entry)["secrets"]
    assert list(secrets) == [
        "KEYSTORE_BASE64", "KEYSTORE_PASSWORD", "KEY_ALIAS", "KEY_PASSWORD", "RUNNER_READ_TOKEN", "GIT_TOKEN",
        "SENTRY_AUTH_TOKEN",
    ]
    assert all(secret["required"] is False for secret in secrets.values())
    assert set(re.findall(r"secrets\.([A-Z0-9_]+)", (WORKFLOWS / ENTRY).read_text())) == {"RUNNER_READ_TOKEN"}


# --- publish: the checks of the commit ------------------------------------------------------------


def test_publish_follows_the_build_that_passed_and_is_the_only_job_that_writes(entry, publish):
    release = load(RELEASE)["jobs"]["publish"]
    assert publish["needs"] == ["plan", "primary", "fallback"]
    assert publish["if"] == release["if"]
    assert publish["runs-on"] == "ubuntu-latest"
    # The release, the checks of the commit, and the artifact it deletes.
    assert publish["permissions"] == {"contents": "write", "checks": "read", "actions": "write"}
    assert entry["permissions"] == {"contents": "read", "packages": "read"}
    names = [item.get("name") or item.get("uses") for item in publish["steps"]]
    assert names == [
        "Check the commit's checks",
        "actions/checkout@v7",
        "List the changes since the build before",
        "Download the signed APKs",
        "Publish the rolling pre-release",
        "Delete the build artifact",
    ]
    # Nothing is downloaded or published for a commit whose checks did not pass: no step after the
    # first runs with always() but the one that deletes the artifact.
    for item in publish["steps"][:5]:
        assert "if" not in item, item
    assert publish["steps"][1]["with"] == {
        "ref": "${{ needs.plan.outputs.sha }}", "fetch-depth": 0, "persist-credentials": False,
    }
    download = step(publish, "Download the signed APKs")["with"]
    assert download["name"] == "android-qa-apk-${{ needs.primary.result == 'success' && 'primary' || 'fallback' }}"
    # The name the build workflow uploads under for this kind.
    upload = step(load(BUILD)["jobs"]["build"], "Upload the signed APKs")["with"]["name"]
    assert upload.replace("${{ inputs.kind }}", "qa").replace("${{ inputs.attempt }}", "primary") == "android-qa-apk-primary"
    changes = step(publish, "List the changes since the build before")
    assert changes["with"] == {
        "operation": "changes",
        "sha": "${{ needs.plan.outputs.sha }}",
        "since": "${{ needs.plan.outputs.release-tag }}",
    }
    # The job waits for the checks, so its own timeout has to leave room for the longest wait.
    assert publish["timeout-minutes"] == 120
    assert "At most 100." in trigger(entry)["inputs"]["checks-timeout-minutes"]["description"]


def test_the_checks_are_decided_by_the_script_of_the_version_bump(entry, publish):
    # One rule in three workflows: an APK is handed out for a commit that passed what a release of
    # that commit would require. Only the wait is this workflow's.
    ours = step(publish, "Check the commit's checks")
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
    # A release is decided from the checks as they are.
    assert "CHECKS_WAIT_MINUTES" not in bump["env"] and "CHECKS_WAIT_MINUTES" not in release["env"]
    inputs = trigger(entry)["inputs"]
    assert inputs["required-checks"]["default"] == trigger(load(RELEASE))["inputs"]["required-checks"]["default"]
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


def wait_for_checks(publish, tmp_path, api, minutes="1", required="pr-check / result"):
    summary = tmp_path / "summary"
    summary.write_text("")
    env = {
        "TOKEN": "job-token",
        "TARGET_SHA": SHA,
        "REQUIRED_CHECKS": required,
        "CHECKS_WAIT_MINUTES": minutes,
        "CHECKS_POLL_SECONDS": "0.01",
        "GITHUB_API_URL": f"http://127.0.0.1:{api.server_port}",
        "GITHUB_REPOSITORY": "acme/app",
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    result = run_script(step(publish, "Check the commit's checks")["run"], env, tmp_path)
    return result, summary.read_text()


def test_publish_waits_for_a_check_that_has_not_finished(publish, tmp_path):
    # The push started the checks and this build together. First the check does not exist on the
    # commit, then it is queued, then it runs, then it has passed.
    with check_runs_api() as api:
        api.runs = Answers(
            [],
            [check_run(conclusion=None, status="queued")],
            [check_run(conclusion=None, status="in_progress")],
            [check_run()],
        )
        result, summary = wait_for_checks(publish, tmp_path, api)
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(api.requests) == 4
    assert "Waiting for pr-check / result ('pr-check / result' did not run on the commit)." in result.stdout
    assert "Waiting for pr-check / result ('pr-check / result' is in_progress)." in result.stdout
    assert "| pr-check / result | [success](https://github.invalid/runs/1) |" in summary
    assert "::error" not in result.stdout


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped"])
def test_a_check_that_fails_ends_the_wait_and_nothing_is_published(publish, tmp_path, conclusion):
    with check_runs_api() as api:
        api.runs = Answers([check_run(conclusion=None, status="in_progress")], [check_run(conclusion=conclusion)])
        result, summary = wait_for_checks(publish, tmp_path, api, minutes="100")
        assert len(api.requests) == 2, "the wait ends with the first answer that is final"
    assert result.returncode == 1
    assert f"::error title=The commit has not passed its checks::{SHA[:7]}: 'pr-check / result' is {conclusion}. " in result.stdout
    assert "after waiting" not in result.stdout
    assert f"[{conclusion}]" in summary


def test_one_failed_check_is_final_while_another_still_runs(publish, tmp_path):
    with check_runs_api() as api:
        api.runs = [check_run(conclusion="failure"), check_run(name="lint", conclusion=None, status="in_progress")]
        result, _ = wait_for_checks(publish, tmp_path, api, minutes="100", required="pr-check / result\nlint")
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
def test_a_check_that_never_finishes_fails_the_job_after_the_wait(publish, tmp_path, runs, state):
    with check_runs_api() as api:
        api.runs = runs
        # A fraction of a minute, so that the test does not take one.
        result, _ = wait_for_checks(publish, tmp_path, api, minutes="0.002")
        assert len(api.requests) >= 2
    assert result.returncode == 1
    assert f"::error title=The commit has not passed its checks::{SHA[:7]}: {state} after waiting 0.002 minutes. " in result.stdout


def test_a_check_that_already_passed_is_not_waited_for(publish, tmp_path):
    with check_runs_api() as api:
        api.runs = [check_run()]
        result, _ = wait_for_checks(publish, tmp_path, api)
        assert (result.returncode, len(api.requests)) == (0, 1)
    assert "Waiting" not in result.stdout


def test_the_wait_has_to_be_a_number(publish, tmp_path):
    with check_runs_api() as api:
        result, _ = wait_for_checks(publish, tmp_path, api, minutes="soon")
        assert api.requests == []
    assert result.returncode == 1
    assert "The time to wait for the checks is not a number." in result.stderr


# --- publish: the rolling pre-release ------------------------------------------------------------

GH_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_DIR/calls"
case "$*" in
  "api repos/acme/app/releases/tags/"*" --jq .tag_name")
    if [ -f "$GH_DIR/error" ]; then cat "$GH_DIR/error" >&2; exit 1; fi
    if [ ! -f "$GH_DIR/release" ]; then echo "gh: Not Found (HTTP 404)" >&2; exit 1; fi
    cat "$GH_DIR/release" ;;
  "api repos/acme/app/git/ref/tags/"*)
    if [ -f "$GH_DIR/tag-error" ]; then cat "$GH_DIR/tag-error" >&2; exit 1; fi
    if [ ! -f "$GH_DIR/tag" ]; then echo "gh: Not Found (HTTP 404)" >&2; exit 1; fi
    cat "$GH_DIR/tag" ;;
  "api repos/acme/app/releases/tags/"*" --jq .assets[].name")
    cat "$GH_DIR/assets" ;;
  "api --paginate repos/acme/app/actions/runs/"*)
    if [ ! -f "$GH_DIR/artifacts" ]; then echo "gh: Resource not accessible by integration (HTTP 403)" >&2; exit 1; fi
    cat "$GH_DIR/artifacts" ;;
  "api --method DELETE repos/acme/app/actions/artifacts/"*)
    case " ${GH_UNDELETABLE:-} " in *" ${4##*/} "*) echo "gh: Forbidden (HTTP 403)" >&2; exit 1 ;; esac ;;
  "api --method PATCH "*|"api --method POST "*|"release upload "*|"release create "*|"release edit "*|"release delete-asset "*)
    echo "gh output that must not reach the summary"
    exit "${GH_EXIT:-0}" ;;
  *)
    echo "unexpected gh call: $*" >&2; exit 97 ;;
esac
"""
BUILT = "3ff9b1a3" + "0" * 32
BEFORE = "e8760a1c" + "1" * 32


def gh_directory(tmp_path: Path, release=False, tag=False, assets=()) -> Path:
    """A stand-in for gh: whether the rolling pre-release and its tag exist, and the APKs it holds."""
    directory = tmp_path / "gh"
    directory.mkdir(parents=True)
    executable(directory / "gh", GH_STUB)
    (directory / "calls").write_text("")
    if release:
        (directory / "release").write_text("qa-develop\n")
    if tag:
        (directory / "tag").write_text(BEFORE + "\n")
    (directory / "assets").write_text("".join(f"{name}\n" for name in assets))
    return directory


def publish_build(publish, tmp_path, directory: Path, apks=("HeySleep_release_2.0.0-develop.1057_200001057.apk",), **overrides):
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
        "TAG": "qa-develop",
        "SHA": BUILT,
        "BRANCH": "develop",
        "LINE": "develop",
        "LABEL": "",
        "VERSION": "2.0.0",
        "VERSION_NAME": "2.0.0-develop.1057",
        "VERSION_CODE": "200001057",
        "PREVIOUS_TAG": "",
        "SINCE": BEFORE,
        "PRS": "- #340 fix: Close the session\n- #341 -n 100% `code` $(not run) %s",
        "STALE": "false",
        "UNCHANGED": "false",
        "APK_DIRECTORY": str(apk_directory),
        "NOTES_FILE": str(temp / "android-qa-notes.md"),
        "BUILT_ON": "primary",
        "SELF_HOSTED": "true",
        "GITHUB_SERVER_URL": "https://github.invalid",
        "GITHUB_RUN_NUMBER": "57",
        "GITHUB_RUN_ID": "9001",
        "GITHUB_STEP_SUMMARY": str(summary),
        **overrides,
    }
    result = run_script(step(publish, "Publish the rolling pre-release")["run"], env, tmp_path)
    calls = (directory / "calls").read_text().splitlines()
    notes = temp / "android-qa-notes.md"
    return result, calls, notes.read_text() if notes.exists() else "", summary.read_text()


def test_the_first_build_of_a_line_creates_its_pre_release(publish, tmp_path):
    directory = gh_directory(tmp_path)
    result, calls, notes, summary = publish_build(publish, tmp_path, directory, SINCE="", PRS="- #1 Feature")
    assert result.returncode == 0, result.stdout + result.stderr
    apk = tmp_path / "apk" / "HeySleep_release_2.0.0-develop.1057_200001057.apk"
    notes_file = tmp_path / "temp" / "android-qa-notes.md"
    assert calls == [
        "api repos/acme/app/releases/tags/qa-develop --jq .tag_name",
        "api repos/acme/app/git/ref/tags/qa-develop --jq .object.sha",
        # The tag first, at the commit that was built; then the release of that tag and no other.
        f"api --method POST repos/acme/app/git/refs -f ref=refs/tags/qa-develop -f sha={BUILT}",
        "release create qa-develop --verify-tag --title QA develop: 2.0.0-develop.1057 "
        f"--notes-file {notes_file} --prerelease --latest=false {apk}",
    ]
    assert "Created the pre-release with HeySleep_release_2.0.0-develop.1057_200001057.apk: <https://github.invalid/acme/app/releases/tag/qa-develop>" in summary
    assert "| 2.0.0-develop.1057 | 200001057 | qa-develop | a self-hosted runner |" in summary
    assert "gh output" not in summary
    # No build and no release before it: the whole history of the repository is not listed.
    assert notes.endswith("#### Changes\n\nThe first build of this line, with no release before it to compare with.\n")
    assert "#1 Feature" not in notes


def test_every_later_build_replaces_the_apk_and_moves_the_tag(publish, tmp_path):
    old = ("HeySleep_release_2.0.0-develop.1056_200001056.apk", "HeySleep_release_2.0.0-develop.1049_200001049.apk")
    directory = gh_directory(tmp_path, release=True, tag=True, assets=old + ("HeySleep_release_2.0.0-develop.1057_200001057.apk",))
    result, calls, notes, summary = publish_build(publish, tmp_path, directory)
    assert result.returncode == 0, result.stdout + result.stderr
    apk = tmp_path / "apk" / "HeySleep_release_2.0.0-develop.1057_200001057.apk"
    notes_file = tmp_path / "temp" / "android-qa-notes.md"
    assert calls == [
        "api repos/acme/app/releases/tags/qa-develop --jq .tag_name",
        "api repos/acme/app/git/ref/tags/qa-develop --jq .object.sha",
        # The new APK is there before anything is removed: a download works at every moment.
        f"release upload qa-develop {apk} --clobber",
        f"api --method PATCH repos/acme/app/git/refs/tags/qa-develop -f sha={BUILT} -F force=true",
        f"release edit qa-develop --title QA develop: 2.0.0-develop.1057 --notes-file {notes_file} --prerelease --latest=false",
        "api repos/acme/app/releases/tags/qa-develop --jq .assets[].name",
        # Only the newest build is kept, so the releases do not grow with every push.
        "release delete-asset qa-develop HeySleep_release_2.0.0-develop.1056_200001056.apk --yes",
        "release delete-asset qa-develop HeySleep_release_2.0.0-develop.1049_200001049.apk --yes",
    ]
    assert "Replaced the APK of the pre-release with HeySleep_release_2.0.0-develop.1057_200001057.apk" in summary
    assert "Deleted HeySleep_release_2.0.0-develop.1056_200001056.apk of the build before." in result.stdout


def test_the_notes_state_the_build_and_what_it_adds(publish, tmp_path):
    directory = gh_directory(tmp_path, release=True, tag=True)
    result, _, notes, _ = publish_build(publish, tmp_path, directory, PREVIOUS_TAG="v1.13.0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert notes.startswith("Rolling QA build of `develop`. Every build of it replaces the APK here. This is not a release.\n")
    row = re.search(r"^\| `2\.0\.0-develop\.1057` \| 200001057 \| (.*) \| (.*) \|$", notes, re.MULTILINE)
    assert row, notes
    assert row[1] == f"[`3ff9b1a`](https://github.invalid/acme/app/commit/{BUILT})"
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d UTC, \[run 57\]\(https://github.invalid/acme/app/actions/runs/9001\)", row[2])
    assert "Leads up to `2.0.0`, after `v1.13.0`. It installs over an earlier QA build of 2.0, and a preview or a release of 2.0 installs over it." in notes
    assert "#### Changes since the build before (`e8760a1`)\n\n" in notes
    # The titles are written as they are.
    assert notes.endswith("- #340 fix: Close the session\n- #341 -n 100% `code` $(not run) %s\n")


def test_the_notes_of_a_first_build_start_at_the_last_release_and_a_rebuild_says_so(publish, tmp_path):
    _, _, notes, _ = publish_build(publish, tmp_path / "first", gh_directory(tmp_path / "first"), SINCE="v1.13.0", PRS="")
    assert "#### Changes since `v1.13.0`\n\nNothing listed.\n" in notes
    _, _, notes, _ = publish_build(
        publish, tmp_path / "again", gh_directory(tmp_path / "again", release=True, tag=True), SINCE=BUILT, PRS="", UNCHANGED="true"
    )
    assert notes.endswith("#### Changes\n\nThe same commit as the build before.\n")


def test_a_labelled_build_of_a_maintenance_branch_has_its_own_pre_release(publish, tmp_path):
    directory = gh_directory(tmp_path)
    labelled = {
        "TAG": "qa-release-1.x-ble-test", "BRANCH": "release/1.x", "LINE": "release-1.x", "LABEL": "ble-test",
        "VERSION": "1.13.2", "VERSION_NAME": "1.13.2-ble-test.58", "VERSION_CODE": "101300058", "SINCE": "v1.13.1",
    }
    result, calls, notes, _ = publish_build(publish, tmp_path, directory, apks=("SweetSleep_release_1.13.2-ble-test.58_101300058.apk",), **labelled)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls[2] == f"api --method POST repos/acme/app/git/refs -f ref=refs/tags/qa-release-1.x-ble-test -f sha={BUILT}"
    assert calls[3].startswith("release create qa-release-1.x-ble-test --verify-tag --title QA release-1.x ble-test: 1.13.2-ble-test.58 ")
    assert notes.startswith("Rolling QA build of `release/1.x`, labelled `ble-test`. ")
    assert "an earlier QA build of 1.13" in notes


def test_every_apk_of_the_build_is_published(publish, tmp_path):
    apks = ("sleephub/release/SleepHub_release_1.13.1-develop.57_101300057.apk", "sweet sleep/release/SweetSleep_release_1.13.1-develop.57_101300057.apk")
    directory = gh_directory(tmp_path, release=True, tag=True, assets=("SweetSleep_release_1.13.1-develop.57_101300057.apk", "old.apk"))
    result, calls, _, summary = publish_build(publish, tmp_path, directory, apks=apks)
    assert result.returncode == 0, result.stdout + result.stderr
    first, second = (str(tmp_path / "apk" / name) for name in apks)
    assert calls[2] == f"release upload qa-develop {first} {second} --clobber"
    assert [call for call in calls if call.startswith("release delete-asset")] == ["release delete-asset qa-develop old.apk --yes"]
    assert "with SleepHub_release_1.13.1-develop.57_101300057.apk, SweetSleep_release_1.13.1-develop.57_101300057.apk" in summary


def test_a_release_without_its_tag_gets_the_tag_back(publish, tmp_path):
    # Someone deleted the tag: it is created at the commit, and the release is updated as usual.
    directory = gh_directory(tmp_path, release=True)
    result, calls, _, _ = publish_build(publish, tmp_path, directory)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls[3] == f"api --method POST repos/acme/app/git/refs -f ref=refs/tags/qa-develop -f sha={BUILT}"
    # And a tag whose release was deleted is moved before the release is created for it.
    directory = gh_directory(tmp_path / "tag", tag=True)
    result, calls, _, _ = publish_build(publish, tmp_path / "tag", directory)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls[2] == f"api --method PATCH repos/acme/app/git/refs/tags/qa-develop -f sha={BUILT} -F force=true"
    assert calls[3].startswith("release create qa-develop --verify-tag ")


def test_an_older_build_is_not_published_over_a_newer_one(publish, tmp_path):
    directory = gh_directory(tmp_path, release=True, tag=True)
    result, calls, _, summary = publish_build(publish, tmp_path, directory, STALE="true")
    assert result.returncode == 1
    assert "::error title=A newer build is published::qa-develop is at e8760a1, a later commit that contains 3ff9b1a." in result.stdout
    assert calls == [] and summary == ""


def test_publish_needs_an_apk(publish, tmp_path):
    result, calls, _, _ = publish_build(publish, tmp_path, gh_directory(tmp_path), apks=())
    assert result.returncode == 1
    assert "::error::The build artifact holds no APK." in result.stdout
    assert calls == []


def test_an_api_failure_is_not_read_as_no_release(publish, tmp_path):
    directory = gh_directory(tmp_path, release=True, tag=True)
    (directory / "error").write_text("gh: Server Error (HTTP 502)\n")
    result, calls, _, _ = publish_build(publish, tmp_path, directory)
    assert result.returncode == 1
    assert "::error::Cannot tell whether release qa-develop exists: gh: Server Error (HTTP 502)" in result.stdout
    assert len(calls) == 1
    directory = gh_directory(tmp_path / "tag", release=True, tag=True)
    (directory / "tag-error").write_text("gh: Bad credentials (HTTP 401)\n")
    result, calls, _, _ = publish_build(publish, tmp_path / "tag", directory)
    assert result.returncode == 1
    assert "::error::Cannot tell whether tag qa-develop exists" in result.stdout
    assert len(calls) == 2, "nothing is written"


def test_a_failed_write_fails_the_job(publish, tmp_path):
    result, _, _, summary = publish_build(publish, tmp_path, gh_directory(tmp_path, release=True, tag=True), GH_EXIT="1")
    assert result.returncode != 0
    assert summary == ""


def test_a_build_on_the_fallback_runner_says_so(publish, tmp_path):
    result, _, _, summary = publish_build(publish, tmp_path, gh_directory(tmp_path), BUILT_ON="fallback")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::warning title=Self-hosted runner problem::" in result.stdout
    assert "the GitHub-hosted runner, after the self-hosted build did not pass" in summary


def test_the_rolling_tag_is_never_a_release_tag(publish):
    # What the plan names and what publish writes: qa-..., which neither the release workflow's
    # trigger nor release-version's tag rule reads as a version.
    env = step(publish, "Publish the rolling pre-release")["env"]
    assert env["TAG"] == "${{ needs.plan.outputs.release-tag }}"
    assert env["SHA"] == "${{ needs.plan.outputs.sha }}"
    for name, output in {"SINCE": "since", "PRS": "prs", "STALE": "stale", "UNCHANGED": "unchanged"}.items():
        assert env[name] == f"${{{{ steps.changes.outputs.{output} }}}}"
    source = (ROOT / "release-version" / "release_version.py").read_text()
    assert 'release_tag = f"qa-{line}"' in source
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
    directory = gh_directory(tmp_path)
    (directory / "artifacts").write_text("71\n72\n")
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
    directory = gh_directory(tmp_path)
    (directory / "artifacts").write_text("71\n72\n")
    result, _ = delete_artifacts(publish, tmp_path, directory, GH_UNDELETABLE="71")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::warning::Could not delete artifact 71. It is kept for one day." in result.stdout
    assert "Deleted artifact 72." in result.stdout
    result, calls = delete_artifacts(publish, tmp_path / "unlisted", gh_directory(tmp_path / "unlisted"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::warning::Cannot list the artifacts of this run." in result.stdout
    assert len(calls) == 1


def test_the_artifact_is_kept_only_for_a_run_that_can_be_finished(publish):
    delete = step(publish, "Delete the build artifact")
    # Deleted when the APK is published, and when the checks refused the commit: that APK is kept
    # nowhere. After any other failure it stays for the day GitHub keeps it, so the job can run again.
    assert delete["if"] == "${{ always() && (steps.publish.outcome == 'success' || steps.checks.outcome == 'failure') }}"
    assert step(publish, "Publish the rolling pre-release")["id"] == "publish"
    assert step(publish, "Check the commit's checks")["id"] == "checks"
    upload = step(load(BUILD)["jobs"]["build"], "Upload the signed APKs")["with"]
    assert upload["retention-days"] == "${{ inputs.kind == 'qa' && 1 || 90 }}"


# --- wiring ------------------------------------------------------------------------------------


def test_inputs_and_outputs(entry):
    call = trigger(entry)
    assert list(call["inputs"]) == [
        "runner-labels", "fallback-runs-on", "environment", "java-version", "java-distribution", "checkout-submodules",
        "local-properties-secrets", "required-secrets", "gradle-args", "apk-directory", "expected-cert-sha256",
        "build-tools-version", "sentry-upload", "source-branch", "version", "next-version", "label", "sequence-offset",
        "required-checks", "checks-timeout-minutes", "build-timeout-minutes", "self-hosted-max-workers",
        "self-hosted-gradle-jvmargs", "self-hosted-kotlin-daemon-jvmargs",
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
    assert {name: output["value"] for name, output in call["outputs"].items()} == {
        "version-name": "${{ jobs.plan.outputs.version-name }}",
        "version-code": "${{ jobs.plan.outputs.version-code }}",
        "release-tag": "${{ jobs.plan.outputs.release-tag }}",
    }


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
    (job,) = example["jobs"].values()
    assert job["uses"] == f"asleep-ai/actions/.github/workflows/{ENTRY}@android-ci/v1"
    assert set(job["with"]) <= set(trigger(entry)["inputs"])
    assert job["secrets"] == "inherit"
    # The called workflow cannot be given more than the caller has.
    assert example["permissions"] == {"contents": "write", "packages": "read", "checks": "read", "actions": "write"}
    levels = {"read": 1, "write": 2}
    for called in entry["jobs"].values():
        for scope, level in called.get("permissions", {}).items():
            assert levels[example["permissions"][scope]] >= levels[level], scope
    # Every push to a branch that is released from, and a manual run with the two inputs of one build.
    assert example[True]["push"] == {"branches": ["develop", "release/**"]}
    dispatch = example[True]["workflow_dispatch"]["inputs"]
    assert list(dispatch) == ["version", "label"]
    for name in dispatch:
        assert job["with"][name] == f"${{{{ inputs.{name} }}}}"
        assert dispatch[name]["required"] is False
    # The newest commit of a line wins, and a labelled build is a line of its own.
    assert example["concurrency"] == {"group": "qa-${{ github.ref }}-${{ inputs.label }}", "cancel-in-progress": True}
    usage = yaml.safe_load(README.read_text().split("## android-qa.yml", 1)[1].split("```yaml", 1)[1].split("```", 1)[0])
    assert usage["concurrency"] == example["concurrency"]
    assert usage["permissions"] == example["permissions"]
    assert usage[True]["push"] == example[True]["push"]
    (shown,) = usage["jobs"].values()
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
