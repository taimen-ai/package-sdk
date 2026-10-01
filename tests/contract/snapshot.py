"""Refresh the core OpenAPI slices used by the contract tests.

    python tests/contract/snapshot.py <full-openapi.json> <control-plane commit>
    python tests/contract/snapshot.py --install <full-openapi.json> <control-plane commit>

The full document of the pinned core is large; the catalog slice keeps ``info``, the
request schemas of catalog kinds and the schemas the connector runtime writes and
reads, with every schema they reference. The install slice (``--install``) keeps the
routes the installation plan reads and writes (``INSTALL_PATHS``) with their schemas.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
SLICE = HERE / "control-plane-openapi.catalog.json"
INSTALL_SLICE = HERE / "control-plane-openapi.install.json"
# Маршруты единого плана установки (package_sdk.install): план и применение ядра, вывод
# процессов и календарей, онтологии и их наборы, объекты стенда для переменных установки.
INSTALL_PATHS = {
    "/api/v1/packages:plan": ("post",),
    "/api/v1/packages:apply": ("post",),
    "/api/v1/process-definitions/{ref}": ("get",),
    "/api/v1/process-definitions/{key}:retire": ("post",),
    "/api/v1/calendars/{ref}": ("get",),
    "/api/v1/calendars/{key}:retire": ("post",),
    "/api/v1/knowledge/packs": ("post",),
    "/api/v1/knowledge/packs/{ref}": ("get",),
    "/api/v1/workspaces/{workspace_id}/knowledge-packs": ("get", "put"),
    "/api/v1/workspaces/{workspace_id}": ("get",),
    "/api/v1/projects/{project_id}": ("get",),
    "/api/v1/principals/{principal_id}": ("get",),
    "/api/v1/roles/{role_id}": ("get",),
}
ROOTS = [
    "TaskTypeCreateRequest",
    "ArtifactTypeCreateRequest",
    "ProjectTemplateCreateRequest",
    "WorkspaceTypeCreateRequest",
    "RoleCreateRequest",
    "CapabilityCreateRequest",
    "SkillRegisterRequest",
    "RuleCreateRequest",
    "AgentPublishRequest",
    "KnowledgePackRegisterRequest",
    # среда наблюдателя (package_sdk.connector): что он пишет и что читает о себе
    "ObservationCreateRequest",
    "KnowledgeSnapshotRequest",
    "ArtifactCreateRequest",
    "AgentOut",
]


def refs(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            found |= refs(value)
    elif isinstance(node, list):
        for value in node:
            found |= refs(value)
    return found


def _closure(schemas: dict[str, Any], roots: list[str]) -> set[str]:
    keep: set[str] = set()
    queue = list(roots)
    while queue:
        name = queue.pop()
        if name in keep:
            continue
        keep.add(name)
        queue.extend(refs(schemas[name]) - keep)
    return keep


def build_install(full: dict[str, Any], source: str) -> dict[str, Any]:
    paths = {
        path: {method: full["paths"][path][method] for method in methods}
        for path, methods in INSTALL_PATHS.items()
    }
    schemas = full["components"]["schemas"]
    keep = _closure(schemas, sorted(refs(paths)))
    return {
        "source": source,
        "info": full["info"],
        "paths": paths,
        "components": {"schemas": {name: schemas[name] for name in sorted(keep)}},
    }


def build(full: dict[str, Any], source: str) -> dict[str, Any]:
    schemas = full["components"]["schemas"]
    keep: set[str] = set()
    queue = list(ROOTS)
    while queue:
        name = queue.pop()
        if name in keep:
            continue
        keep.add(name)
        queue.extend(refs(schemas[name]) - keep)
    return {
        "source": source,
        "info": full["info"],
        "components": {"schemas": {name: schemas[name] for name in sorted(keep)}},
    }


if __name__ == "__main__":
    arguments = sys.argv[1:]
    install = arguments[:1] == ["--install"]
    if install:
        arguments = arguments[1:]
    document = json.loads(Path(arguments[0]).read_text(encoding="utf-8"))
    target = INSTALL_SLICE if install else SLICE
    sliced = (build_install if install else build)(document, arguments[1])
    target.write_text(
        json.dumps(sliced, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"{target.name}: core {document['info']['version']}")
