"""Модель пакета каталога (TAI-ADR-0044): объекты, пакеты, установка, загрузка YAML 1.2,
порядок видов, подстановка ${ПЕРЕМЕННЫХ}."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - окружение без PyYAML
    yaml = None  # type: ignore[assignment]


# Проект, в котором работает инструмент: относительно него пути в сообщениях и
# каталог пакетов по умолчанию (packages/).
ROOT = Path.cwd()


PACKAGES_DIR = ROOT / "packages"


API_VERSION = "taimen.ai/v1"


API = "/api/v1"


# Порядок применения: на что ссылаются, то раньше (execution и invokeSkill → Skill,
# ensureWork → TaskType, allowedChildTypes → WorkspaceType; WorkRule → Skill и TaskType;
# artifactSchema → ArtifactType; Agent → Role, Skill, TaskType; WorkRule.identity → Agent).
# NotificationRule — последним: правила уведомлений ни на что в ядре не ссылаются, но живут
# в другом сервисе и начинают исполняться сразу — пусть ядро к этому моменту уже приведено.
# Calendar и Process (TAI-ADR-0054): календарь раньше процесса (cal.* и spec.calendar), процесс —
# после TaskType и Agent (шаги human/approve ссылаются на типы задач, identity — на агента).
# ConnectionType (CP-ADR-0079 §2) — сразу после Capability: ни на что не ссылается, а агенты
# называют подключения его типа (Agent.spec.connections).
CATALOG_KINDS = (
    # Онтологии памяти (TAI-ADR-0062 п.5): регистрируются первыми — на их виды опираются
    # процессы, а включение для workspace (Installation.spec.knowledge) идёт после объектов.
    "KnowledgePack",
    "WorkspaceType",
    "Capability",
    "ConnectionType",
    "Role",
    "Skill",
    "ArtifactType",
    "TaskType",
    "Agent",
    "ProjectTemplate",
    "Calendar",
    "Process",
    "WorkRule",
    "NotificationRule",
)


# Виды, которые применяет только ядро по плану (POST /packages:plan → apply --plan): их
# определения, версии и судьбу открытых экземпляров знает ядро, а не установщик.
PLAN_KINDS = ("Calendar", "Process")


# Экраны пакета описанием (TAI-ADR-0066, CP-ADR-0080): вид View ставит и выводит план ядра,
# Component ядро встраивает в каждый вид, который его называет. Ни тот ни другой — не объект
# каталога установщика: пакет с ними уходит в план ядра целиком, вместе со словарями.
SCREEN_KINDS = ("View", "Component")


# Поле идентичности объекта в API.
IDENTITY = {
    "KnowledgePack": "name",
    "Agent": "key",
    "ArtifactType": "key",
    "TaskType": "key",
    "ProjectTemplate": "key",
    "WorkspaceType": "key",
    "Role": "slug",
    "Capability": "name",
    "Skill": "name",
    "WorkRule": "key",
    "NotificationRule": "key",
    "Process": "key",
    "Calendar": "key",
    "ConnectionType": "key",
}


FOLDERS = {
    "KnowledgePack": "knowledge-packs",
    "Agent": "agents",
    "ArtifactType": "artifact-types",
    "TaskType": "task-types",
    "ProjectTemplate": "project-templates",
    "WorkspaceType": "workspace-types",
    "Role": "roles",
    "Capability": "capabilities",
    "Skill": "skills",
    "WorkRule": "rules",
    "NotificationRule": "notification-rules",
    "Process": "processes",
    "Calendar": "calendars",
    "ConnectionType": "connection-types",
}


SYSTEM_TASK_TYPE = "task"  # ядро держит одну его активную версию всегда


DEFAULT_EXECUTION_INPUTS = "$.customFields"


ENV_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


# CP-ADR-0073 А1: поле назначения — UUID principal'а или ссылка на агента реестра по ключу
AGENT_REF = re.compile(r"agent:([a-z0-9][a-z0-9-]{0,62})")
# CP-ADR-0061, амендмент 2026-10-01: адресат гейта типа задачи и правила — роль пакета по slug
ROLE_REF_PREFIX = "role:"
ROLE_REF = re.compile(r"role:([a-z0-9][a-z0-9-]{0,62})")
# Обе ссылки сверяются через fullmatch, как role_reference_slug ядра: `$` у match пропустил
# бы завершающий перевод строки (``role:x\n``), а ядро такую ссылку отвергнет.


# Сервис уведомлений (ADR-0005 notification-service §7): адрес из установки, свой audience
NOTIFY_URL_ENV = "NOTIFICATION_SERVICE_URL"


NOTIFY_TOKEN_ENV = "NOTIFY_TOKEN"


NOTIFY_AUDIENCE = "notification-service"


NOTIFY_SCOPES = ("notifications:admin",)


# Корни условия on.when правила уведомления (ADR-0005 §4)
NOTIFY_CONDITION_ROOTS = frozenset({"payload", "event", "task"})


# Поля spec и их значения по умолчанию в API. SERVER_DEFAULTED — поле, которое
# сервер заполняет сам (lifecycle, конфигурация шаблона): сравнивается, только
# если задано в файле.
SERVER_DEFAULTED = object()


SPEC_FIELDS: dict[str, dict[str, Any]] = {
    "TaskType": {
        "displayName": "",
        "description": "",
        "fieldSchema": {},
        "lifecycleSchema": SERVER_DEFAULTED,
        "execution": None,
        "approvalSchema": {},
        # CP-ADR-0064: сравнивается, только если задан в файле — ядро старше
        # профиля контекста поле не знает, и пустое значение не повод для версии
        "contextSchema": SERVER_DEFAULTED,
        # CP-ADR-0066: инструкции исполнителю — тоже только если заданы; ядро
        # старше поля его не отдаёт
        "instructions": SERVER_DEFAULTED,
        # CP-ADR-0061, амендмент 2026-09-25: работа после завершения задачи
        "completionSchema": SERVER_DEFAULTED,
        # CP-ADR-0072: входы и выходы — только если заданы в файле
        "artifactSchema": SERVER_DEFAULTED,
        # CP-ADR-0067 В5 (TAI-ADR-0053): критерии приёмки по умолчанию у типа
        "acceptance": SERVER_DEFAULTED,
    },
    # CP-ADR-0072 §6: версии неизменяемы, как у TaskType; maxBytes без значения в файле —
    # глобальный лимит установки на момент публикации, поэтому сравнивается, только если задан.
    "ArtifactType": {
        "displayName": "",
        "description": "",
        "metadataSchema": {},
        "mediaTypes": ["*/*"],
        "maxBytes": SERVER_DEFAULTED,
    },
    "ProjectTemplate": {
        "displayName": "",
        "description": "",
        "fieldSchema": {},
        "lifecycleSchema": SERVER_DEFAULTED,
        "defaultConfig": SERVER_DEFAULTED,
        "defaultViews": [],
        "governanceSchema": {},
        "memoryDefaults": {},
    },
    "WorkspaceType": {
        "displayName": "",
        "description": "",
        "fieldSchema": {},
        "allowedChildTypes": [],
    },
    "Role": {"name": "", "description": ""},
    "Capability": {"description": ""},
}


SKILL_IMMUTABLE = ("protocol", "sideEffects", "riskLevel", "contract")


SKILL_MUTABLE = ("description", "config", "inputSchema", "outputSchema")


# WorkRule (CP-ADR-0063 §11): изменяемый вид, PATCH с If-Match; key и workspaceId неизменны.
# identity (амендмент 2026-09-27, Г1) — тоже PATCH; null снимает личность.
RULE_MUTABLE = ("description", "trigger", "condition", "interpretation", "action", "identity")


RETIRABLE = (
    "TaskType",
    "ProjectTemplate",
    "WorkRule",
    "Agent",
    "NotificationRule",
    "Process",
    "Calendar",
    "ConnectionType",
)


# ConnectionType (CP-ADR-0079 §2): версии задаёт пакет, пара (key, version) неизменяема; у
# версии меняется только статус — PATCH /connection-types/{key}@{version} с If-Match.
CONNECTION_TYPE_ETAG = '"connection-type-{}"'
# Поля spec, которые ядро хранит в версии типа подключения (всё, кроме version).
CONNECTION_TYPE_FIELDS = (
    "displayName",
    "description",
    "auth",
    "oauth2",
    "accountField",
    "settingsSchema",
    "defaultKey",
)
# Версионные виды: объект называется key@version (версию задаёт пакет).
VERSIONED_REF_KINDS = ("Skill", "ConnectionType")


# Каталоги пакета, в которых лежат не объекты каталога: тесты процессов, JSON Schema данных
# (на них ссылается data: {$ref}) и раскладка схемы для визуального редактора.
TESTS_DIR, SCHEMAS_DIR, LAYOUT_DIR = "tests", "schemas", ".layout"
# Словари пакета i18n/<locale>.yaml (TAI-ADR-0066 п.1а): плоское ключ → текст, не объекты.
I18N_DIR = "i18n"

# Файлы, которые не входят в хэш установки: служебные файлы ОС и кэши интерпретатора;
# раскладка визуального редактора (.layout/ в корне пакета) логики не несёт.
_HASH_IGNORED_NAMES = frozenset({".DS_Store", "Thumbs.db"})
_HASH_IGNORED_DIRS = frozenset({"__pycache__", ".git"})


class PackageError(Exception):
    """Ошибка формата или установки пакета — с понятным человеку текстом."""


@dataclass
class Obj:
    kind: str
    key: str
    spec: dict[str, Any]
    package: str
    path: Path

    @property
    def ref(self) -> str:
        if self.kind in VERSIONED_REF_KINDS:
            return f"{self.kind}/{self.key}@{self.spec.get('version')}"
        return f"{self.kind}/{self.key}"


@dataclass
class PackageTest:
    """Тест процесса пакета (tests/<имя>.test.yaml, schema/v1/test.schema.json)."""

    package: str
    path: Path
    data: Any

    @property
    def name(self) -> str:
        return str(self.data.get("name")) if isinstance(self.data, dict) else self.path.name


@dataclass
class Package:
    key: str
    spec: dict[str, Any]
    path: Path
    objects: list[Obj] = field(default_factory=list)
    tests: list[PackageTest] = field(default_factory=list)
    # Источник git установки {git, ref, path?} — у пакета, взятого по lock (TAI-ADR-0062 п.6);
    # None — пакет из каталога установки или по пути
    origin: dict[str, Any] | None = None

    @property
    def renames(self) -> list[dict[str, Any]]:
        return list(self.spec.get("renames") or [])

    @property
    def requires(self) -> list[str]:
        """Ключи пакетов requires (короткая форма и {package, version})."""
        return [key for key, _range in self.requirements]

    @property
    def requirements(self) -> list[tuple[str, str | None]]:
        """(ключ, диапазон SemVer или None — любая версия) каждого требования манифеста."""
        result: list[tuple[str, str | None]] = []
        for entry in self.spec.get("requires") or []:
            if isinstance(entry, dict):
                version = entry.get("version")
                result.append((str(entry.get("package")), str(version) if version else None))
            else:
                result.append((str(entry), None))
        return result


@dataclass
class Installation:
    packages: list[Package]  # в порядке зависимостей
    retire: dict[str, list[str]]
    path: Path | None = None
    # Включение онтологий для пространств работы: [{workspace, packs}] — набор заменяет
    # прежний целиком (PUT /workspaces/{id}/knowledge-packs, TAI-ADR-0062 п.5)
    knowledge: list[dict[str, Any]] = field(default_factory=list)
    # Ключ установки (Installation.key) — для lock и плана; None у набора пакетов без файла
    key: str | None = None

    @property
    def objects(self) -> list[Obj]:
        return [obj for package in self.packages for obj in package.objects]

    def required(self, key: str) -> list[Package]:
        """Пакет `key` и его `requires` (транзитивно) в порядке установки — то, что видит
        пакет на стенде после своей установки (TAI-ADR-0044 п.3). Пакеты той же установки,
        от которых он не зависит, сюда не входят: ни ссылки check, ни каталог песочницы
        на них не опираются."""
        by_key = {package.key: package for package in self.packages}
        needed: set[str] = set()
        pending = [key]
        while pending:
            current = pending.pop()
            if current in needed or current not in by_key:
                continue
            needed.add(current)
            pending.extend(by_key[current].requires)
        return [package for package in self.packages if package.key in needed]

    def visible(self, key: str) -> list[Obj]:
        """Объекты пакета `key` и его `requires` — см. required."""
        return [obj for package in self.required(key) for obj in package.objects]

    @property
    def tests(self) -> list[PackageTest]:
        return [test for package in self.packages for test in package.tests]


# Сколько узлов алиасы YAML могут добавить документу сверх написанных (TASK-001231).
# Алиас разделяет узел, а не копирует его, но каждый обход документа — схема, подстановка
# переменных, JSON для ядра — проходит его столько раз, сколько на него ссылок: девять
# уровней по десять алиасов — миллиард узлов из килобайта текста («billion laughs»).
# Пакеты алиасы пишут (skill-sdk export выносит повторы в &id001), поэтому они не
# запрещены, а ограничены — с большим запасом над живыми пакетами.
YAML_ALIAS_NODES_MAX = 100_000


def _yaml_children(node: Any) -> Iterator[Any]:
    """Дочерние узлы узла PyYAML или ruamel: у отображения — ключи и значения."""
    value = node.value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, tuple):
                yield from item
            else:
                yield item


def alias_expansion_problem(root: Any, limit: int = YAML_ALIAS_NODES_MAX) -> str | None:
    """Почему дерево узлов YAML нельзя читать: рекурсивный алиас или раскрытие алиасов
    больше `limit` узлов сверх написанных; None — можно. Линейно по числу узлов."""
    sizes: dict[int, int] = {}
    on_path: set[int] = set()
    stack: list[tuple[Any, bool]] = [(root, False)]
    while stack:
        node, done = stack.pop()
        ident = id(node)
        if done:
            on_path.discard(ident)
            sizes[ident] = 1 + sum(sizes[id(child)] for child in _yaml_children(node))
            continue
        if ident in sizes:
            continue
        on_path.add(ident)
        stack.append((node, True))
        for child in _yaml_children(node):
            if id(child) in on_path:
                return "recursive alias: the node contains itself"
            if id(child) not in sizes:
                stack.append((child, False))
    extra = sizes[id(root)] - len(sizes)
    if extra > limit:
        return f"aliases expand into {extra} more nodes — over the limit {limit}"
    return None


@cache
def _yaml12_loader() -> Any:
    """SafeLoader, который читает файл пакета так же, как загрузчик ядра (`yaml12`).

    PyYAML следует YAML 1.1: `on`, `off`, `yes`, `no` для него bool (ключ `on` правил
    уведомлений и процессов, TAI-ADR-0054, превращался бы в True), `012` — восьмеричное
    десять, `1_000` — тысяча, `1:30` — девяносто, `2026-09-30` — дата. Ядро читает YAML 1.2:
    bool только true/false, целые — десятичные, `0o` и `0x` (`012` — двенадцать, `1_000`
    и `1:30` — строки), даты — строки, `.inf`/`.nan` и слишком длинные целые отвергает
    (TASK-001247). Правила и их список — в `package_sdk.yaml12` (TASK-001251).

    Раскрытие алиасов ограничено (`alias_expansion_problem`), значения, которых ядро не
    прочтёт, отвергаются (`yaml12.value_problem`) — до построения объектов."""
    from package_sdk import yaml12

    class Loader(yaml.SafeLoader):
        def get_single_node(self) -> Any:
            node = super().get_single_node()
            if node is not None:
                problem = alias_expansion_problem(node) or yaml12.value_problem(node)
                if problem is not None:
                    raise yaml.YAMLError(problem)
            return node

        def _scalar(self, node: Any, parse: Callable[[str], Any]) -> Any:
            try:
                return parse(self.construct_scalar(node))
            except ValueError as error:
                raise yaml.constructor.ConstructorError(
                    None, None, str(error), node.start_mark
                ) from None

        def construct_yaml_bool(self, node: Any) -> bool:
            return bool(self._scalar(node, yaml12.parse_bool))

        def construct_yaml_int(self, node: Any) -> int:
            return int(self._scalar(node, yaml12.parse_int))

        def construct_yaml_float(self, node: Any) -> float:
            return float(self._scalar(node, yaml12.parse_float))

    Loader.add_constructor(yaml12.BOOL_TAG, Loader.construct_yaml_bool)
    Loader.add_constructor(yaml12.INT_TAG, Loader.construct_yaml_int)
    Loader.add_constructor(yaml12.FLOAT_TAG, Loader.construct_yaml_float)
    Loader.yaml_implicit_resolvers = yaml12.implicit_resolvers()
    return Loader


def _read_yaml(path: Path) -> Any:
    if yaml is None:
        raise PackageError(
            "PyYAML is required: pip install pyyaml (on Ubuntu it is already installed — python3-yaml)"
        )
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=_yaml12_loader())
    except yaml.YAMLError as error:
        raise PackageError(f"{_rel(path)}: not YAML: {error}") from error


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _envelope(doc: Any, path: Path) -> tuple[str, str, dict[str, Any]]:
    if not isinstance(doc, dict) or doc.get("apiVersion") != API_VERSION:
        raise PackageError(
            f"{_rel(path)}: the apiVersion: {API_VERSION}, kind, key, spec wrapper is required"
        )
    kind, key, spec = doc.get("kind"), doc.get("key"), doc.get("spec")
    if not isinstance(kind, str) or not isinstance(key, str) or not isinstance(spec, dict):
        raise PackageError(f"{_rel(path)}: kind and key are strings, spec is an object")
    return kind, key, spec


def package_path(value: str | Path) -> Path:
    """Каталог пакета, как его назвали в команде, — абсолютным путём без «.», «..» и ссылок.
    У «.», «./» и «..» своего имени нет (Path(".").name == ""), поэтому имя каталога
    сверяется только после resolve(). Символическая ссылка на каталог пакета разрешается
    в цель: сверяется имя цели, то есть настоящего каталога с package.yaml, — так же, как
    ключ пакета по пути берёт resolve_targets, и так же, как считают хэш содержимого и
    границы data-ссылок (TASK-001164)."""
    return Path(value).expanduser().resolve()


def package_name(directory: str | Path) -> str:
    """Имя каталога пакета — ключ пакета каталога установки (см. package_path)."""
    return package_path(directory).name


def load_package(directory: Path, expected_key: str | None = None) -> Package:
    """Пакет каталога. Ключ пакета каталога установки равен имени его каталога; у пакета по
    пути и из git (expected_key) ключ берётся из манифеста и сверяется с установкой
    (TAI-ADR-0062 п.6): каталог чужого репозитория называется как угодно."""
    manifest_path = directory / "package.yaml"
    if not manifest_path.exists():
        raise PackageError(f"{_rel(directory)}: no package.yaml")
    kind, key, spec = _envelope(_read_yaml(manifest_path), manifest_path)
    if kind != "Package":
        raise PackageError(f"{_rel(manifest_path)}: kind must be Package")
    if expected_key is not None:
        if key != expected_key:
            raise PackageError(
                f"{_rel(manifest_path)}: key {key!r} does not match the package key in the installation "
                f"{expected_key!r}"
            )
    elif key != (name := package_name(directory)):
        raise PackageError(
            f"{_rel(manifest_path)}: key {key!r} does not match the directory name {name!r}"
        )
    package = Package(key=key, spec=spec, path=directory)
    for path in sorted(directory.rglob("*.yaml")):
        if path == manifest_path:
            continue
        inner = path.relative_to(directory).parts
        if inner[0] in (SCHEMAS_DIR, LAYOUT_DIR, I18N_DIR):
            continue
        if inner[0] == TESTS_DIR:
            if path.name.endswith(".test.yaml"):
                package.tests.append(PackageTest(key, path, _read_yaml(path)))
            continue
        obj_kind, obj_key, obj_spec = _envelope(_read_yaml(path), path)
        if obj_kind not in CATALOG_KINDS and obj_kind not in SCREEN_KINDS:
            raise PackageError(
                f"{_rel(path)}: unknown kind {obj_kind!r}; expected one of "
                f"{list(CATALOG_KINDS) + list(SCREEN_KINDS)}"
            )
        if obj_kind == "Process":
            obj_spec = expand_data_ref(obj_spec, path, directory)
        package.objects.append(Obj(obj_kind, obj_key, obj_spec, key, path))
    return package


def expand_data_ref(spec: dict[str, Any], path: Path, package_dir: Path) -> dict[str, Any]:
    """data: {$ref: <файл пакета>} → схема данных из файла (JSON или YAML) внутри пакета.

    Ядру уходят файлы пакета как есть — ссылку оно раскрывает само; раскрытая схема нужна
    статической проверке здесь."""
    data = spec.get("data")
    if not (isinstance(data, dict) and set(data) == {"$ref"} and isinstance(data["$ref"], str)):
        return spec
    ref = data["$ref"]
    if ref.startswith("#") or "://" in ref:
        return spec  # ссылка внутрь документа или удалённая — решает ядро (удалённые оно отвергает)
    target = (path.parent / ref).resolve()
    if not target.is_relative_to(package_dir.resolve()):
        raise PackageError(f"{_rel(path)}: data.$ref {ref!r} points outside the package")
    if not target.is_file():
        raise PackageError(f"{_rel(path)}: data.$ref {ref!r} — no such file in the package")
    try:
        schema = (
            json.loads(target.read_text(encoding="utf-8"))
            if target.suffix == ".json"
            else _read_yaml(target)
        )
    except json.JSONDecodeError as error:
        raise PackageError(f"{_rel(target)}: not JSON: {error}") from error
    if not isinstance(schema, dict):
        raise PackageError(f"{_rel(target)}: the process data schema is a JSON Schema object")
    return {**spec, "data": schema}


def all_package_dirs() -> list[Path]:
    return sorted(p.parent for p in PACKAGES_DIR.glob("*/package.yaml"))


def package_dirs(path: Path | None = None, packages_dir: Path | None = None) -> list[Path]:
    """Где искать пакет по ключу, по порядку: packagesDir установки, packages/ рядом с файлом
    установки, сам его каталог (тестовые пакеты вне packages/), packages/ проекта
    (TAI-ADR-0062 п.6): пакет установки ближе к ней, чем общий каталог проекта."""
    found: list[Path] = []
    for candidate in (
        packages_dir,
        path.parent / "packages" if path else None,
        path.parent if path else None,
        PACKAGES_DIR,
    ):
        if candidate is not None and candidate not in found:
            found.append(candidate)
    return found


# Источник git пакета установки → каталог его зафиксированного содержимого (package-sdk lock).
GitSource = Callable[[dict[str, Any]], Path]


def resolve(
    keys: list[Any],
    retire: dict[str, list[str]] | None = None,
    path: Path | None = None,
    packages_dir: Path | None = None,
    git: GitSource | None = None,
) -> Installation:
    """Пакеты по ключам вместе с requires, в порядке зависимостей. Ключ — пакет каталога
    установки; {key, path} — пакет по пути относительно файла установки; {key, git, ref} —
    пакет из git, содержимое которого по lock даёт git (без него — отказ lock_required)."""
    loaded: dict[str, Package] = {}
    explicit: dict[str, Path] = {}
    origins: dict[str, dict[str, Any]] = {}
    names: list[str] = []
    for entry in keys:
        if isinstance(entry, dict):
            key = str(entry.get("key"))
            if "git" in entry:
                if git is None:
                    raise PackageError(
                        f"lock_required: package {key!r} from git is installed by lock — "
                        "pin the sources: package-sdk lock --install <installation file>"
                    )
                explicit[key] = git(entry)
                origins[key] = {
                    name: entry[name] for name in ("git", "ref", "path") if name in entry
                }
            else:
                base = path.parent if path else ROOT
                explicit[key] = (base / str(entry.get("path"))).resolve()
            names.append(key)
        else:
            names.append(entry)
    search = package_dirs(path, packages_dir)
    order: list[Package] = []
    visiting: set[str] = set()

    def visit(key: str, chain: tuple[str, ...]) -> None:
        if key in loaded:
            return
        if key in visiting:
            raise PackageError(f"requires cycle: {' → '.join(chain + (key,))}")
        directory = explicit.get(key) or next(
            (d / key for d in search if (d / key / "package.yaml").exists()), search[0] / key
        )
        if not (directory / "package.yaml").exists():
            where = ", ".join(_rel(d) for d in search)
            raise PackageError(
                f"package {key!r} not found in {where}"
                + (f" (required by {chain[-1]})" if chain else "")
            )
        visiting.add(key)
        package = load_package(directory, key if key in explicit else None)
        package.origin = origins.get(key)
        for required in package.requires:
            visit(required, chain + (key,))
        visiting.discard(key)
        loaded[key] = package
        order.append(package)

    for key in names:
        visit(key, ())
    return Installation(packages=order, retire=dict(retire or {}), path=path)


def resolve_targets(values: list[str]) -> Installation:
    """Пакеты по ключам или путям: каталог с package.yaml — пакет по пути (репозиторий
    автора), его requires ищутся рядом с ним; иначе — ключ в каталоге пакетов проекта."""
    entries: list[Any] = []
    packages_dir: Path | None = None
    for value in values:
        path = Path(value)
        if (path / "package.yaml").is_file():
            directory = package_path(path)
            entries.append({"key": directory.name, "path": str(directory)})
            packages_dir = packages_dir or directory.parent
        else:
            entries.append(path.name)
    return resolve(entries, packages_dir=packages_dir)


def load_installation(path: Path, git: GitSource | None = None) -> Installation:
    """Установка из файла; git — содержимое источников git по lock (package_sdk.install)."""
    kind, _key, spec = _envelope(_read_yaml(path), path)
    if kind != "Installation":
        raise PackageError(f"{_rel(path)}: kind must be Installation")
    packages = spec.get("packages")
    # Пустой список — законная установка: ядро без доменных пакетов знает только
    # системный тип task (пакета core нет, амендмент TAI-ADR-0044 2026-09-25).
    if not isinstance(packages, list):
        raise PackageError(f"{_rel(path)}: spec.packages is a list of package keys")
    packages_dir = (
        (path.parent / spec["packagesDir"]).resolve() if spec.get("packagesDir") else None
    )
    installation = resolve(packages, spec.get("retire") or {}, path, packages_dir, git)
    installation.key = str(_key)
    installation.knowledge = _knowledge_section(spec.get("knowledge"), path)
    return installation


PACK_REF = re.compile(r"^(tenant:)?[a-z0-9][a-z0-9._-]{0,63}@[0-9]+$")


def _knowledge_section(value: Any, path: Path) -> list[dict[str, Any]]:
    """spec.knowledge установки: [{workspace, packs, strict?}]; ошибка формы — ясный
    отказ до плана, а не падение на разборе ссылки."""
    if value is None:
        return []
    where = f"{_rel(path)}: spec.knowledge"
    if not isinstance(value, list):
        raise PackageError(f"{where} is a list of {{workspace, packs}}")
    entries: list[dict[str, Any]] = []
    for index, entry in enumerate(value):
        at = f"{where}[{index}]"
        if not isinstance(entry, dict):
            raise PackageError(f"{at} — {{workspace, packs}}")
        unknown = sorted(set(entry) - {"workspace", "packs", "strict"})
        if unknown:
            raise PackageError(f"{at}: unknown fields {', '.join(unknown)}")
        workspace = entry.get("workspace")
        if not isinstance(workspace, str) or not workspace.strip():
            raise PackageError(f"{at}.workspace is a UUID or an installation ${{VARIABLE}}")
        packs = entry.get("packs")
        if not isinstance(packs, list):
            raise PackageError(f"{at}.packs is a list of name@version")
        bad = [str(p) for p in packs if not isinstance(p, str) or not PACK_REF.match(p)]
        if bad:
            raise PackageError(
                f"{at}.packs: {', '.join(bad)} — name@version (integer) is required, "
                "for a tenant ontology tenant:name@version"
            )
        strict = entry.get("strict", False)
        if not isinstance(strict, bool):
            raise PackageError(f"{at}.strict is true or false")
        entries.append({"workspace": workspace, "packs": list(packs), "strict": strict})
    return entries


def substitute(
    value: Any, env: dict[str, str], *, missing: Callable[[str], str] | None = None
) -> Any:
    """${NAME} в строках spec → значение окружения инсталляции."""
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name in env:
                return env[name]
            if missing is not None:
                return missing(name)
            raise PackageError(f"environment variable {name} is not set (required by a package)")

        return ENV_REF.sub(replace, value)
    if isinstance(value, dict):
        return {k: substitute(v, env, missing=missing) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, env, missing=missing) for v in value]
    return value


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


DEFAULT_REPLAY_LIMIT = 50


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


# --- связь объектов с пакетом (CP-ADR-0074 §11, амендмент TASK-000904) ----------


def _hashed_files(directory: Path) -> list[tuple[str, Path]]:
    files = []
    for path in directory.rglob("*"):
        inner = path.relative_to(directory)
        if (
            not path.is_file()
            or inner.parts[0] == LAYOUT_DIR
            or path.name in _HASH_IGNORED_NAMES
            or path.suffix == ".pyc"
            or any(part in _HASH_IGNORED_DIRS for part in inner.parts)
        ):
            continue
        files.append((inner.as_posix(), path))
    return sorted(files)


def install_hash(directory: Path) -> str:
    """Хэш установки пакета — sha256 его файлов: «sha256:<hex>».

    Все файлы каталога пакета (кроме служебных файлов ОС, кэшей интерпретатора и раскладки
    визуального редактора .layout/) в порядке
    отсортированных относительных путей; в хэш входят путь, длина и байты каждого файла —
    как они лежат в git, до подстановки ${ПЕРЕМЕННЫХ} установки. Меток времени и прав файлов
    нет: одинаковый пакет даёт одинаковый хэш на любой машине. Та же формула — канон
    contentHash lock-файла (TAI-ADR-0062 п.3)."""
    digest = hashlib.sha256()
    for relative, path in _hashed_files(directory):
        data = path.read_bytes()
        digest.update(relative.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0")
        digest.update(data)
    return "sha256:" + digest.hexdigest()


def package_ref(package: Package) -> dict[str, str]:
    """{key, version} пакета, как их называет его package.yaml (PackageRef ядра)."""
    version = package.spec.get("version")
    if version in (None, ""):
        raise PackageError(
            f"{_rel(package.path / 'package.yaml')}: no spec.version — a link to a package "
            "cannot be written without a version"
        )
    return {"key": package.key, "version": str(version)}
