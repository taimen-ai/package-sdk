"""Тесты наблюдателя без стенда: поддельное ядро и один цикл (plan Р8).

from package_sdk.connector.testing import run_once

result = run_once(observe, config={"baseUrl": "…"}, secrets={"x-token": "t"})
assert [o["kind"] for o in result.observations] == ["x.item_changed"]
"""

from __future__ import annotations

import asyncio
import copy
import datetime
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from package_sdk.connector.runtime import EXECUTOR_KIND, Registered, Runner


class FakeCoreError(Exception):
    """Отказ поддельного ядра с кодом HTTP, как ошибки control-plane-client (``status``)."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"HTTP {status}: {code}")
        self.status = status
        self.code = code


class FakeCore:
    """Ядро в памяти по его контракту: повтор (source, dedupKey) наблюдения — дубль;
    ключ идемпотентности артефакта ищется по (tenant, key) для всех principal, другое тело
    или другой principal — 409; загрузка содержимого живёт ``upload_ttl``, потом 422
    content_ref_not_found. ``fail_after`` — сколько записей пройдёт до отказа (сбой
    публикации); ``lose_next_response`` — следующий create_artifact закоммитится, а ответ
    потеряется."""

    def __init__(self, agent: Mapping[str, Any] | None = None) -> None:
        self.agent: Mapping[str, Any] | None = agent
        self.observations: list[dict[str, Any]] = []
        self.snapshots: list[dict[str, Any]] = []
        self.artifacts: list[dict[str, Any]] = []
        self.contents: dict[str, bytes] = {}
        self.fail_after: int | None = None
        self._writes = 0
        #: часы ядра и срок жизни загрузки (artifact_upload_ttl_seconds)
        self.now: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.UTC)
        self.upload_ttl = datetime.timedelta(hours=24)
        self._expires: dict[str, datetime.datetime] = {}
        #: кто пишет — ключ идемпотентности у ядра общий для всех principal tenant'а
        self.principal = "principal-1"
        self.lose_next_response = False

    def _write(self) -> None:
        if self.fail_after is not None and self._writes >= self.fail_after:
            raise ConnectionError("core unavailable")
        self._writes += 1

    async def remember(self, **kwargs: Any) -> Any:
        self._write()
        for existing in self.observations:
            if (existing.get("source"), existing.get("dedup_key")) == (
                kwargs.get("source"),
                kwargs.get("dedup_key"),
            ):
                return {**existing, "deduplicated": True}
        record = {"id": str(uuid.uuid4()), **copy.deepcopy(kwargs)}
        self.observations.append(record)
        return record

    async def submit_knowledge_snapshot(
        self, *, workspace_id: str, snapshot: Mapping[str, Any]
    ) -> Any:
        self._write()
        self.snapshots.append({"workspaceId": workspace_id, **copy.deepcopy(dict(snapshot))})
        return {"duplicate": False}

    async def upload_artifact_content(self, source: bytes, *, media_type: str) -> Any:
        self._write()
        ref = f"content-{uuid.uuid4()}"
        self.contents[ref] = source
        self._expires[ref] = self.now() + self.upload_ttl
        return {
            "contentRef": ref,
            "mediaType": media_type,
            "sizeBytes": len(source),
            "expiresAt": self._expires[ref].isoformat(),
        }

    async def create_artifact(self, **kwargs: Any) -> Any:
        self._write()
        key = kwargs.get("idempotency_key")
        if key is not None and len(key) > 200:
            raise FakeCoreError(400, "invalid_idempotency_key")
        for existing in self.artifacts:
            if key is not None and existing.get("idempotency_key") == key:
                if existing["request"] != kwargs or existing["principal"] != self.principal:
                    raise FakeCoreError(409, "idempotency_key_reused")
                return existing
        ref = kwargs.get("content_ref")
        if ref is not None and (ref not in self._expires or self._expires[ref] <= self.now()):
            raise FakeCoreError(422, "content_ref_not_found")
        record = {
            "id": str(uuid.uuid4()),
            **copy.deepcopy(kwargs),
            "request": copy.deepcopy(kwargs),
            "principal": self.principal,
        }
        record["content"] = self.contents.get(str(ref))
        self.artifacts.append(record)
        if self.lose_next_response:
            self.lose_next_response = False
            raise ConnectionError("response lost after commit")
        return record

    async def get_my_agent(self) -> Mapping[str, Any] | None:
        return copy.deepcopy(self.agent) if self.agent is not None else None


def agent_body(
    function: Registered,
    *,
    config: Mapping[str, Any] | None = None,
    key: str = "observer-under-test",
    revision: int = 1,
    workspace_id: str | None = "00000000-0000-4000-8000-000000000001",
    state: str = "running",
    status: str = "active",
    interval: int = 60,
    principal_id: str | None = None,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Ответ ``GET /agents/me`` для наблюдателя под тестом. ``params`` — params
    собственного вида исполнителя (``git-connector``): тогда ``config`` не нужен."""
    if function.executor == EXECUTOR_KIND:
        executor_params: dict[str, Any] = {
            "entrypoint": function.entrypoint,
            "intervalSeconds": interval,
            "config": dict(config or {}),
            **dict(params or {}),
        }
    else:
        executor_params = {"intervalSeconds": interval, **dict(params or {})}
    return {
        "key": key,
        "principalId": principal_id,
        "status": status,
        "state": state,
        "workspaceId": workspace_id,
        "revision": {
            "id": f"revision-{revision}",
            "revision": revision,
            "spec": {"executor": {"kind": function.executor, "params": executor_params}},
        },
    }


@dataclass
class Result:
    """Итог одного цикла: что ушло в ядро и каким стало состояние."""

    observations: list[dict[str, Any]]
    snapshots: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    state: dict[str, Any]
    published: list[str] = field(default_factory=list)


def run_once(
    function: Registered,
    *,
    config: Mapping[str, Any] | None = None,
    secrets: Mapping[str, str] | None = None,
    state: Mapping[str, Any] | None = None,
    core: FakeCore | None = None,
    data_dir: Path | None = None,
    params: Mapping[str, Any] | None = None,
) -> Result:
    """Один цикл наблюдателя на поддельном ядре: секреты — файлы во временном каталоге."""
    core = core or FakeCore()
    if core.agent is None:
        core.agent = agent_body(function, config=config, params=params)
    with tempfile.TemporaryDirectory() as tmp:
        secrets_dir = Path(tmp) / "secrets"
        secrets_dir.mkdir()
        for name, value in (secrets or {}).items():
            (secrets_dir / name).write_text(value, encoding="utf-8")
        data = data_dir or Path(tmp) / "data"
        runner = Runner(
            function, core, data_dir=data, secrets_dir=secrets_dir, run_async=asyncio.run
        )
        if state is not None:
            runner._save({"state": dict(state)})
        revision = runner.revision()
        if revision is None:
            raise AssertionError("FakeCore.agent is None: the principal is not an agent")
        published = runner.cycle(revision)
        stored = runner._load().get("state") or {}
    return Result(
        observations=core.observations,
        snapshots=core.snapshots,
        artifacts=core.artifacts,
        state=dict(stored),
        published=published,
    )
