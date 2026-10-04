"""Machine schema of the package format, shipped with the SDK (TAI-ADR-0062 п.3).

The schema files live in ``schema/v1/`` of the component and are packaged into the
wheel as ``package_sdk/schemas/v1``. Cross-file references resolve by ``$id`` through
one registry, so an editor that resolves them by path and a validator agree.

``view.schema.json`` — the spec of the kinds ``View`` and ``Component`` (CP-ADR-0080) — is
a byte copy of the core's ``control_plane/domain/view.schema.json``: it has no ``$id`` and
only ``$defs``; :func:`screen_validator` validates a spec against one of them.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
from referencing import Registry, Resource

VERSION = "v1"
OBJECT = "object"
TEST = "test"
PLAN = "plan"
LOCK = "lock"
KNOWLEDGE_PACK = "knowledge-pack"
KNOWLEDGE_TEMPLATE = "knowledge-template"
VIEW = "view"
# Kinds whose spec the view schema describes, and the definition of each.
SCREEN_DEFINITIONS = {"View": "viewSpec", "Component": "componentSpec"}


def schema_dir() -> Path:
    """Directory with the ``*.schema.json`` files: in the installed wheel or the source tree."""
    installed = Path(__file__).parent / "schemas" / VERSION
    if installed.is_dir():
        return installed
    source = Path(__file__).resolve().parents[2] / "schema" / VERSION
    if source.is_dir():
        return source
    raise FileNotFoundError("package format schema not found next to package_sdk")


@cache
def load(name: str) -> dict[str, Any]:
    """One schema document by its short name (``object``, ``test``, ``plan``, …)."""
    document: dict[str, Any] = json.loads(
        (schema_dir() / f"{name}.schema.json").read_text(encoding="utf-8")
    )
    return document


@cache
def registry() -> Registry:
    resources = []
    for path in sorted(schema_dir().glob("*.schema.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if "$id" in document:  # the core's byte copies carry none and refer to nothing
            resources.append((document["$id"], Resource.from_contents(document)))
    return Registry().with_resources(resources)


def validator(name: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(
        load(name),
        registry=registry(),
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )


@cache
def screen_validator(kind: str) -> jsonschema.Draft202012Validator:
    """Validator of the spec of a ``View`` or a ``Component`` (``SCREEN_DEFINITIONS``), as the
    core builds it (``views._validator``)."""
    definitions = load(VIEW)["$defs"]
    return jsonschema.Draft202012Validator(
        {"$defs": definitions, "$ref": f"#/$defs/{SCREEN_DEFINITIONS[kind]}"}
    )


def errors(name: str, document: Any) -> list[str]:
    """Human-readable violations of ``document`` against schema ``name``, stable order."""
    found = validator(name).iter_errors(document)
    return sorted(
        f"{'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
        for error in found
    )
