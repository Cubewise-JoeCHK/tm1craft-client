"""The ``tm1craft-client`` command line: dump a TM1 instance to a tm1craft service.

Two commands. ``dump <instance>`` resolves the instance's connection from
the ADR-0003 INI credentials registry (:mod:`tm1craft_client.config`),
captures the four model-object kinds read-only with TM1py
(:func:`tm1craft_client.capture.capture_bundle`) and hands the bundle to a
tm1craft arc-service model-dump job
(:func:`tm1craft_client.jobclient.upload_bundle`) — the client never
builds a database, staging and graph building are the service's business.
``list`` prints the registry's instance sections.

Channel discipline: progress and build stages render on **stderr**; the
final summary (job id, status, server-reported counts) goes to **stdout**
— so ``tm1craft-client dump prod 2>capture.log`` pipes a clean report.
Exit codes: ``0`` success, ``1`` capture/upload/service failure (the
underlying error's message, one line, redacted — a traceback only under
``--debug``), ``2`` usage/config problems (missing INI, unknown instance,
missing ``--service``). The password is never printed and argv is never
logged.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import sys
from urllib.parse import urlparse

from TM1py import TM1Service

from tm1craft_client.capture import capture_bundle
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
    commands = parser.add_subparsers(dest="command", required=True, metavar="{dump,list}")

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
    dump.set_defaults(handler=_run_dump)

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
    """The dump pipeline: resolve config, capture with TM1py, upload, report on stdout."""
    _, registry = _open_registry(args.credentials)
    instance = resolve_instance(
        registry,
        args.instance,
        base=args.base,
        user=args.user,
        password=args.password,
    )
    service_url = resolve_service_url(registry, args.service)
    license_key = args.license_key or os.environ.get(LICENSE_KEY_ENV_VAR) or None
    try:
        tm1 = _connect_tm1(instance)
        try:
            bundle = capture_bundle(tm1, instance.name, on_progress=_report_capture_progress)
        finally:
            tm1.logout()
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
