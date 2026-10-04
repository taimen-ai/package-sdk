"""Тесты пакета кодом ядра в процессе — без стенда (TAI-ADR-0054 п.8, TAI-ADR-0062 п.2).

`package-sdk test --server` отправляет пакет ядру (`POST /packages:test`, CP-ADR-0074 §10).
Песочница делает то же самое в процессе и тем же кодом ядра закреплённой версии —
своего вычисления процессов, выражений и правил в SDK нет (FR-015):

- **каталог** — объекты **всех** видов пакета и его `requires`: скиллы, типы задач,
  агенты, роли, типы артефактов, календари, процессы, правила вывода работы и прочие;
  чего нет в пакетах, того нет и в песочнице;
- **процессы** — доменные модули ядра (`package_source`, `process_definition`,
  `process_engine`, `process_sandbox`), без базы;
- **правила и типы задач** — у них нет модели в памяти, их тест — прикладной код ядра
  (`package_trials.run_subject_tests`): публикация объектов, наблюдение, решение гейта,
  завершение и приёмка в транзакции, которая всегда откатывается (CP-ADR-0074 Z2).
  Этому коду нужен PostgreSQL: пустая база песочницы — `--database-url` или
  переменная `PACKAGE_SDK_SANDBOX_DATABASE_URL`. Схему ядра песочница накатывает сама
  (миграции закреплённой версии), tenant песочницы заводит при первом прогоне — только
  в пустую базу или в ту, что уже подготовила сама (схема ядра и tenant песочницы);
  базу стенда, чужую или схему ядра без tenant'ов отвергает до любой записи, под
  pg_advisory_lock (`prepare_database`). Без
  базы такие тесты не исполняются и помечаются `skipped` с находкой
  `sandbox_database_required` — прогон не зелёный, молчаливой деградации нет (FR-014);
- **покрытие** — процессов, правил (ветки условия, исходы) и типов задач (исходы
  гейтов, предусловия, действия завершения, критерии приёмки), функциями ядра.

Чем отличается от `POST /packages:test`:

- каталог — объекты пакетов (`requires` по файлам), а не каталог tenant'а; объекты
  `requires` в тестах правил и типов задач публикуются вместе с объектами пакета, как
  если бы `requires` уже были установлены;
- `governedBy` не сверяется с памятью: памяти нет, предупреждение ядра
  «документа нет в базе знаний» здесь не появится;
- переменные установки `${…}` подставляются из `--env` (файл .env) и окружения, как у
  `package-sdk test`; незаданные остаются, а `workspaceId` вида `${…}` снимается, как это
  делает ядро без `workspaceId` запроса;
- настройки пакета (CP-ADR-0081 §6, Б5): `given.settings` и шаг `settings` теста процесса
  ядро проверяет по схеме из файлов пакета и пишет во временную историю, как
  `POST /packages:test`. Организации у песочницы нет: ссылка `x-ref` на тип задачи или
  календарь должна быть в пакете и его `requires`, а id роли, principal'а и workspace
  считаются существующими — как вымышленные principal теста. Процесс из `requires`, который
  читает настройки своего пакета, видит их значения по умолчанию. Ядро старше настроек —
  находка-ошибка `sandbox_settings_unsupported`, если пакет их читает или тест их задаёт.
- виды каталога, которых процессы, правила и типы задач не касаются (`ConnectionType`,
  CP-ADR-0079), в песочницу не передаются, если разбор пакетов ядра рядом их ещё не
  знает: пакет провайдера с типом подключения тестируется так же, как без него.

Нужен код ядра — extra `package-sdk[sandbox]` (control-plane закреплённой версии):

    package-sdk sandbox <пакет>
    package-sdk sandbox <пакет> --test tests/cancel.test.yaml --json
    package-sdk sandbox <пакет> --database-url postgresql://…/sandbox
    package-sdk sandbox                     # все пакеты с тестами (CI)

Код выхода 0 — все тесты зелёные и находок-ошибок нет.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from package_sdk import core as cp_core
from package_sdk import model as cp
from package_sdk import source as cp_source

# Где песочница берёт базу для тестов правил и типов задач.
DATABASE_ENV = "PACKAGE_SDK_SANDBOX_DATABASE_URL"
# Находка: тест правила или типа задачи не исполнялся — нет базы песочницы.
DATABASE_REQUIRED = "sandbox_database_required"
# Статус теста, который песочница не исполняла.
SKIPPED = "skipped"
# Tenant песочницы в её базе: заводится при первом прогоне, дальше переиспользуется.
SANDBOX_TENANT = "package-sandbox"
# Находка: пакет читает настройки или тест их задаёт, а ядро рядом настроек не знает.
SETTINGS_UNSUPPORTED = "sandbox_settings_unsupported"
# Виды x-ref, значения которых — ключи объектов каталога, а не id организации.
_CATALOG_REFS = {"taskType": "TaskType", "calendar": "Calendar"}
# Виды каталога, которых тесты пакета не касаются: песочнице они не нужны, а разбор пакетов
# ядра, который их ещё не знает, ответил бы unknown_kind (CP-ADR-0079 §2: ConnectionType).
PROCESS_NEUTRAL_KINDS = ("ConnectionType",)


class CoreMissing(RuntimeError):
    """control-plane с движком процессов не импортируется."""


def _domain() -> dict[str, Any]:
    try:
        from control_plane.api.v1.packages import SHAPES, SUPPORTING
        from control_plane.application.commands import package_trials
        from control_plane.domain import process_definition as pd
        from control_plane.domain import process_engine as engine
        from control_plane.domain import process_sandbox as sandbox
        from control_plane.domain.calendar import Calendar, CalendarError
        from control_plane.domain.package_source import (
            KINDS,
            SUBJECT_PROCESS,
            ParsedPackage,
            parse_package,
            select_tests,
        )
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise CoreMissing(
            "the core's code with the process engine and work rule tests is required: install "
            "package-sdk[sandbox] — " + str(exc)
        ) from exc
    try:  # CP-ADR-0081: ядро старше настроек пакетов этих модулей не знает
        from control_plane.domain import package_settings, settings_refs
    except ImportError:
        package_settings = settings_refs = None
    if "settings_schema" not in getattr(sandbox.World, "__dataclass_fields__", {}):
        package_settings = settings_refs = None
    return {
        "package_settings": package_settings,
        "settings_refs": settings_refs,
        "pd": pd,
        "engine": engine,
        "sandbox": sandbox,
        "trials": package_trials,
        "shapes": SHAPES,
        "supporting": SUPPORTING,
        "Calendar": Calendar,
        "CalendarError": CalendarError,
        "SUBJECT_PROCESS": SUBJECT_PROCESS,
        "ParsedPackage": ParsedPackage,
        "parse_package": parse_package,
        "select_tests": select_tests,
        "kinds": frozenset(KINDS),
    }


def _files(
    package: cp.Package, env: dict[str, str], known_kinds: frozenset[str] | None = None
) -> list[tuple[str, str]]:
    """Файлы пакета для разбора ядром; объекты видов, нейтральных к тестам и неизвестных
    ядру рядом (known_kinds), пропускаются."""
    skipped = {
        obj.path.relative_to(package.path).as_posix()
        for obj in package.objects
        if obj.kind in PROCESS_NEUTRAL_KINDS
        and known_kinds is not None
        and obj.kind not in known_kinds
    }
    return [
        (f["path"], f["content"])
        for f in cp_source.package_files(package, env, strict=False)
        if f["path"] not in skipped
    ]


def _skill_entry(pd: Any, spec: dict[str, Any]) -> Any:
    contract = spec.get("contract")
    if isinstance(contract, dict):
        return pd.SkillEntry(contract.get("inputs"), contract.get("outputs"))
    return pd.SkillEntry(spec.get("inputSchema"), spec.get("outputSchema"))


def _without_install_workspace(spec: dict[str, Any]) -> dict[str, Any]:
    raw = spec.get("workspaceId")
    if isinstance(raw, str) and raw.startswith("${") and raw.endswith("}"):
        return {k: v for k, v in spec.items() if k != "workspaceId"}
    return spec


def with_requires(d: dict[str, Any], own: Any, required: dict[str, Any]) -> Any:
    """Пакет, каким его видят тесты правил и типов задач: его объекты и объекты `requires`.

    На стенде `requires` уже установлены и лежат в каталоге tenant'а; в песочнице их
    объекты публикуются в той же откатываемой транзакции, что и объекты пакета. Файл
    объекта из `requires` назван с ключом его пакета (`<пакет>/<файл>`), чтобы находка
    указывала, чей это файл. Тесты — только свои."""
    objects = list(own.objects)
    seen = {obj.ref for obj in objects}
    for key, parsed in required.items():
        for obj in parsed.objects:
            if obj.ref in seen:
                continue
            seen.add(obj.ref)
            objects.append(type(obj)(obj.kind, obj.key, obj.spec, f"{key}/{obj.file}", obj.lines))
    return d["ParsedPackage"](
        manifest=own.manifest,
        manifest_object=own.manifest_object,
        objects=objects,
        tests=list(own.tests),
    )


@dataclass
class _Settings:
    """Настройки пакетов для песочницы: находки объявления, ``SettingsScope`` каталога по
    ключу пакета и поля ``World`` ядра."""

    problems: list[Any] = field(default_factory=list)
    scopes: dict[str, Any] = field(default_factory=dict)
    world: dict[str, Any] = field(default_factory=dict)


def _test_settings(test: Any) -> list[Any]:
    """Значения настроек, которые сохраняет тест: ``given.settings`` и шаги ``settings``."""
    data = test.data if isinstance(test.data, dict) else {}
    given = data.get("given") if isinstance(data.get("given"), dict) else {}
    saved = [given["settings"]] if "settings" in given else []
    for step in data.get("steps") or ():
        if isinstance(step, dict) and "settings" in step:
            saved.append(step["settings"])
    return saved


def _package_settings(
    d: dict[str, Any], parsed: dict[str, Any], target: str, tests: list[Any], objects: list[Any]
) -> _Settings:
    """Объявления настроек пакета и его ``requires`` кодом ядра (CP-ADR-0081 Б5)."""
    pd = d["pd"]
    out = _Settings()
    own = parsed[target]
    core, refs = d["package_settings"], d["settings_refs"]
    if core is None:
        # Пакет читает настройки или тест их задаёт, а ядро рядом их не знает: молча
        # прогнать такие тесты нельзя — их значения ядро бы не увидело (FR-014).
        declares = own.manifest_object is not None and "settings" in own.manifest_object.spec
        if any(_test_settings(t) for t in tests) or (
            declares and any("settings" in json.dumps(o.spec) for o in own.of_kind("Process"))
        ):
            out.problems.append(
                pd.Problem(
                    SETTINGS_UNSUPPORTED,
                    "error",
                    "/spec/settings",
                    "the package reads its settings or its tests save them, and the "
                    "control-plane code next to the SDK predates CP-ADR-0081 (no "
                    "control_plane.domain.package_settings): the sandbox cannot run them",
                    hint="the core at the revision with package settings, or --server",
                    file="package.yaml",
                )
            )
        return out
    schemas: dict[str, Any] = {}
    for key, package in parsed.items():
        declaration = core.check_declaration(package)
        if key == target:
            out.problems.extend(declaration.problems)
        manifest = package.manifest_object
        schema = declaration.declared.schema if declaration.declared is not None else None
        schemas[key] = schema
        out.scopes[key] = refs.SettingsScope(manifest.key if manifest else key, schema)
    schema = schemas.get(target)
    catalog = {kind: {o.key for o in objects if o.kind == kind} for kind in _CATALOG_REFS.values()}
    known: set[tuple[str, str]] = set()
    for test in tests:
        for values in _test_settings(test):
            if schema is None or not isinstance(values, dict) or core.validate(values, schema):
                continue
            for ref in core.references(values, schema):
                kind = _CATALOG_REFS.get(ref.kind)
                if kind is None or ref.value in catalog[kind]:
                    known.add((ref.kind, ref.value))
    out.world = {
        "settings_schema": schema,
        "known_refs": frozenset(known),
        # процесс из requires читает настройки своего пакета: в песочнице — по умолчанию
        "other_settings": {
            obj.key: core.effective({}, schemas[key])
            for key, package in parsed.items()
            if key != target and schemas.get(key) is not None
            for obj in package.of_kind("Process")
        },
    }
    return out


def database_url(value: str | None = None) -> str | None:
    """Адрес базы песочницы: явный или из PACKAGE_SDK_SANDBOX_DATABASE_URL; драйвер ядра —
    psycopg (`postgresql://` дополняется до `postgresql+psycopg://`)."""
    url = value or os.environ.get(DATABASE_ENV) or None
    if url is None:
        return None
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def _migrations() -> Path:
    import control_plane

    root = Path(control_plane.__file__).resolve().parents[2]
    scripts = root / "migrations"
    if not (scripts / "env.py").is_file():
        raise CoreMissing(
            f"the core's migrations are not next to its code ({scripts}): work rule and task type tests "
            "need control-plane from source (path dependency of extra sandbox)"
        )
    return scripts


# Ключ pg_advisory_lock песочницы: проверка базы, миграции и bootstrap — по одному прогону
# за раз (два первых прогона на пустой базе иначе создают одни и те же типы наперегонки).
_LOCK_KEY = 0x7061636B_73646B  # "packsdk"
# Схемы, которые есть в любой базе и таблиц песочницы не содержат.
_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


def database_refusal(
    tables: set[str], foreign_tenants: list[str], *, sandbox_tenant: bool
) -> str | None:
    """Почему песочнице нельзя писать в эту базу; None — можно.

    `tables` — таблицы базы вне системных схем (`схема.таблица`), `foreign_tenants` —
    slug'и tenant'ов, кроме tenant'а песочницы, `sandbox_tenant` — есть ли tenant
    песочницы. Можно только в пустую базу или в ту, которую песочница уже подготовила
    сама: схема ядра (`public.alembic_version`), tenant песочницы и никаких других.
    Схема ядра без tenant'ов — тоже чужая база: её готовила не песочница (стенд до
    bootstrap, база другого инструмента), и миграции закреплённого ядра необратимо
    изменили бы её схему."""
    if not tables:
        return None
    if not {"public.alembic_version", "public.tenants"} <= tables:
        shown = ", ".join(sorted(tables)[:5]) + (" …" if len(tables) > 5 else "")
        return f"the database is not empty and was not prepared by the sandbox (tables: {shown})"
    if foreign_tenants:
        return (
            "the database has tenants that are not the sandbox's ("
            + ", ".join(foreign_tenants)
            + ") — this is a stand database or someone else's database"
        )
    if not sandbox_tenant:
        return (
            f"the database has the core's schema but no sandbox tenant ({SANDBOX_TENANT}) — it was "
            "prepared by something other than the sandbox, or the first sandbox run stopped "
            "between the migration and creating the tenant; recreate the database "
            "(DROP DATABASE and CREATE DATABASE) and run again"
        )
    return None


@contextmanager
def _exclusive(url: str) -> Iterator[Any]:
    """Соединение под pg_advisory_lock песочницы; проверка базы — до любой записи."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    engine = create_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _LOCK_KEY})
            try:
                tables = {
                    f"{schema}.{name}"
                    for schema, name in conn.execute(
                        text(
                            "SELECT table_schema, table_name FROM information_schema.tables"
                            " WHERE table_schema <> ALL(:system)"
                            " AND table_schema NOT LIKE 'pg\\_%'"
                        ),
                        {"system": list(_SYSTEM_SCHEMAS)},
                    )
                }
                foreign: list[str] = []
                own = False
                if "public.tenants" in tables:
                    foreign = list(
                        conn.scalars(
                            text(
                                "SELECT slug FROM tenants WHERE slug <> :own ORDER BY slug LIMIT 5"
                            ),
                            {"own": SANDBOX_TENANT},
                        )
                    )
                    own = bool(
                        conn.scalar(
                            text("SELECT EXISTS (SELECT 1 FROM tenants WHERE slug = :own)"),
                            {"own": SANDBOX_TENANT},
                        )
                    )
                refusal = database_refusal(tables, foreign, sandbox_tenant=own)
                if refusal is not None:
                    raise cp.PackageError(
                        f"the sandbox does not write to this database: {refusal}; it needs its own empty database "
                        f"({DATABASE_ENV}), nothing changed"
                    )
                yield conn
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _LOCK_KEY})
    finally:
        engine.dispose()


def _migrate(url: str, revision: str = "head") -> None:
    """Схема ядра закреплённой версии (alembic upgrade) — только после проверки базы."""
    from alembic import command
    from alembic.config import Config
    from alembic.util.exc import CommandError

    config = Config()
    config.set_main_option("script_location", str(_migrations()))
    previous = os.environ.get("CP_DATABASE_URL")
    # env.py миграций ядра берёт адрес из CP_DATABASE_URL
    os.environ["CP_DATABASE_URL"] = url
    try:
        command.upgrade(config, revision)
    except CommandError as exc:
        raise cp.PackageError(
            f"cannot bring the sandbox database schema to the pinned core version: {exc} — the "
            "database is newer than the core or was prepared by another core; give the "
            "sandbox a new empty database"
        ) from exc
    finally:
        if previous is None:
            os.environ.pop("CP_DATABASE_URL", None)
        else:
            os.environ["CP_DATABASE_URL"] = previous


def prepare_database(url: str) -> None:
    """Проверить базу песочницы, накатить на неё схему ядра и завести tenant песочницы —
    под блокировкой песочницы, так что подготовленная база всегда с её tenant'ом.

    Чужая база (не пустая и не подготовленная песочницей) отвергается до любой записи."""
    _prepare(url)


def _prepare(url: str) -> Any:
    """Проверка базы, миграции и tenant песочницы под блокировкой; ответ — его AuthContext."""
    with _exclusive(url):
        _migrate(url)
        return asyncio.run(_sandbox_caller(url))


async def _caller(session_factory: Any) -> Any:
    """Администратор tenant'а песочницы — от его имени публикуются объекты теста."""
    from control_plane.application.authorization import AuthContext
    from control_plane.application.commands.bootstrap import bootstrap
    from control_plane.domain.enums import Permission, PrincipalKind
    from control_plane.infrastructure.db.models import ApiKey, Principal, Tenant
    from sqlalchemy import func, select

    async with session_factory() as db, db.begin():
        tenant = await db.scalar(select(Tenant).where(Tenant.slug == SANDBOX_TENANT))
        if tenant is None:
            if await db.scalar(select(func.count()).select_from(Tenant)):
                raise cp.PackageError(
                    "the sandbox database already has a tenant that is not the sandbox's — it needs its own "
                    "empty database "
                    f"({DATABASE_ENV})"
                )
            result = await bootstrap(
                db,
                tenant_slug=SANDBOX_TENANT,
                tenant_name="Package sandbox",
                admin_display_name="package-sdk sandbox",
                request_id="package-sdk-sandbox",
            )
            key, kind = result.api_key, result.admin.kind
        else:
            key = await db.scalar(
                select(ApiKey)
                .where(ApiKey.tenant_id == tenant.id, ApiKey.revoked_at.is_(None))
                .order_by(ApiKey.created_at)
                .limit(1)
            )
            if key is None:
                raise cp.PackageError(f"tenant {SANDBOX_TENANT} in the sandbox database has no key")
            kind = await db.scalar(select(Principal.kind).where(Principal.id == key.principal_id))
    return AuthContext(
        tenant_id=key.tenant_id,
        principal_id=key.principal_id,
        principal_kind=str(kind or PrincipalKind.HUMAN),
        api_key_id=key.id,
        permissions=frozenset({Permission.ADMIN.value}),
        request_id="package-sdk-sandbox",
    )


def _settings(url: str) -> Any:
    from control_plane.config import Settings

    # _env_file=None: песочница не читает .env автора пакета как настройки ядра
    return Settings(database_url=url, log_level="WARNING", _env_file=None)


async def _sandbox_caller(url: str) -> Any:
    from control_plane.infrastructure.db.engine import build_engine, build_session_factory

    engine = build_engine(_settings(url))
    try:
        return await _caller(build_session_factory(engine))
    finally:
        await engine.dispose()


async def _subject_tests(
    d: dict[str, Any], url: str, package: Any, tests: list[Any], ctx: Any
) -> Any:
    from control_plane.infrastructure.db.engine import build_engine, build_session_factory

    settings = _settings(url)
    engine = build_engine(settings)
    try:
        session_factory = build_session_factory(engine)
        return await d["trials"].run_subject_tests(
            session_factory,
            ctx,
            settings,
            package=package,
            tests=tests,
            workspace_id=None,
            shapes=d["shapes"],
            supporting=d["supporting"],
        )
    finally:
        await engine.dispose()


def subject_tests(d: dict[str, Any], url: str, package: Any, tests: list[Any]) -> Any:
    """SubjectReport ядра: база проверяется, схема ядра накатывается, тесты идут кодом ядра."""
    from sqlalchemy.exc import SQLAlchemyError

    try:
        # проверка базы, миграции и tenant песочницы — под блокировкой, до них не пишем
        ctx = _prepare(url)
        return asyncio.run(_subject_tests(d, url, package, tests, ctx))
    except (SQLAlchemyError, OSError) as exc:
        raise cp.PackageError(
            f"the sandbox database ({DATABASE_ENV}) is unavailable: {exc}"
        ) from exc


def _skipped(test: Any) -> dict[str, Any]:
    return {
        "file": test.file,
        "name": test.name,
        "subject": test.subject,
        "object": test.object,
        "process": None,
        "status": SKIPPED,
        "durationMs": 0,
        "failures": [],
    }


def run_installation(
    installation: cp.Installation,
    target: cp.Package,
    *,
    tests: list[str] | None = None,
    env: dict[str, str] | None = None,
    database: str | None = None,
) -> dict[str, Any]:
    """PackageTestOut пакета `target` установки: находки, результаты тестов, покрытие.

    `tests` — пути файлов тестов в пакете (`tests/<имя>.test.yaml`), None — все;
    `env` — переменные установки: `${…}` подставляются в текст файлов пакета и его
    `requires`, как у `package-sdk test`; незаданные остаются как есть; `database` —
    адрес пустой базы PostgreSQL для тестов правил и типов задач."""
    d = _domain()
    pd, engine, sandbox, trials = d["pd"], d["engine"], d["sandbox"], d["trials"]
    started = time.monotonic()
    parsed = {
        package.key: d["parse_package"](_files(package, env or {}, d["kinds"]))
        for package in installation.required(target.key)
    }
    own = parsed[target.key]
    problems = list(own.problems)
    chosen, missing = d["select_tests"](own, tests)
    problems.extend(missing)
    processes = [t for t in chosen if t.subject == d["SUBJECT_PROCESS"]]
    subjects = [t for t in chosen if t.subject != d["SUBJECT_PROCESS"]]
    # Форма и словарь правил и типов задач — чистыми функциями ядра, как у /packages:test.
    problems.extend(trials.static_problems(own, d["shapes"], None))
    problems.extend(trials.limit_problems(subjects))

    # Каталог — все объекты пакета и его requires.
    objects = [obj for package in parsed.values() for obj in package.objects]
    calendars: dict[str, Any] = {}
    for obj in objects:
        if obj.kind != "Calendar":
            continue
        try:
            calendars[obj.key] = d["Calendar"].from_spec(obj.spec)
        except d["CalendarError"] as exc:
            problems.append(obj.place(pd.Problem("invalid_calendar", "error", "/spec", str(exc))))
    skills = {
        f"{o.key}@{o.spec.get('version')}": _skill_entry(pd, o.spec)
        for o in objects
        if o.kind == "Skill"
    }
    task_types = {o.key: o.spec.get("fieldSchema") or None for o in objects if o.kind == "TaskType"}
    agents = frozenset(o.key for o in objects if o.kind == "Agent")
    catalog = pd.Catalog(
        skills=skills,
        task_types=task_types,
        agents=agents,
        calendars=frozenset(calendars),
        artifact_types=frozenset(o.key for o in objects if o.kind == "ArtifactType"),
        processes=frozenset(o.key for o in objects if o.kind == "Process"),
    )
    settings = _package_settings(d, parsed, target.key, processes, objects)
    problems.extend(settings.problems)
    definitions: dict[str, Any] = {}
    for key, package in parsed.items():
        package_catalog = catalog
        if key in settings.scopes:
            package_catalog = replace(catalog, settings=settings.scopes[key])
        for obj in package.of_kind("Process"):
            try:
                spec = _without_install_workspace(pd.normalized_spec(obj.spec))
            except pd.SpecError as exc:
                if package is own:
                    problems.append(
                        obj.place(pd.Problem("invalid_document", "error", exc.path, exc.message))
                    )
                continue
            checked = pd.check_process(
                obj.key, spec, package_catalog, file=obj.file, locate=obj.locate
            )
            if package is own:
                problems.extend(checked.problems)
            if not checked.errors:
                definitions[obj.key] = engine.Definition.build(obj.key, spec, package_catalog)

    results: list[dict[str, Any]] = []
    coverage: list[Any] = []
    rule_coverage = trials.rule_coverage(own, [])
    type_coverage = trials.task_type_coverage(own, [], {})
    if not any(p.error for p in problems):
        world = sandbox.World(
            definitions=definitions,
            skills=skills,
            task_types=task_types,
            agents=agents,
            roles=frozenset(o.key for o in objects if o.kind == "Role"),
            calendars=calendars,
            **settings.world,
        )
        process_results = [sandbox.run_test(world, t.file, t.data) for t in processes]
        results = [
            {**r.out(), "subject": d["SUBJECT_PROCESS"], "object": r.process}
            for r in process_results
        ]
        coverage = sandbox.package_coverage(
            [definitions[o.key] for o in own.of_kind("Process") if o.key in definitions],
            process_results,
        )
        if subjects and database:
            required = {k: p for k, p in parsed.items() if k != target.key}
            report = subject_tests(d, database, with_requires(d, own, required), subjects)
            for problem in report.problems:
                if problem not in problems:
                    problems.append(problem)
            results += [r.out() for r in report.tests]
            # Покрытие — только своих правил и типов: объекты requires здесь не тестируются.
            mine = {o.key for o in own.objects}
            rule_coverage = [c for c in report.rule_coverage if c.rule in mine]
            type_coverage = [c for c in report.task_type_coverage if c.task_type in mine]
        elif subjects:
            problems.append(
                pd.Problem(
                    DATABASE_REQUIRED,
                    "warning",
                    "/tests",
                    f"work rule and task type tests {len(subjects)} — they run in the core's "
                    "application code in a rolled-back PostgreSQL transaction, and there is no "
                    "sandbox database; they were not run",
                    hint=f"an empty database — --database-url or {DATABASE_ENV}; or --server",
                )
            )
            results += [_skipped(t) for t in subjects]
        order = {test.file: index for index, test in enumerate(chosen)}
        results.sort(key=lambda result: order.get(result["file"], len(order)))
    problems.sort(key=lambda p: (not p.error, p.file or "", p.line or 0, p.path, p.code))
    status = (
        "invalid"
        if any(p.error for p in problems)
        else ("failed" if any(r["status"] != "passed" for r in results) else "passed")
    )
    return {
        "status": status,
        "checkOnly": False,
        "problems": [p.out() for p in problems],
        "tests": results,
        "coverage": [c.out() for c in coverage],
        "ruleCoverage": [c.out() for c in rule_coverage],
        "taskTypeCoverage": [c.out() for c in type_coverage],
        "durationMs": int((time.monotonic() - started) * 1000),
    }


def run_package(
    key: str | Path,
    *,
    tests: list[str] | None = None,
    env: dict[str, str] | None = None,
    database: str | None = None,
) -> dict[str, Any]:
    """PackageTestOut пакета `key` (ключ или каталог с package.yaml) — см. run_installation."""
    installation = cp.resolve_targets([str(key)])
    return run_installation(
        installation, installation.packages[-1], tests=tests, env=env, database=database
    )


def packages_with_tests() -> list[str]:
    """Ключи пакетов, у которых есть тесты (tests/*.test.yaml)."""
    return sorted(d.name for d in cp.all_package_dirs() if any((d / "tests").glob("*.test.yaml")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="package-sdk sandbox",
        description="package tests by the core's code in-process, without a stand",
    )
    parser.add_argument(
        "packages",
        nargs="*",
        help="package keys or directories with package.yaml; by default all packages with tests",
    )
    parser.add_argument(
        "--test",
        action="append",
        help="path of a test file in the package (tests/<name>.test.yaml)",
    )
    parser.add_argument("--json", action="store_true", help="PackageTestOut responses as JSON")
    parser.add_argument(
        "--env", type=Path, default=Path(".env"), help="installation variables file"
    )
    parser.add_argument(
        "--database-url",
        help=f"an empty PostgreSQL database for work rule and task type tests (or {DATABASE_ENV})",
    )
    args = parser.parse_args(argv)
    env = {**cp.read_env_file(args.env), **os.environ}
    keys = args.packages or packages_with_tests()
    ok = True
    reports = []
    for key in keys:
        try:
            report = run_package(
                key, tests=args.test, env=env, database=database_url(args.database_url)
            )
        except (CoreMissing, cp.PackageError) as exc:
            print("error:", exc, file=sys.stderr)
            return 2
        reports.append(report)
        ok = ok and report["status"] == "passed"
        if not args.json:
            print(f"== {key} (in-process core sandbox, {report['durationMs']} ms)")
            cp_core.print_test_report(report)
    if args.json:
        print(
            json.dumps(reports if len(reports) != 1 else reports[0], ensure_ascii=False, indent=2)
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
