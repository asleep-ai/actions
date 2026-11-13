"""Tests of android-pr-check.yml and android-pr-check-jobs.yml.

The run steps are executed as the shell GitHub would run, against throwaway
git repositories and stand-ins for gradlew and keytool. The wiring tests keep
the copies that have to match in step: the verify and release-build jobs, and
the primary and fallback calls. GitHub's expressions (job conditions, runs-on)
are not evaluated here; those tests only pin the text.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ENTRY = "android-pr-check.yml"
JOBS = "android-pr-check-jobs.yml"
EXAMPLE = ROOT / "android-ci" / "pr-check.yml.example"
OWN_ACTION = re.compile(r"^asleep-ai/actions/(?P<name>[^@/]+)@(?P<ref>.+)$")


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def trigger(workflow: dict) -> dict:
    # YAML 1.1 reads the key `on` as the boolean true.
    return workflow[True]["workflow_call"]


def step(job: dict, name: str) -> dict:
    matches = [candidate for candidate in job["steps"] if candidate.get("name") == name]
    assert len(matches) == 1, f"{name!r}: {len(matches)} steps"
    return matches[0]


def checkout_step(job: dict) -> dict:
    (checkout,) = [item for item in job["steps"] if item.get("id") == "checkout"]
    return checkout


def run_script(script: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a step's script the way `shell: bash` does."""
    environment = {"PATH": os.environ["PATH"], "HOME": str(cwd), **env}
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture(scope="module")
def entry() -> dict:
    return load(ENTRY)


@pytest.fixture(scope="module")
def jobs() -> dict:
    return load(JOBS)["jobs"]


# --- result -----------------------------------------------------------------

PASS = ("pass", "pass", "pass")
NONE = ("", "", "")
LABELS = '["self-hosted","Linux","X64"]'


def result_env(
    self_hosted, primary, primary_verdicts, fallback, fallback_verdicts, labels=LABELS, pick_runner="success"
) -> dict[str, str]:
    return {
        "EVENT_NAME": "pull_request",
        "RUNNER_LABELS": labels,
        "FALLBACK_RUNS_ON": "ubuntu-latest",
        "PICK_RUNNER": pick_runner,
        "SELF_HOSTED": self_hosted,
        "PRIMARY": primary,
        "PRIMARY_CODE_STYLE": primary_verdicts[0],
        "PRIMARY_VERIFY": primary_verdicts[1],
        "PRIMARY_RELEASE_BUILD": primary_verdicts[2],
        "FALLBACK": fallback,
        "FALLBACK_CODE_STYLE": fallback_verdicts[0],
        "FALLBACK_VERIFY": fallback_verdicts[1],
        "FALLBACK_RELEASE_BUILD": fallback_verdicts[2],
    }


RESULT_CASES = {
    # name: (env, exit status, text the log must contain, text it must not contain)
    "self-hosted pass": (
        result_env("true", "success", PASS, "skipped", NONE),
        0,
        "Passed on the self-hosted runners.",
        "::warning",
    ),
    "self-hosted unchecked pass": (
        result_env("", "success", PASS, "skipped", NONE, pick_runner="failure"),
        0,
        "Passed on the self-hosted runners.",
        "::warning",
    ),
    "runners offline": (
        result_env("false", "success", PASS, "skipped", NONE),
        0,
        "::warning title=Self-hosted runners offline::",
        "::error",
    ),
    "github-hosted only": (
        result_env("", "success", PASS, "skipped", NONE, labels="", pick_runner="skipped"),
        0,
        "Passed on ubuntu-latest.",
        "::warning",
    ),
    "pull request from a fork": (
        result_env("", "success", PASS, "skipped", NONE, pick_runner="skipped"),
        0,
        "Passed on ubuntu-latest (a pull request from a fork does not run on the self-hosted runners).",
        "::warning",
    ),
    "pull request from a fork fails once": (
        result_env("", "failure", ("pass", "infra", "pass"), "skipped", NONE, pick_runner="skipped"),
        1,
        "::error::Failed on ubuntu-latest (a pull request from a fork",
        "Passed",
    ),
    "code failure is final": (
        result_env("true", "failure", ("pass", "code", "pass"), "skipped", NONE),
        1,
        "::error::Failed on the self-hosted runners (result=failure, code-style=pass, verify=code, release-build=pass).",
        "::warning",
    ),
    "infra failure passes on the re-run": (
        result_env("true", "failure", ("pass", "infra", "pass"), "success", PASS),
        0,
        "::warning title=Self-hosted runner problem::",
        "::error",
    ),
    "runner went away, re-run passes": (
        result_env("true", "failure", ("pass", "", "pass"), "success", PASS),
        0,
        "verify=none",
        "::error",
    ),
    "job timeout, re-run passes": (
        result_env("true", "cancelled", ("pass", "infra", "pass"), "success", PASS),
        0,
        "Passed on the re-run on ubuntu-latest.",
        "::error",
    ),
    "re-run fails too": (
        result_env("true", "failure", ("pass", "infra", "pass"), "failure", ("pass", "code", "pass")),
        1,
        "::error::Did not pass on the self-hosted runners",
        "Passed",
    ),
    "re-run hits its timeout": (
        result_env("true", "failure", ("pass", "infra", "pass"), "cancelled", ("pass", "", "")),
        1,
        "::error::Did not pass on the self-hosted runners",
        "Passed",
    ),
    "cancelled run": (
        result_env("true", "cancelled", NONE, "skipped", NONE),
        1,
        "::error::Did not finish on the self-hosted runners",
        "Passed",
    ),
    "nothing ran": (
        result_env("", "skipped", NONE, "skipped", NONE, pick_runner="cancelled"),
        1,
        "::error::Did not finish",
        "Passed",
    ),
    "github-hosted infra failure is not re-run": (
        result_env("false", "failure", ("pass", "infra", "pass"), "skipped", NONE),
        1,
        "::error::Failed on ubuntu-latest (no self-hosted runner was online)",
        "Passed",
    ),
    "github-hosted timeout is not re-run": (
        result_env("false", "cancelled", ("pass", "infra", "pass"), "skipped", NONE),
        1,
        "::error::Did not finish on ubuntu-latest (no self-hosted runner was online)",
        "Passed",
    ),
    # The verdict speaks for the build step, the result for the job: neither alone is a pass.
    "pass verdicts with a failed result": (
        result_env("", "failure", PASS, "skipped", NONE, labels="", pick_runner="skipped"),
        1,
        "::error::Failed on ubuntu-latest (result=failure",
        "Passed",
    ),
    "success without a verdict": (
        result_env("true", "success", ("pass", "", "pass"), "skipped", NONE),
        1,
        "succeeded without a pass verdict from every job",
        "Passed",
    ),
    "success with a code verdict": (
        result_env("true", "success", ("pass", "code", "pass"), "skipped", NONE),
        1,
        "succeeded without a pass verdict from every job",
        "Passed",
    ),
    "re-run success without a verdict": (
        result_env("true", "failure", ("pass", "infra", "pass"), "success", ("pass", "pass", "")),
        1,
        "The re-run on ubuntu-latest succeeded without a pass verdict from every job",
        "Passed",
    ),
}


@pytest.mark.parametrize("case", RESULT_CASES)
def test_result(entry, tmp_path, case):
    env, status, expected, unexpected = RESULT_CASES[case]
    summary = tmp_path / "summary.md"
    script = step(entry["jobs"]["result"], "Combine the runs")["run"]
    result = run_script(script, {**env, "GITHUB_STEP_SUMMARY": str(summary)}, tmp_path)
    output = result.stdout + result.stderr
    assert result.returncode == status, output
    assert expected in output
    assert unexpected not in output
    rows = summary.read_text()
    assert f"| primary | {env['PRIMARY'] or 'none'} |" in rows
    assert f"| fallback | {env['FALLBACK'] or 'none'} |" in rows


def test_result_refuses_pull_request_target(entry, tmp_path):
    # Nothing ran: primary is skipped for this event, whose default checkout is the base branch.
    env = result_env("", "skipped", NONE, "skipped", NONE, pick_runner="skipped")
    env["EVENT_NAME"] = "pull_request_target"
    script = step(entry["jobs"]["result"], "Combine the runs")["run"]
    result = run_script(script, {**env, "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md")}, tmp_path)
    assert result.returncode == 1
    assert "::error::pull_request_target is not supported" in result.stdout
    assert entry["jobs"]["primary"]["if"] == "${{ !cancelled() && github.event_name != 'pull_request_target' }}"


def test_result_reads_every_job_of_both_runs(entry):
    result = entry["jobs"]["result"]
    assert result["if"] == "${{ always() }}", "a skipped result job would count as a passing required check"
    assert result["needs"] == ["pick-runner", "primary", "fallback"]
    assert result["runs-on"] == "ubuntu-latest"
    env = step(result, "Combine the runs")["env"]
    assert env["PICK_RUNNER"] == "${{ needs.pick-runner.result }}"
    for run in ("primary", "fallback"):
        assert env[run.upper()] == f"${{{{ needs.{run}.result }}}}"
        for job in ("code-style", "verify", "release-build"):
            name = f"{run}_{job}".upper().replace("-", "_")
            assert env[name] == f"${{{{ needs.{run}.outputs.{job}-verdict }}}}"


# --- checkout -----------------------------------------------------------------


@pytest.mark.parametrize("job", ["code-style", "verify", "release-build"])
def test_stale_git_locks_are_removed_before_checkout_on_self_hosted_runners(jobs, job):
    first, second = jobs[job]["steps"][:2]
    assert first["name"] == "Remove stale git lock files"
    assert first["if"] == "${{ runner.environment == 'self-hosted' }}"
    assert second is checkout_step(jobs[job])
    assert first["run"] == jobs["verify"]["steps"][0]["run"]


def test_stale_git_locks_of_the_repository_and_its_submodules_are_removed(jobs, tmp_path):
    workspace = tmp_path / "work folder"
    git_directory = workspace / ".git"
    submodule = git_directory / "modules" / "external" / "content assets"
    locks = [
        git_directory / "index.lock",
        git_directory / "shallow.lock",
        git_directory / "refs" / "heads" / "main.lock",
        submodule / "index.lock",
        submodule / "config.lock",
        submodule / "modules" / "nested" / "HEAD.lock",
    ]
    kept = [
        git_directory / "index",
        git_directory / "config",
        submodule / "index",
        submodule / "HEAD",
        # Lock files of package managers are part of the checkout, not git's.
        workspace / "yarn.lock",
        workspace / "external" / "content assets" / "Podfile.lock",
    ]
    for path in locks + kept:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    # A directory is never a lock.
    (git_directory / "modules" / "odd.lock").mkdir()
    script = jobs["verify"]["steps"][0]["run"]
    result = run_script(script, {"GITHUB_WORKSPACE": str(workspace)}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert [path for path in locks if path.exists()] == []
    assert [path for path in kept if not path.exists()] == []
    assert (git_directory / "modules" / "odd.lock").is_dir()
    assert "Removing .git/modules/external/content assets/index.lock, left by an earlier job" in result.stdout
    assert result.stdout.count("Removing ") == len(locks)


def test_stale_git_lock_removal_does_nothing_in_a_fresh_work_folder(jobs, tmp_path):
    script = jobs["verify"]["steps"][0]["run"]
    for workspace in (tmp_path / "empty", tmp_path / "clean"):
        workspace.mkdir()
    (tmp_path / "clean" / ".git").mkdir()
    (tmp_path / "clean" / ".git" / "index").write_text("")
    for workspace in (tmp_path / "empty", tmp_path / "clean"):
        result = run_script(script, {"GITHUB_WORKSPACE": str(workspace)}, tmp_path)
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""


@pytest.mark.parametrize("job", ["verify", "release-build"])
def test_gradle_job_checkout(jobs, job):
    checkout = checkout_step(jobs[job])["with"]
    # The plan step compares a pull request's merge commit with its first parent.
    assert checkout["fetch-depth"] == 2
    # No step fetches anything after the checkout, so no code of the build finds a token in the clone.
    assert checkout["persist-credentials"] is False
    # GIT_TOKEN only where submodules need it: the job token reads the repository itself.
    assert checkout["token"] == "${{ inputs.checkout-submodules != 'false' && secrets.GIT_TOKEN || github.token }}"
    assert checkout["submodules"] == "${{ inputs.checkout-submodules }}"


# --- the plan step: which changes need a build ---------------------------------

DEFAULT_IGNORE = "*.md\ndocs/**\n.gitignore\nLICENSE\n.github/ISSUE_TEMPLATE/**\n.github/PULL_REQUEST_TEMPLATE.md\n"


def git(repo: Path, *args: str) -> str:
    command = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]
    result = subprocess.run([*command, *args], cwd=repo, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def commit(repo: Path, changes: dict[str, str | None], message: str = "change") -> str:
    """Write (or, for None, delete) the given files and commit them."""
    for name, content in changes.items():
        path = repo / name
        if content is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def new_repository(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    commit(path, {"app/Main.kt": "fun main() {}\n", "README.md": "# app\n", "docs/guide.md": "guide\n"}, "base")
    return path


def merged_pull_request(tmp_path: Path, changes: dict[str, str | None]) -> Path:
    """A checkout like a pull_request run's: HEAD is the merge commit of the change into the base branch."""
    repo = new_repository(tmp_path / "repo")
    git(repo, "checkout", "-q", "-b", "feature")
    commit(repo, changes)
    git(repo, "checkout", "-q", "main")
    commit(repo, {"app/Other.kt": "val other = 1\n"}, "the base branch moved on")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge", "feature")
    return repo


def plan(jobs, repo: Path, job: str = "verify", **env: str) -> tuple[str, str]:
    output = repo.parent / "github-output"
    output.write_text("")
    environment = {
        "EVENT_NAME": "pull_request",
        "REF": "refs/pull/1/merge",
        "PATHS_IGNORE": DEFAULT_IGNORE,
        "QA_SOURCE_BRANCH": "",
        "GITHUB_OUTPUT": str(output),
        **env,
    }
    result = run_script(step(jobs[job], "Decide whether to build")["run"], environment, repo)
    assert result.returncode == 0, result.stdout + result.stderr
    decision = output.read_text()
    assert decision in ("build=true\n", "build=false\n")
    assert result.stdout.startswith(decision.strip() + ": ")
    return decision.strip().split("=")[1], result.stdout


@pytest.mark.parametrize(
    ("changes", "build", "reason"),
    [
        ({"app/Main.kt": "fun main() = Unit\n"}, "true", "1 of 1 changed file(s) are outside paths-ignore"),
        ({"README.md": "# new\n", "docs/a/b.png": "x", "LICENSE": "x"}, "false", "all 3 changed file(s) match"),
        ({"README.md": "# new\n", "app/New.kt": "val x = 1\n"}, "true", "the first being app/New.kt"),
        # *.md is the repository root only: Markdown elsewhere may be packaged into the app.
        ({"feature/onboarding/src/main/assets/terms.md": "terms\n"}, "true", "1 of 1 changed file(s)"),
        ({".github/ISSUE_TEMPLATE/bug.md": "x", ".github/PULL_REQUEST_TEMPLATE.md": "x"}, "false", "all 2 changed"),
        ({".github/workflows/pr-check.yml": "name: x\n"}, "true", "the first being .github/workflows/pr-check.yml"),
        # A deletion is a change of that path.
        ({"docs/guide.md": None}, "false", "all 1 changed file(s) match"),
        ({"app/Main.kt": None}, "true", "the first being app/Main.kt"),
        ({"docs/it's here.md": "x", "docs/sp ace/한글.md": "x"}, "false", "all 2 changed file(s) match"),
    ],
)
def test_plan_of_a_pull_request_reads_the_merge_commit(jobs, tmp_path, changes, build, reason):
    repo = merged_pull_request(tmp_path, changes)
    decision, log = plan(jobs, repo)
    assert (decision, reason in log) == (build, True), log


def test_plan_does_not_count_what_the_base_branch_gained(jobs, tmp_path):
    # merged_pull_request adds app/Other.kt on the base branch; a docs-only pull request stays docs-only.
    repo = merged_pull_request(tmp_path, {"docs/guide.md": "more\n"})
    assert plan(jobs, repo)[0] == "false"


@pytest.mark.parametrize(
    ("paths_ignore", "path", "build"),
    [
        ("", "README.md", "true"),
        ("**/*.md", "feature/a/terms.md", "false"),
        ("**/*.md", "README.md", "false"),
        ("notes/**", "notes", "true"),
        ("docs/**", "documents/a.md", "true"),
        ("  docs/**  \r\n\r\n*.txt", "notes.txt", "false"),
        ("*.txt", "a/notes.txt", "true"),
        ("app/?.kt", "app/A.kt", "false"),
        ("app/?.kt", "app/AB.kt", "true"),
        # A pattern is matched as a whole path, and its dots are literal.
        ("LICENSE", "LICENSE.txt", "true"),
        ("*.md", "READMExmd", "true"),
    ],
)
def test_plan_glob_syntax(jobs, tmp_path, paths_ignore, path, build):
    repo = merged_pull_request(tmp_path, {path: "changed\n"})
    assert plan(jobs, repo, PATHS_IGNORE=paths_ignore)[0] == build


def test_plan_without_a_first_parent_builds(jobs, tmp_path):
    # A checkout that is not two commits deep cannot be compared: build, never skip.
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, {"docs/guide.md": "guide\n"})
    decision, log = plan(jobs, repo)
    assert decision == "true"
    assert "git cannot list the changed files" in log


def pushed(tmp_path: Path, changes: dict[str, str | None]) -> Path:
    """A checkout like a push run's: HEAD is the pushed commit, on top of the branch's previous tip."""
    repo = new_repository(tmp_path / "repo")
    commit(repo, changes, "pushed commit")
    return repo


@pytest.mark.parametrize(
    "changes",
    [
        {"app/Main.kt": "fun main() = Unit\n"},
        # The check of a pushed commit is what a release of it relies on: documentation only, or
        # nothing at all (a merge commit that changes no file), is still built.
        {"docs/guide.md": "more\n"},
        {"README.md": "# new\n", "LICENSE": "x"},
        {},
    ],
)
@pytest.mark.parametrize("ref",["refs/heads/develop", "refs/heads/main", "refs/heads/release/1.x", "refs/heads/feature"])
@pytest.mark.parametrize("job", ["verify", "release-build"])
def test_plan_of_a_push_always_builds(jobs, tmp_path, changes, ref, job):
    # With no branch left to the QA build (the verify job always, the release build with
    # release-build-on-push), every push is built, whatever it changed.
    repo = pushed(tmp_path, changes)
    decision, log = plan(jobs, repo, job=job, EVENT_NAME="push", REF=ref)
    assert decision == "true"
    assert "a push always builds" in log


def test_plan_of_a_push_does_not_read_the_previous_tip(jobs):
    # A push run that compared with the previous tip passed a documentation-only commit whose
    # predecessor never passed. Neither Gradle job takes the previous tip any more.
    for job in ("verify", "release-build"):
        decide = step(jobs[job], "Decide whether to build")
        assert set(decide["env"]) == {"EVENT_NAME", "REF", "PATHS_IGNORE", "QA_SOURCE_BRANCH"}
        assert "github.event.before" not in str(jobs[job])
        assert "fetch" not in decide["run"]


@pytest.mark.parametrize("event", ["workflow_dispatch", "schedule", "merge_group"])
def test_plan_of_other_events_always_builds(jobs, tmp_path, event):
    repo = merged_pull_request(tmp_path, {"docs/guide.md": "more\n"})
    decision, log = plan(jobs, repo, EVENT_NAME=event, REF="refs/heads/develop", QA_SOURCE_BRANCH="develop")
    assert decision == "true"
    assert f"a {event} run always builds" in log


@pytest.mark.parametrize(
    ("event", "ref", "source", "build"),
    [
        # The branches android-qa.yml builds: the source branch and release/<major>.x.
        ("push", "refs/heads/develop", "develop", "false"),
        ("push", "refs/heads/release/1.x", "develop", "false"),
        ("push", "refs/heads/release/12.x", "main", "false"),
        ("push", "refs/heads/main", "main", "false"),
        # Every other push has no QA build, and keeps its release build.
        ("push", "refs/heads/main", "develop", "true"),
        ("push", "refs/heads/develop-2", "develop", "true"),
        ("push", "refs/heads/feature/develop", "develop", "true"),
        ("push", "refs/heads/release/1.x/fix", "develop", "true"),
        ("push", "refs/heads/release/next", "develop", "true"),
        ("push", "refs/heads/release/01.x", "develop", "true"),
        ("push", "refs/tags/develop", "develop", "true"),
        ("push", "refs/tags/release/1.x", "develop", "true"),
        # release-build-on-push: no branch is left to the QA build.
        ("push", "refs/heads/develop", "", "true"),
        ("push", "refs/heads/release/1.x", "", "true"),
        # A pull request into such a branch is built as ever: R8 breakage surfaces before the merge.
        ("pull_request", "refs/heads/develop", "develop", "true"),
        ("pull_request", "refs/pull/1/merge", "develop", "true"),
    ],
)
def test_the_release_build_of_a_push_is_left_to_the_qa_build_of_that_push(jobs, tmp_path, event, ref, source, build):
    if event == "push":
        repo = pushed(tmp_path, {"app/Main.kt": "fun main() = Unit\n"})
    else:
        repo = merged_pull_request(tmp_path, {"app/Main.kt": "fun main() = Unit\n"})
    decision, log = plan(jobs, repo, job="release-build", EVENT_NAME=event, REF=ref, QA_SOURCE_BRANCH=source)
    assert decision == build
    branch = ref.split("refs/heads/", 1)[-1]
    left = f"the release variant of a push to {branch} is built by the QA build of the same push (android-qa.yml)"
    assert (left in log) == (build == "false")
    if build == "false":
        assert "a release requires the check of that build next to this one" in log


def test_only_the_release_build_leaves_a_push_to_the_qa_build(entry, jobs):
    # The verify job builds and tests every push: its check is half of what a release requires.
    assert step(jobs["verify"], "Decide whether to build")["env"]["QA_SOURCE_BRANCH"] == ""
    assert step(jobs["release-build"], "Decide whether to build")["env"]["QA_SOURCE_BRANCH"] == (
        "${{ !inputs.release-build-on-push && (inputs.source-branch || github.event.repository.default_branch) || '' }}"
    )
    env = step(jobs["verify"], "Decide whether to build")["env"]
    assert env["PATHS_IGNORE"] == "${{ inputs.paths-ignore }}"
    inputs = trigger(entry)["inputs"]
    assert inputs["release-build-on-push"] == {
        **inputs["release-build-on-push"], "type": "boolean", "default": False, "required": False,
    }
    assert inputs["source-branch"]["default"] == ""
    assert "release-build-skip-push-branches" not in inputs
    for run in ("primary", "fallback"):
        passed = entry["jobs"][run]["with"]
        assert passed["release-build-on-push"] == "${{ inputs.release-build-on-push }}"
        assert passed["source-branch"] == "${{ inputs.source-branch }}"


def test_what_the_push_build_no_longer_covers_is_required_of_a_release_by_default(entry):
    # The other half of the rule, in the two workflows that release: a commit is tagged and built
    # for release only when the QA build of its push succeeded too. The defaults change together,
    # so a caller that takes them cannot end up with a release whose variant no job has built.
    assert trigger(entry)["inputs"]["release-build-on-push"]["default"] is False
    for name in ("android-version-bump.yml", "android-release.yml"):
        assert trigger(load(name))["inputs"]["required-checks"]["default"].splitlines() == [
            "pr-check / result", "qa / built",
        ], name
    # "qa / built" is the job `built` of android-qa.yml under a calling job named qa, for a push.
    qa = load("android-qa.yml")["jobs"]["built"]
    assert qa["name"] == "${{ github.event_name == 'push' && 'built' || 'built (manual run)' }}"
    example = yaml.safe_load((ROOT / "android-ci" / "qa.yml.example").read_text())
    assert list(example["jobs"]) == ["qa"]


def test_plan_of_the_default_paths_ignore(entry, jobs, tmp_path):
    default = trigger(entry)["inputs"]["paths-ignore"]["default"]
    assert default == DEFAULT_IGNORE
    repo = merged_pull_request(tmp_path, {"README.md": "x", ".gitignore": "x", "docs/a.md": "x"})
    assert plan(jobs, repo, PATHS_IGNORE=default)[0] == "false"


# --- local.properties ----------------------------------------------------------

KEYTOOL_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$KEYTOOL_ARGS"
echo "keytool output on stdout must not reach local.properties"
while [ "$#" -gt 0 ]; do
  if [ "$1" = -keystore ]; then echo stub > "$2"; fi
  shift
done
"""


def write_local_properties(jobs, tmp_path, local_properties: str, signing_key="true", real_keytool=False):
    workspace = tmp_path / "workspace"
    temp = tmp_path / "temp"
    bin_directory = tmp_path / "bin"
    for directory in (workspace, temp, bin_directory):
        directory.mkdir()
    path = os.environ["PATH"]
    if not real_keytool:
        executable(bin_directory / "keytool", KEYTOOL_STUB)
        path = f"{bin_directory}{os.pathsep}{path}"
    env = {
        "PATH": path,
        "RUNNER_TEMP": str(temp),
        "LOCAL_PROPERTIES": local_properties,
        "SIGNING_KEY": signing_key,
        "KEYTOOL_ARGS": str(tmp_path / "keytool-args"),
    }
    script = step(jobs["verify"], "Write local.properties")["run"]
    return run_script(script, env, workspace), workspace, temp


def test_local_properties_holds_the_signing_keys_and_the_caller_lines(jobs, tmp_path):
    caller = "# placeholders\nAPI_KEY=ci-placeholder\n\nEMPTY=\r\nBASE_URL=https://ci.placeholder.invalid/?a=b c\n"
    result, workspace, temp = write_local_properties(jobs, tmp_path, caller)
    assert result.returncode == 0, result.stderr
    keystore = temp / "android-pr-check.jks"
    assert keystore.is_file()
    assert (workspace / "local.properties").read_text().splitlines() == [
        f"KEYSTORE_FILE={keystore}",
        "KEYSTORE_PASSWORD=android-pr-check",
        "KEY_ALIAS=android-pr-check",
        "KEY_PASSWORD=android-pr-check",
        "API_KEY=ci-placeholder",
        "EMPTY=",
        "BASE_URL=https://ci.placeholder.invalid/?a=b c",
    ]
    arguments = (tmp_path / "keytool-args").read_text().splitlines()
    assert arguments[0] == "-genkeypair"
    assert arguments[arguments.index("-keystore") + 1] == str(keystore)
    assert arguments[arguments.index("-alias") + 1] == "android-pr-check"
    # PKCS12 has one password for the store and the key.
    assert arguments[arguments.index("-storepass") + 1] == arguments[arguments.index("-keypass") + 1]


def test_local_properties_without_caller_lines(jobs, tmp_path):
    result, workspace, _ = write_local_properties(jobs, tmp_path, "")
    assert result.returncode == 0, result.stderr
    names = [line.split("=", 1)[0] for line in (workspace / "local.properties").read_text().splitlines()]
    assert names == ["KEYSTORE_FILE", "KEYSTORE_PASSWORD", "KEY_ALIAS", "KEY_PASSWORD"]


def test_local_properties_without_the_signing_key_holds_only_the_caller_lines(jobs, tmp_path):
    result, workspace, temp = write_local_properties(jobs, tmp_path, "API_KEY=ci-placeholder\n", signing_key="false")
    assert result.returncode == 0, result.stderr
    assert (workspace / "local.properties").read_text() == "API_KEY=ci-placeholder\n"
    assert not (temp / "android-pr-check.jks").exists()
    assert not (tmp_path / "keytool-args").exists()


def test_local_properties_is_not_written_when_there_is_nothing_to_write(jobs, tmp_path):
    result, workspace, temp = write_local_properties(jobs, tmp_path, "# nothing\n\n", signing_key="false")
    assert result.returncode == 0, result.stderr
    assert "Nothing to write" in result.stdout
    assert not (workspace / "local.properties").exists()
    assert not (temp / "android-pr-check.jks").exists()


def test_only_the_verify_job_can_do_without_the_signing_key(jobs):
    assert step(jobs["verify"], "Write local.properties")["env"]["SIGNING_KEY"] == "${{ inputs.verify-signing-key }}"
    assert step(jobs["release-build"], "Write local.properties")["env"]["SIGNING_KEY"] == "true"


@pytest.mark.parametrize("signing_key", ["true", "false"])
@pytest.mark.parametrize(
    ("local_properties", "message"),
    [
        ("API_KEY=x\nJUST_A_NAME\n", "Line 2 is not NAME=value."),
        ("1BAD=x\n", "Line 1 is not NAME=value."),
        ("API KEY=x\n", "Line 1 is not NAME=value."),
        ("A=1\n\nKEY_ALIAS=mine\n", "Line 3 sets KEY_ALIAS, which this workflow writes itself."),
        ("KEYSTORE_FILE=/tmp/other.jks\n", "Line 1 sets KEYSTORE_FILE, which this workflow writes itself."),
    ],
)
def test_local_properties_rejects_other_lines_without_printing_them(
    jobs, tmp_path, local_properties, message, signing_key
):
    result, workspace, temp = write_local_properties(jobs, tmp_path, local_properties, signing_key=signing_key)
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert f"::error title=Invalid local-properties::{message}" in output
    for line in local_properties.splitlines():
        if line and line not in ("API_KEY=x", "A=1"):
            assert line not in output
    assert not (workspace / "local.properties").exists()
    assert not (temp / "android-pr-check.jks").exists()


def test_the_keytool_command_makes_a_key_the_written_properties_open(jobs, tmp_path):
    # ANDROID_CI_KEYTOOL=1 (CI) turns a missing JDK into a failure instead of a skip.
    required = os.environ.get("ANDROID_CI_KEYTOOL") == "1"
    if shutil.which("keytool") is None and not required:
        pytest.skip("no JDK on PATH")
    result, workspace, temp = write_local_properties(jobs, tmp_path, "", real_keytool=True)
    if "Unable to locate a Java Runtime" in result.stderr and not required:
        pytest.skip("keytool is the macOS placeholder without a JDK")
    assert result.returncode == 0, result.stderr
    properties = dict(line.split("=", 1) for line in (workspace / "local.properties").read_text().splitlines())
    assert properties["KEYSTORE_FILE"] == str(temp / "android-pr-check.jks")
    command = ["keytool", "-list", "-keystore", properties["KEYSTORE_FILE"]]
    command += ["-storepass", properties["KEYSTORE_PASSWORD"], "-alias", properties["KEY_ALIAS"]]
    listing = subprocess.run(command, text=True, capture_output=True, check=False)
    assert listing.returncode == 0, listing.stdout + listing.stderr
    assert "PrivateKeyEntry" in listing.stdout


# --- Gradle --------------------------------------------------------------------

GRADLEW_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$GRADLEW_ARGS"
echo "token=${GITHUB_TOKEN:-}"
echo "BUILD ${GRADLEW_OUTCOME}"
[ "$GRADLEW_OUTCOME" = SUCCESSFUL ]
"""
TASKS = {"verify": "ciVerify", "release-build": "ciReleaseBuild"}


def run_gradle(jobs, tmp_path, job: str, runner: str, outcome: str = "SUCCESSFUL", **overrides):
    workspace = tmp_path / "workspace"
    temp = tmp_path / "temp"
    workspace.mkdir()
    temp.mkdir()
    # A file the argument pattern below would match if the shell expanded globs.
    (workspace / "a.Foo").write_text("")
    executable(workspace / "gradlew", GRADLEW_STUB)
    build = step(jobs[job], "Run Gradle")
    # The task names are the contract with the calling repository, not inputs.
    assert build["env"]["GRADLE_TASK"] == TASKS[job]
    arguments = build["env"]["GRADLE_ARGS"].replace(f"${{{{ inputs.{job}-args }}}}", overrides.pop("args", ""))
    assert "${{" not in arguments
    env = {
        "GRADLE_TASK": build["env"]["GRADLE_TASK"],
        "GRADLE_ARGS": arguments,
        "GITHUB_ACTOR": "octocat",
        "GITHUB_TOKEN": "token-for-packages",
        "TOKEN_SOURCE": "GIT_TOKEN",
        "RUNNER_KIND": runner,
        "MAX_WORKERS": "32",
        "GRADLE_JVMARGS": "-Xmx16g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8",
        "KOTLIN_DAEMON_JVMARGS": "-Xmx6g",
        "RUNNER_TEMP": str(temp),
        "GRADLEW_ARGS": str(tmp_path / "gradlew-args"),
        "GRADLEW_OUTCOME": outcome,
        **overrides,
    }
    result = run_script(build["run"], env, workspace)
    return result, (tmp_path / "gradlew-args").read_text().splitlines(), temp / "gradle.log"


def test_gradle_on_a_github_hosted_runner_adds_nothing(jobs, tmp_path):
    result, arguments, log = run_gradle(jobs, tmp_path, "verify", "github-hosted")
    assert result.returncode == 0, result.stderr
    assert arguments == ["ciVerify", "--continue"]
    # gradle-log-verdict reads the copy; the job log shows the same lines after the step's own.
    assert result.stdout.endswith(log.read_text())
    assert "BUILD SUCCESSFUL" in log.read_text()
    assert "token=token-for-packages" in log.read_text()


@pytest.mark.parametrize("job", ["verify", "release-build"])
def test_gradle_gets_git_token_and_the_log_names_the_token_source(jobs, tmp_path, job):
    env = step(jobs[job], "Run Gradle")["env"]
    # Gradle reads GITHUB_ACTOR and GITHUB_TOKEN as GitHub Packages credentials.
    assert env["GITHUB_TOKEN"] == "${{ secrets.GIT_TOKEN || github.token }}"
    assert env["TOKEN_SOURCE"].startswith("${{ secrets.GIT_TOKEN != '' && 'GIT_TOKEN' || 'the job token")
    result, _, log = run_gradle(jobs, tmp_path, job, "github-hosted")
    line = "GitHub Packages credentials for Gradle: GITHUB_ACTOR=octocat, GITHUB_TOKEN from GIT_TOKEN.\n"
    assert result.stdout.startswith(line)
    # The token itself is printed only by the gradlew stand-in.
    assert "token-for-packages" not in line and line not in log.read_text()


def test_gradle_on_a_self_hosted_runner_limits_workers_and_memory(jobs, tmp_path):
    result, arguments, _ = run_gradle(jobs, tmp_path, "verify", "self-hosted", args="--init-script scripts/x.gradle.kts")
    assert result.returncode == 0, result.stderr
    assert arguments == [
        "ciVerify",
        "--continue",
        "--init-script",
        "scripts/x.gradle.kts",
        "--no-daemon",
        "--max-workers=32",
        "-Dorg.gradle.jvmargs=-Xmx16g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8",
        "-Pkotlin.daemon.jvmargs=-Xmx6g",
    ]


def test_gradle_on_a_self_hosted_runner_with_empty_memory_inputs(jobs, tmp_path):
    result, arguments, _ = run_gradle(
        jobs, tmp_path, "release-build", "self-hosted", MAX_WORKERS="", GRADLE_JVMARGS="", KOTLIN_DAEMON_JVMARGS=""
    )
    assert result.returncode == 0, result.stderr
    assert arguments == ["ciReleaseBuild", "--no-daemon"]


def test_release_build_does_not_continue_after_a_failure(jobs, tmp_path):
    result, arguments, _ = run_gradle(jobs, tmp_path, "release-build", "github-hosted", args="-Pname=value")
    assert result.returncode == 0, result.stderr
    assert arguments == ["ciReleaseBuild", "-Pname=value"]


def test_gradle_arguments_are_split_but_not_expanded(jobs, tmp_path):
    result, arguments, _ = run_gradle(jobs, tmp_path, "verify", "github-hosted", args="  --tests *.Foo\n-Pa=b ")
    assert result.returncode == 0, result.stderr
    assert arguments == ["ciVerify", "--continue", "--tests", "*.Foo", "-Pa=b"]


def test_a_failed_gradle_run_fails_the_step_despite_the_pipe(jobs, tmp_path):
    result, _, log = run_gradle(jobs, tmp_path, "verify", "self-hosted", outcome="FAILED")
    assert result.returncode != 0
    assert "BUILD FAILED" in log.read_text()


# --- wiring -------------------------------------------------------------------


def test_verify_and_release_build_differ_only_where_they_must(jobs):
    verify, release = jobs["verify"]["steps"], jobs["release-build"]["steps"]
    assert [item.get("name") for item in verify] == [item.get("name") for item in release]
    differing = {}
    for ours, theirs in zip(verify, release):
        if ours != theirs:
            differing[ours["name"]] = {
                (key, name)
                for key in ours.keys() | theirs.keys()
                if ours.get(key) != theirs.get(key)
                for name in ours[key].keys() | theirs[key].keys()
                if ours[key].get(name) != theirs[key].get(name)
            }
    assert differing == {
        "Decide whether to build": {("env", "QA_SOURCE_BRANCH")},
        "Write local.properties": {("env", "SIGNING_KEY")},
        "Run Gradle": {("env", "GRADLE_TASK"), ("env", "GRADLE_ARGS")},
        # Both runs upload in one workflow run, so the names must not collide.
        "Upload the test and lint reports": {("with", "name")},
    }
    uploads = [step(jobs[job], "Upload the test and lint reports")["with"]["name"] for job in TASKS]
    assert uploads == [f"android-pr-check-{job}-reports-${{{{ inputs.attempt }}}}" for job in TASKS]


@pytest.mark.parametrize("job", ["verify", "release-build"])
def test_gradle_job_reports_a_verdict_for_every_ending(jobs, job):
    steps = jobs[job]["steps"]
    verdict = steps[-1]
    assert verdict["id"] == "verdict" and verdict["if"] == "${{ always() }}"
    assert jobs[job]["outputs"] == {"verdict": "${{ steps.verdict.outputs.verdict }}"}
    assert verdict["with"] == {
        "log-file": "${{ runner.temp }}/gradle.log",
        "setup-outcome": "${{ steps.checkout.outcome }}",
        "build-outcome": "${{ steps.build.outcome }}",
        # Covers the steps between checkout and the build, which are skipped on a docs-only change.
        "job-status": "${{ job.status }}",
    }
    ids = [item.get("id") for item in steps]
    assert ids.index("checkout") < ids.index("plan") < ids.index("build") < ids.index("verdict")
    for item in steps[ids.index("plan") + 1 : ids.index("build") + 1]:
        assert item["if"] == "${{ steps.plan.outputs.build == 'true' }}", item["name"]
    upload = step(jobs[job], "Upload the test and lint reports")
    assert upload["continue-on-error"] is True, "a failed upload would turn a code failure into infra"


def test_code_style_reports_a_verdict_for_every_ending(jobs, tmp_path):
    job = jobs["code-style"]
    verdict = job["steps"][-1]
    assert verdict["if"] == "${{ always() }}"
    assert verdict["env"] == {
        "SETUP": "${{ steps.checkout.outcome }} ${{ steps.mise.outcome }}",
        "CHECK": "${{ steps.pre-commit.outcome }}",
    }
    assert [item.get("id") for item in job["steps"][1:]] == ["checkout", "mise", "pre-commit", "verdict"]
    expected = {
        ("success success", "success"): "pass",
        ("success success", "failure"): "code",
        ("success success", "cancelled"): "infra",
        ("success success", "skipped"): "infra",
        ("success failure", "skipped"): "infra",
        ("failure skipped", "skipped"): "infra",
        ("success cancelled", "skipped"): "infra",
    }
    for (setup, check), wanted in expected.items():
        output = tmp_path / f"output-{setup}-{check}".replace(" ", "-")
        result = run_script(verdict["run"], {"SETUP": setup, "CHECK": check, "GITHUB_OUTPUT": str(output)}, tmp_path)
        assert result.returncode == 0, result.stderr
        assert output.read_text() == f"verdict={wanted}\n", (setup, check)


def test_full_scan_paths_default_is_the_one_of_pre_commit_changed(entry, jobs):
    # The workflow always passes the input, so the action's own default never applies.
    action = yaml.safe_load((ROOT / "pre-commit-changed" / "action.yml").read_text())
    assert trigger(entry)["inputs"]["full-scan-paths"]["default"] == action["inputs"]["full-scan-paths"]["default"]
    pre_commit = step(jobs["code-style"], "Run pre-commit on the changed files")
    assert pre_commit["with"]["full-scan-paths"] == "${{ inputs.full-scan-paths }}"


def test_primary_and_fallback_pass_the_same_inputs(entry):
    inputs = trigger(entry)["inputs"]
    inner = trigger(load(JOBS))
    primary, fallback = entry["jobs"]["primary"], entry["jobs"]["fallback"]
    assert primary["uses"] == fallback["uses"] == f"./.github/workflows/{JOBS}"
    # secrets: inherit reaches one level only, so GIT_TOKEN is handed on by name.
    assert primary["secrets"] == fallback["secrets"] == {"GIT_TOKEN": "${{ secrets.GIT_TOKEN }}"}
    assert set(inner["secrets"]) == {"GIT_TOKEN"}
    assert set(primary["with"]) == set(fallback["with"]) == set(inner["inputs"])
    assert primary["with"]["attempt"] == "primary" and fallback["with"]["attempt"] == "fallback"
    forwarded = set(inputs) - {"runner-labels", "fallback-runs-on"}
    assert forwarded == set(inner["inputs"]) - {"runs-on", "attempt"}
    for name in forwarded:
        assert primary["with"][name] == fallback["with"][name] == f"${{{{ inputs.{name} }}}}"
        assert inputs[name]["type"] == inner["inputs"][name]["type"], name
        assert "default" in inputs[name], f"{name}: the caller-facing workflow owns the defaults"
        # An input that may be empty cannot be required in the called workflow.
        if inputs[name]["default"] == "":
            assert inner["inputs"][name]["required"] is False, name
    # The Gradle task names are the calling repository's contract, not inputs.
    assert not [name for name in inputs if name.endswith("-task")]


def test_self_hosted_runners_are_used_only_for_the_repository_s_own_code(entry):
    jobs = entry["jobs"]
    condition = " ".join(jobs["pick-runner"]["if"].split())
    for clause in (
        "inputs.runner-labels != ''",
        "github.event_name != 'pull_request_target'",
        # No pull request in the event, or one whose head is this repository: not a fork.
        "(!github.event.pull_request || github.event.pull_request.head.repo.full_name == github.repository)",
    ):
        assert clause in condition
    assert condition.count("||") == 1 and condition.count("&&") == 2
    # The other jobs take the decision from pick-runner's result instead of repeating the condition:
    # skipped means GitHub-hosted, failed means the self-hosted labels unchecked.
    runs_on = jobs["primary"]["with"]["runs-on"]
    assert runs_on.startswith(
        "${{ needs.pick-runner.outputs.runs-on || (needs.pick-runner.result != 'skipped' && inputs.runner-labels) || "
    )
    hosted = "(startsWith(inputs.fallback-runs-on, '[') && inputs.fallback-runs-on) || toJSON(inputs.fallback-runs-on || 'ubuntu-latest') }}"
    assert runs_on.endswith("|| " + hosted)
    assert jobs["fallback"]["with"]["runs-on"] == "${{ " + hosted
    assert "fork" not in str(jobs["primary"]) and "head.repo" not in str(jobs["fallback"])


def test_fallback_runs_only_after_a_self_hosted_infrastructure_failure(entry):
    condition = " ".join(entry["jobs"]["fallback"]["if"].split())
    clauses = (
        "!cancelled()",
        "needs.pick-runner.result != 'skipped'",
        "needs.pick-runner.outputs.self-hosted != 'false'",
        "(needs.primary.result == 'failure' || needs.primary.result == 'cancelled')",
        "needs.primary.outputs.code-style-verdict != 'code'",
        "needs.primary.outputs.verify-verdict != 'code'",
        "needs.primary.outputs.release-build-verdict != 'code'",
    )
    assert condition == "${{ " + " && ".join(clauses) + " }}"
    assert entry["jobs"]["fallback"]["needs"] == ["pick-runner", "primary"]
    outputs = trigger(load(JOBS))["outputs"]
    assert {name: value["value"] for name, value in outputs.items()} == {
        f"{job}-verdict": f"${{{{ jobs.{job}.outputs.verdict }}}}" for job in ("code-style", "verify", "release-build")
    }


def uses_lines() -> list[str]:
    lines = []
    for name in (ENTRY, JOBS):
        lines += re.findall(r"^\s*(?:- )?uses: (.+)$", (WORKFLOWS / name).read_text(), re.MULTILINE)
    return lines


def test_actions_of_this_repository_are_referenced_by_full_path_and_one_ref():
    refs: dict[str, set[str]] = {}
    for line in uses_lines():
        # A `./` action path would be looked up in the calling repository's checkout.
        assert line.startswith("./.github/workflows/") or not line.startswith("./"), line
        match = OWN_ACTION.match(line)
        if match:
            assert (ROOT / match["name"] / "action.yml").is_file()
            refs.setdefault(match["name"], set()).add(match["ref"])
    assert set(refs) == {"pick-runner", "pre-commit-changed", "gradle-log-verdict"}
    assert refs["pick-runner"] == {"pick-runner/v1"}
    # The actions released with the workflows share the workflows' ref: the development branch until the
    # release, android-ci/v1 from then on. A half-done switch fails here.
    (shared,) = refs["pre-commit-changed"] | refs["gradle-log-verdict"]
    assert shared.startswith("android-ci/")


def test_third_party_actions_are_pinned_to_a_commit():
    third_party = [
        line for line in uses_lines() if not line.startswith(("./", "actions/", "asleep-ai/actions/"))
    ]
    assert {line.split("@")[0] for line in third_party} == {"jdx/mise-action", "gradle/actions/setup-gradle"}
    for line in third_party:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+", line), line


def test_secrets_are_optional_and_reach_only_the_steps_that_need_them(entry, jobs):
    secrets = trigger(entry)["secrets"]
    assert set(secrets) == {"RUNNER_READ_TOKEN", "GIT_TOKEN"}
    assert all(secret["required"] is False for secret in secrets.values())
    text = (WORKFLOWS / JOBS).read_text()
    assert "RUNNER_READ_TOKEN" not in text
    for name, job in jobs.items():
        for item in job["steps"]:
            if "secrets.GIT_TOKEN" not in str(item):
                continue
            assert name in ("verify", "release-build")
            assert item.get("id") in ("checkout", "build"), item
    assert checkout_step(jobs["code-style"])["with"]["persist-credentials"] is False
    assert "secrets." not in str(jobs["code-style"])


def test_permissions_are_the_least_the_jobs_need(entry):
    # The called workflow cannot be granted more than the caller has, so the example grants exactly this.
    example = yaml.safe_load(EXAMPLE.read_text())
    needed = {"contents": "read", "packages": "read"}
    assert example["permissions"] == entry["permissions"] == load(JOBS)["permissions"] == needed
    assert entry["jobs"]["pick-runner"]["permissions"] == entry["jobs"]["result"]["permissions"] == {}
    assert load(JOBS)["jobs"]["code-style"]["permissions"] == {"contents": "read"}


def test_the_example_passes_only_what_the_workflow_declares(entry):
    example = yaml.safe_load(EXAMPLE.read_text())
    (job,) = example["jobs"].values()
    assert job["uses"].startswith(f"asleep-ai/actions/.github/workflows/{ENTRY}@")
    assert set(job["with"]) <= set(trigger(entry)["inputs"])
    # Named one by one: `secrets: inherit` would hand every secret of the calling repository over.
    assert job["secrets"] == {name: f"${{{{ secrets.{name} }}}}" for name in trigger(entry)["secrets"]}
    readme = (ROOT / "android-ci" / "README.md").read_text()
    usage = readme.split("### Usage", 1)[1].split("```yaml", 1)[1].split("```", 1)[0]
    assert yaml.safe_load(usage)["jobs"]["pr-check"]["secrets"] == job["secrets"]
    assert yaml.safe_load(usage)["permissions"] == example["permissions"]


def test_readme_lists_every_input_with_its_default(entry):
    readme = (ROOT / "android-ci" / "README.md").read_text()
    inputs = trigger(entry)["inputs"]
    rows = re.findall(r"^\| `([a-z-]+)` \| (.*?) \| ", readme.split("### Inputs", 1)[1].split("###", 1)[0], re.MULTILINE)
    assert [name for name, _ in rows] == list(inputs), "the table follows the order of the workflow file"
    for name, shown in rows:
        default = inputs[name]["default"]
        if name == "paths-ignore":
            for pattern in default.split():
                assert f"\n{pattern}\n" in readme, pattern
        elif name == "full-scan-paths":
            assert "pre-commit-changed" in shown
        elif default == "":
            assert shown == "`''`", name
        elif isinstance(default, bool):
            assert shown == f"`{str(default).lower()}`", name
        else:
            assert shown == f"`{default}`", name
