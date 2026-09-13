"""The capture module's contract tests: bundle shapes, password stripping,
paged metadata-only dimension fetch, the $count cross-check, decode repair,
and structural parity with the tm1_json fixtures.

TM1py is duck-typed: fake services/objects and a fake connection (stdlib
only) stand in for ``TM1Service`` — see ``capture``'s module docstring for
the exact slices the module consumes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tm1craft_client.capture import (
    ENTITY_KINDS,
    CaptureError,
    capture_bundle,
    decode_strict_utf8,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """One tm1_json entity-shape fixture from the tm1craft corpus."""
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def odata_body(value: list) -> bytes:
    """An OData collection payload (``{"value": [...]}``) as response bytes."""
    return json.dumps({"value": value}).encode("utf-8")


# --- duck-typed TM1py fakes ---------------------------------------------------


class FakeODataResponse:
    """Duck-typed ``requests.Response``: only ``.content`` is consumed."""

    def __init__(self, content: bytes) -> None:
        self.content = content


class FakeConnection:
    """Duck-typed TM1py ``RestService``: canned routes plus ``$count`` values.

    Routes map a URL substring to response bytes (first hit wins, so put
    the more specific key first); ``counts`` maps a collection name to its
    ``/<Collection>/$count`` answer. ``flaky`` maps a substring to a number
    of times GETs matching it raise before serving — the retry tests.
    """

    def __init__(
        self,
        routes: dict[str, bytes],
        counts: dict[str, int],
        *,
        flaky: dict[str, int] | None = None,
    ) -> None:
        self.routes = routes
        self.counts = counts
        self.flaky = dict(flaky or {})
        self.requests: list[tuple[str, dict[str, str] | None]] = []

    def GET(self, url: str, headers: dict[str, str] | None = None, **kwargs: Any) -> FakeODataResponse:  # noqa: N802 — TM1py's RestService method name, duck-typed
        self.requests.append((url, headers))
        for substring, failures_left in list(self.flaky.items()):
            if substring in url and failures_left > 0:
                self.flaky[substring] = failures_left - 1
                raise ConnectionError(f"transient transport failure for {url}")
        if url.endswith("/$count"):
            collection = url.split("/")[1]
            return FakeODataResponse(str(self.counts[collection]).encode("utf-8"))
        for substring, body in self.routes.items():
            if substring in url:
                return FakeODataResponse(body)
        raise AssertionError(f"unexpected GET {url}")

    def requested_urls(self, substring: str) -> list[str]:
        """Every requested URL carrying ``substring``, in request order."""
        return [url for url, _ in self.requests if substring in url]


class FakeTM1Object:
    """Duck-typed TM1py object: the entity dict as ``body_as_dict``."""

    def __init__(self, body_as_dict: dict[str, Any]) -> None:
        self._body_as_dict = body_as_dict

    @property
    def body_as_dict(self) -> dict[str, Any]:
        return self._body_as_dict


class FakeBodyTextObject:
    """Duck-typed TM1py object exposing only ``body`` (the entity as JSON text)."""

    def __init__(self, body_as_dict: dict[str, Any]) -> None:
        self._body = json.dumps(body_as_dict)

    @property
    def body(self) -> str:
        return self._body


class FakeService:
    """Duck-typed ``tm1.processes`` / ``tm1.chores`` slice: ``get_all()``."""

    def __init__(self, entities: list[dict[str, Any]]) -> None:
        self._entities = entities

    def get_all(self) -> list[Any]:
        return [FakeTM1Object(entity) for entity in self._entities]


class FakeMixedService:
    """The same slice over ready-made objects — exercises both the
    ``body_as_dict`` and the ``.body`` JSON-text paths TM1py objects offer."""

    def __init__(self, objects: list[Any]) -> None:
        self._objects = objects

    def get_all(self) -> list[Any]:
        return list(self._objects)


class FakeTM1:
    """Duck-typed ``TM1Service``: the three slices capture touches."""

    def __init__(
        self,
        processes: FakeService | None = None,
        chores: FakeService | None = None,
        connection: FakeConnection | None = None,
    ) -> None:
        self.processes = processes
        self.chores = chores
        self.connection = connection


# --- standard fixture data ------------------------------------------------------


def raw_cube_entities() -> list[dict[str, Any]]:
    """Two cubes in the composed-query shape: one MDX view, one name-only."""
    return [
        {
            "Name": "SalesDemo",
            "Rules": "['Sales'] = N: DB('SalesCube', !Year) * 1.1;\r\n",
            "Attributes": {"Description": "Sales planning demo cube"},
            "Dimensions": [{"Name": "Year"}, {"Name": "Measure"}],
            "Views": [{"Name": "budget", "MDX": "SELECT {} ON COLUMNS FROM [SalesDemo]"}, {"Name": "native input"}],
        },
        {"Name": "SalesCube", "Rules": "", "Attributes": {}, "Dimensions": [{"Name": "Year"}], "Views": []},
    ]


def raw_dimension_entities() -> list[dict[str, Any]]:
    """Three dimensions in the composed-query shape (paging fodder)."""
    year_hierarchy = {
        "Name": "Year",
        "UniqueName": "[Year].[Year]",
        "Cardinality": 2,
        "Visible": True,
        "Elements@odata.count": 24,
        "Edges@odata.count": 23,
        "DefaultMember": {"Name": "FY24"},
        "ElementAttributes": [{"Name": "Financial Year", "Type": "Alias"}],
        "Subsets": [{"Name": "Budget quarters", "Expression": "SELECT {[FY24]} ON ROWS"}, {"Name": "All quarters"}],
    }
    plain_hierarchy = {
        "Name": "Measure",
        "UniqueName": "[Measure].[Measure]",
        "Cardinality": 3,
        "Visible": True,
        "ElementAttributes": [],
        "Subsets": [],
    }
    return [
        {
            "Name": "Year",
            "Attributes": {"Description": "Financial calendar years"},
            "AllLeavesHierarchyName": "Year",
            "DefaultHierarchy": {"Name": "Year", "UniqueName": "[Year].[Year]"},
            "Hierarchies": [year_hierarchy],
        },
        {"Name": "Measure", "Attributes": {}, "Hierarchies": [plain_hierarchy]},
        {"Name": "}Dimensions", "Attributes": {}, "Hierarchies": [dict(plain_hierarchy, Name="}Dimensions")]},
    ]


def raw_process_entities() -> list[dict[str, Any]]:
    """Two process entities: the odbc fixture plus a body-as-text one."""
    odbc = load_fixture("tm1_json/odbc_load.json")
    return [odbc, {"Name": "plain.process", "PrologProcedure": "SaveDataAll;\r\n", "DataSource": {"Type": "None"}}]


def raw_chore_entities() -> list[dict[str, Any]]:
    """An active chore with two usable tasks (one without Parameters) and a
    task whose process name is unusable (dropped), plus a control chore."""
    return [
        {
            "Name": "Nightly load",
            "Active": True,
            "StartTime": "2026-09-13T02:00:00Z",
            "Frequency": {"Day": 1, "Hour": 2, "Minute": 0, "Second": 0},
            "ExecutionMode": "SingleCommit",
            "DSTSensitive": False,
            "Tasks": [
                {"Process": {"Name": "}tm1craft.demo.load"}, "Parameters": [{"Name": "pDebug", "Value": 0}]},
                {"Process": {"Name": "}tm1craft.demo.noop"}},
                {"Process": None},
            ],
        },
        {"Name": "}ChoreAdmin", "Active": False, "Tasks": []},
    ]


def make_connection(
    *,
    counts: dict[str, int] | None = None,
    cube_value: list | None = None,
    dimension_pages: dict[str, list] | None = None,
    process_fallback: bytes | None = None,
    chore_fallback: bytes | None = None,
    flaky: dict[str, int] | None = None,
) -> FakeConnection:
    """A FakeConnection serving the standard fixture data, tuned per test."""
    all_counts = {"Processes": 2, "Cubes": 2, "Dimensions": 3, "Chores": 2}
    all_counts.update(counts or {})
    routes: dict[str, bytes] = {}
    if cube_value is not None:
        routes["/Cubes?"] = odata_body(cube_value)
    else:
        routes["/Cubes?"] = odata_body(raw_cube_entities())
    for skip, page in (dimension_pages or {"$skip=0": raw_dimension_entities()}).items():
        routes[skip] = odata_body(page)
    if process_fallback is not None:
        routes["/Processes?"] = process_fallback
    if chore_fallback is not None:
        routes["/Chores?"] = chore_fallback
    return FakeConnection(routes, all_counts, flaky=flaky)


def make_tm1(
    connection: FakeConnection,
    *,
    processes: list[dict[str, Any]] | None = None,
    chores: list[dict[str, Any]] | None = None,
) -> FakeTM1:
    """A FakeTM1 wired with the standard fixture processes/chores.

    Process entities alternate between the ``body_as_dict`` and ``.body``
    paths so both TM1py object shapes stay covered.
    """
    process_entities = processes if processes is not None else raw_process_entities()
    process_objects = [
        FakeBodyTextObject(entity) if index % 2 else FakeTM1Object(entity)
        for index, entity in enumerate(process_entities)
    ]
    return FakeTM1(
        processes=FakeMixedService(process_objects),
        chores=FakeService(chores if chores is not None else raw_chore_entities()),
        connection=connection,
    )


# --- happy path ---------------------------------------------------------------


def test_capture_bundle_happy_path_all_four_kinds():
    connection = make_connection()
    bundle = capture_bundle(make_tm1(connection), "fpm-lab")

    assert bundle["instance"] == "fpm-lab"
    assert set(bundle["entities"]) == set(ENTITY_KINDS)

    processes = bundle["entities"]["processes"]
    assert [entity["Name"] for entity in processes] == ["}tm1craft.demo.odbc.load", "plain.process"]
    assert processes[0]["DataSource"]["Type"] == "ODBC"
    assert processes[1]["PrologProcedure"] == "SaveDataAll;\r\n"

    cubes = bundle["entities"]["cubes"]
    assert [cube["Name"] for cube in cubes] == ["SalesDemo", "SalesCube"]
    assert cubes[0]["Dimensions"] == ["Year", "Measure"]
    assert cubes[0]["Rules"].startswith("['Sales']")

    dimensions = bundle["entities"]["dimensions"]
    assert [dimension["Name"] for dimension in dimensions] == ["Year", "Measure", "}Dimensions"]
    year = dimensions[0]
    assert year["Hierarchies"][0]["ElementCount"] == 24
    assert year["Hierarchies"][0]["EdgeCount"] == 23
    assert year["Hierarchies"][0]["Cardinality"] == 2
    assert year["Hierarchies"][0]["Visible"] is True
    assert year["Hierarchies"][0]["DefaultMember"] == {"Name": "FY24"}
    assert year["DefaultHierarchy"] == {"Name": "Year", "UniqueName": "[Year].[Year]"}
    assert year["AllLeavesHierarchyName"] == "Year"

    chores = bundle["entities"]["chores"]
    assert [chore["Name"] for chore in chores] == ["Nightly load", "}ChoreAdmin"]
    tasks = chores[0]["Tasks"]
    assert tasks[0] == {
        "Process": {"Name": "}tm1craft.demo.load"},
        "Parameters": [{"Name": "pDebug", "Value": 0}],
    }
    assert "Parameters" not in tasks[1]
    assert len(tasks) == 2  # the Process-less task is dropped

    # the bundle must survive the wire to the arc-service job API
    assert json.loads(json.dumps(bundle)) == bundle


def test_capture_bundle_queries_match_the_plugin_dump_queries():
    connection = make_connection()
    capture_bundle(make_tm1(connection), "fpm-lab")
    requested = [url for url, _ in connection.requests]
    # processes ride tm1.processes.get_all(); the /Processes query is the decode-fallback path only
    assert not any(url.startswith("/Processes?") for url in requested)
    assert any(
        url.startswith("/Cubes?$select=Name,Rules,Attributes&$expand=Dimensions($select=Name),Views($select=Name,MDX)")
        for url in requested
    )
    assert any(
        url.startswith("/Dimensions?$select=Name,Attributes,AllLeavesHierarchyName&$expand=DefaultHierarchy")
        for url in requested
    )
    assert any("$filter=not%20startswith(Name,'%7D')))" in url for url in requested)


def test_on_progress_reports_each_kind():
    events: list[tuple[str, str]] = []
    connection = make_connection()
    capture_bundle(make_tm1(connection), "fpm-lab", on_progress=lambda kind, message: events.append((kind, message)))
    assert [kind for kind, _ in events] == ["processes", "cubes", "dimensions", "chores"]
    assert events[2] == ("dimensions", "3 fetched")


# --- password stripping -------------------------------------------------------


def test_password_stripped_depth_safe_from_every_entity():
    odbc = load_fixture("tm1_json/odbc_load.json")
    odbc["DataSource"]["Nested"] = [{"userName": "etl", "Password": "deep-secret"}, {"password": "deeper"}]
    odbc["DataSource"]["query"] = "SELECT 1"
    connection = make_connection(counts={"Processes": 1})
    bundle = capture_bundle(make_tm1(connection, processes=[odbc]), "fpm-lab")

    datasource = bundle["entities"]["processes"][0]["DataSource"]
    assert "password" not in {key.lower() for key in datasource}
    assert [sorted(item) for item in datasource["Nested"]] == [["userName"], []]
    assert datasource["userName"] == "etl_reader"  # identity stays; only the credential goes
    assert datasource["query"] == "SELECT 1"


def test_password_never_enters_the_bundle_from_any_kind():
    odbc = load_fixture("tm1_json/odbc_load.json")
    cubes = raw_cube_entities()
    cubes[0]["Password"] = "nope"
    dimensions = raw_dimension_entities()
    dimensions[0]["Hierarchies"][0]["Subsets"] = [{"Name": "s", "Expression": "X", "password": "nope"}]
    chores = raw_chore_entities()
    chores[0]["password"] = "nope"
    connection = make_connection(counts={"Processes": 1}, cube_value=cubes, dimension_pages={"$skip=0": dimensions})
    bundle = capture_bundle(
        make_tm1(connection, processes=[odbc], chores=chores),
        "fpm-lab",
        dimension_page_size=10,
    )
    serialized = json.dumps(bundle)
    assert "nope" not in serialized
    assert '"password"' not in serialized.lower()


# --- metadata-only paged dimension fetch ----------------------------------------


def test_dimension_fetch_pages_across_two_pages():
    dimensions = raw_dimension_entities()
    connection = make_connection(
        dimension_pages={"$skip=0": dimensions[:2], "$skip=2": dimensions[2:]},
        counts={"Dimensions": 3},
    )
    bundle = capture_bundle(make_tm1(connection), "fpm-lab", dimension_page_size=2)

    paged_urls = connection.requested_urls("$skip=")
    assert [url.split("&$skip=")[1] for url in paged_urls] == ["0", "2"]
    assert "&$top=2" in paged_urls[0]
    assert [dimension["Name"] for dimension in bundle["entities"]["dimensions"]] == ["Year", "Measure", "}Dimensions"]


def test_dimension_fetch_stops_after_a_short_page_without_an_extra_request():
    dimensions = raw_dimension_entities()
    connection = make_connection(dimension_pages={"$skip=0": dimensions}, counts={"Dimensions": 3})
    capture_bundle(make_tm1(connection), "fpm-lab")  # default page size 250: one short page
    assert len(connection.requested_urls("$skip=")) == 1


def test_count_endpoint_is_polled_with_text_plain_accept():
    connection = make_connection()
    capture_bundle(make_tm1(connection), "fpm-lab")
    count_requests = [(url, headers) for url, headers in connection.requests if url.endswith("/$count")]
    assert {url.split("/")[1] for url, _ in count_requests} == {"Processes", "Cubes", "Dimensions", "Chores"}
    assert all(headers == {"Accept": "text/plain"} for _, headers in count_requests)


def test_count_mismatch_raises_a_clear_error():
    connection = make_connection(counts={"Dimensions": 5})
    with pytest.raises(CaptureError) as raised:
        capture_bundle(make_tm1(connection), "fpm-lab")
    message = str(raised.value)
    assert "dimensions" in message
    assert "3" in message and "5" in message
    assert "/Dimensions/$count" in message


def test_dimension_fetch_retries_a_failed_page_once():
    connection = make_connection(flaky={"$skip=0": 1})
    bundle = capture_bundle(make_tm1(connection), "fpm-lab")
    assert len(connection.requested_urls("$skip=0")) == 2  # one failure, one retry, then done
    assert len(bundle["entities"]["dimensions"]) == 3


def test_dimension_fetch_fails_after_the_one_retry():
    connection = make_connection(flaky={"$skip=0": 2})
    with pytest.raises(CaptureError, match="after one retry"):
        capture_bundle(make_tm1(connection), "fpm-lab")


def test_dimension_page_size_must_be_positive():
    with pytest.raises(ValueError):
        capture_bundle(make_tm1(make_connection()), "fpm-lab", dimension_page_size=0)


# --- shape validation (all-or-nothing, #420) ------------------------------------


def test_cube_payload_missing_views_raises():
    broken = [{"Name": "SalesCube", "Dimensions": [{"Name": "Year"}]}]
    connection = make_connection(cube_value=broken)
    with pytest.raises(CaptureError, match="expanded cube shape"):
        capture_bundle(make_tm1(connection), "fpm-lab")


def test_dimension_payload_missing_hierarchy_subsets_raises():
    broken = [{"Name": "Year", "Hierarchies": [{"Name": "Year", "ElementAttributes": []}]}]
    connection = make_connection(dimension_pages={"$skip=0": broken})
    with pytest.raises(CaptureError, match="hierarchy shape"):
        capture_bundle(make_tm1(connection), "fpm-lab")


def test_odata_payload_without_a_value_array_raises():
    connection = FakeConnection(
        {"/Cubes?": b'{"error":{"code":"278","message":"nope"}}'},
        {"Processes": 0, "Cubes": 0, "Dimensions": 0, "Chores": 0},
    )
    with pytest.raises(CaptureError, match="OData value array"):
        capture_bundle(FakeTM1(processes=FakeService([]), chores=FakeService([]), connection=connection), "fpm-lab")


# --- decode repair --------------------------------------------------------------


def test_decode_strict_utf8_repairs_a_stray_code_page_byte():
    assert decode_strict_utf8(b"Epilog = 'ok';\r\n") == "Epilog = 'ok';\r\n"
    # a single stray 0xA6 (latin-1 '¦') inside an otherwise-UTF-8 body
    assert decode_strict_utf8(b"v = 1; \xa6 done") == "v = 1; ¦ done"
    # valid multi-byte UTF-8 around the stray byte passes through untouched
    assert decode_strict_utf8("café".encode() + b"\xa6") == "café¦"


def test_decode_repair_builds_valid_text_from_the_repaired_bytes():
    text = decode_strict_utf8(b"\xc3\xa9\xc3\xa9\xa6")
    assert text == "éé¦"
    text.encode("utf-8")  # the repair must leave re-encodable text


def test_tm1py_decode_damage_falls_back_to_a_connection_refetch():
    damaged = {
        "Name": "p.codepage",
        "PrologProcedure": "v = 1; \ufffd done",
        "DataSource": {"Type": "None"},
    }
    healthy_body = (
        '{"value": [{"Name": "p.codepage", "PrologProcedure": "v = 1; \xa6 done", "DataSource": {"Type": "None"}}]}'
    )
    connection = make_connection(
        counts={"Processes": 1},
        process_fallback=healthy_body.encode("latin-1"),  # the stray byte, exactly one wide
    )
    bundle = capture_bundle(make_tm1(connection, processes=[damaged]), "fpm-lab")

    process = bundle["entities"]["processes"][0]
    assert process["PrologProcedure"] == "v = 1; ¦ done"
    assert "\ufffd" not in json.dumps(bundle)
    assert connection.requested_urls("/Processes?")  # the kind was re-fetched through the connection


# --- control objects ------------------------------------------------------------


def test_control_objects_are_included():
    connection = make_connection()
    bundle = capture_bundle(make_tm1(connection), "fpm-lab")
    assert "}tm1craft.demo.odbc.load" in [entity["Name"] for entity in bundle["entities"]["processes"]]
    assert "}Dimensions" in [entity["Name"] for entity in bundle["entities"]["dimensions"]]
    assert "}ChoreAdmin" in [entity["Name"] for entity in bundle["entities"]["chores"]]


# --- mapper details --------------------------------------------------------------


def test_view_mdx_is_carried_verbatim_and_absent_when_not_projected():
    connection = make_connection()
    bundle = capture_bundle(make_tm1(connection), "fpm-lab")
    views = bundle["entities"]["cubes"][0]["Views"]
    assert views[0] == {"Name": "budget", "MDX": "SELECT {} ON COLUMNS FROM [SalesDemo]"}
    assert views[1] == {"Name": "native input"}  # no MDX key at all — never a guessed None
    assert bundle["entities"]["cubes"][1]["Views"] == []


def test_subset_expression_and_size_facts_degrade_without_guessing():
    dimensions = [
        {
            "Name": "Year",
            "Attributes": None,  # anything unexpected degrades to {}
            "Hierarchies": [
                {
                    "Name": "Year",
                    "ElementAttributes": [{"Name": "", "Type": "Alias"}, "junk", {"Name": "Alias attr", "Type": 7}],
                    "Subsets": [{"Name": ""}, {"Name": "dynamic", "Expression": "SELECT"}, 42],
                }
            ],
        }
    ]
    connection = make_connection(
        dimension_pages={"$skip=0": dimensions}, counts={"Dimensions": 1, "Processes": 0, "Chores": 0}
    )
    bundle = capture_bundle(
        FakeTM1(processes=FakeService([]), chores=FakeService([]), connection=connection), "fpm-lab"
    )

    hierarchy = bundle["entities"]["dimensions"][0]["Hierarchies"][0]
    assert "Cardinality" not in hierarchy  # not carried → not guessed
    assert "ElementCount" not in hierarchy
    assert bundle["entities"]["dimensions"][0]["Attributes"] == {}
    assert hierarchy["ElementAttributes"] == [{"Name": "Alias attr", "Type": ""}]
    assert hierarchy["Subsets"] == [{"Name": "dynamic", "Expression": "SELECT"}]


# --- structural parity with the tm1_json fixtures ---------------------------------


def fixture_without_passwords(value: Any) -> Any:
    """The fixture minus ``password`` keys at any depth — capture's documented
    transformation, spelled out locally so this test stays independent of
    the implementation's strip."""
    if isinstance(value, dict):
        return {key: fixture_without_passwords(item) for key, item in value.items() if key.lower() != "password"}
    if isinstance(value, list):
        return [fixture_without_passwords(item) for item in value]
    return value


def test_bundle_processes_match_the_tm1_json_fixture_shapes():
    """Every fixture process fed through capture comes out verbatim, minus
    the password — the exact keys and values the Tm1JsonLoader consumes."""
    fixture_processes = [
        load_fixture("tm1_json/demo_load.json"),
        load_fixture("tm1_json/ascii_load.json"),
        load_fixture("tm1_json/odbc_load.json"),
    ]
    connection = make_connection(counts={"Processes": 3})
    bundle = capture_bundle(make_tm1(connection, processes=fixture_processes), "fpm-lab")

    captured = bundle["entities"]["processes"]
    assert len(captured) == 3
    for fixture, entity in zip(fixture_processes, captured, strict=True):
        assert entity == fixture_without_passwords(fixture)


def test_bundle_cube_and_dimension_match_the_tm1_json_fixture_shapes():
    cube_fixture = load_fixture("tm1_json/sales_demo_cube.json")
    dimension_fixture = load_fixture("tm1_json/year_dimension.json")
    cube_value = [
        {
            "Name": cube_fixture["Name"],
            "Rules": cube_fixture["Rules"],
            "Attributes": cube_fixture["Attributes"],
            "Dimensions": [{"Name": name} for name in cube_fixture["Dimensions"]],
            "Views": [],
        }
    ]
    dimension_value = [
        {
            "Name": dimension_fixture["Name"],
            "Attributes": dimension_fixture["Attributes"],
            "Hierarchies": [
                {
                    "Name": dimension_fixture["Name"],
                    "ElementAttributes": dimension_fixture["ElementAttributes"],
                    "Subsets": [],
                }
            ],
        }
    ]
    connection = make_connection(
        counts={"Cubes": 1, "Dimensions": 1, "Processes": 0, "Chores": 0},
        cube_value=cube_value,
        dimension_pages={"$skip=0": dimension_value},
    )
    bundle = capture_bundle(
        FakeTM1(processes=FakeService([]), chores=FakeService([]), connection=connection), "fpm-lab"
    )

    cube = bundle["entities"]["cubes"][0]
    assert cube["Name"] == cube_fixture["Name"]
    assert cube["Rules"] == cube_fixture["Rules"]
    assert cube["Attributes"] == cube_fixture["Attributes"]
    assert cube["Dimensions"] == cube_fixture["Dimensions"]
    assert cube["Views"] == []

    dimension = bundle["entities"]["dimensions"][0]
    assert dimension["Name"] == dimension_fixture["Name"]
    assert dimension["Attributes"] == dimension_fixture["Attributes"]
    assert dimension["Hierarchies"][0]["ElementAttributes"] == dimension_fixture["ElementAttributes"]
    # the loader's dimension marker: the metadata-only Hierarchies shape
    assert isinstance(dimension["Hierarchies"], list)
