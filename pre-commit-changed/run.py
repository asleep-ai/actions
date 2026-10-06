"""Run pre-commit on the files a pull request or push changed.

action.yml passes every input through an environment variable, so no input is
ever interpolated into a shell command. Any git failure stops the run: a check
that cannot see its history must fail, never pass silently. The one exception
checks more instead of failing: a push whose previous tip is missing locally,
as after a force push, runs pre-commit on every file.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
PULL_REQUEST_EVENTS = ("pull_request", "pull_request_target")


class Failure(Exception):
    """A state in which the set of files to check cannot be trusted."""


def git(*args: str) -> bytes:
    result = subprocess.run(["git", *args], capture_output=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise Failure(f"git {' '.join(args)} exited {result.returncode}: {detail}")
    return result.stdout


def paths(output: bytes) -> list[str]:
    # -z output is NUL-separated and unquoted; fsdecode round-trips any bytes.
    return [os.fsdecode(path) for path in output.split(b"\0") if path]


def full_sha(sha: str, name: str) -> str:
    # Only a full hex SHA ever reaches git, so no input can act as an option.
    if not SHA.fullmatch(sha):
        raise Failure(f"{name} must be a full 40- or 64-character lowercase hex SHA, got {sha!r}")
    return sha


def in_history(sha: str) -> bool:
    result = subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True, check=False)
    return result.returncode == 0


def commit(sha: str, name: str) -> str:
    if not in_history(full_sha(sha, name)):
        raise Failure(f"{name} {sha} is not in the local history; check out with fetch-depth: 0")
    return sha


def is_zero(sha: str) -> bool:
    return SHA.fullmatch(sha) is not None and set(sha) == {"0"}


def glob(pattern: str) -> re.Pattern[str]:
    """Compile a root-relative glob: `**/` spans zero or more directories,
    any other `**` matches anything, and `*` and `?` stay within one segment."""
    pattern = re.sub(r"^(\./|/)+", "", pattern)
    regex, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            regex, i = regex + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            regex, i = regex + ".*", i + 2
        else:
            regex, i = regex + {"*": "[^/]*", "?": "[^/]"}.get(pattern[i], re.escape(pattern[i])), i + 1
    return re.compile(regex)


def resolve_base(event: str, base: str, head: str) -> tuple[str | None, str]:
    """Return the commit to diff head against, or None and why every file is checked."""
    no_earlier = f"{head} has no earlier commit to compare against"
    if event in PULL_REQUEST_EVENTS:
        # Diff from the merge base: commits the base branch gained after the
        # pull request branched (from this repository or a fork) are not its own.
        merge_base = git("merge-base", commit(base, "base-sha"), head).decode().strip()
        return commit(merge_base, "merge base"), ""
    if event == "push":
        # A direct diff covers a multi-commit push and a force push alike and
        # needs no merge base. A zero SHA is a new branch with no previous tip.
        if is_zero(full_sha(base, "base-sha")):
            return None, no_earlier
        if not in_history(base):
            # No ref reaches the old tip of a force push, so a fresh clone never
            # has it. Checking every file is stronger than the missing diff.
            annotate("warning", f"base-sha {base} is not in the local history, as after a force push; "
                                "checking all files instead of the pushed range")
            return None, "the push's previous tip is not in the local history"
        return base, ""
    if base:
        return commit(base, "base-sha"), ""
    # Manual and other events check the head commit's own changes.
    parents = git("rev-list", "--parents", "-n", "1", head).decode().split()[1:]
    return (parents[0], "") if parents else (None, no_earlier)


def annotate(level: str, message: str) -> None:
    # Workflow commands are read line by line, so %, CR and LF are encoded.
    message = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level}::{message}", file=sys.stderr)


def pre_commit(*args: str) -> int:
    sys.stdout.flush()
    try:
        return subprocess.run(["pre-commit", "run", "--show-diff-on-failure", *args], check=False).returncode
    except FileNotFoundError:
        raise Failure("pre-commit is not on PATH; provision it before this action") from None


def main() -> int:
    event = os.environ.get("EVENT_NAME", "").strip()
    base = os.environ.get("BASE_SHA", "").strip()
    head = os.environ.get("HEAD_SHA", "").strip()
    lines = os.environ.get("FULL_SCAN_PATHS", "").splitlines()
    patterns = [glob(line.strip()) for line in lines if line.strip()]
    if not event:
        raise Failure("event-name is required")
    os.chdir(os.fsdecode(git("rev-parse", "--show-toplevel").rstrip(b"\n")))
    checked_out = git("rev-parse", "HEAD").decode().strip()
    head = commit(head or checked_out, "head-sha")
    if head != checked_out:
        # Hooks read the working tree, so the diff must describe that commit.
        raise Failure(f"head-sha {head} is not the checked-out commit {checked_out}")
    base_commit, reason = resolve_base(event, base, head)
    if base_commit is None:
        print(f"Checking all files: {reason}.")
        return pre_commit("--all-files")
    print(f"Checking changes from {base_commit} to {head}.")
    # Every status counts here, deletions and both sides of a rename included:
    # removing or moving shared configuration changes what every file must pass.
    changed = paths(git("diff", "--name-only", "--no-renames", "-z", base_commit, head, "--"))
    if any(pattern.fullmatch(path) for path in changed for pattern in patterns):
        print("Shared configuration changed; checking all files.")
        return pre_commit("--all-files")
    # T counts too: a symlink replaced by a regular file is new content to check.
    files = paths(git("diff", "--name-only", "--diff-filter=ACMRT", "-z", base_commit, head, "--"))
    if not files:
        print("No added, copied, modified, renamed or type-changed files to check.")
        return 0
    # pre-commit resolves each path against the repository root, so the "./"
    # prefix only stops a name that starts with "-" from parsing as an option.
    return pre_commit("--files", *(f"./{path}" for path in files))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failure as error:
        annotate("error", str(error))
        sys.exit(1)
