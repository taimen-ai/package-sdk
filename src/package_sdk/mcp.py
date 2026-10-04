"""MCP-сервер автора пакетов — ``package-sdk mcp`` (FR-030, plan Р10; extra ``mcp``).

Инструменты агента-автора поверх тех же функций, что у CLI, по stdio:

- ``pkg_check`` — проверка пакета или установки (схема, ссылки, валидаторы ядра; с
  ``server`` — ещё и ядром стенда), как ``package-sdk check --json``;
- ``pkg_test`` — пирамида тестов, как ``package-sdk test --json``;
- ``pkg_describe`` — предпосылки установки пакета, как ``package-sdk describe``;
- ``pkg_edit`` — операции ``package-sdk edit`` с сохранением стиля файла;
- ``pkg_plan`` — единый план установки (``package-sdk.plan/v1``) в файл и его ``planHash``;
- ``pkg_apply(plan_file, plan_hash)`` — применение ровно этого плана.

Границы, которые держит сервер, а не агент:

- **корень сессии** — ``PACKAGE_SDK_ROOT``, иначе первый корень ``file://`` клиента MCP,
  иначе рабочий каталог. От него разрешаются относительные пути и ``.env``; правка
  (``pkg_edit``, в том числе фрагменты ``@<файл>``) — только внутри него, план — только в
  ``<корень>/.package-sdk/``, и файл, который не план, не перезаписывается;
- **стенды** — только из ``PACKAGE_SDK_SERVERS`` окружения сервера (адреса через пробел или
  запятую, ``https://`` или ``http://localhost``). Адрес, который назвал агент, но которого
  нет в списке, — отказ ``server_not_allowed`` до того, как токен покинет процесс: текст в
  пакете не уведёт учётку на чужой адрес;
- **применение** — только по хэшу плана, который видел человек: без ``plan_hash`` вида
  ``sha256:<64 hex>`` или с хэшем, который не совпадает с ``planHash`` файла плана, —
  отказ до стенда. План читается один раз, и применяется ровно прочитанный документ.
  Подтверждение человека берёт хост: хук плагина автора спрашивает его перед каждым
  ``pkg_apply``. Стенд, изменившийся после плана, даёт ``plan_stale`` до первой записи.

Учётка стенда — та же, что у CLI: ``CP_TOKEN`` или credential оператора, который находит
``control-plane-client`` (``commands._bearer``); токен credential берётся перед каждым запросом
к стенду (``package_sdk.auth``), ``CP_TOKEN`` — как есть. Сервер не хранит состояния между
вызовами.
Инструменты возвращают JSON; отказ — ``{"error": <код>, "message": …, "hint"?: …}``.
"""

# Без «from __future__ import annotations»: сервер MCP читает аннотации инструментов
# (Context), объявленных внутри build_server, во время регистрации.
import argparse
import asyncio
import difflib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from package_sdk import install as install_module
from package_sdk.core import CoreUnsupported
from package_sdk.install.plan import check_server
from package_sdk.model import (
    API_VERSION,
    DEFAULT_REPLAY_LIMIT,
    NOTIFY_URL_ENV,
    Installation,
    PackageError,
    package_path,
    read_env_file,
)

SERVER_NAME = "package-sdk"
PLAN_HASH = re.compile(r"sha256:[0-9a-f]{64}")
ROOT_ENV = "PACKAGE_SDK_ROOT"
SERVERS_ENV = "PACKAGE_SDK_SERVERS"
# Рабочий каталог сервера в корне сессии: планы и установки одного пакета для них.
WORK_DIR = ".package-sdk"
DEFAULT_PLAN = "plan.json"
# Первая строка установки, которую pkg_plan пишет для каталога одного пакета: такой файл
# сервер перезаписывает, любой другой — нет.
GENERATED = "# Installation of one package for a package-sdk mcp plan (pkg_plan by path).\n"
# Та же строка до перевода вывода на английский (TAI-ADR-0060): установки, записанные
# прежними версиями, сервер тоже узнаёт своими.
GENERATED_BEFORE = "# Установка одного пакета для плана package-sdk mcp (pkg_plan по path).\n"
# Опции package-sdk edit, значение которых — фрагмент YAML или @<файл>.
FRAGMENT_OPTIONS = frozenset({"step", "stage", "row", "on_event", "on-event", "schema"})
PATH_OPTIONS = frozenset({"file", "package"})
_CODE = re.compile(r"^([a-z]+(?:_[a-z]+)+):")
# Адреса и учётные данные, которые сервер берёт только из окружения своего процесса:
# .env пишет автор пакета, а значит и агент, — оттуда они не читаются.
PROCESS_ONLY = re.compile(
    r"^(NOTIFICATION_SERVICE_URL|NOTIFY_TOKEN|CP_TOKEN|CONTROL_PLANE_\w*|IAM_\w*|"
    r"PACKAGE_SDK_\w*|XDG_CONFIG_HOME|HOME)$"
)
INSTRUCTIONS = (
    "Authoring catalog packages: check (pkg_check), test (pkg_test), describe "
    "(pkg_describe), edit files keeping their style (pkg_edit), plan an installation "
    "(pkg_plan) and apply exactly that plan (pkg_apply). Work from tests: write "
    "tests/*.test.yaml first, then the objects, and run pkg_test after each change. "
    "Nothing here writes to a stand except pkg_apply. Stands are only those configured "
    "for this server. Show the whole plan of pkg_plan to the human and call pkg_apply "
    "with its planHash only after the human explicitly said yes to that plan in this "
    "conversation; any change after the plan needs a new plan and a new yes."
)
TOOL_DESCRIPTIONS = {
    "pkg_check": (
        "Check a package directory ('path') or an installation file ('install'): schema, "
        "closed references, declared variables and the core's validators; with 'server' "
        "(one of the stands configured for this server) the core of that stand checks the "
        "named packages as well (checkOnly). Findings have code, file, line, path, message "
        "and hint. 'schema_only' checks without the core's code. Writes nothing."
    ),
    "pkg_test": (
        "Run the test pyramid of a package directory ('path') or an installation "
        "('install'): check, skill contracts, integration tests, scenarios "
        "tests/*.test.yaml executed by the core's code — the in-process sandbox, or the "
        "core of 'server' (a configured stand). 'tests' narrows the scenarios by name or "
        "file. Status passed | failed, stages with failures by scenario step and coverage "
        "with what no test reached. Runs the package's integration code locally; writes "
        "nothing to a stand."
    ),
    "pkg_describe": (
        "What an installation of a package directory needs: variables, the package settings "
        "an administrator changes later (path, type, default, required, x-ref), agent nodes, "
        "ontologies, requires and core compatibility; 'env_example' adds a template of the "
        "variables file. Writes nothing."
    ),
    "pkg_edit": (
        "A style-preserving edit of a package file inside the session root (comments and "
        "layout of other lines stay): operation add-step | add-stage | add-decision-row | "
        "add-rule | add-form-field | rename | set, 'options' — the options of `package-sdk "
        'edit <operation>` by name without dashes, for example {"file": …, "in": '
        '"review", "step": "{id: x, set: {a: \'1\'}}"}; a flag is true. The document '
        "is checked against the schema before it is written. 'dry_run' returns the diff "
        "and writes nothing."
    ),
    "pkg_plan": (
        "Build the one installation plan (package-sdk.plan/v1) of an installation file "
        "('install') or of one package directory ('path') for the stand 'server' (a "
        "configured stand; may be omitted when only one is configured), without a single "
        "write to it, and save it under .package-sdk/ of the session root ('out', default "
        ".package-sdk/plan.json). 'overwrite_console' overwrites the fields a person changed "
        "in the console since the last apply (by default they are kept); the flag is part "
        "of the plan and of its hash. Returns planFile, planHash, overwriteConsole, the "
        "objects with console edits (consoleEdits: fields overwritten and kept), the plan "
        "by section and its lines for the human. Show the whole plan to the human, console "
        "edits the apply overwrites first; pkg_apply applies exactly this file and hash."
    ),
    "pkg_apply": (
        "Apply exactly the saved plan 'plan_file' (an absolute path) — only the plan the "
        "human saw and said yes to: 'plan_hash' is its planHash, required. The plan is "
        "built again before the first write; a stand that moved since gives plan_stale — "
        "then plan again, show it and ask again. Call only after the human's explicit yes "
        "to this plan."
    ),
}


# --- ответы ---------------------------------------------------------------------------


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _error(code: str, message: str, hint: str | None = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"error": code, "message": message}
    if hint:
        out["hint"] = hint
    out.update(extra)
    return out


def _failure(error: Exception) -> dict[str, Any]:
    """Исключение инструмента → отказ одной формы: код из сообщения («plan_stale: …») или вид."""
    if isinstance(error, CoreUnsupported):
        return _error(
            "core_unsupported",
            str(error),
            "Check that the stand's core is new enough for package tests and plans",
        )
    if isinstance(error, urllib.error.URLError):
        return _error("core_unreachable", f"the stand is unreachable: {error.reason}")
    if isinstance(error, PackageError):
        message = str(error)
        match = _CODE.match(message)
        return _error(match.group(1) if match else "package_error", message)
    return _error("error", f"{type(error).__name__}: {error}")


# --- корень сессии и стенды -----------------------------------------------------------


@dataclass(frozen=True)
class Session:
    """Корень сессии: от него разрешаются пути, внутри него — всё, что сервер пишет."""

    root: Path

    def path(self, value: str | Path) -> Path:
        """Путь агента: относительный — от корня сессии."""
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.root / path

    def inside(self, value: str | Path, what: str) -> Path:
        """Путь внутри корня сессии (после разрешения ссылок), иначе отказ."""
        resolved = self.path(value).resolve()
        if not resolved.is_relative_to(self.root):
            raise PackageError(
                f"path_outside_root: {what} {value} — outside the session root {self.root} "
                f"(the root is set by {ROOT_ENV} or the MCP client)"
            )
        return resolved

    @property
    def work(self) -> Path:
        return self.root / WORK_DIR


def session_at(root: str | Path) -> Session:
    return Session(Path(root).expanduser().resolve())


def allowed_servers(environ: Mapping[str, str] | None = None) -> list[str]:
    """Стенды, с которыми серверу разрешено говорить: PACKAGE_SDK_SERVERS окружения
    процесса (не .env пакета — его пишет автор, а значит и агент)."""
    values = (os.environ if environ is None else environ).get(SERVERS_ENV, "")
    return [v.rstrip("/") for v in re.split(r"[\s,]+", values) if v.strip()]


def stand(server: str, environ: Mapping[str, str] | None = None) -> str:
    """Адрес стенда из списка разрешённых; проверка — до того, как появится токен."""
    allowed = allowed_servers(environ)
    if not server:
        if len(allowed) == 1:
            server = allowed[0]
        else:
            raise PackageError(
                "arguments_invalid: server is required — one of the stands "
                + (", ".join(allowed) if allowed else f"(none configured: set {SERVERS_ENV})")
            )
    try:
        server = check_server(server)
    except PackageError as error:
        raise PackageError(f"server_not_allowed: {error}") from error
    if server not in allowed:
        raise PackageError(
            f"server_not_allowed: stand {server} is not configured for this server — allowed: "
            + (", ".join(allowed) or f"none (set {SERVERS_ENV} in the server environment)")
        )
    return server


# --- общее ----------------------------------------------------------------------------


def notify_address(url: str, environ: Mapping[str, str] | None = None) -> str:
    """Адрес сервиса уведомлений: https (http — только localhost) и под одним из
    разрешённых стендов — иначе токен уведомлений туда не уйдёт."""
    try:
        url = check_server(url)
    except PackageError as error:
        raise PackageError(f"server_not_allowed: {NOTIFY_URL_ENV}: {error}") from error
    allowed = allowed_servers(environ)
    if not any(url == s or url.startswith(s + "/") for s in allowed):
        raise PackageError(
            f"server_not_allowed: {NOTIFY_URL_ENV} {url} — not under an allowed stand "
            f"({', '.join(allowed) or f'set {SERVERS_ENV}'})"
        )
    return url


def _env(session: Session, env_file: str) -> dict[str, str]:
    """Переменные установки: файл (по умолчанию .env корня сессии, только внутри корня) под
    окружением процесса. Адреса и учётные переменные (PROCESS_ONLY) — только из окружения
    процесса; адрес уведомлений сверяется со списком разрешённых стендов."""
    from_file = read_env_file(session.inside(env_file or ".env", "env_file"))
    env = {k: v for k, v in from_file.items() if not PROCESS_ONLY.match(k)}
    env.update(os.environ)
    if env.get(NOTIFY_URL_ENV):
        env[NOTIFY_URL_ENV] = notify_address(env[NOTIFY_URL_ENV])
    return env


def _installation(session: Session, path: str, install: str) -> tuple[Installation, list[str]]:
    """Пакет по каталогу или установка по файлу — как ``--package`` / ``--install`` CLI."""
    from package_sdk import commands

    if bool(path) == bool(install):
        raise PackageError("arguments_invalid: name exactly one — path (package) or install")
    namespace = argparse.Namespace(
        install=session.path(install) if install else None,
        package=[str(session.path(path))] if path else None,
    )
    return commands._installation_from(namespace)


# --- инструменты: чтение и правка -----------------------------------------------------


def check(
    session: Session,
    path: str = "",
    install: str = "",
    server: str = "",
    workspace_id: str = "",
    schema_only: bool = False,
    env_file: str = ".env",
) -> dict[str, Any]:
    from package_sdk import commands

    target = stand(server) if server else None
    installation, named = _installation(session, path, install)
    return commands.check_report(
        installation,
        named,
        env=_env(session, env_file),
        server=target,
        workspace=workspace_id or None,
        schema_only=schema_only,
    )


def test(
    session: Session,
    path: str = "",
    install: str = "",
    tests: list[str] | None = None,
    server: str = "",
    workspace_id: str = "",
    env_file: str = ".env",
) -> dict[str, Any]:
    from package_sdk import sandbox, testing

    target = stand(server) if server else None
    installation, named = _installation(session, path, install)
    pyramid = testing.Pyramid(
        installation,
        named,
        _env(session, env_file),
        test=list(tests or []) or None,
        server=target,
        workspace=workspace_id or None,
        database=sandbox.database_url(None),
    )
    if pyramid.wanted() and not any(pyramid.selected(p) for p in pyramid.packages()):
        return _error(
            "test_not_found",
            f"no scenario {', '.join(pyramid.wanted())} in the packages {', '.join(named)}",
            "Name a scenario by its name or file (tests/<name>.test.yaml)",
        )
    return pyramid.execute()


test.__test__ = False  # type: ignore[attr-defined]  # не тест pytest


def describe(session: Session, path: str, env_example: bool = False) -> dict[str, Any]:
    from package_sdk import manifest

    package, installation, problem = manifest.load_for_describe(session.path(path))
    info = manifest.describe(package, installation)
    out: dict[str, Any] = {**info, "problem": problem}
    if env_example:
        out["envExample"] = manifest.env_example(info)
    return out


def _edit_argv(session: Session, operation: str, options: Mapping[str, Any]) -> list[str]:
    """Опции операции → аргументы CLI; файлы и фрагменты @<файл> — только внутри корня
    сессии и передаются абсолютными путями."""
    argv = [operation]
    for name, value in options.items():
        key = str(name).lstrip("-")
        flag = "--" + key.replace("_", "-")
        if value is True:
            argv.append(flag)
            continue
        if value is False or value is None:
            continue
        text = value if isinstance(value, str) else json.dumps(value)
        if key in PATH_OPTIONS:
            text = str(session.inside(text, key))
        elif key in FRAGMENT_OPTIONS and text.startswith("@"):
            text = "@" + str(session.inside(text[1:], f"{key} @"))
        argv += [flag, text]
    return argv


def edit(
    session: Session, operation: str, options: Mapping[str, Any], dry_run: bool = False
) -> dict[str, Any]:
    from package_sdk import edit as edit_module

    parser = edit_module.build_parser()
    parser.exit_on_error = False
    argv = _edit_argv(session, operation, options)
    try:
        args = parser.parse_args(["--dry-run", *argv] if dry_run else argv)
    except (argparse.ArgumentError, SystemExit) as error:
        return _error(
            "arguments_invalid",
            f"package-sdk edit {operation}: {error}",
            "Pass the options of `package-sdk edit <operation> --help` by name without dashes",
        )
    try:
        result, changes = edit_module._run(args)
    except edit_module.PkgError as error:
        return {"ok": False, "error": error.as_dict()}
    diff = "".join(
        line
        for path, before, after in changes
        for line in difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            f"a/{path}",
            f"b/{path}",
        )
    )
    return {
        "ok": True,
        "dryRun": dry_run,
        **result,
        "files": [str(p) for p, before, after in changes if before != after],
        "diff": diff,
    }


# --- инструменты: план и применение ---------------------------------------------------


def plan_output(session: Session, out: str) -> Path:
    """Файл плана: только ``<корень>/.package-sdk/*.json``; существующий файл, который не
    план, не перезаписывается."""
    path = session.path(out) if out else session.work / DEFAULT_PLAN
    work = session.work.resolve()  # каталог-ссылка наружу корня — не рабочий каталог
    resolved = path.resolve()
    inside = work.is_relative_to(session.root) and resolved.is_relative_to(work)
    if not inside or resolved.suffix != ".json":
        raise PackageError(
            f"out_not_allowed: {out} — the plan is written only as a .json file in {session.work}"
        )
    if resolved.exists():
        try:
            existing = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = None
        if not isinstance(existing, dict) or existing.get("format") != install_module.PLAN_FORMAT:
            raise PackageError(f"out_not_a_plan: {resolved} — not a plan, it cannot be overwritten")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def package_installation(package: Path, plan_file: Path) -> Path:
    """Установка одного пакета по пути для плана по ``path``: файл рядом с планом, чтобы
    ``apply`` нашёл её по плану (``install`` плана — относительно его файла). ``requires``
    ищутся рядом с пакетом, как у ``resolve_targets``. Чужой файл с тем же именем не
    перезаписывается."""
    from package_sdk import yaml12

    directory = package_path(package)
    if not (directory / "package.yaml").is_file():
        raise PackageError(f"package_not_found: {package} — no package.yaml")
    target = plan_file.with_name(plan_file.stem + ".install.yaml")
    if target.is_symlink() or not target.resolve().is_relative_to(plan_file.parent):
        raise PackageError(
            f"install_file_exists: {target} — a symlink; writing through it is not allowed"
        )
    if target.exists() and not target.read_text(encoding="utf-8").startswith(
        (GENERATED, GENERATED_BEFORE)
    ):
        raise PackageError(
            f"install_file_exists: {target} — already exists and was not written by "
            "package-sdk mcp; name another out"
        )
    base = target.parent
    document = {
        "apiVersion": API_VERSION,
        "kind": "Installation",
        "key": directory.name,
        "spec": {
            "packagesDir": os.path.relpath(directory.parent, base),
            "packages": [{"key": directory.name, "path": os.path.relpath(directory, base)}],
        },
    }
    target.write_text(GENERATED + yaml12.dump(document, sort_keys=False), encoding="utf-8")
    return target


def plan(
    session: Session,
    server: str = "",
    install: str = "",
    path: str = "",
    out: str = "",
    workspace_id: str = "",
    replay_limit: int = DEFAULT_REPLAY_LIMIT,
    env_file: str = ".env",
    overwrite_console: bool = False,
) -> dict[str, Any]:
    from package_sdk import commands

    if bool(path) == bool(install):
        raise PackageError("arguments_invalid: name exactly one — install or path (package)")
    target_server = stand(server)
    plan_file = plan_output(session, out)
    install_file = (
        session.path(install) if install else package_installation(session.path(path), plan_file)
    )
    env = _env(session, env_file)
    lines: list[str] = []
    document = install_module.plan(
        install_file,
        target=commands._target(target_server, env, install_file),
        env=env,
        workspace=workspace_id or None,
        replay_limit=replay_limit,
        overwrite_console=overwrite_console,
        out=plan_file,
        log=lines.append,
    )
    return {
        "planFile": str(plan_file),
        "planHash": document["planHash"],
        "server": document["server"],
        "overwriteConsole": install_module.overwrites_console(document),
        "consoleEdits": [
            {
                "package": found.package,
                "kind": found.kind,
                "key": found.key,
                "overwritten": list(found.overwritten),
                "kept": list(found.kept),
            }
            for found in install_module.console_edits(document)
        ],
        "changes": install_module.count_changes(document),
        "lines": install_module.format_plan(document),
        "log": [line for line in lines if line.startswith("warning:")],
        "plan": document,
    }


def apply(
    session: Session,
    plan_file: str,
    plan_hash: str,
    env_file: str = ".env",
) -> dict[str, Any]:
    """Применить сохранённый план. Человек подтвердил этот план у хоста (хук плагина, ``ask``)
    по его хэшу. Файл читается один раз: применяется ровно прочитанный документ, чей
    ``planHash`` равен подтверждённому, — подмена файла после проверки ничего не меняет."""
    from package_sdk import commands

    if not isinstance(plan_hash, str) or not PLAN_HASH.fullmatch(plan_hash):
        return _error(
            "plan_hash_required",
            "a plan is applied only by its planHash (sha256:<64 hex>)",
            "Run pkg_plan, show the plan to the human and pass its planHash after their yes",
        )
    path = session.path(plan_file)
    document = install_module.read_plan(path)  # единственное чтение; planHash сверен с телом
    if document["planHash"] != plan_hash:
        return _error(
            "plan_hash_mismatch",
            f"{plan_file} is the plan {document['planHash']}, not {plan_hash}",
            "Apply the plan the human saw: show this plan and ask again, or plan again",
        )
    server = stand(document["server"])
    install_file = install_module.install_file(document, path)
    env = _env(session, env_file)
    lines: list[str] = []

    def confirmed(question: str) -> bool:
        # вопрос SDK называет хэш плана, который он применит: он обязан быть подтверждённым
        return plan_hash in question and PLAN_HASH.findall(question) == [plan_hash]

    applied = install_module.apply(
        document,
        target=commands._target(server, env, install_file),
        env=env,
        install=install_file,
        confirm=confirmed,
        log=lines.append,
    )
    return {"planHash": plan_hash, "server": server, "applied": applied, "log": lines}


# --- сервер ---------------------------------------------------------------------------


async def _client_root(context: Any) -> Path | None:
    """Первый корень ``file://`` клиента MCP, если клиент их объявил."""
    try:
        capabilities = context.session.client_capabilities
        if capabilities is None or getattr(capabilities, "roots", None) is None:
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = await asyncio.wait_for(context.session.list_roots(), timeout=5)
    except Exception:  # клиент без корней или без обратного канала — корень по умолчанию
        return None
    for root in result.roots:
        parsed = urllib.parse.urlparse(str(root.uri))
        if parsed.scheme == "file":
            return Path(urllib.parse.unquote(parsed.path))
    return None


async def session_of(context: Any) -> Session:
    configured = os.environ.get(ROOT_ENV)
    if configured:
        return session_at(configured)
    root = await _client_root(context) if context is not None else None
    return session_at(root or Path.cwd())


async def _call(
    context: Any, function: Callable[..., dict[str, Any]], /, *args: Any, **kwargs: Any
) -> str:
    """Инструмент — в потоке (ядро и песочница синхронны), ответ или отказ — JSON."""
    try:
        session = await session_of(context)
        return _dump(await asyncio.to_thread(function, session, *args, **kwargs))
    except (PackageError, CoreUnsupported, urllib.error.URLError, OSError, RuntimeError) as error:
        return _dump(_failure(error))


def build_server() -> Any:
    """Сервер MCP с инструментами автора (без запуска транспорта)."""
    from mcp.server import MCPServer
    from mcp.server.mcpserver import Context
    from mcp.types import ToolAnnotations

    from package_sdk import __version__

    reading = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
    writing = ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
    # Применение меняет стенд и может выводить объекты из оборота; тот же хэш второй раз —
    # уже устаревший план.
    applying = ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
    )
    author = MCPServer(name=SERVER_NAME, instructions=INSTRUCTIONS, version=__version__)

    @author.tool(description=TOOL_DESCRIPTIONS["pkg_check"], annotations=reading)
    async def pkg_check(
        ctx: Context,
        path: str = "",
        install: str = "",
        server: str = "",
        workspace_id: str = "",
        schema_only: bool = False,
        env_file: str = ".env",
    ) -> str:
        return await _call(
            ctx,
            check,
            path=path,
            install=install,
            server=server,
            workspace_id=workspace_id,
            schema_only=schema_only,
            env_file=env_file,
        )

    @author.tool(description=TOOL_DESCRIPTIONS["pkg_test"], annotations=reading)
    async def pkg_test(
        ctx: Context,
        path: str = "",
        install: str = "",
        tests: list[str] | None = None,
        server: str = "",
        workspace_id: str = "",
        env_file: str = ".env",
    ) -> str:
        return await _call(
            ctx,
            test,
            path=path,
            install=install,
            tests=tests,
            server=server,
            workspace_id=workspace_id,
            env_file=env_file,
        )

    @author.tool(description=TOOL_DESCRIPTIONS["pkg_describe"], annotations=reading)
    async def pkg_describe(ctx: Context, path: str, env_example: bool = False) -> str:
        return await _call(ctx, describe, path, env_example=env_example)

    @author.tool(description=TOOL_DESCRIPTIONS["pkg_edit"], annotations=writing)
    async def pkg_edit(
        ctx: Context, operation: str, options: dict[str, Any], dry_run: bool = False
    ) -> str:
        return await _call(ctx, edit, operation, options, dry_run=dry_run)

    @author.tool(description=TOOL_DESCRIPTIONS["pkg_plan"], annotations=writing)
    async def pkg_plan(
        ctx: Context,
        server: str = "",
        install: str = "",
        path: str = "",
        out: str = "",
        workspace_id: str = "",
        replay_limit: int = DEFAULT_REPLAY_LIMIT,
        env_file: str = ".env",
        overwrite_console: bool = False,
    ) -> str:
        return await _call(
            ctx,
            plan,
            server=server,
            install=install,
            path=path,
            out=out,
            workspace_id=workspace_id,
            replay_limit=replay_limit,
            env_file=env_file,
            overwrite_console=overwrite_console,
        )

    @author.tool(description=TOOL_DESCRIPTIONS["pkg_apply"], annotations=applying)
    async def pkg_apply(
        ctx: Context, plan_file: str, plan_hash: str, env_file: str = ".env"
    ) -> str:
        return await _call(ctx, apply, plan_file, plan_hash, env_file=env_file)

    return author


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="package-sdk mcp",
        description=(
            f"package author MCP server over stdio; stands — {SERVERS_ENV}, session root — "
            f"{ROOT_ENV}, the client roots or the current directory"
        ),
    )
    parser.parse_args(argv)
    try:
        server = build_server()
    except ImportError as error:
        print(f"error: no MCP library ({error}) — install package-sdk[mcp]", file=sys.stderr)
        return 1
    server.run("stdio")
    return 0
