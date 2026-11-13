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
  "api repos/acme/app/compare/"*)
    # One file per branch that exists; any other branch answers as GitHub does.
    comparison="${2#repos/acme/app/compare/}"
    branch="${comparison%%...*}"
    file="$GH_DIR/compare-$(printf '%s' "$branch" | tr '/' '_')"
    if [ ! -f "$file" ]; then echo "gh: Not Found (HTTP 404)" >&2; exit 1; fi
    cat "$file" ;;
  "release upload "*|"release create "*|"workflow run "*)
    exit "${GH_EXIT:-0}" ;;
  *)
    echo "unexpected gh call: $*" >&2; exit 97 ;;
esac
"""
SHA = "1f" * 20


def gh_directory(tmp_path: Path, tags: list[str], released=(), compare="identical", release_exists=False) -> Path:
    """A stand-in for gh: the tags, the tags with a published release, how the tagged commit compares with
    each branch (a status means main), and whether the release asked for by tag exists."""
    directory = tmp_path / "gh"
    directory.mkdir(parents=True)
    executable(directory / "gh", GH_STUB)
    (directory / "tags").write_text("".join(f"{tag}\n" for tag in tags))
    (directory / "sha").write_text(SHA + "\n")
    for branch, status in ({"main": compare} if isinstance(compare, str) else compare).items():
        (directory / f"compare-{branch.replace('/', '_')}").write_text(status + "\n")
    (directory / "calls").write_text("")
    (directory / "released").write_text("".join(f"{tag}\n" for tag in released))
    if release_exists:
        (directory / "release").write_text("v0\n")
    return directory


def check_tag(entry, tmp_path, tag: str, directory: Path, branch: str = "main"):
    output = tmp_path / "output"
    output.write_text("")
    env = {
        "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}",
        "GH_DIR": str(directory),
        "GH_REPO": "acme/app",
        "GH_TOKEN": "token",
        "TAG": tag,
        "RELEASE_BRANCH": branch,
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
    assert outputs == {"tag": "v1.10.1", "sha": SHA, "latest": "true"}
    assert "repos/acme/app/compare/main..." + SHA in (directory / "calls").read_text()
    assert "on main" in result.stdout


def test_the_first_release_is_the_latest(entry, tmp_path):
    result, outputs = check_tag(entry, tmp_path, "v0.1.0", gh_directory(tmp_path, ["skeleton-v0.1", "v0.1.0"]))
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs["latest"] == "true"


@pytest.mark.parametrize(
    "tag", ["v1.10.1-qa.3", "v1.10", "1.10.1", "develop", "v01.2.3", "v1.2.3 ", "refs/tags/v1.2.3", ""]
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


def test_a_release_of_a_lower_line_is_built_from_its_maintenance_branch(entry, tmp_path):
    # 2.0.1 exists, and 1.13.2 is released after it from release/1.x, which main never receives.
    compare = {"main": "diverged", "release/1.x": "identical"}
    directory = gh_directory(tmp_path, LINE_TAGS, released=["v1.13.0", "v1.13.1", "v2.0.0", "v2.0.1"], compare=compare)
    result, outputs = check_tag(entry, tmp_path, "v1.13.2", directory)
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"tag": "v1.13.2", "sha": SHA, "latest": "false"}
    assert "on release/1.x" in result.stdout


def test_a_release_of_the_highest_line_is_the_latest(entry, tmp_path):
    directory = gh_directory(tmp_path, LINE_TAGS, released=["v1.13.0", "v1.13.1", "v1.13.2", "v2.0.0"])
    result, outputs = check_tag(entry, tmp_path, "v2.0.1", directory)
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs["latest"] == "true"


def test_an_api_failure_is_not_read_as_no_release(entry, tmp_path):
    directory = gh_directory(tmp_path, TAGS, released=RELEASED)
    (directory / "release-error").write_text("gh: Server Error (HTTP 502)\n")
    result, _ = check_tag(entry, tmp_path, "v1.9.0", directory)
    assert result.returncode == 1
    assert "::error::Cannot list the releases of acme/app." in result.stdout


@pytest.mark.parametrize(("status", "code"), [("identical", 0), ("behind", 0), ("ahead", 1), ("diverged", 1)])
def test_the_tagged_commit_has_to_be_on_the_release_branch(entry, tmp_path, status, code):
    # No release/1.x branch exists here: that comparison answers 404.
    result, _ = check_tag(entry, tmp_path, "v1.10.1", gh_directory(tmp_path, TAGS, compare=status))
    assert result.returncode == code, result.stdout + result.stderr
    assert ("is on neither main nor release/1.x" in result.stdout) == (code == 1)


def test_a_commit_on_another_line_s_branch_is_refused(entry, tmp_path):
    compare = {"main": "diverged", "release/1.x": "ahead", "release/2.x": "identical"}
    result, _ = check_tag(entry, tmp_path, "v1.10.1", gh_directory(tmp_path, TAGS, compare=compare))
    assert result.returncode == 1
    assert "is on neither main nor release/1.x" in result.stdout


def test_an_empty_release_branch_skips_the_branch_check(entry, tmp_path):
    directory = gh_directory(tmp_path, TAGS, compare="diverged")
    result, _ = check_tag(entry, tmp_path, "v1.10.1", directory, branch="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "compare" not in (directory / "calls").read_text()


def test_plan_takes_the_version_code_from_the_one_formula(entry):
    plan = entry["jobs"]["plan"]
    code = step_by_id(plan, "code")
    assert code["uses"].startswith("asleep-ai/actions/release-version@")
    assert code["with"] == {"operation": "code", "kind": "release", "version": "${{ steps.tag.outputs.tag }}"}
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
    emptied, values = build["steps"][:2]
    # Nothing an earlier job left in the work folder is there when the secrets arrive, and the script
    # does not import from the work folder: `python3 -` alone would put it first on the module path.
    assert emptied["id"] == "workspace"
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


def test_the_checkout_is_the_tag_and_keeps_no_token(build):
    checkout = step_by_id(build, "checkout")["with"]
    assert checkout == {
        "ref": "refs/tags/${{ inputs.tag }}",
        "token": "${{ inputs.checkout-submodules != 'false' && secrets.GIT_TOKEN || github.token }}",
        "submodules": "${{ inputs.checkout-submodules }}",
        "persist-credentials": False,
    }


def test_a_moved_tag_is_refused(build, tmp_path):
    from test_pr_check import commit, git, new_repository

    repo = new_repository(tmp_path / "repo")
    head = git(repo, "rev-parse", "HEAD")
    script = step_by_id(build, "commit")["run"]
    assert run_script(script, {"TAG": "v1.0.0", "PLANNED_SHA": head}, repo).returncode == 0
    commit(repo, {"app/Main.kt": "fun main() = Unit\n"})
    result = run_script(script, {"TAG": "v1.0.0", "PLANNED_SHA": head}, repo)
    assert result.returncode == 1
    assert "The tag was moved" in result.stdout


# --- build: Gradle ------------------------------------------------------------------------


def test_the_gradle_user_home_belongs_to_the_job(build, tmp_path):
    temp = tmp_path / "temp"
    stale = temp / "gradle-user-home" / "init.d" / "evil.gradle"
    stale.parent.mkdir(parents=True)
    stale.write_text("left behind")
    github_env = tmp_path / "github-env"
    env = {"RUNNER_TEMP": str(temp), "GITHUB_ENV": str(github_env)}
    result = run_script(step_by_id(build, "gradle-home")["run"], env, tmp_path)
    assert result.returncode == 0, result.stderr
    home = temp / "gradle-user-home"
    assert github_env.read_text() == f"GRADLE_USER_HOME={home}\n"
    assert home.is_dir() and list(home.iterdir()) == []
    ids = [item.get("id") for item in build["steps"]]
    assert ids.index("gradle-home") < ids.index("gradle") < ids.index("build")
    # Nothing is restored into a build that signs, and nothing it produced is saved.
    assert step_by_id(build, "gradle")["with"] == {"cache-disabled": True}


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
        "VERSION_NAME": "1.13.1",
        "VERSION_CODE": "101350001",
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
        "-PciVersionCode=101350001",
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


def test_gradle_on_a_self_hosted_runner_limits_workers_and_memory(build, tmp_path):
    result, arguments, _ = run_gradle(build, tmp_path, runner="self-hosted")
    assert result.returncode == 0, result.stderr
    assert arguments[3:] == [
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
    assert step_by_id(build, "build")["env"]["SENTRY_AUTH_TOKEN"] == "${{ secrets.SENTRY_AUTH_TOKEN }}"
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
    "app/build/outputs/apk/sleephub/release/SleepHub_release_1.13.1_101350001.apk",
    "app/build/outputs/apk/sweet sleep/release/SweetSleep_release_1.13.1_101350001.apk",
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
        "APK_DIRECTORY": "app/build/outputs/apk",
        "VERSION_NAME": "1.13.1",
        "VERSION_CODE": "101350001",
        "BUILD_TOOLS_VERSION": "",
        "ANDROID_HOME": str(sdk),
        "APK_NAME": "1.13.1",
        "APK_CODE": "101350001",
        "GITHUB_OUTPUT": str(output),
        **overrides,
    }
    result = run_script(step_by_id(build, "apk")["run"], env, workspace)
    lines = output.read_text().splitlines()
    paths = lines[1:-1] if lines else None
    if lines:
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
        assert f"{name}: versionName=1.13.1 versionCode=101350001" in result.stdout


@pytest.mark.parametrize(
    ("built", "message"),
    [
        ({"APK_NAME": "0.0.0-dev", "APK_CODE": "1"}, "App_1.13.1.apk is version 0.0.0-dev (1), and the release is 1.13.1 (101350001)."),
        ({"APK_CODE": "101300001"}, "App_1.13.1.apk is version 1.13.1 (101300001)"),
        ({"APK_NAME": "1.13.1-qa.3"}, "App_1.13.1.apk is version 1.13.1-qa.3 (101350001)"),
    ],
)
def test_an_apk_with_another_version_is_refused(build, tmp_path, built, message):
    result, paths = check_apks(build, tmp_path, **built)
    assert result.returncode == 1
    assert message in result.stdout
    assert "-PciVersionName and -PciVersionCode" in result.stdout
    assert paths is None


def test_one_flavor_with_another_version_refuses_the_release(build, tmp_path):
    result, paths = check_apks(build, tmp_path, apks=FLAVOR_APKS, APK_OTHER="SweetSleep_release_1.13.1_101350001.apk")
    assert result.returncode == 1
    assert "SweetSleep_release_1.13.1_101350001.apk is version 0.0.0-dev (1)" in result.stdout
    assert result.stdout.count("::error::") == 1
    assert paths is None


def test_a_build_that_left_no_apk_is_refused(build, tmp_path):
    result, paths = check_apks(build, tmp_path, apks=())
    assert result.returncode == 1
    assert "::error::ciRelease left no APK under app/build/outputs/apk." in result.stdout
    assert paths is None


@pytest.mark.parametrize("directory", ["app/build/outputs/elsewhere", "", "-delete"])
def test_the_apk_directory_has_to_exist(build, tmp_path, directory):
    result, paths = check_apks(build, tmp_path, APK_DIRECTORY=directory)
    assert result.returncode == 1
    assert "is not a directory after the build" in result.stdout
    assert paths is None
    assert (tmp_path / "workspace" / ONE_APK[0]).exists()


def test_apks_that_would_collide_as_release_assets_are_refused(build, tmp_path):
    apks = ("app/build/outputs/apk/one/release/app-release.apk", "app/build/outputs/apk/two/release/app-release.apk")
    result, paths = check_apks(build, tmp_path, apks=apks)
    assert result.returncode == 1
    assert "share a file name: app-release.apk" in result.stdout
    assert paths is None


def test_aapt2_comes_from_the_named_build_tools_version(build, tmp_path):
    result, _ = check_apks(build, tmp_path, BUILD_TOOLS_VERSION="35.0.0")
    assert result.returncode == 0, result.stdout + result.stderr
    result, _ = check_apks(build, tmp_path / "missing", BUILD_TOOLS_VERSION="34.0.0")
    assert result.returncode == 1
    assert "aapt2 was not found" in result.stdout and "(version 34.0.0)" in result.stdout


def test_without_an_sdk_the_apks_are_not_passed_unchecked(build, tmp_path):
    result, paths = check_apks(build, tmp_path, ANDROID_HOME=str(tmp_path / "no-sdk"))
    assert result.returncode == 1
    assert "No Android SDK with build-tools" in result.stdout
    assert paths is None


def test_the_apks_are_verified_uploaded_and_the_secrets_deleted(build):
    names = [item.get("name") for item in build["steps"]]
    order = [
        "Run Gradle",
        "Check the APKs",
        "Verify the APK signatures",
        "Upload the signed APKs",
        "Delete the keystore and local.properties",
        "Empty the work folder again",
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
    assert upload["with"]["name"] == "android-release-apk-${{ inputs.attempt }}"
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
    assert verdict["if"] == "${{ always() && steps.values.outcome != 'failure' }}"
    ids = [item.get("id") for item in steps]
    before = [identifier for identifier in ids[: ids.index("build")] if identifier != "values"]
    assert None not in before, "a step before the build without an id cannot be reported"
    reported = re.findall(r"\$\{\{ steps\.([a-z-]+)\.outcome \}\}", verdict["with"]["setup-outcome"])
    assert reported == before
    assert re.sub(r"\$\{\{ steps\.[a-z-]+\.outcome \}\}", "", verdict["with"]["setup-outcome"]).strip() == ""
    assert verdict["with"]["build-outcome"] == "${{ steps.build.outcome }}"
    assert verdict["with"]["job-status"] == "${{ job.status }}"
    # A step that may be skipped would read as a failed setup.
    for item in steps[: ids.index("build") + 1]:
        assert "if" not in item, item.get("name")
    # A missing secret is final: the other runner would miss it too. The verdict is the only output:
    # the runner drops a job output that contains a secret's value, and a file or artifact name can.
    assert build["outputs"] == {
        "verdict": "${{ steps.values.outcome == 'failure' && 'code' || steps.verdict.outputs.verdict }}"
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
    assert set(primary["with"]) == set(fallback["with"]) == set(inner["inputs"])
    assert primary["with"]["attempt"] == "primary" and fallback["with"]["attempt"] == "fallback"
    planned = {"tag": "tag", "sha": "sha", "version-name": "version", "version-code": "version-code"}
    for name, output in planned.items():
        assert primary["with"][name] == fallback["with"][name] == f"${{{{ needs.plan.outputs.{output} }}}}"
    forwarded = set(inner["inputs"]) - {"runs-on", "attempt", *planned}
    assert forwarded == set(inputs) - {"tag", "runner-labels", "fallback-runs-on", "release-branch", "release-notes-prompt"}
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
    assert entry["jobs"]["plan"]["permissions"] == {"contents": "read"}
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
    entry, tmp_path, exists: str, apks=("SleepHub_release_1.13.1_101350001.apk",), built_on="primary", latest="true"
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
        "VERSION_CODE": "101350001",
        "LATEST": latest,
        "EXISTS": exists,
        "APK_DIRECTORY": str(apk_directory),
        "NOTES_FILE": str(tmp_path / "notes.md"),
        "BUILT_ON": built_on,
        "SELF_HOSTED": "true",
        "GITHUB_SERVER_URL": "https://github.invalid",
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    script = step(entry["jobs"]["publish"], "Create or update the GitHub Release")["run"]
    result = run_script(script, env, tmp_path)
    return result, (directory / "calls").read_text().splitlines(), summary


def test_a_new_release_is_created_with_the_notes_and_the_apk(entry, tmp_path):
    result, calls, summary = publish_release(entry, tmp_path, "false")
    assert result.returncode == 0, result.stdout + result.stderr
    apk = tmp_path / "apk" / "SleepHub_release_1.13.1_101350001.apk"
    assert calls == [
        f"release create v1.13.1 --verify-tag --title v1.13.1 --notes-file {tmp_path / 'notes.md'} --latest=true {apk}"
    ]
    text = summary.read_text()
    assert "Created the release with SleepHub_release_1.13.1_101350001.apk: <https://github.invalid/acme/app/releases/tag/v1.13.1>" in text
    assert "| 1.13.1 | 101350001 | true | a self-hosted runner |" in text
    assert "::warning" not in result.stdout


def test_a_release_of_a_lower_line_is_not_marked_latest(entry, tmp_path):
    result, calls, summary = publish_release(entry, tmp_path, "false", latest="false")
    assert result.returncode == 0, result.stdout + result.stderr
    assert " --latest=false " in calls[0]
    assert "| 1.13.1 | 101350001 | false | a self-hosted runner |" in summary.read_text()


def test_every_apk_of_the_build_is_attached(entry, tmp_path):
    # As the artifact arrives: the directories of the flavors, one with a space.
    apks = ("sleephub/release/SleepHub_release_1.13.1_101350001.apk", "sweet sleep/release/SweetSleep_release_1.13.1_101350001.apk")
    result, calls, summary = publish_release(entry, tmp_path, "false", apks=apks)
    assert result.returncode == 0, result.stdout + result.stderr
    first, second = (str(tmp_path / "apk" / name) for name in apks)
    assert calls == [
        f"release create v1.13.1 --verify-tag --title v1.13.1 --notes-file {tmp_path / 'notes.md'} --latest=true {first} {second}"
    ]
    assert "Created the release with SleepHub_release_1.13.1_101350001.apk, SweetSleep_release_1.13.1_101350001.apk" in summary.read_text()


def test_an_existing_release_keeps_its_notes_and_gets_the_apk(entry, tmp_path):
    result, calls, summary = publish_release(entry, tmp_path, "true", apks=("SweetSleep_release_1.13.1_101350001.apk",))
    assert result.returncode == 0, result.stdout + result.stderr
    apk = tmp_path / "apk" / "SweetSleep_release_1.13.1_101350001.apk"
    # Neither the notes nor the latest mark of an existing release are touched.
    assert calls == [f"release upload v1.13.1 {apk} --clobber"]
    text = summary.read_text()
    assert "Added SweetSleep_release_1.13.1_101350001.apk to the existing release" in text
    assert "| 1.13.1 | 101350001 | unchanged | a self-hosted runner |" in text


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
    # The signing secrets live in the environment of the build job, where a caller cannot pass them from.
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
    assert example["permissions"] == {"contents": "write", "packages": "read", "pull-requests": "read"}
    assert example[True]["push"]["tags"] == ["v[0-9]+.[0-9]+.[0-9]+"]
    assert "workflow_dispatch" in example[True]


def test_readme_lists_every_input_with_its_default(entry):
    section = README.read_text().split("## android-release.yml", 1)[1].split("\n## ", 1)[0]
    inputs = trigger(entry)["inputs"]
    rows = re.findall(r"^\| `([a-z0-9-]+)` \| (.*?) \| ", section.split("### Inputs", 1)[1].split("###", 1)[0], re.MULTILINE)
    assert [name for name, _ in rows] == list(inputs), "the table follows the order of the workflow file"
    for name, shown in rows:
        default = inputs[name]["default"]
        assert shown == ("`''`" if default == "" else f"`{default}`"), name
    for name in trigger(entry)["secrets"]:
        assert f"`{name}`" in section, name
