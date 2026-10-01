"""Contract: what the installation plan sends to the core and reads from it (S013).

The slice ``control-plane-openapi.install.json`` is cut from the OpenAPI of the core
with the routes of the plan (``snapshot.py --install``). Every body the SDK sends
validates against the core's request schema, every field it reads is in the core's
response schema, and the kinds the core plans are the kinds the SDK leaves to it — an
invented contract of a neighbour is refused (constitution, article V).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from package_sdk.core import CORE_PLANNED_KINDS
from package_sdk.install.plan import RETIRE_REASON, VARIABLE_LOOKUP
from package_sdk.model import API, PLAN_KINDS
from package_sdk.source import plan_request

SLICE = json.loads(
    (Path(__file__).parent / "control-plane-openapi.install.json").read_text(encoding="utf-8")
)
SCHEMAS: dict[str, Any] = SLICE["components"]["schemas"]
REGISTRY = Registry().with_resource("urn:core", DRAFT202012.create_resource(SLICE))


def _schema(name: str) -> dict[str, Any]:
    return SCHEMAS[name]


def _valid(name: str, body: Any) -> None:
    validator = jsonschema.Draft202012Validator(
        {"$ref": f"urn:core#/components/schemas/{name}"}, registry=REGISTRY
    )
    errors = sorted(validator.iter_errors(body), key=str)
    assert errors == [], [e.message for e in errors]


def _operation(path: str, method: str) -> dict[str, Any]:
    operation: dict[str, Any] = SLICE["paths"][API + path][method]
    return operation


def _request(path: str, method: str) -> str:
    ref = _operation(path, method)["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    return str(ref).rsplit("/", 1)[-1]


def _response(path: str, method: str) -> str:
    content = _operation(path, method)["responses"]["200"]["content"]["application/json"]
    return str(content["schema"]["$ref"]).rsplit("/", 1)[-1]


def test_the_core_plans_exactly_the_kinds_the_sdk_leaves_to_it() -> None:
    kinds = _schema("PlanChangeOut")["properties"]["kind"]["enum"]
    assert tuple(kinds) == CORE_PLANNED_KINDS
    assert set(PLAN_KINDS) <= set(CORE_PLANNED_KINDS)


def test_plan_and_apply_bodies_are_the_core_requests(tmp_path: Path) -> None:
    from package_sdk.model import load_package

    directory = tmp_path / "demo"
    directory.mkdir()
    (directory / "package.yaml").write_text(
        "apiVersion: taimen.ai/v1\nkind: Package\nkey: demo\nspec: {version: 0.1.0}\n",
        encoding="utf-8",
    )
    request = plan_request(
        load_package(directory), {}, workspace="11111111-2222-4333-8444-555555555555"
    )
    assert _request("/packages:plan", "post") == "PackagePlanRequest"
    _valid("PackagePlanRequest", request)
    apply_body = {
        "package": request["package"],
        "planHash": "sha256:" + "a" * 64,
        "workspaceId": request["workspaceId"],
    }
    assert _request("/packages:apply", "post") == "PackageApplyRequest"
    _valid("PackageApplyRequest", apply_body)
    assert "overwriteConsole" not in request  # без флага — тело прежнее
    # флаг перезаписи правок консоли: и в плане, и в применении
    overwriting = plan_request(load_package(directory), {}, overwrite_console=True)
    assert overwriting["overwriteConsole"] is True
    _valid("PackagePlanRequest", overwriting)
    _valid("PackageApplyRequest", {**apply_body, "overwriteConsole": True})
    # что план читает из ответа ядра
    answer = _schema(_response("/packages:plan", "post"))["properties"]
    assert {"planHash", "changes", "processes", "problems"} <= set(answer)
    assert {"action", "deprecates", "fields"} <= set(_schema("PlanChangeOut")["properties"])
    field = _schema("PlanFieldOut")["properties"]
    assert {"path", "owner", "applies"} <= set(field)
    assert "console" in field["owner"]["enum"]


@pytest.mark.parametrize("collection", ["process-definitions", "calendars"])
def test_retire_has_a_dry_run_and_takes_a_reason(collection: str) -> None:
    operation = _operation(f"/{collection}/{{key}}:retire", "post")
    assert "dryRun" in [p["name"] for p in operation["parameters"] if p["in"] == "query"]
    _valid(_request(f"/{collection}/{{key}}:retire", "post"), {"reason": RETIRE_REASON})
    reading = _schema(_response(f"/{collection}/{{ref}}", "get"))["properties"]
    assert "status" in reading
    if collection == "process-definitions":
        retired = _schema(_response(f"/{collection}/{{key}}:retire", "post"))["properties"]
        assert {"openInstances", "byVersion"} <= set(retired)


def test_ontologies_are_read_registered_and_enabled_as_the_plan_does() -> None:
    assert "get" in SLICE["paths"][API + "/knowledge/packs/{ref}"]
    assert "post" in SLICE["paths"][API + "/knowledge/packs"]
    workspace = "/workspaces/{workspace_id}/knowledge-packs"
    _valid(_request(workspace, "put"), {"packs": ["company@1"], "strict": False})
    current = _schema(_response(workspace, "get"))["properties"]
    assert {"configured", "packs", "strict"} <= set(current)
    pack = _schema(_response("/knowledge/packs/{ref}", "get"))["properties"]
    assert {"kinds", "relations"} <= set(pack)


@pytest.mark.parametrize("kind", sorted(VARIABLE_LOOKUP))
def test_objects_named_by_variables_can_be_read(kind: str) -> None:
    template = VARIABLE_LOOKUP[kind]
    matches = [
        path
        for path in SLICE["paths"]
        if path.startswith(API + template.split("{", 1)[0]) and path.count("/") == 4
    ]
    assert matches and all("get" in SLICE["paths"][path] for path in matches)
