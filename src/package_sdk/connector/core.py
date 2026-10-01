"""Вызовы ядра, которыми пользуется наблюдатель (control-plane-client, ADR-0030)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol


class Core(Protocol):
    """Часть API ядра для наблюдателя: наблюдения, снимки знаний, артефакты, своя ревизия."""

    async def remember(self, **kwargs: Any) -> Any: ...

    async def submit_knowledge_snapshot(
        self, *, workspace_id: str, snapshot: Mapping[str, Any]
    ) -> Any: ...

    async def upload_artifact_content(self, source: bytes, *, media_type: str) -> Any: ...

    async def create_artifact(self, **kwargs: Any) -> Any: ...

    async def get_my_agent(self) -> Mapping[str, Any] | None: ...


class ClientCore:
    """Ядро через ControlPlaneClient: одна сессия клиента на вызов — наблюдатель не
    держит цикл событий между циклами (extra ``connector``)."""

    def __init__(self, url: str, *, client: Callable[[], Any] | None = None) -> None:
        self.url = url
        self._client = client

    def _new_client(self) -> Any:
        if self._client is not None:
            return self._client()
        from control_plane_client.client import ControlPlaneClient
        from control_plane_client.credentials import resolve_credential

        return ControlPlaneClient(self.url, resolve_credential(self.url))

    async def _call(self, method: str, **kwargs: Any) -> Any:
        async with self._new_client() as client:
            return await getattr(client, method)(**kwargs)

    async def remember(self, **kwargs: Any) -> Any:
        return await self._call("remember", **kwargs)

    async def submit_knowledge_snapshot(
        self, *, workspace_id: str, snapshot: Mapping[str, Any]
    ) -> Any:
        return await self._call(
            "submit_knowledge_snapshot", workspace_id=workspace_id, snapshot=dict(snapshot)
        )

    async def upload_artifact_content(self, source: bytes, *, media_type: str) -> Any:
        return await self._call("upload_artifact_content", source=source, media_type=media_type)

    async def create_artifact(self, **kwargs: Any) -> Any:
        return await self._call("create_artifact", **kwargs)

    async def get_my_agent(self) -> Mapping[str, Any] | None:
        from control_plane_client import NotFoundError

        try:
            body: Mapping[str, Any] = await self._call("get_my_agent")
        except NotFoundError:
            return None
        return body
