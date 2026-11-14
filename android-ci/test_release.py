"""Tests of android-release.yml and android-release-build.yml.

As in test_pr_check.py, the run steps are executed as the shell GitHub would
run, here against stand-ins for gh, gradlew and aapt2, and the wiring tests pin
what cannot be executed: job conditions, the order of the steps that handle
secrets, and the two calls of the build workflow.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
import yaml

from test_pr_check import OWN_ACTION, ROOT, WORKFLOWS, executable, load, run_script, step, trigger

ENTRY = "android-release.yml"
BUILD = "android-release-build.yml"
EXAMPLE = ROOT / "android-ci" / "release.yml.example"
README = ROOT / "android-ci" / "README.md"
SIGNING = {
    "KEYSTORE_BASE64": "a2V5c3RvcmU=",
    "KEYSTORE_PASSWORD": "store-pass",
    "KEY_ALIAS": "upload-key",
    "KEY_PASSWORD": "key-pass",
}


@pytest.fixture(scope="module")
def entry() -> dict:
    return load(ENTRY)


@pytest.fixture(scope="module")
def build() -> dict:
    return load(BUILD)["jobs"]["build"]


def step_by_id(job: dict, identifier: str) -> dict:
    (found,) = [item for item in job["steps"] if item.get("id") == identifier]
    return found


# --- plan: the tag ---------------------------------------------------------------

GH_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_DIR/calls"
case "$*" in
  "api --paginate repos/acme/app/git/matching-refs/tags/v --jq .[].ref | ltrimstr(\\"refs/tags/\\")")
    cat "$GH_DIR/tags" ;;
  "api --paginate repos/acme/app/releases --jq .[] | select(.draft | not) | .tag_name")
    if [ -f "$GH_DIR/release-error" ]; then cat "$GH_DIR/release-error" >&2; exit 1; fi
    cat "$GH_DIR/released" ;;
  "api repos/acme/app/commits/tags/"*)
    cat "$GH_DIR/sha" ;;
  "api repos/acme/app/releases/tags/"*)
    if [ -f "$GH_DIR/release-error" ]; then cat "$GH_DIR/release-error" >&2; exit 1; fi
    if [ ! -f "$GH_DIR/release" ]; then echo "gh: Not Found (HTTP 404)" >&2; exit 1; fi
    cat "$GH_DIR/release" ;;
  "release upload "*|"release create "*|"workflow run "*)
    exit "${GH_EXIT:-0}" ;;
  *)
    echo "unexpected gh call: $*" >&2; exit 97 ;;
esac
"""
SHA = "1f" * 20


def gh_directory(tmp_path: Path, tags: list[str], released=(), release_exists=False) -> Path:
    """A stand-in for gh: the tags, the tags with a published release, and whether the release asked for
    by tag exists."""
    directory = tmp_path / "gh"
    directory.mkdir(parents=True)
    executable(directory / "gh", GH_STUB)
    (directory / "tags").write_text("".join(f"{tag}\n" for tag in tags))
    (directory / "sha").write_text(SHA + "\n")
    (directory / "calls").write_text("")
    (directory / "released").write_text("".join(f"{tag}\n" for tag in released))
    if release_exists:
        (directory / "release").write_text("v0\n")
    return directory


def check_tag(entry, tmp_path, tag: str, directory: Path):
    output = tmp_path / "output"
    output.write_text("")
    env = {
        "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}",
        "GH_DIR": str(directory),
        "GH_REPO": "acme/app",
        "GH_TOKEN": "token",
        "TAG": tag,
        "GITHUB_OUTPUT": str(output),
    }
    result = run_script(step_by_id(entry["jobs"]["plan"], "tag")["run"], env, tmp_path)
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    return result, outputs


TAGS = ["v1.9.0", "v1.10.0", "v1.10.1", "v1.10.1-qa.3", "v1.11.2-handok-reset-off.84", "v2", "vnext"]
RELEASED = ["v1.11.2-handok-reset-off.84", "v1.10.0", "v1.9.0"]
# Two lines: 1.x on its maintenance branch, 2.x on the main line.
LINE_TAGS = ["v1.13.0", "v1.13.1", "v1.13.2", "v2.0.0", "v2.0.1"]


def test_a_new_release_of_the_highest_tag_is_planned(entry, tmp_path):
    directory = gh_directory(tmp_path, TAGS, released=RELEASED)
    result, outputs = check_tag(entry, tmp_path, "v1.10.1", directory)
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"tag": "v1.10.1", "sha": SHA, "latest": "true", "kind": "release"}


def test_the_first_release_is_the_latest(entry, tmp_path):
    result, outputs = check_tag(entry, tmp_path, "v0.1.0", gh_directory(tmp_path, ["skeleton-v0.1", "v0.1.0"]))
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs["latest"] == "true"


@pytest.mark.parametrize(
    "tag",
    [
        "v1.10.1-qa.3", "v1.10", "1.10.1", "develop", "v01.2.3", "v1.2.3 ", "refs/tags/v1.2.3", "",
        # The rolling tag of a QA build, and suffixes that only look like a preview.
        "qa-develop", "v1.10.1-preview", "v1.10.1-preview.0", "v1.10.1-preview.01", "v1.10.1-rc.1", "v1.10-preview.1",
        "v1.10.1-preview.1.2", "1.10.1-preview.1",
    ],
)
def test_only_a_release_tag_is_built(entry, tmp_path, tag):
    directory = gh_directory(tmp_path, TAGS)
    result, outputs = check_tag(entry, tmp_path, tag, directory)
    assert result.returncode == 1
    assert "is not a release tag" in result.stdout
    assert outputs == {}
    assert (directory / "calls").read_text() == "", "nothing is asked for a ref that is not a release tag"


def test_a_tag_that_does_not_exist_is_refused(entry, tmp_path):
    # As on a manual run started for a branch with a version-shaped tag input.
    result, _ = check_tag(entry, tmp_path, "v3.0.0", gh_directory(tmp_path, TAGS))
    assert result.returncode == 1
    assert "::error::Tag v3.0.0 does not exist in acme/app." in result.stdout


def test_a_new_release_below_the_highest_tag_of_its_line_is_refused(entry, tmp_path):
    result, _ = check_tag(entry, tmp_path, "v1.9.0", gh_directory(tmp_path, TAGS))
    assert result.returncode == 1
    assert "v1.9.0 is below v1.10.1, the highest release tag of major 1" in result.stdout
    result, _ = check_tag(entry, tmp_path / "lines", "v1.13.1", gh_directory(tmp_path / "lines", LINE_TAGS))
    assert result.returncode == 1
    assert "v1.13.1 is below v1.13.2, the highest release tag of major 1" in result.stdout


def test_an_older_release_that_exists_can_be_built_again(entry, tmp_path):
    result, outputs = check_tag(entry, tmp_path, "v1.9.0", gh_directory(tmp_path, TAGS, released=RELEASED + ["v1.10.1"]))
    assert result.returncode == 0, result.stdout + result.stderr
    # It does not take the latest mark from the newer release.
    assert outputs["latest"] == "false"
    assert "GitHub Release exists: true" in result.stdout


def test_a_release_of_a_lower_line_is_not_the_latest(entry, tmp_path):
    # 2.0.1 exists, and 1.13.2 is released after it from release/1.x.
    directory = gh_directory(tmp_path, LINE_TAGS, released=["v1.13.0", "v1.13.1", "v2.0.0", "v2.0.1"])
    result, outputs = check_tag(entry, tmp_path, "v1.13.2", directory)
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"tag": "v1.13.2", "sha": SHA, "latest": "false", "kind": "release"}


def test_a_release_of_the_highest_line_is_the_latest(entry, tmp_path):
    directory = gh_directory(tmp_path, LINE_TAGS, released=["v1.13.0", "v1.13.1", "v1.13.2", "v2.0.0"])
    result, outputs = check_tag(entry, tmp_path, "v2.0.1", directory)
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs["latest"] == "true"


# Previews of 2.1.0 on the main line and of 1.14.0 on the maintenance branch, next to the releases.
PREVIEW_TAGS = LINE_TAGS + ["v2.1.0-preview.1", "v2.1.0-preview.2", "v2.1.0-preview.10", "v1.14.0-preview.1", "qa-develop"]


@pytest.mark.parametrize("tag", ["v2.1.0-preview.10", "v1.14.0-preview.1"])
def test_a_preview_tag_is_built_as_a_preview_and_is_never_the_latest(entry, tmp_path, tag):
    result, outputs = check_tag(entry, tmp_path, tag, gh_directory(tmp_path, PREVIEW_TAGS, released=LINE_TAGS))
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"tag": tag, "sha": SHA, "latest": "false", "kind": "preview"}
    assert f"Preview {tag} at {SHA}. Latest: false. GitHub Release exists: false." in result.stdout


def test_the_first_build_of_a_repository_can_be_a_preview(entry, tmp_path):
    # One caller has no release yet, and shows 2.0.0 at demo sites before it releases it.
    result, outputs = check_tag(entry, tmp_path, "v2.0.0-preview.1", gh_directory(tmp_path, ["v2.0.0-preview.1"]))
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"tag": "v2.0.0-preview.1", "sha": SHA, "latest": "false", "kind": "preview"}


def test_a_preview_does_not_change_which_release_is_the_latest(entry, tmp_path):
    # The highest release tag stays the latest release, whatever previews exist above it.
    result, outputs = check_tag(entry, tmp_path, "v2.0.1", gh_directory(tmp_path, PREVIEW_TAGS, released=LINE_TAGS[:-1]))
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"tag": "v2.0.1", "sha": SHA, "latest": "true", "kind": "release"}


def test_a_new_preview_has_to_be_the_highest_of_its_version(entry, tmp_path):
    result, _ = check_tag(entry, tmp_path, "v2.1.0-preview.2", gh_directory(tmp_path, PREVIEW_TAGS))
    assert result.returncode == 1
    assert "v2.1.0-preview.2 is below v2.1.0-preview.10, the highest preview of 2.1.0" in result.stdout
    # Built again for the pre-release it already has, it passes.
    directory = gh_directory(tmp_path / "again", PREVIEW_TAGS, released=["v2.1.0-preview.2"])
    result, outputs = check_tag(entry, tmp_path / "again", "v2.1.0-preview.2", directory)
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs["kind"] == "preview" and outputs["latest"] == "false"


@pytest.mark.parametrize(
    ("tag", "highest"),
    [
        # The version is released: what follows it is a preview of the next version.
        ("v2.0.0-preview.3", "v2.0.1"),
        ("v1.13.0-preview.1", "v1.13.2"),
    ],
)
def test_a_new_preview_of_a_version_that_is_released_is_refused(entry, tmp_path, tag, highest):
    result, _ = check_tag(entry, tmp_path, tag, gh_directory(tmp_path, LINE_TAGS + [tag]))
    assert result.returncode == 1
    version = tag[1:].split("-")[0]
    assert f"{tag} is a preview of {version}, and {highest}, the highest release tag of major {tag[1]}, is not below" in result.stdout


@pytest.mark.parametrize("tag", ["v2.0.1-preview.1", "v1.13.2-preview.1", "v2.1.3-preview.2", "v3.0.10-preview.1"])
def test_a_preview_of_a_patch_release_is_not_built(entry, tmp_path, tag):
    # The version bump refuses to plan one. A tag that a person pushed is refused here, before the
    # API is asked anything: its versionCode would be below the release that is out, whatever the
    # tags of the repository are.
    directory = gh_directory(tmp_path, LINE_TAGS + [tag])
    result, outputs = check_tag(entry, tmp_path, tag, directory)
    assert result.returncode == 1 and outputs == {}
    assert f"::error::'{tag}' is a preview of a patch release, and only a new minor or major version has previews (vX.Y.0-preview.N)" in result.stdout
    assert "Nothing is built." in result.stdout
    assert not (directory / "calls").exists() or (directory / "calls").read_text() == ""


def test_a_preview_tag_that_does_not_exist_is_refused(entry, tmp_path):
    result, _ = check_tag(entry, tmp_path, "v2.1.0-preview.3", gh_directory(tmp_path, PREVIEW_TAGS))
    assert result.returncode == 1
    assert "::error::Tag v2.1.0-preview.3 does not exist in acme/app." in result.stdout


def test_an_api_failure_is_not_read_as_no_release(entry, tmp_path):
    directory = gh_directory(tmp_path, TAGS, released=RELEASED)
    (directory / "release-error").write_text("gh: Server Error (HTTP 502)\n")
    result, _ = check_tag(entry, tmp_path, "v1.9.0", directory)
    assert result.returncode == 1
    assert "::error::Cannot list the releases of acme/app." in result.stdout


# --- plan: the branch and the checks of the tagged commit ------------------------------------


@pytest.fixture
def lines(tmp_path) -> dict:
    """A clone as the plan job's checkout has it: every branch of the remote, and HEAD at the tag.

        develop:      base ── merge of #12 (feature: head) ── a
        release/1.x:    └── h
        open pull request:                                    └── open
    """
    from test_pr_check import commit, git, new_repository

    seed = new_repository(tmp_path / "seed")
    git(seed, "branch", "-m", "develop")
    commits = {"base": git(seed, "rev-parse", "HEAD")}
    git(seed, "checkout", "-q", "-b", "release/1.x")
    commits["h"] = commit(seed, {"hotfix.kt": "1"}, "hotfix (#21)")
    git(seed, "checkout", "-q", "-b", "feature", "develop")
    commits["head"] = commit(seed, {"feature.kt": "1"}, "work on the feature")
    git(seed, "checkout", "-q", "develop")
    commit(seed, {"other.kt": "1"}, "the base branch moved on (#11)")
    git(seed, "merge", "-q", "--no-ff", "-m", "Merge pull request #12 from acme/feature", "feature")
    commits["merge"] = git(seed, "rev-parse", "HEAD")
    commits["a"] = commit(seed, {"a.kt": "1"}, "fix (#13)")
    git(seed, "checkout", "-q", "-b", "open")
    commits["open"] = commit(seed, {"open.kt": "1"}, "head of an open pull request")
    git(seed, "checkout", "-q", "develop")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", seed.as_uri(), str(clone))
    git(clone, "checkout", "-q", "--detach", commits["a"])
    return {"clone": clone, **commits}


def check_line(entry, lines: dict, tag: str, sha: str, source: str = "develop"):
    env = {"TAG": tag, "SHA": sha, "SOURCE_BRANCH": source, "RUNNER_TEMP": str(lines["clone"].parent)}
    return run_script(step_by_id(entry["jobs"]["plan"], "line")["run"], env, lines["clone"])


@pytest.mark.parametrize(
    ("tag", "commit_name", "branch"),
    [
        ("v2.0.0", "a", "develop"),
        ("v2.0.0", "merge", "develop"),
        # Before the lines parted: the commit is on both branches.
        ("v1.13.0", "base", "develop"),
        ("v1.13.1", "h", "release/1.x"),
        # A preview is held to the branch of its version's line like the release itself.
        ("v2.0.0-preview.1", "a", "develop"),
        ("v1.14.0-preview.2", "h", "release/1.x"),
    ],
)
def test_a_tag_on_a_commit_of_its_release_branch_is_built(entry, lines, tag, commit_name, branch):
    result = check_line(entry, lines, tag, lines[commit_name])
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"The commit of {tag} ({lines[commit_name][:7]}) is on {branch}." in result.stdout


@pytest.mark.parametrize(
    ("tag", "commit_name"),
    [
        # A tag that a person pushed on a commit that was never merged.
        ("v2.0.0", "open"),
        # The head of a pull request merged with a merge commit: in the history, and never the branch's tip.
        ("v2.0.0", "head"),
        # release/1.x releases major 1 only.
        ("v2.0.0", "h"),
        ("v3.0.0", "h"),
        ("v2.0.0-preview.1", "open"),
        ("v2.0.0-preview.1", "h"),
    ],
)
def test_a_tag_on_any_other_commit_is_refused(entry, lines, tag, commit_name):
    result = check_line(entry, lines, tag, lines[commit_name])
    assert result.returncode == 1
    major = tag[1]
    assert f"is in the first-parent history of neither develop nor release/{major}.x" in result.stdout
    assert "::error title=The tag is not on a release branch::" in result.stdout


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("trunk", "Branch trunk does not exist in this repository."),
        ("", "source-branch is not a branch name"),
        ("-x", "source-branch is not a branch name"),
        ("develop..open", "source-branch is not a branch name"),
    ],
)
def test_the_source_branch_has_to_exist(entry, lines, source, message):
    result = check_line(entry, lines, "v2.0.0", lines["a"], source=source)
    assert result.returncode == 1
    assert message in result.stdout


def test_the_branch_and_the_checks_are_checked_for_every_tag_before_anything_is_built(entry):
    plan = entry["jobs"]["plan"]
    names = [item.get("name") or item.get("uses") for item in plan["steps"]]
    assert names == [
        "Check the tag",
        "actions/checkout@v7",
        "Check the branch of the tagged commit",
        "Check the commit's checks",
        "Compute the versionCode",
        "Pick the runner",
    ]
    # No step is conditional on who pushed the tag or on what started the run.
    for item in plan["steps"][:4]:
        assert "if" not in item, item
    assert "github.event_name" not in json.dumps(plan) and "github.actor" not in json.dumps(plan)
    # The checkout is of the tag that was just checked, with every branch, and leaves no token.
    assert plan["steps"][1]["with"] == {
        "ref": "refs/tags/${{ steps.tag.outputs.tag }}",
        "fetch-depth": 0,
        "persist-credentials": False,
    }
    line = step_by_id(plan, "line")
    assert line["env"] == {
        "TAG": "${{ steps.tag.outputs.tag }}",
        "SHA": "${{ steps.tag.outputs.sha }}",
        "SOURCE_BRANCH": "${{ inputs.source-branch || github.event.repository.default_branch }}",
    }
    checks = step(plan, "Check the commit's checks")
    assert checks["env"] == {
        "TOKEN": "${{ github.token }}",
        "TARGET_SHA": "${{ steps.tag.outputs.sha }}",
        "REQUIRED_CHECKS": "${{ inputs.required-checks }}",
    }
    assert plan["permissions"] == {"contents": "read", "checks": "read"}
    # The primary build needs the plan, so a refusal here means that no job reads the signing key.
    assert entry["jobs"]["primary"]["needs"] == "plan"


def test_the_checks_are_decided_by_the_script_of_the_version_bump(entry):
    # One rule in two workflows: what the version bump requires before it tags is what the release
    # requires of a tag, including one that the version bump never saw.
    bump = load("android-version-bump.yml")
    ours = step(entry["jobs"]["plan"], "Check the commit's checks")
    theirs = step(bump["jobs"]["plan"], "Check the commit's checks")
    assert ours["run"] == theirs["run"]
    inputs = trigger(entry)["inputs"]
    assert inputs["required-checks"]["default"] == trigger(bump)["inputs"]["required-checks"]["default"]
    assert inputs["source-branch"]["default"] == trigger(bump)["inputs"]["source-branch"]["default"] == ""


@pytest.mark.parametrize(
    ("conclusion", "code"), [("success", 0), ("failure", 1), ("cancelled", 1), ("skipped", 1), (None, 1)]
)
def test_a_tag_on_a_commit_whose_check_did_not_succeed_is_not_built(entry, tmp_path, conclusion, code):
    from test_version_bump import SHA as CHECKED, check_run, check_runs_api

    with check_runs_api() as api:
        # None: the check never ran on the commit, as on one that was pushed together with later commits.
        # The QA build of the push passed in every case: the pull request check decides here.
        api.runs = [check_run(name="qa / built")] + ([check_run(conclusion=conclusion)] if conclusion else [])
        env = {
            "TOKEN": "job-token",
            "TARGET_SHA": CHECKED,
            "REQUIRED_CHECKS": trigger(entry)["inputs"]["required-checks"]["default"],
            "GITHUB_API_URL": f"http://127.0.0.1:{api.server_port}",
            "GITHUB_REPOSITORY": "acme/app",
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        }
        result = run_script(step(entry["jobs"]["plan"], "Check the commit's checks")["run"], env, tmp_path)
    assert result.returncode == code, result.stdout + result.stderr
    assert ("::error title=The commit has not passed its checks::" in result.stdout) == (code == 1)


def test_plan_takes_the_version_code_from_the_one_formula(entry):
    plan = entry["jobs"]["plan"]
    code = step_by_id(plan, "code")
    assert code["uses"].startswith("asleep-ai/actions/release-version@")
    # release or preview, as the tag step read the tag: a preview gets the code of a preview, and its
    # whole name, X.Y.Z-preview.N, as the version the APK has to carry.
    assert code["with"] == {
        "operation": "code",
        "kind": "${{ steps.tag.outputs.kind }}",
        "version": "${{ steps.tag.outputs.tag }}",
    }
    # To the other jobs the kind goes as true or false: the runner drops a job output that holds
    # the value of a secret, and "release" is a likely key alias.
    assert "kind" not in plan["outputs"]
    assert plan["outputs"]["preview"] == "${{ steps.tag.outputs.kind == 'preview' }}"
    assert plan["outputs"]["version"] == "${{ steps.code.outputs.version }}"
    assert plan["outputs"]["version-code"] == "${{ steps.code.outputs.version-code }}"
    assert step_by_id(plan, "tag")["env"]["TAG"] == "${{ inputs.tag || github.ref_name }}"
    pick = step_by_id(plan, "pick")
    assert pick["if"] == "${{ inputs.runner-labels != '' }}"
    assert pick["with"]["token"] == "${{ secrets.RUNNER_READ_TOKEN }}"


# --- build: secrets and variables ----------------------------------------------------


def check_values(build, tmp_path, secrets: dict, variables: dict | None = None, listed="", required=""):
    temp = tmp_path / "temp"
    temp.mkdir(exist_ok=True)
    env = {
        "SECRETS_JSON": json.dumps(secrets),
        "VARS_JSON": json.dumps(variables or {}),
        "LOCAL_PROPERTIES_SECRETS": listed,
        "REQUIRED_SECRETS": required,
        "RUNNER_TEMP": str(temp),
    }
    result = run_script(step_by_id(build, "values")["run"], env, tmp_path)
    return result, temp / "android-release.properties"


def java_properties(text: str) -> dict:
    """Read a properties file the way java.util.Properties does, for the escapes the workflow writes."""
    loaded = {}
    for line in text.splitlines():
        name, raw = line.split("=", 1)
        raw = raw.lstrip(" \t\f")
        value, index = [], 0
        while index < len(raw):
            character = raw[index]
            if character != "\\":
                value.append(character)
                index += 1
                continue
            escape = raw[index + 1]
            if escape == "u":
                value.append(chr(int(raw[index + 2 : index + 6], 16)))
                index += 6
            else:
                value.append({"n": "\n", "r": "\r", "t": "\t", "f": "\f"}.get(escape, escape))
                index += 2
        loaded[name] = "".join(value).encode("utf-16", "surrogatepass").decode("utf-16")
    return loaded


def test_the_listed_values_are_prepared_for_local_properties(build, tmp_path):
    secrets = {**SIGNING, "ASLEEP_APP_SECRET": "app-secret", "MIXPANEL_PROJECT_TOKEN": "mixpanel", "github_token": "ghs_job"}
    variables = {"SENTRY_DSN": "https://key@sentry.invalid/1", "UNLISTED": "not written"}
    result, prepared = check_values(
        build, tmp_path, secrets, variables, listed="ASLEEP_APP_SECRET, MIXPANEL_PROJECT_TOKEN\n  SENTRY_DSN\n"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert prepared.read_text().splitlines() == [
        "KEYSTORE_PASSWORD=store-pass",
        "KEY_ALIAS=upload-key",
        "KEY_PASSWORD=key-pass",
        "ASLEEP_APP_SECRET=app-secret",
        "MIXPANEL_PROJECT_TOKEN=mixpanel",
        "SENTRY_DSN=https://key@sentry.invalid/1",
    ]
    assert prepared.stat().st_mode & 0o777 == 0o600
    # Names are printed, values never.
    assert "ASLEEP_APP_SECRET" in result.stdout
    for value in (*secrets.values(), *variables.values()):
        assert value not in result.stdout + result.stderr


def test_everything_missing_is_named_at_once(build, tmp_path):
    secrets = {"KEYSTORE_BASE64": "a2V5", "KEY_ALIAS": "", "ASLEEP_APP_SECRET": "s"}
    result, prepared = check_values(
        build, tmp_path, secrets, {"SENTRY_DSN": ""}, listed="ASLEEP_APP_SECRET SENTRY_DSN", required="SENTRY_AUTH_TOKEN"
    )
    assert result.returncode == 1
    assert (
        "::error title=Missing secrets or variables::KEYSTORE_PASSWORD, KEY_ALIAS, KEY_PASSWORD, SENTRY_DSN, "
        "SENTRY_AUTH_TOKEN. " in result.stdout
    )
    assert "secrets: inherit" in result.stdout
    assert not prepared.exists()


def test_a_required_value_is_checked_and_not_written(build, tmp_path):
    secrets = {**SIGNING, "SENTRY_AUTH_TOKEN": "sentry-token", "GIT_TOKEN": "git-token"}
    result, prepared = check_values(build, tmp_path, secrets, required="SENTRY_AUTH_TOKEN,GIT_TOKEN")
    assert result.returncode == 0, result.stdout + result.stderr
    assert [line.split("=")[0] for line in prepared.read_text().splitlines()] == [
        "KEYSTORE_PASSWORD", "KEY_ALIAS", "KEY_PASSWORD",
    ]
    assert "token" not in prepared.read_text()


def test_a_secret_wins_over_a_variable_of_the_same_name(build, tmp_path):
    result, prepared = check_values(
        build, tmp_path, {**SIGNING, "API_URL": "from-secret"}, {"API_URL": "from-variable", "OTHER": "v"}, listed="API_URL OTHER"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert java_properties(prepared.read_text())["API_URL"] == "from-secret"
    assert java_properties(prepared.read_text())["OTHER"] == "v"


@pytest.mark.parametrize(
    "value",
    [
        "abc\\def",
        "  leading spaces",
        "trailing space ",
        "tab\tand\nnewline",
        "한글 비밀번호",
        "emoji \U0001f600",
        "a=b:c#d!e",
        "C:\\keys\\release.jks",
        "\\u0041",
    ],
)
def test_values_are_escaped_so_that_java_reads_them_back_unchanged(build, tmp_path, value):
    secrets = {**SIGNING, "KEYSTORE_PASSWORD": value, "ODD": value}
    result, prepared = check_values(build, tmp_path, secrets, listed="ODD")
    assert result.returncode == 0, result.stdout + result.stderr
    text = prepared.read_text(encoding="ascii")
    assert len(text.splitlines()) == 4, "one line per entry, whatever the value holds"
    loaded = java_properties(text)
    assert loaded["KEYSTORE_PASSWORD"] == value
    assert loaded["ODD"] == value


@pytest.mark.parametrize(
    ("listed", "required", "message"),
    [
        ("KEY_ALIAS", "", "local-properties-secrets lists KEY_ALIAS, which this workflow writes itself."),
        ("A KEYSTORE_FILE", "", "local-properties-secrets lists KEYSTORE_FILE, which this workflow writes itself."),
        ("NAME=pasted-secret-value", "", "1 entry is not the name of a secret or variable"),
        ("OK", "has-hyphen 1DIGIT", "2 entries are not the name of a secret or variable"),
    ],
)
def test_invalid_names_fail_without_being_printed(build, tmp_path, listed, required, message):
    result, prepared = check_values(build, tmp_path, {**SIGNING, "A": "a", "OK": "ok"}, listed=listed, required=required)
    assert result.returncode == 1
    assert f"::error title=Invalid input::{message}" in result.stdout
    assert "pasted-secret-value" not in result.stdout + result.stderr
    assert not prepared.exists()


def test_the_values_step_is_the_only_one_that_sees_every_secret(build):
    text = (WORKFLOWS / BUILD).read_text()
    assert text.count("toJSON(secrets)") == 1
    emptied, planned, values = build["steps"][:3]
    # Nothing an earlier job left in the work folder is there when the secrets arrive, and the script
    # does not import from the work folder: `python3 -` alone would put it first on the module path.
    assert emptied["id"] == "workspace"
    # What the plan passed is checked next, and it reads no secret.
    assert planned["id"] == "planned" and "secrets" not in json.dumps(planned)
    assert values["id"] == "values", "the check runs before anything is checked out or set up"
    assert "python3 -I - <<'VALUES'" in values["run"]
    assert values["env"]["SECRETS_JSON"] == "${{ toJSON(secrets) }}"
    assert values["env"]["VARS_JSON"] == "${{ toJSON(vars) }}"
    assert "toJSON(secrets)" not in (WORKFLOWS / ENTRY).read_text()


# --- build: the work folder -----------------------------------------------------------


def populated_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "work folder"
    for name in (".git/hooks/post-checkout", ".git/config", "app/build/outputs/apk/release/old.apk", "local.properties"):
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("left by an earlier job")
    return workspace


@pytest.mark.parametrize("name", ["Empty the work folder", "Empty the work folder again"])
def test_a_self_hosted_work_folder_is_emptied(build, tmp_path, name):
    workspace = populated_workspace(tmp_path)
    env = {"RUNNER_KIND": "self-hosted", "GITHUB_WORKSPACE": str(workspace), "RUNNER_TEMP": str(tmp_path)}
    result = run_script(step(build, name)["run"], env, workspace)
    assert result.returncode == 0, result.stderr
    assert workspace.is_dir() and list(workspace.iterdir()) == []


@pytest.mark.parametrize("name", ["Empty the work folder", "Empty the work folder again"])
def test_a_github_hosted_work_folder_is_left_alone(build, tmp_path, name):
    workspace = populated_workspace(tmp_path)
    env = {"RUNNER_KIND": "github-hosted", "GITHUB_WORKSPACE": str(workspace), "RUNNER_TEMP": str(tmp_path)}
    result = run_script(step(build, name)["run"], env, workspace)
    assert result.returncode == 0, result.stderr
    assert (workspace / ".git/config").exists()


def test_the_work_folder_is_emptied_before_checkout_and_after_the_build_whatever_happened(build):
    ids = [item.get("id") for item in build["steps"]]
    assert ids.index("workspace") == 0 < ids.index("values") < ids.index("checkout")
    names = [item.get("name") for item in build["steps"]]
    again = step(build, "Empty the work folder again")
    assert again["if"] == "${{ always() }}"
    assert names.index("Delete the keystore and local.properties") < names.index("Empty the work folder again")
    # The steps decide by themselves, so that their outcome is never `skipped`: gradle-log-verdict reads
    # a skipped setup step as a failed one.
    assert "if" not in step(build, "Empty the work folder")


def test_the_checkout_is_the_tag_and_keeps_no_token(build, entry):
    checkout = step_by_id(build, "checkout")["with"]
    assert checkout == {
        "ref": "${{ inputs.ref }}",
        "token": "${{ inputs.checkout-submodules != 'false' && secrets.GIT_TOKEN || github.token }}",
        "submodules": "${{ inputs.checkout-submodules }}",
        "persist-credentials": False,
    }
    # A release is checked out by its tag, as a tag: a branch of the same name is not built.
    for name in ("primary", "fallback"):
        assert entry["jobs"][name]["with"]["ref"] == "refs/tags/${{ needs.plan.outputs.tag }}"


def test_a_moved_tag_is_refused(build, tmp_path):
    from test_pr_check import commit, git, new_repository

    repo = new_repository(tmp_path / "repo")
    head = git(repo, "rev-parse", "HEAD")
    script = step_by_id(build, "commit")["run"]
    assert run_script(script, {"REF": "refs/tags/v1.0.0", "PLANNED_SHA": head}, repo).returncode == 0
    commit(repo, {"app/Main.kt": "fun main() = Unit\n"})
    result = run_script(script, {"REF": "refs/tags/v1.0.0", "PLANNED_SHA": head}, repo)
    assert result.returncode == 1
    assert "refs/tags/v1.0.0 is at" in result.stdout and "The ref was moved" in result.stdout


# --- build: Gradle ------------------------------------------------------------------------


# What the step that chooses the home is handed for a release, as android-release.yml calls the build:
# it passes neither gradle-home nor source-branch.
RELEASE_RUN = {
    "GRADLE_HOME": "cold",
    "KIND": "release",
    "RUNNER_KIND": "self-hosted",
    "EVENT_NAME": "workflow_dispatch",
    "RUN_REF": "refs/tags/v1.13.1",
    "SOURCE_BRANCH": "develop",
    "REPOSITORY": "acme/app",
}
# And for the QA build of a push to the source branch, as android-qa.yml calls its first build.
QA_RUN = {**RELEASE_RUN, "GRADLE_HOME": "warm", "KIND": "qa", "EVENT_NAME": "push", "RUN_REF": "refs/heads/develop"}
QA_HOME = "android-qa-gradle/acme/app/home"


def choose_home(build, tmp_path, **overrides):
    """Run the step that chooses the Gradle user home on a host whose runner user has `host` as its home."""
    temp, host = tmp_path / "temp", tmp_path / "host"
    temp.mkdir(exist_ok=True)
    host.mkdir(exist_ok=True)
    github_env, output = tmp_path / "github-env", tmp_path / "home-output"
    github_env.write_text("")
    output.write_text("")
    env = {
        "RUNNER_TEMP": str(temp),
        "HOME": str(host),
        "GITHUB_ENV": str(github_env),
        "GITHUB_OUTPUT": str(output),
        "GITHUB_RUN_ID": "77",
        "GITHUB_RUN_ATTEMPT": "1",
        **QA_RUN,
        **overrides,
    }
    result = run_script(step_by_id(build, "gradle-home")["run"], env, tmp_path)
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    lines = github_env.read_text().splitlines()
    assert len(lines) <= 1 and all(line.startswith("GRADLE_USER_HOME=") for line in lines), lines
    return result, Path(lines[0].split("=", 1)[1]) if lines else None, outputs


def fill(home: Path, *names: str) -> None:
    for name in names:
        path = home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"left in {name}")


# What a build leaves in a Gradle user home that is worth keeping.
CACHES = ("caches/modules-2/files-2.1/androidx.core/core-1.13.0.aar", "wrapper/dists/gradle-8.9-bin/abc/gradle-8.9/bin/gradle")


def test_the_gradle_user_home_of_a_release_belongs_to_the_job(build, tmp_path):
    temp = tmp_path / "temp"
    stale = temp / "gradle-user-home" / "init.d" / "evil.gradle"
    stale.parent.mkdir(parents=True)
    stale.write_text("left behind")
    result, home, outputs = choose_home(build, tmp_path, **RELEASE_RUN)
    assert result.returncode == 0, result.stderr
    assert home == temp / "gradle-user-home"
    assert home.is_dir() and list(home.iterdir()) == []
    assert outputs == {"warm": "false"}
    assert not (tmp_path / "host" / "android-qa-gradle").exists()
    ids = [item.get("id") for item in build["steps"]]
    assert ids.index("gradle-home") < ids.index("gradle") < ids.index("build")
    # Nothing is restored into a build that signs, and nothing it produced is saved.
    assert step_by_id(build, "gradle")["with"] == {"cache-disabled": True}
    # Cold unless a caller asks for more, and android-release.yml asks for nothing.
    inputs = trigger(load(BUILD))["inputs"]
    assert (inputs["gradle-home"]["default"], inputs["gradle-home"]["required"]) == ("cold", False)
    assert (inputs["source-branch"]["default"], inputs["source-branch"]["required"]) == ("", False)


@pytest.mark.parametrize(
    "run",
    [
        {},
        {"KIND": "label", "EVENT_NAME": "workflow_dispatch"},
        {"EVENT_NAME": "workflow_dispatch"},
        # The maintenance branches are built from merged code as well, and share the home of the repository.
        {"RUN_REF": "refs/heads/release/1.x"},
        {"RUN_REF": "refs/heads/release/12.x", "EVENT_NAME": "workflow_dispatch"},
        {"RUN_REF": "refs/heads/release/0.x"},
        {"RUN_REF": "refs/heads/main", "SOURCE_BRANCH": "main"},
        {"GRADLE_HOME": "reset"},
    ],
)
def test_a_qa_build_of_a_release_branch_on_a_self_hosted_runner_uses_the_qa_home(build, tmp_path, run):
    result, home, outputs = choose_home(build, tmp_path, **run)
    assert result.returncode == 0, result.stdout + result.stderr
    assert home == tmp_path / "host" / QA_HOME
    assert home.is_dir() and list(home.iterdir()) == []
    assert outputs == {"warm": "true", "home": str(home)}
    # Only the runner's user reads it.
    assert home.stat().st_mode & 0o077 == 0 and (tmp_path / "host" / "android-qa-gradle").stat().st_mode & 0o077 == 0
    assert not (tmp_path / "temp" / "gradle-user-home").exists()
    assert "::warning" not in result.stdout
    assert f"Gradle user home: {home} (the QA home of acme/app)" in result.stdout


@pytest.mark.parametrize(
    ("run", "reason"),
    [
        # A release and a preview are cold whatever a caller passes: the build workflow itself says so.
        ({"KIND": "release"}, "a release or a preview is built cold"),
        ({"KIND": "preview"}, "a release or a preview is built cold"),
        ({"KIND": "release", "RUN_REF": "refs/tags/v1.13.1", "EVENT_NAME": "workflow_dispatch"}, "a release or a preview is built cold"),
        ({"KIND": "preview", "GRADLE_HOME": "reset"}, "a release or a preview is built cold"),
        ({"KIND": ""}, "a release or a preview is built cold"),
        # The build after an infrastructure failure, and a caller without self-hosted runners.
        ({"RUNNER_KIND": "github-hosted"}, "a GitHub-hosted runner keeps nothing between jobs"),
        ({"RUNNER_KIND": ""}, "a GitHub-hosted runner keeps nothing between jobs"),
        ({"GRADLE_HOME": "cold"}, "gradle-home is cold"),
        # A pull request never reads or writes the home, whatever branch it names.
        ({"EVENT_NAME": "pull_request", "RUN_REF": "refs/pull/12/merge"}, "this is a pull_request run"),
        ({"EVENT_NAME": "pull_request"}, "this is a pull_request run"),
        ({"EVENT_NAME": "pull_request_target"}, "this is a pull_request_target run"),
        ({"EVENT_NAME": "schedule"}, "this is a schedule run"),
        ({"EVENT_NAME": "workflow_run"}, "this is a workflow_run run"),
        ({"EVENT_NAME": "merge_group", "RUN_REF": "refs/heads/gh-readonly-queue/develop/pr-1"}, "this is a merge_group run"),
        ({"EVENT_NAME": ""}, "this is a  run"),
        # Code that is not merged: any other branch, a tag, and names that only look like a release branch.
        ({"RUN_REF": "refs/heads/feature/login"}, "this run is on refs/heads/feature/login"),
        ({"RUN_REF": "refs/heads/developer"}, "this run is on refs/heads/developer"),
        ({"RUN_REF": "refs/heads/main"}, "this run is on refs/heads/main"),
        ({"RUN_REF": "refs/heads/release/1.x/fix"}, "this run is on refs/heads/release/1.x/fix"),
        ({"RUN_REF": "refs/heads/release/01.x"}, "this run is on refs/heads/release/01.x"),
        ({"RUN_REF": "refs/heads/release/x"}, "this run is on refs/heads/release/x"),
        ({"RUN_REF": "refs/heads/release/1.2"}, "this run is on refs/heads/release/1.2"),
        ({"RUN_REF": "refs/heads/hotfix/release/1.x"}, "this run is on refs/heads/hotfix/release/1.x"),
        ({"RUN_REF": "refs/tags/develop"}, "this run is on refs/tags/develop"),
        ({"RUN_REF": "refs/tags/release/1.x"}, "this run is on refs/tags/release/1.x"),
        ({"RUN_REF": "refs/tags/v1.13.1", "EVENT_NAME": "workflow_dispatch"}, "this run is on refs/tags/v1.13.1"),
        ({"RUN_REF": "refs/pull/12/merge"}, "this run is on refs/pull/12/merge"),
        ({"RUN_REF": "develop"}, "this run is on develop"),
        ({"RUN_REF": "refs/heads/", "SOURCE_BRANCH": ""}, "this run is on refs/heads/"),
        ({"RUN_REF": "", "SOURCE_BRANCH": ""}, "this run is on "),
    ],
)
def test_every_other_build_is_cold_whatever_it_asks_for(build, tmp_path, run, reason):
    # The home of earlier QA builds is there, and is neither read nor touched.
    kept = tmp_path / "host" / QA_HOME
    fill(kept, *CACHES, "gradle.properties")
    result, home, outputs = choose_home(build, tmp_path, **run)
    assert result.returncode == 0, result.stdout + result.stderr
    assert home == tmp_path / "temp" / "gradle-user-home"
    assert home.is_dir() and list(home.iterdir()) == []
    assert outputs == {"warm": "false"}
    (said,) = [line for line in result.stdout.splitlines() if line.startswith("Gradle user home: ")]
    assert said.startswith(f"Gradle user home: {home} (cold: ") and reason in said
    assert sorted(path.name for path in kept.iterdir()) == ["caches", "gradle.properties", "wrapper"]
    assert "::warning" not in result.stdout


@pytest.mark.parametrize("value", ["", "Warm", "hot", "warm ", "true"])
def test_a_gradle_home_that_is_no_known_word_is_cold_and_says_so(build, tmp_path, value):
    result, home, outputs = choose_home(build, tmp_path, GRADLE_HOME=value)
    assert result.returncode == 0, result.stdout + result.stderr
    assert home == tmp_path / "temp" / "gradle-user-home" and outputs == {"warm": "false"}
    assert "::warning title=QA Gradle home not used::gradle-home is none of cold, warm and reset. The build is cold." in result.stdout
    assert not (tmp_path / "host" / "android-qa-gradle").exists()


def test_the_qa_home_is_kept_between_builds_and_holds_no_configuration(build, tmp_path):
    result, home, _ = choose_home(build, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "The QA home is new: this build fills it." in result.stdout
    fill(
        home, *CACHES,
        # What must not decide how the next signed build runs, or stay on the host: configuration and logs.
        "init.gradle", "init.gradle.kts", "gradle.properties", "init.d/evil.gradle", "init.d/more/evil.init.gradle.kts",
        "daemon/8.9/daemon-4711.out.log", "daemon/8.9/registry.bin",
        # setup-gradle writes these in the step after, for this build and for one that runs on another runner.
        "init.d/gradle-actions.build-result-capture.init.gradle",
    )
    (home / "gradle.properties").write_text("KEY_PASSWORD=key-pass\n")
    # The next build: on a maintenance branch, started by hand, with a label. One home for the repository.
    result, again, outputs = choose_home(
        build, tmp_path, KIND="label", EVENT_NAME="workflow_dispatch", RUN_REF="refs/heads/release/1.x"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert again == home and outputs == {"warm": "true", "home": str(home)}
    assert "The QA home exists: earlier QA builds of acme/app left it." in result.stdout
    for name in CACHES:
        assert (home / name).read_text() == f"left in {name}"
    left = sorted(str(path.relative_to(home)) for path in home.rglob("*") if path.is_file() and not path.is_relative_to(home / "caches") and not path.is_relative_to(home / "wrapper"))
    assert left == ["daemon/8.9/registry.bin", "init.d/gradle-actions.build-result-capture.init.gradle"]
    assert (
        "::warning title=QA Gradle home held configuration::Deleted from the QA home before the build: "
        "gradle.properties init.d/evil.gradle init.d/more init.gradle init.gradle.kts. " in result.stdout
    )
    # Names are printed, a value never.
    assert "key-pass" not in result.stdout + result.stderr
    # A home that is as the build before left it says nothing.
    result, _, _ = choose_home(build, tmp_path)
    assert result.returncode == 0 and "::warning" not in result.stdout


def test_each_repository_has_a_qa_home_of_its_own(build, tmp_path):
    homes = {}
    for repository in ("acme/app", "acme/app-v2", "other/app", "acme-app/v2", "acme/app.v2", "acme/app_v2"):
        result, home, _ = choose_home(build, tmp_path, REPOSITORY=repository)
        assert result.returncode == 0, result.stdout + result.stderr
        assert home == tmp_path / "host" / "android-qa-gradle" / repository / "home"
        fill(home, "caches/owner")
        homes[repository] = home
    assert len(set(homes.values())) == len(homes)
    for repository, home in homes.items():
        # Owner and repository are two directories: "acme/app-v2" and "acme-app/v2" do not meet.
        assert [path.name for path in home.parent.iterdir()] == ["home"], repository


@pytest.mark.parametrize("repository", ["", "acme", "acme/", "/app", "acme/app/extra", "acme/..", "acme/.", "../app", "-acme/app", "acme/a b", "acme/a$b"])
def test_a_repository_name_that_cannot_name_a_directory_is_built_cold(build, tmp_path, repository):
    result, home, outputs = choose_home(build, tmp_path, REPOSITORY=repository)
    assert result.returncode == 0, result.stdout + result.stderr
    assert home == tmp_path / "temp" / "gradle-user-home" and outputs == {"warm": "false"}
    assert "::warning title=QA Gradle home not used::The name of the repository cannot name a directory. The build is cold." in result.stdout
    assert not (tmp_path / "host" / "android-qa-gradle").exists()


@pytest.mark.parametrize("home_directory", ["", "relative/home", "{tmp}/missing", "{tmp}/a-file"])
def test_a_host_without_a_home_directory_is_built_cold(build, tmp_path, home_directory):
    (tmp_path / "a-file").write_text("")
    result, home, outputs = choose_home(build, tmp_path, HOME=home_directory.replace("{tmp}", str(tmp_path)))
    assert result.returncode == 0, result.stdout + result.stderr
    assert home == tmp_path / "temp" / "gradle-user-home" and outputs == {"warm": "false"}
    assert "::warning title=QA Gradle home not used::HOME is not a directory" in result.stdout


def test_a_qa_home_that_cannot_be_made_is_built_cold(build, tmp_path):
    # A file where the directory of the QA homes belongs: mkdir fails, and the build goes on without.
    (tmp_path / "host").mkdir()
    (tmp_path / "host" / "android-qa-gradle").write_text("")
    result, home, outputs = choose_home(build, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert home == tmp_path / "temp" / "gradle-user-home" and outputs == {"warm": "false"}
    assert "::warning title=QA Gradle home not used::The QA home could not be prepared" in result.stdout


def test_gradle_home_reset_starts_with_an_empty_qa_home(build, tmp_path):
    home = tmp_path / "host" / QA_HOME
    fill(home, *CACHES, "caches/8.9/transforms/damaged/metadata.bin")
    result, again, outputs = choose_home(build, tmp_path, GRADLE_HOME="reset", EVENT_NAME="workflow_dispatch")
    assert result.returncode == 0, result.stdout + result.stderr
    assert again == home and outputs == {"warm": "true", "home": str(home)}
    assert "Discarded the QA home: gradle-home is reset." in result.stdout
    assert home.is_dir() and list(home.iterdir()) == []
    # Moved away and deleted: nothing of it is left next to the new home.
    assert [path.name for path in home.parent.iterdir()] == ["home"]
    # A reset where there is no home yet is a first build.
    result, other, _ = choose_home(build, tmp_path, GRADLE_HOME="reset", REPOSITORY="acme/other")
    assert result.returncode == 0 and other.is_dir() and "Discarded" not in result.stdout


def test_what_an_interrupted_discard_left_is_deleted_by_the_next_build(build, tmp_path):
    home = tmp_path / "host" / QA_HOME
    fill(home, *CACHES)
    fill(home.parent / "discarded-76-1-4242", "caches/half/deleted.bin")
    result, again, _ = choose_home(build, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert again == home and [path.name for path in home.parent.iterdir()] == ["home"]
    assert (home / CACHES[0]).exists()


@pytest.mark.parametrize("linked", ["home", "caches", "wrapper", "init.d"])
def test_a_link_to_another_gradle_user_home_is_not_followed(build, tmp_path, linked):
    # The home the pull request builds share, with what a pull request's build may have put there.
    shared = tmp_path / "host" / ".gradle"
    fill(shared, "caches/modules-2/files-2.1/poisoned.jar", "wrapper/dists/gradle", "init.d/evil.gradle")
    home = tmp_path / "host" / QA_HOME
    if linked == "home":
        home.parent.mkdir(parents=True)
        home.symlink_to(shared, target_is_directory=True)
    else:
        fill(home, "caches/own.jar" if linked != "caches" else "wrapper/own")
        (home / linked).symlink_to(shared / linked, target_is_directory=True)
    result, again, outputs = choose_home(build, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    # An empty home of its own, and the other home as it was.
    assert again == home and outputs["warm"] == "true"
    assert home.is_dir() and not home.is_symlink() and list(home.iterdir()) == []
    assert "::warning title=QA Gradle home discarded::" in result.stdout and "was a symbolic link" in result.stdout
    assert sorted(str(path.relative_to(shared)) for path in shared.rglob("*") if path.is_file()) == [
        "caches/modules-2/files-2.1/poisoned.jar", "init.d/evil.gradle", "wrapper/dists/gradle",
    ]
    assert [path.name for path in home.parent.iterdir()] == ["home"]


@pytest.mark.parametrize("linked", ["android-qa-gradle", "android-qa-gradle/acme", "android-qa-gradle/acme/app"])
def test_a_linked_directory_above_the_qa_home_is_not_used(build, tmp_path, linked):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    path = tmp_path / "host" / linked
    path.parent.mkdir(parents=True)
    path.symlink_to(elsewhere, target_is_directory=True)
    result, home, outputs = choose_home(build, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert home == tmp_path / "temp" / "gradle-user-home" and outputs == {"warm": "false"}
    assert f"::warning title=QA Gradle home not used::{path} is a symbolic link, and the QA home is a directory of its own. The build is cold." in result.stdout
    assert list(elsewhere.iterdir()) == []


# --- build: the QA home after the build ----------------------------------------------------


def finish_home(build, tmp_path, outcome: str, qa_home: Path | str | None = None):
    host = tmp_path / "host"
    host.mkdir(exist_ok=True)
    env = {
        "HOME": str(host),
        "QA_HOME": str(host / QA_HOME if qa_home is None else qa_home),
        "BUILD_OUTCOME": outcome,
        "GITHUB_RUN_ID": "77",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    return run_script(step(build, "Tidy or discard the QA Gradle home")["run"], env, tmp_path)


@pytest.mark.parametrize("outcome", ["failure", "cancelled"])
def test_a_qa_home_that_a_failed_gradle_run_used_is_not_used_again(build, tmp_path, outcome):
    home = tmp_path / "host" / QA_HOME
    fill(home, *CACHES, "caches/8.9/transforms/half-written/metadata.bin")
    other = tmp_path / "host" / "android-qa-gradle" / "acme" / "other" / "home"
    fill(other, *CACHES)
    result = finish_home(build, tmp_path, outcome)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"::notice title=QA Gradle home discarded::The Gradle run in the QA home ended as {outcome}" in result.stdout
    # Nothing of it is left, and the next build makes a new one. The home of another repository stays.
    assert list(home.parent.iterdir()) == []
    assert (other / CACHES[0]).exists()
    again, chosen, _ = choose_home(build, tmp_path)
    assert again.returncode == 0 and chosen == home and "The QA home is new" in again.stdout


@pytest.mark.parametrize("outcome", ["success", "skipped"])
def test_a_qa_home_is_kept_after_any_other_build_without_logs_and_configuration(build, tmp_path, outcome):
    # skipped: a step before Gradle failed, and Gradle never ran in the home.
    home = tmp_path / "host" / QA_HOME
    fill(
        home, *CACHES, "daemon/8.9/daemon-4711.out.log", "daemon/8.9/registry.bin", "gradle.properties", "init.gradle",
        "init.d/left-by-the-build.gradle", "init.d/gradle-actions.build-result-capture.init.gradle",
    )
    result = finish_home(build, tmp_path, outcome)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Kept the QA home for the next QA build, without daemon logs and configuration: {home}" in result.stdout
    left = sorted(str(path.relative_to(home)) for path in home.rglob("*") if path.is_file())
    assert left == sorted([*CACHES, "daemon/8.9/registry.bin", "init.d/gradle-actions.build-result-capture.init.gradle"])
    assert "::" not in result.stdout


@pytest.mark.parametrize(
    "named",
    ["", "{host}/.gradle", "{host}/android-qa-gradle/acme/app", "{host}/android-qa-gradle/home", "{host}/android-qa-gradle/acme/../../.gradle/home", "{host}/elsewhere/acme/app/home", "/", "{host}"],
)
def test_only_a_qa_home_is_ever_discarded(build, tmp_path, named):
    # The step deletes a directory by a path it was handed. Whatever is not a QA home stays.
    host = tmp_path / "host"
    fill(host, ".gradle/caches/shared.jar", ".gradle/home/x", QA_HOME + "/caches/own.jar", "elsewhere/acme/app/home/x")
    result = finish_home(build, tmp_path, "failure", qa_home=named.replace("{host}", str(host)))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::warning title=QA Gradle home left as it is::" in result.stdout
    assert sorted(str(path.relative_to(host)) for path in host.rglob("*") if path.is_file()) == [
        ".gradle/caches/shared.jar", ".gradle/home/x", "android-qa-gradle/acme/app/home/caches/own.jar",
        "elsewhere/acme/app/home/x",
    ]


def test_a_qa_home_that_became_a_link_or_is_gone_is_left_alone(build, tmp_path):
    host = tmp_path / "host"
    fill(host, ".gradle/caches/shared.jar")
    home = host / QA_HOME
    # Another build discarded the home meanwhile.
    result = finish_home(build, tmp_path, "failure")
    assert result.returncode == 0 and "The QA home is gone: another build discarded it." in result.stdout
    home.parent.mkdir(parents=True)
    home.symlink_to(host / ".gradle", target_is_directory=True)
    for outcome in ("failure", "success"):
        result = finish_home(build, tmp_path, outcome)
        assert result.returncode == 0 and "::warning title=QA Gradle home left as it is::" in result.stdout
        assert (host / ".gradle/caches/shared.jar").exists() and home.is_symlink()


def test_the_qa_home_is_seen_to_after_every_build_that_used_it(build):
    names = [item.get("name") for item in build["steps"]]
    tidy = step(build, "Tidy or discard the QA Gradle home")
    # Whatever happened to the build, and only after a build that was given the QA home.
    assert tidy["if"] == "${{ always() && steps.gradle-home.outputs.warm == 'true' }}"
    # The path is what the step that chose the home said before any code of the repository ran, not
    # a variable the build could have set for the steps after it.
    assert tidy["env"] == {
        "QA_HOME": "${{ steps.gradle-home.outputs.home }}",
        "BUILD_OUTCOME": "${{ steps.build.outcome }}",
    }
    assert "GRADLE_USER_HOME" not in tidy["run"]
    # After the secrets are gone, and before the verdict: the step fails nothing, so the verdict is the build's.
    assert names.index("Delete the keystore and local.properties") < names.index("Tidy or discard the QA Gradle home")
    assert names[-1] == "Report the verdict" and names[-2] == "Tidy or discard the QA Gradle home"
    assert "exit 1" not in tidy["run"]


def test_no_other_step_and_no_other_workflow_names_the_qa_home(build, entry):
    # The directory is the QA build's alone because nothing else is told where it is: the pull
    # request jobs use the home of the runner's user, a release the one of its job.
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text()
        assert ("android-qa-gradle" in text) == (path.name == BUILD), path.name
        if path.name != BUILD:
            assert "GRADLE_USER_HOME" not in text, path.name
    for item in build["steps"]:
        text = json.dumps(item)
        names_it = "android-qa-gradle" in text or "steps.gradle-home.outputs.home" in text
        assert names_it == (item.get("id") == "gradle-home" or item.get("name") == "Tidy or discard the QA Gradle home"), item.get("name")
        # And the home is set in one place. The Gradle step prints it.
        if "GRADLE_USER_HOME" in text:
            assert item.get("id") in ("gradle-home", "build"), item.get("name")
    chosen = step_by_id(build, "gradle-home")
    assert chosen["env"] == {
        "GRADLE_HOME": "${{ inputs.gradle-home }}",
        "KIND": "${{ inputs.kind }}",
        "RUNNER_KIND": "${{ runner.environment }}",
        # The event and the ref of the run, never an input: a caller cannot say that its run is a
        # push to the source branch.
        "EVENT_NAME": "${{ github.event_name }}",
        "RUN_REF": "${{ github.ref }}",
        "SOURCE_BRANCH": "${{ inputs.source-branch || github.event.repository.default_branch }}",
        "REPOSITORY": "${{ github.repository }}",
    }
    assert "if" not in chosen
    # A release asks for nothing, so its two builds are cold by the default of the input, and by
    # their kind whatever the input says.
    for name in ("primary", "fallback"):
        assert "gradle-home" not in entry["jobs"][name]["with"]
        assert entry["jobs"][name]["with"]["kind"] == "${{ needs.plan.outputs.preview == 'true' && 'preview' || 'release' }}"


def test_no_secret_is_written_to_the_gradle_user_home(build):
    # Where each secret is on disk, and for how long: the keystore and the prepared values in the
    # job's temp directory, local.properties in the work folder. All three are deleted, the work
    # folder is emptied, and none of these paths is in a Gradle user home.
    signing = (ROOT / "android-signing" / "signing.py").read_text()
    assert 'temp = env.get("RUNNER_TEMP", "").strip()' in signing and "path = Path(temp) / KEYSTORE_NAME" in signing
    assert 'os.path.join(os.environ["RUNNER_TEMP"], "android-release.properties")' in step_by_id(build, "values")["run"]
    properties = step_by_id(build, "properties")["run"]
    assert '> local.properties' in properties and "GRADLE_USER_HOME" not in properties and "$HOME" not in properties
    # No step writes a gradle.properties: the two that name it delete it from the QA home.
    for item in build["steps"]:
        if "gradle.properties" in item.get("run", ""):
            assert item.get("id") == "gradle-home" or item.get("name") == "Tidy or discard the QA Gradle home"
            for line in item["run"].splitlines():
                if "gradle.properties" in line:
                    assert line.lstrip().startswith(("rm -rf ", "for path in ")), line
    # Memory settings go to Gradle on the command line, not into a file of the home.
    gradle = step_by_id(build, "build")["run"]
    assert '"-Dorg.gradle.jvmargs=$GRADLE_JVMARGS"' in gradle and '"-Pkotlin.daemon.jvmargs=$KOTLIN_DAEMON_JVMARGS"' in gradle
    # And the build writes no task output to a cache of the home, where the values of
    # local.properties would stay inside compiled classes: the flag is there for every kind and home.
    assert "--no-daemon --no-build-cache" in gradle
    assert "GRADLE_HOME" not in gradle and "warm" not in json.dumps(step_by_id(build, "build")["env"])


def write_local_properties(build, tmp_path, keystore: str | None = "release.jks"):
    workspace, temp = tmp_path / "workspace", tmp_path / "temp"
    workspace.mkdir()
    temp.mkdir()
    (workspace / "local.properties").write_text("STALE=1\n")
    (temp / "android-release.properties").write_text("KEYSTORE_PASSWORD=p\nKEY_ALIAS=a\nKEY_PASSWORD=k\nAPP=x\n")
    path = ""
    if keystore:
        (temp / keystore).write_text("keystore")
        path = str(temp / keystore)
    env = {"RUNNER_TEMP": str(temp), "KEYSTORE_FILE": path}
    return run_script(step_by_id(build, "properties")["run"], env, workspace), workspace, temp


def test_local_properties_gets_the_keystore_path_and_the_prepared_values(build, tmp_path):
    result, workspace, temp = write_local_properties(build, tmp_path)
    assert result.returncode == 0, result.stderr
    assert (workspace / "local.properties").read_text().splitlines() == [
        f"KEYSTORE_FILE={temp / 'release.jks'}", "KEYSTORE_PASSWORD=p", "KEY_ALIAS=a", "KEY_PASSWORD=k", "APP=x",
    ]
    assert (workspace / "local.properties").stat().st_mode & 0o077 == 0
    assert not (temp / "android-release.properties").exists()
    assert "Wrote local.properties: 5 entries." in result.stdout


def test_local_properties_is_not_written_without_a_keystore(build, tmp_path):
    result, workspace, _ = write_local_properties(build, tmp_path, keystore=None)
    assert result.returncode == 1
    assert "The keystore was not decoded." in result.stdout
    assert (workspace / "local.properties").read_text() == "STALE=1\n"


def test_the_keystore_path_is_the_one_android_signing_decodes_to(build):
    assert step_by_id(build, "keystore")["with"] == {
        "operation": "decode",
        "keystore-base64": "${{ secrets.KEYSTORE_BASE64 }}",
    }
    assert step_by_id(build, "properties")["env"] == {"KEYSTORE_FILE": "${{ steps.keystore.outputs.keystore-file }}"}


GRADLEW_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$GRADLEW_ARGS"
echo "sentry=${SENTRY_AUTH_TOKEN:-none} home=${GRADLE_USER_HOME:-none}"
echo "BUILD ${GRADLEW_OUTCOME}"
[ "$GRADLEW_OUTCOME" = SUCCESSFUL ]
"""


def run_gradle(build, tmp_path, runner="github-hosted", outcome="SUCCESSFUL", **overrides):
    workspace, temp = tmp_path / "workspace", tmp_path / "temp"
    workspace.mkdir()
    temp.mkdir()
    (workspace / "a.Foo").write_text("")
    executable(workspace / "gradlew", GRADLEW_STUB)
    env = {
        "KIND": "release",
        "VERSION_NAME": "1.13.1",
        "VERSION_CODE": "101350100",
        "GRADLE_ARGS": "",
        "GITHUB_ACTOR": "octocat",
        "GITHUB_TOKEN": "token-for-packages",
        "TOKEN_SOURCE": "GIT_TOKEN",
        "SENTRY_AUTH_TOKEN": "sentry-token",
        "RUNNER_KIND": runner,
        "MAX_WORKERS": "32",
        "GRADLE_JVMARGS": "-Xmx16g -Dfile.encoding=UTF-8",
        "KOTLIN_DAEMON_JVMARGS": "-Xmx6g",
        "RUNNER_TEMP": str(temp),
        "GRADLE_USER_HOME": str(temp / "gradle-user-home"),
        "GRADLEW_ARGS": str(tmp_path / "gradlew-args"),
        "GRADLEW_OUTCOME": outcome,
        **overrides,
    }
    result = run_script(step_by_id(build, "build")["run"], env, workspace)
    return result, (tmp_path / "gradlew-args").read_text().splitlines(), temp / "gradle.log"


def test_gradle_builds_ci_release_with_the_version_and_no_cache(build, tmp_path):
    result, arguments, log = run_gradle(build, tmp_path, GRADLE_ARGS=" -PciBrand=sweetsleep  --tests *.Foo ")
    assert result.returncode == 0, result.stderr
    assert arguments == [
        "ciRelease",
        "-PciVersionName=1.13.1",
        "-PciVersionCode=101350100",
        # Which of the four kinds the build is, for a build that names its APK after it.
        "-PciBuildKind=release",
        "-PciBrand=sweetsleep",
        "--tests",
        "*.Foo",
        "--no-daemon",
        "--no-build-cache",
    ]
    assert "BUILD SUCCESSFUL" in log.read_text()
    assert f"sentry=sentry-token home={tmp_path / 'temp' / 'gradle-user-home'}" in log.read_text()
    # The step says that the token is there, not what it is.
    assert "SENTRY_AUTH_TOKEN is set for this step." in result.stdout
    assert "sentry-token" not in result.stdout.split("sentry=")[0]


def test_a_qa_build_runs_ci_qa_the_same_way(build, tmp_path):
    result, arguments, log = run_gradle(
        build, tmp_path, KIND="qa", VERSION_NAME="2.0.0-qa.57", VERSION_CODE="200000057", SENTRY_AUTH_TOKEN=""
    )
    assert result.returncode == 0, result.stderr
    # The same uncached build as a release, in whatever home: it is signed with the same key.
    assert arguments == [
        "ciQa", "-PciVersionName=2.0.0-qa.57", "-PciVersionCode=200000057", "-PciBuildKind=qa", "--no-daemon",
        "--no-build-cache",
    ]
    assert "Task: ciQa. Kind: qa. Version: 2.0.0-qa.57 (200000057)." in result.stdout
    assert "SENTRY_AUTH_TOKEN is not set: the build uploads nothing to Sentry." in result.stdout


@pytest.mark.parametrize(
    ("kind", "task", "version", "code"),
    [
        ("release", "ciRelease", "1.13.1", "101350100"),
        # A preview tag is built by the task of a release, a label build by the task of a QA build.
        ("preview", "ciRelease", "1.14.0-preview.2", "101449002"),
        ("qa", "ciQa", "1.13.1-qa.57", "101300057"),
        ("label", "ciQa", "1.13.1-ble-test.2", "101350002"),
    ],
)
def test_each_of_the_four_kinds_has_its_task_and_is_passed_to_the_build(build, tmp_path, kind, task, version, code):
    result, arguments, _ = run_gradle(build, tmp_path, KIND=kind, VERSION_NAME=version, VERSION_CODE=code)
    assert result.returncode == 0, result.stderr
    assert arguments[:4] == [task, f"-PciVersionName={version}", f"-PciVersionCode={code}", f"-PciBuildKind={kind}"]
    assert step_by_id(build, "build")["env"]["KIND"] == "${{ inputs.kind }}"


@pytest.mark.parametrize("kind", ["", "Release", "ciRelease", "assembleDebug", "qa; rm -rf ."])
def test_the_gradle_task_is_one_of_two_names(build, tmp_path, kind):
    # The task is chosen by the kind, never taken from a value: nothing a caller writes is run as a
    # task, and nothing but one of four words reaches the build as its kind.
    result, _, _ = run_gradle_unstarted(build, tmp_path, kind)
    assert result.returncode == 1
    assert "::error::kind is none of release, preview, qa and label." in result.stdout
    assert not (tmp_path / "gradlew-args").exists(), "gradlew is not started"


def check_planned(build, tmp_path, **overrides):
    env = {
        "KIND": "qa", "REF": SHA, "PLANNED_SHA": SHA, "VERSION_NAME": "1.13.1-qa.57", "VERSION_CODE": "101300057",
        **overrides,
    }
    return run_script(step_by_id(build, "planned")["run"], env, tmp_path)


@pytest.mark.parametrize(
    ("kind", "version"),
    [("release", "1.13.1"), ("preview", "2.0.0-preview.3"), ("qa", "1.13.1-qa.57"), ("label", "1.13.1-ble-test.2")],
)
def test_a_complete_plan_passes(build, tmp_path, kind, version):
    result = check_planned(build, tmp_path, KIND=kind, VERSION_NAME=version)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Planned: {kind} build {version} (101300057) of {SHA} at {SHA}." in result.stdout


@pytest.mark.parametrize(
    ("overrides", "named"),
    [
        ({"VERSION_NAME": ""}, "VERSION_NAME"),
        ({"VERSION_CODE": ""}, "VERSION_CODE"),
        ({"PLANNED_SHA": "", "REF": ""}, "REF, PLANNED_SHA"),
        # Put together from an output that was dropped: half a name.
        ({"VERSION_NAME": "-qa.57"}, "part of VERSION_NAME (-qa.57)"),
        ({"VERSION_NAME": "1.13.1-qa."}, "part of VERSION_NAME (1.13.1-qa.)"),
        ({"VERSION_NAME": "1.13.1-.2"}, "part of VERSION_NAME (1.13.1-.2)"),
    ],
)
def test_a_plan_with_a_dropped_output_ends_the_build_with_the_reason(build, tmp_path, overrides, named):
    # The runner drops a job output that contains the value of a secret and does not fail the job
    # that set it. Without this step the build would fail on a version Gradle cannot read, or on
    # a checkout of nothing, and name no reason.
    result = check_planned(build, tmp_path, **overrides)
    assert result.returncode == 1
    assert f"::error title=The plan is incomplete::Empty: {named}. The plan job passed, so the runner dropped an output" in result.stdout
    assert "the value of a secret" in result.stdout
    planned = step_by_id(build, "planned")
    assert planned["env"] == {
        "KIND": "${{ inputs.kind }}",
        "REF": "${{ inputs.ref }}",
        "PLANNED_SHA": "${{ inputs.sha }}",
        "VERSION_NAME": "${{ inputs.version-name }}",
        "VERSION_CODE": "${{ inputs.version-code }}",
    }
    # Another runner would be handed the same plan: the build reports code, and is not run again.
    assert "steps.planned.outcome == 'failure'" in build["outputs"]["verdict"]


def run_gradle_unstarted(build, tmp_path, kind):
    workspace, temp = tmp_path / "workspace", tmp_path / "temp"
    workspace.mkdir()
    temp.mkdir()
    executable(workspace / "gradlew", GRADLEW_STUB)
    env = {
        "KIND": kind, "VERSION_NAME": "1.13.1", "VERSION_CODE": "101350100", "GRADLE_ARGS": "", "GITHUB_ACTOR": "octocat",
        "GITHUB_TOKEN": "t", "TOKEN_SOURCE": "GIT_TOKEN", "SENTRY_AUTH_TOKEN": "", "RUNNER_KIND": "github-hosted",
        "MAX_WORKERS": "", "GRADLE_JVMARGS": "", "KOTLIN_DAEMON_JVMARGS": "", "RUNNER_TEMP": str(temp),
        "GRADLE_USER_HOME": str(temp / "gradle-user-home"), "GRADLEW_ARGS": str(tmp_path / "gradlew-args"),
        "GRADLEW_OUTCOME": "SUCCESSFUL",
    }
    return run_script(step_by_id(build, "build")["run"], env, workspace), None, None


def test_gradle_on_a_self_hosted_runner_limits_workers_and_memory(build, tmp_path):
    result, arguments, _ = run_gradle(build, tmp_path, runner="self-hosted")
    assert result.returncode == 0, result.stderr
    assert arguments[4:] == [
        "--no-daemon",
        "--no-build-cache",
        "--max-workers=32",
        "-Dorg.gradle.jvmargs=-Xmx16g -Dfile.encoding=UTF-8",
        "-Pkotlin.daemon.jvmargs=-Xmx6g",
    ]


def test_gradle_without_a_sentry_token_says_so(build, tmp_path):
    result, _, log = run_gradle(build, tmp_path, SENTRY_AUTH_TOKEN="")
    assert result.returncode == 0, result.stderr
    assert "SENTRY_AUTH_TOKEN is not set: the build uploads nothing to Sentry." in result.stdout
    assert "sentry=none" in log.read_text()


def test_a_failed_release_build_fails_the_step_despite_the_pipe(build, tmp_path):
    result, _, log = run_gradle(build, tmp_path, outcome="FAILED")
    assert result.returncode != 0
    assert "BUILD FAILED" in log.read_text()


def test_the_sentry_token_reaches_the_gradle_step_only(build, entry):
    for item in build["steps"]:
        mentions = "SENTRY_AUTH_TOKEN" in json.dumps({key: value for key, value in item.items() if key != "run"})
        assert mentions == (item.get("id") == "build"), item.get("name")
    # A release always gets it. A QA build gets it only when its caller asks for the upload.
    assert step_by_id(build, "build")["env"]["SENTRY_AUTH_TOKEN"] == (
        "${{ inputs.sentry-upload && secrets.SENTRY_AUTH_TOKEN || '' }}"
    )
    upload = trigger(load(BUILD))["inputs"]["sentry-upload"]
    assert (upload["type"], upload["default"], upload["required"]) == ("boolean", True, False)
    for name in ("primary", "fallback"):
        assert "sentry-upload" not in entry["jobs"][name]["with"]
    assert "SENTRY_AUTH_TOKEN" not in json.dumps(entry["jobs"])


# --- build: the APKs -----------------------------------------------------------------------

# Answers with the version in APK_NAME and APK_CODE, except for an APK listed in APK_OTHER, which
# answers as a local build does.
AAPT2_STUB = """#!/usr/bin/env bash
[ "$1 $2" = "dump badging" ] || exit 64
name="$APK_NAME"
code="$APK_CODE"
case " ${APK_OTHER:-} " in *" $(basename "$3") "*) name=0.0.0-dev; code=1 ;; esac
echo "package: name='ai.asleep.app' versionCode='${code}' versionName='${name}' platformBuildVersionName='16'"
echo "application-label:'App'"
"""
ONE_APK = ("app/build/outputs/apk/release/App_1.13.1.apk",)
FLAVOR_APKS = (
    "app/build/outputs/apk/sleephub/release/SleepHub_release_1.13.1_101350100.apk",
    "app/build/outputs/apk/sweet sleep/release/SweetSleep_release_1.13.1_101350100.apk",
)


def check_apks(build, tmp_path, apks=ONE_APK, **overrides):
    workspace, sdk = tmp_path / "workspace", tmp_path / "sdk"
    (workspace / "app/build/outputs/apk").mkdir(parents=True)
    # Not APKs: the files Gradle writes next to them.
    (workspace / "app/build/outputs/apk/output-metadata.json").write_text("{}")
    for name in apks:
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("apk")
    for version in ("35.0.0", "36.1.0", "9.0.0"):
        (sdk / "build-tools" / version).mkdir(parents=True)
        executable(sdk / "build-tools" / version / "aapt2", AAPT2_STUB)
    output = tmp_path / "output"
    output.write_text("")
    env = {
        "KIND": "release",
        "APK_DIRECTORY": "app/build/outputs/apk",
        "VERSION_NAME": "1.13.1",
        "VERSION_CODE": "101350100",
        "BUILD_TOOLS_VERSION": "",
        "ANDROID_HOME": str(sdk),
        "APK_NAME": "1.13.1",
        "APK_CODE": "101350100",
        "GITHUB_OUTPUT": str(output),
        **overrides,
    }
    result = run_script(step_by_id(build, "apk")["run"], env, workspace)
    lines = output.read_text().splitlines()
    # A refusal of the APKs themselves is marked, and lists no path.
    check_apks.refused = lines == ["refused=true"]
    paths = lines[1:-1] if lines and not check_apks.refused else None
    if paths is not None:
        assert (lines[0], lines[-1]) == ("paths<<android-release-apks", "android-release-apks")
    return result, paths


def test_the_one_apk_with_the_planned_version_passes(build, tmp_path):
    result, paths = check_apks(build, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert paths == list(ONE_APK)


def test_every_flavor_s_apk_is_checked_and_listed(build, tmp_path):
    result, paths = check_apks(build, tmp_path, apks=FLAVOR_APKS)
    assert result.returncode == 0, result.stdout + result.stderr
    # One path per line, a space in a flavor directory included.
    assert paths == list(FLAVOR_APKS)
    for name in FLAVOR_APKS:
        assert f"{name}: versionName=1.13.1 versionCode=101350100" in result.stdout


@pytest.mark.parametrize(
    ("built", "message"),
    [
        ({"APK_NAME": "0.0.0-dev", "APK_CODE": "1"}, "App_1.13.1.apk is version 0.0.0-dev (1), and the release is 1.13.1 (101350100)."),
        ({"APK_CODE": "101300001"}, "App_1.13.1.apk is version 1.13.1 (101300001)"),
        ({"APK_NAME": "1.13.1-qa.3"}, "App_1.13.1.apk is version 1.13.1-qa.3 (101350100)"),
    ],
)
def test_an_apk_with_another_version_is_refused(build, tmp_path, built, message):
    result, paths = check_apks(build, tmp_path, **built)
    assert result.returncode == 1
    assert message in result.stdout
    assert "-PciVersionName and -PciVersionCode" in result.stdout
    assert paths is None and check_apks.refused


def test_one_flavor_with_another_version_refuses_the_release(build, tmp_path):
    result, paths = check_apks(build, tmp_path, apks=FLAVOR_APKS, APK_OTHER="SweetSleep_release_1.13.1_101350100.apk")
    assert result.returncode == 1
    assert "SweetSleep_release_1.13.1_101350100.apk is version 0.0.0-dev (1)" in result.stdout
    assert result.stdout.count("::error::") == 1
    assert paths is None and check_apks.refused


def test_a_build_that_left_no_apk_is_refused(build, tmp_path):
    result, paths = check_apks(build, tmp_path, apks=())
    assert result.returncode == 1
    assert "::error::ciRelease left no APK under app/build/outputs/apk." in result.stdout
    assert paths is None and check_apks.refused


def test_a_qa_apk_has_to_carry_the_qa_version(build, tmp_path):
    qa = {"KIND": "qa", "VERSION_NAME": "2.0.0-qa.57", "VERSION_CODE": "200000057"}
    result, paths = check_apks(build, tmp_path, APK_NAME="2.0.0-qa.57", APK_CODE="200000057", **qa)
    assert result.returncode == 0, result.stdout + result.stderr
    assert paths == list(ONE_APK)
    # A ciQa that builds the release version, or ignores the properties.
    result, paths = check_apks(build, tmp_path / "release", **qa)
    assert result.returncode == 1
    assert "App_1.13.1.apk is version 1.13.1 (101350100), and the QA build is 2.0.0-qa.57 (200000057)." in result.stdout
    assert paths is None and check_apks.refused
    result, _ = check_apks(build, tmp_path / "none", apks=(), **qa)
    assert "::error::ciQa left no APK under app/build/outputs/apk." in result.stdout
    # The other two kinds are named in the refusal as what they are.
    label = {"KIND": "label", "VERSION_NAME": "1.13.2-ble-test.1", "VERSION_CODE": "101350101"}
    result, _ = check_apks(build, tmp_path / "label", **label)
    assert "and the label build is 1.13.2-ble-test.1 (101350101)." in result.stdout
    preview = {"KIND": "preview", "VERSION_NAME": "1.14.0-preview.1", "VERSION_CODE": "101449001"}
    result, _ = check_apks(build, tmp_path / "preview", **preview)
    assert "and the preview is 1.14.0-preview.1 (101449001)." in result.stdout
    result, _ = check_apks(build, tmp_path / "preview-none", apks=(), **preview)
    assert "::error::ciRelease left no APK under app/build/outputs/apk." in result.stdout


@pytest.mark.parametrize("name", ["App 1.13.1.apk", "앱_1.13.1.apk", "App_(1.13.1).apk", "-App.apk", ".App.apk"])
def test_an_apk_whose_name_github_would_change_is_refused(build, tmp_path, name):
    # GitHub replaces other characters in the name of a release asset, and the publish jobs find
    # the APKs of a release by the names the build gave them.
    result, paths = check_apks(build, tmp_path, apks=(f"app/build/outputs/apk/release/{name}",))
    assert result.returncode == 1
    assert f"::error::APK file names may hold letters, digits and . _ - only, and these do not: {name}." in result.stdout
    assert paths is None and check_apks.refused


@pytest.mark.parametrize("directory", ["app/build/outputs/elsewhere", "", "-delete"])
def test_the_apk_directory_has_to_exist(build, tmp_path, directory):
    result, paths = check_apks(build, tmp_path, APK_DIRECTORY=directory)
    assert result.returncode == 1
    assert "is not a directory after the build" in result.stdout
    assert paths is None and check_apks.refused
    assert (tmp_path / "workspace" / ONE_APK[0]).exists()


def test_apks_that_would_collide_as_release_assets_are_refused(build, tmp_path):
    apks = ("app/build/outputs/apk/one/release/app-release.apk", "app/build/outputs/apk/two/release/app-release.apk")
    result, paths = check_apks(build, tmp_path, apks=apks)
    assert result.returncode == 1
    assert "share a file name: app-release.apk" in result.stdout
    assert paths is None and check_apks.refused


def test_aapt2_comes_from_the_named_build_tools_version(build, tmp_path):
    result, _ = check_apks(build, tmp_path, BUILD_TOOLS_VERSION="35.0.0")
    assert result.returncode == 0, result.stdout + result.stderr
    result, _ = check_apks(build, tmp_path / "missing", BUILD_TOOLS_VERSION="34.0.0")
    assert result.returncode == 1
    assert "aapt2 was not found" in result.stdout and "(version 34.0.0)" in result.stdout
    # The tool is missing on this runner; another runner may have it.
    assert not check_apks.refused


def test_without_an_sdk_the_apks_are_not_passed_unchecked(build, tmp_path):
    result, paths = check_apks(build, tmp_path, ANDROID_HOME=str(tmp_path / "no-sdk"))
    assert result.returncode == 1
    assert "No Android SDK with build-tools" in result.stdout
    assert paths is None and not check_apks.refused


def test_the_apks_are_verified_uploaded_and_the_secrets_deleted(build):
    names = [item.get("name") for item in build["steps"]]
    order = [
        "Run Gradle",
        "Check the APKs",
        "Verify the APK signatures",
        "Upload the signed APKs",
        "Delete the keystore and local.properties",
        "Empty the work folder again",
        "Tidy or discard the QA Gradle home",
        "Report the verdict",
    ]
    assert names[-len(order) :] == order
    # Every APK that was checked is verified and uploaded: both steps take the same list.
    verify = step(build, "Verify the APK signatures")["with"]
    assert verify == {
        "operation": "verify",
        "apk-file": "${{ steps.apk.outputs.paths }}",
        "expected-cert-sha256": "${{ inputs.expected-cert-sha256 }}",
        "build-tools-version": "${{ inputs.build-tools-version }}",
    }
    upload = step(build, "Upload the signed APKs")
    # Two names for four kinds: what android-release.yml downloads (a release or a preview) and
    # what android-qa.yml downloads (a QA build or a label build).
    family = "(inputs.kind == 'qa' || inputs.kind == 'label')"
    assert upload["with"]["name"] == "android-${{ " + family + " && 'qa' || 'release' }}-apk-${{ inputs.attempt }}"
    # A QA artifact is the way to the publish job and nothing else: the shortest time GitHub keeps one.
    assert upload["with"]["retention-days"] == "${{ " + family + " && 1 || 90 }}"
    assert upload["with"]["path"] == "${{ steps.apk.outputs.paths }}"
    assert upload["with"]["if-no-files-found"] == "error"
    cleanup = step(build, "Delete the keystore and local.properties")
    assert cleanup["if"] == "${{ always() }}"
    assert cleanup["with"]["operation"] == "cleanup"
    assert cleanup["with"]["paths"].splitlines() == [
        "local.properties",
        "${{ runner.temp }}/android-release.properties",
    ]


# --- build: verdict and outputs ------------------------------------------------------------


def test_the_verdict_covers_every_step_before_the_build(build):
    steps = build["steps"]
    verdict = steps[-1]
    assert verdict["id"] == "verdict"
    assert verdict["if"] == "${{ always() && steps.planned.outcome != 'failure' && steps.values.outcome != 'failure' }}"
    ids = [item.get("id") for item in steps]
    before = [identifier for identifier in ids[: ids.index("build")] if identifier not in ("planned", "values")]
    assert None not in before, "a step before the build without an id cannot be reported"
    reported = re.findall(r"\$\{\{ steps\.([a-z-]+)\.outcome \}\}", verdict["with"]["setup-outcome"])
    assert reported == before
    assert re.sub(r"\$\{\{ steps\.[a-z-]+\.outcome \}\}", "", verdict["with"]["setup-outcome"]).strip() == ""
    assert verdict["with"]["build-outcome"] == "${{ steps.build.outcome }}"
    assert verdict["with"]["job-status"] == "${{ job.status }}"
    # A step that may be skipped would read as a failed setup.
    for item in steps[: ids.index("build") + 1]:
        assert "if" not in item, item.get("name")
    # A missing secret is final, and so are an incomplete plan and APKs that cannot be released: the
    # other runner would end the same way, after another cold build. The verdict is the only output:
    # the runner drops a job output that contains a secret's value, and a file or artifact name can.
    #
    # A Gradle run in the QA home that failed is never final: what reads like a failure of the code
    # may be a damaged entry of the home, so the verdict is infra and the calling workflow builds
    # again, cold, where a failure of the code fails again. The three refusals above stay code: no
    # home changes a missing secret or the version an APK carries.
    assert build["outputs"] == {
        "verdict": (
            "${{ (steps.planned.outcome == 'failure' || steps.values.outcome == 'failure' "
            "|| steps.apk.outputs.refused == 'true') && 'code' "
            "|| (steps.gradle-home.outputs.warm == 'true' && steps.verdict.outputs.verdict == 'code' && 'infra') "
            "|| steps.verdict.outputs.verdict }}"
        )
    }
    assert set(trigger(load(BUILD))["outputs"]) == {"verdict"}


def test_the_build_job_reads_its_secrets_from_the_environment(build):
    assert build["environment"] == "${{ inputs.environment }}"
    assert build["runs-on"] == "${{ fromJSON(inputs.runs-on) }}"
    assert load(BUILD)["permissions"] == {"contents": "read", "packages": "read"}
    assert "permissions" not in build


# --- wiring ------------------------------------------------------------------------------------


def test_primary_and_fallback_pass_the_same_inputs(entry):
    inputs = trigger(entry)["inputs"]
    inner = trigger(load(BUILD))
    primary, fallback = entry["jobs"]["primary"], entry["jobs"]["fallback"]
    assert primary["uses"] == fallback["uses"] == f"./.github/workflows/{BUILD}"
    # The names in local-properties-secrets are known only to the caller, so every secret is handed on.
    assert primary["secrets"] == fallback["secrets"] == "inherit"
    assert "secrets" not in inner
    # sentry-upload is left at its default: a release always gets the token. So are gradle-home and
    # the branch it is decided with: a release is built cold, and the QA home is the QA build's.
    left_out = {"sentry-upload", "gradle-home", "source-branch"}
    assert set(primary["with"]) == set(fallback["with"]) == set(inner["inputs"]) - left_out
    assert primary["with"]["attempt"] == "primary" and fallback["with"]["attempt"] == "fallback"
    # release or preview, from the plan's true or false: the word itself is not a job output.
    assert primary["with"]["kind"] == fallback["with"]["kind"] == "${{ needs.plan.outputs.preview == 'true' && 'preview' || 'release' }}"
    planned = {"sha": "sha", "version-name": "version", "version-code": "version-code"}
    for name, output in planned.items():
        assert primary["with"][name] == fallback["with"][name] == f"${{{{ needs.plan.outputs.{output} }}}}"
    forwarded = set(inner["inputs"]) - {"runs-on", "attempt", "kind", "ref", *left_out, *planned}
    assert forwarded == set(inputs) - {
        "tag", "runner-labels", "fallback-runs-on", "source-branch", "required-checks", "release-notes-prompt",
    }
    for name in forwarded:
        assert primary["with"][name] == fallback["with"][name] == f"${{{{ inputs.{name} }}}}"
        assert inputs[name]["type"] == inner["inputs"][name]["type"], name
        if inputs[name]["default"] == "":
            assert inner["inputs"][name]["required"] is False, name
    hosted = "(startsWith(inputs.fallback-runs-on, '[') && inputs.fallback-runs-on) || toJSON(inputs.fallback-runs-on || 'ubuntu-latest') }}"
    assert primary["with"]["runs-on"] == "${{ needs.plan.outputs.runs-on || " + hosted
    assert fallback["with"]["runs-on"] == "${{ " + hosted


def test_fallback_runs_only_after_a_self_hosted_infrastructure_failure(entry):
    condition = " ".join(entry["jobs"]["fallback"]["if"].split())
    clauses = (
        "!cancelled()",
        "needs.plan.result == 'success'",
        "needs.plan.outputs.self-hosted == 'true'",
        "(needs.primary.result == 'failure' || needs.primary.result == 'cancelled')",
        "needs.primary.outputs.verdict != 'code'",
    )
    assert condition == "${{ " + " && ".join(clauses) + " }}"
    assert entry["jobs"]["fallback"]["needs"] == ["plan", "primary"]
    assert "if" not in entry["jobs"]["primary"]


def test_publish_takes_the_apk_of_the_build_that_passed(entry):
    publish = entry["jobs"]["publish"]
    assert publish["needs"] == ["plan", "primary", "fallback"]
    assert publish["if"] == (
        "${{ !cancelled() && (needs.primary.result == 'success' || needs.fallback.result == 'success') }}"
    )
    assert publish["runs-on"] == "ubuntu-latest"
    # The only job that can write, and it runs none of the repository's code.
    assert publish["permissions"] == {"contents": "write", "pull-requests": "read"}
    assert entry["permissions"] == {"contents": "read", "packages": "read"}
    assert entry["jobs"]["plan"]["permissions"] == {"contents": "read", "checks": "read"}
    download = step(publish, "Download the signed APKs")["with"]
    # The name the build job uploads under, built from the results and not read from an output.
    assert download["name"] == "android-release-apk-${{ needs.primary.result == 'success' && 'primary' || 'fallback' }}"
    assert "outputs.artifact" not in (WORKFLOWS / ENTRY).read_text()
    checkout = publish["steps"][0]["with"]
    assert checkout == {"ref": "refs/tags/${{ needs.plan.outputs.tag }}", "fetch-depth": 0, "persist-credentials": False}
    notes = step(publish, "Write the release notes")
    assert notes["uses"] == "asleep-ai/actions/release-notes@release-notes/v1"
    assert notes["if"] == "${{ steps.release.outputs.exists == 'false' }}"
    previous = step(publish, "Find the previous release of the line")
    assert previous["id"] == "previous" and previous["if"] == notes["if"]
    assert notes["with"]["previous-tag"] == "${{ steps.previous.outputs.tag }}"
    # Without a previous release the action would look for a tag itself, and could find one that was
    # left out on purpose. A pattern without a wildcard that names no tag finds none.
    pattern = notes["with"]["tag-pattern"]
    assert not set("*?[") & set(pattern) and not re.fullmatch(r"v[0-9].*", pattern)
    assert notes["with"]["openai-api-key"] == "${{ secrets.OPENAI_API_KEY }}"
    create = step(publish, "Create or update the GitHub Release")
    assert notes["with"]["output-file"] == create["env"]["NOTES_FILE"]
    assert create["env"]["LATEST"] == "${{ needs.plan.outputs.latest }}"


def previous_release(entry, tmp_path, repo: Path, tag: str, released: list[str]) -> str:
    directory = gh_directory(tmp_path, [], released=released)
    output = tmp_path / "previous-output"
    output.write_text("")
    env = {
        "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}",
        "GH_DIR": str(directory),
        "GH_REPO": "acme/app",
        "TAG": tag,
        "GITHUB_OUTPUT": str(output),
    }
    script = step(entry["jobs"]["publish"], "Find the previous release of the line")["run"]
    result = run_script(script, env, repo)
    assert result.returncode == 0, result.stdout + result.stderr
    return output.read_text()


def test_the_notes_start_at_the_previous_release_of_the_same_line(entry, tmp_path):
    from test_pr_check import commit, git, new_repository

    # main line:    base(v1.13.0) ── a(v2.0.0) ── b(v2.0.1, tagged, never published) ── c(v2.0.2)
    # release/1.x:    └── h1(v1.13.1) ── h2(v1.13.2)
    repo = new_repository(tmp_path / "repo")
    git(repo, "tag", "v1.13.0")
    git(repo, "tag", "v1.13.0-handok-reset-off.84")
    git(repo, "checkout", "-q", "-b", "release/1.x")
    commit(repo, {"hotfix.kt": "1"}, "hotfix one")
    git(repo, "tag", "-a", "v1.13.1", "-m", "v1.13.1")
    commit(repo, {"hotfix.kt": "2"}, "hotfix two")
    git(repo, "tag", "v1.13.2")
    git(repo, "checkout", "-q", "main")
    for number, tag in enumerate(("v2.0.0", "v2.0.1", "v2.0.2")):
        commit(repo, {"app.kt": str(number)}, f"change {number}")
        git(repo, "tag", tag)
    released = ["v2.0.0", "v1.13.1", "v1.13.0-handok-reset-off.84", "v1.13.0"]

    def previous(tag: str, name: str) -> str:
        return previous_release(entry, tmp_path / name, repo, tag, released)

    # A maintenance release starts at the release before it on its own branch, not at 2.0.0.
    assert previous("v1.13.2", "maintenance") == "tag=v1.13.1\n"
    # The first release of a new major starts where its line left the old one.
    assert previous("v2.0.0", "major") == "tag=v1.13.0\n"
    # v2.0.1 was never published: its changes belong in the notes of v2.0.2.
    assert previous("v2.0.2", "unpublished") == "tag=v2.0.0\n"
    assert previous("v1.13.0", "first") == "tag=\n"


def test_the_notes_of_a_preview_start_at_the_preview_before_it(entry, tmp_path):
    from test_pr_check import commit, git, new_repository

    # base(v1.13.0) ── a(v2.0.0-preview.1) ── b(.2, never published) ── c(.3) ── d(.10) ── e(v2.0.0) ── f(v2.1.0-preview.1)
    repo = new_repository(tmp_path / "repo")
    git(repo, "tag", "v1.13.0")
    tags = ("v2.0.0-preview.1", "v2.0.0-preview.2", "v2.0.0-preview.3", "v2.0.0-preview.10", "v2.0.0", "v2.1.0-preview.1")
    for number, tag in enumerate(tags):
        commit(repo, {"app.kt": str(number)}, f"change {number}")
        git(repo, "tag", tag)
    released = ["v1.13.0", "v2.0.0-preview.1", "v2.0.0-preview.3", "v2.0.0-preview.10", "v2.0.0"]

    def previous(tag: str, name: str) -> str:
        return previous_release(entry, tmp_path / name, repo, tag, released)

    # The first preview of a version starts where its release would: at the last release of the line.
    assert previous("v2.0.0-preview.1", "first") == "tag=v1.13.0\n"
    # The next ones list what changed since the published preview before, in the order of their numbers.
    assert previous("v2.0.0-preview.2", "second") == "tag=v2.0.0-preview.1\n"
    assert previous("v2.0.0-preview.3", "unpublished") == "tag=v2.0.0-preview.1\n"
    assert previous("v2.0.0-preview.10", "tenth") == "tag=v2.0.0-preview.3\n"
    # The release never starts at one of its previews: its notes are everything since the release before.
    assert previous("v2.0.0", "release") == "tag=v1.13.0\n"
    # And a preview of the next version starts at that release, not at a preview of another version.
    assert previous("v2.1.0-preview.1", "next") == "tag=v2.0.0\n"


def test_an_unreadable_release_list_stops_the_notes(entry, tmp_path):
    from test_pr_check import new_repository

    repo = new_repository(tmp_path / "repo")
    directory = gh_directory(tmp_path, [])
    (directory / "release-error").write_text("gh: Server Error (HTTP 502)\n")
    env = {
        "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}",
        "GH_DIR": str(directory),
        "GH_REPO": "acme/app",
        "TAG": "v1.0.0",
        "GITHUB_OUTPUT": str(tmp_path / "previous-output"),
    }
    result = run_script(step(entry["jobs"]["publish"], "Find the previous release of the line")["run"], env, repo)
    assert result.returncode == 1
    assert "::error::Cannot list the releases of acme/app." in result.stdout


def publish_release(
    entry, tmp_path, exists: str, apks=("SleepHub_release_1.13.1_101350100.apk",), built_on="primary", latest="true",
    **overrides,
):
    directory = gh_directory(tmp_path, [])
    apk_directory = tmp_path / "apk"
    for name in apks:
        (apk_directory / name).parent.mkdir(parents=True, exist_ok=True)
        (apk_directory / name).write_text("apk")
    apk_directory.mkdir(exist_ok=True)
    summary = tmp_path / "summary"
    env = {
        "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}",
        "GH_DIR": str(directory),
        "GH_REPO": "acme/app",
        "GH_TOKEN": "token",
        "TAG": "v1.13.1",
        "VERSION": "1.13.1",
        "VERSION_CODE": "101350100",
        "LATEST": latest,
        "KIND": "release",
        "EXISTS": exists,
        "APK_DIRECTORY": str(apk_directory),
        "NOTES_FILE": str(tmp_path / "notes.md"),
        "BUILT_ON": built_on,
        "SELF_HOSTED": "true",
        "GITHUB_SERVER_URL": "https://github.invalid",
        "GITHUB_STEP_SUMMARY": str(summary),
        **overrides,
    }
    script = step(entry["jobs"]["publish"], "Create or update the GitHub Release")["run"]
    result = run_script(script, env, tmp_path)
    return result, (directory / "calls").read_text().splitlines(), summary


PREVIEW = {"TAG": "v2.0.0-preview.2", "VERSION": "2.0.0-preview.2", "VERSION_CODE": "200049002", "KIND": "preview"}


def test_a_preview_is_published_as_a_pre_release_that_is_not_the_latest(entry, tmp_path):
    apks = ("HeySleep_preview_2.0.0-preview.2_200049002.apk",)
    result, calls, summary = publish_release(entry, tmp_path, "false", apks=apks, latest="false", **PREVIEW)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == [
        "release create v2.0.0-preview.2 --verify-tag --title v2.0.0-preview.2 "
        f"--notes-file {tmp_path / 'notes.md'} --prerelease --latest=false {tmp_path / 'apk' / apks[0]}"
    ]
    text = summary.read_text()
    assert "### Android preview v2.0.0-preview.2" in text
    assert "Created the pre-release with HeySleep_preview_2.0.0-preview.2_200049002.apk" in text
    assert "| 2.0.0-preview.2 | 200049002 | false | a self-hosted runner |" in text


def test_a_preview_built_again_keeps_its_pre_release(entry, tmp_path):
    result, calls, _ = publish_release(entry, tmp_path, "true", apks=("a.apk",), latest="false", **PREVIEW)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == [f"release upload v2.0.0-preview.2 {tmp_path / 'apk' / 'a.apk'} --clobber"]


def test_the_plan_decides_that_a_preview_is_a_pre_release(entry):
    create = step(entry["jobs"]["publish"], "Create or update the GitHub Release")
    assert create["env"]["KIND"] == "${{ needs.plan.outputs.preview == 'true' && 'preview' || 'release' }}"
    assert create["env"]["VERSION"] == "${{ needs.plan.outputs.version }}"


def test_a_new_release_is_created_with_the_notes_and_the_apk(entry, tmp_path):
    result, calls, summary = publish_release(entry, tmp_path, "false")
    assert result.returncode == 0, result.stdout + result.stderr
    apk = tmp_path / "apk" / "SleepHub_release_1.13.1_101350100.apk"
    assert calls == [
        f"release create v1.13.1 --verify-tag --title v1.13.1 --notes-file {tmp_path / 'notes.md'} --latest=true {apk}"
    ]
    text = summary.read_text()
    assert "Created the release with SleepHub_release_1.13.1_101350100.apk: <https://github.invalid/acme/app/releases/tag/v1.13.1>" in text
    assert "| 1.13.1 | 101350100 | true | a self-hosted runner |" in text
    assert "::warning" not in result.stdout


def test_a_release_of_a_lower_line_is_not_marked_latest(entry, tmp_path):
    result, calls, summary = publish_release(entry, tmp_path, "false", latest="false")
    assert result.returncode == 0, result.stdout + result.stderr
    assert " --latest=false " in calls[0]
    assert "| 1.13.1 | 101350100 | false | a self-hosted runner |" in summary.read_text()


def test_every_apk_of_the_build_is_attached(entry, tmp_path):
    # As the artifact arrives: the directories of the flavors, one with a space.
    apks = ("sleephub/release/SleepHub_release_1.13.1_101350100.apk", "sweet sleep/release/SweetSleep_release_1.13.1_101350100.apk")
    result, calls, summary = publish_release(entry, tmp_path, "false", apks=apks)
    assert result.returncode == 0, result.stdout + result.stderr
    first, second = (str(tmp_path / "apk" / name) for name in apks)
    assert calls == [
        f"release create v1.13.1 --verify-tag --title v1.13.1 --notes-file {tmp_path / 'notes.md'} --latest=true {first} {second}"
    ]
    assert "Created the release with SleepHub_release_1.13.1_101350100.apk, SweetSleep_release_1.13.1_101350100.apk" in summary.read_text()


def test_an_existing_release_keeps_its_notes_and_gets_the_apk(entry, tmp_path):
    result, calls, summary = publish_release(entry, tmp_path, "true", apks=("SweetSleep_release_1.13.1_101350100.apk",))
    assert result.returncode == 0, result.stdout + result.stderr
    apk = tmp_path / "apk" / "SweetSleep_release_1.13.1_101350100.apk"
    # Neither the notes nor the latest mark of an existing release are touched.
    assert calls == [f"release upload v1.13.1 {apk} --clobber"]
    text = summary.read_text()
    assert "Added SweetSleep_release_1.13.1_101350100.apk to the existing release" in text
    assert "| 1.13.1 | 101350100 | unchanged | a self-hosted runner |" in text


def test_a_release_built_on_the_fallback_runner_says_so(entry, tmp_path):
    result, _, summary = publish_release(entry, tmp_path, "false", built_on="fallback")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::warning title=Self-hosted runner problem::" in result.stdout
    assert "the GitHub-hosted runner, after the self-hosted build did not pass" in summary.read_text()


def test_publish_needs_an_apk(entry, tmp_path):
    result, calls, _ = publish_release(entry, tmp_path, "false", apks=())
    assert result.returncode == 1
    assert "::error::The build artifact holds no APK." in result.stdout
    assert calls == []


def test_look_for_the_release_tells_missing_from_unreachable(entry, tmp_path):
    script = step(entry["jobs"]["publish"], "Look for the GitHub Release")["run"]
    states = {"missing": ("false", 0), "present": ("true", 0), "unreachable": ("", 1)}
    for name, (exists, code) in states.items():
        directory = gh_directory(tmp_path / name, [], release_exists=name == "present")
        if name == "unreachable":
            (directory / "release-error").write_text("gh: Bad credentials (HTTP 401)\n")
        output = tmp_path / name / "output"
        output.write_text("")
        env = {
            "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}",
            "GH_DIR": str(directory),
            "GH_REPO": "acme/app",
            "TAG": "v1.13.1",
            "GITHUB_OUTPUT": str(output),
        }
        result = run_script(script, env, tmp_path / name)
        assert result.returncode == code, name
        assert output.read_text() == (f"exists={exists}\n" if exists else "")


def uses_lines() -> list[str]:
    lines = []
    for name in (ENTRY, BUILD):
        lines += re.findall(r"^\s*(?:- )?uses: (.+)$", (WORKFLOWS / name).read_text(), re.MULTILINE)
    return lines


def test_actions_are_referenced_as_in_the_pull_request_check():
    refs: dict[str, set[str]] = {}
    for line in uses_lines():
        assert line.startswith("./.github/workflows/") or not line.startswith("./"), line
        match = OWN_ACTION.match(line)
        if match:
            assert (ROOT / match["name"] / "action.yml").is_file()
            refs.setdefault(match["name"], set()).add(match["ref"])
    assert refs == {
        "release-version": {"android-ci/v1"},
        "android-signing": {"android-ci/v1"},
        "gradle-log-verdict": {"android-ci/v1"},
        # Released before the workflows, and referenced at their own tags.
        "pick-runner": {"pick-runner/v1"},
        "release-notes": {"release-notes/v1"},
    }
    third_party = [line for line in uses_lines() if not line.startswith(("./", "actions/", "asleep-ai/actions/"))]
    assert {line.split("@")[0] for line in third_party} == {"gradle/actions/setup-gradle"}
    for line in third_party:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+", line), line
    # The same Gradle setup as the pull request check.
    assert third_party[0] in (WORKFLOWS / "android-pr-check-jobs.yml").read_text()


def test_no_secret_is_required_by_the_declaration(entry):
    secrets = trigger(entry)["secrets"]
    assert list(secrets) == [
        "KEYSTORE_BASE64", "KEYSTORE_PASSWORD", "KEY_ALIAS", "KEY_PASSWORD",
        "RUNNER_READ_TOKEN", "GIT_TOKEN", "SENTRY_AUTH_TOKEN", "OPENAI_API_KEY",
    ]
    # The signing secrets may live in the environment of the build job, where a caller cannot pass them from.
    assert all(secret["required"] is False for secret in secrets.values())
    used = set(re.findall(r"secrets\.([A-Z0-9_]+)", (WORKFLOWS / ENTRY).read_text() + (WORKFLOWS / BUILD).read_text()))
    assert used <= set(secrets)
    assert {"KEYSTORE_PASSWORD", "KEY_ALIAS", "KEY_PASSWORD"} == set(secrets) - used, "read through toJSON(secrets)"


def test_the_example_passes_only_what_the_workflow_declares(entry):
    example = yaml.safe_load(EXAMPLE.read_text())
    (job,) = example["jobs"].values()
    assert job["uses"] == f"asleep-ai/actions/.github/workflows/{ENTRY}@android-ci/v1"
    assert set(job["with"]) <= set(trigger(entry)["inputs"])
    assert job["secrets"] == "inherit"
    # The called workflow cannot be given more than the caller has.
    assert example["permissions"] == {
        "contents": "write", "packages": "read", "checks": "read", "pull-requests": "read",
    }
    # A tag a person pushed starts the release too: a release tag, or the tag of a preview. No other
    # tag does, the rolling tags of the QA builds (qa-...) included.
    assert example[True]["push"]["tags"] == ["v[0-9]+.[0-9]+.[0-9]+", "v[0-9]+.[0-9]+.[0-9]+-preview.[0-9]+"]
    assert "workflow_dispatch" in example[True]


def test_readme_lists_every_input_with_its_default(entry):
    section = README.read_text().split("## android-release.yml", 1)[1].split("\n## ", 1)[0]
    inputs = trigger(entry)["inputs"]
    rows = re.findall(r"^\| `([a-z0-9-]+)` \| (.*?) \| ", section.split("### Inputs", 1)[1].split("###", 1)[0], re.MULTILINE)
    assert [name for name, _ in rows] == list(inputs), "the table follows the order of the workflow file"
    for name, shown in rows:
        default = inputs[name]["default"]
        # A default of several lines is shown line by line.
        assert shown == ("`''`" if default == "" else "<br>".join(f"`{line}`" for line in str(default).splitlines())), name
    for name in trigger(entry)["secrets"]:
        assert f"`{name}`" in section, name
