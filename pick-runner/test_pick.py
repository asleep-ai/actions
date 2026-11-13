"""Unit tests for pick.py.

Run: uv run --with pytest python -m pytest pick-runner -q
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("pick", Path(__file__).with_name("pick.py"))
assert _spec is not None and _spec.loader is not None
pick = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = pick
_spec.loader.exec_module(pick)

API = "https://api.github.com"
LABELS = ["self-hosted", "Linux", "X64"]


def runner(status="online", labels=("self-hosted", "Linux", "X64"), busy=False):
    return {"status": status, "busy": busy, "labels": [{"name": name} for name in labels]}


def pages(*documents):
    """A fetch stub serving one document per page, linked with rel=next."""
    calls = []

    def fetch(url, token):
        calls.append((url, token))
        index = len(calls) - 1
        link = None
        if index + 1 < len(documents):
            link = f'<{API}/orgs/acme/actions/runners?per_page=100&page={index + 2}>; rel="next"'
        body = documents[index]
        return (body if isinstance(body, bytes) else json.dumps(body).encode()), link

    fetch.calls = calls
    return fetch


def run_main(tmp_path, fetch, **overrides):
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    env = {
        "LABELS": json.dumps(LABELS),
        "FALLBACK": "ubuntu-latest",
        "TOKEN": "t0ken",
        "ORG": "acme",
        "GITHUB_API_URL": API,
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    env.update(overrides)
    code = pick.main(env, fetch)
    outputs = {}
    if output.exists():
        for line in output.read_text().splitlines():
            key, _, value = line.partition("=")
            outputs[key] = value
    return code, outputs


def test_online_runner_picks_labels(tmp_path):
    code, out = run_main(tmp_path, pages({"runners": [runner(), runner(status="offline")]}))
    assert code == 0
    assert json.loads(out["runs-on"]) == LABELS
    assert out["self-hosted"] == "true"
    assert out["online-count"] == "1"


def test_busy_runner_counts_as_available(tmp_path):
    _, out = run_main(tmp_path, pages({"runners": [runner(busy=True)]}))
    assert out["self-hosted"] == "true"
    assert out["online-count"] == "1"


def test_all_offline_picks_fallback(tmp_path, capsys):
    _, out = run_main(tmp_path, pages({"runners": [runner(status="offline")]}))
    assert json.loads(out["runs-on"]) == "ubuntu-latest"
    assert out["self-hosted"] == "false"
    assert out["online-count"] == "0"
    assert "::warning title=Self-hosted runners offline::" in capsys.readouterr().out


def test_runner_missing_a_label_does_not_count(tmp_path):
    _, out = run_main(tmp_path, pages({"runners": [runner(labels=("self-hosted", "Linux"))]}))
    assert out["self-hosted"] == "false"


def test_labels_compare_case_insensitively(tmp_path):
    _, out = run_main(tmp_path, pages({"runners": [runner(labels=("Self-Hosted", "linux", "x64"))]}))
    assert out["self-hosted"] == "true"
    # The caller's labels are echoed back as given.
    assert json.loads(out["runs-on"]) == LABELS


def test_follows_pagination(tmp_path):
    fetch = pages(
        {"runners": [runner(status="offline")] * 100},
        {"runners": [runner()]},
    )
    _, out = run_main(tmp_path, fetch)
    assert out["online-count"] == "1"
    assert len(fetch.calls) == 2
    assert all(token == "t0ken" for _, token in fetch.calls)


def test_pagination_outside_api_is_a_failed_check(tmp_path, capsys):
    def fetch(url, token):
        return json.dumps({"runners": []}).encode(), '<https://evil.example/next>; rel="next"'

    _, out = run_main(tmp_path, fetch)
    assert out["self-hosted"] == "true"
    assert out["online-count"] == ""
    assert "pagination link points outside" in capsys.readouterr().out


@pytest.mark.parametrize("url", [
    "https://api.github.com.evil.example/next",
    "https://api.github.com@evil.example/next",
])
def test_pagination_to_a_lookalike_host_is_refused(url):
    with pytest.raises(pick.CheckError):
        pick.next_link(f'<{url}>; rel="next"', API)


def test_fallback_array(tmp_path):
    _, out = run_main(tmp_path, pages({"runners": []}), FALLBACK='["ubuntu-24.04"]')
    assert json.loads(out["runs-on"]) == ["ubuntu-24.04"]


def test_fallback_json_string_is_one_label(tmp_path):
    # The form a runs-on output is written in; the quotes are not part of the label.
    _, out = run_main(tmp_path, pages({"runners": []}), FALLBACK='"ubuntu-latest"')
    assert json.loads(out["runs-on"]) == "ubuntu-latest"


@pytest.mark.parametrize("fallback", [
    "",
    "[]",
    '["ubuntu-latest", 1]',
    '[" ubuntu-latest"]',
    '{"group":"larger"}',
    "ubuntu-latest, macos-latest",
    "ubuntu-latest macos-latest",
    '"ubuntu-latest',
    '""',
])
def test_invalid_fallback_fails_the_step(tmp_path, capsys, fallback):
    code, out = run_main(tmp_path, pages({"runners": []}), FALLBACK=fallback)
    assert code == 1
    assert out == {}
    # The message names the input that is wrong.
    assert "::error title=pick-runner::fallback " in capsys.readouterr().out


def test_no_token_picks_labels_unchecked(tmp_path, capsys):
    fetch = pages({"runners": []})
    _, out = run_main(tmp_path, fetch, TOKEN="")
    assert out["self-hosted"] == "true"
    assert out["online-count"] == ""
    assert fetch.calls == []
    assert "::warning title=Runner check skipped::" in capsys.readouterr().out


def test_http_error_picks_labels_unchecked(tmp_path, capsys):
    def fetch(url, token):
        raise pick.CheckError("HTTP 403: forbidden")

    _, out = run_main(tmp_path, fetch)
    assert out["self-hosted"] == "true"
    assert out["online-count"] == ""
    printed = capsys.readouterr().out
    assert "::warning title=Runner check failed::" in printed
    assert "HTTP 403: forbidden" in printed


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"total_count": 0}', b'{"runners": ["x"]}'])
def test_unreadable_response_picks_labels_unchecked(tmp_path, capsys, body):
    _, out = run_main(tmp_path, pages(body))
    assert out["self-hosted"] == "true"
    assert out["online-count"] == ""
    # A recognised failure with its reason, not a crash caught by the catch-all.
    assert "unexpected error" not in capsys.readouterr().out


def test_unexpected_error_picks_labels_unchecked(tmp_path, capsys):
    # What http.client raises for a token it cannot send as a header.
    def fetch(url, token):
        raise ValueError(f"Invalid header value b'Bearer {token}'")

    code, out = run_main(tmp_path, fetch)
    assert code == 0
    assert out["self-hosted"] == "true"
    assert out["online-count"] == ""
    printed = capsys.readouterr().out
    assert "::warning title=Runner check failed::" in printed
    assert "unexpected error (ValueError)" in printed
    assert "t0ken" not in printed


def test_token_whitespace_is_stripped(tmp_path):
    fetch = pages({"runners": [runner()]})
    _, out = run_main(tmp_path, fetch, TOKEN=" t0ken\n")
    assert out["online-count"] == "1"
    assert [token for _, token in fetch.calls] == ["t0ken"]


def test_blank_token_is_no_token(tmp_path, capsys):
    fetch = pages({"runners": []})
    _, out = run_main(tmp_path, fetch, TOKEN=" \n")
    assert out["self-hosted"] == "true"
    assert fetch.calls == []
    assert "::warning title=Runner check skipped::" in capsys.readouterr().out


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append((self.path, self.headers.get("Authorization")))
        self.server.respond(self)

    def log_message(self, *args):
        pass


def reply(handler, status, body=b"", headers=(), length=None):
    handler.send_response(status)
    for name, value in headers:
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(body) if length is None else length))
    handler.end_headers()
    handler.wfile.write(body)


@pytest.fixture
def serve():
    """Start local HTTP servers; each answers GETs with the given function."""
    servers = []

    def start(respond):
        server = HTTPServer(("127.0.0.1", 0), _Handler)
        server.requests = []
        server.respond = respond
        server.url = f"http://127.0.0.1:{server.server_port}"
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_http_get_returns_body_and_link(serve):
    api = serve(lambda h: reply(h, 200, b'{"runners": []}', [("Link", '<x>; rel="next"')]))
    body, link = pick.http_get(f"{api.url}/orgs/acme/actions/runners", "t0ken")
    assert body == b'{"runners": []}'
    assert link == '<x>; rel="next"'
    assert api.requests == [("/orgs/acme/actions/runners", "Bearer t0ken")]


def test_http_get_http_error_is_a_failed_check(serve):
    api = serve(lambda h: reply(h, 403, b'{"message": "Forbidden"}'))
    with pytest.raises(pick.CheckError, match="HTTP 403: .*Forbidden"):
        pick.http_get(f"{api.url}/runners", "t0ken")


def test_http_get_truncated_body_is_a_failed_check(serve):
    api = serve(lambda h: reply(h, 200, b'{"runners": [', length=5000))
    with pytest.raises(pick.CheckError):
        pick.http_get(f"{api.url}/runners", "t0ken")


def test_http_get_reply_that_is_not_http_is_a_failed_check(serve):
    api = serve(lambda h: h.wfile.write(b"not http at all\r\n\r\n"))
    with pytest.raises(pick.CheckError):
        pick.http_get(f"{api.url}/runners", "t0ken")


def test_http_get_does_not_follow_a_redirect_to_another_host(serve):
    other = serve(lambda h: reply(h, 200, b'{"runners": []}'))
    api = serve(lambda h: reply(h, 302, headers=[("Location", f"{other.url}/elsewhere")]))
    with pytest.raises(pick.CheckError, match="redirect points outside"):
        pick.http_get(f"{api.url}/runners", "t0ken")
    # The token never reached the other host.
    assert other.requests == []


def test_http_get_follows_a_redirect_on_the_same_host(serve):
    def respond(handler):
        if handler.path == "/old":
            reply(handler, 301, headers=[("Location", f"{handler.server.url}/new")])
        else:
            reply(handler, 200, b'{"runners": []}')

    api = serve(respond)
    body, _ = pick.http_get(f"{api.url}/old", "t0ken")
    assert body == b'{"runners": []}'
    assert [path for path, _ in api.requests] == ["/old", "/new"]


def test_truncated_body_picks_labels_unchecked(tmp_path, serve, capsys):
    api = serve(lambda h: reply(h, 200, b'{"runners": [', length=5000))
    code, out = run_main(tmp_path, pick.http_get, GITHUB_API_URL=api.url)
    assert code == 0
    assert out["self-hosted"] == "true"
    assert out["online-count"] == ""
    assert "::warning title=Runner check failed::" in capsys.readouterr().out


@pytest.mark.parametrize("labels", [
    "", "self-hosted", "[]", '["ok", ""]', '["ok", 1]', '{"a": 1}', '[" self-hosted", "Linux"]',
])
def test_invalid_labels_fail_the_step(tmp_path, capsys, labels):
    code, out = run_main(tmp_path, pages({"runners": []}), LABELS=labels)
    assert code == 1
    assert out == {}
    assert "::error title=pick-runner::" in capsys.readouterr().out


def test_empty_organization_fails_the_step(tmp_path):
    code, _ = run_main(tmp_path, pages({"runners": []}), ORG=" ")
    assert code == 1


def test_warning_text_cannot_break_out_of_the_command():
    assert pick.escape("a\nb%c\r") == "a%0Ab%25c%0D"


def test_summary_written(tmp_path):
    run_main(tmp_path, pages({"runners": [runner()]}))
    assert "pick-runner: **self-hosted**" in (tmp_path / "summary").read_text()


def test_action_yml_expressions_only_where_evaluated_on_purpose():
    # GitHub evaluates ${{ }} anywhere in action.yml, descriptions included, and
    # refuses to load the action when one does not parse.
    import re

    allowed = re.compile(r"^\s*(default|value|[A-Z_]+):\s*\$\{\{")
    text = (Path(__file__).with_name("action.yml")).read_text()
    offending = [line for line in text.splitlines() if "${{" in line and not allowed.match(line)]
    assert offending == []
