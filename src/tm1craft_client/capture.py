"""Capture a TM1 instance's model objects into an arc-service bundle dict.

``capture_bundle`` is the client-side replacement of the tm1craft Arc
plugin's browser dump (``craftModelDumpRun`` in ``apps/arc/plugin/plugin.js``):
strictly READ-only, it walks one connected ``TM1Service`` and returns the
bundle ``{"instance": ..., "entities": {"processes": [...], "cubes": [...],
"dimensions": [...], "chores": [...]}}`` the tm1craft arc-service job API
(``POST /model-dump-job/{job_id}/entities`` chunks, ``dump_job.stage_chunk``)
consumes — the same entity shapes the plugin POSTs and the ``Tm1JsonLoader``
parses.

The wire contract is the plugin's, mirrored exactly:

- **Processes** ride ``tm1.processes.get_all()`` (control objects included —
  no filtering anywhere in this module). TM1py fetches the TM1-encrypted
  ODBC ``password`` inside the ``DataSource`` ComplexType per its ``$select``,
  so :func:`strip_passwords` removes that key (any spelling, any depth) from
  *every* entity before the bundle is returned — the #521 doctrine, matching
  the service-side backstop in ``tm1craft_arc_service.dump_model``.
- **Cubes** ride the composed OData query through ``tm1.connection`` —
  TM1py has no fetch with this shape: ``CubeService.get_all`` expands only
  ``Dimensions($select=Name)`` (no Rules text, no builder ``Attributes``, no
  view inventory) and ``views.get_all`` pays the full native-view
  Rows/Titles/Columns payload, exactly the bloat the plugin's
  ``Views($select=Name,MDX)`` select avoids (docs/tm1-odata-recipes.md #594).
- **Dimensions** are metadata-only, the hard rule: ``tm1.dimensions.get()``
  is never called — it expands all Elements/Edges and would drag whole
  element trees over the wire. The recipes-doc narrow query (hierarchies
  with Cardinality/ElementCount, element-attribute definitions, subsets with
  their MDX Expression, ``@odata.count`` annotations) runs paged
  (``$top``/``$skip``, one retry per page) through the connection.
- **Chores** ride ``tm1.chores.get_all()`` (schedule fields and Tasks
  included), mapped to the plugin's chore shape.

Decode repair: TM1py decodes response bodies itself (``requests`` swaps
stray code-page bytes for U+FFFD), so after a TM1py fetch the entities are
scanned for the replacement character; damaged kinds are re-fetched through
the connection where :func:`decode_strict_utf8` repairs the stray byte with
latin-1 at exactly the surrogate-escaped positions.

Every kind's fetched count is cross-checked against the server's
``/<Collection>/$count`` (fetched with ``Accept: text/plain`` — plain JSON
accept headers make TM1 11.8 serve the count as an OData payload) and a
mismatch aborts the capture: an incomplete fetch must never read as a dump.

``tm1`` is duck-typed in tests — the slices used are
``tm1.processes.get_all()``, ``tm1.chores.get_all()`` (TM1py objects with
``body_as_dict``/``body``, or plain dicts) and
``tm1.connection.GET(url, headers=...)`` returning a response with
``.content`` bytes.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

# The bundle keys, mirroring the arc-service ``dump_model.ENTITY_KINDS``.
ENTITY_KINDS = ("processes", "cubes", "dimensions", "chores")

# The plugin's process fetch: all four TI tab bodies plus Parameters,
# Variables and the DataSource ComplexType (docs/tm1-odata-recipes.md).
PROCESSES_QUERY = (
    "/Processes?$select=Name,PrologProcedure,MetadataProcedure,"
    "DataProcedure,EpilogProcedure,Parameters,Variables,DataSource"
)

# The plugin's cube fetch (#594): MDX rides the Views select — carried
# verbatim when the server returns it, name-only when it doesn't.
CUBES_QUERY = "/Cubes?$select=Name,Rules,Attributes&$expand=Dimensions($select=Name),Views($select=Name,MDX)"

# The plugin's metadata-only dimension fetch: hierarchies with size facts
# counted server-side, attribute definitions, dynamic subset MDX — never an
# element payload. Spaces are ``%20`` and ``}`` is ``%7D`` (TM1 rejects
# form-encoded spaces with OData error 278).
DIMENSIONS_QUERY = (
    "/Dimensions?$select=Name,Attributes,AllLeavesHierarchyName"
    "&$expand=DefaultHierarchy($select=Name,UniqueName),%20"
    "Hierarchies($select=Name,UniqueName,Cardinality,Visible;"
    "%20$expand=Elements/$count,Edges/$count,DefaultMember($select=Name),"
    "ElementAttributes($select=Name,Type),"
    "Subsets($select=Name,Expression;$filter=not%20startswith(Name,'%7D')))"
)

# The plugin's chore fetch: task order with process names; the schedule
# fields ride along unselected.
CHORES_QUERY = "/Chores?$expand=Tasks($expand=Process($select=Name))"

# Bundle kind → OData collection name, for the ``$count`` cross-check.
COUNT_COLLECTIONS = {
    "processes": "Processes",
    "cubes": "Cubes",
    "dimensions": "Dimensions",
    "chores": "Chores",
}

# Default ``$top`` for the paged dimension fetch (the recipes-doc paging
# fallback: per entity kind, not per object).
DIMENSION_PAGE_SIZE = 250

# Entity fetches: the metadata annotations are the bulk of a naive
# response; ``metadata=none`` keeps the wire lean (recipes doc).
ACCEPT_JSON = "application/json;odata.metadata=none"

# ``$count`` fetches: TM1 11.8 needs a plain-text accept header.
ACCEPT_TEXT = "text/plain"

# What ``requests`` leaves where a body byte failed its UTF-8 decode —
# the marker that a TM1py fetch silently damaged the text.
REPLACEMENT_CHAR = "\ufffd"

# ``on_progress(kind, message)`` — called once per entity kind once it is
# captured (``("dimensions", "1637 fetched")``).
ProgressCallback = Callable[[str, str], None]


class CaptureError(Exception):
    """A capture aborted: shape failure, count mismatch, or a dead fetch."""


def capture_bundle(
    tm1: Any,
    instance_name: str,
    *,
    on_progress: ProgressCallback | None = None,
    dimension_page_size: int = DIMENSION_PAGE_SIZE,
) -> dict[str, Any]:
    """Capture the four model-object kinds from a connected TM1 instance.

    ``tm1`` is a connected TM1py ``TM1Service`` (duck-typed). Strictly
    READ-only: every call in here is a GET or a service read. Returns the
    bundle dict the arc-service job API consumes; every entity is
    password-stripped depth-safe before it enters the bundle.

    ``dimension_page_size`` bounds one page of the paged dimension fetch.
    """
    if dimension_page_size < 1:
        raise ValueError("dimension_page_size must be at least 1")
    progress = on_progress or (lambda kind, message: None)
    connection = tm1.connection
    entities = {
        "processes": _capture_processes(tm1, connection, progress),
        "cubes": _capture_cubes(connection, progress),
        "dimensions": _capture_dimensions(connection, progress, dimension_page_size),
        "chores": _capture_chores(tm1, connection, progress),
    }
    _cross_check_counts(connection, entities)
    return {
        "instance": instance_name,
        "entities": {kind: [strip_passwords(entity) for entity in entities[kind]] for kind in ENTITY_KINDS},
    }


# --- per-kind capture ---------------------------------------------------------


def _capture_processes(tm1: Any, connection: Any, progress: ProgressCallback) -> list[dict[str, Any]]:
    """All processes (control ones included) with full bodies + DataSource.

    Rides ``tm1.processes.get_all()`` per the capture spec. TM1py decodes
    the response body itself, so a stray code-page byte inside a process
    body arrives as U+FFFD; when that marker shows up the kind is
    re-fetched through the connection, where
    :func:`decode_strict_utf8` repairs the byte at its own position.
    """
    entities = [_entity_dict(process) for process in tm1.processes.get_all()]
    if _has_decode_damage(entities):
        entities = _fetch_odata_entities(connection, "processes", PROCESSES_QUERY)
    progress("processes", f"{len(entities)} fetched")
    return entities


def _capture_cubes(connection: Any, progress: ProgressCallback) -> list[dict[str, Any]]:
    """All cubes via the composed OData query: Rules text, builder
    Attributes, dimension names, and the view inventory (names + MDX when
    the server projects it).

    The connection (not a TM1py service) carries this kind because no
    TM1py fetch matches the plugin's shape — see the module docstring.
    """
    cubes = _fetch_odata_entities(connection, "cubes", CUBES_QUERY)
    _validate_cube_shape(cubes)
    entities = [map_cube_entity(cube) for cube in cubes]
    progress("cubes", f"{len(entities)} fetched")
    return entities


def _capture_dimensions(connection: Any, progress: ProgressCallback, page_size: int) -> list[dict[str, Any]]:
    """All dimensions, metadata-only, paged ``$top``/``$skip`` through the
    connection — one retry per page.

    ``tm1.dimensions.get()`` is never called: it expands every Elements and
    Edges navigation, the exact payload this fetch exists to avoid.
    """
    dimensions = _fetch_odata_entities(connection, "dimensions", DIMENSIONS_QUERY, page_size=page_size)
    _validate_dimension_shape(dimensions)
    entities = [map_dimension_entity(dimension) for dimension in dimensions]
    progress("dimensions", f"{len(entities)} fetched")
    return entities


def _capture_chores(tm1: Any, connection: Any, progress: ProgressCallback) -> list[dict[str, Any]]:
    """All chores (control ones included) with their Tasks, mapped to the
    plugin's chore shape.

    Rides ``tm1.chores.get_all()`` per the capture spec; the decode-damage
    fallback re-fetches through the connection exactly like processes.
    """
    chores = [_entity_dict(chore) for chore in tm1.chores.get_all()]
    if _has_decode_damage(chores):
        chores = _fetch_odata_entities(connection, "chores", CHORES_QUERY)
    entities = [map_chore_entity(chore) for chore in chores]
    progress("chores", f"{len(entities)} fetched")
    return entities


# --- connection plumbing ------------------------------------------------------


def _fetch_odata_entities(
    connection: Any,
    kind: str,
    query: str,
    *,
    page_size: int | None = None,
) -> list[dict[str, Any]]:
    """Run one composed OData query through the connection; entity dicts back.

    Without ``page_size`` a single GET serves the kind (the plugin's flow);
    with one, the query pages ``$top``/``$skip`` until a short page ends
    the loop. Every page gets one retry (the recipes-doc truncation
    pitfall: a cut mid-UTF-8 stream is a transient, not a shape).
    """
    entities: list[dict[str, Any]] = []
    offset = 0
    while True:
        url = query if page_size is None else f"{query}&$top={page_size}&$skip={offset}"
        payload = _get_odata_json(connection, url, kind)
        if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
            raise CaptureError(f"{kind} fetch did not return an OData value array: {url}")
        page = payload["value"]
        entities.extend(page)
        if page_size is None or len(page) < page_size:
            return entities
        offset += page_size


def _get_odata_json(connection: Any, url: str, kind: str) -> Any:
    """GET one OData URL, retry once, and decode the body ourselves.

    The bytes never ride ``requests``' lossy text decode:
    :func:`decode_strict_utf8` repairs stray code-page bytes with latin-1
    at exactly their positions, so a process body keeps its ``¦`` instead
    of a U+FFFD hole.
    """
    raw = _request_with_retry(connection, url, ACCEPT_JSON, kind)
    try:
        return json.loads(decode_strict_utf8(raw))
    except json.JSONDecodeError as problem:
        raise CaptureError(f"{kind} response is not valid JSON (truncated stream?): {url}") from problem


def _request_with_retry(connection: Any, url: str, accept: str, kind: str) -> bytes:
    """One GET through the connection, retried once; raw bytes back."""
    last_error: Exception | None = None
    for _ in range(2):
        try:
            return connection.GET(url, headers={"Accept": accept}).content
        except Exception as problem:  # noqa: BLE001 — duck-typed transports raise anything
            last_error = problem
    raise CaptureError(f"{kind} fetch failed after one retry: {url} ({last_error})") from last_error


def _fetch_collection_count(connection: Any, collection: str) -> int:
    """The server's own entity count for one collection.

    ``/<Collection>/$count`` with ``Accept: text/plain`` — the plain-text
    header is what TM1 11.8 needs to answer with a bare number.
    """
    raw = _request_with_retry(connection, f"/{collection}/$count", ACCEPT_TEXT, collection)
    try:
        return int(decode_strict_utf8(raw).strip())
    except ValueError as problem:
        raise CaptureError(f"/{collection}/$count did not return a number: {raw!r}") from problem


def _cross_check_counts(connection: Any, entities: dict[str, list]) -> None:
    """Every kind's fetched count must match the server's ``$count``.

    A mismatch means the fetch lost entities silently (the >~2.8 MB
    truncation pitfall on direct REST) — abort with a clear error instead
    of dumping a partial model.
    """
    for kind, collection in COUNT_COLLECTIONS.items():
        fetched = len(entities[kind])
        reported = _fetch_collection_count(connection, collection)
        if reported != fetched:
            raise CaptureError(
                f"{kind}: fetched {fetched} entities but /{collection}/$count reports {reported} — "
                "the fetch is incomplete; aborting the capture"
            )


# --- decode + strip helpers ---------------------------------------------------


def decode_strict_utf8(raw: bytes) -> str:
    """Decode a TM1 response body: strict UTF-8, stray code-page bytes repaired.

    A body that fails strict UTF-8 (a single stray 0xA6 inside a process
    body, the classic server code-page leftover) is repaired with latin-1
    at exactly the surrogate-escaped positions — the valid UTF-8 around it,
    including multi-byte characters, passes through untouched.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        escaped = raw.decode("utf-8", errors="surrogateescape")
        return "".join(chr(ord(char) - 0xDC00) if 0xDC80 <= ord(char) <= 0xDCFF else char for char in escaped)


def strip_passwords(value: Any) -> Any:
    """Return ``value`` with every ``password`` key removed, at any depth.

    Case-insensitive on the key (the plugin's ``craftStripPasswords`` +
    the service backstop's contract): objects rebuild without it, arrays
    map, scalars pass through. ``userName``, DSN names, ``query`` and file
    paths stay — only the credential goes.
    """
    if isinstance(value, dict):
        return {
            key: strip_passwords(item)
            for key, item in value.items()
            if not (isinstance(key, str) and key.lower() == "password")
        }
    if isinstance(value, list):
        return [strip_passwords(item) for item in value]
    return value


def _entity_dict(entity: Any) -> dict[str, Any]:
    """The REST-entity dict behind a TM1py object (or the dict itself).

    TM1py objects expose the entity as ``body_as_dict`` (rebuilt per
    access) or ``body`` (JSON text); plain dicts pass through copied.
    Nothing under the returned dict is mutated downstream — the strip and
    the mappers rebuild.
    """
    if isinstance(entity, dict):
        return dict(entity)
    body_as_dict = getattr(entity, "body_as_dict", None)
    if isinstance(body_as_dict, dict):
        return body_as_dict
    body = getattr(entity, "body", None)
    if isinstance(body, str):
        return json.loads(body)
    raise CaptureError(f"entity fetch returned an unsupported type: {type(entity).__name__}")


def _has_decode_damage(entities: list[dict[str, Any]]) -> bool:
    """Whether any fetched entity carries the U+FFFD decode-replacement marker."""
    serialized = json.dumps(entities, ensure_ascii=False, default=str)
    return REPLACEMENT_CHAR in serialized


# --- shape validation (the plugin's all-or-nothing readers, #420) --------------


def _validate_cube_shape(cubes: list[Any]) -> None:
    """Every cube must carry its Dimensions and Views arrays — anything
    less (a truncated stream, a reshaped payload) is a failed fetch, never
    a partial dump."""
    if not all(
        isinstance(cube, dict) and isinstance(cube.get("Dimensions"), list) and isinstance(cube.get("Views"), list)
        for cube in cubes
    ):
        raise CaptureError("cubes fetch did not return the expanded cube shape (Dimensions + Views arrays)")


def _validate_dimension_shape(dimensions: list[Any]) -> None:
    """Every dimension must carry a Hierarchies array, and every hierarchy
    the ElementAttributes and Subsets arrays."""
    for dimension in dimensions:
        if not isinstance(dimension, dict) or not isinstance(dimension.get("Hierarchies"), list):
            raise CaptureError("dimensions fetch did not return the hierarchy shape (Hierarchies array per dimension)")
        for hierarchy in dimension["Hierarchies"]:
            if (
                not isinstance(hierarchy, dict)
                or not isinstance(hierarchy.get("ElementAttributes"), list)
                or not isinstance(hierarchy.get("Subsets"), list)
            ):
                raise CaptureError(
                    "dimensions fetch did not return the hierarchy shape "
                    "(ElementAttributes + Subsets arrays per hierarchy)"
                )


# --- mappers (the plugin's craftModelDump* shapes, verbatim) -------------------


def map_cube_entity(cube: dict[str, Any]) -> dict[str, Any]:
    """One fetched cube entity → the bundle shape: Rules inline, the
    Attributes dictionary, dimensions flattened to names, and each view as
    ``{Name}`` plus ``MDX`` verbatim when the entry carries a string one —
    absent otherwise, never guessed (#594)."""
    return {
        "Name": cube.get("Name"),
        "Rules": cube["Rules"] if isinstance(cube.get("Rules"), str) else "",
        "Attributes": cube["Attributes"] if _is_plain_dict(cube.get("Attributes")) else {},
        "Dimensions": _name_list(cube.get("Dimensions")),
        "Views": [
            entry
            for entry in (_mapped_view(view) for view in cube.get("Views", []) if isinstance(view, dict))
            if entry is not None
        ],
    }


def _mapped_view(view: dict[str, Any]) -> dict[str, Any] | None:
    """One view entry: ``{Name}``, plus ``MDX`` only when it is a string."""
    if not isinstance(view.get("Name"), str) or view["Name"] == "":
        return None
    entry = {"Name": view["Name"]}
    if isinstance(view.get("MDX"), str):
        entry["MDX"] = view["MDX"]
    return entry


def map_dimension_entity(dimension: dict[str, Any]) -> dict[str, Any]:
    """One fetched dimension entity → the bundle shape: dimension-level
    Attributes, ``AllLeavesHierarchyName`` and ``DefaultHierarchy`` when
    carried, and the mapped hierarchies."""
    mapped: dict[str, Any] = {
        "Name": dimension.get("Name"),
        "Attributes": dimension["Attributes"] if _is_plain_dict(dimension.get("Attributes")) else {},
        "Hierarchies": [
            map_hierarchy_entity(hierarchy)
            for hierarchy in dimension.get("Hierarchies", [])
            if isinstance(hierarchy, dict)
        ],
    }
    if isinstance(dimension.get("AllLeavesHierarchyName"), str):
        mapped["AllLeavesHierarchyName"] = dimension["AllLeavesHierarchyName"]
    default = dimension.get("DefaultHierarchy")
    if isinstance(default, dict) and isinstance(default.get("Name"), str):
        mapped["DefaultHierarchy"] = {
            "Name": default["Name"],
            "UniqueName": default["UniqueName"] if isinstance(default.get("UniqueName"), str) else "",
        }
    return mapped


def map_hierarchy_entity(hierarchy: dict[str, Any]) -> dict[str, Any]:
    """One fetched hierarchy → the bundle entry: size facts from the
    ``@odata.count`` annotations, the default member, typed attribute
    definitions, and the subsets with their MDX ``Expression`` when
    dynamic."""
    mapped: dict[str, Any] = {
        "Name": hierarchy.get("Name") if isinstance(hierarchy.get("Name"), str) else "",
        "UniqueName": hierarchy.get("UniqueName") if isinstance(hierarchy.get("UniqueName"), str) else "",
        "ElementAttributes": [
            {"Name": definition["Name"], "Type": definition["Type"] if isinstance(definition.get("Type"), str) else ""}
            for definition in hierarchy.get("ElementAttributes", [])
            if isinstance(definition, dict) and isinstance(definition.get("Name"), str) and definition["Name"] != ""
        ],
        "Subsets": [
            entry
            for entry in (_mapped_subset(subset) for subset in hierarchy.get("Subsets", []) if isinstance(subset, dict))
            if entry is not None
        ],
    }
    if isinstance(hierarchy.get("Cardinality"), (int, float)) and not isinstance(hierarchy["Cardinality"], bool):
        mapped["Cardinality"] = hierarchy["Cardinality"]
    if isinstance(hierarchy.get("Visible"), bool):
        mapped["Visible"] = hierarchy["Visible"]
    if isinstance(hierarchy.get("Elements@odata.count"), int):
        mapped["ElementCount"] = hierarchy["Elements@odata.count"]
    if isinstance(hierarchy.get("Edges@odata.count"), int):
        mapped["EdgeCount"] = hierarchy["Edges@odata.count"]
    default_member = hierarchy.get("DefaultMember")
    if isinstance(default_member, dict) and isinstance(default_member.get("Name"), str):
        mapped["DefaultMember"] = {"Name": default_member["Name"]}
    return mapped


def _mapped_subset(subset: dict[str, Any]) -> dict[str, Any] | None:
    """One subset entry: ``{Name}``, plus ``Expression`` only when it is a
    string (the dynamic-MDX case)."""
    if not isinstance(subset.get("Name"), str) or subset["Name"] == "":
        return None
    entry = {"Name": subset["Name"]}
    if isinstance(subset.get("Expression"), str):
        entry["Expression"] = subset["Expression"]
    return entry


def map_chore_entity(chore: dict[str, Any]) -> dict[str, Any]:
    """One fetched chore → the plugin's chore shape: the schedule fields
    ride along verbatim — keys the fetch didn't return drop out instead of
    being guessed (#307) — and each task keeps its process name plus its
    Parameters; tasks without a usable process name are dropped."""
    mapped: dict[str, Any] = {"Name": chore.get("Name")}
    for field in ("Active", "StartTime", "Frequency", "ExecutionMode", "DSTSensitive"):
        if field in chore:
            mapped[field] = chore[field]
    mapped["Tasks"] = [
        entry
        for entry in (map_chore_task(task) for task in chore.get("Tasks") or [] if isinstance(task, dict))
        if entry is not None
    ]
    return mapped


def map_chore_task(task: dict[str, Any]) -> dict[str, Any] | None:
    """One chore task: ``{Process: {Name}}`` plus ``Parameters`` when the
    fetch carried them; ``None`` when the process name is unusable."""
    process = task.get("Process")
    if not isinstance(process, dict) or not isinstance(process.get("Name"), str):
        return None
    entry = {"Process": {"Name": process["Name"]}}
    if task.get("Parameters") is not None:
        entry["Parameters"] = task["Parameters"]
    return entry


def _name_list(entries: Any) -> list[str]:
    """Expanded ``{Name}`` entries flattened to a filtered name list."""
    if not isinstance(entries, list):
        return []
    return [
        entry["Name"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("Name"), str) and entry["Name"]
    ]


def _is_plain_dict(value: Any) -> bool:
    """A real attribute dictionary (the loader's 'no attributes' shape is
    ``{}``, anything unexpected degrades to it)."""
    return isinstance(value, dict) and not isinstance(value, list)
