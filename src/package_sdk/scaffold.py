"""Заготовки пакета и объектов (FR-005, FR-006; TAI-ADR-0062).

``package-sdk init <dir>`` создаёт пакет, который сразу проходит ``check`` и свой тест в
песочнице: манифест, процесс-пример с тестом, файл CI, README. ``--integration``
добавляет код интеграции (наблюдатель на ``package_sdk.connector``) и описание агента,
``--image`` — Dockerfile образа.

``package-sdk add <kind> <key>`` добавляет объект любого вида каталога: минимальный
``spec``, который проходит схему и проверки ядра, со ссылкой на схему для редактора и
подсказками из описаний полей схемы.

Шаблоны без доменных слов: пакет-заготовка ничего не знает о предметной области
автора (ст. II, страж нейтральности).
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from package_sdk import schema as schema_module
from package_sdk import yaml12
from package_sdk.model import (
    API_VERSION,
    CATALOG_KINDS,
    FOLDERS,
    PackageError,
    _read_yaml,
    _rel,
    package_name,
)

KEY = re.compile(r"^[a-z][a-z0-9-]{0,62}$")

# Имя определения spec вида в object.schema.json — источник подсказок; у KnowledgePack
# spec — отдельный документ knowledge-pack.schema.json (его делит память).
SPEC_DEFS = {
    "KnowledgePack": "knowledge-pack",
    "WorkspaceType": "workspaceTypeSpec",
    "Capability": "capabilitySpec",
    "Role": "roleSpec",
    "Skill": "skillSpec",
    "ArtifactType": "artifactTypeSpec",
    "TaskType": "taskTypeSpec",
    "Agent": "agentSpec",
    "ProjectTemplate": "projectTemplateSpec",
    "Calendar": "calendarSpec",
    "Process": "processSpec",
    "WorkRule": "workRuleSpec",
    "NotificationRule": "notificationRuleSpec",
}

# Жизненный цикл задачи по умолчанию: четыре статуса, как у типов пакетов платформы.
LIFECYCLE = {
    "statuses": [
        {"key": "todo", "category": "active", "displayName": "To do"},
        {"key": "in_progress", "category": "active", "displayName": "In progress"},
        {"key": "done", "category": "terminal_success", "displayName": "Done"},
        {"key": "cancelled", "category": "terminal_cancelled", "displayName": "Cancelled"},
    ],
    "transitions": [
        {"from": "todo", "to": ["in_progress", "done", "cancelled"]},
        {"from": "in_progress", "to": ["todo", "done", "cancelled"]},
    ],
    "initialStatus": "todo",
    "claimStatus": "in_progress",
    "releaseStatus": "todo",
    "completionStatus": "done",
}


def _spec_definition(kind: str) -> dict[str, Any]:
    if kind == "KnowledgePack":
        return schema_module.load(SPEC_DEFS[kind])
    definition: dict[str, Any] = schema_module.load(schema_module.OBJECT)["$defs"][SPEC_DEFS[kind]]
    return definition


def title(key: str) -> str:
    return key.replace("-", " ").replace(".", " ").capitalize()


def module_name(key: str) -> str:
    return key.replace("-", "_").replace(".", "_")


def minimal_spec(kind: str, key: str) -> dict[str, Any]:
    """Самый короткий spec вида, который принимает схема и ядро."""
    name = title(key)
    if kind == "KnowledgePack":
        # имя онтологии — ключ объекта; версия неизменяема, правка — новая версия
        return {"name": key, "version": 1, "kinds": [{"kind": module_name(key), "title": name}]}
    if kind == "WorkspaceType":
        return {"displayName": name}
    if kind == "Capability":
        return {"description": f"{name}: what an executor with this capability can do"}
    if kind == "Role":
        return {"name": name, "description": "Who in the organization performs this role"}
    if kind == "Skill":
        return {
            "version": "1",
            "description": f"{name}: what the skill does",
            "protocol": "local",
            "sideEffects": "none",
            "riskLevel": "low",
            "inputSchema": {"type": "object", "properties": {}},
            "outputSchema": {"type": "object", "properties": {}},
        }
    if kind == "ArtifactType":
        return {"displayName": name, "metadataSchema": {"type": "object", "properties": {}}}
    if kind == "TaskType":
        return {
            "displayName": name,
            "fieldSchema": {"type": "object", "properties": {}},
            "lifecycleSchema": LIFECYCLE,
            "instructions": "What the executor does and how to hand in the result.",
        }
    if kind == "Agent":
        return {
            "displayName": name,
            "identity": {"kind": "service", "permissions": ["tasks.read"]},
            "placement": "none",
        }
    if kind == "ProjectTemplate":
        return {"displayName": name}
    if kind == "Calendar":
        year = datetime.date.today().year
        return {
            "displayName": name,
            "timezone": "UTC",
            "years": [{"year": year, "holidays": [], "workdays": []}],
        }
    if kind == "Process":
        return {
            "version": 1,
            "displayName": name,
            # от чьего имени действует процесс и кому задачи о нём самом (заготовка add
            # создаёт агента и роль, если их нет)
            "identity": {"agent": f"{key}-process"},
            "owner": [{"role": f"{key}-owner"}],
            "data": {
                "type": "object",
                "properties": {"subject": {"type": "string"}, "done": {"type": "boolean"}},
            },
            "start": {
                "on": {"observation": f"{module_name(key)}.requested"},
                "key": "event.payload.id",
                "set": {"subject": "event.payload.subject"},
            },
            "stages": [
                {
                    "id": "work",
                    "steps": [
                        {"id": "mark-done", "set": {"done": "true"}},
                        {"id": "close", "complete": {"outcome": "done"}},
                    ],
                }
            ],
        }
    if kind == "WorkRule":
        return {
            "description": f"{name}: which work the rule files and when",
            "trigger": {"kind": "observation", "type": f"{module_name(key)}.observed"},
            "action": {
                "kind": "ensure_work",
                "taskType": "task",
                "dedupKeyTemplate": f"{key}:{{{{payload.id}}}}",
                "fields": {"title": "{{payload.title}}"},
            },
        }
    if kind == "NotificationRule":
        return {
            "description": f"{name}: whom the rule notifies and about what",
            "on": {"type": "task.created"},
            "recipient": {"kind": "taskAssignee"},
            "notification": {
                "type": "task.created",
                "title": "{{task.publicId}} {{task.title}}",
                "body": "A new task.",
            },
            "dedupKeyTemplate": "control-plane:event:{{event.id}}",
        }
    raise PackageError(f"вид {kind!r} — не объект каталога; виды: {', '.join(CATALOG_KINDS)}")


def process_test(key: str) -> dict[str, Any]:
    """Тест процесса-заготовки: дело открывается наблюдением и закрывается с исходом."""
    return {
        "process": key,
        "name": "the case closes after the request",
        "steps": [
            {
                "emit": {
                    "observation": f"{module_name(key)}.requested",
                    "payload": {"id": "1", "subject": "x"},
                }
            },
            {"expect": {"status": "completed", "outcome": "done"}},
        ],
    }


def _singular(folder: str) -> str:
    if folder.endswith("ies"):
        return folder[:-3] + "y"  # capabilities → capability
    if folder.endswith("sses"):
        return folder[:-2]  # processes → process
    return folder.removesuffix("s")


def resolve_kind(value: str) -> str:
    """TaskType, task-type или task-types → TaskType."""
    if value in CATALOG_KINDS:
        return value
    folders = {folder: kind for kind, folder in FOLDERS.items()}
    folders.update({_singular(folder): kind for kind, folder in FOLDERS.items()})
    folders["rule"] = "WorkRule"
    lowered = value.lower().replace("_", "-")
    if lowered in folders:
        return folders[lowered]
    raise PackageError(f"вид {value!r} неизвестен; виды: {', '.join(CATALOG_KINDS)}")


# Тег выпуска компонента платформы: v<major>.<minor>.<patch>[суффикс].
RELEASE_TAG = re.compile(r"^v\d+\.\d+\.\d+[0-9A-Za-z.+-]*$")
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
SCP_REMOTE = re.compile(r"^[\w.-]+@([\w.-]+):(?!/)(.+)$")  # user@host:owner/repo.git


@dataclass(frozen=True)
class Source:
    """Откуда поставлен компонент платформы: https-адрес репозитория (без ``.git`` и
    учётных данных), тег выпуска, стоящий ровно на ревизии, и полный коммит."""

    repository: str | None = None
    tag: str | None = None
    commit: str | None = None

    @property
    def ref(self) -> str | None:
        """Ревизия для закрепления: тег выпуска, иначе полный коммит."""
        return self.tag or self.commit


def web_url(remote: str) -> str | None:
    """https-адрес репозитория по адресу клона: без учётных данных, ``.git`` и ``/`` в
    конце; ``ssh://`` и ``user@host:path`` переводятся в https. Локальный путь и http —
    None: такой адрес другим не открыть."""
    from urllib.parse import urlsplit

    scp = SCP_REMOTE.match(remote)
    if scp:
        remote = f"https://{scp.group(1)}/{scp.group(2)}"
    parts = urlsplit(remote)
    if parts.scheme not in ("https", "ssh") or not parts.hostname:
        return None
    host = parts.hostname + (f":{parts.port}" if parts.scheme == "https" and parts.port else "")
    path = parts.path.rstrip("/").removesuffix(".git").rstrip("/")
    return f"https://{host}{path}" if path.strip("/") else None


def _git(directory: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(directory), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None


@cache
def revision(distribution: str) -> Source:
    """Источник, из которого поставлен компонент платформы (:class:`Source`).

    Берётся из метаданных установки (``direct_url.json``): установка из git
    (``git+https://…@v0.2.0``) даёт адрес, коммит и тег, если ставили по тегу; установка из
    каталога-клона (``uv tool install ./package-sdk``, соседние path-зависимости) — адрес
    ``origin`` клона, коммит ``HEAD`` и тег ``v*``, который стоит ровно на нём. Версия из
    метаданных пакета тегом не считается: у компонентов она не обязана совпадать с тегом
    выпуска. Сведений нет — пустой :class:`Source`."""
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import distribution as find
    except ImportError:  # pragma: no cover
        return Source()
    try:
        raw = find(distribution).read_text("direct_url.json")
        info = json.loads(raw) if raw else {}
    except (PackageNotFoundError, ValueError):
        return Source()
    url = str(info.get("url") or "")
    vcs = info.get("vcs_info") or {}
    if vcs.get("vcs") == "git":
        repository = web_url(url)
        tag, commit = vcs.get("requested_revision"), vcs.get("commit_id")
    elif "dir_info" in info and url.startswith("file://"):
        from urllib.parse import unquote, urlsplit

        directory = Path(unquote(urlsplit(url).path))
        origin = _git(directory, "remote", "get-url", "origin")
        repository = web_url(origin) if origin else None
        commit = _git(directory, "rev-parse", "HEAD")
        tag = _git(directory, "describe", "--tags", "--exact-match", "--match", "v*", "HEAD")
    else:
        return Source()
    return Source(
        repository=repository,
        tag=tag if isinstance(tag, str) and RELEASE_TAG.match(tag) else None,
        commit=commit if isinstance(commit, str) and FULL_SHA.match(commit) else None,
    )


def schema_url(name: str = "object.schema.json") -> str | None:
    """Стабильный адрес схемы выпуска SDK: файл ``schema/<версия формата>/<name>`` в
    репозитории, из которого поставлен этот package-sdk, на теге выпуска —
    ``<репозиторий>/raw/<тег>/schema/v1/<name>`` (форма raw-адреса forge по ссылке на
    ревизию). None — SDK не с тега выпуска или адрес репозитория неизвестен."""
    source = revision("package-sdk")
    if source.tag is None or source.repository is None:
        return None
    return f"{source.repository}/raw/{source.tag}/schema/{schema_module.VERSION}/{name}"


def _schema_ref(path: Path, name: str = "object.schema.json") -> str:
    """Ссылка ``$schema`` для редактора в файле пакета.

    SDK поставлен с тега выпуска — адрес схемы этого выпуска в его репозитории
    (:func:`schema_url`): он одинаков у всех, кто клонирует пакет, и не меняется, пока
    пакет не переведут на другой выпуск. Иначе (dev-версия без тега, рабочая копия) —
    запасной вариант: относительный путь от файла к схеме установленного SDK. Он работает
    на машине, где выполнен ``init``, но не у остальных. Адрес ветки не берётся: схема
    dev-сборки может разойтись с любой опубликованной, а в публичном репозитории лежат
    только выпуски."""
    return schema_url(name) or os.path.relpath(schema_module.schema_dir() / name, path.parent)


def _hints(kind: str, spec: dict[str, Any]) -> list[str]:
    """Комментарии-подсказки: необязательные поля вида с описаниями из схемы."""
    definition = _spec_definition(kind)
    lines = []
    for name, prop in (definition.get("properties") or {}).items():
        if name in spec:
            continue
        text = " ".join(str(prop.get("description") or "").split())  # комментарий в одну строку
        if not text and isinstance(prop.get("$ref"), str):
            text = f"see {prop['$ref'].rsplit('/', 1)[-1]} in the schema"
        lines.append(f"#   {name}: {text}".rstrip(": ") if text else f"#   {name}")
    if not lines:
        return []
    return ["# Optional fields of spec (descriptions from the schema):", *lines]


def _dump(value: Any) -> str:
    # Строки, которые ядро, YAML 1.1 или YAML 1.2 прочтут не строкой, — в кавычках (TASK-001253).
    return yaml12.dump(value, allow_unicode=True, sort_keys=False, width=100)


def render_object(kind: str, key: str, path: Path) -> str:
    """YAML объекта: ссылка на схему, описание вида, минимальный spec и подсказки."""
    definition = _spec_definition(kind)
    head = [f"# yaml-language-server: $schema={_schema_ref(path)}"]
    about = " ".join(str(definition.get("description") or "").split())
    if about:
        head.append(f"# {kind}: {about}")
    document = {"apiVersion": API_VERSION, "kind": kind, "key": key}
    body = _dump(document) + _dump({"spec": minimal_spec(kind, key)})
    hints = _hints(kind, minimal_spec(kind, key))
    return "\n".join(head) + "\n" + body + ("\n".join(hints) + "\n" if hints else "")


def _write_all(plan: list[tuple[Path, str]]) -> list[Path]:
    """Записать файлы плана, только если ни одного из них ещё нет: заготовка не
    перезаписывает файлы и не обрывается на полпути."""
    existing = [path for path, _text in plan if path.exists()]
    if existing:
        raise PackageError(
            "уже есть — заготовка не перезаписывает файлы: "
            + ", ".join(_rel(path) for path in existing)
        )
    for path, text in plan:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return [path for path, _text in plan]


def _object_text(kind: str, key: str, path: Path, spec: dict[str, Any]) -> str:
    document = {"apiVersion": API_VERSION, "kind": kind, "key": key}
    return (
        f"# yaml-language-server: $schema={_schema_ref(path)}\n"
        + _dump(document)
        + _dump({"spec": spec})
    )


# У процесса рядом заводятся <ключ>-process и <ключ>-owner: ключ оставляет им место в 63.
PROCESS_KEY_MAX = 63 - len("-process")


def _plan_object(package_dir: Path, kind: str, key: str) -> list[tuple[Path, str]]:
    """Файлы заготовки объекта; у процесса — ещё личность, роль владельца и тест, если их
    нет."""
    kind = resolve_kind(kind)
    pattern = r"^[a-z][a-z0-9_.-]{0,99}$" if kind == "Skill" else KEY.pattern
    if not re.match(pattern, key):
        raise PackageError(f"ключ {key!r}: строчные латинские буквы, цифры и дефис")
    if kind == "Process" and len(key) > PROCESS_KEY_MAX:
        raise PackageError(
            f"ключ процесса {key!r} длиннее {PROCESS_KEY_MAX}: рядом заводятся "
            f"{key}-process и {key}-owner, а ключ объекта — до 63 символов"
        )
    path = package_dir / FOLDERS[kind] / f"{key}.yaml"
    plan = [(path, render_object(kind, key, path))]
    if kind == "Process":
        for companion_kind, companion_key, companion_spec in (
            (
                "Agent",
                f"{key}-process",
                {
                    "displayName": f"{title(key)} process",
                    "identity": {"kind": "service", "permissions": ["tasks.write"]},
                    "placement": "none",
                },
            ),
            ("Role", f"{key}-owner", {"name": f"{title(key)} owner"}),
        ):
            companion = package_dir / FOLDERS[companion_kind] / f"{companion_key}.yaml"
            if not companion.exists():
                plan.append(
                    (
                        companion,
                        _object_text(companion_kind, companion_key, companion, companion_spec),
                    )
                )
        test = package_dir / "tests" / f"{key}.test.yaml"
        if not test.exists():
            header = f"# yaml-language-server: $schema={_schema_ref(test, 'test.schema.json')}\n"
            plan.append((test, header + _dump(process_test(key))))
    return plan


def add(package_dir: Path, kind: str, key: str) -> Scaffolded:
    """Заготовка объекта в пакете: <папка вида>/<key>.yaml, у процесса — и его тест
    tests/<key>.test.yaml (FR-006). Чтобы CI из init оставался зелёным без ручной правки,
    add обновляет сгенерированный раздел README (объект в нём перечислен, а CI проверяет
    раздел ``docs --check``) и, если пакету теперь нужна база песочницы (правило, тип
    задачи или их сценарии), включает в workflow сервис PostgreSQL
    (:func:`enable_database`). Если блок базы в workflow правили руками и база не включена,
    add его не трогает и возвращает предупреждение. Возвращает созданные и изменённые
    файлы."""
    if not (package_dir / "package.yaml").exists():
        raise PackageError(f"{_rel(package_dir)}: нет package.yaml — сначала package-sdk init")
    created = _write_all(_plan_object(package_dir, kind, key))
    updated = [_refresh_docs(package_dir)]
    warnings: list[str] = []
    if needs_database(package_dir):
        enabled = enable_database(package_dir)
        updated.append(enabled)
        if enabled is None and not database_enabled(package_dir):
            warnings.append(
                f"{_rel(package_dir / WORKFLOW_PATH)}: блок базы правили руками, и add его "
                "не тронул — включите базу PostgreSQL в workflow вручную (сервис postgres и "
                "PACKAGE_SDK_SANDBOX_DATABASE_URL): без неё сценарии правил и типов задач не "
                "исполняются, и CI красный"
            )
    return Scaffolded(created, updated=[path for path in updated if path], warnings=warnings)


def _refresh_docs(package_dir: Path) -> Path | None:
    """Обновить сгенерированный раздел README, если он там есть. Возвращает README, если
    раздел изменился; README без раздела не трогается."""
    from package_sdk import manifest

    readme = package_dir / "README.md"
    if not readme.is_file():
        return None
    current = readme.read_text(encoding="utf-8")
    if manifest.DOCS_BEGIN not in current or manifest.DOCS_END not in current:
        return None
    try:
        section = _docs_section(package_dir)
    except PackageError:  # пакет не читается — раздел обновит docs --write после правки
        return None
    updated = manifest.apply_docs(current, section + "\n")
    if updated == current:
        return None
    readme.write_text(updated, encoding="utf-8")
    return readme


def engines() -> dict[str, str]:
    """Совместимость с ядром, код которого стоит рядом (extra sandbox): тот же minor."""
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover
        return {}
    try:
        current = version("control-plane")
    except PackageNotFoundError:
        return {}
    match = re.match(r"^(\d+)\.(\d+)", current)
    if not match:
        return {}
    major, minor = int(match.group(1)), int(match.group(2))
    upper = f"{major}.{minor + 1}" if major == 0 else f"{major + 1}"
    return {"control-plane": f">={major}.{minor},<{upper}"}


# Компоненты платформы соседними каталогами рядом с package-sdk в CI пакета: имя
# репозитория (оно же имя дистрибутива) → переменная его ревизии в workflow. Какие нужны
# дополнениям — path-зависимости из [tool.uv.sources] package-sdk и ядра.
WORKFLOW_COMPONENTS = {
    "package-sdk": "PACKAGE_SDK_REF",
    "control-plane": "CONTROL_PLANE_REF",
    "platform-auth-sdk": "PLATFORM_AUTH_SDK_REF",
    "skill-sdk": "SKILL_SDK_REF",
}
# Каталог компонентов в workspace CI: с точки ключ пакета не начинается, так что каталог
# пакета (его имя — ключ) не совпадёт ни с одним компонентом.
WORKFLOW_PLATFORM_DIR = ".platform"

WORKFLOW = """\
# The package check in CI: the test pyramid without a stand (the schema, references and
# validators of the core, the skill contracts and tests of the integration code, the
# scenarios in the sandbox run by the core code) and the generated README section.
#
# package-sdk runs all of it with the code of the core, so the components of the platform
# are cloned next to it as sibling directories ({platform_dir}/), at revisions of one
# platform release. They are never installed from the public package index: no such names
# there (dependency confusion). The package is checked out into a directory named by its
# key ({key}/): check, test and docs require the directory name to match the key.
name: package
on:
  push:
  pull_request:
permissions:
  contents: read
jobs:
  check:
    runs-on: ubuntu-latest
{services}    env:
      # Where the components are cloned from ($PLATFORM_GIT/<name>.git) and their revisions
      # of one platform release: a tag (v…) or a full commit SHA. Each component is pinned
      # by its own variable and only here: every step below reads these variables. init
      # fills them from the installation it ran in; an empty one stops the job.
{refs}{database}    steps:
      - name: Pinned revisions
        run: |
          if [ -z "$PLATFORM_GIT" ]; then
            echo "::error::PLATFORM_GIT is not set: the address of the component repositories"
            exit 1
          fi
          missing=""
          for name in {names}; do
            if [ -z "${{!name}}" ]; then missing="$missing $name"; fi
          done
          if [ -n "$missing" ]; then
            echo "::error::not set:$missing (a tag or a full commit SHA of one platform release)"
            exit 1
          fi
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262  # v4
        with:
          path: {key}
          persist-credentials: false
      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e  # v6
      - name: package-sdk and the components of the platform as sibling directories
        env:
          GIT_TERMINAL_PROMPT: "0"  # a missing repository fails, not asks for a login
        run: |
          mkdir -p {platform_dir} && cd {platform_dir}
          clone() {{  # clone <name> <ref>: a tag or a full commit SHA
            url="$PLATFORM_GIT/$1.git"
            git clone --quiet --filter=blob:none --no-checkout "$url" "$1" || {{
              echo "::error::cannot clone $url: no such repository or no access (PLATFORM_GIT)"
              exit 1
            }}
            git -C "$1" checkout --quiet --detach "$2" || {{
              echo "::error::$url has no revision $2"
              exit 1
            }}
          }}
{clones}{install_comment}          uv tool install "./package-sdk[{extras}]"{with_pytest}
          echo "$(uv tool dir --bin)" >> "$GITHUB_PATH"
      - name: package-sdk test
        run: package-sdk test .
        working-directory: {key}
      - name: The generated README section is up to date
        run: package-sdk docs . --check
        working-directory: {key}
"""

POSTGRES = """\
services:
  postgres:
    # Pin the image by digest (postgres:16@sha256:…), as the actions below; it has pg_trgm.
    image: postgres:16
    env:
      POSTGRES_PASSWORD: sandbox
      POSTGRES_DB: sandbox
    ports: ["5432:5432"]
    options: >-
      --health-cmd "pg_isready -U postgres" --health-interval 5s --health-retries 20
"""

DATABASE_URL = (
    "PACKAGE_SDK_SANDBOX_DATABASE_URL: postgresql://postgres:sandbox@localhost:5432/sandbox"
)
WORKFLOW_PATH = Path(".github") / "workflows" / "package.yml"


def _indent(text: str, spaces: int, comment: bool = False) -> str:
    prefix = " " * spaces + ("# " if comment else "")
    return "".join(prefix + line + "\n" for line in text.splitlines())


def _services(database: bool) -> str:
    if database:
        return _indent(POSTGRES, 4)
    return (
        "    # Scenarios of rules and task types need an empty PostgreSQL database (with\n"
        "    # pg_trgm): without it they are not run and the job is red. Uncomment the\n"
        "    # service and PACKAGE_SDK_SANDBOX_DATABASE_URL below.\n"
        + _indent(POSTGRES, 4, comment=True)
    )


def needs_database(package_dir: Path) -> bool:
    """Нужна ли пирамиде пакета база PostgreSQL: в нём есть сценарии правил или типов
    задач или сами правила и типы задач — их сценарии исполняются только на базе, а
    непокрытые сценариями пирамида перечисляет как непроверенные."""
    for kind in ("WorkRule", "TaskType"):
        if any((package_dir / FOLDERS[kind]).glob("*.yaml")):
            return True
    for path in (package_dir / "tests").glob("*.test.yaml"):
        try:
            document = _read_yaml(path)  # загрузчиком пакета, как читает ядро
        except (OSError, PackageError):
            continue
        if isinstance(document, dict) and document.get("subject") in ("rule", "taskType"):
            return True
    return False


def workflow(*, key: str, integration: bool, database: bool = False) -> str:
    """Workflow CI пакета (``package.yml`` в каталоге workflow): пирамида
    ``package-sdk test`` и ``package-sdk docs --check``.

    Пакет выкачивается в каталог с именем ``key``: check, test и docs сверяют имя
    каталога с ключом пакета. package-sdk ставится из клона рядом с соседними
    компонентами, которые нужны его дополнениям (``sandbox``: control-plane и
    platform-auth-sdk; код интеграции — ещё ``skills`` и ``connector``: skill-sdk и клиент
    ядра из control-plane, и ``pytest`` для его тестов); компоненты лежат в
    ``.platform/``, чтобы каталог пакета ни с одним не совпал. Ревизия каждого компонента
    задана один раз, переменной окружения job, и все шаги берут её оттуда; значения и
    адрес, откуда клонировать компоненты (``PLATFORM_GIT`` — владелец репозитория SDK), —
    из установки, в которой выполнен init (:func:`revision`). ``database`` — сервис
    PostgreSQL для сценариев правил и типов задач (:func:`needs_database`); без него блок
    сервиса лежит в файле закомментированным."""
    components = [name for name in WORKFLOW_COMPONENTS if integration or name != "skill-sdk"]
    names = [WORKFLOW_COMPONENTS[name] for name in components]
    sdk = revision("package-sdk").repository
    platform = sdk.rsplit("/", 1)[0] if sdk else ""
    refs = [f'PLATFORM_GIT: "{platform}"']
    refs += [f'{WORKFLOW_COMPONENTS[name]}: "{revision(name).ref or ""}"' for name in components]
    clones = "".join(
        f'          clone {name} "${WORKFLOW_COMPONENTS[name]}"\n' for name in components
    )
    return WORKFLOW.format(
        key=key,
        platform_dir=WORKFLOW_PLATFORM_DIR,
        services=_services(database),
        refs=_indent("\n".join(refs), 6),
        database=_indent(DATABASE_URL, 6, comment=not database),
        names=" ".join(names),
        clones=clones,
        install_comment=(
            "          # third-party dependencies of integration/ — one more --with each\n"
            if integration
            else ""
        ),
        extras="sandbox,skills,connector" if integration else "sandbox",
        with_pytest=" --with pytest" if integration else "",
    )


def database_enabled(package_dir: Path) -> bool:
    """Включена ли база в workflow пакета: есть незакомментированная строка
    PACKAGE_SDK_SANDBOX_DATABASE_URL. Workflow нет — пакет проверяется не заготовкой init,
    подсказывать нечего (True)."""
    path = package_dir / WORKFLOW_PATH
    if not path.is_file():
        return True
    name = DATABASE_URL.split(":", 1)[0] + ":"
    return any(
        line.lstrip().startswith(name) for line in path.read_text(encoding="utf-8").splitlines()
    )


def enable_database(package_dir: Path) -> Path | None:
    """Включить в workflow пакета сервис PostgreSQL: раскомментировать блок, который
    положил init. Возвращает файл, если он изменён; None — файла нет, база уже включена
    или блок правили руками (тогда файл не трогается)."""
    path = package_dir / WORKFLOW_PATH
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    commented = _services(False), _indent(DATABASE_URL, 6, comment=True)
    if not all(block in text for block in commented):
        return None
    text = text.replace(commented[0], _services(True)).replace(
        commented[1], _indent(DATABASE_URL, 6)
    )
    path.write_text(text, encoding="utf-8")
    return path


# .package-sdk/ — планы и установки MCP-сервера автора (package-sdk mcp), не для git.
GITIGNORE = "__pycache__/\n*.pyc\n.venv/\n.env\n.package-sdk/\n"


def _readme(key: str, name: str) -> str:
    return f"""# {name}

Catalog package `{key}`.

```bash
package-sdk check --package .     # schema, references, validators of the core
package-sdk test .                # pyramid: check, skills, integration, scenarios
package-sdk add task-type <key>   # a stub object of any kind
```

{{docs}}
"""


def _docs_section(package_dir: Path) -> str:
    """Сгенерированный раздел README (package-sdk docs)."""
    from package_sdk import manifest

    package, installation, _problem = manifest.load_for_describe(package_dir)
    return str(manifest.render_docs(manifest.describe(package, installation), package)).rstrip()


INTEGRATION_PYPROJECT = """\
[project]
name = "{key}-integration"
version = "0.1.0"
description = "Integration code of the package {key}"
requires-python = ">=3.12"
# package-sdk[connector] and the core client come from the observer base image of the
# platform or a pinned source (git+https://…@<tag>), never from the public index: not here.
dependencies = []

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/{module}"]
"""

OBSERVER = '''\
"""Observer of the package {key}: polls the external system and writes observations to the core."""

from package_sdk.connector import Observation, ObserveContext, observer, run


@observer(kind="{key}-observer", entrypoint="{module}.observer:observe")
def observe(ctx: ObserveContext) -> None:
    cursor = ctx.state.get("cursor")
    base_url = ctx.config["baseUrl"]
    token = ctx.secret("{key}-token")  # a secret file of the node, reread every cycle
    for item in fetch(base_url, token, since=cursor):
        ctx.emit(
            Observation(
                kind="{module}.item_changed",
                dedup_key=f"{key}:{{item['id']}}:{{item['version']}}",
                data=item,
                external_ref={{"system": "{key}", "id": item["id"]}},
            )
        )
        cursor = item["cursor"]
    ctx.state["cursor"] = cursor  # saved after a successful publication


def fetch(base_url: str, token: str, *, since: str | None) -> list[dict]:
    """The request to the external system: code of the package author."""
    return []


if __name__ == "__main__":
    run(observe)
'''


def _observer_agent(key: str, module: str) -> dict[str, Any]:
    return {
        "displayName": f"{title(key)} observer",
        "identity": {"kind": "agent", "permissions": ["observations.write"]},
        "executor": {
            "kind": "observer",
            "params": {
                "entrypoint": f"{module}.observer:observe",
                "intervalSeconds": 900,
                "config": {"baseUrl": "https://example.invalid/api"},
            },
        },
        "placement": {"requires": [f"{key}-access"], "secrets": [f"{key}-token"]},
        "state": "stopped",
    }


@dataclass
class Scaffolded:
    """Итог init и add: созданные файлы, пропущенные (README.md и .gitignore уже были),
    изменённые (README, в который дописан раздел docs; workflow CI, в котором add включил
    базу) и предупреждения — что автору сделать руками, чтобы CI остался зелёным."""

    created: list[Path]
    skipped: list[Path] = field(default_factory=list)
    updated: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# Файлы, которые в свежем клоне часто уже есть: init их пропускает, а не падает.
KEPT_IF_PRESENT = ("README.md", ".gitignore")


def init(
    directory: Path,
    *,
    key: str | None = None,
    display_name: str | None = None,
    license: str | None = None,
    integration: bool = False,
    image: bool = False,
    database: bool | None = None,
) -> Scaffolded:
    """Пакет-заготовка в каталоге (пустом, новом или свежем клоне) — FR-005. Сначала
    раскладывается весь план: если хоть один файл уже есть (кроме README.md и
    .gitignore), не пишется ничего. В существующий README дописывается сгенерированный
    раздел (его сверяет ``docs --check`` в CI), остальной текст остаётся. ``database`` —
    сервис PostgreSQL в workflow CI; None — по содержимому каталога (:func:`needs_database`)."""
    key = key or package_name(directory)
    if not KEY.match(key) or len(key) > PROCESS_KEY_MAX:
        raise PackageError(
            f"ключ пакета {key!r}: строчные латинские буквы, цифры и дефис, до "
            f"{PROCESS_KEY_MAX} символов (--key)"
        )
    if image and not integration:
        raise PackageError("--image собирает образ интеграции — нужен и --integration")
    name = display_name or title(key)
    manifest_path = directory / "package.yaml"
    spec: dict[str, Any] = {"version": "0.1.0", "displayName": name}
    if license:
        spec["license"] = license
    compatible = engines()
    if compatible:
        spec["engines"] = compatible
    spec["variables"] = {}
    manifest = {"apiVersion": API_VERSION, "kind": "Package", "key": key, "spec": spec}
    plan: list[tuple[Path, str]] = [
        (
            manifest_path,
            f"# yaml-language-server: $schema={_schema_ref(manifest_path)}\n" + _dump(manifest),
        )
    ]
    plan += _plan_object(directory, "Process", key)
    module = module_name(key)
    if integration:
        agent_path = directory / FOLDERS["Agent"] / f"{key}-observer.yaml"
        plan.append(
            (
                agent_path,
                _object_text("Agent", f"{key}-observer", agent_path, _observer_agent(key, module)),
            )
        )
        root = directory / "integration"
        plan += [
            (root / "pyproject.toml", INTEGRATION_PYPROJECT.format(key=key, module=module)),
            (root / "src" / module / "__init__.py", f'"""Integration of the package {key}."""\n'),
            (root / "src" / module / "observer.py", OBSERVER.format(key=key, module=module)),
        ]
    if image:
        from package_sdk import image as image_module

        plan += [
            (
                directory / "Dockerfile",
                image_module.render_observer(
                    key, "integration/", entrypoint=f"{module}.observer:observe"
                ),
            ),
            (directory / ".dockerignore", image_module.render_dockerignore("integration/")),
        ]
    if database is None:
        database = needs_database(directory)
    plan.append(
        (
            directory / WORKFLOW_PATH,
            workflow(key=key, integration=integration, database=database),
        )
    )
    plan.append((directory / ".gitignore", GITIGNORE))
    skipped = [path for path, _text in plan if path.name in KEPT_IF_PRESENT and path.exists()]
    plan = [(path, text) for path, text in plan if path not in skipped]
    created = _write_all([(p, t) for p, t in plan])
    readme = directory / "README.md"
    updated: list[Path] = []
    if readme.exists():
        from package_sdk import manifest as manifest_module

        # README клона (шаблон хостинга) остаётся, а сгенерированный раздел дописывается в
        # конец: CI проверяет его docs --check, и без него job красный с первого push
        current = readme.read_text(encoding="utf-8")
        text = manifest_module.apply_docs(current, _docs_section(directory) + "\n")
        if text != current:
            readme.write_text(text, encoding="utf-8")
            updated.append(readme)
        else:
            skipped.append(readme)
    else:
        readme.write_text(_readme(key, name).replace("{docs}", _docs_section(directory)), "utf-8")
        created.append(readme)
    return Scaffolded(created, skipped, updated)
