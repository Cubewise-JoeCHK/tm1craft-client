"""Tests for the CLI wiring: fakes behind capture/upload/TM1py, exit codes, channel split.

No test touches a network: the TM1py service, ``capture_bundle`` and
``upload_bundle`` are monkeypatched with recording fakes; the config
layer (``tests/test_config.py``) covers the INI parsing itself.
"""

import importlib.metadata
import json

import pytest

from tm1craft_client import cli
from tm1craft_client.capture import ENTITY_KINDS, CaptureError
from tm1craft_client.jobclient import JobResult, ServiceError

REGISTRY_INI = """\
[prod]
base = http://tm1-prod:12354/api/v1
user = admin
password = s3cret-prod

[dev]
base = https://tm1-dev:12354/api/v1
user = devuser

[service]
upload_url = http://craft-service.example.com
"""

DONE_RESULT = JobResult(job_id="job-1", instance="prod", status="done", attached=False, nodes=10, edges=20, unchanged=2)

BUNDLE = {"instance": "prod", "entities": {"processes": [{"Name": "Process1"}]}}


@pytest.fixture()
def registry_path(tmp_path):
    """The standard two-instance registry written to tmp."""
    path = tmp_path / "tm1-client.ini"
    path.write_text(REGISTRY_INI, encoding="utf-8")
    return str(path)


@pytest.fixture()
def tm1_recorder(monkeypatch):
    """Replace cli.TM1Service with a fake that records constructor kwargs and logout calls."""
    created = []

    class FakeTM1Service:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.logged_out = False
            created.append(self)

        def logout(self):
            self.logged_out = True

    monkeypatch.setattr(cli, "TM1Service", FakeTM1Service)
    return created


@pytest.fixture()
def no_default_ini(tmp_path, monkeypatch):
    """A cwd and HOME with no credentials INI, so --credentials and the default order are both testable."""
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def install_capture(monkeypatch, bundle=BUNDLE, problem=None):
    """Replace cli.capture_bundle with a recording fake returning ``bundle`` or raising ``problem``."""
    calls = []

    def fake_capture_bundle(tm1, instance_name, *, on_progress=None, dimension_page_size=250):
        calls.append({"tm1": tm1, "instance": instance_name})
        if problem is not None:
            raise problem
        if on_progress is not None:
            on_progress("processes", "2 fetched")
        return bundle

    monkeypatch.setattr(cli, "capture_bundle", fake_capture_bundle)
    return calls


def install_upload(monkeypatch, result=DONE_RESULT, problem=None):
    """Replace cli.upload_bundle with a recording fake returning ``result`` or raising ``problem``."""
    calls = []

    def fake_upload_bundle(
        service_url,
        bundle,
        *,
        instance,
        arc_origin=None,
        license_key=None,
        poll_timeout_seconds=1800.0,
        on_stage=None,
    ):
        calls.append(
            {
                "service_url": service_url,
                "bundle": bundle,
                "instance": instance,
                "arc_origin": arc_origin,
                "license_key": license_key,
            }
        )
        if problem is not None:
            raise problem
        if on_stage is not None:
            on_stage("staging-write")
        return result

    monkeypatch.setattr(cli, "upload_bundle", fake_upload_bundle)
    return calls


# --- dump: happy path and forwarding -----------------------------------------


def test_dump_happy_path_exits_zero(registry_path, tm1_recorder, monkeypatch, capsys):
    install_capture(monkeypatch)
    upload_calls = install_upload(monkeypatch)
    exit_code = cli.main(["dump", "prod", "--credentials", registry_path])
    assert exit_code == 0
    assert len(upload_calls) == 1
    assert upload_calls[0]["service_url"] == "http://craft-service.example.com"
    assert upload_calls[0]["instance"] == "prod"
    output = capsys.readouterr()
    assert "job-1" in output.out
    assert "done" in output.out
    assert "nodes: 10" in output.out
    assert "edges: 20" in output.out
    assert "unchanged: 2" in output.out
    assert "capture processes: 2 fetched" in output.err
    assert "build stage: staging-write" in output.err


def test_dump_connection_args_reach_tm1py(registry_path, tm1_recorder, monkeypatch):
    install_capture(monkeypatch)
    install_upload(monkeypatch)
    cli.main(["dump", "prod", "--credentials", registry_path])
    assert len(tm1_recorder) == 1
    assert tm1_recorder[0].kwargs == {
        "base_url": "http://tm1-prod:12354/api/v1",
        "user": "admin",
        "password": "s3cret-prod",
        "ssl": False,
    }


def test_dump_https_base_sets_ssl_true(registry_path, tm1_recorder, monkeypatch):
    install_capture(monkeypatch)
    install_upload(monkeypatch)
    cli.main(["dump", "dev", "--credentials", registry_path])
    assert tm1_recorder[0].kwargs["ssl"] is True


def test_dump_passes_the_captured_bundle_through(registry_path, tm1_recorder, monkeypatch):
    capture_calls = install_capture(monkeypatch)
    upload_calls = install_upload(monkeypatch)
    cli.main(["dump", "prod", "--credentials", registry_path])
    assert capture_calls[0]["instance"] == "prod"
    assert capture_calls[0]["tm1"] is tm1_recorder[0]
    assert upload_calls[0]["bundle"] is BUNDLE["entities"]


def test_dump_hands_upload_bundle_the_kind_mapping_not_the_outer_bundle(registry_path, tm1_recorder, monkeypatch):
    """The #5 live-acceptance regression: ``upload_bundle`` validates kind keys, so the
    CLI must unwrap ``capture_bundle``'s ``{"instance", "entities"}`` wrapper — passing
    the outer dict dies on 'bundle carries kinds outside the wire contract'."""
    install_capture(monkeypatch)
    upload_calls = install_upload(monkeypatch)
    exit_code = cli.main(["dump", "prod", "--credentials", registry_path])
    assert exit_code == 0
    assert set(upload_calls[0]["bundle"]) <= set(ENTITY_KINDS)
    assert "instance" not in upload_calls[0]["bundle"]


class _EmptyModelResponse:
    """Duck-typed ``requests.Response``: only ``.content`` is consumed."""

    def __init__(self, content: bytes) -> None:
        self.content = content


class _EmptySlice:
    """Duck-typed ``tm1.processes`` / ``tm1.chores`` slice: no entities."""

    def get_all(self):
        return []


class _EmptyModelTM1:
    """Duck-typed ``TM1Service`` serving an empty model to the real ``capture_bundle``.

    The connection answers every collection query with ``{"value": []}`` and
    every ``/<Collection>/$count`` with ``0``, so the capture's count
    cross-check passes with all four kinds empty.
    """

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.logged_out = False
        self.processes = _EmptySlice()
        self.chores = _EmptySlice()

    @property
    def connection(self) -> "_EmptyModelConnection":
        return _EmptyModelConnection()

    def logout(self) -> None:
        self.logged_out = True


class _EmptyModelConnection:
    def GET(self, url: str, headers=None, **kwargs) -> _EmptyModelResponse:  # noqa: N802 — TM1py's method name
        if url.endswith("/$count"):
            return _EmptyModelResponse(b"0")
        return _EmptyModelResponse(b'{"value": []}')


def test_dump_runs_the_real_capture_into_the_upload_contract(registry_path, monkeypatch, capsys):
    """The #5 seam, end to end with the real ``capture_bundle``: the entities
    mapping it returns (not the ``{"instance", "entities"}`` wrapper) is what
    reaches ``upload_bundle`` — the live acceptance failed exactly here."""
    created = []

    def fake_tm1_service(**kwargs):
        service = _EmptyModelTM1(**kwargs)
        created.append(service)
        return service

    monkeypatch.setattr(cli, "TM1Service", fake_tm1_service)
    upload_calls = install_upload(monkeypatch)
    exit_code = cli.main(["dump", "prod", "--credentials", registry_path])
    assert exit_code == 0
    assert created[0].logged_out is True
    assert upload_calls[0]["bundle"] == {"processes": [], "cubes": [], "dimensions": [], "chores": []}
    assert "capture chores: 0 fetched" in capsys.readouterr().err


def test_dump_logs_out_even_when_capture_fails(registry_path, tm1_recorder, monkeypatch):
    install_capture(monkeypatch, problem=CaptureError("boom"))
    install_upload(monkeypatch)
    exit_code = cli.main(["dump", "prod", "--credentials", registry_path])
    assert exit_code == 1
    assert len(tm1_recorder) == 1
    assert tm1_recorder[0].logged_out is True


def test_dump_flag_overrides_reach_tm1py(registry_path, tm1_recorder, monkeypatch):
    install_capture(monkeypatch)
    install_upload(monkeypatch)
    argv = [
        "dump",
        "prod",
        "--credentials",
        registry_path,
        "--base",
        "http://override:9999/api/v1",
        "--user",
        "override-user",
        "--password",
        "override-secret",
    ]
    cli.main(argv)
    assert tm1_recorder[0].kwargs == {
        "base_url": "http://override:9999/api/v1",
        "user": "override-user",
        "password": "override-secret",
        "ssl": False,
    }


def test_dump_service_flag_wins_over_ini(registry_path, tm1_recorder, monkeypatch):
    install_capture(monkeypatch)
    upload_calls = install_upload(monkeypatch)
    cli.main(["dump", "prod", "--credentials", registry_path, "--service", "http://flag-service.example.com"])
    assert upload_calls[0]["service_url"] == "http://flag-service.example.com"


def test_dump_forwards_arc_origin(registry_path, tm1_recorder, monkeypatch):
    install_capture(monkeypatch)
    upload_calls = install_upload(monkeypatch)
    cli.main(["dump", "prod", "--credentials", registry_path, "--arc-origin", "http://arc.example.com"])
    assert upload_calls[0]["arc_origin"] == "http://arc.example.com"


def test_dump_without_arc_origin_forwards_none(registry_path, tm1_recorder, monkeypatch):
    install_capture(monkeypatch)
    upload_calls = install_upload(monkeypatch)
    cli.main(["dump", "prod", "--credentials", registry_path])
    assert upload_calls[0]["arc_origin"] is None


def test_dump_license_key_flag_wins_over_env(registry_path, tm1_recorder, monkeypatch):
    monkeypatch.setenv("TM1CRAFT_CLIENT_LICENSE_KEY", "env-key")
    install_capture(monkeypatch)
    upload_calls = install_upload(monkeypatch)
    cli.main(["dump", "prod", "--credentials", registry_path, "--license-key", "flag-key"])
    assert upload_calls[0]["license_key"] == "flag-key"


def test_dump_license_key_falls_back_to_env(registry_path, tm1_recorder, monkeypatch):
    monkeypatch.setenv("TM1CRAFT_CLIENT_LICENSE_KEY", "env-key")
    install_capture(monkeypatch)
    upload_calls = install_upload(monkeypatch)
    cli.main(["dump", "prod", "--credentials", registry_path])
    assert upload_calls[0]["license_key"] == "env-key"


def test_dump_without_license_key_forwards_none(registry_path, tm1_recorder, monkeypatch):
    monkeypatch.delenv("TM1CRAFT_CLIENT_LICENSE_KEY", raising=False)
    install_capture(monkeypatch)
    upload_calls = install_upload(monkeypatch)
    cli.main(["dump", "prod", "--credentials", registry_path])
    assert upload_calls[0]["license_key"] is None


def test_dump_default_credentials_order_reads_cwd_ini(tmp_path, tm1_recorder, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (tmp_path / "tm1-client.ini").write_text(REGISTRY_INI, encoding="utf-8")
    install_capture(monkeypatch)
    upload_calls = install_upload(monkeypatch)
    exit_code = cli.main(["dump", "prod"])
    assert exit_code == 0
    assert upload_calls[0]["service_url"] == "http://craft-service.example.com"


# --- dump: exit code 2 — usage/config problems --------------------------------


def test_dump_missing_ini_exits_two_with_the_order(no_default_ini, capsys):
    exit_code = cli.main(["dump", "prod"])
    assert exit_code == 2
    message = capsys.readouterr().err
    assert "./tm1-client.ini" in message and "~/.tm1-client.ini" in message


def test_dump_explicit_credentials_missing_exits_two(no_default_ini, capsys):
    exit_code = cli.main(["dump", "prod", "--credentials", "/no/such/registry.ini"])
    assert exit_code == 2
    assert "not found" in capsys.readouterr().err


def test_dump_unknown_instance_exits_two(registry_path, capsys):
    exit_code = cli.main(["dump", "staging", "--credentials", registry_path])
    assert exit_code == 2
    assert "staging" in capsys.readouterr().err


def test_dump_missing_service_exits_two(tmp_path, capsys):
    path = tmp_path / "registry.ini"
    path.write_text("[prod]\nbase = http://tm1:12354/api/v1\nuser = admin\n", encoding="utf-8")
    exit_code = cli.main(["dump", "prod", "--credentials", str(path)])
    assert exit_code == 2
    assert "--service" in capsys.readouterr().err


def test_dump_missing_instance_argument_exits_two():
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["dump"])
    assert exit_info.value.code == 2


def test_dump_non_http_base_exits_two(registry_path, tm1_recorder, monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "resolve_instance",
        lambda *args, **kwargs: cli.InstanceConfig(name="prod", base="ftp://tm1:12354/api/v1", user="u", password=""),
    )
    install_capture(monkeypatch)
    install_upload(monkeypatch)
    exit_code = cli.main(["dump", "prod", "--credentials", registry_path])
    assert exit_code == 2
    assert "http" in capsys.readouterr().err


# --- dump: exit code 1 — capture/upload/service failures ----------------------


def test_dump_capture_failure_is_one_stderr_line(registry_path, tm1_recorder, monkeypatch, capsys):
    install_capture(monkeypatch, problem=CaptureError("count mismatch: 4 fetched, 3 counted"))
    install_upload(monkeypatch)
    exit_code = cli.main(["dump", "prod", "--credentials", registry_path])
    assert exit_code == 1
    output = capsys.readouterr()
    assert output.err == "error: count mismatch: 4 fetched, 3 counted\n"
    assert "Traceback" not in output.err
    assert output.out == ""


def test_dump_service_failure_is_one_stderr_line(registry_path, tm1_recorder, monkeypatch, capsys):
    install_capture(monkeypatch)
    install_upload(monkeypatch, problem=ServiceError("POST", "http://craft/model-dump-job", 500, "kaboom"))
    exit_code = cli.main(["dump", "prod", "--credentials", registry_path])
    assert exit_code == 1
    output = capsys.readouterr()
    assert output.err.splitlines()[-1] == "error: POST http://craft/model-dump-job returned HTTP 500: kaboom"
    assert "Traceback" not in output.err


def test_dump_failed_build_is_exit_one_with_the_server_error(registry_path, tm1_recorder, monkeypatch, capsys):
    failed = JobResult(job_id="job-9", instance="prod", status="failed", attached=False, error="cube vanished")
    install_capture(monkeypatch)
    install_upload(monkeypatch, result=failed)
    exit_code = cli.main(["dump", "prod", "--credentials", registry_path])
    assert exit_code == 1
    output = capsys.readouterr()
    assert "job-9" in output.err and "failed" in output.err and "cube vanished" in output.err
    assert output.out == ""


def test_dump_error_path_never_prints_the_password(registry_path, tm1_recorder, monkeypatch, capsys):
    install_capture(monkeypatch)
    install_upload(monkeypatch, problem=RuntimeError("upload leaked s3cret-prod in its body"))
    exit_code = cli.main(["dump", "prod", "--credentials", registry_path])
    assert exit_code == 1
    message = capsys.readouterr().err
    assert "s3cret-prod" not in message
    assert "***" in message


def test_dump_debug_reraises_instead_of_one_line(registry_path, tm1_recorder, monkeypatch):
    install_capture(monkeypatch, problem=CaptureError("boom"))
    install_upload(monkeypatch)
    with pytest.raises(cli.DumpError, match="boom") as exit_info:
        cli.main(["--debug", "dump", "prod", "--credentials", registry_path])
    assert isinstance(exit_info.value.__cause__, CaptureError)


def test_dump_multiline_error_collapses_to_one_line(registry_path, tm1_recorder, monkeypatch, capsys):
    install_capture(monkeypatch)
    install_upload(monkeypatch, problem=RuntimeError("line one\nline two\n  line three"))
    exit_code = cli.main(["dump", "prod", "--credentials", registry_path])
    assert exit_code == 1
    assert capsys.readouterr().err.splitlines()[-1] == "error: line one line two line three"


# --- list ---------------------------------------------------------------------


def test_list_prints_names_and_bases(registry_path, capsys):
    exit_code = cli.main(["list", "--credentials", registry_path])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "prod: http://tm1-prod:12354/api/v1" in out
    assert "dev: https://tm1-dev:12354/api/v1" in out
    assert "service" not in out
    assert "s3cret-prod" not in out


def test_list_missing_ini_exits_two(no_default_ini, capsys):
    exit_code = cli.main(["list"])
    assert exit_code == 2
    assert "./tm1-client.ini" in capsys.readouterr().err


# --- parser surface -----------------------------------------------------------


def test_top_level_help_lists_the_commands(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "dump" in out and "list" in out


def test_dump_help_states_the_credentials_order(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["dump", "--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "./tm1-client.ini" in out and "~/.tm1-client.ini" in out
    for flag in ("--credentials", "--base", "--user", "--password", "--service", "--arc-origin", "--license-key"):
        assert flag in out


def test_version_prints_the_package_metadata_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert importlib.metadata.version("tm1craft-client") in capsys.readouterr().out


def test_console_script_entry_point_is_declared():
    matches = importlib.metadata.entry_points(group="console_scripts", name="tm1craft-client")
    assert [entry.value for entry in matches] == ["tm1craft_client.cli:main"]


# --- two-phase: dump --out writes a file, upload replays it (#16) --------------


PROD_ONLY_INI = """\
[prod]
base = http://tm1-prod:12354/api/v1
user = admin
password = s3cret-prod
"""

FULL_BUNDLE = {
    "instance": "prod",
    "entities": {kind: ([] if kind != "processes" else [{"Name": "Process1"}]) for kind in ENTITY_KINDS},
}


def test_dump_out_writes_the_bundle_and_needs_no_service(tmp_path, tm1_recorder, monkeypatch, capsys):
    """--out captures to a file with no service anywhere: a registry without
    [service] succeeds, upload_bundle is never called, and the file holds the
    bundle verbatim."""
    registry = tmp_path / "tm1-client.ini"
    registry.write_text(PROD_ONLY_INI, encoding="utf-8")
    out_file = tmp_path / "bundle.json"
    install_capture(monkeypatch)
    upload_calls = install_upload(monkeypatch)

    exit_code = cli.main(["dump", "prod", "--credentials", str(registry), "--out", str(out_file)])

    assert exit_code == 0
    assert upload_calls == []
    assert json.loads(out_file.read_text(encoding="utf-8")) == BUNDLE
    output = capsys.readouterr()
    assert "captured prod" in output.out
    assert f"path: {out_file.resolve()}" in output.out


def test_dump_out_unwritable_path_exits_one(tmp_path, tm1_recorder, monkeypatch, capsys):
    registry = tmp_path / "tm1-client.ini"
    registry.write_text(PROD_ONLY_INI, encoding="utf-8")
    install_capture(monkeypatch)
    install_upload(monkeypatch)

    exit_code = cli.main(
        ["dump", "prod", "--credentials", str(registry), "--out", str(tmp_path / "no-such-dir" / "b.json")]
    )

    assert exit_code == 1
    assert "cannot write" in capsys.readouterr().err


def test_upload_round_trips_a_bundle_file(tmp_path, monkeypatch, capsys):
    bundle_file = tmp_path / "bundle.json"
    bundle_file.write_text(json.dumps(FULL_BUNDLE), encoding="utf-8")
    upload_calls = install_upload(monkeypatch)

    exit_code = cli.main(["upload", str(bundle_file), "--service", "http://svc.example.com"])

    assert exit_code == 0
    assert len(upload_calls) == 1
    assert upload_calls[0]["service_url"] == "http://svc.example.com"
    assert upload_calls[0]["instance"] == "prod"
    assert upload_calls[0]["bundle"] == FULL_BUNDLE["entities"]
    assert "job job-1 done" in capsys.readouterr().out


def test_upload_reads_the_service_from_the_registry(tmp_path, registry_path, monkeypatch):
    bundle_file = tmp_path / "bundle.json"
    bundle_file.write_text(json.dumps(FULL_BUNDLE), encoding="utf-8")
    upload_calls = install_upload(monkeypatch)

    exit_code = cli.main(["upload", str(bundle_file), "--credentials", registry_path])

    assert exit_code == 0
    assert upload_calls[0]["service_url"] == "http://craft-service.example.com"


def test_upload_without_service_or_registry_exits_two(tmp_path, no_default_ini, monkeypatch, capsys):
    bundle_file = tmp_path / "bundle.json"
    bundle_file.write_text(json.dumps(FULL_BUNDLE), encoding="utf-8")
    install_upload(monkeypatch)

    exit_code = cli.main(["upload", str(bundle_file)])

    assert exit_code == 2
    assert "no service URL" in capsys.readouterr().err


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        "[1, 2, 3]",
        '{"entities": {"processes": []}}',
        '{"instance": "prod", "entities": {"processes": []}}',
    ],
)
def test_upload_rejects_malformed_bundle_files(tmp_path, monkeypatch, capsys, content):
    bundle_file = tmp_path / "bundle.json"
    bundle_file.write_text(content, encoding="utf-8")
    install_upload(monkeypatch)

    exit_code = cli.main(["upload", str(bundle_file), "--service", "http://svc.example.com"])

    assert exit_code == 2
    assert "bundle" in capsys.readouterr().err


def test_upload_help_lists_the_flags(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["upload", "--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--credentials", "--service", "--arc-origin", "--license-key"):
        assert flag in out


def test_dump_help_mentions_out(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["dump", "--help"])
    assert exit_info.value.code == 0
    assert "--out" in capsys.readouterr().out
