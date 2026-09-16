"""The ``tm1craft-client`` command line: dump a TM1 instance to a tm1craft service.

Three commands. ``dump <instance>`` resolves the instance's connection from
the ADR-0003 INI credentials registry (:mod:`tm1craft_client.config`),
captures the four model-object kinds read-only with TM1py
(:func:`tm1craft_client.capture.capture_bundle`) and hands the bundle to a
tm1craft arc-service model-dump job
(:func:`tm1craft_client.jobclient.upload_bundle`) — the client never
builds a database, staging and graph building are the service's business.
``dump --out <path>`` stops after the capture and writes the bundle to a
local file instead — the two-phase flow for boxes that cannot reach the
service: capture there, move the file, then ``upload <path>`` replays it
into the same job funnel from anywhere. ``list`` prints the registry's
instance sections.

Channel discipline: progress and build stages render on **stderr**; the
final summary (job id, status, server-reported counts — or, in the
``--out`` mode, the captured counts and the file path) goes to **stdout**
— so ``tm1craft-client dump prod 2>capture.log`` pipes a clean report.
Exit codes: ``0`` success, ``1`` capture/upload/service failure (the
underlying error's message, one line, redacted — a traceback only under
``--debug``), ``2`` usage/config problems (missing INI, unknown instance,
missing ``--service``, malformed bundle file). The password is never
printed and argv is never logged.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import sys
from urllib.parse import urlparse

from TM1py import TM1Service

from tm1craft_client.capture import ENTITY_KINDS, capture_bundle
from tm1craft_client.config import (
    ConfigError,
    InstanceConfig,
    find_credentials_file,
    list_instances,
    load_registry,
    redact,
    resolve_instance,
    resolve_service_url,
)
from tm1craft_client.jobclient import JobResult, upload_bundle

LICENSE_KEY_ENV_VAR = "TM1CRAFT_CLIENT_LICENSE_KEY"
# --license-key falls back to this environment variable.

PACKAGE_NAME = "tm1craft-client"


class DumpError(Exception):
    """A capture/upload/service failure after config resolved; the message is one line and redacted."""


def main(argv: list[str] | None = None) -> int:
    """Run the CLI; return the process exit code (0 ok, 1 failure, 2 usage/config)."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except ConfigError as problem:
        if args.debug:
            raise
        print(f"error: {problem}", file=sys.stderr)
        return 2
    except DumpError as problem:
        if args.debug:
            raise
        print(f"error: {problem}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    """Assemble the parser: the global flags plus the ``dump`` and ``list`` subcommands."""
    parser = argparse.ArgumentParser(
        prog=PACKAGE_NAME,
        description=(
            "Capture a TM1 instance and hand it to a tm1craft service. "
            "The client only captures and uploads — it never builds a database."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_package_version()}")
    parser.add_argument("--debug", action="store_true", help="on failure, show the traceback instead of one line")
    commands = parser.add_subparsers(dest="command", required=True, metavar="{dump,upload,list}")

    credentials_help = (
        "INI credentials registry (ADR-0003 format); default: try ./tm1-client.ini then ~/.tm1-client.ini"
    )

    dump = commands.add_parser("dump", help="capture one TM1 instance and upload it to a tm1craft service")
    dump.add_argument("instance", help="TM1 instance name — matches an INI section exactly")
    dump.add_argument("--credentials", metavar="PATH", help=credentials_help)
    dump.add_argument(
        "--base", help="one-off override of the instance's base URL (full API base, e.g. http://host:12354/api/v1)"
    )
    dump.add_argument("--user", help="one-off override of the instance's user")
    dump.add_argument("--password", help="one-off override of the instance's password (may be empty)")
    dump.add_argument(
        "--service", metavar="URL", help="tm1craft service base URL; wins over the INI's [service] upload_url"
    )
    dump.add_argument("--arc-origin", help="Arc origin forwarded to the service (rides the job start as arcOrigin)")
    dump.add_argument(
        "--license-key",
        help=f"service license key (default: env {LICENSE_KEY_ENV_VAR}); absent → no Authorization header",
    )
    dump.add_argument(
        "--out",
        metavar="PATH",
        help="write the captured bundle to this file instead of uploading — no service is contacted; "
        "replay it later with 'upload'",
    )
    dump.set_defaults(handler=_run_dump)

    upload = commands.add_parser("upload", help="upload a bundle file written by 'dump --out' to a tm1craft service")
    upload.add_argument("bundle", help="bundle file written by 'dump --out'")
    upload.add_argument(
        "--credentials",
        metavar="PATH",
        help="optional INI whose [service] upload_url is used when --service is not given",
    )
    upload.add_argument(
        "--service", metavar="URL", help="tm1craft service base URL; wins over the INI's [service] upload_url"
    )
    upload.add_argument("--arc-origin", help="Arc origin forwarded to the service (rides the job start as arcOrigin)")
    upload.add_argument(
        "--license-key",
        help=f"service license key (default: env {LICENSE_KEY_ENV_VAR}); absent → no Authorization header",
    )
    upload.set_defaults(handler=_run_upload)

    listing = commands.add_parser("list", help="print the registry's instance sections (name + base URL)")
    listing.add_argument("--credentials", metavar="PATH", help=credentials_help)
    listing.set_defaults(handler=_run_list)
    return parser


def _package_version() -> str:
    """The installed package version (importlib metadata, not a hardcoded literal)."""
    try:
        return importlib.metadata.version(PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _open_registry(explicit: str | None) -> tuple[str, argparse.Namespace]:
    """Locate and parse the credentials INI; return ``(path, registry)``."""
    path = find_credentials_file(explicit)
    return path, load_registry(path)


def _run_dump(args: argparse.Namespace) -> int:
    """The dump pipeline: resolve config, capture with TM1py, upload (or write) the bundle, report on stdout."""
    _, registry = _open_registry(args.credentials)
    instance = resolve_instance(
        registry,
        args.instance,
        base=args.base,
        user=args.user,
        password=args.password,
    )
    # Fail fast: without --out the service URL must resolve before any TM1
    # work happens — capturing a whole model just to die on config is waste.
    service_url = None if args.out else resolve_service_url(registry, args.service)
    bundle = _capture_instance(instance)
    if args.out:
        try:
            _write_bundle_file(bundle, args.out)
        except (OSError, TypeError, ValueError) as problem:
            raise DumpError(f"cannot write bundle file {args.out}: {_one_line(str(problem))}") from problem
        _print_capture_summary(bundle, args.out)
        return 0
    license_key = args.license_key or os.environ.get(LICENSE_KEY_ENV_VAR) or None
    try:
        result = upload_bundle(
            service_url,
            bundle["entities"],
            instance=instance.name,
            arc_origin=args.arc_origin,
            license_key=license_key,
            on_stage=_report_build_stage,
        )
    except ConfigError:
        raise
    except Exception as problem:
        raise DumpError(redact(_one_line(str(problem)), instance.password)) from problem
    if result.status != "done":
        detail = f": {result.error}" if result.error else ""
        raise DumpError(redact(_one_line(f"job {result.job_id} ended {result.status}{detail}"), instance.password))
    _print_summary(result)
    return 0


def _run_upload(args: argparse.Namespace) -> int:
    """The replay half of the two-phase flow: read a bundle file, hand it to the same job funnel."""
    instance_name, entities = _read_bundle_file(args.bundle)
    service_url = _resolve_upload_service(args.service, args.credentials)
    license_key = args.license_key or os.environ.get(LICENSE_KEY_ENV_VAR) or None
    try:
        result = upload_bundle(
            service_url,
            entities,
            instance=instance_name,
            arc_origin=args.arc_origin,
            license_key=license_key,
            on_stage=_report_build_stage,
        )
    except ConfigError:
        raise
    except Exception as problem:
        raise DumpError(_one_line(str(problem))) from problem
    if result.status != "done":
        detail = f": {result.error}" if result.error else ""
        raise DumpError(_one_line(f"job {result.job_id} ended {result.status}{detail}"))
    _print_summary(result)
    return 0


def _capture_instance(instance: InstanceConfig) -> dict:
    """Connect, capture, log out — the TM1 half of both dump modes, DumpError-wrapped and redacted."""
    try:
        tm1 = _connect_tm1(instance)
        try:
            return capture_bundle(tm1, instance.name, on_progress=_report_capture_progress)
        finally:
            tm1.logout()
    except ConfigError:
        raise
    except Exception as problem:
        raise DumpError(redact(_one_line(str(problem)), instance.password)) from problem


def _resolve_upload_service(flag: str | None, credentials: str | None) -> str:
    """The service URL for ``upload``: the flag wins; else the registry's ``[service]`` when one loads.

    Unlike ``dump``, a missing registry is fine here — upload touches no TM1
    instance, so only the ``[service]`` section is wanted from the INI. An
    explicit ``--credentials`` path still surfaces its own errors (the user
    pointed at something); the implicit default order degrades to the
    flag-required message instead.
    """
    if flag:
        return flag
    if credentials:
        _, registry = _open_registry(credentials)
        return resolve_service_url(registry, None)
    try:
        _, registry = _open_registry(None)
    except ConfigError:
        raise ConfigError(
            "no service URL — pass --service <url> or set upload_url in the registry's [service] section"
        ) from None
    return resolve_service_url(registry, None)


def _write_bundle_file(bundle: dict, path: str) -> None:
    """Write the bundle verbatim as UTF-8 JSON, newline-terminated.

    The bundle self-describes (``instance`` + the four password-stripped
    entity kinds), so the file needs no envelope — ``upload`` reads back
    exactly what ``capture_bundle`` produced.
    """
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(bundle, handle, ensure_ascii=False)
        handle.write("\n")


def _read_bundle_file(path: str) -> tuple[str, dict]:
    """Read a bundle written by ``dump --out``; return ``(instance, entities)``.

    A file that is not a bundle — unparseable, wrong shape, missing entity
    kinds — is a usage problem (exit 2), named with what is missing.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            bundle = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as problem:
        raise ConfigError(f"cannot read bundle file {path}: {_one_line(str(problem))}") from problem
    if (
        not isinstance(bundle, dict)
        or not isinstance(bundle.get("instance"), str)
        or not bundle["instance"]
        or not isinstance(bundle.get("entities"), dict)
    ):
        raise ConfigError(f"bundle file {path} is not a dump bundle (need an instance string and an entities object)")
    entities = bundle["entities"]
    missing = [kind for kind in ENTITY_KINDS if not isinstance(entities.get(kind), list)]
    if missing:
        raise ConfigError(f"bundle file {path} is missing entity kinds: {', '.join(missing)}")
    return bundle["instance"], entities


def _print_capture_summary(bundle: dict, out_path: str) -> None:
    """Print the ``--out`` report to stdout: per-kind counts and where the bundle landed."""
    counts = ", ".join(f"{len(bundle['entities'].get(kind, []))} {kind}" for kind in ENTITY_KINDS)
    print(f"captured {bundle['instance']}: {counts}")
    print(f"path: {os.path.abspath(out_path)}")


def _connect_tm1(instance: InstanceConfig) -> TM1Service:
    """Open the TM1py session for ``instance``; ssl follows the base URL's scheme (TM1py wants ssl=False for http)."""
    scheme = urlparse(instance.base).scheme.lower()
    if scheme not in ("http", "https"):
        raise ConfigError(f"instance '{instance.name}': base must be an http:// or https:// URL (got {instance.base})")
    return TM1Service(base_url=instance.base, user=instance.user, password=instance.password, ssl=scheme == "https")


def _report_capture_progress(kind: str, message: str) -> None:
    """Render one capture-progress line to stderr (stdout stays the summary channel)."""
    print(f"capture {kind}: {message}", file=sys.stderr)


def _report_build_stage(stage: str) -> None:
    """Render one build-stage line to stderr."""
    print(f"build stage: {stage}", file=sys.stderr)


def _print_summary(result: JobResult) -> None:
    """Print the final report to stdout — the only thing the success path writes there."""
    attached = "yes" if result.attached else "no"
    print(f"job {result.job_id} {result.status} (instance: {result.instance}, attached: {attached})")
    if result.path:
        print(f"path: {result.path}")
    if result.nodes is not None:
        print(f"nodes: {result.nodes}")
    if result.edges is not None:
        print(f"edges: {result.edges}")
    if result.unchanged is not None:
        print(f"unchanged: {result.unchanged}")


def _run_list(args: argparse.Namespace) -> int:
    """Print the registry's instance sections — one ``name: base`` line each."""
    _, registry = _open_registry(args.credentials)
    for name, base in list_instances(registry):
        print(f"{name}: {base}" if base else name)
    return 0


def _one_line(message: str) -> str:
    """Collapse an exception's text to the one line the non-debug failure path prints."""
    return " ".join(message.split())
