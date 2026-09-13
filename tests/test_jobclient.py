"""Tests for the job client, against a stdlib stub of the service's ``/model-dump-job`` routes.

The stub (``StubDumpService``) scripts the wire contract of the tm1craft
arc service's ``dump_job.py`` — start/probe/chunk/build/status — so the
client's funnel, attach-on-409, poll, and auth behaviors are exercised
over real HTTP without the FastAPI service.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from tm1craft_client import jobclient
from tm1craft_client.jobclient import JobResult, ServiceError, upload_bundle

DONE_RECORD: dict[str, Any] = {
    "nodes": 12,
    "edges": 30,
    "unchanged": 2,
    "reparsed": 10,
    "removed": 0,
    "reparse": {"Process": 6, "Cube": 4},
    "analysis": {"ok": True, "modules": 3, "orphans": 0},
    "path": "/srv/tm1craft/dumps/dev.db",
}
# The build-report fields a done job record carries (dump_job.py:27-32).

BUNDLE: dict[str, list[dict[str, Any]]] = {
    "processes": [{"Name": "sys.b Load Ledger", "PrologProcedure": "log('load');"}],
    "cubes": [{"Name": "Ledger", "Dimensions": ["Year"], "Views": []}],
    "dimensions": [{"Name": "Year", "Hierarchies": [{"Name": "Year", "ElementAttributes": [], "Subsets": []}]}],
    "chores": [{"Name": "nightly", "Active": True, "Tasks": []}],
}


class StubDumpService:
    """Scripted fake of the arc service's ``/model-dump-job`` routes — the dump_job.py wire contract.

    Knobs the tests set before uploading: ``start_refusal_status`` makes the
    start POST answer with that status, ``build_failure_status`` fails the
    build POST, ``live_job`` is a pre-existing queued/running job the probe
    finds, and ``poll_states`` is the per-GET script the poll walks after a
    build POST (the last state repeats once the script runs out). Every
    request's Authorization header, every start body, and every chunk body
    is recorded for the assertions.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.chunks: list[dict[str, Any]] = []
        self.authorization_headers: list[str | None] = []
        self.start_bodies: list[dict[str, Any]] = []
        self.probe_queries: list[str] = []
        self.job_counter = 0
        self.start_refusal_status: int | None = None
        self.build_failure_status: int | None = None
        self.live_job: dict[str, Any] | None = None
        self.poll_states: list[dict[str, Any]] = []

    def log_authorization(self, header: str | None) -> None:
        """Record one request's Authorization header."""
        with self.lock:
            self.authorization_headers.append(header)

    def handle_start(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """The start POST: create a staging job, or refuse with the scripted status."""
        with self.lock:
            self.start_bodies.append(body)
            if self.start_refusal_status is not None:
                return self.start_refusal_status, {
                    "detail": f"a dump build is already running for instance '{body.get('instance')}'"
                }
            self.job_counter += 1
            job_id = f"job-{self.job_counter}"
            self.jobs[job_id] = {
                "job_id": job_id,
                "instance": body.get("instance"),
                "arc_origin": body.get("arcOrigin"),
                "status": "staging",
                "stage": None,
                "staged": {},
                "error": None,
                "poll_index": 0,
            }
            return 202, {"job_id": job_id, "instance": body.get("instance"), "status": "staging"}

    def handle_chunk(self, job_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """The entities POST: append the kind-tagged chunk to the staging job's bundle."""
        with self.lock:
            record = self.jobs.get(job_id)
            if record is None:
                return 404, {"detail": f"unknown dump job '{job_id}'"}
            if record["status"] != "staging":
                return 409, {"detail": f"dump job '{job_id}' is {record['status']} — no longer accepting chunks"}
            kind, entities = body["kind"], body["entities"]
            self.chunks.append({"job_id": job_id, "kind": kind, "entities": entities})
            record["staged"][kind] = record["staged"].get(kind, 0) + len(entities)
            return 200, {
                "job_id": job_id,
                "kind": kind,
                "received": len(entities),
                "staged": dict(record["staged"]),
                "status": record["status"],
            }

    def handle_build(self, job_id: str) -> tuple[int, dict[str, Any]]:
        """The build POST: queue the job, or fail with the scripted status."""
        with self.lock:
            record = self.jobs.get(job_id)
            if record is None:
                return 404, {"detail": f"unknown dump job '{job_id}'"}
            if self.build_failure_status is not None:
                return self.build_failure_status, {"detail": "boom"}
            record["status"] = "queued"
            return 202, {"job_id": job_id, "instance": record["instance"], "status": "queued"}

    def handle_status(self, job_id: str) -> tuple[int, dict[str, Any]]:
        """The poll GET: serve the next scripted state (the last one repeats)."""
        with self.lock:
            record = self.jobs.get(job_id)
            if record is None:
                return 404, {"detail": f"unknown dump job '{job_id}'"}
            served = {key: value for key, value in record.items() if key != "poll_index"}
            if record["status"] == "staging" or not self.poll_states:
                return 200, served
            index = min(record["poll_index"], len(self.poll_states) - 1)
            record["poll_index"] = record.get("poll_index", 0) + 1
            served.update(self.poll_states[index])
            return 200, served

    def handle_probe(self, instance: str) -> tuple[int, dict[str, Any]]:
        """The active-job probe GET: the instance's queued/running job, or 404."""
        with self.lock:
            self.probe_queries.append(instance)
            live = self.live_job
            if live is not None and live.get("instance") == instance and live.get("status") in ("queued", "running"):
                return 200, dict(live)
            return 404, {"detail": f"no active dump job for instance '{instance}'"}


def build_stub_handler(stub: StubDumpService) -> type[BaseHTTPRequestHandler]:
    """Build the request-handler class bound to one stub's script."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_string: str, *args: Any) -> None:
            """Silence the per-request stderr line."""

        def read_json_body(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw) if raw else None

        def send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            parts = parsed.path.strip("/").split("/")
            stub.log_authorization(self.headers.get("Authorization"))
            if parts == ["model-dump-job"]:
                values = parse_qs(parsed.query).get("instance")
                status, payload = stub.handle_probe(values[0] if values else "")
            elif len(parts) == 2 and parts[0] == "model-dump-job":
                status, payload = stub.handle_status(parts[1])
            else:
                status, payload = 404, {"detail": f"no route for {self.path}"}
            self.send_json(status, payload)

        def do_POST(self) -> None:
            parts = urlparse(self.path).path.strip("/").split("/")
            stub.log_authorization(self.headers.get("Authorization"))
            body = self.read_json_body()
            if parts == ["model-dump-job"] and isinstance(body, dict):
                status, payload = stub.handle_start(body)
            elif len(parts) == 3 and parts[0] == "model-dump-job" and parts[2] == "entities" and isinstance(body, dict):
                status, payload = stub.handle_chunk(parts[1], body)
            elif len(parts) == 3 and parts[0] == "model-dump-job" and parts[2] == "build":
                status, payload = stub.handle_build(parts[1])
            else:
                status, payload = 404, {"detail": f"no route for {self.path}"}
            self.send_json(status, payload)

    return Handler


@pytest.fixture
def stub_service() -> Iterator[tuple[StubDumpService, str]]:
    """Boot the scripted stub on an ephemeral localhost port behind a daemon thread."""
    stub = StubDumpService()
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_stub_handler(stub))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield stub, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_upload_bundle_drives_the_full_funnel(stub_service, monkeypatch):
    """Happy path: start, kind-tagged chunks intact, build, poll — the stage sequence renders."""
    stub, service_url = stub_service
    monkeypatch.setattr(jobclient, "POLL_INTERVAL_SECONDS", 0.0)
    stub.poll_states = [
        {"status": "running", "stage": "staging-write"},
        {"status": "running", "stage": "graph-build"},
        {"status": "running", "stage": "facts-run"},
        {"status": "done", "stage": None, **DONE_RECORD},
    ]
    stages_seen: list[str] = []
    result = upload_bundle(
        service_url,
        BUNDLE,
        instance="dev",
        arc_origin="http://localhost:3000",
        on_stage=stages_seen.append,
    )
    assert isinstance(result, JobResult)
    assert (result.job_id, result.status, result.attached) == ("job-1", "done", False)
    assert result.error is None
    assert result.path == DONE_RECORD["path"]
    assert (result.nodes, result.edges, result.unchanged, result.reparsed, result.removed) == (12, 30, 2, 10, 0)
    assert result.reparse == {"Process": 6, "Cube": 4}
    assert result.analysis == {"ok": True, "modules": 3, "orphans": 0}
    assert stages_seen == ["staging-write", "graph-build", "facts-run"]
    assert stub.start_bodies == [{"instance": "dev", "arcOrigin": "http://localhost:3000"}]
    assert [(chunk["kind"], chunk["entities"]) for chunk in stub.chunks] == [
        (kind, BUNDLE[kind]) for kind in jobclient.ENTITY_KINDS
    ]
    assert stub.jobs["job-1"]["staged"] == {"processes": 1, "cubes": 1, "dimensions": 1, "chores": 1}


def test_upload_bundle_sends_only_kinds_present_in_the_bundle(stub_service, monkeypatch):
    """A kind absent from the bundle is never posted; a present-but-empty kind is sent empty."""
    stub, service_url = stub_service
    monkeypatch.setattr(jobclient, "POLL_INTERVAL_SECONDS", 0.0)
    stub.poll_states = [
        {"status": "running", "stage": "staging-write"},
        {"status": "done", "stage": None, **DONE_RECORD},
    ]
    partial = {"processes": BUNDLE["processes"], "chores": []}
    result = upload_bundle(service_url, partial, instance="dev")
    assert result.status == "done"
    assert [chunk["kind"] for chunk in stub.chunks] == ["processes", "chores"]
    assert stub.chunks[0]["entities"] == BUNDLE["processes"]
    assert stub.chunks[1]["entities"] == []


def test_upload_bundle_rejects_bundle_kinds_outside_the_wire_contract():
    """An unknown kind would silently drop data — refuse before any request leaves."""
    with pytest.raises(ValueError, match="hierarchies"):
        upload_bundle("http://127.0.0.1:9", {"hierarchies": []}, instance="dev")


def test_upload_bundle_attaches_to_the_live_job_on_409(stub_service, monkeypatch):
    """The start POST's 409 probe finds the running job: its id is reused, nothing is re-uploaded."""
    stub, service_url = stub_service
    monkeypatch.setattr(jobclient, "POLL_INTERVAL_SECONDS", 0.0)
    stub.start_refusal_status = 409
    stub.live_job = {"job_id": "job-live", "instance": "dev", "status": "running", "stage": "staging-write"}
    stub.jobs["job-live"] = {**stub.live_job, "error": None, "poll_index": 0}
    stub.poll_states = [
        {"status": "running", "stage": "graph-build"},
        {"status": "done", "stage": None, **DONE_RECORD},
    ]
    stages_seen: list[str] = []
    result = upload_bundle(service_url, BUNDLE, instance="dev", on_stage=stages_seen.append)
    assert (result.job_id, result.status, result.attached) == ("job-live", "done", True)
    assert result.path == DONE_RECORD["path"]
    assert stub.probe_queries == ["dev"]
    assert stub.chunks == []
    assert stub.start_bodies  # the refused start did go out
    assert stages_seen == ["graph-build"]


def test_upload_bundle_refused_409_without_a_live_job_raises(stub_service):
    """A 409 whose probe reads 404 — the refusal no longer resolves — raises a clear error."""
    stub, service_url = stub_service
    stub.start_refusal_status = 409
    with pytest.raises(ServiceError) as raised:
        upload_bundle(service_url, BUNDLE, instance="dev")
    assert raised.value.status_code == 404
    assert "no active dump job for instance 'dev'" in str(raised.value)
    assert stub.chunks == []


def test_upload_bundle_returns_the_failed_build_state(stub_service, monkeypatch):
    """A job that lands ``failed`` is a terminal JobResult carrying the server's error."""
    stub, service_url = stub_service
    monkeypatch.setattr(jobclient, "POLL_INTERVAL_SECONDS", 0.0)
    stub.poll_states = [
        {"status": "running", "stage": "graph-build"},
        {"status": "failed", "stage": None, "error": "dump store write failed: disk full"},
    ]
    stages_seen: list[str] = []
    result = upload_bundle(service_url, BUNDLE, instance="dev", on_stage=stages_seen.append)
    assert (result.job_id, result.status, result.attached) == ("job-1", "failed", False)
    assert result.error == "dump store write failed: disk full"
    assert result.path is None
    assert stages_seen == ["graph-build"]


def test_upload_bundle_500_mid_stream_raises_with_route_and_body_excerpt(stub_service, monkeypatch):
    """A mid-stream 500 raises with the route, the status, and the response body excerpt."""
    stub, service_url = stub_service
    monkeypatch.setattr(jobclient, "POLL_INTERVAL_SECONDS", 0.0)
    stub.build_failure_status = 500
    with pytest.raises(ServiceError) as raised:
        upload_bundle(service_url, BUNDLE, instance="dev")
    assert raised.value.status_code == 500
    assert "POST" in str(raised.value)
    assert "/model-dump-job/job-1/build" in str(raised.value)
    assert "boom" in str(raised.value)


def test_upload_bundle_sends_the_bearer_header_when_licensed(stub_service, monkeypatch):
    """A license key rides every request as Authorization: Bearer."""
    stub, service_url = stub_service
    monkeypatch.setattr(jobclient, "POLL_INTERVAL_SECONDS", 0.0)
    stub.poll_states = [{"status": "done", "stage": None, **DONE_RECORD}]
    result = upload_bundle(service_url, BUNDLE, instance="dev", license_key="lic-123")
    assert result.status == "done"
    assert stub.authorization_headers
    assert set(stub.authorization_headers) == {"Bearer lic-123"}


def test_upload_bundle_sends_no_authorization_header_without_a_license(stub_service, monkeypatch):
    """No license key, no Authorization header — the back-compat wire shape."""
    stub, service_url = stub_service
    monkeypatch.setattr(jobclient, "POLL_INTERVAL_SECONDS", 0.0)
    stub.poll_states = [{"status": "done", "stage": None, **DONE_RECORD}]
    upload_bundle(service_url, BUNDLE, instance="dev")
    assert stub.authorization_headers
    assert set(stub.authorization_headers) == {None}


def test_upload_bundle_times_out_when_the_job_never_turns_terminal(stub_service, monkeypatch):
    """The overall poll budget raises a TimeoutError naming the job and its last status."""
    stub, service_url = stub_service
    monkeypatch.setattr(jobclient, "POLL_INTERVAL_SECONDS", 0.01)
    stub.poll_states = [{"status": "running", "stage": "staging-write"}]
    with pytest.raises(TimeoutError) as raised:
        upload_bundle(service_url, BUNDLE, instance="dev", poll_timeout_seconds=0.2)
    assert "job-1" in str(raised.value)
    assert "running" in str(raised.value)
