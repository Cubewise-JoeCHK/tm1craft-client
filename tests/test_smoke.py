"""Smoke test: the package imports and carries its version."""

import tm1craft_client


def test_package_imports_with_version():
    assert isinstance(tm1craft_client.__version__, str)
    assert tm1craft_client.__version__ == "0.1.0"
