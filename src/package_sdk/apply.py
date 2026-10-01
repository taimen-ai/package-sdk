"""Каталог обычными ресурсами ядра: сравнение с tenant, применение и выгрузка объектов в пакет.

Секции единого плана (TAI-ADR-0062 п.7, ``package_sdk.install``) строятся здесь же: в режиме
``dry_run`` установщик ничего не пишет, а каждое изменение записывает данными в ``changes``
(``{package, kind, key, operation, fields, expected, …}``) — это и есть секция плана, и её же
установщик строит заново перед записью, чтобы узнать устаревший план."""

from __future__ import annotations

import copy
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

try:
    import yaml

    from package_sdk import yaml12
except ImportError:  # pragma: no cover - окружение без PyYAML
    yaml = None
    yaml12 = None  # type: ignore[assignment]

from package_sdk.check import _domain, normalize_rule
from package_sdk.manifest import missing_variable, package_env
from package_sdk.model import (
    API,
    API_VERSION,
    CATALOG_KINDS,
    DEFAULT_EXECUTION_INPUTS,
    IDENTITY,
    NOTIFY_AUDIENCE,
    NOTIFY_URL_ENV,
    PLAN_KINDS,
    RULE_MUTABLE,
    SERVER_DEFAULTED,
    SKILL_IMMUTABLE,
    SKILL_MUTABLE,
    SPEC_FIELDS,
    Installation,
    Obj,
    Package,
    PackageError,
    canonical,
    install_hash,
    package_ref,
    substitute,
)

# Связь объекта с пакетом (CP-ADR-0074 §11, амендмент TASK-000904): POST /packages:record.
PACKAGES_RECORD = "/packages:record"
OPENAPI_PATH = "/openapi.json"
# Какие виды ядро записывает через packages:record, знает ядро (RecordedKind в
# api/v1/schemas.py control-plane): установщик читает перечень из OpenAPI ядра. Константа —
# только запасной путь, если OpenAPI не прочитать; тест сверяет её с ядром и снимком OpenAPI.
# Виды, у которых в едином плане своя секция: онтологии (knowledge), правила уведомлений
# (notification-rules), процессы и календари (core).
SECTION_KINDS = frozenset({"KnowledgePack", "NotificationRule", *PLAN_KINDS})
RECORDED_KINDS_FALLBACK = (
    "ArtifactType",
    "TaskType",
    "ProjectTemplate",
    "WorkspaceType",
    "Role",
    "Capability",
    "Skill",
    "WorkRule",
    "Agent",
)


class HttpLike(Protocol):
    def call(
        self, method: str, path: str, body: Any = None, headers: dict | None = None
    ) -> dict: ...


class HttpError(RuntimeError):
    """Ответ с ошибкой: текст как прежде ("… HTTP <код>: <тело>"), плюс код и разобранное тело."""

    def __init__(self, message: str, status: int, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _origin(url: str) -> tuple[str, str, int | None]:
    """Origin адреса для сравнения: схема, хост и порт (порт по умолчанию — явно)."""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:  # порт не число — такого origin нет, совпасть он не может
        return scheme, parts.netloc, -1
    return scheme, (parts.hostname or "").lower(), port or {"http": 80, "https": 443}.get(scheme)


def _shown_origin(url: str) -> str:
    """Origin для сообщения: схема и хост с портом, как их назвал сервер, без учётных данных."""
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    return f"{parts.scheme}://{host}"


class RedirectRefused(HttpError):
    """Стенд ответил редиректом, по которому SDK не идёт (TASK-001258): на другой origin
    (схема, хост или порт) — ``Authorization`` и ``Idempotency-Key`` не должны уйти чужому
    хосту, а тело POST/PUT при редиректе теряется; с телом — и на тот же origin."""


class _SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    """Редирект — только GET/HEAD на тот же origin, с теми же заголовками запроса.

    Заголовки вызывающего (``Authorization``, ``Idempotency-Key``) ``Http`` кладёт
    непереносимыми (``add_unredirected_header``): обычный обработчик urllib их при
    редиректе не копирует. Этот обработчик возвращает их только на тот же origin, а редирект
    на другой — отказ с понятной ошибкой: адрес стенда надо поправить в ``--server``."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        method = req.get_method()
        path = urllib.parse.urlsplit(req.full_url).path
        refusal = None
        if _origin(newurl) != _origin(req.full_url):
            refusal = (
                f"{method} {path}: HTTP {code}: сервер перенаправляет на "
                f"{_shown_origin(newurl)} — укажите его в --server"
            )
        elif method not in ("GET", "HEAD"):
            refusal = (
                f"{method} {path}: HTTP {code}: сервер перенаправляет на {newurl} — тело "
                "запроса при редиректе теряется, укажите точный адрес в --server"
            )
        if refusal is not None:
            fp.close()
            raise RedirectRefused(refusal, code)
        follow = super().redirect_request(req, fp, code, msg, headers, newurl)
        if follow is not None:
            for key, value in req.unredirected_hdrs.items():
                follow.add_unredirected_header(key, value)
        return follow


_OPENER = urllib.request.build_opener(_SameOriginRedirects())


class Http:
    """Минимальный HTTP-клиент CLI; bootstrap передаёт свой с тем же call().

    Редирект на другой origin — отказ (:class:`RedirectRefused`), а не повтор запроса с
    учёткой у чужого хоста: например, старый адрес стенда отвечает 308 на новый."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, body: Any = None, headers: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            # учётка и ключ идемпотентности — только этому origin (см. _SameOriginRedirects)
            request.add_unredirected_header(key, value)
        try:
            with _OPENER.open(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            raw = error.read().decode(errors="replace")
            try:
                body = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                body = None
            raise HttpError(
                f"{method} {path}: HTTP {error.code}: {raw[:400]}", error.code, body
            ) from error


def _moved_endpoint(wanted: Any, actual: Any) -> str | None:
    """Новый implementation.endpoint, если контракт wanted отличается от опубликованного
    только им; иначе None — это другое обещание и нужна новая версия."""
    if not isinstance(wanted, dict) or not isinstance(actual, dict):
        return None
    endpoint = (wanted.get("implementation") or {}).get("endpoint")
    published = (actual.get("implementation") or {}).get("endpoint")
    if not isinstance(endpoint, str) or endpoint == published:
        return None
    moved = copy.deepcopy(wanted)
    moved["implementation"]["endpoint"] = published
    return endpoint if is_subset(moved, actual) else None


def is_subset(wanted: Any, actual: Any) -> bool:
    """wanted ⊆ actual: в словарях сравниваются только ключи wanted (сервер мог
    заполнить значения по умолчанию), списки и скаляры — точно."""
    if isinstance(wanted, dict):
        return isinstance(actual, dict) and all(
            k in actual and is_subset(v, actual[k]) for k, v in wanted.items()
        )
    return canonical(wanted) == canonical(actual)


def _desired(kind: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Поля spec, которые сравниваются с сервером, с умолчаниями API."""
    result = {}
    for name, default in SPEC_FIELDS[kind].items():
        if name in spec:
            result[name] = copy.deepcopy(spec[name])
        elif default is not SERVER_DEFAULTED:
            result[name] = copy.deepcopy(default)
    if kind == "TaskType" and result.get("execution"):
        result["execution"].setdefault("inputs", DEFAULT_EXECUTION_INPUTS)
    if kind == "ArtifactType":
        # ядро хранит media types в нижнем регистре, без параметров и повторов
        result["mediaTypes"] = list(
            dict.fromkeys(m.split(";", 1)[0].strip().lower() for m in result["mediaTypes"])
        )
    return result


def _differences(kind: str, desired: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    changed = []
    for name, value in desired.items():
        current = actual.get(name)
        if kind == "ProjectTemplate" and name == "defaultConfig":
            same = is_subset(value, current)
        else:
            same = canonical(value) == canonical(current)
        if not same:
            changed.append(name)
    return changed


# --- связь объектов с пакетом (CP-ADR-0074 §11, амендмент TASK-000904) ----------


def _pointer_parts(pointer: str) -> list[str]:
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def _resolve_ref(document: dict[str, Any], node: Any) -> Any:
    seen = 0
    while isinstance(node, dict) and isinstance(node.get("$ref"), str) and seen < 20:
        pointer = node["$ref"]
        if not pointer.startswith("#/"):
            return None
        node = document
        for part in _pointer_parts(pointer[1:]):
            node = node.get(part) if isinstance(node, dict) else None
        seen += 1
    return node


def recorded_kinds_from_openapi(document: Any) -> tuple[str, ...] | None:
    """Виды, которые ядро принимает в POST /packages:record: перечень objects[].kind тела
    запроса из OpenAPI ядра. None — маршрута в документе нет (ядро старше связей с пакетом)."""
    if not isinstance(document, dict):
        raise PackageError("OpenAPI ядра — не объект JSON")
    operation = ((document.get("paths") or {}).get(API + PACKAGES_RECORD) or {}).get("post")
    if operation is None:
        return None
    content = ((operation.get("requestBody") or {}).get("content") or {}).get(
        "application/json"
    ) or {}
    schema = _resolve_ref(document, content.get("schema"))
    objects = _resolve_ref(document, ((schema or {}).get("properties") or {}).get("objects"))
    item = _resolve_ref(document, (objects or {}).get("items"))
    kind = _resolve_ref(document, ((item or {}).get("properties") or {}).get("kind"))
    variants = [kind] + list((kind or {}).get("anyOf") or (kind or {}).get("oneOf") or [])
    kinds: list[str] = []
    for variant in variants:
        variant = _resolve_ref(document, variant) or {}
        kinds += [v for v in variant.get("enum") or [] if isinstance(v, str)]
        if isinstance(variant.get("const"), str):
            kinds.append(variant["const"])
    if not kinds:
        raise PackageError(
            f"OpenAPI ядра: у POST {API}{PACKAGES_RECORD} нет перечня objects[].kind"
        )
    return tuple(dict.fromkeys(kinds))


def record_objects(
    package: Package, kinds: Iterable[str], exclude: Iterable[str] = ()
) -> list[dict[str, str]]:
    """objects [{kind, key}] для packages:record: все объекты пакета тех видов, что ядро
    записывает, — применённые и не изменившиеся; процессы и календари — никогда (их связывает
    POST /packages:apply), как и exclude — виды, которые у этого пакета ставит план ядра.
    Скилл — по имени: связь у объекта, не у версии."""
    allowed = set(kinds) - set(PLAN_KINDS) - set(exclude)
    objects: list[dict[str, str]] = []
    for obj in package.objects:
        entry = {"kind": obj.kind, "key": obj.key}
        if obj.kind in allowed and entry not in objects:
            objects.append(entry)
    return objects


def knowledge_targets(installation: Installation, env: dict[str, str]) -> list[dict[str, Any]]:
    """Секция knowledge установки для плана и применения: [{workspace, packs, strict}] с
    подставленными ${ПЕРЕМЕННЫМИ}; повтор workspace — один итоговый набор."""
    merged: dict[str, dict[str, Any]] = {}
    for entry in installation.knowledge:
        workspace = substitute(str(entry.get("workspace") or ""), env)
        target = merged.setdefault(
            workspace, {"workspace": workspace, "packs": [], "strict": False}
        )
        for value in entry.get("packs") or []:
            if value not in target["packs"]:
                target["packs"].append(str(value))
        # строгий режим, заданный в любой из записей workspace, — для всего набора
        target["strict"] = target["strict"] or bool(entry.get("strict"))
    return list(merged.values())


@dataclass
class Applier:
    http: HttpLike
    # прежняя форма учётки — Authorization в заголовках (bootstrap): не в repr
    headers: dict[str, str] = field(repr=False)
    env: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False
    log: Callable[[str], None] = print
    result: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Сервис уведомлений (NotificationRule): свой адрес и свой токен; None — не задан
    notify: HttpLike | None = None
    notify_headers: dict[str, str] = field(default_factory=dict, repr=False)
    _notify_checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Пакеты установки по ключу: {key, version} для POST /agents и packages:record
    _packages: dict[str, Package] = field(default_factory=dict)
    # Изменения данными — секция плана (TAI-ADR-0062 п.7): что установщик делает или сделал бы
    changes: list[dict[str, Any]] = field(default_factory=list)

    # -- транспорт --

    def _get(self, path: str) -> dict:
        return self.http.call("GET", API + path, None, self.headers)

    @staticmethod
    def _pages(get: Callable[[str], dict], path: str, params: dict[str, Any]) -> list[dict]:
        items: list[dict] = []
        cursor = None
        while True:
            query = {
                k: v for k, v in {**params, "limit": 100, "cursor": cursor}.items() if v is not None
            }
            page = get(f"{path}?{urllib.parse.urlencode(query)}")
            items.extend(page.get("items") or [])
            cursor = page.get("nextCursor")
            if not cursor:
                return items

    def _list(self, path: str, **params: Any) -> list[dict]:
        return self._pages(self._get, path, params)

    def _notify_call(self, method: str, path: str, body: Any = None) -> dict:
        if self.notify is None:
            raise PackageError(
                f"сервис уведомлений не задан — нужен {NOTIFY_URL_ENV} и токен audience "
                f"{NOTIFY_AUDIENCE}"
            )
        return self.notify.call(method, API + path, body, self.notify_headers)

    def _notify_list(self, **params: Any) -> list[dict]:
        return self._pages(
            lambda path: self._notify_call("GET", path), "/notification-rules", params
        )

    def _write(self, method: str, path: str, body: Any = None, extra: dict | None = None) -> dict:
        headers = {**self.headers, "Idempotency-Key": str(uuid.uuid4()), **(extra or {})}
        return self.http.call(method, API + path, body, headers)

    def _say(self, obj_ref: str, message: str) -> None:
        self.log(f"   {obj_ref}: {'(план) ' if self.dry_run else ''}{message}")

    def _change(
        self,
        kind: str,
        key: str,
        operation: str,
        detail: str,
        *,
        package: str | None = None,
        fields: list[str] | None = None,
        expected: Any = None,
        **extra: Any,
    ) -> None:
        """Изменение секции плана. expected — что установщик видел на стенде (сверка перед
        записью: другое — план устарел), detail — пояснение человеку, в сверку не входит."""
        change: dict[str, Any] = {"kind": kind, "key": key, "operation": operation}
        if package is not None:
            change["package"] = package
        if fields:
            change["fields"] = list(fields)
        if expected is not None:
            change["expected"] = copy.deepcopy(expected)
        change.update(extra)
        change["detail"] = detail
        self.changes.append(change)

    # -- секции единого плана (TAI-ADR-0062 п.7) --

    def catalog(
        self, installation: Installation, owned: dict[str, frozenset[str]] | None = None
    ) -> None:
        """Секция catalog: виды, которые ставит установщик, в порядке CATALOG_KINDS; owned —
        виды, которые у пакета ставит план ядра (секция core), они здесь пропускаются. После
        записи (не в dry_run) — связь объектов с пакетом (packages:record)."""
        owned = owned or {}
        self._packages = {package.key: package for package in installation.packages}
        recorded = None if self.dry_run else self._recorded_kinds()
        for kind in CATALOG_KINDS:
            if kind in SECTION_KINDS:
                continue
            for obj in installation.objects:
                if obj.kind == kind and kind not in owned.get(obj.package, frozenset()):
                    getattr(self, f"_apply_{kind}")(obj, self._spec(installation, obj))
        if recorded is not None:
            for package in installation.packages:
                self._record(package, recorded, owned.get(package.key, frozenset()))

    def notification_rules(self, installation: Installation) -> None:
        """Секция notification-rules: сначала :validate всех правил, потом запись."""
        rules = [o for o in installation.objects if o.kind == "NotificationRule"]
        if not rules:
            return
        if self.notify is None:
            raise PackageError(
                f"в установке есть NotificationRule, а сервис уведомлений не задан — нужен "
                f"{NOTIFY_URL_ENV} и токен audience {NOTIFY_AUDIENCE}"
            )
        self._validate_notification_rules(installation, rules)
        for obj in rules:
            self._apply_NotificationRule(obj, self._spec(installation, obj))

    def retire_keys(self, retire: dict[str, list[str]]) -> None:
        """Вывод из оборота видов, которые выводит установщик (Process и Calendar — ядро)."""
        for kind, keys in retire.items():
            if kind in PLAN_KINDS:
                continue
            if kind == "NotificationRule" and self.notify is None:
                raise PackageError(
                    f"retire NotificationRule: сервис уведомлений не задан — нужен "
                    f"{NOTIFY_URL_ENV} и токен audience {NOTIFY_AUDIENCE}"
                )
            for key in keys:
                self._retire(kind, key)

    def _spec(self, installation: Installation, obj: Obj) -> dict[str, Any]:
        """spec объекта с ${ПЕРЕМЕННЫМИ}: значения установки поверх default манифеста;
        незаданная обязательная — ошибка с описанием переменной, без заглушки (FR-010)."""
        package = next((p for p in installation.packages if p.key == obj.package), None)
        if package is None:
            return substitute(obj.spec, self.env)

        def missing(name: str) -> str:
            raise PackageError(f"{obj.ref}: {missing_variable(package, name)}")

        return substitute(obj.spec, package_env(package, self.env), missing=missing)

    # -- связь объектов с пакетом (CP-ADR-0074 §11, амендмент TASK-000904) --

    def _recorded_kinds(self) -> tuple[str, ...]:
        """Виды, которые ядро записывает через packages:record, — из его OpenAPI; если
        OpenAPI не прочитать — запасной перечень с предупреждением."""
        try:
            document = self.http.call("GET", OPENAPI_PATH, None, self.headers)
            kinds = recorded_kinds_from_openapi(document)
        except (RuntimeError, OSError, ValueError, PackageError) as error:
            self.log(
                f"   ! OpenAPI ядра не прочитан ({error}) — виды для {PACKAGES_RECORD}: "
                f"{', '.join(RECORDED_KINDS_FALLBACK)}"
            )
            return RECORDED_KINDS_FALLBACK
        if kinds is None:
            raise PackageError(
                f"ядро не записывает связь объектов с пакетом (нет POST {API}{PACKAGES_RECORD}) — "
                "нужен control-plane не старше 1a4b4c2 (CP-ADR-0074, амендмент TASK-000904)"
            )
        return kinds

    def _record(
        self, package: Package, kinds: tuple[str, ...], exclude: Iterable[str] = ()
    ) -> None:
        """POST /packages:record: все объекты пакета тех видов, что ядро записывает, — и не
        изменившиеся: связь переходит на эту версию пакета. Ошибка останавливает установку."""
        ref = package_ref(package)
        where = f"{package.key} {ref['version']}"
        others = sorted(
            {o.kind for o in package.objects}
            - set(kinds)
            - set(PLAN_KINDS)
            - set(exclude)
            - {"NotificationRule", "KnowledgePack"}
        )
        if others:
            self.log(
                f"   ! {where}: {', '.join(others)} ядро через {PACKAGES_RECORD} не связывает — "
                "связь с пакетом у них не записана"
            )
        objects = record_objects(package, kinds, exclude)
        if not objects:
            self._say(where, f"связывать через {PACKAGES_RECORD} нечего")
            return
        body = {"package": ref, "installHash": install_hash(package.path), "objects": objects}
        try:
            response = self._write("POST", PACKAGES_RECORD, body)
        except RuntimeError as error:
            raise PackageError(
                f"пакет {where}: объекты применены, но ядро не записало их связь с пакетом "
                f"(POST {API}{PACKAGES_RECORD}): {error}. Установка остановлена; повторный apply "
                "идемпотентен"
            ) from error
        count = len((response or {}).get("recorded") or objects)
        self._say(where, f"связь с пакетом записана: объектов {count}, {body['installHash'][:19]}…")

    # KnowledgePack — онтология памяти (TAI-ADR-0062 п.5): версия неизменяема
    def _apply_KnowledgePack(self, obj: Obj, spec: dict[str, Any]) -> None:
        version = spec.get("version")
        if self.dry_run:
            self._say(obj.ref, f"версия {version} будет зарегистрирована, если её ещё нет")
            return
        try:
            self._write("POST", "/knowledge/packs", spec)
        except HttpError as error:
            if error.status == 409:
                raise PackageError(
                    f"{obj.ref}: версия {version} уже зарегистрирована с другим содержимым — "
                    "версия онтологии неизменяема, поднимите version"
                ) from error
            raise
        self._say(obj.ref, f"версия {version} зарегистрирована (или уже была такой)")
        self.result[obj.ref] = {"version": version}

    def _enable_knowledge(self, target: dict[str, Any]) -> None:
        """PUT /workspaces/{id}/knowledge-packs: итоговый набор заменяет прежний целиком."""
        where = f"workspace {target['workspace']}"
        packs = list(target["packs"])
        strict = bool(target.get("strict"))
        mode = ", строгий режим" if strict else ""
        if self.dry_run:
            self._say(where, f"онтологии → {', '.join(packs) or '—'}{mode} (набор заменит текущий)")
            return
        self._write(
            "PUT",
            f"/workspaces/{target['workspace']}/knowledge-packs",
            {"packs": packs, "strict": strict},
        )
        self._say(where, f"онтологии: {', '.join(packs) or '—'}{mode}")
        self.result[f"knowledge/{target['workspace']}"] = {"packs": packs, "strict": strict}

    # NotificationRule — в сервисе уведомлений (ADR-0005 notification-service §5–7)
    def _validate_notification_rules(self, installation: Installation, rules: list[Obj]) -> None:
        """:validate всех правил уведомлений до первой записи: правило, которое сервис не
        примет, останавливает секцию целиком."""
        for obj in rules:
            body = {"key": obj.key, "spec": self._spec(installation, obj)}
            try:
                self._notify_checks[obj.key] = self._notify_call(
                    "POST", "/notification-rules:validate", body
                )
            except RuntimeError as error:
                raise PackageError(
                    f"{obj.ref}: сервис уведомлений не принимает правило — {error}"
                ) from error

    def _apply_NotificationRule(self, obj: Obj, spec: dict[str, Any]) -> None:
        """Версию считает сервис по хэшу спецификации: запись — только если :validate сказал
        changed; без изменений ничего не пишется."""
        current = next((r for r in self._notify_list(key=obj.key) if r["key"] == obj.key), None)
        if not self._notify_checks[obj.key].get("changed"):
            self._say(obj.ref, f"v{current['version'] if current else '?'} без изменений")
            if current is not None:
                self.result[obj.ref] = {"version": current["version"]}
            return
        reason = "нет в сервисе" if current is None else "изменилась спецификация"
        self._change(
            obj.kind,
            obj.key,
            "create" if current is None else "version",
            f"новая версия ({reason})",
            package=obj.package,
            expected={"version": current["version"] if current else None},
        )
        if self.dry_run:
            self._say(obj.ref, f"новая версия ({reason})")
            return
        rule = self._notify_call("POST", "/notification-rules", {"key": obj.key, "spec": spec})
        self._say(obj.ref, f"опубликована v{rule['version']} ({reason})")
        self.result[obj.ref] = {"version": rule["version"]}

    # версии неизменяемы: TaskType и ProjectTemplate
    def _apply_versioned(
        self, obj: Obj, spec: dict[str, Any], collection: str, entity: str
    ) -> None:
        desired = _desired(obj.kind, spec)
        active = sorted(
            self._list(f"/{collection}", key=obj.key, status="active"), key=lambda i: i["version"]
        )
        latest = self._get(f"/{collection}/{active[-1]['id']}") if active else None
        expected = {"active": [item["version"] for item in active]}
        if latest is not None and not _differences(obj.kind, desired, latest):
            keep = latest
            self._say(obj.ref, f"v{latest['version']} без изменений")
        else:
            changed = _differences(obj.kind, desired, latest) if latest is not None else []
            reason = "нет в tenant" if latest is None else "изменились " + ", ".join(changed)
            self._change(
                obj.kind,
                obj.key,
                "create" if latest is None else "version",
                f"новая версия ({reason})",
                package=obj.package,
                fields=changed,
                expected=expected,
            )
            if self.dry_run:
                self._say(obj.ref, f"новая версия ({reason})")
                keep = None
            else:
                keep = self._write("POST", f"/{collection}", {"key": obj.key, **spec})
                if obj.kind == "TaskType" and spec.get("execution") and not keep.get("execution"):
                    raise PackageError(
                        f"{obj.ref}: control-plane не сохранил execution — релиз ядра старше CP-ADR-0056 §3"
                    )
                self._say(obj.ref, f"опубликована v{keep['version']} ({reason})")
        for item in active:
            if keep is None or item["id"] != keep["id"]:
                self._change(
                    obj.kind,
                    obj.key,
                    "deprecate",
                    f"v{item['version']} → deprecated",
                    package=obj.package,
                    expected=expected,
                    version=item["version"],
                )
                if not self.dry_run:
                    self._write("POST", f"/{collection}/{item['id']}:deprecate")
                self._say(obj.ref, f"v{item['version']} → deprecated")
        if keep is not None:
            self.result[obj.ref] = {"id": keep["id"], "version": keep["version"]}

    def _apply_ArtifactType(self, obj: Obj, spec: dict[str, Any]) -> None:
        """Версии неизменяемы и из оборота не выводятся (CP-ADR-0072 §6: у API нет
        :deprecate) — новая версия публикуется, только если файл отличается от последней."""
        desired = _desired(obj.kind, spec)
        versions = self._list("/artifact-types", key=obj.key)
        latest = max(versions, key=lambda i: i["version"]) if versions else None
        changed = _differences(obj.kind, desired, latest) if latest is not None else []
        if latest is not None and not changed:
            keep = latest
            self._say(obj.ref, f"v{latest['version']} без изменений")
        else:
            reason = "нет в tenant" if latest is None else "изменились " + ", ".join(changed)
            self._change(
                obj.kind,
                obj.key,
                "create" if latest is None else "version",
                f"новая версия ({reason})",
                package=obj.package,
                fields=changed,
                expected={"latest": latest["version"] if latest is not None else None},
            )
            if self.dry_run:
                self._say(obj.ref, f"новая версия ({reason})")
                return
            keep = self._write("POST", "/artifact-types", {"key": obj.key, **desired})
            self._say(obj.ref, f"опубликована v{keep['version']} ({reason})")
        self.result[obj.ref] = {"id": keep["id"], "version": keep["version"]}

    def _apply_Agent(self, obj: Obj, spec: dict[str, Any]) -> None:
        """Ревизию считает ядро по хэшу описания (CP-ADR-0073): сначала :validate — без
        изменений ничего не пишется; state и число экземпляров меняются без новой ревизии.
        package — источник ревизии (CP-ADR-0074, амендмент TASK-000904): в хэш описания не входит."""
        body: dict[str, Any] = {"key": obj.key, "spec": spec}
        if obj.package in self._packages:
            body["package"] = package_ref(self._packages[obj.package])
        check = self._write("POST", "/agents:validate", body)
        current = check.get("currentRevision")
        if not check.get("wouldCreateRevision") and not check.get("wouldChangeState"):
            self._say(obj.ref, f"ревизия {current} без изменений")
            self.result[obj.ref] = {"revision": current}
            return
        reason = (
            "нет в tenant"
            if current is None
            else "новая ревизия"
            if check.get("wouldCreateRevision")
            else "меняется состояние"
        )
        self._change(
            obj.kind,
            obj.key,
            "create"
            if current is None
            else "version"
            if check.get("wouldCreateRevision")
            else "patch",
            reason,
            package=obj.package,
            fields=[] if current is None or check.get("wouldCreateRevision") else ["state"],
            expected={"revision": current},
        )
        if self.dry_run:
            self._say(obj.ref, reason)
            return
        agent = self._write("POST", "/agents", body)
        what = f"ревизия {agent['currentRevision']}" + (
            f" ({reason})" if reason != "новая ревизия" else ""
        )
        self._say(obj.ref, f"опубликована {what}, {agent['state']} × {agent['replicas']}")
        self.result[obj.ref] = {"id": agent["id"], "revision": agent["currentRevision"]}

    def _apply_TaskType(self, obj: Obj, spec: dict[str, Any]) -> None:
        self._apply_versioned(obj, spec, "task-types", "task_type")

    def _apply_ProjectTemplate(self, obj: Obj, spec: dict[str, Any]) -> None:
        self._apply_versioned(obj, spec, "project-templates", "project_template")

    def _retire(self, kind: str, key: str) -> None:
        if kind == "NotificationRule":
            current = next(
                (r for r in self._notify_list(key=key, includeRetired="true") if r["key"] == key),
                None,
            )
            if current is None:
                self._say(f"{kind}/{key}", "нет в сервисе уведомлений")
            elif current.get("state") == "retired":
                self._say(f"{kind}/{key}", "уже выведено из оборота")
            else:
                self._change(
                    kind,
                    key,
                    "retire",
                    "уведомлений по правилу больше нет, отправленные остаются",
                    expected={"version": current["version"]},
                )
                if not self.dry_run:
                    self._notify_call("POST", f"/notification-rules/{key}:retire")
                self._say(
                    f"{kind}/{key}",
                    f"v{current['version']} → retired: уведомлений по нему больше нет, "
                    "отправленные остаются",
                )
            return
        if kind == "Agent":
            try:
                agent = self._get(f"/agents/{key}")
            except Exception as error:
                if "404" not in str(error):
                    raise
                self._say(f"{kind}/{key}", "нет в tenant")
                return
            if agent.get("status") == "retired":
                self._say(f"{kind}/{key}", "уже выведен из оборота")
                return
            self._change(
                kind,
                key,
                "retire",
                "исполнитель остановится, credential будет отозван",
                expected={"revision": agent.get("currentRevision")},
            )
            if not self.dry_run:
                self._write(
                    "POST", f"/agents/{key}:retire", {"reason": "retire в установке пакетов"}
                )
            self._say(f"{kind}/{key}", "→ retired: исполнитель остановлен, credential отозван")
            return
        if kind == "WorkRule":
            # DELETE архивирует: правило больше не оценивается, заведённая им работа остаётся.
            live = [r for r in self._list("/rules", key=key) if r.get("status") != "archived"]
            if not live:
                self._say(f"{kind}/{key}", "уже в архиве")
            for rule in live:
                self._change(
                    kind,
                    key,
                    "retire",
                    "правило уйдёт в архив, заведённая им работа останется",
                    expected={"version": rule.get("version")},
                )
                if not self.dry_run:
                    self._write("DELETE", f"/rules/{rule['id']}")
                self._say(f"{kind}/{key}", "→ archived (retire)")
            return
        collection = {"TaskType": "task-types", "ProjectTemplate": "project-templates"}[kind]
        active = self._list(f"/{collection}", key=key, status="active")
        if not active:
            self._say(f"{kind}/{key}", "уже выведен из оборота")
        for item in sorted(active, key=lambda i: i["version"]):
            self._change(
                kind,
                key,
                "deprecate",
                f"v{item['version']} → deprecated (retire)",
                expected={"active": sorted(i["version"] for i in active)},
                version=item["version"],
            )
            if not self.dry_run:
                self._write("POST", f"/{collection}/{item['id']}:deprecate")
            self._say(f"{kind}/{key}", f"v{item['version']} → deprecated (retire)")

    # изменяемые: WorkspaceType, Role
    def _apply_mutable(
        self,
        obj: Obj,
        spec: dict[str, Any],
        current: dict | None,
        create_path: str,
        update_path: Callable[[dict], str],
        etag: Callable[[dict], str],
    ) -> None:
        desired = _desired(obj.kind, spec)
        identity = IDENTITY[obj.kind]
        if current is None:
            self._change(obj.kind, obj.key, "create", "будет создан", package=obj.package)
            if self.dry_run:
                self._say(obj.ref, "будет создан")
                return
            current = self._write("POST", create_path, {identity: obj.key, **desired})
            self._say(obj.ref, "создан")
        else:
            changed = _differences(obj.kind, desired, current)
            if changed:
                self._change(
                    obj.kind,
                    obj.key,
                    "patch",
                    "изменятся " + ", ".join(changed),
                    package=obj.package,
                    fields=changed,
                    expected={"version": current.get("version")},
                )
            if not changed:
                self._say(obj.ref, "без изменений")
            elif self.dry_run:
                self._say(obj.ref, "изменятся " + ", ".join(changed))
            else:
                current = self._write(
                    "PATCH",
                    update_path(current),
                    {name: desired[name] for name in changed},
                    {"If-Match": etag(current)},
                )
                self._say(obj.ref, "обновлены " + ", ".join(changed))
        self.result[obj.ref] = {"id": current["id"]}

    def _apply_WorkspaceType(self, obj: Obj, spec: dict[str, Any]) -> None:
        current = next((t for t in self._list("/workspace-types") if t["key"] == obj.key), None)
        if current is not None and current.get("status") != "active":
            raise PackageError(
                f"{obj.ref}: тип workspace в статусе {current.get('status')} — вернуть его пакет не может"
            )
        self._apply_mutable(
            obj,
            spec,
            current,
            "/workspace-types",
            lambda c: f"/workspace-types/{c['id']}",
            lambda c: f'"workspace_type-{c["version"]}"',
        )

    def _apply_Role(self, obj: Obj, spec: dict[str, Any]) -> None:
        # Роли пакета — уровня tenant; роль workspace с тем же slug — чужая топология.
        current = next(
            (
                r
                for r in self._list("/roles")
                if r["slug"] == obj.key and r.get("workspaceId") is None
            ),
            None,
        )
        self._apply_mutable(
            obj,
            spec,
            current,
            "/roles",
            lambda c: f"/roles/{c['id']}",
            lambda c: f'"role-{c["version"]}"',
        )

    # только добавление: Capability
    def _apply_Capability(self, obj: Obj, spec: dict[str, Any]) -> None:
        current = next((c for c in self._list("/capabilities") if c["name"] == obj.key), None)
        description = spec.get("description", "")
        if current is None:
            self._change(obj.kind, obj.key, "create", "будет создана", package=obj.package)
            if self.dry_run:
                self._say(obj.ref, "будет создана")
                return
            current = self._write(
                "POST", "/capabilities", {"name": obj.key, "description": description}
            )
            self._say(obj.ref, "создана")
        elif current.get("description", "") != description:
            self._say(
                obj.ref, "!! описание в tenant отличается, а API его не меняет — оставлено как есть"
            )
        else:
            self._say(obj.ref, "без изменений")
        self.result[obj.ref] = {"id": current["id"]}

    # Skill: версия задана пакетом, контракт неизменяем
    def _apply_Skill(self, obj: Obj, spec: dict[str, Any]) -> None:
        version = spec["version"]
        current = next(
            (s for s in self._list("/skills", name=obj.key) if s.get("version") == version), None
        )
        if current is None:
            self._change(
                obj.kind,
                obj.key,
                "create",
                f"версия {version} будет зарегистрирована",
                package=obj.package,
                version=version,
            )
            if self.dry_run:
                self._say(obj.ref, "будет зарегистрирован")
                return
            current = self._write("POST", "/skills", {"name": obj.key, **spec})
            self._say(obj.ref, "зарегистрирован")
            self.result[obj.ref] = {"id": current["id"]}
            return
        current = self._get(f"/skills/{current['id']}")
        immutable = []
        endpoint = None
        for name in SKILL_IMMUTABLE:
            if name not in spec:
                continue
            wanted = spec[name]
            if name == "contract":
                domain = _domain()
                if domain is not None:
                    wanted = domain.skill_contract.normalize_contract(wanted)
                same = is_subset(wanted, current.get(name))
                if not same:
                    # адрес реализации — свойство инсталляции, не версии (амендмент ADR-0056
                    # от 2026-09-29): отличие только в нём переводится PATCH'ем
                    endpoint = _moved_endpoint(wanted, current.get(name))
                    same = endpoint is not None
            else:
                same = canonical(wanted) == canonical(current.get(name))
            if not same:
                immutable.append(name)
        if immutable:
            raise PackageError(
                f"{obj.ref}: в опубликованной версии отличаются {', '.join(immutable)} — "
                "контракт версии неизменяем, поднимите spec.version"
            )
        changes = {
            name: spec[name]
            for name in SKILL_MUTABLE
            if name in spec
            and not (spec.get("contract") and name in ("inputSchema", "outputSchema"))
            and canonical(spec[name]) != canonical(current.get(name))
        }
        if endpoint is not None:
            changes["endpoint"] = endpoint
        if changes:
            self._change(
                obj.kind,
                obj.key,
                "patch",
                "изменятся " + ", ".join(changes),
                package=obj.package,
                fields=list(changes),
                expected={"rowVersion": current.get("rowVersion")},
                version=version,
            )
        if not changes:
            self._say(obj.ref, "без изменений")
        elif self.dry_run:
            self._say(obj.ref, "изменятся " + ", ".join(changes))
        else:
            current = self._write(
                "PATCH",
                f"/skills/{current['id']}",
                changes,
                {"If-Match": f'"skill-{current["rowVersion"]}"'},
            )
            self._say(obj.ref, "обновлены " + ", ".join(changes))
        self.result[obj.ref] = {"id": current["id"]}

    # WorkRule: изменяемый, как WorkspaceType; статус — через :enable / :disable
    def _apply_WorkRule(self, obj: Obj, spec: dict[str, Any]) -> None:
        live = [r for r in self._list("/rules", key=obj.key) if r.get("status") != "archived"]
        current = live[0] if live else None
        status = spec.get("status", "enabled")
        body = {name: spec[name] for name in RULE_MUTABLE if name in spec}
        workspace = spec.get("workspaceId")
        if current is None:
            self._change(
                obj.kind, obj.key, "create", f"будет создано ({status})", package=obj.package
            )
            if self.dry_run:
                self._say(obj.ref, f"будет создано ({status})")
                return
            payload = {"key": obj.key, **body, "status": status}
            if workspace:
                payload["workspaceId"] = workspace
            current = self._rule_write(obj, "POST", "/rules", payload)
            self._say(obj.ref, f"создано ({current['status']}, v{current['version']})")
            self.result[obj.ref] = {"id": current["id"], "version": current["version"]}
            return
        if (workspace or None) != current.get("workspaceId"):
            raise PackageError(
                f"{obj.ref}: workspaceId правила неизменяем (в tenant {current.get('workspaceId')}, "
                f"в пакете {workspace}) — выведите правило через retire и заведите заново"
            )
        domain = _domain()
        wanted = (
            normalize_rule(spec, domain)
            if domain is not None
            else {
                "description": spec.get("description", ""),
                "condition": spec.get("condition", True),
                **body,
            }
        )
        changed = [
            name
            for name in RULE_MUTABLE
            if canonical(wanted.get(name)) != canonical(current.get(name))
        ]
        expected = {"version": current.get("version"), "status": current.get("status")}
        if changed:
            self._change(
                obj.kind,
                obj.key,
                "patch",
                "изменятся " + ", ".join(changed),
                package=obj.package,
                fields=changed,
                expected=expected,
            )
        if not changed:
            self._say(obj.ref, f"v{current['version']} без изменений")
        elif self.dry_run:
            self._say(obj.ref, "изменятся " + ", ".join(changed))
        else:
            current = self._rule_write(
                obj,
                "PATCH",
                f"/rules/{current['id']}",
                {name: wanted[name] for name in changed},
                {"If-Match": f'"rule-{current["version"]}"'},
            )
            self._say(obj.ref, f"обновлены {', '.join(changed)} → v{current['version']}")
        if current.get("status") != status:
            verb = "enable" if status == "enabled" else "disable"
            self._change(
                obj.kind, obj.key, verb, f"→ {status}", package=obj.package, expected=expected
            )
            if not self.dry_run:
                current = self._write("POST", f"/rules/{current['id']}:{verb}")
            self._say(obj.ref, f"→ {status}")
        self.result[obj.ref] = {"id": current["id"], "version": current["version"]}

    def _rule_write(
        self, obj: Obj, method: str, path: str, body: dict, extra: dict | None = None
    ) -> dict:
        try:
            return self._write(method, path, body, extra)
        except RuntimeError as error:
            # Контракт identity и agent:<key> опубликован раньше реализации (declarative-cycle
            # C002): до C005/C006 ядро отвечает 501 not_implemented и ничего не пишет.
            if "HTTP 501" in str(error):
                raise PackageError(
                    f"{obj.ref}: ядро ещё не исполняет поле правила — {error}"
                ) from error
            raise


EXPORT_FIELDS = {
    "TaskType": (
        "displayName",
        "description",
        "fieldSchema",
        "lifecycleSchema",
        "execution",
        "approvalSchema",
        "contextSchema",
        "instructions",
        "completionSchema",
        "artifactSchema",
        "acceptance",
    ),
    "ArtifactType": ("displayName", "description", "metadataSchema", "mediaTypes", "maxBytes"),
    # Agent выгружается из ревизии; state и число экземпляров — из желаемого состояния агента
    "Agent": (
        "displayName",
        "description",
        "identity",
        "work",
        "executor",
        "workingCopy",
        "skills",
        "placement",
        "state",
    ),
    "ProjectTemplate": (
        "displayName",
        "description",
        "fieldSchema",
        "lifecycleSchema",
        "defaultConfig",
        "defaultViews",
        "governanceSchema",
        "memoryDefaults",
    ),
    "WorkspaceType": ("displayName", "description", "fieldSchema", "allowedChildTypes"),
    "Role": ("name", "description"),
    "Capability": ("description",),
    "Skill": (
        "version",
        "description",
        "protocol",
        "config",
        "inputSchema",
        "outputSchema",
        "sideEffects",
        "riskLevel",
        "contract",
    ),
    # workspaceId — топология установки, в пакет не выгружается
    "WorkRule": (
        "description",
        "trigger",
        "condition",
        "interpretation",
        "action",
        "identity",
        "status",
    ),
    # спецификация хранится сервисом как применена (ADR-0005 §5) — выгружается как есть
    "NotificationRule": (
        "description",
        "on",
        "recipient",
        "notification",
        "dedupKeyTemplate",
        "close",
        "status",
    ),
}


def _fetch(applier: Applier, kind: str, key: str, version: str | None) -> dict:
    if kind == "NotificationRule":
        if version:
            raise PackageError(
                "NotificationRule выгружается только действующей версией — без --version"
            )
        current = next((r for r in applier._notify_list(key=key) if r["key"] == key), None)
        if current is None:
            raise PackageError(f"{kind}/{key} не найден в сервисе уведомлений")
        return {**current["spec"], "version": current["version"]}
    if kind in ("TaskType", "ProjectTemplate"):
        collection = "task-types" if kind == "TaskType" else "project-templates"
        items = applier._list(f"/{collection}", key=key)
        if version:
            items = [i for i in items if str(i["version"]) == version]
        else:
            items = [i for i in items if i.get("status") == "active"] or items
        if not items:
            raise PackageError(f"{kind}/{key}{'@' + version if version else ''} не найден")
        return applier._get(f"/{collection}/{max(items, key=lambda i: i['version'])['id']}")
    if kind == "Agent":
        try:
            return agent_body(applier._get(f"/agents/{key}{'@' + version if version else ''}"))
        except Exception as error:
            if "404" not in str(error):
                raise
            raise PackageError(
                f"{kind}/{key}{'@' + version if version else ''} не найден"
            ) from error
    if kind == "ArtifactType":
        try:
            return applier._get(f"/artifact-types/{key}{'@' + version if version else ''}")
        except Exception as error:  # 404 — не найден, остальное пусть видно как есть
            if "404" not in str(error):
                raise
            raise PackageError(
                f"{kind}/{key}{'@' + version if version else ''} не найден"
            ) from error
    if kind == "WorkspaceType":
        found = [t for t in applier._list("/workspace-types") if t["key"] == key]
    elif kind == "Role":
        found = [r for r in applier._list("/roles") if r["slug"] == key]
    elif kind == "Capability":
        found = [c for c in applier._list("/capabilities") if c["name"] == key]
    elif kind == "WorkRule":
        found = [r for r in applier._list("/rules", key=key) if r.get("status") != "archived"]
    else:
        found = [
            s
            for s in applier._list("/skills", name=key)
            if not version or s.get("version") == version
        ]
        if found:
            found = [applier._get(f"/skills/{found[-1]['id']}")]
    if not found:
        raise PackageError(f"{kind}/{key} не найден")
    return found[-1]


def agent_body(agent: dict) -> dict:
    """Описание агента, как в пакете: спецификация ревизии плюс желаемое состояние."""
    spec = copy.deepcopy(agent["revision"]["spec"])
    # умолчания схемы (running, один экземпляр) в файл не пишутся — как в пакетах
    if agent["state"] != "running":
        spec["state"] = agent["state"]
    if isinstance(spec.get("placement"), dict) and agent["replicas"] != 1:
        spec["placement"]["replicas"] = agent["replicas"]
    return spec


def to_document(kind: str, key: str, body: dict) -> dict:
    spec: dict[str, Any] = {}
    for name in EXPORT_FIELDS[kind]:
        value = body.get(name)
        if value in (None, "", {}, []) and name not in ("fieldSchema", "approvalSchema"):
            continue
        if (
            kind == "Skill"
            and body.get("contract")
            and name in ("inputSchema", "outputSchema", "protocol")
        ):
            continue  # у скилла с контрактом они выводятся из контракта
        spec[name] = value
    if kind == "Skill" and spec.get("contract"):
        # Контракт на сервере нормализован: пустые значения по умолчанию в файле не нужны.
        contract = {k: v for k, v in spec["contract"].items() if v not in (None, [])}
        contract["implementation"] = {
            k: v for k, v in (contract.get("implementation") or {}).items() if v is not None
        }
        spec["contract"] = contract
    return {"apiVersion": API_VERSION, "kind": kind, "key": key, "spec": spec}


class _Dumper(yaml12.SafeDumper if yaml12 else object):  # type: ignore[misc]
    """Писатель файла пакета (export, pull): строку, которую ядро, YAML 1.1 или YAML 1.2
    прочтут не строкой (``0o12``, ``1e3``, ``yes``), — в кавычках (``yaml12.SafeDumper``),
    многострочную — блоком."""


def _str_presenter(dumper: Any, data: str) -> Any:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


if yaml is not None:
    _Dumper.add_representer(str, _str_presenter)


def dump_document(document: dict, schema_rel: str) -> str:
    header = f"# yaml-language-server: $schema={schema_rel}\n"
    return header + yaml.dump(
        document, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=100
    )
