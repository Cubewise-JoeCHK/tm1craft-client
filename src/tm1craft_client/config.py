"""Resolve the CLI's config from the ADR-0003 INI credentials registry.

One INI file names every TM1 instance the client may dump (one
``[<instance name>]`` section each, keys ``base``/``user``/``password`` —
the registry format of the tm1craft arc service's credentials doctrine,
``docs/adr/0003-tm1-credentials-registry.md``) plus the optional
``[service]`` section with the ``upload_url`` of a tm1craft service. The
file is read per invocation — no cache, no writes; its permissions are
the operator's duty.

The security rules that bind the service's registry bind this module
too: the secret never renders (:class:`InstanceConfig` is secret-blind —
``__repr__``/``__str__`` show ``***``) and :func:`redact` scrubs a
resolved password out of error-path text before the CLI prints it. An
empty password is legal here (TM1 servers accept one for a configured
user) where the service's resolver requires one — the client connects
to the instance it is told to, not to its own deployment.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass

DEFAULT_CREDENTIALS_PATHS = ("./tm1-client.ini", "~/.tm1-client.ini")
# Where --credentials looks when it is not given, in order.

SERVICE_SECTION = "service"
# The one registry section that is not a TM1 instance.

UPLOAD_URL_KEY = "upload_url"
# The [service] key carrying the tm1craft service's base URL.

_REDACTED = "***"


class ConfigError(Exception):
    """A usage/config problem: missing INI, unknown instance, missing key — exit code 2."""


@dataclass(frozen=True, repr=False)
class InstanceConfig:
    """One TM1 instance's REST endpoint and credentials — secret-blind.

    The password is the only field the rendered forms withhold:
    ``__repr__`` (and therefore ``__str__``) show ``***`` in its place,
    so the instance is safe to interpolate into an error message.
    """

    name: str
    base: str
    user: str
    password: str = ""

    def __repr__(self) -> str:
        """Render the connection with the password masked."""
        return f"InstanceConfig(name={self.name!r}, base={self.base!r}, user={self.user!r}, password={_REDACTED!r})"


def find_credentials_file(explicit: str | None) -> str:
    """Return the credentials INI path: ``explicit`` when given, else the defaults in order.

    An explicit ``--credentials`` path must exist. Without one,
    :data:`DEFAULT_CREDENTIALS_PATHS` is tried in order — first hit wins.
    Any miss raises :class:`ConfigError` (never falls through silently).
    """
    if explicit:
        resolved = os.path.abspath(os.path.expanduser(explicit))
        if not os.path.isfile(resolved):
            raise ConfigError(f"credentials file not found: {explicit}")
        return resolved
    for candidate in DEFAULT_CREDENTIALS_PATHS:
        resolved = os.path.abspath(os.path.expanduser(candidate))
        if os.path.isfile(resolved):
            return resolved
    order = " then ".join(DEFAULT_CREDENTIALS_PATHS)
    raise ConfigError(f"no credentials file found (tried {order}) — pass --credentials <path>")


def load_registry(path: str) -> configparser.ConfigParser:
    """Parse the credentials INI at ``path``; a miss is a :class:`ConfigError`."""
    registry = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, encoding="utf-8") as handle:
            registry.read_file(handle)
    except (OSError, UnicodeDecodeError, configparser.Error) as problem:
        raise ConfigError(f"cannot read credentials file {path}: {problem}") from problem
    return registry


def resolve_instance(
    registry: configparser.ConfigParser,
    instance: str,
    *,
    base: str | None = None,
    user: str | None = None,
    password: str | None = None,
) -> InstanceConfig:
    """Resolve one instance from the registry; the ``base``/``user``/``password`` flags win when given.

    The section must match ``instance`` exactly (configparser never
    casefolds section names — the ADR-0003 rule). ``base`` and ``user``
    are required (from INI or flag); ``password`` may be empty. Every
    error message names the instance and the fix — none carries secret
    material.
    """
    if not registry.has_section(instance):
        known = ", ".join(name for name, _ in list_instances(registry)) or "none"
        raise ConfigError(
            f"unknown instance '{instance}' — the registry has no [{instance}] section (known instances: {known})"
        )
    section = registry[instance]
    resolved_base = base if base is not None else section.get("base", "")
    resolved_user = user if user is not None else section.get("user", "")
    resolved_password = password if password is not None else section.get("password", "")
    if not resolved_base:
        raise ConfigError(f"instance '{instance}' has no base URL — set base in [{instance}] or pass --base")
    if not resolved_user:
        raise ConfigError(f"instance '{instance}' has no user — set user in [{instance}] or pass --user")
    return InstanceConfig(name=instance, base=resolved_base, user=resolved_user, password=resolved_password)


def resolve_service_url(registry: configparser.ConfigParser, flag: str | None) -> str:
    """Return the tm1craft service's URL: ``flag`` wins, else the registry's ``[service] upload_url``."""
    if flag:
        return flag
    upload_url = registry.get(SERVICE_SECTION, UPLOAD_URL_KEY, fallback="").strip()
    if upload_url:
        return upload_url
    raise ConfigError("no service URL — pass --service <url> or set upload_url in the registry's [service] section")


def list_instances(registry: configparser.ConfigParser) -> list[tuple[str, str]]:
    """The registry's instance sections as ``(name, base)`` pairs, in file order; ``[service]`` is not an instance."""
    return [(name, registry[name].get("base", "")) for name in registry.sections() if name != SERVICE_SECTION]


def redact(text: str, secret: str) -> str:
    """Scrub every occurrence of ``secret`` from ``text`` — the error-path complement of the blind repr."""
    return text.replace(secret, _REDACTED) if secret else text
