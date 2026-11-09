"""Unit tests for generate.py.

Run: uv run --with pytest --with 'openai>=1.55,<2' python -m pytest test_generate.py
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

_spec = importlib.util.spec_from_file_location("generate", Path(__file__).with_name("generate.py"))
assert _spec is not None and _spec.loader is not None
generate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = generate  # dataclasses resolve annotations via sys.modules[cls.__module__]
_spec.loader.exec_module(generate)


def test_format_enriches_with_pr_body(monkeypatch) -> None:
    monkeypatch.setattr(
        generate, "pr_body", lambda n: "Fixes a release-only R8/JNI crash." if n == 476 else ""
    )

    out = generate.format_commits_for_prompt(["Update wakeword service to 0.1.1 (#476)"])

    assert "Update wakeword service to 0.1.1 (#476)" in out  # subject + ref preserved
    assert "PR #476 description:\nFixes a release-only R8/JNI crash." in out  # body injected


def test_format_without_pr_ref_makes_no_lookup(monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(generate, "pr_body", lambda n: calls.append(n) or "should-not-appear")

    out = generate.format_commits_for_prompt(["Tidy internal helper"])

    assert out == "### Commit 1\nTidy internal helper"  # unchanged
    assert calls == []  # no (#NNN) -> gh is never invoked


def test_pr_body_returns_empty_on_subprocess_failure(monkeypatch) -> None:
    failures = [
        FileNotFoundError("gh"),                          # gh not on PATH (OSError)
        subprocess.TimeoutExpired(cmd="gh", timeout=10),  # hang (SubprocessError)
    ]
    for exc in failures:
        def boom(*_args: object, _exc: BaseException = exc, **_kwargs: object) -> object:
            raise _exc

        monkeypatch.setattr(generate.subprocess, "run", boom)
        generate.pr_body.cache_clear()

        assert generate.pr_body(99999) == ""  # best-effort -> fallback, no crash


def test_enrichment_uses_trailing_subject_ref_only(monkeypatch) -> None:
    fetched: list[int] = []
    monkeypatch.setattr(generate, "pr_body", lambda n: fetched.append(n) or f"body {n}")

    out = generate.format_commits_for_prompt(['Revert "Feature (#42)" (#43)'])

    assert fetched == [43]  # the merge PR, not the reverted original #42
    assert "PR #43 description:\nbody 43" in out
    assert "PR #42 description:" not in out


def test_enrichment_ignores_refs_outside_subject(monkeypatch) -> None:
    fetched: list[int] = []
    monkeypatch.setattr(generate, "pr_body", lambda n: fetched.append(n) or "body")

    entry = "Tidy helper\n\nFollow-up to (#41); see also (#40)."
    out = generate.format_commits_for_prompt([entry])

    assert fetched == []  # refs only in the body are not enriched
    assert "PR #" not in out  # nothing appended


def test_enrichment_respects_total_budget(monkeypatch) -> None:
    fetched: list[int] = []
    monkeypatch.setattr(generate, "pr_body", lambda n: fetched.append(n) or "body")
    # monotonic(): set deadline, commit 1 under budget, then over for the rest.
    ticks = iter([0.0, 0.0] + [10_000.0] * 10)
    monkeypatch.setattr(generate.time, "monotonic", lambda: next(ticks))

    generate.format_commits_for_prompt(["First (#1)", "Second (#2)"])

    assert fetched == [1]  # budget spent before the second commit's lookup


def test_pr_body_is_truncated_to_char_limit(monkeypatch) -> None:
    long_body = "x" * (generate.PR_BODY_CHAR_LIMIT + 500)
    monkeypatch.setattr(generate, "pr_body", lambda _n: long_body)

    out = generate.format_commits_for_prompt(["Big PR (#7)"])

    assert "[... truncated]" in out
    assert out.count("x") <= generate.PR_BODY_CHAR_LIMIT  # capped, not verbatim


def test_total_pr_body_budget_caps_appended_text(monkeypatch) -> None:
    body = "y" * generate.PR_BODY_CHAR_LIMIT
    monkeypatch.setattr(generate, "pr_body", lambda _n: body)
    count = generate.PR_BODY_TOTAL_LIMIT // generate.PR_BODY_CHAR_LIMIT + 3
    commits = [f"Change {i} (#{i})" for i in range(1, count + 1)]

    out = generate.format_commits_for_prompt(commits)

    assert out.count("description:") < count  # stops once the total budget is spent


def test_estimate_cost_bills_cached_prompt_tokens_at_cached_rate() -> None:
    usage = generate.Usage(prompt_tokens=1_000, cached_tokens=200, completion_tokens=100)

    cost = generate.estimate_cost_usd("gpt-5.5", usage)

    assert cost == pytest.approx((800 * 5.00 + 200 * 0.50 + 100 * 30.00) / 1_000_000)


def test_estimate_cost_accepts_dated_snapshots_but_not_prefix_lookalikes() -> None:
    usage = generate.Usage(prompt_tokens=1_000_000)

    assert generate.estimate_cost_usd("gpt-5.4-mini-2026-01-01", usage) == pytest.approx(0.75)
    assert generate.estimate_cost_usd("gpt-5.5-2026-04-23", usage) == pytest.approx(5.00)
    assert generate.estimate_cost_usd("gpt-5.4-unlisted-variant", usage) is None  # shares a prefix, not priced
    assert generate.estimate_cost_usd("some-future-model", usage) is None


class _FakeOpenAI:
    """Stands in for `openai.OpenAI`: one canned completion with usage; records request kwargs."""

    last_request: ClassVar[dict[str, object]] = {}

    def __init__(self, **_kwargs: object) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    @classmethod
    def _create(cls, **kwargs: object) -> SimpleNamespace:
        cls.last_request = kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="## Highlights\n- x\n"))],
            usage=SimpleNamespace(
                prompt_tokens=1_200,
                completion_tokens=300,
                prompt_tokens_details=SimpleNamespace(cached_tokens=400),
            ),
        )


def test_generate_ai_notes_records_usage_on_report(monkeypatch) -> None:
    monkeypatch.setattr(generate, "OpenAI", _FakeOpenAI)
    report = generate.RunReport(tag="v1.0.0", model="gpt-5.5")

    notes = generate.generate_ai_notes(
        api_key="k", model="gpt-5.5", system_prompt="p", version="v1.0.0", commits=["Add x"], report=report
    )

    assert notes == "## Highlights\n- x"
    assert report.usage == generate.Usage(prompt_tokens=1_200, cached_tokens=400, completion_tokens=300)
    assert report.reason == ""
    assert "reasoning_effort" not in _FakeOpenAI.last_request  # omitted unless requested


def test_generate_ai_notes_sends_reasoning_effort_when_set(monkeypatch) -> None:
    monkeypatch.setattr(generate, "OpenAI", _FakeOpenAI)
    report = generate.RunReport(tag="v1.0.0", model="gpt-6-astra")

    generate.generate_ai_notes(
        api_key="k",
        model="gpt-6-astra",
        system_prompt="p",
        version="v1.0.0",
        commits=["Add x"],
        report=report,
        reasoning_effort="low",
    )

    assert _FakeOpenAI.last_request["reasoning_effort"] == "low"


def test_generate_ai_notes_marks_openai_error(monkeypatch) -> None:
    def boom(**_kwargs: object) -> object:
        raise generate.OpenAIError("down")

    monkeypatch.setattr(generate, "OpenAI", boom)
    report = generate.RunReport(tag="v1.0.0", model="gpt-5.5")

    notes = generate.generate_ai_notes(
        api_key="k", model="gpt-5.5", system_prompt="p", version="v1.0.0", commits=["Add x"], report=report
    )

    assert notes is None
    assert report.reason == "openai-error"
    assert report.usage == generate.Usage()  # nothing billed


def test_publish_report_writes_outputs_summary_and_notice(monkeypatch, tmp_path, capsys) -> None:
    out, summary = tmp_path / "output", tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_ACTION_REF", "release-notes/v1")
    report = generate.RunReport(
        tag="v1.2.1",
        model="gpt-5.5",
        status="ai",
        commits=3,
        usage=generate.Usage(prompt_tokens=1_000, cached_tokens=0, completion_tokens=100),
        duration_s=4.25,
    )

    generate.publish_report(report)

    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert outputs["status"] == "ai"
    assert outputs["reason"] == ""
    assert outputs["estimated-cost-usd"] == "0.008"  # (1000 * 5 + 100 * 30) / 1e6
    assert "| `v1.2.1` | ai | `gpt-5.5` | 3 | 1,000 (0) | 100 | $0.0080 | 4.25s |" in summary.read_text()
    notices = [line for line in capsys.readouterr().err.splitlines() if line.startswith("::notice ")]
    assert len(notices) == 1
    record = json.loads(notices[0].split("::", 2)[2])
    assert record["action_ref"] == "release-notes/v1"
    assert record["estimated_cost_usd"] == 0.008


def test_publish_report_handles_unknown_model_and_missing_sinks(monkeypatch, tmp_path, capsys) -> None:
    out = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)  # e.g. run outside Actions
    report = generate.RunReport(tag="v1.0.0", model="mystery-model", reason="no-api-key")

    generate.publish_report(report)

    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert outputs["status"] == "fallback"
    assert outputs["estimated-cost-usd"] == ""  # unlisted model: tokens only, no cost
    assert "::notice title=release-notes report::" in capsys.readouterr().err  # still annotated


def test_publish_report_keeps_outputs_one_per_line(monkeypatch, tmp_path, capsys) -> None:
    out = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    report = generate.RunReport(tag="v1.0.0", model="gpt-6-astra\nextra", reason="no-api-key")

    generate.publish_report(report)

    lines = out.read_text().splitlines()
    assert all("=" in line for line in lines)  # a bare line would make the runner fail the step
    assert dict(line.split("=", 1) for line in lines)["model"] == "gpt-6-astra extra"


def test_render_notes_reports_no_api_key_fallback(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(generate, "git_commit_list", lambda _prev, _cur: ["Add thing (#1)", "Fix other"])
    report = generate.RunReport(tag="v1.0.0", model="gpt-5.5")

    out = generate.render_notes("v1.0.0", "gpt-5.5", report)

    assert out == "## Changes\n\n- Add thing (#1)\n- Fix other\n"
    assert (report.status, report.reason, report.commits) == ("fallback", "no-api-key", 2)


@pytest.fixture
def path_history(tmp_path, monkeypatch):
    """Real tagged history with module, shared, and space-containing paths."""
    monkeypatch.chdir(tmp_path)

    def git(*args):
        return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout

    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "tag.gpgsign", "false")

    def commit(path, subject):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(subject)
        git("add", "--", path)
        git("commit", "-m", subject)

    commit("libs/sleepmetrics/base.py", "Old metrics (#1)")
    git("tag", "v1")
    commit("libs/postprocess/core.py", "Postprocess (#2)")
    commit("libs/sleepmetrics/core.py", "Metrics (#3)")
    commit("shared/config", "Shared (#4)")
    commit("libs/space module/core.py", "Space module (#5)")
    git("tag", "v2")
    commit("libs/sleepmetrics/later.py", "After release (#6)")
    return git, commit


def test_git_paths_preserve_unfiltered_command_and_limit(path_history):
    git, _ = path_history
    expected = git("log", "-z", "--max-count=500", "--pretty=format:%B", "v1..v2")
    expected = [entry for entry in expected.split("\x00") if entry.strip()]
    assert generate.git_commit_list("v1", "v2") == expected
    assert generate.git_commit_list("v1", "v2", paths=[]) == generate.git_commit_list("v1", "v2")
    assert [entry.strip() for entry in generate.git_commit_list("v1", "v2", 1)] == ["Space module (#5)"]


def test_git_paths_filter_range_and_multiple_space_paths(path_history):
    selected = generate.git_commit_list(
        "v1", "v2", paths=["libs/sleepmetrics", "libs/space module"]
    )
    assert [entry.strip() for entry in selected] == ["Space module (#5)", "Metrics (#3)"]
    assert [entry.strip() for entry in generate.git_commit_list(None, "v1", paths=["libs/sleepmetrics"])] == [
        "Old metrics (#1)"
    ]
    assert [entry.strip() for entry in generate.git_commit_list("v1", "v2", 1, ["libs/sleepmetrics"])] == [
        "Metrics (#3)"
    ]


@pytest.mark.parametrize("paths", [None, "", " \n\t\n"])
def test_render_blank_paths_matches_existing_output(path_history, monkeypatch, paths):
    monkeypatch.setenv("PREV", "v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if paths is None:
        monkeypatch.delenv("PATHS_INPUT", raising=False)
    else:
        monkeypatch.setenv("PATHS_INPUT", paths)
    report = generate.RunReport(tag="v2", model="test")
    assert generate.render_notes("v2", "test", report) == generate.fallback(generate.git_commit_list("v1", "v2"))
    assert (report.reason, report.commits) == ("no-api-key", 4)


@pytest.mark.parametrize("ai", [False, True])
def test_render_paths_filters_before_enrichment_and_output(path_history, monkeypatch, ai):
    monkeypatch.setenv("PREV", "v1")
    monkeypatch.setenv("PATHS_INPUT", " libs/sleepmetrics \n\nlibs/space module\n")
    monkeypatch.setenv("OPENAI_API_KEY", "test" if ai else "")
    monkeypatch.setattr(generate, "OpenAI", _FakeOpenAI)
    fetched = []
    monkeypatch.setattr(generate, "pr_body", lambda n: fetched.append(n) or f"Description {n}")
    report = generate.RunReport(tag="v2", model="test")
    out = generate.render_notes("v2", "test", report)
    assert report.commits == 2
    if ai:
        assert fetched == [5, 3]
        prompt = str(_FakeOpenAI.last_request["messages"])
        assert "Space module (#5)" in prompt and "Metrics (#3)" in prompt
        assert all(subject not in prompt for subject in ["Postprocess", "Shared", "Old metrics", "After release"])
        assert report.status == "ai"
    else:
        assert fetched == []
        assert out == "## Changes\n\n- Space module (#5)\n- Metrics (#3)\n"


def test_paths_empty_match_skips_ai_and_enrichment(path_history, monkeypatch):
    monkeypatch.setenv("PREV", "v1")
    monkeypatch.setenv("PATHS_INPUT", "missing")
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    def unexpected(*args, **kwargs):
        pytest.fail("Empty path selection must not call external services")

    monkeypatch.setattr(generate, "OpenAI", unexpected)
    monkeypatch.setattr(generate, "pr_body", unexpected)
    report = generate.RunReport(tag="v2", model="test")
    assert generate.render_notes("v2", "test", report) == "## Changes\n\n\n"
    assert (report.reason, report.commits) == ("no-commits", 0)


def test_paths_use_native_magic_and_merge_history(path_history):
    git, commit = path_history
    git("checkout", "-b", "feature", "v1")
    commit("libs/sleepmetrics/branch.py", "Branch metrics (#7)")
    git("checkout", "main")
    git("merge", "--no-ff", "feature", "-m", "Merge feature")
    paths = [":(top)libs/*", ":(exclude)libs/postprocess"]
    expected = git("log", "-z", "--max-count=500", "--pretty=format:%B", "v1..HEAD", "--", *paths)
    assert [entry.strip() for entry in generate.git_commit_list("v1", "HEAD", paths=paths)] == [
        entry.strip() for entry in expected.split("\x00") if entry.strip()
    ]


def test_pathspec_is_one_literal_argv_not_shell(path_history):
    _, commit = path_history
    path = "libs/$(touch injected); module/file.py"
    commit(path, "Literal path (#8)")
    assert [entry.strip() for entry in generate.git_commit_list("v2", "HEAD", paths=[path])] == ["Literal path (#8)"]
    assert not Path("injected").exists()
