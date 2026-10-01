"""Пакеты каталога Control Plane (TAI-ADR-0044): загрузка, проверка, применение, экспорт.

Пакет — каталог packages/<key>/ с package.yaml и YAML-файлами объектов в обёртке
{apiVersion, kind, key, spec}; spec — тело запроса API control-plane. Установка —
файл окружения (kind: Installation): какие пакеты ставить и какие ключи вывести
из оборота. Команды каталога CLI package-sdk (TAI-ADR-0062):

    package-sdk check                              # все пакеты, без стенда
    package-sdk check --install deploy/packages.yaml
    package-sdk export --server https://cp.example.com --kind TaskType --key <ключ> \\
        --package packages/<пакет>

Установка — один план на все виды (TAI-ADR-0062 п.6–7): источники фиксируются в
packages.lock, план строится без записи и сохраняется с хэшем, применяется ровно он и только
после подтверждения человека:

    package-sdk lock  --install deploy/packages.yaml
    package-sdk plan  --install deploy/packages.yaml --server https://cp.example.com \\
        --out plan.json
    package-sdk apply --plan plan.json
    package-sdk cache prune                        # выгрузки git без ссылок из packages.lock

Процессы и календари (TAI-ADR-0054, виды Process и Calendar) проверяет ядро, а тесты
пакета (tests/*.test.yaml) прогоняет его песочница:

    package-sdk check --install deploy/packages.yaml --server https://cp.example.com
    package-sdk test  --package packages/<пакет> --server https://cp.example.com
    package-sdk migrate-expr --package packages/<пакет> [--write]

Токен для plan/apply/export: переменная CP_TOKEN (access token audience control-plane)
или credential MCP-плагина через control_plane_client — запускать тогда
с extra connector (control-plane-client). Токен credential берётся перед каждым запросом и
обновляется, когда истекает (package_sdk.auth); CP_TOKEN не обновляется — его срок на
человеке.

Вид NotificationRule (TAI-ADR-0053, ADR-0005 notification-service) применяется не к
ядру, а к сервису уведомлений: адрес — переменная установки NOTIFICATION_SERVICE_URL,
токен — NOTIFY_TOKEN (audience notification-service, scope notifications:admin) или
обмен того же IAM credential на этот audience через control_plane_client.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - окружение без PyYAML
    yaml = None

from package_sdk import install as install_module
from package_sdk import schema as schema_module
from package_sdk.apply import Applier, Http, HttpLike, _fetch, dump_document, to_document
from package_sdk.auth import Authorized, Bearer
from package_sdk.check import CORE_MISSING, check
from package_sdk.core import (
    CoreRejected,
    CoreUnsupported,
    ProcessApi,
    core_errors,
    core_warnings,
    format_core_error,
    static_error,
)
from package_sdk.migrate import migrate_expressions
from package_sdk.model import (
    CATALOG_KINDS,
    DEFAULT_REPLAY_LIMIT,
    FOLDERS,
    NOTIFY_AUDIENCE,
    NOTIFY_SCOPES,
    NOTIFY_TOKEN_ENV,
    NOTIFY_URL_ENV,
    PLAN_KINDS,
    Installation,
    Package,
    PackageError,
    _rel,
    all_package_dirs,
    load_package,
    package_name,
    read_env_file,
    resolve,
    resolve_targets,
)
from package_sdk.source import plan_request, test_request


def _bearer(server: str) -> Bearer:
    """Учётка ядра для CLI и MCP-сервера автора: CP_TOKEN или credential оператора так же, как
    его находит control-plane-client (``resolve_credential``: IAM, затем API key) — порядок
    поиска здесь не повторяется. Поставщик, а не строка: access token живёт минуты, а план и
    применение — дольше (TASK-001256); CP_TOKEN — явный выбор человека и не обновляется."""
    token = os.environ.get("CP_TOKEN")
    if token:
        return Bearer.static_token(token)
    try:
        from control_plane_client.credentials import resolve_credential
    except ImportError as error:
        raise PackageError(
            "нет CP_TOKEN и нет control_plane_client — задайте CP_TOKEN или поставьте "
            "клиент ядра: package-sdk[connector] или package-sdk[mcp]"
        ) from error
    credential = resolve_credential(server)
    if credential is None:
        raise PackageError(f"нет credential для {server} в ~/.config/iam/credentials.json")
    return Bearer.of_credential(credential)


def _bearer_for(
    audience: str, scopes: tuple[str, ...], *, fallback: str, environ: dict[str, str]
) -> Bearer:
    """Токен другого audience тем же IAM credential: переменная fallback (как CP_TOKEN) или
    обмен PAT через control_plane_client — его IamCredential принимает audience и scopes
    (CONTROL_PLANE_IAM_AUDIENCE/SCOPES), обмен здесь не переписывается. Legacy API key ядра
    на другой audience не меняется — тогда только переменная. PAT должен допускать audience
    в потолке, иначе IAM ответит iam_audience_not_allowed."""
    token = environ.get(fallback)
    if token:
        return Bearer.static_token(token)
    try:
        from control_plane_client.iam import (
            ENV_IAM_AUDIENCE,
            ENV_IAM_SCOPES,
            iam_credential_from_environment,
        )
    except ImportError as error:
        raise PackageError(
            f"нет {fallback} и нет control_plane_client — задайте {fallback} (access token "
            f"audience {audience}) или поставьте клиент ядра: package-sdk[connector] или "
            "package-sdk[mcp]"
        ) from error
    credential = iam_credential_from_environment(
        {**environ, ENV_IAM_AUDIENCE: audience, ENV_IAM_SCOPES: " ".join(scopes)}
    )
    if credential is None:
        raise PackageError(
            f"нет {fallback}, а IAM credential не настроен (CONTROL_PLANE_IAM_URL) — токен "
            f"audience {audience} получить нечем"
        )
    return Bearer.of_credential(credential)


def _notify_target(environ: dict[str, str]) -> tuple[Authorized, dict[str, str]]:
    """Сервис уведомлений: транспорт со свежим токеном перед каждым запросом и пустые
    прочие заголовки (форма ``Target.notify``)."""
    url = environ.get(NOTIFY_URL_ENV)
    if not url:
        raise PackageError(
            f"NotificationRule применяются к сервису уведомлений — задайте {NOTIFY_URL_ENV}"
        )
    token = _bearer_for(NOTIFY_AUDIENCE, NOTIFY_SCOPES, fallback=NOTIFY_TOKEN_ENV, environ=environ)
    return Authorized(Http(url), token), {}


def _installation_from(args: argparse.Namespace) -> tuple[Installation, list[str]]:
    """Установка из --install или пакеты из --package (путь каталога или ключ); второе —
    ключи пакетов, названных явно (их ядро и проверяет: запрос — один пакет)."""
    if getattr(args, "install", None):
        # источники git — по lock, из кэша; сверка с источником и lock — дело плана
        installation = install_module.load(args.install, strict=False).installation
        return installation, [p.key for p in installation.packages]
    values = list(getattr(args, "package", None) or [])
    if not values:
        installation = resolve([d.name for d in all_package_dirs()])
        return installation, [p.key for p in installation.packages]
    installation = resolve_targets(values)
    names = [
        package_name(v) if (Path(v) / "package.yaml").is_file() else Path(v).name for v in values
    ]
    return installation, names


def _named(installation: Installation, keys: list[str]) -> list[Package]:
    return [p for p in installation.packages if p.key in keys]


def _process_api(server: str) -> ProcessApi:
    return ProcessApi(Authorized(Http(server), _bearer(server)), {})


def check_report(
    installation: Installation,
    named: list[str],
    *,
    env: dict[str, str],
    server: str | None = None,
    workspace: str | None = None,
    schema_only: bool = False,
) -> dict:
    """Отчёт проверки пакетов (то же, что ``check --json``): статическая проверка и, с
    server, проверка ядром (checkOnly) пакетов, названных явно. Общий для CLI и MCP-сервера
    автора (``package_sdk.mcp``)."""
    errors, warnings = check(installation, env=env)
    if CORE_MISSING in warnings and not schema_only:
        # Молча проверять одну схему нельзя (TAI-ADR-0062, FR-014): это отдельный режим.
        warnings.remove(CORE_MISSING)
        errors.append(CORE_MISSING + " — либо запустите с --schema-only")
    problems = [static_error(e) for e in errors]
    core = "skipped"
    if server:
        try:
            api = _process_api(server)
            for package in _named(installation, named):
                try:
                    response = api.test(
                        test_request(package, env, workspace=workspace), check_only=True
                    )
                except CoreRejected as error:
                    problems += error.errors
                    continue
                problems += core_errors(response)
                warnings += [format_core_error(w) for w in core_warnings(response)]
            core = "checked"
        except CoreUnsupported as error:
            core = "unsupported"
            warnings.append(
                f"ядро не поддерживает проверку процессов ({error}) — проверена только схема"
            )
        except (urllib.error.URLError, OSError) as error:
            core = "unreachable"
            warnings.append(f"ядро недоступно ({error}) — проверена только схема")
    return {
        "ok": not problems,
        "errors": problems,
        "warnings": warnings,
        "core": core,
        "packages": len(installation.packages),
        "objects": len(installation.objects),
        "tests": len(installation.tests),
    }


def _check_command(args: argparse.Namespace) -> int:
    installation, named = _installation_from(args)
    env = {**(read_env_file(args.env) if args.env else {}), **os.environ}
    report = check_report(
        installation,
        named,
        env=env,
        server=args.server,
        workspace=args.workspace,
        schema_only=args.schema_only,
    )
    problems, warnings, core = report["errors"], report["warnings"], report["core"]
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for warning in warnings:
            print("предупреждение:", warning)
        for problem in problems:
            print(
                "ошибка:",
                format_core_error(problem)
                if problem.get("code") != "static_check"
                else (f"{problem['file']}: " if problem.get("file") else "") + problem["message"],
            )
        print(
            f"{'не пройдено' if problems else 'ok'}: пакетов {report['packages']}, "
            f"объектов {report['objects']}, тестов {report['tests']}"
            + {
                "checked": ", проверено ядром",
                "unsupported": ", ядро: только схема",
                "unreachable": ", ядро недоступно",
                "skipped": "",
            }[core]
        )
    return 1 if problems else 0


def _target(server: str, env: dict[str, str], installation: Path) -> install_module.Target:
    """Стенд плана и применения: ядро и, если установка его касается, сервис уведомлений."""
    target = install_module.Target(
        server=server,
        http=Http(server),
        token=_bearer(server),
    )
    loaded = install_module.load(installation, strict=False).installation
    uses_notify = any(o.kind == "NotificationRule" for o in loaded.objects) or bool(
        loaded.retire.get("NotificationRule")
    )
    if uses_notify:
        target.notify = _notify_target(env)
    return target


def _plan_command(args: argparse.Namespace, env: dict[str, str]) -> int:
    log = (lambda _m: None) if args.json else print
    document = install_module.plan(
        args.install,
        target=_target(args.server, env, args.install),
        env=env,
        workspace=args.workspace,
        replay_limit=args.replay_limit,
        overwrite_console=args.overwrite_console,
        out=args.out,
        log=log,
    )
    if args.json:
        print(json.dumps(document, ensure_ascii=False, indent=2))
    else:
        print(f"план сохранён: {args.out} — применить: package-sdk apply --plan {args.out}")
    return 0


def _ask(question: str) -> bool:
    """Подтверждение человека в терминале; без терминала — отказ."""
    if not sys.stdin.isatty():
        print(f"{question} — нужен ответ человека в терминале, применение отменено")
        return False
    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes", "д", "да")


def _apply_plan_command(args: argparse.Namespace, env: dict[str, str]) -> int:
    document = install_module.read_plan(args.plan)
    # адрес стенда называет человек, а не файл плана: план применяется туда, куда сказали
    server = args.server.rstrip("/")
    if server != document["server"]:
        raise PackageError(f"план построен для {document['server']}, а применяется к {server}")
    installation = install_module.install_file(document, args.plan)
    install_module.apply(
        args.plan,
        target=_target(server, env, installation),
        env=env,
        confirm=_ask,
    )
    return 0


def _lock_command(args: argparse.Namespace) -> int:
    install_module.lock(args.install)
    return 0


def _cache_command(args: argparse.Namespace) -> int:
    """package-sdk cache prune: выгрузки кэша git, на которые не ссылается ни один lock."""
    if args.all:
        result = install_module.prune(None)
        print(f"кэш источников git очищен: удалено {len(result.removed)}")
        return 0
    locks = [Path(p) for p in args.lock] if args.lock else install_module.find_locks(Path("."))
    if not locks:
        # без единого lock «не нужные ни одному lock» — это все выгрузки: молча так не чистим
        raise PackageError(
            "под текущим каталогом нет packages.lock — запустите prune в каталоге установок, "
            "назовите их lock-файлы (--lock) или очистите весь кэш явно (--all); ничего не удалено"
        )
    for path in locks:
        print(f"   учтён {_rel(path.resolve())}")
    result = install_module.prune(locks)
    print(
        f"выгрузок удалено: {len(result.removed)}, оставлено по lock: {result.kept}, "
        f"недавно использованных: {result.recent}"
    )
    return 0


def _export_planned(
    args: argparse.Namespace, http: HttpLike, headers: dict[str, str], env: dict[str, str]
) -> int:
    """Process и Calendar (FR-008): правка файла пакета с сохранением комментариев; поля,
    которые на стенде правил человек, — из плана ядра по нынешнему пакету."""
    from package_sdk import export

    plan: dict | None = None
    if (args.package / "package.yaml").exists():
        try:
            package = load_package(args.package)
            plan = ProcessApi(http, headers).plan(
                plan_request(package, env, workspace=getattr(args, "workspace", None))
            )
        except (PackageError, RuntimeError, OSError) as error:
            print(f"предупреждение: план ядра не построен ({error}) — поля консоли не отмечены")
    for key in args.key:
        body = export.fetch(http, headers, args.kind, key, args.version)
        print(export.export_object(args.package, args.kind, key, body, plan=plan, env=env).line())
    return 0


def _describe_command(args: argparse.Namespace) -> int:
    """Предпосылки установки пакета (FR-011): переменные, узлы агентов, онтологии,
    зависимости, совместимость; --env-example — заготовка файла переменных."""
    from package_sdk import manifest

    package, installation, problem = manifest.load_for_describe(args.path)
    info = manifest.describe(package, installation)
    if args.env_example:
        print(manifest.env_example(info), end="")
    elif args.json:
        print(json.dumps({**info, "problem": problem}, ensure_ascii=False, indent=2))
    else:
        print(manifest.format_describe(info, problem), end="")
    return 0


def _docs_command(args: argparse.Namespace) -> int:
    """Сгенерированные разделы README пакета: печать, --write или --check (для CI)."""
    from package_sdk import manifest

    package, installation, _problem = manifest.load_for_describe(args.path)
    section = manifest.render_docs(manifest.describe(package, installation), package)
    if not (args.write or args.check):
        print(section, end="")
        return 0
    readme = args.path / "README.md"
    current = readme.read_text(encoding="utf-8") if readme.exists() else ""
    updated = manifest.apply_docs(current, section)
    if args.check:
        if updated != current:
            print(f"устарел раздел README: {_rel(readme)} — запустите package-sdk docs --write")
            return 1
        print(f"ok: {_rel(readme)}")
        return 0
    if updated != current:
        readme.write_text(updated, encoding="utf-8")
        print("записан", _rel(readme))
    return 0


def main(argv: list[str] | None = None) -> int:
    given = sys.argv[1:] if argv is None else argv
    if given and given[0] == "test":
        # пирамида тестов — своя команда со своими аргументами (FR-017)
        from package_sdk import testing

        return testing.main(given[1:])
    parser = argparse.ArgumentParser(
        prog="package-sdk",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check_cmd = sub.add_parser(
        "check", help="проверить пакеты: схема и ссылки; с --server — ещё и ядром"
    )
    check_cmd.add_argument(
        "--install", type=Path, help="файл установки; без него — все пакеты packages/"
    )
    check_cmd.add_argument(
        "--package", action="append", help="пакет (каталог или ключ); можно несколько"
    )
    check_cmd.add_argument(
        "--server", help="Control Plane: проверка процессов ядром (checkOnly), если оно умеет"
    )
    check_cmd.add_argument(
        "--env", type=Path, help="откуда брать ${ПЕРЕМЕННЫЕ} пакета (по умолчанию — окружение)"
    )
    check_cmd.add_argument(
        "--workspace", help="workspace, чьи роли и календари читает проверка ядром"
    )
    check_cmd.add_argument(
        "--json",
        action="store_true",
        help="ошибки — JSON {code, severity, path, file, line, message, hint}",
    )
    check_cmd.add_argument(
        "--schema-only",
        action="store_true",
        help="без кода ядра: только схема и ссылки (иначе отсутствие ядра — ошибка)",
    )
    lock_cmd = sub.add_parser(
        "lock", help="зафиксировать источники установки: коммит и хэш содержимого (packages.lock)"
    )
    lock_cmd.add_argument("--install", type=Path, required=True, help="файл установки")
    cache_cmd = sub.add_parser("cache", help="кэш источников git (package-sdk cache prune)")
    cache_sub = cache_cmd.add_subparsers(dest="cache_command", required=True)
    prune_cmd = cache_sub.add_parser(
        "prune",
        help="удалить выгрузки, на которые не ссылается ни один packages.lock текущего каталога",
    )
    prune_cmd.add_argument(
        "--all", action="store_true", help="удалить весь кэш: выгрузки и зеркала источников"
    )
    prune_cmd.add_argument(
        "--lock",
        action="append",
        help="учесть этот lock-файл (можно несколько); по умолчанию — все packages.lock "
        "под текущим каталогом",
    )
    plan_cmd = sub.add_parser(
        "plan", help="построить единый план установки (все виды) и сохранить его с хэшем"
    )
    plan_cmd.add_argument("--install", type=Path, required=True, help="файл установки")
    plan_cmd.add_argument("--server", required=True)
    plan_cmd.add_argument(
        "--out", type=Path, required=True, help="файл плана (package-sdk.plan/v1) для apply --plan"
    )
    plan_cmd.add_argument(
        "--env", type=Path, default=Path(".env"), help="откуда брать ${ПЕРЕМЕННЫЕ} пакета"
    )
    plan_cmd.add_argument("--workspace", help="workspace процессов пакета (workspaceId ядра)")
    plan_cmd.add_argument(
        "--replay-limit",
        type=int,
        default=DEFAULT_REPLAY_LIMIT,
        help="экземпляров на процесс для replay (0–200)",
    )
    plan_cmd.add_argument(
        "--overwrite-console",
        action="store_true",
        help="перезаписать поля, которые человек правил в консоли после прошлого применения "
        "(по умолчанию они сохраняются); флаг хранится в плане под его хэшем",
    )
    plan_cmd.add_argument("--json", action="store_true", help="план документом JSON")
    apply_cmd = sub.add_parser(
        "apply", help="применить ровно сохранённый план (после подтверждения человека)"
    )
    apply_cmd.add_argument("--plan", type=Path, required=True, help="файл плана от plan --out")
    apply_cmd.add_argument(
        "--server", required=True, help="стенд; должен совпасть с тем, для которого построен план"
    )
    apply_cmd.add_argument(
        "--env", type=Path, default=Path(".env"), help="откуда брать ${ПЕРЕМЕННЫЕ} пакета"
    )
    migrate_cmd = sub.add_parser(
        "migrate-expr", help="перевести прежние выражения пакета в CEL (diff; --write)"
    )
    migrate_cmd.add_argument("--package", required=True, help="пакет (каталог или ключ)")
    migrate_cmd.add_argument("--write", action="store_true", help="записать с сохранением файла")
    export_cmd = sub.add_parser("export", help="выгрузить объекты из Control Plane в пакет")
    export_cmd.add_argument("--server", help="Control Plane; для NotificationRule не нужен")
    export_cmd.add_argument(
        "--env",
        type=Path,
        default=Path(".env"),
        help=f"файл переменных установки: {NOTIFY_URL_ENV} для NotificationRule, значения "
        "${…} — чтобы выгрузка Process и Calendar вернула их ссылками на переменные",
    )
    export_cmd.add_argument(
        "--workspace", help="workspace плана ядра для полей консоли (Process и Calendar)"
    )
    export_cmd.add_argument("--kind", required=True, choices=list(CATALOG_KINDS))
    export_cmd.add_argument("--key", required=True, action="append")
    export_cmd.add_argument("--version", help="версия (по умолчанию новейшая активная)")
    export_cmd.add_argument(
        "--package", type=Path, required=True, help="каталог пакета, например packages/<пакет>"
    )
    init_cmd = sub.add_parser(
        "init", help="заготовка пакета: манифест, процесс с тестом, CI, README"
    )
    init_cmd.add_argument("dir", type=Path, help="каталог пакета (новый или пустой)")
    init_cmd.add_argument("--key", help="ключ пакета; по умолчанию — имя каталога")
    init_cmd.add_argument("--display-name", help="название пакета для людей")
    init_cmd.add_argument("--license", help="лицензия пакета (идентификатор SPDX)")
    init_cmd.add_argument(
        "--integration", action="store_true", help="код интеграции: наблюдатель и описание агента"
    )
    init_cmd.add_argument("--image", action="store_true", help="Dockerfile образа интеграции")
    init_cmd.add_argument(
        "--database",
        action="store_true",
        help="сервис PostgreSQL в CI для сценариев правил и типов задач "
        "(по умолчанию — если они уже есть в каталоге)",
    )
    add_cmd = sub.add_parser("add", help="заготовка объекта любого вида каталога")
    add_cmd.add_argument("kind", help="вид: TaskType, task-type, rule, process, …")
    add_cmd.add_argument("key", help="ключ объекта")
    add_cmd.add_argument(
        "--package", type=Path, default=Path("."), help="каталог пакета (по умолчанию текущий)"
    )
    describe_cmd = sub.add_parser(
        "describe",
        help="предпосылки установки пакета: переменные, узлы агентов, онтологии, зависимости",
    )
    describe_cmd.add_argument("path", type=Path, help="каталог пакета")
    describe_cmd.add_argument(
        "--env-example", action="store_true", help="заготовка файла переменных установки"
    )
    describe_cmd.add_argument("--json", action="store_true", help="то же в JSON")
    docs_cmd = sub.add_parser("docs", help="сгенерированные разделы README пакета")
    docs_cmd.add_argument("path", type=Path, help="каталог пакета")
    mode = docs_cmd.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="обновить раздел в README.md")
    mode.add_argument("--check", action="store_true", help="1, если раздел README устарел")
    args = parser.parse_args(argv)

    try:
        if args.command in ("init", "add"):
            from package_sdk import scaffold

            if args.command == "init":
                result = scaffold.init(
                    args.dir,
                    key=args.key,
                    display_name=args.display_name,
                    license=args.license,
                    integration=args.integration,
                    image=args.image,
                    database=True if args.database else None,
                )
            else:
                result = scaffold.add(args.package, args.kind, args.key)
            for path in result.created:
                print("создан", _rel(path))
            for path in result.updated:
                print("изменён", _rel(path))
            for path in result.skipped:
                print("оставлен как был", _rel(path))
            for warning in result.warnings:
                print("предупреждение:", warning)
            return 0
        if args.command == "describe":
            return _describe_command(args)
        if args.command == "docs":
            return _docs_command(args)
        if args.command == "check":
            return _check_command(args)
        if args.command == "migrate-expr":
            package = resolve_targets([args.package]).packages[-1]
            migrate_expressions(package, write=args.write)
            return 0
        if args.command == "lock":
            return _lock_command(args)
        if args.command == "cache":
            return _cache_command(args)

        env = {**read_env_file(args.env), **os.environ}
        if args.command == "plan":
            return _plan_command(args, env)
        if args.command == "apply":
            return _apply_plan_command(args, env)
        if args.command == "export" and args.kind == "NotificationRule":
            notify_http, notify_headers = _notify_target(env)
            # ядро выгрузке правила уведомлений не нужно
            applier = Applier(
                None,
                {},
                log=lambda _m: None,  # type: ignore[arg-type]
                notify=notify_http,
                notify_headers=notify_headers,
            )
        else:
            if not args.server:
                raise PackageError("нужен --server (адрес Control Plane)")
            http = Authorized(Http(args.server), _bearer(args.server))
            headers: dict[str, str] = {}
            if args.kind in PLAN_KINDS:
                return _export_planned(args, http, headers, env)
            applier = Applier(http, headers, log=lambda _m: None)
        target = args.package / FOLDERS[args.kind]
        target.mkdir(parents=True, exist_ok=True)
        schema_rel = os.path.relpath(schema_module.schema_dir() / "object.schema.json", target)
        for key in args.key:
            body = _fetch(applier, args.kind, key, args.version)
            path = target / f"{key}.yaml"
            path.write_text(
                dump_document(to_document(args.kind, key, body), schema_rel), encoding="utf-8"
            )
            print("записан", _rel(path), f"(v{body.get('version')})" if "version" in body else "")
        return 0
    except CoreUnsupported as error:
        print(
            f"ошибка: {error} — проверьте, что Control Plane новее движка процессов (CP-ADR-0074)",
            file=sys.stderr,
        )
        return 1
    except (PackageError, RuntimeError) as error:
        print("ошибка:", error, file=sys.stderr)
        return 1
    except urllib.error.URLError as error:
        print(f"ошибка: Control Plane недоступен: {error.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
