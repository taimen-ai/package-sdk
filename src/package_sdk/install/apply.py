"""Применение сохранённого единого плана (TAI-ADR-0062 п.7, FR-022).

Применяется только сохранённый неизменённый план:

1. ``planHash`` документа сходится (правленый файл отвергается), сервер — тот же;
2. установка собрана по тем же источникам (``lockHash``) и тем же значениям переменных
   (``variablesHash``), ядро той же версии (``engines``);
3. **до первой записи** каждая секция построена заново и сверена с планом, а план ядра
   каждого пакета запрошен заново и сверен по ``planHash`` — любое расхождение ``plan_stale``;
4. человек подтверждает план (``confirm``); без подтверждения применяет только
   ``assume_yes`` — инициализация стенда, которая сама решение оператора; этот путь помечен
   в журнале;
5. план ядра строится заново и применяется с тем же ``overwriteConsole``, что в плане (флаг
   под ``planHash`` документа и под ``planHash`` ядра);
6. секции — в порядке ``catalog`` → ``core`` → ``knowledge`` → ``notification-rules`` →
   ``retire``; перед каждой секцией установщика она строится заново и сверяется
   (``plan_stale`` до первой записи секции), секцию ``core`` сверяет само ядро
   (``409 plan_stale``).

Секции между собой не атомарны: каждая запись идемпотентна, и повторный план показывает
остаток (plan Р5, «честное ограничение»).
"""

from __future__ import annotations

import os
import urllib.parse
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from package_sdk.apply import HttpError
from package_sdk.check import check
from package_sdk.install.lock import GitCache, load
from package_sdk.install.plan import (
    CORE_COMPONENT,
    PLAN_FORMAT,
    RETIRE_REASON,
    Prepared,
    Target,
    catalog_section,
    check_server,
    comparable,
    core_packages,
    core_version,
    digest,
    format_plan,
    knowledge_section,
    notification_section,
    overwrites_console,
    owned_by_core,
    read_plan,
    retire_section,
    variable_problems,
    variable_values,
    verify_document,
)
from package_sdk.model import API, PLAN_KINDS, Installation, PackageError
from package_sdk.source import plan_request

STALE = "plan_stale"


def _stale(message: str) -> PackageError:
    return PackageError(
        f"{STALE}: план устарел — {message}. Постройте план заново (package-sdk plan … --out) "
        "и примените новый"
    )


def _first_difference(planned: list[dict[str, Any]], current: list[dict[str, Any]]) -> str:
    for index in range(max(len(planned), len(current))):
        before = planned[index] if index < len(planned) else None
        after = current[index] if index < len(current) else None
        if before != after:
            return f"в плане {_short(before)}, на стенде сейчас {_short(after)}"
    return "порядок изменений другой"


def _short(change: dict[str, Any] | None) -> str:
    if change is None:
        return "—"
    ref = f"{change.get('kind', '')}/{change.get('key', change.get('workspace', ''))}"
    return f"{ref} {change.get('operation', '')}".strip()


def _same(title: str, planned: list[dict[str, Any]], current: list[dict[str, Any]]) -> None:
    before, after = comparable(planned), comparable(current)
    if before != after:
        raise _stale(f"секция {title}: {_first_difference(before, after)}")


def _section(document: Mapping[str, Any], kind: str) -> dict[str, Any]:
    return next(s for s in document["sections"] if s["kind"] == kind)


def _recheck(
    kind: str,
    document: Mapping[str, Any],
    installation: Installation,
    target: Target,
    env: Mapping[str, str],
) -> None:
    """Секция установщика, построенная заново, совпадает с планом."""
    planned = _section(document, kind)
    ignore: list[str] = []
    if kind == "catalog":
        _same(
            kind, planned["changes"], catalog_section(installation, target, env, ignore)["changes"]
        )
    elif kind == "notification-rules":
        current = notification_section(installation, target, env, ignore)
        _same(kind, planned["changes"], current["changes"])
    elif kind == "knowledge":
        current = knowledge_section(installation, target, env)
        _same(kind, planned["register"], current["register"])
        _same(kind, planned["enable"], current["enable"])
    elif kind == "retire":
        _same(kind, planned["items"], retire_section(installation, target, env, ignore)["items"])


def _core_request(
    document: Mapping[str, Any],
    section: Mapping[str, Any],
    installation: Installation,
    env: Mapping[str, str],
) -> dict[str, Any]:
    package = next(p for p in installation.packages if p.key == section["package"])
    return plan_request(
        package,
        dict(env),
        workspace=section.get("workspaceId"),
        replay_limit=int(section.get("replayLimit", 50)),
        overwrite_console=overwrites_console(document),
    )


def _preflight(
    document: Mapping[str, Any],
    prepared: Prepared,
    target: Target,
    env: Mapping[str, str],
) -> None:
    """Всё, что можно сверить до первой записи: входы плана и каждая его секция."""
    if prepared.sources.lock_hash != document.get("lockHash"):
        raise _stale("пакеты или их источники изменились после построения плана (lockHash)")
    if digest(prepared.variables) != document.get("variablesHash"):
        raise _stale("значения переменных установки изменились (variablesHash)")
    planned_version = (document.get("engines") or {}).get(CORE_COMPONENT)
    if prepared.version != planned_version:
        raise _stale(f"ядро стенда обновилось: {planned_version} → {prepared.version}")
    installation = prepared.installation
    planned_core = [s["package"] for s in document["sections"] if s["kind"] == "core"]
    if planned_core != [p.key for p in core_packages(installation)]:
        raise _stale("пакеты с процессами и календарями не те, что в плане")
    for kind in ("catalog", "knowledge", "notification-rules", "retire"):
        _recheck(kind, document, installation, target, env)
    api = target.process_api()
    for section in document["sections"]:
        if section["kind"] != "core":
            continue
        response = api.plan(_core_request(document, section, installation, env))
        if response.get("planHash") != section["planHash"]:
            raise _stale(
                f"план ядра пакета {section['package']}: {section['planHash']} → "
                f"{response.get('planHash')} (каталог или живые экземпляры изменились)"
            )


def install_file(
    document: Mapping[str, Any], plan_path: Path | None, install: Path | None = None
) -> Path:
    """Файл установки плана: заданный явно или названный планом относительно своего файла."""
    if install is not None:
        return install
    ref = Path(str(document.get("install") or ""))
    if not str(ref):
        raise PackageError("в плане нет файла установки (install) — постройте план заново")
    if ref.is_absolute() or plan_path is None:
        return ref
    return Path(os.path.normpath(plan_path.resolve().parent / ref))


def _knowledge(
    section: Mapping[str, Any],
    installation: Installation,
    target: Target,
    env: Mapping[str, str],
    log: Callable[[str], None],
) -> None:
    applier = target.applier(env, dry_run=False, log=log)
    wanted = {(c["package"], c["key"]) for c in section.get("register") or []}
    for obj in installation.objects:
        if obj.kind == "KnowledgePack" and (obj.package, obj.key) in wanted:
            applier._apply_KnowledgePack(obj, applier._spec(installation, obj))
    for entry in section.get("enable") or []:
        applier._enable_knowledge(
            {"workspace": entry["workspace"], "packs": entry["packs"], "strict": entry["strict"]}
        )


def _retire(
    section: Mapping[str, Any],
    installation: Installation,
    target: Target,
    env: Mapping[str, str],
    log: Callable[[str], None],
) -> None:
    applier = target.applier(env, dry_run=False, log=log)
    applier.retire_keys({k: v for k, v in installation.retire.items() if k not in PLAN_KINDS})
    for item in section.get("items") or []:
        kind = item.get("kind")
        if kind not in PLAN_KINDS:
            continue
        collection = "process-definitions" if kind == "Process" else "calendars"
        key = str(item["key"])
        headers = {**target.headers, "Idempotency-Key": str(uuid.uuid4())}
        try:
            answer = target.http.call(
                "POST",
                f"{API}/{collection}/{urllib.parse.quote(key, safe='')}:retire",
                {"reason": RETIRE_REASON},
                headers,
            )
        except HttpError as error:
            raise PackageError(f"{kind}/{key}: вывод из оборота отвергнут — {error}") from error
        opened = answer.get("openInstances")
        tail = f", живых экземпляров {opened} — доживут" if opened else ""
        log(f"   {kind}/{key}: → retired{tail}")


def apply(
    plan: Path | Mapping[str, Any],
    *,
    target: Target,
    env: Mapping[str, str],
    install: Path | None = None,
    assume_yes: bool = False,
    confirm: Callable[[str], bool] | None = None,
    cache: GitCache | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Применить ровно сохранённый план.

    plan — файл плана или документ от :func:`package_sdk.install.plan`; install — файл
    установки, если план не называет его относительно своего файла. Человек подтверждает
    план через confirm; assume_yes=True — только инициализация стенда (bootstrap), и это
    видно в журнале."""
    plan_path = plan if isinstance(plan, Path) else None
    document = read_plan(plan) if isinstance(plan, Path) else dict(plan)
    if plan_path is None:
        verify_document(document)
    server = check_server(target.server)
    if server != document["server"]:
        raise PackageError(f"план построен для {document['server']}, а применяется к {server}")
    install_path = install_file(document, plan_path, install)
    sources = load(install_path, strict=True, cache=cache, log=log)
    installation = sources.installation
    errors, _warnings = check(installation, env=dict(env))
    if errors:
        raise PackageError("пакеты не прошли проверку:\n  " + "\n  ".join(errors))
    variables = variable_values(installation, env)
    problems = variable_problems(installation, variables, target)
    if problems:
        raise PackageError("переменные установки не подходят стенду:\n  " + "\n  ".join(problems))
    _preflight(document, Prepared(sources, core_version(target), variables), target, env)
    if assume_yes:
        log(
            "   ! применение без подтверждения человека (assume_yes): только инициализация "
            "стенда, которая сама — решение оператора"
        )
    else:
        if confirm is None:
            raise PackageError("применение плана требует подтверждения человека")
        for line in format_plan(document):
            log(line)
        overwrite = (
            " с перезаписью правок консоли (overwriteConsole)"
            if overwrites_console(document)
            else ""
        )
        if not confirm(f"Применить план {document['planHash']}{overwrite}?"):
            raise PackageError("применение отменено: план не подтверждён")
    applied: dict[str, Any] = {"planHash": document["planHash"], "core": []}
    for section in document["sections"]:
        kind = section["kind"]
        if kind == "catalog":
            _recheck(kind, document, installation, target, env)
            applier = target.applier(env, dry_run=False, log=log)
            applier.catalog(installation, owned_by_core(installation))
            applied["catalog"] = applier.result
        elif kind == "core":
            request = _core_request(document, section, installation, env)
            body: dict[str, Any] = {"package": request["package"], "planHash": section["planHash"]}
            if request.get("workspaceId"):
                body["workspaceId"] = request["workspaceId"]
            if request.get("overwriteConsole"):
                body["overwriteConsole"] = True
            response = target.process_api().apply(body)
            for item in response.get("applied") or []:
                version = f" v{item['version']}" if item.get("version") is not None else ""
                log(f"   {item.get('kind')}/{item.get('key')}: {item.get('action')}{version}")
            log(f"   применён план ядра {section['package']}: {section['planHash']}")
            applied["core"].append(response)
        elif kind == "knowledge":
            _recheck(kind, document, installation, target, env)
            _knowledge(section, installation, target, env, log)
        elif kind == "notification-rules":
            _recheck(kind, document, installation, target, env)
            applier = target.applier(env, dry_run=False, log=log)
            applier.notification_rules(installation)
        elif kind == "retire":
            _recheck(kind, document, installation, target, env)
            _retire(section, installation, target, env, log)
    log(f"применён план {document['planHash']} ({PLAN_FORMAT})")
    return applied
