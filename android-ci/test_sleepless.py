"""Tests of android-sleepless.yml.

The run steps are executed as the shell GitHub would run them, against a
local stand-in for the GitHub API (tags, releases and their assets, refs and
tag objects) that also stands in for Sleepless, and a stand-in for aapt2 that
reads the badging line the test put into the APK. The wiring tests pin what
cannot be executed: the job conditions, where each job runs, the permissions,
which step gets which secret, and the copies of the scripts the two jobs share.
The upload job is also run end to end, with the script of sleepless-upload.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from test_pr_check import OWN_ACTION, ROOT, WORKFLOWS, executable, load, run_script, step, trigger

ENTRY = "android-sleepless.yml"
EXAMPLE = ROOT / "android-ci" / "sleepless.yml.example"
README = ROOT / "android-ci" / "README.md"
ACTION = ROOT / "sleepless-upload" / "action.yml"
REPOSITORY = "acme/heysleep"
BASE = f"/repos/{REPOSITORY}/"
COMMIT = "3ff9b1a3" + "0" * 32
OTHER_COMMIT = "9c1d2e3f" + "1" * 32
PACKAGE = "com.asleep.heysleep"
APK_NAME = "HeySleep_qa_2.0.1-qa.57_200000123.apk"
LABELS = '["self-hosted","threadripper"]'
TOKEN = "gads-token-5e1f"


def badging(package: str = PACKAGE, code: str = "200000123", name: str = "2.0.1-qa.57") -> str:
    return (
        f"package: name='{package}' versionCode='{code}' versionName='{name}' platformBuildVersionName='15' "
        "platformBuildVersionCode='35' compileSdkVersion='35' compileSdkVersionCodename='15'"
    )


def apk_bytes(**fields: str) -> bytes:
    """An "APK" whose badging the stand-in for aapt2 prints."""
    return b"PK\x03\x04\nBADGING:" + badging(**fields).encode() + b"\n" + os.urandom(4096)


AAPT2 = f"""#!{sys.executable}
import sys
data = open(sys.argv[3], "rb").read()
for line in data.split(b"\\n"):
    if line.startswith(b"BADGING:"):
        print(line[len(b"BADGING:"):].decode())
        print("sdkVersion:'26'")
        sys.exit(0)
sys.stderr.write("ERROR: dump failed because no AndroidManifest.xml found.\\n")
sys.exit(1)
"""


# --- a stand-in for the GitHub API, and for Sleepless ----------------------------------------------


class GitHub(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.refs: dict[str, dict] = {}
        self.objects: dict[str, dict] = {}
        self.releases: dict[str, dict] = {}
        # A release that changes while it is read: answered one by one, the last one stays.
        self.release_reads: dict[str, list[dict]] = {}
        self.files: dict[int, bytes] = {}
        self.requests: list[tuple] = []
        # (method, start of the path below the repository) -> statuses to answer, used up one by one.
        self.refusals: dict[tuple, list[int]] = {}
        self.redirect = False
        self.page_size = 100
        self.next_asset = 100

    @property
    def api(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def tag(self, name: str, commit: str = COMMIT, message: str | None = None) -> None:
        """A tag: annotated with message, else lightweight."""
        if message is None:
            self.refs[f"refs/tags/{name}"] = {"type": "commit", "sha": commit}
            return
        sha = hashlib.sha1(f"{name}\n{commit}\n{message}".encode()).hexdigest()
        self.objects[sha] = {"sha": sha, "tag": name, "message": message, "object": {"type": "commit", "sha": commit}}
        self.refs[f"refs/tags/{name}"] = {"type": "tag", "sha": sha}

    def release(self, tag: str, **assets: bytes) -> dict[str, int]:
        ids = {}
        listed = []
        for name, content in assets.items():
            self.next_asset += 1
            self.files[self.next_asset] = content
            ids[name] = self.next_asset
            listed.append({"id": self.next_asset, "name": name, "size": len(content), "state": "uploaded"})
        self.releases[tag] = {"tag_name": tag, "assets": listed}
        return ids

    def uploaded(self, code: str, package: str = PACKAGE) -> None:
        self.refs[f"refs/sleepless/{package}/{code}"] = {"type": "commit", "sha": COMMIT}

    def refused(self, method: str, rest: str) -> int | None:
        for (asked, start), statuses in self.refusals.items():
            if asked == method and rest.startswith(start) and statuses:
                return statuses.pop(0)
        return None

    def asked(self, method: str, start: str) -> list[tuple]:
        return [request for request in self.requests if request[0] == method and request[1].startswith(start)]

    def handle_error(self, request, client_address):
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def answer(self, status: int, document=None, raw: bytes | None = None, headers: dict | None = None) -> None:
        body = raw if raw is not None else (json.dumps(document).encode() if document is not None else b"")
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        chunks, left = [], length
        while left:
            chunk = self.rfile.read(min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)

    def do_GET(self):
        server: GitHub = self.server
        path, _, query = self.path.partition("?")
        server.requests.append(("GET", path, dict(self.headers)))
        if path.startswith("/download/"):
            content = server.files.get(int(path.rsplit("/", 1)[1]))
            return self.answer(200, raw=content) if content is not None else self.answer(404, {"message": "Not Found"})
        rest = path[len(BASE):] if path.startswith(BASE) else None
        if rest is None:
            return self.answer(404, {"message": "Not Found"})
        status = server.refused("GET", rest)
        if status:
            return self.answer(status, {"message": "refused"})
        if match := re.fullmatch(r"git/ref/tags/(.+)", rest):
            name = urllib.parse.unquote(match[1])
            ref = server.refs.get(f"refs/tags/{name}")
            return self.answer(200, {"ref": f"refs/tags/{name}", "object": ref}) if ref else self.answer(404, {"message": "Not Found"})
        if match := re.fullmatch(r"git/tags/([0-9a-f]{40})", rest):
            item = server.objects.get(match[1])
            return self.answer(200, item) if item else self.answer(404, {"message": "Not Found"})
        if match := re.fullmatch(r"releases/tags/(.+)", rest):
            name = urllib.parse.unquote(match[1])
            reads = server.release_reads.get(name)
            release = (reads.pop(0) if len(reads) > 1 else reads[0]) if reads else server.releases.get(name)
            return self.answer(200, release) if release else self.answer(404, {"message": "Not Found"})
        if match := re.fullmatch(r"releases/assets/([0-9]+)", rest):
            content = server.files.get(int(match[1]))
            if content is None:
                return self.answer(404, {"message": "Not Found"})
            if server.redirect:
                return self.answer(302, headers={"Location": f"{server.api}/download/{match[1]}"})
            return self.answer(200, raw=content)
        if match := re.fullmatch(r"git/matching-refs/(.+)", rest):
            prefix = "refs/" + match[1]
            refs = sorted(name for name in server.refs if name.startswith(prefix))
            page = int(urllib.parse.parse_qs(query).get("page", ["1"])[0])
            listed = refs[(page - 1) * server.page_size:page * server.page_size]
            headers = {}
            if page * server.page_size < len(refs):
                headers["Link"] = f'<{server.api}{path}?page={page + 1}>; rel="next"'
            return self.answer(200, [{"ref": name, "object": server.refs[name]} for name in listed], headers=headers)
        return self.answer(404, {"message": "Not Found"})

    def do_POST(self):
        server: GitHub = self.server
        raw = self.body()
        server.requests.append(("POST", self.path, dict(self.headers), raw))
        if self.path == "/gads/oauth/token":
            return self.answer(200, {"access_token": TOKEN})
        if self.path == "/gads/apps":
            return self.answer(201, {"id": 1})
        rest = self.path[len(BASE):] if self.path.startswith(BASE) else ""
        status = server.refused("POST", rest)
        if status:
            return self.answer(status, {"message": "Repository rule violations found" if status == 422 else "refused"})
        body = json.loads(raw)
        if rest == "git/tags":
            sha = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()
            server.objects[sha] = {**body, "sha": sha, "object": {"type": body["type"], "sha": body["object"]}}
            return self.answer(201, {"sha": sha})
        if rest == "git/refs":
            if body["ref"] in server.refs:
                return self.answer(422, {"message": "Reference already exists"})
            server.refs[body["ref"]] = {"type": "tag" if body["sha"] in server.objects else "commit", "sha": body["sha"]}
            return self.answer(201, {"ref": body["ref"]})
        return self.answer(404, {"message": "Not Found"})


@pytest.fixture
def github():
    server = GitHub()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def entry() -> dict:
    return load(ENTRY)


@pytest.fixture(scope="module")
def plan(entry) -> dict:
    return entry["jobs"]["plan"]


@pytest.fixture(scope="module")
def upload(entry) -> dict:
    return entry["jobs"]["upload"]


def run_step(job: dict, name: str, tmp_path: Path, github: GitHub | None = None, **env: str):
    """Run one step's script with the runner's variables; returns the result and the step's outputs."""
    output = tmp_path / "output"
    output.write_text("")
    summary = tmp_path / "summary"
    if not summary.exists():
        summary.write_text("")
    temp = tmp_path / "runner-temp"
    temp.mkdir(exist_ok=True)
    environment = {
        "GITHUB_API_URL": github.api if github else "http://127.0.0.1:9",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
        "GITHUB_RUN_ID": "4711",
        "RUNNER_TEMP": str(temp),
        "TOKEN": "job-token",
        **env,
    }
    result = run_script(step(job, name)["run"], environment, tmp_path)
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
    return result, outputs


def sdk(tmp_path: Path) -> Path:
    """An Android SDK with two build-tools versions; only the higher one has a working aapt2."""
    home = tmp_path / "android-sdk"
    for version, body in (("35.0.0", "#!/bin/sh\nexit 3\n"), ("36.1.0", AAPT2), ("9.0.0", "#!/bin/sh\nexit 4\n")):
        directory = home / "build-tools" / version
        directory.mkdir(parents=True)
        executable(directory / "aapt2", body)
    return home


# --- the contract --------------------------------------------------------------------------------


def test_the_inputs_the_secrets_and_the_outputs(entry):
    call = trigger(entry)
    inputs = call["inputs"]
    assert list(inputs) == ["tag", "app", "asset", "runner-labels", "upload-max-time"]
    assert inputs["tag"]["required"] is True and "default" not in inputs["tag"]
    assert inputs["app"]["required"] is True and "default" not in inputs["app"]
    assert inputs["asset"]["default"] == ""
    # Pinned to the self-hosted runners of the office network.
    assert inputs["runner-labels"]["default"] == LABELS
    assert inputs["upload-max-time"] == {**inputs["upload-max-time"], "type": "number", "default": 900}
    # Two secrets, passed by name. The variables are read through vars and are not passed.
    assert list(call["secrets"]) == ["GADS_CLIENT_SECRET", "RUNNER_READ_TOKEN"]
    assert all(secret["required"] is False for secret in call["secrets"].values())
    used = set(re.findall(r"secrets\.([A-Z0-9_]+)", (WORKFLOWS / ENTRY).read_text()))
    assert used == set(call["secrets"])
    assert call["outputs"]["uploaded"]["value"] == "${{ jobs.upload.outputs.uploaded == 'true' }}"
    assert call["outputs"]["version-code"]["value"] == "${{ jobs.plan.outputs.version-code }}"


def test_the_plan_runs_on_github_and_the_upload_on_the_labels_and_nowhere_else(entry, plan, upload):
    assert list(entry["jobs"]) == ["plan", "upload"]
    assert plan["runs-on"] == "ubuntu-latest"
    # The labels as they were given: no output of pick-runner, so no fallback can get here.
    assert upload["runs-on"] == "${{ fromJSON(inputs.runner-labels) }}"
    assert upload["needs"] == "plan"
    assert upload["if"] == "${{ needs.plan.outputs.run == 'true' }}"
    assert upload["timeout-minutes"] == 60
    pick = step(plan, "Pick the runner")
    assert pick["uses"] == "asleep-ai/actions/pick-runner@pick-runner/v1"
    assert pick["if"] == "${{ steps.uploads.outputs.upload == 'true' }}"
    assert pick["with"] == {"labels": "${{ inputs.runner-labels }}", "token": "${{ secrets.RUNNER_READ_TOKEN }}", "fallback": "ubuntu-latest"}
    # What the upload job decides on is the plan's word, and the plan's word follows pick-runner's self-hosted.
    where = step(plan, "Decide where the upload runs")
    assert where["env"]["SELF_HOSTED"] == "${{ steps.pick.outputs.self-hosted }}"
    assert plan["outputs"]["run"] == "${{ steps.where.outputs.run }}"
    assert "runs-on" not in json.dumps(plan["outputs"])


def test_the_steps_in_their_order(plan, upload):
    assert [item.get("name") for item in plan["steps"]] == [
        "Check the inputs and the settings", "Find the APK", "Download the APK", "Read the APK",
        "Compare with the uploads", "Pick the runner", "Decide where the upload runs",
    ]
    assert [item.get("name") for item in upload["steps"]] == [
        "Download the APK", "Compare with the uploads", "Upload to Sleepless", "Record the upload", "Delete the APK",
    ]
    # The upload job checks nothing out and runs no code of the repository.
    assert not any("actions/checkout" in str(item.get("uses", "")) for item in upload["steps"])
    assert step(plan, "Read the APK")["if"] == step(plan, "Compare with the uploads")["if"] == "${{ steps.download.outputs.found == 'true' }}"
    assert step(upload, "Compare with the uploads")["if"] == "${{ steps.download.outputs.found == 'true' }}"
    assert step(upload, "Upload to Sleepless")["if"] == "${{ steps.uploads.outputs.upload == 'true' }}"
    assert step(upload, "Record the upload")["if"] == "${{ steps.upload.outcome == 'success' }}"
    assert step(upload, "Delete the APK")["if"] == "${{ always() }}"
    assert upload["outputs"] == {"uploaded": "${{ steps.upload.outcome == 'success' }}"}


def test_the_permissions_are_the_least_the_jobs_need(entry, plan, upload):
    assert entry["permissions"] == {"contents": "read"}
    assert plan["permissions"] == {"contents": "read"}
    # The ref that records an upload.
    assert upload["permissions"] == {"contents": "write"}
    example = yaml.safe_load(EXAMPLE.read_text())
    assert example["permissions"] == {"contents": "write"}


def test_the_secret_reaches_the_upload_step_alone(plan, upload):
    upload_step = step(upload, "Upload to Sleepless")
    assert upload_step["uses"] == "asleep-ai/actions/sleepless-upload@android-ci/v1"
    assert upload_step["env"] == {
        "GADS_URL": "${{ vars.GADS_URL }}",
        "GADS_CLIENT_ID": "${{ vars.GADS_CLIENT_ID }}",
        "GADS_CLIENT_SECRET": "${{ secrets.GADS_CLIENT_SECRET }}",
        "GADS_TENANT": "${{ vars.GADS_TENANT }}",
    }
    for job in (plan, upload):
        for item in job["steps"]:
            text = json.dumps(item)
            if item is upload_step:
                continue
            # The plan sees whether a value is set, never the value.
            assert "secrets.GADS_CLIENT_SECRET }}" not in text, item["name"]
            assert "vars.GADS_URL }}" not in text, item["name"]
            if "RUNNER_READ_TOKEN" in text:
                assert item["name"] == "Pick the runner"
    check = step(plan, "Check the inputs and the settings")
    assert check["env"]["GADS_CLIENT_SECRET_SET"] == "${{ secrets.GADS_CLIENT_SECRET != '' }}"
    assert check["env"]["GADS_URL_SET"] == "${{ vars.GADS_URL != '' }}"


def test_the_upload_step_describes_the_build_as_the_medical_apps_do(upload):
    upload_step = step(upload, "Upload to Sleepless")
    action = yaml.safe_load(ACTION.read_text())
    assert set(upload_step["with"]) <= set(action["inputs"])
    assert upload_step["with"] == {
        "file": "${{ steps.download.outputs.path }}",
        # <app> <versionName> (<versionCode>), and the action adds " · <tag> <commit>".
        "description": "${{ format('{0} {1} ({2})', inputs.app, needs.plan.outputs.version-name, needs.plan.outputs.version-code) }}",
        "ref": "${{ inputs.tag }}",
        "sha": "${{ needs.plan.outputs.commit }}",
        "max-time": "${{ inputs.upload-max-time }}",
    }


def test_the_jobs_share_one_download_script_and_one_comparison(plan, upload):
    # The upload job reads the APK again and asks again; the scripts are the plan's.
    for name in ("Download the APK", "Compare with the uploads"):
        assert step(plan, name)["run"] == step(upload, name)["run"], name
        assert set(step(plan, name)["env"]) == set(step(upload, name)["env"]), name
    # The plan does not compare bytes with anything; the upload job compares with the plan's.
    assert step(plan, "Download the APK")["env"]["EXPECTED_SHA256"] == ""
    assert step(upload, "Download the APK")["env"]["EXPECTED_SHA256"] == "${{ needs.plan.outputs.sha256 }}"
    assert step(upload, "Download the APK")["env"]["EXPECTED_SIZE"] == "${{ needs.plan.outputs.size }}"
    # The rolling tag is the plan's to compare: the bytes the upload job has are the plan's.
    assert step(plan, "Compare with the uploads")["env"]["TAG_CODE"] == "${{ steps.find.outputs.tag-code }}"
    assert step(upload, "Compare with the uploads")["env"]["TAG_CODE"] == ""


def test_actions_are_referenced_by_full_path_and_their_tags():
    lines = re.findall(r"^\s*(?:- )?uses: (.+)$", (WORKFLOWS / ENTRY).read_text(), re.MULTILINE)
    refs = {}
    for line in lines:
        match = OWN_ACTION.match(line)
        assert match, line
        assert (ROOT / match["name"] / "action.yml").is_file()
        refs.setdefault(match["name"], set()).add(match["ref"])
    assert refs == {"pick-runner": {"pick-runner/v1"}, "sleepless-upload": {"android-ci/v1"}}


def test_every_output_the_jobs_read_is_written(entry):
    text = (WORKFLOWS / ENTRY).read_text()
    read = set(re.findall(r"needs\.plan\.outputs\.([a-z0-9-]+)", text)) | set(re.findall(r"jobs\.plan\.outputs\.([a-z0-9-]+)", text))
    assert read <= set(entry["jobs"]["plan"]["outputs"])
    for job in entry["jobs"].values():
        for name, value in job.get("outputs", {}).items():
            assert value.startswith("${{ steps."), name


# --- inputs and settings -------------------------------------------------------------------------


def check_env(**overrides) -> dict:
    return {
        "TAG": "qa-develop", "APP": "HeySleep", "LABELS": LABELS, "MAX_TIME": "900",
        "GADS_URL_SET": "true", "GADS_CLIENT_ID_SET": "true", "GADS_TENANT_SET": "true", "GADS_CLIENT_SECRET_SET": "true",
        **overrides,
    }


@pytest.mark.parametrize("tag", ["qa-develop", "qa-release-1.x", "qa-develop-ble-test", "v2.0.0", "v2.1.0-preview.1"])
def test_the_inputs_of_a_run(plan, tmp_path, tag):
    result, _ = run_step(plan, "Check the inputs and the settings", tmp_path, **check_env(TAG=tag))
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"tag {tag}, app HeySleep, runner labels {LABELS}, upload-max-time 900 seconds." in result.stdout


@pytest.mark.parametrize(
    ("overrides", "said"),
    [
        ({"TAG": ""}, "tag is ''."),
        ({"TAG": "refs/tags/v1"}, "tag is 'refs/tags/v1'."),
        ({"TAG": "-v1"}, "tag is '-v1'."),
        ({"APP": ""}, "app is ''."),
        ({"APP": "  "}, "app is '  '."),
        ({"APP": "x" * 61}, "It is the name of the app in the dashboard"),
        ({"MAX_TIME": "59"}, "upload-max-time is 59. It is a whole number of seconds from 60 to 1500"),
        ({"MAX_TIME": "1501"}, "upload-max-time is 1501."),
        ({"MAX_TIME": "900.5"}, "upload-max-time is 900.5."),
        ({"LABELS": '["threadripper"]'}, "runner-labels is [\"threadripper\"]. It is a JSON array of labels that holds self-hosted"),
        ({"LABELS": "ubuntu-latest"}, "runner-labels is ubuntu-latest."),
        ({"LABELS": "[]"}, "runner-labels is []."),
        ({"LABELS": '["self-hosted", " threadripper"]'}, "runner-labels is"),
    ],
)
def test_inputs_that_cannot_be_are_refused(plan, tmp_path, overrides, said):
    result, _ = run_step(plan, "Check the inputs and the settings", tmp_path, **check_env(**overrides))
    assert result.returncode == 1
    assert "::error title=Invalid input::" in result.stdout and said in result.stdout


def test_labels_compare_as_github_compares_them(plan, tmp_path):
    result, _ = run_step(plan, "Check the inputs and the settings", tmp_path, **check_env(LABELS='["Self-Hosted","threadripper"]'))
    assert result.returncode == 0, result.stdout + result.stderr


def test_missing_settings_are_named_together_before_anything_is_downloaded(plan, tmp_path):
    result, _ = run_step(
        plan, "Check the inputs and the settings", tmp_path, **check_env(GADS_URL_SET="false", GADS_CLIENT_SECRET_SET="false"),
    )
    assert result.returncode == 1
    assert "::error title=Sleepless settings missing::Empty: GADS_URL, GADS_CLIENT_SECRET. " in result.stdout
    assert plan["steps"][0]["name"] == "Check the inputs and the settings"


# --- the APK of the release ----------------------------------------------------------------------


def find(plan, tmp_path, github, tag="qa-develop", asset=""):
    # The release is read again at once, instead of after 15 seconds.
    return run_step(plan, "Find the APK", tmp_path, github, TAG=tag, ASSET=asset, RELEASE_POLL_SECONDS="0")


def release_reads(github: GitHub, tag: str) -> int:
    return len(github.asked("GET", f"{BASE}releases/tags/{tag}"))


def test_the_only_apk_of_a_rolling_pre_release_is_found_with_the_code_its_tag_names(plan, tmp_path, github):
    github.tag("qa-develop", message="2.0.1-qa.57 (200000123)\n")
    ids = github.release("qa-develop", **{APK_NAME: apk_bytes(), "notes.txt": b"x"})
    result, outputs = find(plan, tmp_path, github)
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"asset-id": str(ids[APK_NAME]), "asset-name": APK_NAME, "commit": COMMIT, "tag-code": "200000123"}
    assert f"qa-develop is 3ff9b1a, whose tag names versionCode 200000123. The APK: {APK_NAME}" in result.stdout
    # The job token reads, and goes nowhere else.
    assert all(request[2].get("Authorization") == "Bearer job-token" for request in github.requests)


def test_a_release_tag_names_no_code(plan, tmp_path, github):
    # The version bump writes the tag as its own message; a lightweight tag has none.
    github.tag("v2.0.0", message="v2.0.0")
    github.tag("v2.0.1", commit=OTHER_COMMIT)
    for tag, commit in (("v2.0.0", COMMIT), ("v2.0.1", OTHER_COMMIT)):
        github.release(tag, **{f"HeySleep_release_{tag}.apk": apk_bytes()})
        result, outputs = find(plan, tmp_path, github, tag=tag)
        assert result.returncode == 0, result.stdout + result.stderr
        assert outputs["commit"] == commit and outputs["tag-code"] == ""


def test_several_apks_need_asset(plan, tmp_path, github):
    github.tag("qa-develop", message="2.0.1-qa.57 (200000123)")
    names = ["HeySleep_brand_2.0.1-qa.57_200000123.apk", "HeySleep_main_2.0.1-qa.57_200000123.apk"]
    ids = github.release("qa-develop", **{name: apk_bytes() for name in names})
    result, _ = find(plan, tmp_path, github)
    assert result.returncode == 1
    assert f"::error title=No APK to upload::The release qa-develop holds 2 APKs: {names[0]}, {names[1]}. Name the one to upload with asset" in result.stdout
    # Both are of the build the tag names: no replacing ends that, and the release is not read again.
    assert release_reads(github, "qa-develop") == 1
    result, outputs = find(plan, tmp_path, github, asset="HeySleep_main_*.apk")
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs["asset-name"] == names[1] and outputs["asset-id"] == str(ids[names[1]])
    result, _ = find(plan, tmp_path, github, asset="HeySleep_*.apk")
    assert result.returncode == 1 and "holds 2 APKs that match 'HeySleep_*.apk'" in result.stdout
    result, _ = find(plan, tmp_path, github, asset="Other_*.apk")
    assert result.returncode == 1
    assert f"No APK of the release qa-develop matches asset 'Other_*.apk'. Its APKs: {names[0]}, {names[1]}." in result.stdout
    # A pattern is a file name, compared as it is written.
    result, _ = find(plan, tmp_path, github, asset="heysleep_main_*.apk")
    assert result.returncode == 1


def test_an_apk_that_a_qa_build_is_publishing_is_not_chosen(plan, tmp_path, github):
    # android-qa.yml uploads a new APK of a taken name as <name>.run-<id>-<attempt>.apk first.
    github.tag("qa-develop", message="2.0.1-qa.57 (200000123)")
    ids = github.release("qa-develop", **{APK_NAME: apk_bytes(), APK_NAME[:-4] + ".run-99-1.apk": apk_bytes()})
    result, outputs = find(plan, tmp_path, github)
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs["asset-id"] == str(ids[APK_NAME])


def test_an_asset_that_is_still_uploading_is_not_chosen(plan, tmp_path, github):
    github.tag("qa-develop", message="2.0.1-qa.57 (200000123)")
    github.release("qa-develop", **{APK_NAME: apk_bytes()})
    github.releases["qa-develop"]["assets"][0]["state"] = "open"
    result, _ = find(plan, tmp_path, github)
    assert result.returncode == 1 and "The release qa-develop holds no APK. Its APKs: none." in result.stdout
    # It was waited for, as long as an APK that is going up is waited for.
    assert release_reads(github, "qa-develop") == 9


# The publish job of android-qa.yml replacing the APK of qa-develop: the tag is moved to the new build
# first, the new APK goes up next to the old one, and the old one is deleted last.
OLD_APK = "HeySleep_qa_2.0.1-qa.57_200000123.apk"
NEW_APK = "HeySleep_qa_2.0.1-qa.58_200000124.apk"
NEW_TAG = "2.0.1-qa.58 (200000124)"


def moment(github: GitHub, tag: str, *names: str, going_up: tuple = ()) -> dict:
    """The release as it is listed at one moment of a replacing: these of its assets, some still going up."""
    assets = [
        {**item, "state": "open" if item["name"] in going_up else "uploaded"}
        for item in github.releases[tag]["assets"] if item["name"] in names
    ]
    return {"tag_name": tag, "assets": assets}


@pytest.mark.parametrize("asset", ["", "HeySleep_qa_*.apk"])
@pytest.mark.parametrize(
    ("old", "new"),
    [
        # <App>_<kind>_<versionName>_<versionCode>.apk
        (OLD_APK, NEW_APK),
        # The version name alone, or the versionCode alone.
        ("HeySleep_qa_2.0.1-qa.57.apk", "HeySleep_qa_2.0.1-qa.58.apk"),
        ("HeySleep_qa_200000123.apk", "HeySleep_qa_200000124.apk"),
    ],
)
def test_while_a_qa_build_replaces_its_apk_the_new_one_is_chosen_by_its_name(plan, tmp_path, github, asset, old, new):
    # Both are uploaded and both have names of their own: the old one is about to be deleted.
    github.tag("qa-develop", message=NEW_TAG)
    ids = github.release("qa-develop", **{old: apk_bytes(code="200000123"), new: apk_bytes(code="200000124")})
    result, outputs = find(plan, tmp_path, github, asset=asset)
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs["asset-id"] == str(ids[new]) and outputs["asset-name"] == new and outputs["tag-code"] == "200000124"
    # No download of either to find out, and no waiting.
    assert release_reads(github, "qa-develop") == 1
    assert github.asked("GET", f"{BASE}releases/assets/") == []
    assert f"qa-develop also holds {old}, of the build before: the QA build that moved the tag to 2.0.1-qa.58 (200000124) is replacing it." in result.stdout


@pytest.mark.parametrize("asset", ["", "HeySleep_qa*.apk"])
@pytest.mark.parametrize("replacing", ["going up", "copy", "names that do not tell"])
def test_while_the_names_do_not_tell_the_release_is_read_again_until_the_replacing_is_done(plan, tmp_path, github, asset, replacing):
    github.tag("qa-develop", message=NEW_TAG)
    if replacing == "going up":
        # The new APK is still going up next to the old one, then both are there, then the old one goes.
        ids = github.release("qa-develop", **{OLD_APK: apk_bytes(code="200000123"), NEW_APK: apk_bytes(code="200000124")})
        reads = [
            moment(github, "qa-develop", OLD_APK, NEW_APK, going_up=(NEW_APK,)),
            moment(github, "qa-develop", OLD_APK, NEW_APK, going_up=(NEW_APK,)),
            moment(github, "qa-develop", OLD_APK, NEW_APK),
        ]
        chosen, after = ids[NEW_APK], 3
    elif replacing == "copy":
        # A build that names every APK alike: the new one goes up as a copy, then takes the name.
        staged = "HeySleep_qa.run-99-1.apk"
        ids = github.release("qa-develop", **{"HeySleep_qa.apk": apk_bytes(code="200000123"), staged: apk_bytes(code="200000124")})
        renamed = {"id": ids[staged], "name": "HeySleep_qa.apk", "size": 4136, "state": "uploaded"}
        reads = [
            moment(github, "qa-develop", "HeySleep_qa.apk", staged),
            moment(github, "qa-develop", staged),
            {"tag_name": "qa-develop", "assets": [renamed]},
        ]
        chosen, after = ids[staged], 3
    else:
        ids = github.release("qa-develop", **{"HeySleep_qa.apk": apk_bytes(code="200000123"), "HeySleep_qa_arm64.apk": apk_bytes(code="200000124")})
        reads = [
            moment(github, "qa-develop", "HeySleep_qa.apk", "HeySleep_qa_arm64.apk"),
            moment(github, "qa-develop", "HeySleep_qa_arm64.apk"),
        ]
        chosen, after = ids["HeySleep_qa_arm64.apk"], 2
    github.release_reads["qa-develop"] = reads
    result, outputs = find(plan, tmp_path, github, asset=asset)
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs["asset-id"] == str(chosen)
    assert release_reads(github, "qa-develop") == after
    assert "The release qa-develop is being replaced by 2.0.1-qa.58 (200000124), the build its tag names: reading it again in 0 seconds (1 of 8)." in result.stdout


def test_a_release_that_stays_in_between_fails_as_before(plan, tmp_path, github):
    # Two APKs whose names do not tell, for longer than a replacing takes: the publish job did not
    # finish, or the build makes two APKs and asset names none.
    github.tag("qa-develop", message=NEW_TAG)
    github.release("qa-develop", **{"HeySleep_qa.apk": apk_bytes(), "HeySleep_qa_arm64.apk": apk_bytes()})
    result, outputs = find(plan, tmp_path, github)
    assert result.returncode == 1
    assert "::error title=No APK to upload::The release qa-develop holds 2 APKs: HeySleep_qa.apk, HeySleep_qa_arm64.apk. Name the one to upload with asset" in result.stdout
    assert release_reads(github, "qa-develop") == 9
    assert result.stdout.count("reading it again in 0 seconds") == 8
    assert outputs == {}


def test_the_wait_is_two_minutes_and_the_calling_file_does_not_change_it(plan):
    find_step = step(plan, "Find the APK")
    assert "RELEASE_POLL_SECONDS" not in find_step["env"]
    script = find_step["run"]
    assert "READS = 9" in script and 'pause = float(os.environ.get("RELEASE_POLL_SECONDS") or 15)' in script
    assert plan["timeout-minutes"] == 15


# A rolling pre-release of android-qa.yml with a build per app (asset-pattern): SleepHub of one commit,
# SweetSleep of another and published last, each named in a line of the tag's message.
HUB_APK = "SleepHub_qa_2.0.1-qa.57_200000123.apk"
SWEET_APK = "SweetSleep_qa_2.0.1-qa.60_200000126.apk"
APPS_TAG = (
    "2.0.1-qa.60 (200000126)\n\n"
    f"SleepHub_*.apk 2.0.1-qa.57 (200000123) {OTHER_COMMIT}\n"
    f"SweetSleep_*.apk 2.0.1-qa.60 (200000126) {COMMIT}\n"
)


def test_each_apk_of_a_pre_release_with_a_build_per_app_is_of_its_apps_build(plan, tmp_path, github):
    github.tag("qa-release-1.x", message=APPS_TAG)
    ids = github.release("qa-release-1.x", **{HUB_APK: apk_bytes(), SWEET_APK: apk_bytes(code="200000126")})
    # The calling file names the app, as for a release with an APK per brand: its code and commit are its line's.
    result, outputs = find(plan, tmp_path, github, tag="qa-release-1.x", asset="SleepHub_*.apk")
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"asset-id": str(ids[HUB_APK]), "asset-name": HUB_APK, "commit": OTHER_COMMIT, "tag-code": "200000123"}
    result, outputs = find(plan, tmp_path, github, tag="qa-release-1.x", asset="SweetSleep_*.apk")
    assert outputs == {"asset-id": str(ids[SWEET_APK]), "asset-name": SWEET_APK, "commit": COMMIT, "tag-code": "200000126"}
    # Without asset both are of a build the tag names: no replacing ends that.
    result, outputs = find(plan, tmp_path, github, tag="qa-release-1.x")
    assert result.returncode == 1 and outputs == {}
    assert f"The release qa-release-1.x holds 2 APKs: {HUB_APK}, {SWEET_APK}. Name the one to upload with asset" in result.stdout
    assert release_reads(github, "qa-release-1.x") == 3
    # The lines are the ones release-version reads and android-qa.yml writes.
    sys.path.insert(0, str(ROOT / "release-version"))
    import release_version

    assert re.sub(r"\?P<[a-z]+>", "", release_version.ROLLING_BUILD.pattern) in step(plan, "Find the APK")["run"]


def test_while_one_app_replaces_its_apk_the_other_apps_apk_is_found_as_it_is(plan, tmp_path, github):
    # SweetSleep's next build moved the tag and is going up next to its build before.
    new_sweet = "SweetSleep_qa_2.0.1-qa.61_200000127.apk"
    github.tag("qa-release-1.x", message=APPS_TAG.replace("2.0.1-qa.60 (200000126)", "2.0.1-qa.61 (200000127)"))
    ids = github.release(
        "qa-release-1.x", **{HUB_APK: apk_bytes(), SWEET_APK: apk_bytes(code="200000126"), new_sweet: apk_bytes(code="200000127")},
    )
    result, outputs = find(plan, tmp_path, github, tag="qa-release-1.x", asset="SweetSleep_*.apk")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (outputs["asset-id"], outputs["tag-code"]) == (str(ids[new_sweet]), "200000127")
    assert f"also holds {SWEET_APK}, of the build before: the QA build that moved the tag to 2.0.1-qa.61 (200000127)" in result.stdout
    result, outputs = find(plan, tmp_path, github, tag="qa-release-1.x", asset="SleepHub_*.apk")
    assert (outputs["asset-id"], outputs["tag-code"], outputs["commit"]) == (str(ids[HUB_APK]), "200000123", OTHER_COMMIT)
    assert release_reads(github, "qa-release-1.x") == 2


def test_an_apk_that_no_line_of_the_tag_names_is_of_no_build(plan, tmp_path, github):
    # SleepHub's APK from before the calling file named the apps; SweetSleep's run wrote its line since.
    github.tag("qa-release-1.x", message=f"2.0.1-qa.60 (200000126)\n\nSweetSleep_*.apk 2.0.1-qa.60 (200000126) {COMMIT}\n")
    ids = github.release("qa-release-1.x", **{HUB_APK: apk_bytes(), SWEET_APK: apk_bytes(code="200000126")})
    result, outputs = find(plan, tmp_path, github, tag="qa-release-1.x", asset="SleepHub_*.apk")
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"asset-id": str(ids[HUB_APK]), "asset-name": HUB_APK, "commit": COMMIT, "tag-code": ""}
    assert f"qa-release-1.x names no build that {HUB_APK} is of: the commit of the tag stands for it." in result.stdout
    assert release_reads(github, "qa-release-1.x") == 1


@pytest.mark.parametrize("message", ["v2.0.0", None])
def test_several_apks_of_a_release_tag_are_an_error_at_once(plan, tmp_path, github, message):
    # A release tag names no build: nothing replaces its APKs, and several of them are a setting to make.
    github.tag("v2.0.0", message=message)
    names = ["HeySleep_brand_2.0.0_200050000.apk", "HeySleep_main_2.0.0_200050000.apk"]
    github.release("v2.0.0", **{name: apk_bytes() for name in names})
    result, outputs = find(plan, tmp_path, github, tag="v2.0.0")
    assert result.returncode == 1
    assert f"::error title=No APK to upload::The release v2.0.0 holds 2 APKs: {names[0]}, {names[1]}. Name the one to upload with asset" in result.stdout
    assert release_reads(github, "v2.0.0") == 1 and "reading it again" not in result.stdout
    assert outputs == {}


@pytest.mark.parametrize(
    ("setup", "said"),
    [
        ("no tag", "There is no tag qa-develop in acme/heysleep."),
        ("no release", "The tag qa-develop has no published GitHub Release, so there is no APK to upload."),
        ("no apk", "The release qa-develop holds no APK. Its APKs: none."),
        ("odd name", "HeySleep qa.apk of qa-develop has a name of other characters than letters, digits and . _ -."),
    ],
)
def test_no_apk_fails_the_plan(plan, tmp_path, github, setup, said):
    if setup != "no tag":
        github.tag("qa-develop", message="2.0.1-qa.57 (200000123)")
    if setup == "no apk":
        github.release("qa-develop", **{"mapping.txt": b"x"})
    if setup == "odd name":
        github.release("qa-develop", **{"HeySleep qa.apk": apk_bytes()})
    result, outputs = find(plan, tmp_path, github)
    assert result.returncode == 1
    assert f"::error title=No APK to upload::{said}" in result.stdout
    assert outputs == {}


def download(job, tmp_path, github, asset_id, name=APK_NAME, **env):
    return run_step(job, "Download the APK", tmp_path, github, TAG="qa-develop", ASSET_ID=str(asset_id), ASSET_NAME=name, **env)


def test_the_apk_is_downloaded_whole_with_its_digest(plan, tmp_path, github):
    content = apk_bytes()
    ids = github.release("qa-develop", **{APK_NAME: content})
    result, outputs = download(plan, tmp_path, github, ids[APK_NAME], EXPECTED_SIZE="", EXPECTED_SHA256="")
    assert result.returncode == 0, result.stdout + result.stderr
    path = tmp_path / "runner-temp" / "android-sleepless" / APK_NAME
    assert path.read_bytes() == content
    assert outputs == {"found": "true", "path": str(path), "size": str(len(content)), "sha256": hashlib.sha256(content).hexdigest()}
    (request,) = github.asked("GET", f"{BASE}releases/assets/")
    assert request[2]["Accept"] == "application/octet-stream"


def test_the_token_is_not_sent_to_where_the_file_is(plan, tmp_path, github):
    # GitHub answers the asset with the address of the file, on another host.
    github.redirect = True
    content = apk_bytes()
    ids = github.release("qa-develop", **{APK_NAME: content})
    result, outputs = download(plan, tmp_path, github, ids[APK_NAME], EXPECTED_SIZE="", EXPECTED_SHA256="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs["sha256"] == hashlib.sha256(content).hexdigest()
    (asked,) = github.asked("GET", f"{BASE}releases/assets/")
    (redirected,) = github.asked("GET", "/download/")
    assert asked[2]["Authorization"] == "Bearer job-token"
    assert "Authorization" not in redirected[2]


def test_an_apk_that_is_gone_is_a_warning_and_no_upload(plan, upload, tmp_path, github):
    # The publishing of a newer build deleted it after the plan chose it.
    for job in (plan, upload):
        result, outputs = download(job, tmp_path, github, 12345, EXPECTED_SIZE="", EXPECTED_SHA256="")
        assert result.returncode == 0, result.stdout + result.stderr
        assert outputs == {"found": "false"}
        assert f"::warning title=The APK is gone::{APK_NAME} of qa-develop is not there any more" in result.stdout


def test_the_upload_job_takes_only_the_bytes_the_plan_read(upload, tmp_path, github):
    content = apk_bytes()
    ids = github.release("qa-develop", **{APK_NAME: content})
    digest = hashlib.sha256(content).hexdigest()
    result, outputs = download(upload, tmp_path, github, ids[APK_NAME], EXPECTED_SIZE=str(len(content)), EXPECTED_SHA256=digest)
    assert result.returncode == 0 and outputs["found"] == "true"
    result, outputs = download(upload, tmp_path, github, ids[APK_NAME], EXPECTED_SIZE=str(len(content)), EXPECTED_SHA256="0" * 64)
    assert result.returncode == 1
    assert "::error title=Not the APK of the plan::" in result.stderr + result.stdout
    assert "found" not in outputs


@pytest.mark.parametrize(("asset_id", "name"), [("", APK_NAME), ("12", ""), ("12", "../x.apk"), ("x", APK_NAME)])
def test_a_plan_whose_outputs_were_dropped_downloads_nothing(upload, tmp_path, github, asset_id, name):
    result, _ = download(upload, tmp_path, github, asset_id, name=name, EXPECTED_SIZE="1", EXPECTED_SHA256="0" * 64)
    assert result.returncode == 1
    assert "::error title=The plan is incomplete::" in result.stdout + result.stderr
    assert github.requests == []


def test_the_apk_is_read_with_the_highest_aapt2(plan, tmp_path):
    apk = tmp_path / APK_NAME
    apk.write_bytes(apk_bytes(code="200000123", name="2.0.1-qa.57"))
    result, outputs = run_step(plan, "Read the APK", tmp_path, APK=str(apk), ANDROID_HOME=str(sdk(tmp_path)))
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"package": PACKAGE, "version-code": "200000123", "version-name": "2.0.1-qa.57"}
    assert f"{APK_NAME}: package {PACKAGE}, versionName 2.0.1-qa.57, versionCode 200000123 (36.1.0)." in result.stdout


@pytest.mark.parametrize(
    ("fields", "said"),
    [
        ({"package": "heysleep"}, "has no package name aapt2 can read: 'heysleep'."),
        ({"code": "0"}, "has no versionCode aapt2 can read: '0'."),
        ({"name": ""}, "has a versionName the dashboard cannot show: ''."),
    ],
)
def test_an_apk_aapt2_cannot_read_fails_the_plan(plan, tmp_path, fields, said):
    apk = tmp_path / APK_NAME
    apk.write_bytes(apk_bytes(**fields))
    result, outputs = run_step(plan, "Read the APK", tmp_path, APK=str(apk), ANDROID_HOME=str(sdk(tmp_path)))
    assert result.returncode == 1 and said in result.stdout + result.stderr
    assert outputs == {}


def test_no_aapt2_fails_the_plan(plan, tmp_path):
    apk = tmp_path / APK_NAME
    apk.write_bytes(b"not an apk")
    result, _ = run_step(plan, "Read the APK", tmp_path, APK=str(apk), ANDROID_HOME=str(tmp_path / "none"))
    assert result.returncode == 1 and "aapt2 was not found" in result.stdout + result.stderr
    result, _ = run_step(plan, "Read the APK", tmp_path, APK=str(apk), ANDROID_HOME=str(sdk(tmp_path)))
    assert result.returncode == 1 and "cannot read" in result.stdout + result.stderr


# --- which builds go up --------------------------------------------------------------------------


def compare(job, tmp_path, github, code="200000123", tag_code="", package=PACKAGE):
    return run_step(job, "Compare with the uploads", tmp_path, github, TAG="qa-develop", PACKAGE=package, VERSION_CODE=code, TAG_CODE=tag_code)


@pytest.mark.parametrize("job_name", ["plan", "upload"])
def test_the_first_upload_of_a_package_goes_up(entry, tmp_path, github, job_name):
    result, outputs = compare(entry["jobs"][job_name], tmp_path, github)
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"upload": "true", "last-code": ""}
    assert f"{PACKAGE} 200000123 (qa-develop) is to be uploaded: the first upload of {PACKAGE}." in result.stdout
    (request,) = github.asked("GET", f"{BASE}git/matching-refs/")
    assert request[1] == f"{BASE}git/matching-refs/sleepless/{PACKAGE}/"


def test_a_higher_code_than_the_last_upload_goes_up(plan, tmp_path, github):
    for code in ("200000100", "200000122"):
        github.uploaded(code)
    result, outputs = compare(plan, tmp_path, github)
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"upload": "true", "last-code": "200000122"}
    assert "is to be uploaded: above 200000122, the last upload of com.asleep.heysleep." in result.stdout


def test_a_code_that_was_uploaded_is_not_uploaded_again(plan, tmp_path, github):
    github.uploaded("200000123")
    result, outputs = compare(plan, tmp_path, github)
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"upload": "false", "last-code": "200000123"}
    assert (
        f"::notice title=Uploaded already::{PACKAGE} 200000123 (qa-develop) was uploaded to Sleepless before "
        f"(refs/sleepless/{PACKAGE}/200000123). Nothing is uploaded: Sleepless does not switch to a build it has."
    ) in result.stdout


def test_a_code_below_the_last_upload_is_not_uploaded(plan, tmp_path, github):
    # A QA build of the formula after a release, or a build that waited while a higher one went up.
    github.uploaded("200050000")
    result, outputs = compare(plan, tmp_path, github)
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"upload": "false", "last-code": "200050000"}
    assert (
        f"::notice title=Not newer than the last upload::{PACKAGE} 200000123 (qa-develop) is below 200050000, the last "
        f"versionCode of {PACKAGE} uploaded to Sleepless. Nothing is uploaded: Sleepless switches to a higher build only."
    ) in result.stdout


def test_the_uploads_of_another_package_do_not_count(plan, tmp_path, github):
    # Another brand is another app in Sleepless.
    github.uploaded("200000999", package=PACKAGE + ".brand")
    github.uploaded("300000000", package="com.asleep.heysleepx")
    result, outputs = compare(plan, tmp_path, github)
    assert outputs == {"upload": "true", "last-code": ""}
    result, outputs = compare(plan, tmp_path, github, code="200000999", package=PACKAGE + ".brand")
    assert outputs["upload"] == "false"


def test_every_page_of_the_uploads_is_read(plan, tmp_path, github):
    github.page_size = 2
    for code in ("200000001", "200000002", "200000003", "200000200", "200000004"):
        github.uploaded(code)
    result, outputs = compare(plan, tmp_path, github)
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"upload": "false", "last-code": "200000200"}
    assert len(github.asked("GET", f"{BASE}git/matching-refs/")) == 3


def test_a_rolling_tag_that_names_another_build_is_a_pre_release_being_replaced(plan, tmp_path, github):
    result, outputs = compare(plan, tmp_path, github, code="200000123", tag_code="200000124")
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"upload": "false", "last-code": ""}
    assert "::warning title=The pre-release is being replaced::qa-develop names the build 200000124, and the APK read is 200000123" in result.stdout
    assert github.requests == []


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_uploads_that_cannot_be_listed_fail(plan, tmp_path, github, status):
    github.refusals[("GET", "git/matching-refs/")] = [status]
    result, outputs = compare(plan, tmp_path, github)
    assert result.returncode == 1
    assert f"Cannot list the uploads of {PACKAGE} (refs/sleepless/{PACKAGE}/*): HTTP {status}." in result.stdout + result.stderr
    assert outputs == {}


@pytest.mark.parametrize(("package", "code"), [("", "200000123"), (PACKAGE, ""), ("com.x/../y", "1"), (PACKAGE, "12a")])
def test_a_comparison_without_its_values_fails(upload, tmp_path, github, package, code):
    result, _ = compare(upload, tmp_path, github, code=code, package=package)
    assert result.returncode == 1 and "::error title=The plan is incomplete::" in result.stdout + result.stderr
    assert github.requests == []


# --- the runner ----------------------------------------------------------------------------------


def where(plan, tmp_path, **overrides):
    env = {
        "TAG": "qa-develop", "LABELS": LABELS, "FOUND": "true", "UPLOAD": "true", "LAST_CODE": "200000100",
        "SELF_HOSTED": "true", "ASSET_NAME": APK_NAME, "COMMIT": COMMIT, "PACKAGE": PACKAGE,
        "VERSION_NAME": "2.0.1-qa.57", "VERSION_CODE": "200000123", **overrides,
    }
    result, outputs = run_step(plan, "Decide where the upload runs", tmp_path, **env)
    assert result.returncode == 0, result.stdout + result.stderr
    return result, outputs, (tmp_path / "summary").read_text()


def test_an_online_runner_runs_the_upload(plan, tmp_path):
    result, outputs, summary = where(plan, tmp_path)
    assert outputs == {"run": "true"}
    assert "::" not in result.stdout
    assert f"| {APK_NAME} | {PACKAGE} | 2.0.1-qa.57 (200000123) | `3ff9b1a` | 200000100 |" in summary
    assert f"To be uploaded by the job `upload`, on a runner with the labels `{LABELS}`." in summary


def test_no_online_runner_is_a_warning_and_no_upload(plan, tmp_path):
    # pick-runner picked its fallback; the upload job is skipped instead, and the run does not fail.
    result, outputs, summary = where(plan, tmp_path, SELF_HOSTED="false")
    assert outputs == {"run": "false"}
    assert (
        f"::warning title=Sleepless upload skipped::No runner with the labels {LABELS} is online, and only such a runner "
        "reaches GADS_URL. 2.0.1-qa.57 (200000123) of qa-develop is not uploaded"
    ) in result.stdout
    assert f"Not uploaded: no runner with the labels `{LABELS}` is online." in summary


@pytest.mark.parametrize(
    ("overrides", "said"),
    [
        ({"UPLOAD": "false", "LAST_CODE": "200000123", "SELF_HOSTED": ""}, "Not uploaded: Sleepless has 200000123 of com.asleep.heysleep, and this build is not higher."),
        ({"UPLOAD": "false", "LAST_CODE": "", "SELF_HOSTED": ""}, "Not uploaded: the pre-release is being replaced by a newer build."),
        ({"FOUND": "false", "UPLOAD": "", "LAST_CODE": "", "SELF_HOSTED": "", "PACKAGE": "", "VERSION_NAME": "", "VERSION_CODE": ""}, "Not uploaded: the APK was replaced while this run read it."),
    ],
)
def test_nothing_to_upload_needs_no_runner(plan, tmp_path, overrides, said):
    result, outputs, summary = where(plan, tmp_path, **overrides)
    assert outputs == {"run": "false"}
    assert "::warning" not in result.stdout
    assert said in summary


# --- recording an upload -------------------------------------------------------------------------


def record(upload, tmp_path, github, code="200000123"):
    return run_step(
        upload, "Record the upload", tmp_path, github, TAG="qa-develop", APP="HeySleep", PACKAGE=PACKAGE,
        VERSION_CODE=code, VERSION_NAME="2.0.1-qa.57", ASSET_NAME=APK_NAME, COMMIT=COMMIT,
    )


def test_an_upload_is_recorded_as_a_ref_on_a_tag_object_of_the_commit(upload, tmp_path, github):
    result, _ = record(upload, tmp_path, github)
    assert result.returncode == 0, result.stdout + result.stderr
    ref = f"refs/sleepless/{PACKAGE}/200000123"
    assert f"Recorded the upload as {ref}." in result.stdout
    target = github.refs[ref]
    assert target["type"] == "tag"
    item = github.objects[target["sha"]]
    assert item["tag"] == "sleepless" and item["type"] == "commit" and item["object"]["sha"] == COMMIT
    assert item["message"] == f"Uploaded to Sleepless: HeySleep 2.0.1-qa.57 (200000123), {APK_NAME} of qa-develop, by run 4711 of {REPOSITORY}."
    assert item["tagger"]["name"] == "github-actions[bot]" and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", item["tagger"]["date"])
    assert "Recorded as `refs/sleepless/com.asleep.heysleep/200000123`." in (tmp_path / "summary").read_text()
    # And the next comparison finds it.
    result, outputs = compare(upload, tmp_path, github)
    assert outputs == {"upload": "false", "last-code": "200000123"}


def test_a_ref_that_another_run_created_is_a_notice(upload, tmp_path, github):
    github.uploaded("200000123")
    result, _ = record(upload, tmp_path, github)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"::notice title=Recorded already::refs/sleepless/{PACKAGE}/200000123 exists: another run uploaded 200000123 at the same time" in result.stdout


@pytest.mark.parametrize(
    ("where_refused", "status", "hint"),
    [
        ("git/tags", 403, "The job token needs contents: write"),
        ("git/refs", 403, "The job token needs contents: write"),
        ("git/refs", 422, "A ruleset of the repository refuses it"),
    ],
)
def test_an_upload_that_cannot_be_recorded_fails_the_job(upload, tmp_path, github, where_refused, status, hint):
    github.refusals[("POST", where_refused)] = [status]
    result, _ = record(upload, tmp_path, github)
    assert result.returncode == 1
    assert f"::error title=The upload is not recorded::{PACKAGE} 200000123 was uploaded to Sleepless, and refs/sleepless/{PACKAGE}/200000123 could not be written (HTTP {status}" in result.stdout
    assert hint in result.stdout
    assert "The next run uploads the build again" in result.stdout


def test_a_server_error_while_recording_is_asked_again(upload, tmp_path, github):
    github.refusals[("POST", "git/refs")] = [502]
    result, _ = record(upload, tmp_path, github)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"refs/sleepless/{PACKAGE}/200000123" in github.refs
    assert len(github.asked("POST", f"{BASE}git/refs")) == 2


# --- the whole way -------------------------------------------------------------------------------


def run_action(tmp_path: Path, github: GitHub, file: str, description: str, ref: str, sha: str, max_time: str):
    """The composite step of sleepless-upload, with the inputs the upload step passes."""
    (action_step,) = yaml.safe_load(ACTION.read_text())["runs"]["steps"]
    output = tmp_path / "action-output"
    output.write_text("")
    env = {
        "FILE": file, "DESC": description, "REF": ref, "SHA": sha, "MAX_TIME": max_time,
        "GADS_URL": f"{github.api}/gads", "GADS_CLIENT_ID": "ci", "GADS_CLIENT_SECRET": "secret", "GADS_TENANT": "asleep",
        "RUNNER_TEMP": str(tmp_path / "runner-temp"), "GITHUB_OUTPUT": str(output),
    }
    return run_script(action_step["run"], env, tmp_path)


def test_a_qa_build_goes_up_once(entry, plan, upload, tmp_path, github):
    content = apk_bytes()
    github.tag("qa-develop", message="2.0.1-qa.57 (200000123)")
    github.release("qa-develop", **{APK_NAME: content})
    github.uploaded("200000100")
    home = sdk(tmp_path)

    def plan_run():
        outputs = {}
        result, found = find(plan, tmp_path, github)
        assert result.returncode == 0, result.stdout + result.stderr
        result, downloaded = download(plan, tmp_path, github, found["asset-id"], EXPECTED_SIZE="", EXPECTED_SHA256="")
        assert result.returncode == 0, result.stdout + result.stderr
        result, read = run_step(plan, "Read the APK", tmp_path, APK=downloaded["path"], ANDROID_HOME=str(home))
        assert result.returncode == 0, result.stdout + result.stderr
        result, compared = compare(plan, tmp_path, github, code=read["version-code"], tag_code=found["tag-code"], package=read["package"])
        assert result.returncode == 0, result.stdout + result.stderr
        outputs.update(found, **downloaded, **read, **compared)
        return outputs

    planned = plan_run()
    assert planned["upload"] == "true" and planned["version-code"] == "200000123"
    # The upload job, on the runner of the office network.
    (tmp_path / "runner-temp").mkdir(exist_ok=True)
    result, downloaded = download(
        upload, tmp_path, github, planned["asset-id"], EXPECTED_SIZE=planned["size"], EXPECTED_SHA256=planned["sha256"],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    result, compared = compare(upload, tmp_path, github, code=planned["version-code"], package=planned["package"])
    assert compared["upload"] == "true"
    # format('{0} {1} ({2})', inputs.app, version-name, version-code)
    description = f"HeySleep {planned['version-name']} ({planned['version-code']})"
    result = run_action(tmp_path, github, downloaded["path"], description, "qa-develop", planned["commit"], "900")
    assert result.returncode == 0, result.stdout + result.stderr
    (apps,) = github.asked("POST", "/gads/apps")
    assert content in apps[3]
    assert "HeySleep 2.0.1-qa.57 (200000123) \u00b7 qa-develop 3ff9b1a".encode() in apps[3]
    result, _ = record(upload, tmp_path, github, code=planned["version-code"])
    assert result.returncode == 0, result.stdout + result.stderr
    result = run_script(step(upload, "Delete the APK")["run"], {"RUNNER_TEMP": str(tmp_path / "runner-temp")}, tmp_path)
    assert result.returncode == 0 and not (tmp_path / "runner-temp" / "android-sleepless").exists()

    # The next day, the same build: the plan stops at the comparison, and nothing runs on the runner.
    again = plan_run()
    assert again["upload"] == "false" and again["last-code"] == "200000123"
    assert len(github.asked("POST", "/gads/apps")) == 1


# --- the calling file and the README -------------------------------------------------------------


def test_the_example_calls_the_workflow_with_what_it_declares(entry):
    example = yaml.safe_load(EXAMPLE.read_text())
    (job,) = example["jobs"].values()
    assert job["uses"] == f"asleep-ai/actions/.github/workflows/{ENTRY}@android-ci/v1"
    assert set(job["with"]) <= set(trigger(entry)["inputs"])
    # By name, not `secrets: inherit`: two are needed.
    assert job["secrets"] == {name: f"${{{{ secrets.{name} }}}}" for name in trigger(entry)["secrets"]}
    events = example[True]
    # The rolling QA build once a day at 19:00 KST; every release and preview once it is published;
    # anything else by hand.
    assert events["schedule"] == [{"cron": "0 10 * * *"}]
    # workflow_run matches the name: of the calling release workflow, which is Release Build in
    # sleephub-android and heysleep-android-v2; the README says to write the caller's own exactly.
    assert events["workflow_run"] == {"workflows": ["Release Build"], "types": ["completed"]}
    section = readme_section()
    assert "| `workflow_run` of `Release Build`, when it succeeded |" in section
    assert "**Write the `name:` of the calling repository's release workflow there exactly**" in section
    assert set(events["workflow_dispatch"]["inputs"]) == {"tag", "asset"}
    assert job["with"]["tag"] == (
        "${{ github.event_name == 'schedule' && 'qa-develop' || github.event_name == 'workflow_run' "
        "&& github.event.workflow_run.head_branch || inputs.tag }}"
    )
    assert job["if"] == "${{ github.event_name != 'workflow_run' || github.event.workflow_run.conclusion == 'success' }}"
    assert "concurrency" not in example


def readme_section() -> str:
    return README.read_text().split("\n## android-sleepless.yml\n", 1)[1].split("\n## ", 1)[0]


def test_the_readme_shows_the_calling_file_of_the_example():
    example = yaml.safe_load(EXAMPLE.read_text())
    usage = yaml.safe_load(readme_section().split("```yaml", 1)[1].split("```", 1)[0])
    for part in (True, "permissions", "jobs"):
        assert usage[part] == example[part], part


def test_the_readme_lists_every_input_with_its_default_and_the_secrets(entry):
    section = readme_section()
    inputs = trigger(entry)["inputs"]
    rows = re.findall(r"^\| `([a-z0-9-]+)` \| (.*?) \| ", section.split("### Inputs", 1)[1].split("###", 1)[0], re.MULTILINE)
    assert [name for name, _ in rows] == list(inputs), "the table follows the order of the workflow file"
    for name, shown in rows:
        default = inputs[name].get("default")
        assert shown == ("required" if default is None else "`''`" if default == "" else f"`{default}`"), name
    for name in [*trigger(entry)["secrets"], "GADS_URL", "GADS_CLIENT_ID", "GADS_TENANT"]:
        assert f"`{name}`" in section, name


def test_the_readme_says_how_far_apart_uploads_have_to_be():
    section = readme_section()
    assert "**Uploads have to be about four hours apart for a result report.**" in section
    for fact in ("every 30 seconds", "about 3 hours 40 minutes", "`0 10 * * *`", "19:00 KST", "refs/sleepless/<package>/<versionCode>"):
        assert fact in section, fact
