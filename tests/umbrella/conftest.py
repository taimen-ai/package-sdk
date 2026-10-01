"""Tests of the moved tools on a tree of packages and installations: by default the
anonymised snapshot in ``tests/fixtures/umbrella``, or the live umbrella given by
``PACKAGE_SDK_UMBRELLA``."""

from __future__ import annotations

import pytest

from tests.umbrella._shim import UMBRELLA, cp


@pytest.fixture(autouse=True)
def umbrella_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """The legacy tool was rooted at the umbrella; the SDK roots at the working directory."""
    monkeypatch.setattr(cp, "ROOT", UMBRELLA)
    monkeypatch.setattr(cp, "PACKAGES_DIR", UMBRELLA / "packages")
