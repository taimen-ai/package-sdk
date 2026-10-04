"""Control Plane catalog packages (TAI-ADR-0044): load, check, apply, export.

A package is a directory packages/<key>/ with package.yaml and YAML object files in the
{apiVersion, kind, key, spec} envelope; spec is the control-plane API request body. An
installation is an environment file (kind: Installation): which packages to install and which
keys to retire. Catalog commands of the package-sdk CLI (TAI-ADR-0062):

    package-sdk check                              # all packages, no stand
    package-sdk check --install deploy/packages.yaml
    package-sdk export --server https://cp.example.com --kind TaskType --key <key> \\
        --package packages/<package>

Installation is one plan for all kinds (TAI-ADR-0062 p.6-7): sources are pinned in
packages.lock, the plan is built without writes and saved with its hash, and exactly that plan
is applied, only after a human confirms:

    package-sdk lock  --install deploy/packages.yaml
    package-sdk plan  --install deploy/packages.yaml --server https://cp.example.com \\
        --out plan.json
    package-sdk apply --plan plan.json
    package-sdk cache prune                        # git checkouts not referenced by packages.lock

Processes and calendars (TAI-ADR-0054, kinds Process and Calendar) are checked by the core, and
package tests (tests/*.test.yaml) are run by its sandbox:

    package-sdk check --install deploy/packages.yaml --server https://cp.example.com
    package-sdk test  --package packages/<package> --server https://cp.example.com
    package-sdk migrate-expr --package packages/<package> [--write]

Token for plan/apply/export: the CP_TOKEN variable (access token, audience control-plane)
or the MCP plugin credential via control_plane_client — then run with
extra connector (control-plane-client). The credential token is taken before each request and
refreshed when it expires (package_sdk.auth); CP_TOKEN is not refreshed — its lifetime is up to
the human.

Kind NotificationRule (TAI-ADR-0053, ADR-0005 notification-service) is applied not to the
core but to the notification service: the address is the installation variable
NOTIFICATION_SERVICE_URL, the token is NOTIFY_TOKEN (audience notification-service, scope
notifications:admin) or an exchange of the same IAM credential for that audience via
control_plane_client.
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
            "no CP_TOKEN and no control_plane_client — set CP_TOKEN or install "
            "the core client: package-sdk[connector] or package-sdk[mcp]"
        ) from error
    credential = resolve_credential(server)
    if credential is None:
        raise PackageError(f"no credential for {server} in ~/.config/iam/credentials.json")
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
            f"no {fallback} and no control_plane_client — set {fallback} (access token "
            f"audience {audience}) or install the core client: package-sdk[connector] or "
            "package-sdk[mcp]"
        ) from error
    credential = iam_credential_from_environment(
        {**environ, ENV_IAM_AUDIENCE: audience, ENV_IAM_SCOPES: " ".join(scopes)}
    )
    if credential is None:
        raise PackageError(
            f"no {fallback}, and the IAM credential is not configured (CONTROL_PLANE_IAM_URL) — "
            f"nothing to obtain a token for audience {audience} with"
        )
    return Bearer.of_credential(credential)


def _notify_target(environ: dict[str, str]) -> tuple[Authorized, dict[str, str]]:
    """Сервис уведомлений: транспорт со свежим токеном перед каждым запросом и пустые
    прочие заголовки (форма ``Target.notify``)."""
    url = environ.get(NOTIFY_URL_ENV)
    if not url:
        raise PackageError(
            f"NotificationRule objects are applied to the notification service — set {NOTIFY_URL_ENV}"
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
        errors.append(CORE_MISSING + " — or run with --schema-only")
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
                f"the core does not support checking processes ({error}) — only the schema was checked"
            )
        except (urllib.error.URLError, OSError) as error:
            core = "unreachable"
            warnings.append(f"the core is unreachable ({error}) — only the schema was checked")
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
            print("warning:", warning)
        for problem in problems:
            print(
                "error:",
                format_core_error(problem)
                if problem.get("code") != "static_check"
                else (f"{problem['file']}: " if problem.get("file") else "") + problem["message"],
            )
        print(
            f"{'failed' if problems else 'ok'}: packages {report['packages']}, "
            f"objects {report['objects']}, tests {report['tests']}"
            + {
                "checked": ", checked by the core",
                "unsupported": ", core: schema only",
                "unreachable": ", core unreachable",
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
        print(f"plan saved: {args.out} — to apply: package-sdk apply --plan {args.out}")
    return 0


def _ask(question: str) -> bool:
    """Подтверждение человека в терминале; без терминала — отказ."""
    if not sys.stdin.isatty():
        print(f"{question} — a human answer in a terminal is required, apply cancelled")
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
        raise PackageError(
            f"the plan was built for {document['server']}, but is applied to {server}"
        )
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
        print(f"git source cache cleared: removed {len(result.removed)}")
        return 0
    locks = [Path(p) for p in args.lock] if args.lock else install_module.find_locks(Path("."))
    if not locks:
        # без единого lock «не нужные ни одному lock» — это все выгрузки: молча так не чистим
        raise PackageError(
            "no packages.lock under the current directory — run prune in the installations directory, "
            "name their lock files (--lock) or clear the whole cache explicitly (--all); nothing removed"
        )
    for path in locks:
        print(f"   counted {_rel(path.resolve())}")
    result = install_module.prune(locks)
    print(
        f"checkouts removed: {len(result.removed)}, kept by lock: {result.kept}, "
        f"recently used: {result.recent}"
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
            print(f"warning: the core plan was not built ({error}) — console fields are not marked")
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
            print(f"README section is outdated: {_rel(readme)} — run package-sdk docs --write")
            return 1
        print(f"ok: {_rel(readme)}")
        return 0
    if updated != current:
        readme.write_text(updated, encoding="utf-8")
        print("written", _rel(readme))
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
        "check", help="check packages: schema and references; with --server also by the core"
    )
    check_cmd.add_argument(
        "--install", type=Path, help="installation file; without it, all packages in packages/"
    )
    check_cmd.add_argument(
        "--package", action="append", help="package (directory or key); may be repeated"
    )
    check_cmd.add_argument(
        "--server", help="Control Plane: check processes by the core (checkOnly) if it supports it"
    )
    check_cmd.add_argument(
        "--env",
        type=Path,
        help="where to take package ${VARIABLES} from (default: the environment)",
    )
    check_cmd.add_argument(
        "--workspace", help="workspace whose roles and calendars the core check reads"
    )
    check_cmd.add_argument(
        "--json",
        action="store_true",
        help="errors as JSON {code, severity, path, file, line, message, hint}",
    )
    check_cmd.add_argument(
        "--schema-only",
        action="store_true",
        help="without the core's code: schema and references only (otherwise a missing core is an error)",
    )
    lock_cmd = sub.add_parser(
        "lock", help="pin installation sources: commit and content hash (packages.lock)"
    )
    lock_cmd.add_argument("--install", type=Path, required=True, help="installation file")
    cache_cmd = sub.add_parser("cache", help="git source cache (package-sdk cache prune)")
    cache_sub = cache_cmd.add_subparsers(dest="cache_command", required=True)
    prune_cmd = cache_sub.add_parser(
        "prune",
        help="remove checkouts not referenced by any packages.lock of the current directory",
    )
    prune_cmd.add_argument(
        "--all", action="store_true", help="remove the whole cache: checkouts and source mirrors"
    )
    prune_cmd.add_argument(
        "--lock",
        action="append",
        help="take this lock file into account (may be repeated); by default all packages.lock "
        "under the current directory",
    )
    plan_cmd = sub.add_parser(
        "plan", help="build the one installation plan (all kinds) and save it with its hash"
    )
    plan_cmd.add_argument("--install", type=Path, required=True, help="installation file")
    plan_cmd.add_argument("--server", required=True)
    plan_cmd.add_argument(
        "--out", type=Path, required=True, help="plan file (package-sdk.plan/v1) for apply --plan"
    )
    plan_cmd.add_argument(
        "--env", type=Path, default=Path(".env"), help="where to take package ${VARIABLES} from"
    )
    plan_cmd.add_argument(
        "--workspace", help="workspace of the package processes (the core's workspaceId)"
    )
    plan_cmd.add_argument(
        "--replay-limit",
        type=int,
        default=DEFAULT_REPLAY_LIMIT,
        help="instances per process for replay (0–200)",
    )
    plan_cmd.add_argument(
        "--overwrite-console",
        action="store_true",
        help="overwrite fields a human edited in the console since the last apply "
        "(by default they are kept); the flag is stored in the plan under its hash",
    )
    plan_cmd.add_argument("--json", action="store_true", help="the plan as a JSON document")
    apply_cmd = sub.add_parser(
        "apply", help="apply exactly the saved plan (after a human confirms)"
    )
    apply_cmd.add_argument("--plan", type=Path, required=True, help="plan file from plan --out")
    apply_cmd.add_argument(
        "--server", required=True, help="stand; must match the one the plan was built for"
    )
    apply_cmd.add_argument(
        "--env", type=Path, default=Path(".env"), help="where to take package ${VARIABLES} from"
    )
    migrate_cmd = sub.add_parser(
        "migrate-expr", help="migrate legacy package expressions to CEL (diff; --write)"
    )
    migrate_cmd.add_argument("--package", required=True, help="package (directory or key)")
    migrate_cmd.add_argument(
        "--write", action="store_true", help="write, preserving the file style"
    )
    export_cmd = sub.add_parser("export", help="export objects from Control Plane into a package")
    export_cmd.add_argument("--server", help="Control Plane; not needed for NotificationRule")
    export_cmd.add_argument(
        "--env",
        type=Path,
        default=Path(".env"),
        help=f"installation variables file: {NOTIFY_URL_ENV} for NotificationRule, values "
        "${…} — so that exporting Process and Calendar returns them as variable references",
    )
    export_cmd.add_argument(
        "--workspace", help="workspace of the core plan for console fields (Process and Calendar)"
    )
    export_cmd.add_argument("--kind", required=True, choices=list(CATALOG_KINDS))
    export_cmd.add_argument("--key", required=True, action="append")
    export_cmd.add_argument("--version", help="version (default: the newest active)")
    export_cmd.add_argument(
        "--package", type=Path, required=True, help="package directory, e.g. packages/<package>"
    )
    init_cmd = sub.add_parser(
        "init", help="package scaffold: manifest, process with a test, CI, README"
    )
    init_cmd.add_argument("dir", type=Path, help="package directory (new or empty)")
    init_cmd.add_argument("--key", help="package key; default: the directory name")
    init_cmd.add_argument("--display-name", help="human-readable package name")
    init_cmd.add_argument("--license", help="package license (SPDX identifier)")
    init_cmd.add_argument(
        "--integration",
        action="store_true",
        help="integration code: observer and agent description",
    )
    init_cmd.add_argument(
        "--image", action="store_true", help="Dockerfile of the integration image"
    )
    init_cmd.add_argument(
        "--database",
        action="store_true",
        help="PostgreSQL service in CI for work rule and task type scenarios "
        "(default: if they already exist in the directory)",
    )
    workflow_cmd = sub.add_parser(
        "workflow",
        help="regenerate the CI workflow of a package by the layout of this installation",
    )
    workflow_cmd.add_argument(
        "dir", type=Path, nargs="?", default=Path("."), help="package directory (default: .)"
    )
    workflow_cmd.add_argument(
        "--check", action="store_true", help="exit 1 if the workflow differs; write nothing"
    )
    add_cmd = sub.add_parser("add", help="scaffold of an object of any catalog kind")
    add_cmd.add_argument("kind", help="kind: TaskType, task-type, rule, process, …")
    add_cmd.add_argument("key", help="object key")
    add_cmd.add_argument(
        "--package",
        type=Path,
        default=Path("."),
        help="package directory (default: the current one)",
    )
    describe_cmd = sub.add_parser(
        "describe",
        help=(
            "package installation prerequisites: variables, settings, agent nodes, "
            "ontologies, dependencies"
        ),
    )
    describe_cmd.add_argument("path", type=Path, help="package directory")
    describe_cmd.add_argument(
        "--env-example", action="store_true", help="template of the installation variables file"
    )
    describe_cmd.add_argument("--json", action="store_true", help="the same as JSON")
    docs_cmd = sub.add_parser("docs", help="generated README sections of a package")
    docs_cmd.add_argument("path", type=Path, help="package directory")
    mode = docs_cmd.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="update the section in README.md")
    mode.add_argument(
        "--check", action="store_true", help="exit 1 if the README section is outdated"
    )
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
                print("created", _rel(path))
            for path in result.updated:
                print("changed", _rel(path))
            for path in result.skipped:
                print("unchanged", _rel(path))
            for warning in result.warnings:
                print("warning:", warning)
            return 0
        if args.command == "workflow":
            return _workflow_command(args)
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
                raise PackageError("--server is required (the Control Plane address)")
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
            print("written", _rel(path), f"(v{body.get('version')})" if "version" in body else "")
        return 0
    except CoreUnsupported as error:
        print(
            f"error: {error} — check that Control Plane is newer than the process engine (CP-ADR-0074)",
            file=sys.stderr,
        )
        return 1
    except (PackageError, RuntimeError) as error:
        print("error:", error, file=sys.stderr)
        return 1
    except urllib.error.URLError as error:
        print(f"error: Control Plane is unreachable: {error.reason}", file=sys.stderr)
        return 1


def _workflow_command(args: argparse.Namespace) -> int:
    from package_sdk import scaffold

    result = scaffold.regenerate(args.dir, check=args.check)
    layout = "flat" if result.layout.flat else "segmented"
    for warning in result.warnings:
        print("warning:", warning)
    if not result.changed:
        print("unchanged", _rel(result.path), f"(layout: {layout})")
        return 0
    if args.check:
        print(
            f"error: {_rel(result.path)} differs from the workflow of this installation "
            f"(layout: {layout}) — run package-sdk workflow",
            file=sys.stderr,
        )
        return 1
    print("changed", _rel(result.path), f"(layout: {layout}) — review the diff before committing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
