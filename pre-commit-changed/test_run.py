"""Tests for run.py, the pre-commit-changed runner.

Each test builds a throwaway git repository and replaces pre-commit with a
stub that records its arguments, so only git and Python are needed. One test
also drives the real pre-commit when it is on PATH.

Run from the repository root:
    uv run --with pytest python -m pytest pre-commit-changed -q
"""
from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
RUNNER = HERE / "run.py"
ZERO = "0" * 40
STUB = """#!PYTHON
import json, os, sys
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
# STUB_FAIL_ON stands in for a hook that rejects any checked file containing that text.
marker = os.environ.get("STUB_FAIL_ON")
if marker and any(os.path.isfile(arg) and marker in open(arg).read() for arg in sys.argv[1:]):
    sys.exit(1)
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
"""
# Shared configuration the default full-scan-paths must cover, at the root and nested.
CONFIG_FILES = {
    ".pre-commit-config.yaml": "repos: []\n# v1\n", "config/ktfmt-baseline.txt": "x\n",
    "app/sub/mise.toml": "[tools]\n# v1\n", "scripts/lint.sh": "echo lint\n",
    ".mise.toml": "[tools]\n# v1\n", "app/.mise.toml": "[tools]\n# v1\n",
    ".tool-versions": "java 17\n", "app/.tool-versions": "java 17\n",
    "uv.lock": "version = 1\n", "app/uv.lock": "version = 1\n",
}


def default_patterns() -> str:
    """The full-scan-paths default from action.yml, as GitHub passes it."""
    block = (HERE / "action.yml").read_text().split("  full-scan-paths:\n", 1)[1]
    lines = block.split("    default: |\n", 1)[1].splitlines()
    return "".join(line.strip() + "\n" for line in itertools.takewhile(lambda line: line.startswith("      "), lines))


class Repo:
    def __init__(self, root: Path, env: dict[str, str], log: Path, origin: Path | None = None) -> None:
        self.root, self.env, self.log = root, env, log
        if origin is None:
            self.git("init", "-q")
        else:
            # --no-local uses the git transport, which, like actions/checkout's
            # fetch, copies only the objects some ref still reaches.
            subprocess.run(["git", "clone", "-q", "--no-local", str(origin), str(root)], env=env,
                           capture_output=True, check=True)

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.root, env=self.env, text=True, capture_output=True,
                              check=True).stdout.strip()

    def commit(self, changes: dict[str, str | None]) -> str:
        for name, text in changes.items():
            path = self.root / name
            if text is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "change")
        return self.git("rev-parse", "HEAD")

    def run(self, event: str, base: str = "", head: str | None = None, *, expected: int = 0,
            **env: str) -> subprocess.CompletedProcess[str]:
        env = {**self.env, "EVENT_NAME": event, "BASE_SHA": base, "FULL_SCAN_PATHS": default_patterns(),
               "HEAD_SHA": self.git("rev-parse", "HEAD") if head is None else head, **env}
        result = subprocess.run([sys.executable, str(RUNNER)], cwd=self.root, env=env, text=True,
                                capture_output=True, check=False)
        assert result.returncode == expected, result.stdout + result.stderr
        return result

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def checked(self) -> list[str]:
        """The arguments of the only pre-commit call after `run --show-diff-on-failure`."""
        [call] = self.calls()
        assert call[:2] == ["run", "--show-diff-on-failure"]
        return call[2:]


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    stub = tmp_path / "bin" / "pre-commit"
    stub.parent.mkdir()
    stub.write_text(STUB.replace("PYTHON", sys.executable))
    stub.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ("EVENT_NAME", "BASE_SHA", "HEAD_SHA", "FULL_SCAN_PATHS")}
    env.update(PATH=f"{stub.parent}{os.pathsep}{env['PATH']}", STUB_LOG=str(tmp_path / "calls.jsonl"),
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.invalid",
               GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.invalid")
    (tmp_path / "repo").mkdir()
    return Repo(tmp_path / "repo", env, tmp_path / "calls.jsonl")


def test_pull_request_from_a_fork_ignores_commits_its_base_gained(repo: Repo) -> None:
    repo.commit({"Feature.kt": "1", "MainOnly.kt": "1"})
    repo.git("checkout", "-q", "-b", "fork-feature")
    head = repo.commit({"Feature.kt": "2", "New File.kt": "1"})
    repo.git("checkout", "-q", "-")
    base = repo.commit({"MainOnly.kt": "2"})  # the base branch moved on after the fork
    repo.git("checkout", "-q", "fork-feature")

    repo.run("pull_request", base, head)

    assert repo.checked() == ["--files", "./Feature.kt", "./New File.kt"]


def test_push_checks_every_commit_in_the_range(repo: Repo) -> None:
    before = repo.commit({"Edit.kt": "1", "Gone.kt": "1", "Keep.kt": "1"})
    repo.commit({"Added.kt": "1"})
    repo.commit({"Edit.kt": "2", "Gone.kt": None})
    repo.commit({"Temp.kt": "1"})
    repo.commit({"Temp.kt": None})

    repo.run("push", before)

    assert repo.checked() == ["--files", "./Added.kt", "./Edit.kt"]


def test_force_push_compares_trees_without_needing_a_merge_base(repo: Repo) -> None:
    before = repo.commit({"Old.kt": "1", "Same.kt": "1"})
    repo.git("checkout", "-q", "--orphan", "rewritten")
    head = repo.commit({"Old.kt": None, "New.kt": "1"})
    merge_base = subprocess.run(["git", "merge-base", before, head], cwd=repo.root, env=repo.env, check=False)
    assert merge_base.returncode == 1

    repo.run("push", before)
    assert repo.checked() == ["--files", "./New.kt"]
    # A pull request has no meaningful range without a merge base, so it fails.
    assert "merge-base" in repo.run("pull_request", before, expected=1).stderr


def test_fresh_clone_after_a_force_push_checks_all_files_with_a_warning(repo: Repo, tmp_path: Path) -> None:
    before = repo.commit({"Old.kt": "1", "Same.kt": "1"})
    branch = repo.git("branch", "--show-current")
    repo.git("checkout", "-q", "--orphan", "rewritten")
    head = repo.commit({"Old.kt": None, "New.kt": "1"})
    repo.git("checkout", "-q", "-B", branch, head)  # the force push: the branch now points at the rewrite
    repo.git("branch", "-q", "-D", "rewritten")
    clone = Repo(tmp_path / "clone", repo.env, repo.log, origin=repo.root)
    repo.git("cat-file", "-e", f"{before}^{{commit}}")  # the origin still stores the old tip,
    missing = subprocess.run(["git", "cat-file", "-e", f"{before}^{{commit}}"], cwd=clone.root, env=clone.env,
                             capture_output=True, check=False)
    assert missing.returncode != 0  # but no ref reaches it, so a fresh clone never receives it

    repo.run("push", before)  # where the old tip is present, the direct tree diff still applies
    result = clone.run("push", before)

    assert repo.calls() == [["run", "--show-diff-on-failure", "--files", "./New.kt"],
                            ["run", "--show-diff-on-failure", "--all-files"]]
    assert f"::warning::base-sha {before} is not in the local history" in result.stderr
    # Only a push falls back: a pull request base must still be present.
    assert "not in the local history" in clone.run("pull_request", before, expected=1).stderr
    assert len(repo.calls()) == 2


@pytest.mark.parametrize("change", [
    {".pre-commit-config.yaml": "repos: []\n"},
    {"config/ktfmt-baseline.txt": None},
    {"app/sub/mise.toml": "[tools]\n"},
    {"scripts/lint.sh": None, "tools/lint.sh": "echo lint\n"},  # moved out of scripts/
    {".mise.toml": "[tools]\n"},
    {"app/.mise.toml": "[tools]\n"},
    {".tool-versions": "java 21\n"},
    {"app/.tool-versions": "java 21\n"},
    {"uv.lock": "version = 2\n"},
    {"app/uv.lock": "version = 2\n"},
], ids=["modified", "deleted", "nested", "renamed-away", "dot-mise-root", "dot-mise-nested",
        "tool-versions-root", "tool-versions-nested", "uv-lock-root", "uv-lock-nested"])
def test_shared_configuration_change_checks_all_files(repo: Repo, change: dict[str, str | None]) -> None:
    before = repo.commit({**CONFIG_FILES, "Main.kt": "1"})
    repo.commit(change)

    repo.run("push", before)

    assert repo.checked() == ["--all-files"]


def test_names_that_only_resemble_shared_configuration_are_checked_individually(repo: Repo) -> None:
    before = repo.commit({**CONFIG_FILES, "Main.kt": "1"})
    repo.commit({"my.mise.toml": "x\n", "uv.lock.bak": "x\n", "docs/tool-versions.md": "x\n"})

    repo.run("push", before)

    assert repo.checked() == ["--files", "./docs/tool-versions.md", "./my.mise.toml", "./uv.lock.bak"]


def test_new_branch_push_checks_all_files(repo: Repo) -> None:
    repo.commit({"Main.kt": "1"})
    repo.run("push", ZERO)
    assert repo.checked() == ["--all-files"]


def test_manual_run_checks_the_head_commit_or_everything_at_the_root(repo: Repo) -> None:
    repo.commit({"Root.kt": "1"})
    repo.run("workflow_dispatch", head="")  # an omitted head means the checked-out HEAD
    repo.commit({"Next.kt": "1"})
    repo.run("workflow_dispatch", head="")

    assert repo.calls() == [["run", "--show-diff-on-failure", "--all-files"],
                            ["run", "--show-diff-on-failure", "--files", "./Next.kt"]]


@pytest.mark.parametrize(("event", "base", "head", "message"), [
    ("pull_request", "f" * 40, None, "not in the local history"),
    ("workflow_dispatch", "f" * 40, None, "not in the local history"),
    ("pull_request", "", None, "must be a full"),
    ("push", "", None, "must be a full"),
    ("push", "F" * 40, None, "must be a full"),
    ("push", "--output=/tmp/x", None, "must be a full"),
    ("push", "HEAD~1", None, "must be a full"),
    ("push", "abc1234", None, "must be a full"),
    ("push", ZERO, "--help", "must be a full"),
    ("push", ZERO, "e" * 40, "not in the local history"),
    ("push", "f" * 40, "e" * 40, f"head-sha {'e' * 40} is not in the local history"),
])
def test_unusable_commits_fail_closed(repo: Repo, event: str, base: str, head: str | None, message: str) -> None:
    repo.commit({"Main.kt": "1"})
    assert message in repo.run(event, base, head, expected=1).stderr
    assert repo.calls() == []


def test_head_must_be_the_checked_out_commit(repo: Repo) -> None:
    other = repo.commit({"Main.kt": "1"})
    repo.commit({"Main.kt": "2"})
    assert "not the checked-out commit" in repo.run("push", ZERO, other, expected=1).stderr
    assert repo.calls() == []


def test_unusual_file_names_are_passed_as_separate_safe_arguments(repo: Repo) -> None:
    before = repo.commit({"Main.kt": "1"})
    repo.commit({"-dash.kt": "1", "--files.kt": "1", "has space.kt": "1"})

    repo.run("push", before)

    assert repo.checked() == ["--files", "./--files.kt", "./-dash.kt", "./has space.kt"]


def test_symlink_replaced_by_a_regular_file_is_checked(repo: Repo) -> None:
    repo.commit({"Target.kt": "1"})
    (repo.root / "Link.kt").symlink_to("Target.kt")
    before = repo.commit({})
    (repo.root / "Link.kt").unlink()
    repo.commit({"Link.kt": "FAIL\n"})
    assert repo.git("diff", "--name-status", before, "HEAD") == "T\tLink.kt"  # a type change

    repo.run("push", before, expected=1, STUB_FAIL_ON="FAIL")

    assert repo.checked() == ["--files", "./Link.kt"]


def test_deleted_only_change_skips_pre_commit(repo: Repo) -> None:
    before = repo.commit({"A.kt": "1", "B.kt": "1"})
    repo.commit({"A.kt": None})

    result = repo.run("push", before)

    assert "No added, copied, modified, renamed or type-changed files" in result.stdout
    assert repo.calls() == []


def test_pre_commit_exit_status_is_propagated(repo: Repo) -> None:
    before = repo.commit({"Main.kt": "1"})
    repo.commit({"Main.kt": "2"})
    repo.run("push", before, expected=3, STUB_EXIT="3")
    repo.run("push", ZERO, expected=1, STUB_EXIT="1")


def test_real_pre_commit_receives_root_relative_names(repo: Repo, tmp_path: Path) -> None:
    env = {**repo.env, "PATH": repo.env["PATH"].split(os.pathsep, 1)[1], "PRE_COMMIT_HOME": str(tmp_path / "home")}
    if shutil.which("pre-commit", path=env["PATH"]) is None:
        pytest.skip("pre-commit is not installed")
    record = tmp_path / "record.py"
    record.write_text("import json, os, sys\n"
                      "open(os.environ['RECORD'], 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n")
    config = ("repos:\n  - repo: local\n    hooks:\n      - id: record\n        name: record\n"
              f"        entry: {sys.executable} {record}\n        language: system\n        files: '\\.kt$'\n")
    before = repo.commit({".pre-commit-config.yaml": config, "Main.kt": "1"})
    repo.commit({"-dash.kt": "1", "sub dir/has space.kt": "1"})
    repo.env = {**env, "RECORD": str(tmp_path / "record.jsonl")}

    repo.run("push", before)

    recorded = [json.loads(line) for line in (tmp_path / "record.jsonl").read_text().splitlines()]
    assert sorted(itertools.chain.from_iterable(recorded)) == ["-dash.kt", "sub dir/has space.kt"]
