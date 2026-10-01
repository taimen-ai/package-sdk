"""Единый план установки ``package-sdk.plan/v1`` (TAI-ADR-0062 п.7, plan Р5).

План — один документ изменений всех видов установки, построенный без единой записи:

- ``catalog`` — виды, которые ставит установщик обычными ресурсами ядра (роли, скиллы,
  типы артефактов, …), как данные: ``{package, kind, key, operation, fields, expected}``;
- ``core`` — по пакету с процессами или календарями ответ ядра ``POST /packages:plan`` с его
  ``planHash``. Ядро планирует и ставит у такого пакета все свои виды (``CORE_PLANNED_KINDS``:
  типы задач, агенты, календари, процессы, правила вывода работы — CP-ADR-0074 п.11,
  амендмент 2026-09-29), поэтому в секцию ``catalog`` они у него не входят;
- ``knowledge`` — регистрация онтологий пакетов и итоговые наборы онтологий пространств
  работы с текущими;
- ``notification-rules`` — правила уведомлений, прошедшие ``:validate`` сервиса;
- ``retire`` — вывод из оборота, в том числе процессов и календарей (``:retire?dryRun=true``
  ядра: сколько живых экземпляров доживёт).

Кроме секций в плане: версия ядра стенда из его ``openapi.json`` (``engines``), хэш фиксации
источников (``lockHash``), хэш значений переменных установки (``variablesHash`` — сами
значения в план не пишутся), флаг ``overwriteConsole`` и ``planHash`` — хэш документа без
самого поля: правленый файл плана применение отвергает.

``overwriteConsole`` (``plan --overwrite-console``) — перезаписать поля объектов ядра, которые
человек правил в консоли после прошлого применения; без флага ядро их сохраняет. Флаг идёт в
запрос плана ядра (он входит и в ``planHash`` ядра) и с ним же — в ``/packages:apply``. Поля с
правками консоли план показывает отдельным списком: какие будут перезаписаны, какие останутся.

Проверки до записи: пакеты проходят ``check``; версия ядра — в диапазоне ``engines`` каждого
пакета; обязательные переменные заданы, значения подходят своему виду, а UUID пространства
работы, проекта, principal'а и роли существуют на стенде.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from package_sdk import schema as schema_module
from package_sdk.apply import OPENAPI_PATH, Applier, HttpError, HttpLike, knowledge_targets
from package_sdk.auth import Authorized, TokenSource
from package_sdk.check import check
from package_sdk.core import (
    CORE_PLANNED_KINDS,
    ProcessApi,
    core_errors,
    print_plan,
)
from package_sdk.install.lock import GitCache, Sources, load
from package_sdk.manifest import (
    missing_variable,
    package_env,
    satisfies,
    variable_uses,
    variable_value_error,
)
from package_sdk.model import (
    API,
    DEFAULT_REPLAY_LIMIT,
    ENV_REF,
    PLAN_KINDS,
    Installation,
    Package,
    PackageError,
    _rel,
    canonical,
)
from package_sdk.source import plan_request

PLAN_FORMAT = "package-sdk.plan/v1"
CORE_COMPONENT = "control-plane"
SECTIONS = ("catalog", "core", "knowledge", "notification-rules", "retire")
# Поля изменения, которые поясняют человеку и в сверку «план устарел» не входят: живые
# экземпляры выводимого процесса меняются сами, и это не расхождение стенда с планом.
INFO_KEYS = frozenset({"detail", "openInstances", "byVersion", "current", "after"})
# Вид переменной, значение которой — UUID объекта стенда, → где его прочитать.
VARIABLE_LOOKUP = {
    "workspace": "/workspaces/{id}",
    "project": "/projects/{id}",
    "principal": "/principals/{id}",
    "role": "/roles/{id}",
}
RETIRE_REASON = "retire в установке пакетов"
# Стенд плана — https; http только на своей машине (как pattern server в plan.schema.json).
SERVER = re.compile(
    r"^(https://[^\s/@]+(/\S*)?|http://(localhost|127\.0\.0\.1|\[::1\])(:[0-9]+)?(/\S*)?)$"
)


def check_server(server: str) -> str:
    server = server.rstrip("/")
    if not SERVER.match(server):
        raise PackageError(
            f"стенд {server!r}: нужен https://… (http — только localhost), без учётных данных"
        )
    return server


@dataclass
class Target:
    """Стенд: Control Plane и, если установка его касается, сервис уведомлений.

    Учётка — поставщик токена ``token`` (``package_sdk.auth``: ``Bearer``, функция → str или
    строка — статический токен): транспорт ``http`` оборачивается в ``Authorized`` и берёт
    ``Authorization`` перед каждым запросом, на ``401 invalid_credentials`` — один повтор с
    обновлённым токеном. ``headers`` — прочие заголовки; готовый ``Authorization`` в них —
    прежняя форма (токен на весь прогон), с ``token`` вместе не задаётся. Так же ``notify``:
    ``(http, поставщик токена)`` или прежнее ``(http, заголовки)``."""

    server: str
    http: HttpLike
    # учётка — ни строкой токена, ни заголовком — не попадает в repr (логи, трассы pytest)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    notify: tuple[HttpLike, dict[str, str] | TokenSource] | None = field(default=None, repr=False)
    token: TokenSource | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.token is not None:
            if any(name.lower() == "authorization" for name in self.headers):
                raise ValueError("Target: учётка — либо token, либо Authorization в headers")
            # dataclasses.replace(target) передаёт уже обёрнутый транспорт: оборачивается
            # исходный, иначе на 401 повторов стало бы два (по одному на обёртку)
            inner = self.http.http if isinstance(self.http, Authorized) else self.http
            self.http = Authorized(inner, self.token)

    def get(self, path: str) -> Any:
        return self.http.call("GET", API + path, None, self.headers)

    def notify_transport(self) -> tuple[HttpLike | None, dict[str, str]]:
        """Сервис уведомлений: транспорт и прочие заголовки; поставщик токена — в транспорте."""
        if self.notify is None:
            return None, {}
        http, auth = self.notify
        if isinstance(auth, dict):
            return http, dict(auth)
        return Authorized(http, auth), {}

    def applier(
        self, env: Mapping[str, str], *, dry_run: bool, log: Callable[[str], None]
    ) -> Applier:
        notify_http, notify_headers = self.notify_transport()
        return Applier(
            self.http,
            self.headers,
            env=dict(env),
            dry_run=dry_run,
            log=log,
            notify=notify_http,
            notify_headers=notify_headers,
        )

    def process_api(self) -> ProcessApi:
        return ProcessApi(self.http, self.headers)


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def document_hash(document: Mapping[str, Any]) -> str:
    """planHash: хэш документа без самого поля."""
    return digest({k: v for k, v in document.items() if k != "planHash"})


def comparable(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Изменения без пояснений — то, что сверяется перед записью."""
    return [{k: v for k, v in change.items() if k not in INFO_KEYS} for change in changes]


# --- версия ядра и совместимость (FR-012) ------------------------------------------------


def core_version(target: Target) -> str:
    """Версия ядра стенда — info.version его openapi.json (TAI-ADR-0062 п.4)."""
    try:
        document = target.http.call("GET", OPENAPI_PATH, None, target.headers)
    except (RuntimeError, OSError) as error:
        raise PackageError(
            f"версия ядра не прочитана ({OPENAPI_PATH}: {error}) — совместимость пакетов "
            "(engines) без неё не проверить, план не строится"
        ) from error
    version = ((document or {}).get("info") or {}).get("version")
    if not isinstance(version, str) or not version:
        raise PackageError(f"{OPENAPI_PATH} ядра без info.version — совместимость не проверить")
    return version


def engines_problems(installation: Installation, version: str) -> tuple[list[str], list[str]]:
    """Пакеты, объявившие диапазон control-plane, в который версия стенда не входит; прочие
    компоненты по стенду план не проверяет — это предупреждение."""
    errors: list[str] = []
    warnings: list[str] = []
    for package in installation.packages:
        where = _rel(package.path / "package.yaml")
        for component, spec in (package.spec.get("engines") or {}).items():
            if component != CORE_COMPONENT:
                warnings.append(
                    f"{where}: engines {component} {spec} — версию {component} стенда план не "
                    "читает, совместимость не проверена"
                )
                continue
            if not satisfies(version, str(spec)):
                errors.append(
                    f"{where}: engines_incompatible: пакет {package.key} совместим с "
                    f"{component} {spec}, а на стенде {component} {version}"
                )
    return errors, warnings


# --- переменные установки (FR-010) -------------------------------------------------------


def _declared(package: Package) -> dict[str, Any]:
    declared = package.spec.get("variables") or {}
    return {k: v for k, v in declared.items() if isinstance(v, dict)}


def variable_values(installation: Installation, env: Mapping[str, str]) -> dict[str, Any]:
    """Значения переменных, которые берёт установка: по пакету — каждая объявленная и каждая
    использованная; у установки — ${…} включения онтологий. Незаданная обязательная — отказ
    с её описанием, без заглушки."""
    missing: list[str] = []
    packages: dict[str, dict[str, str]] = {}
    for package in installation.packages:
        values = package_env(package, dict(env))
        names = sorted(set(_declared(package)) | set(variable_uses(package)))
        packages[package.key] = {}
        for name in names:
            if name in values:
                packages[package.key][name] = values[name]
            else:
                missing.append(missing_variable(package, name))
    own: dict[str, str] = {}
    for entry in installation.knowledge:
        for name in ENV_REF.findall(str(entry.get("workspace") or "")):
            if name in env:
                own[name] = env[name]
            else:
                missing.append(
                    f"переменная {name} не задана — установка: workspace включения онтологий"
                )
    if missing:
        raise PackageError(
            "не заданы переменные установки:\n  " + "\n  ".join(dict.fromkeys(missing))
        )
    return {"packages": packages, "installation": own}


def variable_problems(
    installation: Installation, values: Mapping[str, Any], target: Target
) -> list[str]:
    """Значение подходит виду переменной; UUID объекта стенда существует (plan Р3)."""
    problems: list[str] = []
    checked: dict[tuple[str, str], str | None] = {}
    for package in installation.packages:
        for name, declared in sorted(_declared(package).items()):
            value = values["packages"][package.key].get(name)
            if not value:
                continue
            kind = str(declared.get("kind", "string"))
            problem = variable_value_error(kind, value)
            if problem:
                problems.append(f"переменная {name} пакета {package.key}: {value!r} — {problem}")
                continue
            if kind not in VARIABLE_LOOKUP:
                continue
            if (kind, value) not in checked:
                path = VARIABLE_LOOKUP[kind].format(id=urllib.parse.quote(value, safe=""))
                try:
                    target.get(path)
                    checked[(kind, value)] = None
                except HttpError as error:
                    if error.status != 404:
                        raise
                    checked[(kind, value)] = f"{kind} {value} не найден на стенде"
            if checked[(kind, value)]:
                problems.append(f"переменная {name} пакета {package.key}: {checked[(kind, value)]}")
    return problems


# --- секции ------------------------------------------------------------------------------


def core_packages(installation: Installation) -> list[Package]:
    """Пакеты, которые ставит план ядра: с процессами или календарями."""
    return [p for p in installation.packages if any(o.kind in PLAN_KINDS for o in p.objects)]


def owned_by_core(installation: Installation) -> dict[str, frozenset[str]]:
    return {p.key: frozenset(CORE_PLANNED_KINDS) for p in core_packages(installation)}


def _quiet(lines: list[str]) -> Callable[[str], None]:
    """Журнал установщика в режиме плана: предупреждения — человеку, остальное — в план."""

    def log(line: str) -> None:
        if line.lstrip().startswith("!") or "!!" in line:
            lines.append(line)

    return log


def catalog_section(
    installation: Installation, target: Target, env: Mapping[str, str], warnings: list[str]
) -> dict[str, Any]:
    applier = target.applier(env, dry_run=True, log=_quiet(warnings))
    applier.catalog(installation, owned_by_core(installation))
    return {"kind": "catalog", "changes": applier.changes}


def core_section(
    package: Package,
    target: Target,
    env: Mapping[str, str],
    *,
    workspace: str | None,
    replay_limit: int,
    overwrite_console: bool = False,
) -> dict[str, Any]:
    request = plan_request(
        package,
        dict(env),
        workspace=workspace,
        replay_limit=replay_limit,
        overwrite_console=overwrite_console,
    )
    response = target.process_api().plan(request)
    if not response.get("planHash"):
        raise PackageError(f"ядро не вернуло planHash пакета {package.key} — применять нечего")
    section: dict[str, Any] = {
        "kind": "core",
        "package": package.key,
        "planHash": response["planHash"],
        "plan": response,
        "replayLimit": replay_limit,
    }
    if workspace:
        section["workspaceId"] = workspace
    return section


def _pack_ref(spec: Mapping[str, Any]) -> str:
    prefix = "tenant:" if spec.get("scope") == "tenant" else ""
    return f"{prefix}{spec.get('name')}@{spec.get('version')}"


def _names(items: Any, field_name: str) -> set[str]:
    return {
        str(item[field_name])
        for item in items or []
        if isinstance(item, dict) and field_name in item
    }


def knowledge_section(
    installation: Installation, target: Target, env: Mapping[str, str]
) -> dict[str, Any]:
    """Регистрация онтологий, которых на стенде нет, и наборы онтологий пространств работы,
    которые отличаются от текущих (PUT заменяет набор целиком)."""
    register: list[dict[str, Any]] = []
    for obj in installation.objects:
        if obj.kind != "KnowledgePack":
            continue
        ref = _pack_ref(obj.spec)
        try:
            current = target.get(f"/knowledge/packs/{urllib.parse.quote(ref, safe=':@')}")
        except HttpError as error:
            if error.status != 404:
                raise
            register.append(
                {
                    "package": obj.package,
                    "kind": "KnowledgePack",
                    "key": obj.key,
                    "operation": "register",
                    "version": str(obj.spec.get("version")),
                    "detail": f"онтология {ref} будет зарегистрирована",
                }
            )
            continue
        differs = [
            name
            for name, field_name in (("kinds", "kind"), ("relations", "relation"))
            if _names(obj.spec.get(name), field_name) != _names(current.get(name), field_name)
        ]
        if differs:
            raise PackageError(
                f"{_rel(obj.path)}: онтология {ref} уже зарегистрирована, а {', '.join(differs)} "
                "в пакете другие — версия онтологии неизменяема, поднимите version"
            )
    enable: list[dict[str, Any]] = []
    for entry in knowledge_targets(installation, dict(env)):
        workspace = entry["workspace"]
        current = target.get(
            f"/workspaces/{urllib.parse.quote(workspace, safe='')}/knowledge-packs"
        )
        packs = [str(p) for p in current.get("packs") or []]
        now: dict[str, Any] = {
            "configured": bool(current.get("configured", True)),
            "packs": packs,
            "strict": bool(current.get("strict")),
        }
        if (
            now["configured"]
            and sorted(packs) == sorted(entry["packs"])
            and now["strict"] == entry["strict"]
        ):
            continue
        enable.append(
            {
                "workspace": workspace,
                "packs": list(entry["packs"]),
                "strict": entry["strict"],
                "current": now["packs"],
                "expected": now,
            }
        )
    return {"kind": "knowledge", "register": register, "enable": enable}


def notification_section(
    installation: Installation, target: Target, env: Mapping[str, str], warnings: list[str]
) -> dict[str, Any]:
    applier = target.applier(env, dry_run=True, log=_quiet(warnings))
    applier.notification_rules(installation)
    return {"kind": "notification-rules", "changes": applier.changes}


def _retire_core(target: Target, kind: str, key: str, retiring: set[str]) -> dict[str, Any] | None:
    """Process или Calendar: судьба ключа по ядру (:retire?dryRun=true), None — выводить нечего."""
    collection = "process-definitions" if kind == "Process" else "calendars"
    quoted = urllib.parse.quote(key, safe="")
    try:
        current = target.get(f"/{collection}/{quoted}")
    except HttpError as error:
        if error.status != 404:
            raise
        return None
    if current.get("status") == "retired":
        return None
    try:
        answer = target.http.call(
            "POST",
            f"{API}/{collection}/{quoted}:retire?dryRun=true",
            {"reason": RETIRE_REASON},
            target.headers,
        )
    except HttpError as error:
        body: dict[str, Any] = error.body if isinstance(error.body, dict) else {}
        envelope: dict[str, Any] = body["error"] if isinstance(body.get("error"), dict) else body
        details: dict[str, Any] = envelope.get("details") or {}
        if kind == "Calendar" and envelope.get("code") == "calendar_in_use":
            processes = details.get("processes") or []
            freed = (
                processes
                and details.get("total", len(processes)) == len(processes)
                and all(p.get("key") in retiring and not p.get("openInstances") for p in processes)
            )
            if freed:
                return {
                    "kind": kind,
                    "key": key,
                    "operation": "retire",
                    "expected": {"status": "active"},
                    "after": sorted(f"Process/{p.get('key')}" for p in processes),
                    "detail": "календарь освободится после вывода процессов этого плана",
                }
            names = ", ".join(
                f"{p.get('key')} v{p.get('version')} (живых {p.get('openInstances', 0)})"
                for p in processes
            )
            raise PackageError(
                f"Calendar/{key}: calendar_in_use — календарь нужен процессам: {names or '—'}"
                f" (всего {details.get('total', len(processes))}); выведите их или дождитесь, "
                "пока доживут экземпляры"
            ) from error
        raise
    item: dict[str, Any] = {
        "kind": kind,
        "key": key,
        "operation": "retire",
        "expected": {"status": "active"},
    }
    if kind == "Process":
        opened = int(answer.get("openInstances") or 0)
        item["openInstances"] = opened
        item["byVersion"] = list(answer.get("byVersion") or [])
        item["detail"] = (
            f"новые экземпляры не стартуют, живые доживают: {opened}"
            if opened
            else "новые экземпляры не стартуют, живых нет"
        )
    else:
        item["detail"] = "новые версии процессов на календарь не сошлются"
    return item


def retire_section(
    installation: Installation, target: Target, env: Mapping[str, str], warnings: list[str]
) -> dict[str, Any]:
    applier = target.applier(env, dry_run=True, log=_quiet(warnings))
    applier.retire_keys(installation.retire)
    items = list(applier.changes)
    retiring = set(installation.retire.get("Process") or [])
    for kind in ("Process", "Calendar"):
        for key in installation.retire.get(kind) or []:
            item = _retire_core(target, kind, key, retiring)
            if item is not None:
                items.append(item)
    return {"kind": "retire", "items": items}


def needs_notify(installation: Installation) -> bool:
    return any(o.kind == "NotificationRule" for o in installation.objects) or bool(
        installation.retire.get("NotificationRule")
    )


# --- план --------------------------------------------------------------------------------


@dataclass
class Prepared:
    """Установка, проверенная до плана: источники, версия ядра, переменные."""

    sources: Sources
    version: str
    variables: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    @property
    def installation(self) -> Installation:
        return self.sources.installation


def prepare(
    install: Path,
    *,
    target: Target,
    env: Mapping[str, str],
    cache: GitCache | None = None,
    log: Callable[[str], None] = print,
) -> Prepared:
    """Всё, что проверяется до записи: фиксация источников, check, engines против версии ядра
    стенда, обязательные переменные и их значения."""
    sources = load(install, strict=True, cache=cache, log=log)
    installation = sources.installation
    errors, warnings = check(installation, env=dict(env))
    if errors:
        raise PackageError("пакеты не прошли проверку:\n  " + "\n  ".join(errors))
    version = core_version(target)
    found, engine_warnings = engines_problems(installation, version)
    if found:
        raise PackageError(
            "версия ядра стенда вне совместимости пакетов — план не строится:\n  "
            + "\n  ".join(found)
        )
    variables = variable_values(installation, env)
    problems = variable_problems(installation, variables, target)
    if problems:
        raise PackageError("переменные установки не подходят стенду:\n  " + "\n  ".join(problems))
    if needs_notify(installation) and target.notify is None:
        raise PackageError(
            "в установке есть правила уведомлений — нужен сервис уведомлений "
            "(NOTIFICATION_SERVICE_URL и токен audience notification-service)"
        )
    return Prepared(sources, version, variables, warnings + engine_warnings)


def build_sections(
    prepared: Prepared,
    target: Target,
    env: Mapping[str, str],
    *,
    workspace: str | None,
    replay_limit: int,
    warnings: list[str],
    overwrite_console: bool = False,
) -> list[dict[str, Any]]:
    installation = prepared.installation
    sections = [catalog_section(installation, target, env, warnings)]
    sections += [
        core_section(
            p,
            target,
            env,
            workspace=workspace,
            replay_limit=replay_limit,
            overwrite_console=overwrite_console,
        )
        for p in core_packages(installation)
    ]
    sections.append(knowledge_section(installation, target, env))
    sections.append(notification_section(installation, target, env, warnings))
    sections.append(retire_section(installation, target, env, warnings))
    return sections


def plan(
    install: Path,
    *,
    target: Target,
    env: Mapping[str, str],
    workspace: str | None = None,
    replay_limit: int = DEFAULT_REPLAY_LIMIT,
    overwrite_console: bool = False,
    out: Path | None = None,
    cache: GitCache | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Построить единый план установки; out — записать его файлом. Ничего не пишет на стенд.

    overwrite_console — перезаписать правки консоли в объектах ядра (``overwriteConsole``);
    флаг хранится в плане под его хэшем и с ним же план применяется.

    План с ошибками ядра (например ``migration_required``) не сохраняется: применять его
    нельзя."""
    check_server(target.server)
    prepared = prepare(install, target=target, env=env, cache=cache, log=log)
    warnings = list(prepared.warnings)
    sections = build_sections(
        prepared,
        target,
        env,
        workspace=workspace,
        replay_limit=replay_limit,
        warnings=warnings,
        overwrite_console=overwrite_console,
    )
    for warning in warnings:
        log(f"предупреждение: {warning.strip()}")
    document: dict[str, Any] = {
        "format": PLAN_FORMAT,
        "server": target.server.rstrip("/"),
        "engines": {CORE_COMPONENT: prepared.version},
        "createdAt": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
    }
    if prepared.installation.key:
        document["installation"] = prepared.installation.key
    document["install"] = _install_ref(install, out)
    document["lockHash"] = prepared.sources.lock_hash
    document["variablesHash"] = digest(prepared.variables)
    document["overwriteConsole"] = bool(overwrite_console)
    document["sections"] = sections
    document["planHash"] = document_hash(document)
    clean = True
    for line in format_plan(document):
        log(line)
    for section in sections:
        if section["kind"] == "core" and core_errors(section["plan"]):
            clean = False
        if section["kind"] == "core" and not print_plan(section["plan"], log=lambda _m: None):
            clean = False
    if not clean:
        raise PackageError("в плане ядра ошибки — план не сохранён, применять его нельзя")
    if out is not None:
        out.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return document


def _install_ref(install: Path, out: Path | None) -> str:
    """Файл установки для apply --plan: относительно файла плана (или абсолютный, если план
    не пишется в файл)."""
    if out is None:
        return str(install.resolve())
    return os.path.relpath(install.resolve(), out.resolve().parent).replace(os.sep, "/")


# --- чтение и показ плана ----------------------------------------------------------------


def read_plan(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PackageError(f"{path}: не файл плана: {error}") from error
    verify_document(document, str(path))
    result: dict[str, Any] = document
    return result


def verify_document(document: Any, where: str = "план") -> None:
    """Форма package-sdk.plan/v1 и planHash: правленый план отвергается."""
    if not isinstance(document, dict) or document.get("format") != PLAN_FORMAT:
        raise PackageError(f"{where}: не план {PLAN_FORMAT}")
    problems = schema_module.errors(schema_module.PLAN, document)
    if problems:
        raise PackageError(f"{where}: не план {PLAN_FORMAT}: {problems[0]}")
    kinds = [section["kind"] for section in document["sections"]]
    expected = ["catalog", *(["core"] * kinds.count("core")), *SECTIONS[2:]]
    if kinds != expected:
        raise PackageError(
            f"{where}: секции плана {kinds} — нужны {', '.join(SECTIONS)} в этом порядке "
            "(core — по пакету с процессами)"
        )
    if document_hash(document) != document.get("planHash"):
        raise PackageError(
            f"{where}: план правили после построения (planHash не сходится) — постройте план "
            "заново: package-sdk plan … --out"
        )


_MARKS = {
    "create": "+",
    "register": "+",
    "enable": "+",
    "version": "~",
    "patch": "~",
    "deprecate": "-",
    "disable": "-",
    "retire": "-",
}


def _line(change: Mapping[str, Any]) -> str:
    ref = f"{change.get('kind')}/{change.get('key')}"
    if change.get("version") is not None and change.get("kind") in ("Skill", "KnowledgePack"):
        ref += f"@{change['version']}"
    owner = f" ({change['package']})" if change.get("package") else ""
    fields = f" [{', '.join(change['fields'])}]" if change.get("fields") else ""
    return (
        f"  {_MARKS.get(str(change.get('operation')), '?')} {ref}{owner}: "
        f"{change.get('detail') or change.get('operation')}{fields}"
    )


def overwrites_console(document: Mapping[str, Any]) -> bool:
    """Флаг плана ``overwriteConsole``; у плана без поля — False, прежнее поведение."""
    return document.get("overwriteConsole") is True


@dataclass(frozen=True)
class ConsoleEdit:
    """Объект плана ядра с полями, которые человек правил в консоли после прошлого применения."""

    package: str
    kind: str
    key: str
    overwritten: tuple[str, ...]  # поля, которые применение перезапишет
    kept: tuple[str, ...]  # поля, которые останутся как в консоли


def console_edits(document: Mapping[str, Any]) -> list[ConsoleEdit]:
    """Правки консоли в секциях ядра — по ответу плана ядра: поле ``owner: console`` и
    ``applies`` (перезапишется ли оно). Без ``applies`` судьбу поля решает флаг плана."""
    overwrite = overwrites_console(document)
    edits: list[ConsoleEdit] = []
    for section in document.get("sections") or []:
        if section.get("kind") != "core":
            continue
        for change in (section.get("plan") or {}).get("changes") or []:
            overwritten: list[str] = []
            kept: list[str] = []
            for item in change.get("fields") or []:
                if not isinstance(item, dict) or item.get("owner") != "console":
                    continue
                applies = item.get("applies")
                bucket = overwritten if (overwrite if applies is None else applies) else kept
                bucket.append(str(item.get("path")))
            if overwritten or kept:
                edits.append(
                    ConsoleEdit(
                        str(section.get("package")),
                        str(change.get("kind")),
                        str(change.get("key")),
                        tuple(overwritten),
                        tuple(kept),
                    )
                )
    return edits


def console_lines(document: Mapping[str, Any]) -> list[str]:
    """Правки консоли человеку: что план перезапишет и что сохранит. Без флага и без правок
    консоли — ни строки (вывод прежний)."""
    edits = console_edits(document)
    overwrite = overwrites_console(document)
    if not overwrite and not edits:
        return []
    lines: list[str] = []
    overwritten = [e for e in edits if e.overwritten]
    kept = [e for e in edits if e.kept]
    if overwrite:
        lines.append(
            "правки консоли: перезаписываются (overwriteConsole)"
            + ("" if overwritten else " — в объектах плана правок консоли нет")
        )
    else:
        lines.append("правки консоли: сохраняются (перезаписать — plan --overwrite-console)")
    if overwritten:
        lines.append("  будут перезаписаны:")
        lines.extend(
            f"  ! {e.kind}/{e.key} ({e.package}): {', '.join(e.overwritten)}" for e in overwritten
        )
    if kept:
        lines.append("  останутся как в консоли:")
        lines.extend(f"  = {e.kind}/{e.key} ({e.package}): {', '.join(e.kept)}" for e in kept)
    return lines


def core_changes(response: Mapping[str, Any]) -> int:
    """Изменения плана ядра, которые что-то пишут: не unchanged или с выводом версий."""
    return sum(
        1
        for change in response.get("changes") or []
        if change.get("action") != "unchanged" or change.get("deprecates")
    )


def count_changes(document: Mapping[str, Any]) -> int:
    total = 0
    for section in document.get("sections") or []:
        kind = section.get("kind")
        if kind in ("catalog", "notification-rules"):
            total += len(section.get("changes") or [])
        elif kind == "core":
            total += core_changes(section.get("plan") or {})
        elif kind == "knowledge":
            total += len(section.get("register") or []) + len(section.get("enable") or [])
        elif kind == "retire":
            total += len(section.get("items") or [])
    return total


def format_plan(document: Mapping[str, Any]) -> list[str]:
    """План человеку: по секциям, в порядке применения."""
    engines = ", ".join(f"{k} {v}" for k, v in (document.get("engines") or {}).items())
    lines = [
        f"план установки {document.get('installation') or ''} для {document.get('server')}"
        f" ({engines}): {document.get('planHash')}"
    ]
    titles = {
        "catalog": "каталог",
        "knowledge": "онтологии",
        "notification-rules": "правила уведомлений",
        "retire": "вывод из оборота",
    }
    for section in document.get("sections") or []:
        kind = section.get("kind")
        if kind == "core":
            lines.append(f"ядро, пакет {section.get('package')}:")
            print_plan(section.get("plan") or {}, log=lambda line: lines.append("  " + line))
            continue
        items: list[str] = []
        if kind in ("catalog", "notification-rules"):
            items = [_line(c) for c in section.get("changes") or []]
        elif kind == "retire":
            items = [_line(c) for c in section.get("items") or []]
        elif kind == "knowledge":
            items = [_line(c) for c in section.get("register") or []]
            for entry in section.get("enable") or []:
                strict = ", строгий режим" if entry.get("strict") else ""
                items.append(
                    f"  ~ workspace {entry.get('workspace')}: онтологии → "
                    f"{', '.join(entry.get('packs') or []) or '—'}{strict} "
                    f"(сейчас: {', '.join(entry.get('current') or []) or '—'})"
                )
        lines.append(f"{titles.get(str(kind), kind)}:" + ("" if items else " без изменений"))
        lines.extend(items)
    lines.extend(console_lines(document))
    count = count_changes(document)
    lines.append(f"итого изменений: {count}" if count else "изменений нет")
    return lines
