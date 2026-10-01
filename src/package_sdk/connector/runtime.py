"""Среда исполнения наблюдателя пакета (plan Р8, FR-018, FR-026).

Наблюдатель — функция автора интеграции, помеченная :func:`observer`. Узел fleet
запускает образ вида ``observer`` с окружением агента; :func:`run` читает ревизию
своего агента (``GET /agents/me``), сверяет вид и точку входа и крутит цикл:

- **цикл** — вызов функции с :class:`ObserveContext`. ``emit``, ``snapshot`` и
  ``document`` публикуют сразу и возвращают ответ ядра (id наблюдения для
  ``supersedes``, id артефакта для ссылки из наблюдения). ``ctx.state`` сохраняется
  только после цикла без ошибок: если публикация сорвалась (:class:`PublishError`),
  следующий цикл повторит работу, а повтор наблюдения ядро узнаёт по
  ``(source, dedupKey)``, артефакта — по ключу идемпотентности;
- **ревизия** — между циклами: новая ревизия → выход 75 (узел поднимет процесс на
  ней), агент остановлен или выведен → выход 0; вид не ``observer`` или чужая точка
  входа → выход 2 до первого цикла;
- **сбой цикла** — исключение функции пишется в журнал и наблюдением
  ``connector.cycle_failed`` (раз в час на наблюдателя), процесс не падает;
- **секреты** — только файлы секретов узла (``/run/secrets/<имя>``), читаются на
  каждый вызов ``ctx.secret`` по канону skill-sdk (``package_sdk.connector.secrets``);
  нет файла, он пуст или из одних пробелов — наблюдение ``connector.secret_missing``
  раз в сутки, цикл пропускается; файл отвергнут правилом (имя, ссылка наружу, подмена пути, не
  обычный файл, размер, кодировка, права) — :class:`SecretRejected`, сбой цикла
  ``connector.cycle_failed`` с кодом и причиной. Значение секрета не попадает ни в
  журнал, ни в наблюдения, ни в состояние.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from package_sdk.connector.core import Core
from package_sdk.connector.secrets import SecretRejected, read_secret_file

logger = logging.getLogger("package_sdk.connector")

EXECUTOR_KIND = "observer"
#: Ревизия агента сменилась — узел запускает процесс заново (EX_TEMPFAIL).
EXIT_REVISION_CHANGED = 75
#: Агент остановлен или выведен из оборота.
EXIT_STOPPED = 0
#: Описание нельзя исполнить этим образом: не агент, чужой вид или точка входа.
EXIT_MISCONFIGURED = 2

ENV_DATA_DIR = "CONNECTOR_DATA_DIR"
ENV_SECRETS_DIR = "CONNECTOR_SECRETS_DIR"
#: Точка входа образа для вида исполнителя без params.entrypoint (git-connector).
ENV_ENTRYPOINT = "CONNECTOR_ENTRYPOINT"
DEFAULT_DATA_DIR = "/data"
DEFAULT_SECRETS_DIR = "/run/secrets"
STATE_FILE = "connector-state.json"
DEFAULT_INTERVAL = 900

CYCLE_FAILED = "connector.cycle_failed"
SECRET_MISSING = "connector.secret_missing"
# Служебные отметки наблюдателя в файле состояния (не в ctx.state автора).
_MARKS = "__connector__"


class PublishError(Exception):
    """Запись цикла не дошла до ядра (сеть, 5xx, 408, 429): цикл прерван, state не
    сохраняется, следующий цикл повторит. Отказ ядра по сути (прочие 4xx) — сбой цикла:
    он пишется в connector.cycle_failed, а не крутится молча."""


# Коды, при которых повтор имеет смысл: запись не дошла или ядро просит подождать.
_RETRYABLE = frozenset({0, 408, 425, 429, 500, 502, 503, 504})
# Коды ядра, при которых тоже имеет смысл повтор: запись с тем же ключом ещё идёт.
_RETRYABLE_CODES = frozenset({"idempotency_in_flight"})
# Загрузку содержимого, которой осталось жить меньше этого, не переиспользуют.
_UPLOAD_MARGIN = dt.timedelta(minutes=10)


class SecretMissing(Exception):
    """Секрета узла нет, он пуст или из одних пробелов; имя — в args[0], значения нет."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


@dataclass(frozen=True)
class Observation:
    """Наблюдение внешней системы (POST /observations, CP-ADR-0057)."""

    kind: str
    dedup_key: str
    data: Mapping[str, Any] | None = None
    content: str | None = None
    external_ref: Mapping[str, Any] | None = None
    observed_at: str | dt.datetime | None = None
    source: str | None = None
    #: id наблюдения, которое это заменяет (новая версия того же факта)
    supersedes: str | None = None


@dataclass(frozen=True)
class Snapshot:
    """Снимок знаний источника (POST /knowledge/snapshots, CP-ADR-0060)."""

    source: str
    snapshot_id: str
    entities: list[Mapping[str, Any]] = field(default_factory=list)
    relations: list[Mapping[str, Any]] = field(default_factory=list)
    pack: str | None = None
    scope: str | None = None
    observed_at: str | dt.datetime | None = None


@dataclass(frozen=True)
class Document:
    """Артефакт (CP-ADR-0072): с содержимым (PUT /artifact-contents + POST /artifacts)
    или ссылкой ``uri`` на файл во внешней системе."""

    type: str
    name: str
    content: bytes | None = None
    media_type: str | None = None
    uri: str | None = None
    metadata: Mapping[str, Any] | None = None
    #: повтор с тем же ключом ядро узнаёт и не заводит второй артефакт
    idempotency_key: str | None = None


@dataclass(frozen=True)
class Registered:
    """Функция, помеченная @observer: кто она, какой точкой входа её зовут и какой вид
    исполнителя её исполняет."""

    function: Callable[[ObserveContext], None]
    kind: str
    entrypoint: str
    executor: str = EXECUTOR_KIND
    #: интервал цикла, если в params нет intervalSeconds (у своего вида — из его схемы)
    interval: int = DEFAULT_INTERVAL

    def __call__(self, ctx: ObserveContext) -> None:
        self.function(ctx)


def observer(
    *, kind: str, entrypoint: str, executor: str = EXECUTOR_KIND, interval: int = DEFAULT_INTERVAL
) -> Callable[[Callable[..., None]], Registered]:
    """Пометить функцию наблюдателем. ``kind`` — имя наблюдателя: источник его
    наблюдений по умолчанию; ``entrypoint`` — «модуль:функция», как в
    ``executor.params.entrypoint`` описания агента. ``executor`` — вид исполнителя
    агента: ``observer`` или собственный вид со своими params в схеме ядра (например
    ``git-connector``) — у такого params.entrypoint нет, точку входа образ задаёт
    переменной ``CONNECTOR_ENTRYPOINT``."""

    def mark(function: Callable[..., None]) -> Registered:
        return Registered(function, kind, entrypoint, executor, interval)

    return mark


@dataclass(frozen=True)
class AgentRevision:
    """Ревизия своего агента, как её вернул ``GET /agents/me``."""

    key: str
    revision: int
    revision_id: str
    spec: Mapping[str, Any]
    status: str = "active"
    state: str = "running"
    workspace_id: str | None = None
    principal_id: str | None = None

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> AgentRevision:
        revision = body["revision"]
        return cls(
            key=str(body["key"]),
            revision=int(revision["revision"]),
            revision_id=str(revision["id"]),
            spec=dict(revision.get("spec") or {}),
            status=str(body.get("status") or "active"),
            state=str(body.get("state") or "running"),
            workspace_id=str(body["workspaceId"]) if body.get("workspaceId") else None,
            principal_id=str(body["principalId"]) if body.get("principalId") else None,
        )

    @property
    def label(self) -> str:
        return f"{self.key}@{self.revision}"

    @property
    def executor(self) -> Mapping[str, Any]:
        value = self.spec.get("executor")
        return value if isinstance(value, Mapping) else {}

    @property
    def params(self) -> Mapping[str, Any]:
        value = self.executor.get("params")
        return value if isinstance(value, Mapping) else {}

    @property
    def inactive(self) -> bool:
        return self.status == "retired" or self.state == "stopped"


class ObserveContext:
    """Всё, что наблюдателю нужно на один цикл."""

    def __init__(
        self,
        *,
        agent: AgentRevision,
        source: str,
        state: dict[str, Any],
        secrets_dir: Path,
        core: Core,
        run_async: Callable[[Any], Any],
        uploads: dict[str, Any] | None = None,
        documents: dict[str, dict[str, Any]] | None = None,
        now: Callable[[], dt.datetime] | None = None,
        log: logging.Logger = logger,
        data_dir: Path | None = None,
    ) -> None:
        self.agent = agent
        #: Том реплики (``CONNECTOR_DATA_DIR``): рабочие файлы наблюдателя, которые
        #: переживают перезапуск (клоны репозиториев и т.п.). Состояние — в ``state``.
        self.data_dir = data_dir or Path(DEFAULT_DATA_DIR)
        self.source = source
        #: Состояние наблюдателя (курсор и т.п.): JSON, сохраняется после цикла без ошибок.
        self.state = state
        self.log = log
        self._secrets_dir = secrets_dir
        self._core = core
        self._run = run_async
        #: ключ документа → contentRef загруженного содержимого: повтор цикла шлёт то же
        #: тело POST /artifacts (ядро отвергает другое тело под тем же ключом — 409)
        self.uploads: dict[str, Any] = uploads if uploads is not None else {}
        self.now = now or (lambda: dt.datetime.now(dt.UTC))
        #: ключ документа → артефакт, который ядро уже завело: тот же документ в следующих
        #: циклах возвращается отсюда, без новой загрузки и нового тела под тем же ключом
        self.documents: dict[str, dict[str, Any]] = documents if documents is not None else {}
        #: что цикл уже опубликовал — для журнала и тестов
        self.published: list[str] = []

    @property
    def params(self) -> Mapping[str, Any]:
        """``executor.params`` ревизии целиком — у собственного вида исполнителя
        (``git-connector``) описание наблюдателя лежит здесь, а не в ``config``."""
        return self.agent.params

    @property
    def config(self) -> Mapping[str, Any]:
        """``executor.params.config`` ревизии — данные пакета, секретов в них нет."""
        value = self.agent.params.get("config")
        return value if isinstance(value, Mapping) else {}

    @property
    def workspace_id(self) -> str | None:
        return self.agent.workspace_id

    def secret(self, name: str) -> str:
        """Значение секрета узла по канону skill-sdk (``package_sdk.connector.secrets``).

        Нет файла, он пуст или из одних пробельных символов — :class:`SecretMissing`
        (у непустого значения обрезаются только хвостовые ``\\r``/``\\n``); имя не по шаблону
        ``[a-z0-9][a-z0-9-]{0,62}`` или ``agent-pat``, ссылка за пределы каталога,
        подмена пути, не обычный файл, больше 64 КиБ, не UTF-8, нет прав —
        :class:`SecretRejected` с кодом и причиной."""
        value = read_secret_file(self._secrets_dir, name)
        if not value:
            raise SecretMissing(name)
        return value

    def secret_file(self, name: str) -> Path:
        """Путь к файлу секрета узла — для инструментов, которые читают файл сами
        (credential helper git, клиентский сертификат); значение в процесс не читается.
        Нет файла, пуст или из пробелов — :class:`SecretMissing`, отвергнут —
        :class:`SecretRejected`."""
        self.secret(name)
        return self._secrets_dir / name

    def _publish(self, call: Any, label: str) -> Any:
        try:
            answer = self._run(call)
        except Exception as exc:
            status = getattr(exc, "status", None)
            code = getattr(exc, "code", None)
            if (
                isinstance(status, int)
                and status not in _RETRYABLE
                and code not in _RETRYABLE_CODES
            ):
                raise  # отказ ядра по сути — сбой цикла (cycle_failed), не молчаливый повтор
            raise PublishError(f"{label}: {type(exc).__name__}") from exc
        self.published.append(label)
        return answer

    def document_key(self, document: Document) -> str:
        """Ключ идемпотентности документа этого агента (см. :func:`document_key`)."""
        agent = f"{self.agent.key}:{self.agent.principal_id or ''}"
        return document_key(self.source, document, agent=agent, workspace=self.workspace_id)

    def emit(self, observation: Observation) -> Mapping[str, Any]:
        """Наблюдение сразу в ядро; ответ — запись журнала (``id``, ``deduplicated``)."""
        answer = self._publish(
            _remember(self._core, self, observation),
            f"{observation.kind}:{observation.dedup_key}",
        )
        return answer if isinstance(answer, Mapping) else {}

    def snapshot(self, snapshot: Snapshot) -> Mapping[str, Any]:
        """Снимок знаний в ядро (нужен workspace агента); ответ — счётчики сверки памяти."""
        if not self.workspace_id:
            raise ValueError("снимку знаний нужен workspace агента (work.workspace)")
        answer = self._publish(
            _snapshot(self._core, self, snapshot),
            f"snapshot:{snapshot.source}:{snapshot.snapshot_id}",
        )
        return answer if isinstance(answer, Mapping) else {}

    def document(self, document: Document) -> Mapping[str, Any]:
        """Артефакт в ядро; ответ — артефакт (``id``) для ссылки из наблюдения."""
        if (document.content is None) == (document.uri is None):
            raise ValueError("у документа ровно одно из content и uri")
        if document.content is not None and not document.media_type:
            raise ValueError("у документа с содержимым нужен media_type")
        self.document_key(document)  # неверный ключ — сбой цикла, а не повтор
        answer = self._publish(
            _document(self._core, self, document), f"document:{document.type}:{document.name}"
        )
        return answer if isinstance(answer, Mapping) else {}


def _iso(value: str | dt.datetime | None) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.isoformat()


def _workspace(ctx: ObserveContext) -> dict[str, Any]:
    return {"workspace_id": ctx.workspace_id} if ctx.workspace_id else {}


async def _remember(core: Core, ctx: ObserveContext, item: Observation) -> Any:
    return await core.remember(
        **_workspace(ctx),
        kind=item.kind,
        content=item.content or f"{item.kind}: {item.dedup_key}",
        data=dict(item.data) if item.data is not None else None,
        source=item.source or ctx.source,
        dedup_key=item.dedup_key,
        observed_at=_iso(item.observed_at),
        supersedes=item.supersedes,
        external_ref=dict(item.external_ref) if item.external_ref else None,
    )


async def _snapshot(core: Core, ctx: ObserveContext, item: Snapshot) -> Any:
    body: dict[str, Any] = {
        "source": item.source,
        "snapshotId": item.snapshot_id,
        "observedAt": _iso(item.observed_at) or dt.datetime.now(dt.UTC).isoformat(),
        "entities": [dict(e) for e in item.entities],
        "relations": [dict(r) for r in item.relations],
    }
    if item.pack:
        body["pack"] = item.pack
    if item.scope:
        body["scope"] = item.scope
    return await core.submit_knowledge_snapshot(workspace_id=str(ctx.workspace_id), snapshot=body)


#: Предел ключа идемпотентности ядра (иначе 400 invalid_idempotency_key).
IDEMPOTENCY_KEY_MAX = 200


def document_key(
    source: str, item: Document, *, agent: str = "", workspace: str | None = None
) -> str:
    """Ключ идемпотентности артефакта по умолчанию — фиксированной длины: ``doc:`` и sha256
    от агента, workspace, наблюдателя, типа, имени, metadata и отпечатка содержимого или
    ссылки. Ядро ищет ключ по (tenant, key) и отвечает 409, если его уже использовал другой
    principal: без агента в ключе два агента одного наблюдателя столкнулись бы."""
    if item.idempotency_key:
        if len(item.idempotency_key) > IDEMPOTENCY_KEY_MAX:
            raise ValueError(
                f"idempotency_key длиннее {IDEMPOTENCY_KEY_MAX} символов — ядро его отвергнет"
            )
        return item.idempotency_key
    body = item.content if item.content is not None else str(item.uri).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    metadata = json.dumps(
        dict(item.metadata or {}), sort_keys=True, ensure_ascii=False, default=str
    )
    parts = (agent, workspace or "", source, item.type, item.name, metadata, digest)
    return "doc:" + hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


#: Сколько заведённых артефактов наблюдатель помнит по ключу (старые вытесняются).
DOCUMENTS_REMEMBERED = 2000
_REMEMBERED_FIELDS = ("id", "type", "name", "uri", "contentRef", "workspaceId")


def _stored_upload(ctx: ObserveContext, key: str) -> str | None:
    """contentRef прежней загрузки, если она ещё живёт (с запасом); иначе None."""
    stored = ctx.uploads.get(key)
    if isinstance(stored, str):  # файл состояния прежней версии: срок неизвестен
        return None
    if not isinstance(stored, Mapping) or not stored.get("ref"):
        return None
    expires = stored.get("expiresAt")
    if expires:
        try:
            moment = dt.datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment - _UPLOAD_MARGIN <= ctx.now():
            return None
    return str(stored["ref"])


async def _upload(core: Core, ctx: ObserveContext, key: str, item: Document) -> str:
    uploaded = await core.upload_artifact_content(
        item.content or b"", media_type=str(item.media_type)
    )
    ref = str(uploaded["contentRef"])
    ctx.uploads[key] = {"ref": ref, "expiresAt": uploaded.get("expiresAt")}
    return ref


async def _document(core: Core, ctx: ObserveContext, item: Document) -> Any:
    key = ctx.document_key(item)
    known = ctx.documents.get(key)
    if known is not None:
        return dict(known)

    async def create(place: dict[str, Any]) -> Any:
        return await core.create_artifact(
            **_workspace(ctx),
            type=item.type,
            name=item.name,
            metadata=dict(item.metadata) if item.metadata else None,
            idempotency_key=key,
            **place,
        )

    if item.content is None:
        answer = await create({"uri": item.uri})
    else:
        ref = _stored_upload(ctx, key) or await _upload(core, ctx, key, item)
        try:
            answer = await create({"content_ref": ref})
        except Exception as exc:
            if getattr(exc, "code", None) != "content_ref_not_found":
                raise
            # загрузку успели убрать (срок жизни): загрузить заново — один раз
            ctx.uploads.pop(key, None)
            answer = await create({"content_ref": await _upload(core, ctx, key, item)})
    if isinstance(answer, Mapping):
        ctx.documents[key] = {k: answer[k] for k in _REMEMBERED_FIELDS if k in answer}
        while len(ctx.documents) > DOCUMENTS_REMEMBERED:
            del ctx.documents[next(iter(ctx.documents))]
    ctx.uploads.pop(key, None)
    return answer


@dataclass
class Runner:
    """Цикл наблюдателя по ревизии своего агента."""

    function: Registered
    core: Core
    data_dir: Path
    secrets_dir: Path
    run_async: Callable[[Any], Any]
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)

    @property
    def state_file(self) -> Path:
        return self.data_dir / STATE_FILE

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _save(self, state: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(self.state_file)

    def revision(self) -> AgentRevision | None:
        body = self.run_async(self.core.get_my_agent())
        return AgentRevision.from_body(body) if body else None

    def verdict_at_start(self, revision: AgentRevision | None) -> int | None:
        """Можно ли исполнять описание этим образом: None — да, иначе код выхода."""
        if revision is None:
            logger.error(
                "the principal is not bound to an agent: nothing describes what to observe"
            )
            return EXIT_MISCONFIGURED
        if revision.inactive:
            logger.info(
                "%s is %s/%s; nothing to run", revision.label, revision.status, revision.state
            )
            return EXIT_STOPPED
        if revision.executor.get("kind") != self.function.executor:
            logger.error(
                "%s: executor %r, expected %r",
                revision.label,
                revision.executor.get("kind"),
                self.function.executor,
            )
            return EXIT_MISCONFIGURED
        entrypoint = revision.params.get("entrypoint")
        # у собственного вида исполнителя точки входа в params может не быть
        if entrypoint is None and self.function.executor != EXECUTOR_KIND:
            return None
        if entrypoint != self.function.entrypoint:
            logger.error(
                "%s: entrypoint %r is not this observer (%s)",
                revision.label,
                entrypoint,
                self.function.entrypoint,
            )
            return EXIT_MISCONFIGURED
        return None

    def verdict_between(self, revision: AgentRevision) -> int | None:
        """Между циклами: None — работать дальше, иначе код выхода."""
        try:
            current = self.revision()
        except Exception as exc:  # сбой чтения ревизии не повод останавливаться
            logger.warning("could not read the agent's revision: %s", type(exc).__name__)
            return None
        if current is None:
            logger.warning("%s: the principal is no longer an agent; restarting", revision.label)
            return EXIT_REVISION_CHANGED
        if current.inactive:
            logger.info("%s is %s/%s; stopping", current.label, current.status, current.state)
            return EXIT_STOPPED
        if current.revision_id != revision.revision_id:
            logger.info("agent %s moved to revision %d; restarting", current.key, current.revision)
            return EXIT_REVISION_CHANGED
        return None

    def _mark_once(self, stored: dict[str, Any], key: str, period: str) -> bool:
        """True — отметки за этот период ещё не было (и она поставлена)."""
        marks = stored.setdefault(_MARKS, {})
        if marks.get(key) == period:
            return False
        marks[key] = period
        return True

    def _report(
        self,
        revision: AgentRevision,
        stored: dict[str, Any],
        kind: str,
        mark: str,
        period: str,
        content: str,
        data: dict[str, Any],
    ) -> None:
        if not self._mark_once(stored, mark, period):
            return
        ctx = self._context(revision, {})
        try:
            ctx.emit(
                Observation(
                    kind=kind,
                    dedup_key=f"{self.function.kind}:{mark}:{period}",
                    content=content,
                    data=data,
                )
            )
        except PublishError as exc:  # ядро недоступно — сообщим в следующий раз
            logger.warning("%s not written: %s", kind, type(exc).__name__)
            stored[_MARKS].pop(mark, None)

    def _context(
        self,
        revision: AgentRevision,
        state: dict[str, Any],
        uploads: dict[str, Any] | None = None,
        documents: dict[str, dict[str, Any]] | None = None,
    ) -> ObserveContext:
        return ObserveContext(
            agent=revision,
            source=self.function.kind,
            state=state,
            secrets_dir=self.secrets_dir,
            core=self.core,
            run_async=self.run_async,
            uploads=uploads,
            documents=documents,
            now=self.now,
            data_dir=self.data_dir,
        )

    def cycle(self, revision: AgentRevision) -> list[str]:
        """Один цикл: функция автора публикует по ходу; состояние — после цикла без ошибок."""
        stored = self._load()
        state = dict(stored.get("state") or {})
        # загрузки неудавшегося цикла живут до цикла без ошибок: повтор шлёт то же тело
        marks = stored.setdefault(_MARKS, {})
        uploads = marks.setdefault("uploads", {})
        documents = marks.setdefault("documents", {})
        ctx = self._context(revision, state, uploads, documents)
        now = self.now()
        try:
            self.function(ctx)
        except PublishError as exc:  # не сдвигать state: повтор в следующем цикле
            logger.warning("%s: publish failed: %s", revision.label, exc)
            self._save(stored)  # state прежний, загрузки — для повтора
            return []
        except SecretMissing as missing:
            logger.warning("secret %s is absent or empty; cycle skipped", missing.name)
            self._report(
                revision,
                stored,
                SECRET_MISSING,
                f"secret-missing:{missing.name}",
                now.date().isoformat(),
                f"Нет секрета узла {missing.name}: наблюдатель {self.function.kind} "
                "не опрашивает источник",
                {"secret": missing.name, "agent": revision.key},
            )
            self._save(stored)
            return []
        except SecretRejected as rejected:  # не «секрета нет»: файл есть, но он негоден
            logger.warning(
                "secret %s rejected: %s (%s); cycle skipped",
                rejected.name,
                rejected.code,
                rejected.reason or "-",
            )
            self._report(
                revision,
                stored,
                CYCLE_FAILED,
                "cycle-failed",
                now.strftime("%Y-%m-%dT%H"),
                f"Секрет узла {rejected.name} отвергнут ({rejected.code}"
                f"{': ' + rejected.reason if rejected.reason else ''}): наблюдатель "
                f"{self.function.kind} не опрашивает источник",
                {
                    "error": "SecretRejected",
                    "secret": rejected.name,
                    "code": rejected.code,
                    "reason": rejected.reason,
                    "agent": revision.key,
                    "revision": revision.revision,
                },
            )
            self._save(stored)
            return []
        except Exception as exc:  # сбой цикла не роняет процесс
            logger.exception("%s: cycle failed", revision.label)
            self._report(
                revision,
                stored,
                CYCLE_FAILED,
                "cycle-failed",
                now.strftime("%Y-%m-%dT%H"),
                f"Цикл наблюдателя {self.function.kind} завершился ошибкой {type(exc).__name__}",
                {"error": type(exc).__name__, "agent": revision.key, "revision": revision.revision},
            )
            self._save(stored)
            return []
        done = list(ctx.published)
        stored["state"] = ctx.state
        marks = stored.get(_MARKS) or {}
        for mark in [m for m in marks if m.startswith("secret-missing:")]:
            del marks[mark]  # секрет появился — о новой пропаже снова сообщить
        self._save(stored)
        for item in done:
            logger.info("published %s", item)
        return done

    def serve(self, sleep: Callable[[float], None] = time.sleep, cycles: int | None = None) -> int:
        """Цикл до смены ревизии или остановки; возвращает код выхода процесса."""
        revision = self.revision()
        verdict = self.verdict_at_start(revision)
        if verdict is not None or revision is None:
            return verdict if verdict is not None else EXIT_MISCONFIGURED
        interval = int(revision.params.get("intervalSeconds") or self.function.interval)
        logger.info("%s: observer %s every %d s", revision.label, self.function.kind, interval)
        done = 0
        while True:
            self.cycle(revision)
            done += 1
            verdict = self.verdict_between(revision)
            if verdict is not None:
                return verdict
            if cycles is not None and done >= cycles:
                return EXIT_STOPPED
            sleep(interval)


def _agent_credentials(environ: Mapping[str, str], secrets_dir: Path) -> dict[str, str]:
    """PAT агента из секрета узла → окружение клиента ядра (режим environment IAM)."""
    extra: dict[str, str] = {}
    if environ.get("IAM_PLATFORM_ACCESS_TOKEN"):
        return extra
    pat = secrets_dir / "agent-pat"
    try:
        token = pat.read_text(encoding="utf-8").strip()
    except OSError:
        return extra
    if token:
        extra["IAM_CREDENTIAL_MODE"] = "environment"
        extra["IAM_PLATFORM_ACCESS_TOKEN"] = token
        extra.setdefault("IAM_NO_KEYCHAIN", environ.get("IAM_NO_KEYCHAIN", "1"))
        extra["CONTROL_PLANE_IAM_SCOPES"] = environ.get(
            "CONTROL_PLANE_IAM_SCOPES", "control-plane:read control-plane:write"
        )
    return extra


def run(
    function: Registered,
    *,
    core: Core | None = None,
    environ: Mapping[str, str] | None = None,
) -> NoReturn:
    """Точка входа процесса наблюдателя: цикл и выход с кодом для узла."""
    import asyncio

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    env = dict(os.environ if environ is None else environ)
    secrets_dir = Path(env.get(ENV_SECRETS_DIR, DEFAULT_SECRETS_DIR))
    if core is None:
        os.environ.update(_agent_credentials(env, secrets_dir))
        server = env.get("CONTROL_PLANE_SERVER")
        if not server:
            logger.error("CONTROL_PLANE_SERVER is not set: the node passes it to the agent")
            sys.exit(EXIT_MISCONFIGURED)
        from package_sdk.connector.core import ClientCore

        core = ClientCore(server)
    runner = Runner(
        function,
        core,
        data_dir=Path(env.get(ENV_DATA_DIR, DEFAULT_DATA_DIR)),
        secrets_dir=secrets_dir,
        run_async=asyncio.run,
    )
    sys.exit(runner.serve())


def load_entrypoint(entrypoint: str) -> Registered:
    """«модуль:функция» образа → наблюдатель; не найден или не помечен — ValueError."""
    module_name, _, attribute = entrypoint.partition(":")
    if not module_name or not attribute:
        raise ValueError(f"entrypoint {entrypoint!r}: нужен вид модуль:функция")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ValueError(f"entrypoint {entrypoint!r}: модуля нет в образе ({exc})") from exc
    function = getattr(module, attribute, None)
    if not isinstance(function, Registered):
        raise ValueError(f"entrypoint {entrypoint!r}: не наблюдатель (@observer)")
    if function.entrypoint != entrypoint:
        raise ValueError(
            f"entrypoint {entrypoint!r}: наблюдатель объявлен как {function.entrypoint!r}"
        )
    return function
