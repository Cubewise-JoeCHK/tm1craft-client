"""Tests for the ADR-0003 INI credentials registry loader (config)."""

import pytest

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


@pytest.fixture()
def registry_path(tmp_path):
    """The two-instance registry plus a [service] section, written to tmp."""
    path = tmp_path / "tm1-client.ini"
    path.write_text(REGISTRY_INI, encoding="utf-8")
    return str(path)


@pytest.fixture()
def registry(registry_path):
    """The parsed two-instance registry."""
    return load_registry(registry_path)


def write_ini(tmp_path, text):
    """Write one INI variant into tmp and return its path string."""
    path = tmp_path / "registry.ini"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_resolve_instance_reads_section(registry):
    instance = resolve_instance(registry, "prod")
    assert instance == InstanceConfig(
        name="prod", base="http://tm1-prod:12354/api/v1", user="admin", password="s3cret-prod"
    )


def test_resolve_instance_password_may_be_empty(registry):
    instance = resolve_instance(registry, "dev")
    assert instance.password == ""
    assert instance.user == "devuser"


def test_resolve_instance_unknown_section_raises_with_known_names(registry):
    with pytest.raises(ConfigError) as problem:
        resolve_instance(registry, "staging")
    message = str(problem.value)
    assert "staging" in message
    assert "prod" in message and "dev" in message


def test_resolve_instance_missing_base_raises(tmp_path):
    registry = load_registry(write_ini(tmp_path, "[prod]\nuser = admin\n"))
    with pytest.raises(ConfigError, match="--base"):
        resolve_instance(registry, "prod")


def test_resolve_instance_missing_user_raises(tmp_path):
    registry = load_registry(write_ini(tmp_path, "[prod]\nbase = http://tm1-prod:12354/api/v1\n"))
    with pytest.raises(ConfigError, match="--user"):
        resolve_instance(registry, "prod")


def test_flag_overrides_beat_ini_values(registry):
    instance = resolve_instance(
        registry,
        "prod",
        base="http://override:9999/api/v1",
        user="override-user",
        password="override-secret",
    )
    assert instance.base == "http://override:9999/api/v1"
    assert instance.user == "override-user"
    assert instance.password == "override-secret"


def test_flag_override_only_touches_the_flag_it_covers(registry):
    instance = resolve_instance(registry, "prod", password="one-off")
    assert instance.base == "http://tm1-prod:12354/api/v1"
    assert instance.user == "admin"
    assert instance.password == "one-off"


def test_service_url_flag_wins_over_ini(registry):
    assert resolve_service_url(registry, "http://flag-service.example.com") == "http://flag-service.example.com"


def test_service_url_from_ini_when_no_flag(registry):
    assert resolve_service_url(registry, None) == "http://craft-service.example.com"


def test_service_url_missing_everywhere_raises(tmp_path):
    registry = load_registry(write_ini(tmp_path, "[prod]\nbase = http://tm1-prod:12354/api/v1\nuser = admin\n"))
    with pytest.raises(ConfigError, match="--service"):
        resolve_service_url(registry, None)


def test_list_instances_returns_sections_in_order_without_service(registry):
    assert list_instances(registry) == [
        ("prod", "http://tm1-prod:12354/api/v1"),
        ("dev", "https://tm1-dev:12354/api/v1"),
    ]


def test_instance_rendering_never_shows_the_password():
    instance = InstanceConfig(name="prod", base="http://tm1:12354/api/v1", user="admin", password="s3cret-prod")
    assert "s3cret-prod" not in repr(instance)
    assert "s3cret-prod" not in str(instance)
    assert repr(instance) == str(instance)
    assert "***" in repr(instance)


def test_redact_scrubs_secret_from_error_text():
    assert redact("login failed for s3cret-prod at http://tm1", "s3cret-prod") == "login failed for *** at http://tm1"


def test_redact_without_secret_returns_text_unchanged():
    assert redact("nothing to hide", "") == "nothing to hide"


def test_config_error_messages_never_carry_the_password(tmp_path):
    registry = load_registry(write_ini(tmp_path, "[prod]\nbase = http://tm1:12354/api/v1\npassword = s3cret-prod\n"))
    with pytest.raises(ConfigError) as problem:
        resolve_instance(registry, "prod")
    assert "s3cret-prod" not in str(problem.value)


def test_load_registry_unparseable_raises(tmp_path):
    path = write_ini(tmp_path, "[prod\nbase = broken")
    with pytest.raises(ConfigError, match="cannot read"):
        load_registry(path)


def test_load_registry_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_registry(str(tmp_path / "absent.ini"))


def test_find_credentials_file_explicit(tmp_path):
    path = write_ini(tmp_path, REGISTRY_INI)
    assert find_credentials_file(path) == path


def test_find_credentials_file_explicit_missing_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        find_credentials_file(str(tmp_path / "absent.ini"))


def test_find_credentials_file_default_order_current_directory_first(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "tm1-client.ini").write_text("[cwd]\n", encoding="utf-8")
    (tmp_path / "home" / ".tm1-client.ini").write_text("[home]\n", encoding="utf-8")
    assert find_credentials_file(None) == str(tmp_path / "tm1-client.ini")


def test_find_credentials_file_default_order_falls_through_to_home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / ".tm1-client.ini").write_text("[home]\n", encoding="utf-8")
    assert find_credentials_file(None) == str(tmp_path / "home" / ".tm1-client.ini")


def test_find_credentials_file_no_default_hit_raises_with_the_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    with pytest.raises(ConfigError) as problem:
        find_credentials_file(None)
    message = str(problem.value)
    assert "./tm1-client.ini" in message and "~/.tm1-client.ini" in message
