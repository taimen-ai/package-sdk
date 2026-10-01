"""Every package and installation of the umbrella validates against this schema.

By default runs on the anonymised snapshot in ``tests/fixtures/umbrella``;
``PACKAGE_SDK_UMBRELLA`` points it to the live umbrella. Files are read as YAML 1.2,
as the installer reads them (``on``/``off`` stay strings).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from package_sdk import schema

UMBRELLA = Path(
    os.environ.get("PACKAGE_SDK_UMBRELLA")
    or Path(__file__).resolve().parent / "fixtures" / "umbrella"
)
PACKAGES = UMBRELLA / "packages"
SKIP_DIRS = {"schema", "schemas", ".layout"}

pytestmark = pytest.mark.skipif(not PACKAGES.is_dir(), reason="no umbrella packages next to it")


def read(path: Path) -> Any:
    return YAML(typ="safe", pure=True).load(path.read_text(encoding="utf-8"))


def package_files() -> list[Path]:
    if not PACKAGES.is_dir():
        return []
    return sorted(
        path
        for path in PACKAGES.rglob("*.yaml")
        if not SKIP_DIRS & set(path.relative_to(PACKAGES).parts)
    )


def installation_files() -> list[Path]:
    return sorted((UMBRELLA / "deploy").rglob("packages.yaml")) if PACKAGES.is_dir() else []


@pytest.mark.parametrize("path", package_files(), ids=lambda p: str(p.relative_to(PACKAGES)))
def test_package_file_is_valid(path: Path) -> None:
    name = schema.TEST if path.name.endswith(".test.yaml") else schema.OBJECT
    assert schema.errors(name, read(path)) == []


@pytest.mark.parametrize("path", installation_files(), ids=lambda p: str(p.relative_to(UMBRELLA)))
def test_installation_is_valid(path: Path) -> None:
    assert schema.errors(schema.OBJECT, read(path)) == []
