"""Client for the tm1craft arc service's chunked model-dump job funnel.

The upload half of the capture client: hand a captured entity bundle to a
tm1craft arc service, push it per kind in chunks, start the build, and poll
the job to a terminal state. The client does not know or care what the
service does with the entities — staging, graph building, and provenance
are the service's business.

The wire contract mirrors the service's ``dump_job.py``
(``apps/arc/service/src/tm1craft_arc_service/dump_job.py`` in the tm1craft
repo) and the Arc plugin's ``craftModelDumpRun`` (``plugin.js:5171``):

- ``POST /model-dump-job`` with ``{"instance": …, "arcOrigin": …?}`` → 202
  with the job id (dump_job.py:235). A 409 means a build is already
  queued/running for the instance; the client answers it with the plugin's
  attach-on-409 probe instead of failing.
- ``GET /model-dump-job?instance=…`` → the instance's active job record,
  404 when none (dump_job.py:290).
- ``POST /model-dump-job/{job_id}/entities`` with ``{"kind", "entities"}``
  → one kind-tagged chunk appended to the job's bundle (dump_job.py:310).
- ``POST /model-dump-job/{job_id}/build`` → 202; the build runs on a
  service thread (dump_job.py:340).
- ``GET /model-dump-job/{job_id}`` → the poll target: ``status`` is
  ``staging | queued | running | done | failed`` (dump_job.py:87-95) and,
  while the build runs, ``stage`` names the stage in progress —
  ``staging-write | graph-build | facts-run`` (dump_job.py:97).

When a license key is given, every request carries
``Authorization: Bearer <key>`` (the plugin's ``craftDumpHeaders``,
``plugin.js:5289``); otherwise no Authorization header is sent. Every call
times out; any non-2xx reply raises :class:`ServiceError` carrying the
route, the status code, and a body excerpt.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import requests

ENTITY_KINDS = ("processes", "cubes", "dimensions", "chores")
# The wire's entity kinds, one chunk POST per kind (dump_job.py:314).

TERMINAL_STATUSES = ("done", "failed")
# The job statuses the poll stops on (dump_job.py:94-95).

POLL_INTERVAL_SECONDS = 0.5
# Pause between poll GETs — the plugin's cadence (plugin.js:5473).

REQUEST_TIMEOUT_SECONDS = 30.0
# Per-call HTTP timeout; every request the client sends carries one.

DEFAULT_POLL_TIMEOUT_SECONDS = 1800.0
# Overall poll budget — dump builds finish in minutes (dump_job.py:101).

BODY_EXCERPT_CHARS = 300
# How much of an error reply's body a ServiceError carries.


@dataclass(frozen=True)
class JobResult:
    """A terminal dump-build job: the id, the state, and the server-reported paths/counts.

    ``status`` is ``done`` or ``failed``; a failed job carries the server's
    popup-renderable ``error`` and is returned, not raised — reaching a
    terminal state is the funnel working.
    """

    job_id: str
    instance: str
    status: str
    attached: bool
    """True when the client attached to an already-running job on the start POST's 409."""
    error: str | None = None
    path: str | None = None
    nodes: int | None = None
    edges: int | None = None
    unchanged: int | None = None
    reparsed: int | None = None
    removed: int | None = None
    reparse: dict[str, int] | None = None
    analysis: dict[str, Any] | None = None


class ServiceError(RuntimeError):
    """A non-2xx service reply: carries the route, the status, and a body excerpt."""

    def __init__(self, method: str, url: str, status_code: int, body_excerpt: str) -> None:
        """Record the failed call and render a one-line message from it."""
        self.method = method
        self.url = url
        self.status_code = status_code
        self.body_excerpt = body_excerpt
        super().__init__(f"{method} {url} returned HTTP {status_code}: {body_excerpt}")


def upload_bundle(
    service_url: str,
    bundle: Mapping[str, list[dict[str, Any]]],
    *,
    instance: str,
    arc_origin: str | None = None,
    license_key: str | None = None,
    poll_timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    on_stage: Callable[[str], None] | None = None,
) -> JobResult:
    """Upload ``bundle`` to the tm1craft arc service and drive the dump job to a terminal state.

    The bundle maps entity kind to that kind's TM1 REST entity dicts. Kinds
    present in the bundle go up as kind-tagged chunks (one POST per kind,
    wire order); kinds absent from the bundle are skipped — the service
    treats a chunk that never arrived as empty (dump_job.py:489). Returns
    the terminal :class:`JobResult`; a failed build is a result, a transport
    or refusal failure raises.

    ``on_stage`` renders build progress: it is called with each new
    non-empty ``stage`` value the poll observes, in order
    (``staging-write``, ``graph-build``, ``facts-run`` on a fresh build).
    """
    unknown_kinds = sorted(set(bundle) - set(ENTITY_KINDS))
    if unknown_kinds:
        raise ValueError(f"bundle carries kinds outside the wire contract: {', '.join(unknown_kinds)}")
    base_url = service_url.rstrip("/")
    headers = {"Authorization": f"Bearer {license_key}"} if license_key else {}
    session = requests.Session()
    try:
        job_id, attached = start_dump_job(session, base_url, instance, arc_origin, headers)
        if not attached:
            for kind in ENTITY_KINDS:
                if kind in bundle:
                    stage_entities(session, base_url, job_id, kind, bundle[kind], headers)
            start_build(session, base_url, job_id, headers)
        record = poll_until_terminal(
            session,
            base_url,
            job_id,
            headers,
            poll_timeout_seconds=poll_timeout_seconds,
            on_stage=on_stage,
        )
        return shape_job_result(record, attached=attached)
    finally:
        session.close()


def start_dump_job(
    session: requests.Session,
    base_url: str,
    instance: str,
    arc_origin: str | None,
    headers: dict[str, str],
) -> tuple[str, bool]:
    """Start a dump-build job (dump_job.py:235); return ``(job_id, attached)``.

    On the start POST's 409 — a build already queued/running for the
    instance — attach to that job instead: the plugin's attach-on-409 probe
    (``plugin.js:5392``), which finds the live job and rides it to the
    report rather than dead-ending on the refusal.
    """
    payload: dict[str, Any] = {"instance": instance}
    if arc_origin is not None:
        payload["arcOrigin"] = arc_origin
    url = f"{base_url}/model-dump-job"
    try:
        reply = send_json(session, "POST", url, json_body=payload, headers=headers)
    except ServiceError as refusal:
        if refusal.status_code != 409:
            raise
        return attach_active_job(session, base_url, instance, headers), True
    job_id = reply.get("job_id") if isinstance(reply, dict) else None
    if not job_id:
        raise ValueError(f"service accepted the job start but returned no job id: {reply!r}")
    return str(job_id), False


def attach_active_job(
    session: requests.Session,
    base_url: str,
    instance: str,
    headers: dict[str, str],
) -> str:
    """Probe the instance's active job (dump_job.py:290) and return its id.

    The probe reads 404 when nothing is running — a start refusal whose
    answer just went terminal — and that refusal is re-raised with the
    context, mirroring the plugin's rethrow (``plugin.js:5400``).
    """
    url = f"{base_url}/model-dump-job"
    try:
        record = send_json(session, "GET", url, params={"instance": instance}, headers=headers)
    except ServiceError as probe_miss:
        if probe_miss.status_code == 404:
            raise ServiceError(
                "GET",
                url,
                404,
                f"no active dump job for instance '{instance}' — "
                "the start POST's 409 refusal no longer resolves to a live job",
            ) from probe_miss
        raise
    job_id = record.get("job_id") if isinstance(record, dict) else None
    if not job_id:
        raise ValueError(f"active-job probe returned a record without a job id: {record!r}")
    return str(job_id)


def stage_entities(
    session: requests.Session,
    base_url: str,
    job_id: str,
    kind: str,
    entities: list[dict[str, Any]],
    headers: dict[str, str],
) -> None:
    """Append one kind-tagged chunk to the job's bundle (dump_job.py:310)."""
    send_json(
        session,
        "POST",
        f"{base_url}/model-dump-job/{job_id}/entities",
        json_body={"kind": kind, "entities": entities},
        headers=headers,
    )


def start_build(
    session: requests.Session,
    base_url: str,
    job_id: str,
    headers: dict[str, str],
) -> None:
    """Start the build on a service thread; 202, the connection is never held (dump_job.py:340)."""
    send_json(session, "POST", f"{base_url}/model-dump-job/{job_id}/build", headers=headers)


def poll_until_terminal(
    session: requests.Session,
    base_url: str,
    job_id: str,
    headers: dict[str, str],
    *,
    poll_timeout_seconds: float,
    on_stage: Callable[[str], None] | None,
) -> dict[str, Any]:
    """GET the job record until ``status`` is terminal (dump_job.py:356); return the final record.

    Each new non-empty ``stage`` value fires ``on_stage`` once. Raises
    :class:`TimeoutError` when the job stays non-terminal past
    ``poll_timeout_seconds``.
    """
    url = f"{base_url}/model-dump-job/{job_id}"
    deadline = time.monotonic() + poll_timeout_seconds
    reported_stage: str | None = None
    while True:
        record = send_json(session, "GET", url, headers=headers)
        if not isinstance(record, dict) or "status" not in record:
            raise ValueError(f"poll reply is not a job record: {record!r}")
        stage = record.get("stage")
        if on_stage is not None and isinstance(stage, str) and stage != reported_stage:
            reported_stage = stage
            on_stage(stage)
        if record["status"] in TERMINAL_STATUSES:
            return record
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"dump job {job_id} did not reach a terminal state within "
                f"{poll_timeout_seconds}s (last status: {record['status']!r})"
            )
        time.sleep(POLL_INTERVAL_SECONDS)


def shape_job_result(record: dict[str, Any], *, attached: bool) -> JobResult:
    """Shape a terminal job record into a :class:`JobResult` (the plugin's report shape, ``plugin.js:5441``)."""
    return JobResult(
        job_id=str(record.get("job_id", "")),
        instance=str(record.get("instance", "")),
        status=str(record.get("status", "")),
        attached=attached,
        error=record.get("error"),
        path=record.get("path"),
        nodes=record.get("nodes"),
        edges=record.get("edges"),
        unchanged=record.get("unchanged"),
        reparsed=record.get("reparsed"),
        removed=record.get("removed"),
        reparse=record.get("reparse"),
        analysis=record.get("analysis"),
    )


def send_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    """Send one JSON request with a timeout; raise :class:`ServiceError` on any non-2xx, else return parsed JSON."""
    response = session.request(
        method,
        url,
        json=json_body,
        params=params,
        headers=headers or {},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if not 200 <= response.status_code < 300:
        excerpt = response.text.strip()[:BODY_EXCERPT_CHARS] or "<empty body>"
        raise ServiceError(method, response.url, response.status_code, excerpt)
    if not response.content:
        return {}
    return response.json()
