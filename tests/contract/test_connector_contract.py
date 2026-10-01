"""Контракт среды наблюдателя с ядром (S018): тела запросов, которые уходят через
control-plane-client, проходят схемы среза OpenAPI закреплённого ядра, а поля ответа
GET /agents/me, которые читает наблюдатель, в схеме есть."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

httpx = pytest.importorskip("httpx")
client_module = pytest.importorskip("control_plane_client.client")

from package_sdk.connector import Document, Observation, Snapshot  # noqa: E402
from package_sdk.connector.core import ClientCore  # noqa: E402
from package_sdk.connector.runtime import AgentRevision, ObserveContext  # noqa: E402

SLICE = json.loads(
    (Path(__file__).parent / "control-plane-openapi.catalog.json").read_text(encoding="utf-8")
)
WORKSPACE = "00000000-0000-4000-8000-000000000001"
BODIES = {
    "/api/v1/observations": "ObservationCreateRequest",
    "/api/v1/knowledge/snapshots": "KnowledgeSnapshotRequest",
    "/api/v1/artifacts": "ArtifactCreateRequest",
}


def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(
        {"$ref": f"#/components/schemas/{name}", "components": SLICE["components"]}
    )


def test_published_bodies_pass_the_core_schemas(tmp_path: Path) -> None:
    sent: list[tuple[str, Any]] = []

    def handler(request: Any) -> Any:
        body = (
            json.loads(request.content)
            if request.headers.get("content-type", "").startswith("application/json")
            else None
        )
        sent.append((request.url.path, body))
        if request.url.path == "/api/v1/artifact-contents":
            return httpx.Response(201, json={"contentRef": "ref-1", "sizeBytes": 5})
        return httpx.Response(201, json={"id": "x"})

    core = ClientCore(
        "https://core.example",
        client=lambda: client_module.ControlPlaneClient(
            "https://core.example", "test-key", transport=httpx.MockTransport(handler)
        ),
    )
    agent = AgentRevision("obs", 1, "rev-1", {}, workspace_id=WORKSPACE)
    ctx = ObserveContext(
        agent=agent,
        source="obs",
        state={},
        secrets_dir=tmp_path,
        core=core,
        run_async=asyncio.run,
    )
    ctx.emit(
        Observation(
            kind="x.seen",
            dedup_key="x:1",
            data={"a": 1},
            external_ref={"system": "x", "id": "1", "url": "https://x.example/1"},
            observed_at=dt.datetime(2026, 9, 30, tzinfo=dt.UTC),
        )
    )
    ctx.snapshot(Snapshot(source="x", snapshot_id="s1", pack="company@1", scope="all"))
    ctx.document(
        Document(
            type="file",
            name="a.txt",
            content=b"hello",
            media_type="text/plain",
            idempotency_key="obs:a",
        )
    )
    ctx.document(Document(type="file", name="b.txt", uri="https://x.example/b.txt"))
    ctx.emit(Observation(kind="x.seen", dedup_key="x:2", supersedes=WORKSPACE))

    checked = [path for path, _ in sent if path in BODIES]
    assert set(checked) == set(BODIES) and len(checked) == 5
    for path, body in sent:
        if path in BODIES:
            errors = [e.message for e in _validator(BODIES[path]).iter_errors(body)]
            assert errors == [], (path, body, errors)


def test_agents_me_fields_the_observer_reads_exist() -> None:
    schemas = SLICE["components"]["schemas"]
    agent = schemas["AgentOut"]["properties"]
    assert {"key", "status", "state", "workspaceId", "revision"} <= set(agent)
    revision = schemas["AgentRevisionOut"]["properties"]
    assert {"id", "revision", "spec"} <= set(revision)
    assert set(schemas["AgentOut"]["properties"]["status"].get("enum", ["active", "retired"])) >= {
        "active",
        "retired",
    }
