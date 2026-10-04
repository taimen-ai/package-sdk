"""Contract: the ``spec`` of every catalog kind is the request body of the pinned core.

TAI-ADR-0044 п.2 — ``spec`` is exactly the core API body, without a translation layer.
A field the schema accepts but the core does not know is an invented contract
(constitution, article V). Fields that the SDK leads by design and the core adds in a
tracked task are listed in ``PENDING`` with that task.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from package_sdk import schema

SLICE = json.loads(
    (Path(__file__).parent / "control-plane-openapi.catalog.json").read_text(encoding="utf-8")
)
COMPONENTS: dict[str, Any] = SLICE["components"]["schemas"]

# kind → ($defs of the object schema, request schema of the core, identity field)
KINDS = {
    "TaskType": ("taskTypeSpec", "TaskTypeCreateRequest"),
    "ArtifactType": ("artifactTypeSpec", "ArtifactTypeCreateRequest"),
    "ProjectTemplate": ("projectTemplateSpec", "ProjectTemplateCreateRequest"),
    "WorkspaceType": ("workspaceTypeSpec", "WorkspaceTypeCreateRequest"),
    "Role": ("roleSpec", "RoleCreateRequest"),
    "Capability": ("capabilitySpec", "CapabilityCreateRequest"),
    "Skill": ("skillSpec", "SkillRegisterRequest"),
    "WorkRule": ("workRuleSpec", "RuleCreateRequest"),
    "Agent": ("agentSpec", "AgentSpec"),
}
# (kind, dotted path) → task in which the core accepts the field
PENDING = {
    ("Agent", "executor.image"): "TASK-000940 (S016, CP-ADR-0073 amendment)",
    # the core accepts it on its branch feature/integrations-connections (CP-ADR-0079 §8)
    ("Agent", "connections"): "TASK-001146 (CP-ADR-0079 §8, feature integrations-connections)",
}
# kind → (definition, request schema of the core once the slice has it, task): the kind is in
# the format, the pinned core has no route for it yet. When the slice gains the request schema,
# the kind moves to KINDS.
PENDING_KINDS = {
    "ConnectionType": (
        "connectionTypeSpec",
        "ConnectionTypeSpec",
        "TASK-001146 (CP-ADR-0079 §2, feature integrations-connections)",
    ),
}


def component(node: dict[str, Any]) -> dict[str, Any]:
    """Follow ``$ref`` or a nullable ``anyOf`` to the referenced component."""
    if "$ref" in node:
        return COMPONENTS[node["$ref"].rsplit("/", 1)[-1]]
    for alternative in node.get("anyOf", []):
        if "$ref" in alternative:
            return COMPONENTS[alternative["$ref"].rsplit("/", 1)[-1]]
    return node


def unknown_fields(ours: dict[str, Any], core: dict[str, Any], path: str = "") -> list[str]:
    ours_props = ours.get("properties", {})
    core_props = component(core).get("properties", {})
    found = [f"{path}{name}" for name in ours_props if name not in core_props]
    return found


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_spec_fields_exist_in_core_request(kind: str) -> None:
    definition, request = KINDS[kind]
    ours = schema.load(schema.OBJECT)["$defs"][definition]
    core = COMPONENTS[request]
    missing = unknown_fields(ours, core)
    if kind == "Agent":
        executor_core = component(core["properties"]["executor"])
        missing += unknown_fields(ours["properties"]["executor"], executor_core, "executor.")
    pending = {path for (k, path) in PENDING if k == kind}
    assert sorted(set(missing) - pending) == []


def test_pending_fields_are_still_pending() -> None:
    # A pending field that the core has meanwhile accepted must leave the list.
    executor_core = component(COMPONENTS["AgentSpec"]["properties"]["executor"])
    assert "image" not in executor_core.get("properties", {}), (
        "the core accepts executor.image now: drop it from PENDING and refresh the slice"
    )
    assert "connections" not in COMPONENTS["AgentSpec"].get("properties", {}), (
        "the core accepts Agent.spec.connections now: drop it from PENDING"
    )


@pytest.mark.parametrize("kind", sorted(PENDING_KINDS))
def test_pending_kinds_are_still_pending(kind: str) -> None:
    definition, request, _task = PENDING_KINDS[kind]
    assert definition in schema.load(schema.OBJECT)["$defs"]
    assert request not in COMPONENTS, (
        f"the slice has {request} now: move {kind} from PENDING_KINDS to KINDS"
    )


def test_knowledge_pack_is_forwarded_as_is() -> None:
    request = COMPONENTS["KnowledgePackRegisterRequest"]
    assert request.get("additionalProperties") is True
    assert {"name", "scope", "version"} <= set(request["properties"])


def test_executor_roles_take_what_the_core_takes() -> None:
    # CP-ADR-0048 А1: up to 20 role slugs of the core's form, no repeats
    objects = schema.load(schema.OBJECT)["$defs"]
    ours = objects["taskTypeSpec"]["properties"]["executorRoles"]
    core = COMPONENTS["TaskTypeCreateRequest"]["properties"]["executorRoles"]
    slug = objects[ours["items"]["$ref"].rsplit("/", 1)[-1]]
    assert ours["maxItems"] == core["maxItems"]
    assert ours["uniqueItems"] is True
    for key in ("minLength", "maxLength", "pattern"):
        assert slug[key] == core["items"][key], key
