"""Tests of the sleepless-upload action.

The step's script is run the way a composite action's `shell: bash` runs it,
against a stand-in for Sleepless (GADS): a local HTTP server that answers
POST /oauth/token and POST /apps as each test tells it to, and keeps every
request it got.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

ACTION = Path(__file__).resolve().parent / "action.yml"
TOKEN = "gads-token-7f3c9a1e5b"
SECRET = "client-secret-41d8e2"
SHA = "3ff9b1a3" + "c" * 32
DESCRIPTION = "HeySleep 2.0.1-qa.57 (200000123)"


@dataclass
class Received:
    path: str
    headers: dict
    body: bytes


class Gads(ThreadingHTTPServer):
    """Sleepless as far as the action sees it. A reply list is used up one by one, the last one stays."""

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.requests: list[Received] = []
        self.token_replies = [(200, json.dumps({"access_token": TOKEN, "token_type": "Bearer", "expires_in": 3600}).encode())]
        self.apps_replies = [(201, b'{"id": 7, "status": "queued"}')]
        self.delay = 0.0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def reply(self, path: str) -> tuple:
        replies = self.token_replies if path == "/oauth/token" else self.apps_replies
        return replies.pop(0) if len(replies) > 1 else replies[0]

    def asked(self, path: str) -> list[Received]:
        return [request for request in self.requests if request.path == path]

    def handle_error(self, request, client_address):
        # A client that gave up (max-time) leaves a broken pipe behind; that is the test's point.
        pass


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1, so that curl's "Expect: 100-continue" for a large body is answered at once.
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        chunks, left = [], length
        while left:
            chunk = self.rfile.read(min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        self.server.requests.append(Received(self.path, dict(self.headers), b"".join(chunks)))
        status, payload = self.server.reply(self.path)
        if self.server.delay:
            time.sleep(self.server.delay)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def gads():
    server = Gads()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def action() -> dict:
    return yaml.safe_load(ACTION.read_text())


def form(request: Received) -> dict:
    """The fields of a multipart/form-data body: name -> (file name or None, content)."""
    boundary = re.search(r'boundary=("?)([^";]+)\1', request.headers["Content-Type"])[2].encode()
    fields = {}
    for part in request.body.split(b"--" + boundary)[1:-1]:
        head, _, content = part[2:].partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]*)"', head)[1].decode()
        filename = re.search(rb'filename="([^"]*)"', head)
        fields[name] = (filename[1].decode() if filename else None, content[:-2])
    return fields


def apk(path: Path, size: int = 4096) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PK\x03\x04" + os.urandom(size - 4))
    return path


def run_action(action: dict, tmp_path: Path, gads: Gads | None, file: Path | str, **env: str):
    """Run the step as GitHub would: its env from the inputs, the connection from the caller's env."""
    (step,) = action["runs"]["steps"]
    temp = tmp_path / "runner-temp"
    temp.mkdir(exist_ok=True)
    output = tmp_path / "output"
    output.write_text("")
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "FILE": str(file),
        "DESC": DESCRIPTION,
        "REF": "qa-develop",
        "SHA": SHA,
        "MAX_TIME": "60",
        "GADS_URL": gads.url if gads else "http://127.0.0.1:9",
        "GADS_CLIENT_ID": "ci-client",
        "GADS_CLIENT_SECRET": SECRET,
        "GADS_TENANT": "asleep",
        "RUNNER_TEMP": str(temp),
        "GITHUB_OUTPUT": str(output),
        **env,
    }
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        cwd=tmp_path, env=environment, text=True, capture_output=True, check=False, timeout=120,
    )
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
    return result, outputs


# --- the contract ------------------------------------------------------------------------------


def test_the_inputs_and_the_step(action):
    inputs = action["inputs"]
    assert list(inputs) == ["file", "description", "ref", "sha", "max-time"]
    # file has no default any more: the iOS export folder the original fell back to is gone.
    assert inputs["file"]["required"] is True and "default" not in inputs["file"]
    assert inputs["description"]["required"] is True and "default" not in inputs["description"]
    assert inputs["ref"]["default"] == "${{ github.ref_name }}"
    assert inputs["sha"]["default"] == "${{ github.sha }}"
    assert inputs["max-time"]["default"] == "900"
    (step,) = action["runs"]["steps"]
    assert action["runs"]["using"] == "composite" and step["shell"] == "bash"
    # Every input reaches the script through the environment, never through the script's text.
    assert step["env"] == {
        "FILE": "${{ inputs.file }}", "DESC": "${{ inputs.description }}", "REF": "${{ inputs.ref }}",
        "SHA": "${{ inputs.sha }}", "MAX_TIME": "${{ inputs.max-time }}",
    }
    assert "${{" not in step["run"]
    assert action["outputs"]["http-code"]["value"] == "${{ steps.upload.outputs.http-code }}"


def test_neither_the_secret_nor_the_token_is_on_a_command_line(action):
    (step,) = action["runs"]["steps"]
    script = step["run"]
    # The secret goes from the environment into a file, and curl reads the file.
    assert '--data @"$auth"' in script and "umask 077" in script
    assert '-H @"$header"' in script
    assert "$GADS_CLIENT_SECRET" not in script and "${GADS_CLIENT_SECRET" not in script
    assert re.findall(r"\$token\b|\$\{token\}", script) == ["$token", "$token", "$token"]  # test, mask, header file


# --- uploads -----------------------------------------------------------------------------------


def test_the_file_is_uploaded_with_the_token_and_the_description(action, tmp_path, gads):
    file = apk(tmp_path / "out" / "HeySleep_qa_2.0.1-qa.57_200000123.apk")
    result, outputs = run_action(action, tmp_path, gads, file)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [request.path for request in gads.requests] == ["/oauth/token", "/apps"]
    (token,) = gads.asked("/oauth/token")
    assert token.headers["Content-Type"] == "application/json"
    assert json.loads(token.body) == {"client_id": "ci-client", "client_secret": SECRET, "tenant": "asleep"}
    (upload,) = gads.asked("/apps")
    assert upload.headers["Authorization"] == f"Bearer {TOKEN}"
    fields = form(upload)
    assert fields["file"] == ("HeySleep_qa_2.0.1-qa.57_200000123.apk", file.read_bytes())
    # The format of the medical apps' dashboard: <app> <versionName> (<versionCode>) · <ref> <commit>.
    assert fields["description"] == (None, f"{DESCRIPTION} · qa-develop 3ff9b1a".encode())
    assert outputs == {"http-code": "201"}
    assert f"Uploaded HeySleep_qa_2.0.1-qa.57_200000123.apk (4096 bytes) to Sleepless (HTTP 201): {DESCRIPTION} · qa-develop 3ff9b1a" in result.stdout
    # What Sleepless answered is in the log.
    assert '{"id": 7, "status": "queued"}' in result.stdout
    # Nothing is left in the temp directory: not the secret, not the token, not the answers.
    assert list((tmp_path / "runner-temp" / "sleepless-upload").iterdir()) == []


def test_the_token_is_masked_before_anything_else_is_printed_and_never_printed(action, tmp_path, gads):
    gads.apps_replies = [(400, json.dumps({"error": "bad", "token": TOKEN}).encode())]
    result, _ = run_action(action, tmp_path, gads, apk(tmp_path / "app.apk"))
    lines = result.stdout.splitlines()
    assert lines.count(f"::add-mask::{TOKEN}") == 1
    # The runner masks whatever comes after the mask: here the answer that echoes the token.
    mask = lines.index(f"::add-mask::{TOKEN}")
    assert all(TOKEN not in line for line in lines[:mask])
    assert TOKEN not in result.stderr
    # The client secret is not printed at all.
    assert SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize("status", [200, 201, 202, 204])
def test_every_2xx_is_an_upload(action, tmp_path, gads, status):
    gads.apps_replies = [(status, b"" if status == 204 else b"{}")]
    result, outputs = run_action(action, tmp_path, gads, apk(tmp_path / "app.apk"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"http-code": str(status)}


def test_the_description_and_the_file_name_are_sent_as_they_are(action, tmp_path, gads):
    # -F would read a value that starts with @ or < as a file, and the options after a ; or a ,
    # of a file name. Neither happens.
    file = apk(tmp_path / 'we"ird;dir,x' / "app.apk")
    description = "@/etc/passwd <x; type=text/html, HeySleep"
    result, _ = run_action(action, tmp_path, gads, file, DESC=description, REF="", SHA="")
    assert result.returncode == 0, result.stdout + result.stderr
    fields = form(gads.asked("/apps")[0])
    assert fields["file"] == ("app.apk", file.read_bytes())
    # Without ref and sha, nothing is added.
    assert fields["description"] == (None, description.encode())


@pytest.mark.parametrize(("ref", "sha", "suffix"), [("v2.0.0", "", " · v2.0.0"), ("", SHA, " · 3ff9b1a"), ("v2.0.0", SHA, " · v2.0.0 3ff9b1a")])
def test_ref_and_commit_are_added_as_given(action, tmp_path, gads, ref, sha, suffix):
    result, _ = run_action(action, tmp_path, gads, apk(tmp_path / "app.apk"), REF=ref, SHA=sha)
    assert result.returncode == 0, result.stdout + result.stderr
    assert form(gads.asked("/apps")[0])["description"] == (None, f"{DESCRIPTION}{suffix}".encode())


def test_a_trailing_slash_of_the_url_is_dropped(action, tmp_path, gads):
    result, _ = run_action(action, tmp_path, gads, apk(tmp_path / "app.apk"), GADS_URL=gads.url + "/")
    assert result.returncode == 0, result.stdout + result.stderr
    assert [request.path for request in gads.requests] == ["/oauth/token", "/apps"]


def test_a_large_file_is_streamed_whole(action, tmp_path, gads):
    # HeySleep's QA APK is about 360 MB. 64 MiB here: curl streams the file from disk, as for any
    # size, and Sleepless gets every byte.
    size = 64 * 1024 * 1024
    file = tmp_path / "HeySleep_qa_large.apk"
    with file.open("wb") as out:
        block = os.urandom(1024 * 1024)
        for index in range(64):
            out.write(block[:-1] + bytes([index]))
    expected = hashlib.sha256(file.read_bytes()).hexdigest()
    result, outputs = run_action(action, tmp_path, gads, file)
    assert result.returncode == 0, result.stdout + result.stderr
    name, content = form(gads.asked("/apps")[0])["file"]
    assert name == "HeySleep_qa_large.apk" and len(content) == size
    assert hashlib.sha256(content).hexdigest() == expected
    assert f"({size} bytes)" in result.stdout and outputs == {"http-code": "201"}


# --- failures ----------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403])
def test_a_refused_token_request_stops_before_the_upload(action, tmp_path, gads, status):
    gads.token_replies = [(status, b'{"error": "invalid_client"}')]
    result, outputs = run_action(action, tmp_path, gads, apk(tmp_path / "app.apk"))
    assert result.returncode == 1
    assert f"::error title=Sleepless token request failed::HTTP {status}. Check GADS_CLIENT_ID, GADS_CLIENT_SECRET and GADS_TENANT." in result.stdout
    assert '{"error": "invalid_client"}' in result.stdout
    assert gads.asked("/apps") == [] and outputs == {}
    assert list((tmp_path / "runner-temp" / "sleepless-upload").iterdir()) == []


@pytest.mark.parametrize("body", [b"{}", b'{"access_token": ""}', b'{"access_token": null}', b"not json", b"[]", b""])
def test_a_token_answer_without_access_token_stops_before_the_upload(action, tmp_path, gads, body):
    gads.token_replies = [(200, body)]
    result, _ = run_action(action, tmp_path, gads, apk(tmp_path / "app.apk"))
    assert result.returncode == 1
    assert "::error title=Sleepless token request failed::The answer (HTTP 200) has no access_token." in result.stdout
    assert gads.asked("/apps") == []
    assert "::add-mask::" not in result.stdout


def test_no_server_is_no_answer(action, tmp_path):
    # A port nothing listens on, as GADS_URL is from a GitHub-hosted runner.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    result, _ = run_action(action, tmp_path, None, apk(tmp_path / "app.apk"), GADS_URL=f"http://127.0.0.1:{port}")
    assert result.returncode == 1
    assert "::error title=Sleepless token request failed::No answer from GADS_URL (curl exit 7)." in result.stdout


@pytest.mark.parametrize("status", [400, 401, 404, 409, 422])
def test_a_4xx_upload_fails_and_is_not_made_again(action, tmp_path, gads, status):
    gads.apps_replies = [(status, b'{"error": "refused"}')]
    file = apk(tmp_path / "app.apk")
    result, outputs = run_action(action, tmp_path, gads, file)
    assert result.returncode == 1
    assert f"::error title=Sleepless upload failed::HTTP {status} for app.apk, 4096 bytes." in result.stdout
    assert '{"error": "refused"}' in result.stdout
    assert len(gads.asked("/apps")) == 1 and outputs == {"http-code": str(status)}


def test_a_file_too_large_for_sleepless_says_so(action, tmp_path, gads):
    gads.apps_replies = [(413, b"")]
    result, _ = run_action(action, tmp_path, gads, apk(tmp_path / "app.apk"))
    assert result.returncode == 1
    assert "::error title=Sleepless upload failed::HTTP 413: Sleepless refuses a file of 4096 bytes. Its size limit is the QA team's setting." in result.stdout


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_5xx_upload_is_made_again_and_then_fails(action, tmp_path, gads, status):
    gads.apps_replies = [(status, b'{"error": "busy"}')]
    result, outputs = run_action(action, tmp_path, gads, apk(tmp_path / "app.apk"))
    assert result.returncode == 1
    # curl's --retry 2: three attempts, within max-time.
    assert len(gads.asked("/apps")) == 3
    assert f"::error title=Sleepless upload failed::HTTP {status} for app.apk, 4096 bytes." in result.stdout
    assert outputs == {"http-code": str(status)}
    # One token for all three.
    assert len(gads.asked("/oauth/token")) == 1


def test_an_upload_that_succeeds_when_made_again_is_an_upload(action, tmp_path, gads):
    gads.apps_replies = [(503, b""), (201, b'{"id": 8}')]
    file = apk(tmp_path / "app.apk")
    result, outputs = run_action(action, tmp_path, gads, file)
    assert result.returncode == 0, result.stdout + result.stderr
    uploads = gads.asked("/apps")
    assert len(uploads) == 2 and form(uploads[1])["file"][1] == file.read_bytes()
    assert outputs == {"http-code": "201"}


def test_an_upload_without_an_answer_within_max_time_fails_and_is_not_made_again(action, tmp_path, gads):
    gads.delay = 3
    result, outputs = run_action(action, tmp_path, gads, apk(tmp_path / "app.apk"), MAX_TIME="1")
    assert result.returncode == 1
    assert "::error title=Sleepless upload failed::No answer within max-time (1 seconds) for app.apk, 4096 bytes. Raise max-time when the network to GADS_URL is that slow." in result.stdout
    # The retry window is max-time from the first attempt, and it is over.
    assert len(gads.asked("/apps")) == 1
    assert outputs == {"http-code": "000"}


@pytest.mark.parametrize("file", ["", "missing.apk"])
def test_a_missing_file_stops_before_any_request(action, tmp_path, gads, file):
    result, _ = run_action(action, tmp_path, gads, str(tmp_path / file) if file else "")
    assert result.returncode == 1
    assert "::error title=No file to upload::The file to upload does not exist: " in result.stdout
    assert gads.requests == []


def test_missing_settings_are_named_together(action, tmp_path, gads):
    result, _ = run_action(action, tmp_path, gads, apk(tmp_path / "app.apk"), GADS_URL="", GADS_CLIENT_SECRET="")
    assert result.returncode == 1
    assert "::error title=Sleepless settings missing::Empty: GADS_URL, GADS_CLIENT_SECRET. " in result.stdout
    assert gads.requests == []


@pytest.mark.parametrize("value", ["", "0", "1.5", "soon", "-1", "123456"])
def test_a_max_time_that_is_no_number_of_seconds_is_refused(action, tmp_path, gads, value):
    result, _ = run_action(action, tmp_path, gads, apk(tmp_path / "app.apk"), MAX_TIME=value)
    assert result.returncode == 1
    assert f"::error title=Invalid input::max-time is '{value}'. It is a whole number of seconds." in result.stdout
    assert gads.requests == []
