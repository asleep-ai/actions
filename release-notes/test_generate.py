"""Unit tests for generate.py.

Run: uv run --with pytest --with 'openai>=1.55,<2' python -m pytest test_generate.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("generate", Path(__file__).with_name("generate.py"))
assert _spec is not None and _spec.loader is not None
generate = importlib.util.module_from_spec(_spec)
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


def test_pr_body_returns_empty_when_gh_missing(monkeypatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(generate.subprocess, "run", boom)
    generate.pr_body.cache_clear()

    assert generate.pr_body(99999) == ""  # gh absent -> fallback, no crash


def test_format_dedupes_repeated_ref_within_commit(monkeypatch) -> None:
    calls: list[int] = []

    def fake(n: int) -> str:
        calls.append(n)
        return "body"

    monkeypatch.setattr(generate, "pr_body", fake)

    generate.format_commits_for_prompt(["Revert revert of thing (#42) (#42)"])

    assert calls == [42]  # looked up once despite two refs
