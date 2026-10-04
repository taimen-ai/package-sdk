"""Настройки пакета (TAI-ADR-0067, CP-ADR-0081): ``spec.settings`` манифеста в ``check``,
ссылки ``settings.<путь>`` в процессах, правилах и видах, сводка для ``describe``.

Объявление проверяет сам SDK — чистыми функциями, без кода ядра, теми же кодами и путями
(от ``/spec/settings``), что проверка ядра при плане (CP-ADR-0081 п.1–2, амендмент А1):

- ``settings_schema_unsupported`` — конструкция вне подмножества схемы манифеста: ключевое
  слово не своего типа, ``required`` на поле, которого нет в ``properties``, ``enum`` у
  ``object`` и ``array``, пределы вложенности и числа свойств;
- ``settings_default_missing`` — у необязательного скалярного поля или массива нет ``default``;
- ``settings_default_invalid`` — ``default`` не проходит схему своего поля (тип, ``enum``,
  пределы, формат);
- ``settings_secret_field`` — признак секрета: ``writeOnly``, ``format: password``, имя поля
  по списку ядра (:data:`SECRET_KEY_HINTS`, ``secretRef`` допустим), материал секрета в
  ``default`` или ``enum``;
- ``settings_uischema_unsupported`` — раскладка вне закрытого подмножества JSON Forms
  (``scope`` не на свойство схемы, второй ``Control`` на то же свойство, подпись группы не
  ``<пакет>.settings.groups.<id>``); предупреждение ``settings_uischema_uncovered`` — поле
  без ``Control``;
- ``settings_label_missing`` — обязательного ключа подписи нет в словаре объявленного языка.

Подмножество — ровно подмножество ядра по CP-ADR-0081 п.1–2 (амендмент А1 оставил выбор:
схема манифеста догоняет ADR): ``enum`` у ``boolean``, ``minItems``/``maxItems`` у массива,
массив массивов (вложенность — как у объектов), у ``Control`` нет ``options``, условие
``rule`` — подмножество п.1 с ``const``. Схема манифеста ``schema/v1/object.schema.json``
допускает то же; множества ключевых слов закреплены contract-тестом против кода ядра рядом.
Ошибки схемы формата внутри ``spec.settings`` ``check`` заменяет находками отсюда; то, что
схема ловит, а разбор ниже нет, всё равно выходит находкой с кодом (:func:`_schema_net`).

Ссылки ``settings.<путь>`` (CP-ADR-0081 п.6, амендмент Б1, Б3): поле не объявлено —
``settings_ref_unknown``, тип поля не подходит месту — ``settings_ref_type``; путь находки —
JSON Pointer выражения в файле объекта. Если код ядра рядом знает настройки
(``control_plane.domain.settings_refs``), процессы проверяет его ``check_process``, правила —
его ``work_rules.check_settings_refs`` (первое неподходящее чтение, как в ``plan``), а виды —
его ``check_view`` (``screens``). Без ядра правила проверяются здесь полностью (место чтения
задаёт тип, таблица :data:`_FITS` — как у ядра), а в выражениях CEL процессов и видов
ловится только необъявленный путь, тип проверит ядро в ``plan``. Ветка ``catch … as:
settings`` процесса настроек не читает: там имя — ошибка (Б1).

Число рабочих единиц срока шага и его ``warnBefore`` бывает выражением ``{expr: <CEL>}``
(CP-ADR-0081, амендмент Г1–Г2): оно должно давать целое. Его проверяет ядро, если оно
считает такие сроки (``process_sla.amount_of``); иначе — здесь: выражение из одного чтения
``settings.<путь>`` требует поле ``integer`` (``settings_ref_type``), в составном ловится
только необъявленный путь.

Находка — «<файл>: <код>: <сообщение> [<путь>]», как у экранов (:class:`screens.Finding`).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, fields, replace
from datetime import date
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import jsonschema

from package_sdk import schema as schema_module
from package_sdk import screens
from package_sdk.model import I18N_DIR, Installation, Obj, Package, _rel
from package_sdk.screens import Finding, pointer

SETTINGS_FIELD = "settings"
BASE = "/spec/settings"
SCHEMA_BASE = f"{BASE}/schema"
UISCHEMA_BASE = f"{BASE}/uischema"

SCHEMA_UNSUPPORTED = "settings_schema_unsupported"
DEFAULT_MISSING = "settings_default_missing"
DEFAULT_INVALID = "settings_default_invalid"
SECRET_FIELD = "settings_secret_field"
UISCHEMA_UNSUPPORTED = "settings_uischema_unsupported"
UISCHEMA_UNCOVERED = "settings_uischema_uncovered"
LABEL_MISSING = "settings_label_missing"
REF_UNKNOWN = "settings_ref_unknown"
REF_TYPE = "settings_ref_type"
DECLARATION_CODES = frozenset(
    {
        SCHEMA_UNSUPPORTED,
        DEFAULT_MISSING,
        DEFAULT_INVALID,
        SECRET_FIELD,
        UISCHEMA_UNSUPPORTED,
        UISCHEMA_UNCOVERED,
        LABEL_MISSING,
    }
)
REF_CODES = frozenset({REF_UNKNOWN, REF_TYPE})

REF = "x-ref"
REF_KINDS = ("role", "principal", "workspace", "taskType", "calendar")
TYPES = ("object", "string", "integer", "number", "boolean", "array")
SCALARS = ("string", "integer", "number", "boolean")
FORMATS = ("date", "uri", "email", "uuid")
# Вложенность объектов с корнем, свойств на объект, значений enum (CP-ADR-0081 п.1).
MAX_OBJECT_DEPTH = 3
MAX_PROPERTIES = 100
MAX_ENUM = 100
# Раскладка: вложенность элементов и элементов всего (CP-ADR-0081 п.2).
MAX_UI_DEPTH = 5
MAX_UI_ELEMENTS = 200
FIELD_NAME = re.compile(r"^[a-z][A-Za-z0-9_]{0,62}$")
LABEL_KEY = re.compile(r"^[a-z0-9][a-z0-9-]*(\.[A-Za-z0-9_-]+)+$")
SCOPE_PREFIX = "#/properties/"
SCOPE_STEP = "/properties/"

# Имя поля с признаком секрета — список и исключения ядра (control_plane.domain.project:
# _SECRET_KEY_HINTS, _SECRET_KEY_ALLOWED, secret_key_name; CP-ADR-0081 п.1). Совпадение
# закреплено contract-тестом против кода ядра рядом.
SECRET_KEY_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "privatekey",
    "private_key",
    "credential",
    "authorization",
    "clientsecret",
    "client_secret",
)
SECRET_KEY_ALLOWED = frozenset({"secretref", "secret_ref"})
# Материал секрета в строке — образцы ядра (control_plane.domain.redaction), тоже закреплены.
SECRET_MATERIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pem_private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("provider_token", re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{16,}")),
    ("provider_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("provider_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("provider_token", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("provider_token", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE)),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    (
        "credential_assignment",
        re.compile(
            r"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)"
            r"\s*[:=]\s*[\"']?[A-Za-z0-9+/=_.-]{16,}",
            re.IGNORECASE,
        ),
    ),
)

# Ключевые слова поля по типу (схема манифеста: settingsKeywords); type и default — у любого.
_COMMON = frozenset({"type", "default"})
_BY_TYPE: dict[str, frozenset[str]] = {
    "object": frozenset({"properties", "required", "additionalProperties"}),
    "string": frozenset({"enum", "minLength", "maxLength", "pattern", "format", REF}),
    "integer": frozenset({"enum", "minimum", "maximum"}),
    "number": frozenset({"enum", "minimum", "maximum"}),
    "boolean": frozenset({"enum"}),
    "array": frozenset({"items", "minItems", "maxItems"}),
}
_ALL_KEYWORDS = _COMMON.union(*_BY_TYPE.values())
# Подписи — в словарях пакета, не в схеме (CP-ADR-0081 п.1).
_LABEL_KEYWORDS = ("title", "description")
_SECRET_KEYWORDS = ("writeOnly",)
# Условие правила раскладки: подмножество п.1 без x-ref и default, и const (CP-ADR-0081 п.2).
_RULE_SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "const",
        "enum",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minItems",
        "maxItems",
    }
)
_RULE_EFFECTS = ("SHOW", "HIDE", "ENABLE", "DISABLE")
_UI_FIELDS: dict[str, frozenset[str]] = {
    "VerticalLayout": frozenset({"type", "elements", "rule"}),
    "HorizontalLayout": frozenset({"type", "elements", "rule"}),
    "Group": frozenset({"type", "label", "elements", "rule"}),
    "Control": frozenset({"type", "scope", "label", "rule"}),
    "Label": frozenset({"type", "text", "rule"}),
}
_UI_ROOTS = ("VerticalLayout", "HorizontalLayout", "Group")
_LAYOUTS = ("VerticalLayout", "HorizontalLayout", "Group")

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]+$")
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*$")

CORE_DUE_TYPES_MISSING = (
    "settings: the types of settings.<path> reads in deadline expressions (due {expr}) are not "
    "checked here — the control-plane code next to the SDK predates them "
    "(CP-ADR-0081, amendment of 2026-10-03); the core checks them in plan"
)
CORE_TYPES_MISSING = (
    "settings: the types of settings.<path> reads in process expressions are not checked here "
    "— the control-plane code next to the SDK predates CP-ADR-0081 "
    "(control_plane.domain.settings_refs); the core checks them in plan"
)


def secret_key_name(name: str) -> bool:
    """Имя поля похоже на секрет (``secretRef`` — нет), как ``project.secret_key_name`` ядра."""
    compact = name.lower().replace("-", "_").replace("_", "")
    return compact not in SECRET_KEY_ALLOWED and any(
        hint.replace("_", "") in compact for hint in SECRET_KEY_HINTS
    )


def secret_material(value: str) -> str | None:
    """Вид материала секрета в строке или None (``redaction.secret_material`` ядра)."""
    for kind, pattern in SECRET_MATERIAL_PATTERNS:
        if pattern.search(value):
            return kind
    return None


def _material(value: Any) -> bool:
    """Материал секрета в любой строке значения, имена членов тоже."""
    if isinstance(value, str):
        return secret_material(value) is not None
    if isinstance(value, dict):
        return any(_material(str(k)) or _material(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_material(item) for item in value)
    return False


def declaration(package: Package) -> Any:
    """``spec.settings`` пакета или None — пакет настроек не объявляет."""
    return package.spec.get(SETTINGS_FIELD)


def declared_schema(package: Package) -> Mapping[str, Any] | None:
    """Схема настроек пакета, если объявление — словарь со схемой-словарём."""
    raw = declaration(package)
    schema = raw.get("schema") if isinstance(raw, Mapping) else None
    return schema if isinstance(schema, Mapping) else None


def _parts(where: str) -> list[str]:
    return [p.replace("~1", "/").replace("~0", "~") for p in where.split("/")[1:]]


def _join(where: str, *names: Any) -> str:
    return pointer(*_parts(where), *names)


# --- значения по схеме поля -------------------------------------------------------------------


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _type_error(value: Any, kind: str) -> bool:
    if kind == "string":
        return not isinstance(value, str)
    if kind == "boolean":
        return not isinstance(value, bool)
    if kind == "integer":
        if isinstance(value, bool):
            return True
        if isinstance(value, float):
            return not (math.isfinite(value) and value.is_integer())
        return not isinstance(value, int)
    if kind == "number":
        return not _number(value)
    if kind == "array":
        return not isinstance(value, list)
    if kind == "object":
        return not isinstance(value, dict)
    return True


def _format_error(value: str, kind: str) -> bool:
    if kind == "date":
        if not _DATE.match(value):
            return True
        try:
            date.fromisoformat(value)
        except ValueError:
            return True
        return False
    if kind == "uuid":
        return not _UUID.match(value)
    if kind == "email":
        return not _EMAIL.match(value)
    if kind == "uri":
        if any(ch.isspace() for ch in value):
            return True
        try:
            parts = urlsplit(value)
        except ValueError:
            return True
        return not (_URI_SCHEME.match(parts.scheme) and (parts.netloc or parts.path))
    return False


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def validate(value: Any, node: Mapping[str, Any], path: str = "") -> list[dict[str, str]]:
    """Нарушения схемы поля значением: ``{path, code, message}``, самого значения в них нет
    (``package_settings.validate`` ядра; ``code`` — ключевое слово JSON Schema)."""
    errors: list[dict[str, str]] = []
    _validate(value, node, path, errors)
    return errors


def _validate(value: Any, node: Mapping[str, Any], path: str, errors: list[dict[str, str]]) -> None:
    def error(code: str, message: str, at: str = path) -> None:
        errors.append({"path": at, "code": code, "message": message})

    kind = node.get("type")
    if not isinstance(kind, str) or _type_error(value, kind):
        error("type", f"must be of type {kind}")
        return
    if "enum" in node and _canonical(value) not in {_canonical(i) for i in node["enum"]}:
        error("enum", "must be one of the values of enum")
    if kind in ("integer", "number"):
        if _number(node.get("minimum")) and value < node["minimum"]:
            error("minimum", f"must be >= {node['minimum']}")
        if _number(node.get("maximum")) and value > node["maximum"]:
            error("maximum", f"must be <= {node['maximum']}")
    elif kind == "string":
        if _count(node.get("minLength")) and len(value) < node["minLength"]:
            error("minLength", f"must be at least {node['minLength']} long")
        if _count(node.get("maxLength")) and len(value) > node["maxLength"]:
            error("maxLength", f"must be at most {node['maxLength']} long")
        if isinstance(node.get("pattern"), str):
            try:
                if re.search(node["pattern"], value) is None:
                    error("pattern", "must match the pattern of the field")
            except re.error:
                pass
        if isinstance(node.get("format"), str) and _format_error(value, node["format"]):
            error("format", f"must be a {node['format']}")
    elif kind == "array":
        if _count(node.get("minItems")) and len(value) < node["minItems"]:
            error("minItems", f"must have at least {node['minItems']} items")
        if _count(node.get("maxItems")) and len(value) > node["maxItems"]:
            error("maxItems", f"must have at most {node['maxItems']} items")
        items = node.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                _validate(item, items, f"{path}/{index}", errors)
    elif kind == "object":
        properties = node.get("properties") or {}
        for name in node.get("required") or ():
            if name not in value:
                error("required", "is required", path + pointer(name))
        for name, item in value.items():
            child = properties.get(name)
            if not isinstance(child, Mapping):
                error(
                    "additionalProperties", "is not a field of the settings", path + pointer(name)
                )
                continue
            _validate(item, child, path + pointer(name), errors)


# --- объявление: схема ------------------------------------------------------------------------


@dataclass(frozen=True)
class _Problem:
    code: str
    path: str
    message: str
    warning: bool = False


class _SchemaCheck:
    """Подмножество JSON Schema (CP-ADR-0081 п.1, схема манифеста), признаки секрета, default."""

    def __init__(self) -> None:
        self.problems: list[_Problem] = []

    def add(self, code: str, path: str, message: str) -> None:
        self.problems.append(_Problem(code, path, message))

    def unsupported(self, path: str, message: str) -> None:
        self.add(SCHEMA_UNSUPPORTED, path, message)

    def root(self, schema: Any) -> list[_Problem]:
        if not isinstance(schema, Mapping):
            self.unsupported(SCHEMA_BASE, "the schema is a mapping")
            return self.problems
        if schema.get("type") != "object":
            self.unsupported(SCHEMA_BASE + "/type", "the root of the schema is type: object")
            return self.problems
        self._field(schema, SCHEMA_BASE, depth=1, field=False, required=True)
        return self.problems

    def _field(
        self, node: Any, where: str, *, depth: int, field: bool, required: bool, name: str = ""
    ) -> None:
        """Узел схемы; ``depth`` — объекты от корня, корень тоже."""
        if not isinstance(node, Mapping):
            self.unsupported(where, "a field of the schema is a mapping")
            return
        if self._secret(node, where):
            return
        before = len(self.problems)
        for keyword in node:
            if keyword in _LABEL_KEYWORDS:
                self.unsupported(
                    _join(where, keyword),
                    f"{keyword} is not written in the schema: labels are keys "
                    "<package>.settings.<path>[.help] of the package dictionaries",
                )
            elif keyword not in _ALL_KEYWORDS:
                self.unsupported(
                    _join(where, keyword), f"{keyword} is outside the subset of the settings schema"
                )
        kind = node.get("type")
        if kind not in TYPES:
            self.unsupported(f"{where}/type", "type is one of " + ", ".join(TYPES))
            return
        for keyword in sorted(set(node) & _ALL_KEYWORDS - _COMMON - _BY_TYPE[kind]):
            message = (
                f"{REF} is only on a string (or the items of an array of strings)"
                if keyword == REF
                else f"{keyword} does not apply to type {kind}"
            )
            self.unsupported(f"{where}/{keyword}", message)
        if kind == "object":
            if depth > MAX_OBJECT_DEPTH:
                self.unsupported(where, f"objects nest at most {MAX_OBJECT_DEPTH} levels deep")
                return
            self._object(node, where, depth)
        elif kind == "array":
            self._array(node, where, depth)
        else:
            self._scalar(node, kind, where)
        if "default" in node:
            # default сверяется только с полем без находок: иначе схема поля не та, что задумана
            if len(self.problems) == before:
                for error in validate(node["default"], node):
                    inner = f" (at {error['path']})" if error["path"] else ""
                    self.add(
                        DEFAULT_INVALID,
                        f"{where}/default",
                        f"default does not match the field{inner}: {error['message']}",
                    )
        elif field and not required and kind != "object":
            self.add(
                DEFAULT_MISSING,
                where,
                f"the optional field {name} has no default: give it one or list it in required",
            )

    def _secret(self, node: Mapping[str, Any], where: str) -> bool:
        found = False
        for keyword in _SECRET_KEYWORDS:
            if node.get(keyword) is True:
                self._secret_field(f"{where}/{keyword}", f"{keyword} marks a secret")
                found = True
        if node.get("format") == "password":
            self._secret_field(f"{where}/format", "format: password marks a secret")
            found = True
        if "default" in node and _material(node["default"]):
            self._secret_field(f"{where}/default", "the default carries credential material")
            found = True
        enum = node.get("enum")
        if isinstance(enum, list):
            for index, item in enumerate(enum):
                if isinstance(item, str) and secret_material(item):
                    self._secret_field(
                        f"{where}/enum/{index}", "a value of enum carries credential material"
                    )
                    found = True
        return found

    def _secret_field(self, where: str, message: str) -> None:
        self.add(
            SECRET_FIELD,
            where,
            f"{message}: settings hold no secrets — a secret belongs to a connection or a "
            "named secret of an agent",
        )

    def _object(self, node: Mapping[str, Any], where: str, depth: int) -> None:
        if node.get("additionalProperties", False) is not False:
            self.unsupported(
                f"{where}/additionalProperties", "additionalProperties is false on every object"
            )
        properties = node.get("properties")
        if not isinstance(properties, Mapping) or not properties:
            self.unsupported(f"{where}/properties", "an object declares its properties")
            return
        if len(properties) > MAX_PROPERTIES:
            self.unsupported(
                f"{where}/properties", f"an object has at most {MAX_PROPERTIES} properties"
            )
            return
        required = node.get("required", [])
        if (
            not isinstance(required, list)
            or not all(isinstance(n, str) for n in required)
            or len(set(required)) != len(required)
        ):
            self.unsupported(f"{where}/required", "required is a list of distinct field names")
            required = []
        for index, name in enumerate(required):
            if name not in properties:
                self.unsupported(
                    f"{where}/required/{index}",
                    f"required names {name}, which is not a property of the object",
                )
        for name, child in properties.items():
            at = _join(where, "properties", name)
            if not isinstance(name, str) or not FIELD_NAME.match(name):
                self.unsupported(at, f"a field name matches {FIELD_NAME.pattern}")
                continue
            if secret_key_name(name):
                self._secret_field(at, f"the name {name} marks a secret")
                continue
            self._field(
                child, at, depth=depth + 1, field=True, required=name in required, name=name
            )

    def _array(self, node: Mapping[str, Any], where: str, depth: int) -> None:
        for keyword in ("minItems", "maxItems"):
            if keyword in node and not _count(node[keyword]):
                self.unsupported(f"{where}/{keyword}", f"{keyword} is an integer >= 0")
        items = node.get("items")
        if items is None:
            self.unsupported(f"{where}/items", "an array declares its items: a field schema")
            return
        # items массива — уровень вложенности, как у ядра: массив массивов допустим, пока
        # самый глубокий уровень держит скаляры
        if (
            isinstance(items, Mapping)
            and depth > MAX_OBJECT_DEPTH
            and items.get("type") not in SCALARS
        ):
            self.unsupported(
                f"{where}/items", "at the deepest level the items of an array are scalars"
            )
            return
        self._field(items, f"{where}/items", depth=depth + 1, field=False, required=True)

    def _scalar(self, node: Mapping[str, Any], kind: str, where: str) -> None:
        if "enum" in node:
            enum = node["enum"]
            if (
                not isinstance(enum, list)
                or not 1 <= len(enum) <= MAX_ENUM
                or any(_type_error(item, kind) for item in enum)
                or len({_canonical(item) for item in enum}) != len(enum)
            ):
                self.unsupported(
                    f"{where}/enum",
                    f"enum is a list of 1..{MAX_ENUM} distinct values of type {kind}",
                )
        for keyword in ("minimum", "maximum"):
            if keyword in node and not _number(node[keyword]):
                self.unsupported(f"{where}/{keyword}", f"{keyword} is a number")
        for keyword in ("minLength", "maxLength"):
            if keyword in node and not _count(node[keyword]):
                self.unsupported(f"{where}/{keyword}", f"{keyword} is an integer >= 0")
        if "pattern" in node:
            pattern = node["pattern"]
            try:
                ok = isinstance(pattern, str) and bool(pattern) and re.compile(pattern) is not None
            except re.error:
                ok = False
            if not ok:
                self.unsupported(f"{where}/pattern", "pattern is a regular expression")
        if "format" in node and node["format"] not in FORMATS:
            self.unsupported(f"{where}/format", "format is one of " + ", ".join(FORMATS))
        if REF in node and node[REF] not in REF_KINDS:
            self.unsupported(f"{where}/{REF}", f"{REF} is one of " + ", ".join(REF_KINDS))


# --- объявление: раскладка формы --------------------------------------------------------------


def scope_path(scope: Any) -> str | None:
    """``#/properties/a/properties/b`` → ``a.b``; None — другая форма."""
    if not isinstance(scope, str) or not scope.startswith(SCOPE_PREFIX):
        return None
    names = scope[len(SCOPE_PREFIX) :].split(SCOPE_STEP)
    if not all(FIELD_NAME.match(name) for name in names):
        return None
    return ".".join(names)


def field_at(schema: Mapping[str, Any], path: str) -> Mapping[str, Any] | None:
    """Поле схемы по пути через точку; в ``items`` массива путь не заходит."""
    node: Any = schema
    for name in path.split("."):
        properties = node.get("properties") if isinstance(node, Mapping) else None
        if not isinstance(properties, Mapping) or name not in properties:
            return None
        node = properties[name]
    return node if isinstance(node, Mapping) else None


def settings_fields(
    schema: Mapping[str, Any], base: str = ""
) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """Каждое свойство схемы, вложенные тоже, с путём через точку."""
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return
    for name, node in properties.items():
        path = f"{base}.{name}" if base else str(name)
        if not isinstance(node, Mapping):
            continue
        yield path, node
        if node.get("type") == "object":
            yield from settings_fields(node, path)


class _UiCheck:
    """Закрытое подмножество JSON Forms, которое рисует консоль (CP-ADR-0081 п.2)."""

    def __init__(self, package: str, schema: Mapping[str, Any]) -> None:
        self.package = package
        self.schema = schema
        self.problems: list[_Problem] = []
        self.elements = 0
        self.controls: dict[str, str] = {}

    def unsupported(self, path: str, message: str) -> None:
        self.problems.append(_Problem(UISCHEMA_UNSUPPORTED, path, message))

    def root(self, node: Any) -> list[_Problem]:
        if not isinstance(node, Mapping) or node.get("type") not in _UI_ROOTS:
            self.unsupported(
                UISCHEMA_BASE + "/type", "the root of uischema is " + ", ".join(_UI_ROOTS)
            )
            return self.problems
        self._element(node, UISCHEMA_BASE, 1)
        if not self.problems:
            for path, node_ in settings_fields(self.schema):
                if node_.get("type") == "object":
                    continue
                if not any(path == c or path.startswith(c + ".") for c in self.controls):
                    self.problems.append(
                        _Problem(
                            UISCHEMA_UNCOVERED,
                            UISCHEMA_BASE,
                            f"no Control edits {path}: its value applies but the form cannot "
                            "change it",
                            warning=True,
                        )
                    )
        return self.problems

    def _element(self, node: Any, where: str, depth: int) -> None:
        self.elements += 1
        if self.elements == MAX_UI_ELEMENTS + 1:
            self.unsupported(UISCHEMA_BASE, f"uischema has at most {MAX_UI_ELEMENTS} elements")
        if depth > MAX_UI_DEPTH:
            self.unsupported(where, f"elements nest at most {MAX_UI_DEPTH} levels deep")
            return
        if not isinstance(node, Mapping):
            self.unsupported(where, "an element is a mapping")
            return
        kind = node.get("type")
        if kind not in _UI_FIELDS:
            self.unsupported(f"{where}/type", "type is one of " + ", ".join(_UI_FIELDS))
            return
        for name in sorted(set(node) - _UI_FIELDS[kind]):
            self.unsupported(_join(where, name), f"a {kind} has no {name}")
        if "rule" in node:
            self._rule(node["rule"], f"{where}/rule")
        if kind == "Group":
            label = node.get("label")
            prefix = f"{self.package}.settings.groups."
            if not _key(label) or not str(label).startswith(prefix):
                self.unsupported(f"{where}/label", f"the label of a Group is a key {prefix}<id>")
        if kind == "Control":
            self._control(node, where)
        if kind == "Label" and not _key(node.get("text")):
            self.unsupported(f"{where}/text", "text is a key of the package dictionaries")
        if kind in _LAYOUTS:
            elements = node.get("elements")
            if not isinstance(elements, list) or not elements:
                self.unsupported(f"{where}/elements", "elements is a non-empty list")
                return
            for index, child in enumerate(elements):
                self._element(child, f"{where}/elements/{index}", depth + 1)

    def _control(self, node: Mapping[str, Any], where: str) -> None:
        if "label" in node and not _key(node["label"]):
            self.unsupported(f"{where}/label", "label is a key of the package dictionaries")
        path = self._scope(node.get("scope"), f"{where}/scope")
        if path is None:
            return
        if path in self.controls:
            self.unsupported(
                f"{where}/scope", f"{path} already has a Control at {self.controls[path]}"
            )
            return
        self.controls[path] = where

    def _scope(self, scope: Any, where: str) -> str | None:
        path = scope_path(scope)
        if path is None or field_at(self.schema, path) is None:
            self.unsupported(
                where,
                f"scope {scope!r} points at no property of the settings schema "
                "(#/properties/<field>[/properties/<field>…])",
            )
            return None
        return path

    def _rule(self, rule: Any, where: str) -> None:
        if not isinstance(rule, Mapping) or set(rule) != {"effect", "condition"}:
            self.unsupported(where, "a rule is {effect, condition}")
            return
        if rule["effect"] not in _RULE_EFFECTS:
            self.unsupported(f"{where}/effect", "effect is one of " + ", ".join(_RULE_EFFECTS))
        condition = rule["condition"]
        at = f"{where}/condition"
        if (
            not isinstance(condition, Mapping)
            or not {"scope", "schema"} <= set(condition)
            or not set(condition) <= {"scope", "schema", "failWhenUndefined"}
        ):
            self.unsupported(
                at,
                "a condition is {scope, schema, failWhenUndefined?}: OR, AND and LEAF are "
                "outside the subset",
            )
            return
        self._scope(condition["scope"], f"{at}/scope")
        schema = condition["schema"]
        if (
            not isinstance(schema, Mapping)
            or not schema
            or not set(schema) <= _RULE_SCHEMA_KEYWORDS
        ):
            self.unsupported(
                f"{at}/schema",
                "the schema of a condition uses " + ", ".join(sorted(_RULE_SCHEMA_KEYWORDS)),
            )
        if "failWhenUndefined" in condition and not isinstance(
            condition["failWhenUndefined"], bool
        ):
            self.unsupported(f"{at}/failWhenUndefined", "failWhenUndefined is a boolean")


def _key(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= 200 and bool(LABEL_KEY.match(value))


# --- объявление: подписи ----------------------------------------------------------------------


def label_keys(package: Package) -> list[tuple[str, str]]:
    """(путь, ключ словаря) обязательных подписей настроек (CP-ADR-0081 п.1): имя пакета,
    каждое свойство схемы (не ``items`` массива — амендмент Б6), ``label`` и ``text`` раскладки."""
    schema = declared_schema(package)
    if schema is None:
        return []
    key = package.key
    required = [(BASE, f"{key}.title")]
    for path, _node in settings_fields(schema):
        names = [p for name in path.split(".") for p in ("properties", name)]
        required.append((SCHEMA_BASE + pointer(*names), f"{key}.settings.{path}"))
    raw = declaration(package)
    uischema = raw.get("uischema") if isinstance(raw, Mapping) else None
    required += list(_ui_keys(uischema, UISCHEMA_BASE))
    return required


def shown_keys(package: Package) -> set[str]:
    """Ключи словаря, которые показывает форма настроек: обязательные и пояснения полей
    ``<пакет>.settings.<путь>.help`` — для ``unused_message`` экранов."""
    used = {key for _, key in label_keys(package)}
    schema = declared_schema(package)
    if schema is not None:
        used |= {f"{package.key}.settings.{path}.help" for path, _ in settings_fields(schema)}
    return used


def _ui_keys(node: Any, where: str) -> Iterator[tuple[str, str]]:
    if not isinstance(node, Mapping):
        return
    for name in ("label", "text"):
        if isinstance(node.get(name), str):
            yield f"{where}/{name}", node[name]
    elements = node.get("elements")
    for index, child in enumerate(elements if isinstance(elements, list) else ()):
        yield from _ui_keys(child, f"{where}/elements/{index}")


def _labels(package: Package) -> list[_Problem]:
    found, _ = screens.dictionaries(package)
    locales = screens.declared_locales(package)
    if not locales:
        return [
            _Problem(
                LABEL_MISSING,
                BASE,
                "the package declares settings: package.yaml declares locales and defaultLocale, "
                "and the dictionaries i18n/<locale>.yaml hold the labels of the fields",
            )
        ]
    problems = []
    for where, message_key in label_keys(package):
        for locale in locales:
            dictionary = found.get(locale)
            if dictionary is None or message_key not in dictionary.messages:
                problems.append(
                    _Problem(
                        LABEL_MISSING,
                        where,
                        f"{message_key} is not in the dictionary of {locale} "
                        f"({I18N_DIR}/{locale}.yaml)",
                    )
                )
    return problems


# --- объявление: сетка схемы формата ----------------------------------------------------------


def _schema_net(raw: Any) -> list[_Problem]:
    """Нарушения схемы формата внутри ``spec.settings`` — коды по месту (``schema`` или
    ``uischema``). Нужны, только если разбор выше чего-то не заметил."""
    object_schema = schema_module.load(schema_module.OBJECT)
    validator = jsonschema.Draft202012Validator(
        {"$ref": f"{object_schema['$id']}#/$defs/packageSettings"},
        registry=schema_module.registry(),
    )
    problems = []
    for error in validator.iter_errors(raw):
        parts = list(error.absolute_path)
        code = UISCHEMA_UNSUPPORTED if parts[:1] == ["uischema"] else SCHEMA_UNSUPPORTED
        where = BASE + pointer(*parts)
        problems.append(_Problem(code, where, f"outside the format schema: {error.message}"))
    return problems


def check_declaration(package: Package) -> list[Finding]:
    """Находки ``spec.settings`` пакета: подмножества п.1 и п.2 и подписи."""
    if SETTINGS_FIELD not in package.spec:
        return []
    raw = declaration(package)
    manifest = _rel(package.path / "package.yaml")
    problems: list[_Problem] = []
    if not isinstance(raw, Mapping):
        problems.append(
            _Problem(SCHEMA_UNSUPPORTED, BASE, "settings is a mapping {schema, uischema?}")
        )
    else:
        for name in sorted(set(raw) - {"schema", "uischema"}):
            problems.append(
                _Problem(SCHEMA_UNSUPPORTED, _join(BASE, name), f"settings has no field {name!r}")
            )
        schema = raw.get("schema")
        if schema is None:
            problems.append(_Problem(SCHEMA_UNSUPPORTED, BASE, "settings declares its schema"))
        else:
            problems += _SchemaCheck().root(schema)
        if raw.get("uischema") is not None and not problems and isinstance(schema, Mapping):
            problems += _UiCheck(package.key, schema).root(raw["uischema"])
        if not any(not p.warning for p in problems):
            net = _schema_net(raw)
            problems += net
            if not net:
                problems += _labels(package)
    return [Finding(manifest, p.code, p.message, p.path, p.warning) for p in problems]


# --- ссылки settings.<путь> -------------------------------------------------------------------

# Чтение настроек в выражении CEL: settings.a.b, settings.tags[0] (вне строковых литералов).
_CEL_READ = re.compile(r"(?<![\w.$])settings((?:\.[A-Za-z_]\w*|\[\d+\])+)")
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
_LOGICAL = ("and", "or")
_ORDERS = ("lt", "le", "gt", "ge")
# Чем служит чтение в правиле и какие типы JSON ему подходят (ядро: work_rules._FITS, Б3);
# count — число рабочих единиц срока процесса выражением (CP-ADR-0081 Г2).
_ORDERED = frozenset({"number", "integer", "string"})
_SCALAR = frozenset({"number", "integer", "string", "boolean"})
_FITS: Mapping[str, frozenset[str] | None] = {
    "order": _ORDERED,  # lt, le, gt, ge
    "equal": _SCALAR | {"array"},  # eq, ne
    "member": _SCALAR,  # левый операнд in
    "list": frozenset({"array"}),  # правый операнд in, forEach
    "text": _SCALAR,  # подстановка внутри текста
    "exists": None,  # что угодно
    "whole": None,  # шаблон из одного плейсхолдера сохраняет значение как есть
    "count": frozenset({"integer"}),
}
_USES = {
    "order": "a comparison (lt, le, gt, ge) wants a number or a string",
    "equal": "eq and ne want a scalar or an array",
    "member": "the left operand of in wants a scalar",
    "list": "the right operand of in and forEach want an array",
    "text": "a placeholder inside text wants a scalar",
    "count": "a number of working units of a due wants an integer",
}


def _segments(path: str) -> list[str]:
    """``settings.a.tags[0]`` → ``[a, tags, 0]``."""
    rest = path[len(SETTINGS_FIELD) :].replace("[", ".").replace("]", "")
    return [part for part in rest.split(".") if part]


def settings_type(schema: Mapping[str, Any] | None, path: str) -> str | None:
    """Тип JSON поля по чтению ``settings.<путь>``; None — схема такого поля не объявляет."""
    if schema is None:
        return None
    node: Any = schema
    segments = _segments(path)
    if not segments:
        return None
    for segment in segments:
        if not isinstance(node, Mapping):
            return None
        if segment.isdigit():
            if node.get("type") != "array" or not isinstance(node.get("items"), Mapping):
                return None
            node = node["items"]
            continue
        properties = node.get("properties")
        if not isinstance(properties, Mapping) or segment not in properties:
            return None
        node = properties[segment]
    if not isinstance(node, Mapping):
        return None
    kind = node.get("type")
    return kind if isinstance(kind, str) else "object"


def _unknown_reason(package: Package) -> str:
    if declaration(package) is None:
        return f"package {package.key} declares no settings (spec.settings in package.yaml)"
    return f"the settings of package {package.key} declare no such field"


@dataclass(frozen=True)
class _Read:
    where: str  # JSON Pointer места в файле объекта
    path: str  # settings.<путь>
    use: str | None = None  # для правил: чем служит чтение (_FITS)


def _strings(node: Any, where: str) -> Iterator[tuple[str, str]]:
    if isinstance(node, str):
        yield where, node
    elif isinstance(node, Mapping):
        for name, item in node.items():
            if name in ("description", "displayName"):
                continue
            yield from _strings(item, _join(where, name))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _strings(item, f"{where}/{index}")


def shadowed(node: Any, where: str = "/spec") -> list[str]:
    """Пути ``do`` веток ``catch … as: settings`` процесса: там имя — ошибка, а не
    настройки (Б1), и чтения внутри ссылками на настройки не считаются."""
    found: list[str] = []
    if isinstance(node, Mapping):
        for name, item in node.items():
            here = _join(where, name)
            if name == "catch" and isinstance(item, list):
                for index, clause in enumerate(item):
                    if isinstance(clause, Mapping) and clause.get("as") == SETTINGS_FIELD:
                        found.append(f"{here}/{index}/do")
                    else:
                        found += shadowed(clause, f"{here}/{index}")
            else:
                found += shadowed(item, here)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found += shadowed(item, f"{where}/{index}")
    return found


def _outside(reads: list[_Read], branches: list[str]) -> list[_Read]:
    """Чтения вне веток ``branches`` (пути JSON Pointer)."""
    return [
        r for r in reads if not any(r.where == b or r.where.startswith(b + "/") for b in branches)
    ]


def cel_reads(text: str) -> list[str]:
    """Пути ``settings.<…>``, которые читает текст выражения CEL."""
    bare = screens._STRING_LITERAL.sub('""', text)
    return [SETTINGS_FIELD + match.group(1) for match in _CEL_READ.finditer(bare)]


def _expression_reads(spec: Mapping[str, Any]) -> list[_Read]:
    return [
        _Read(where, path) for where, text in _strings(spec, "/spec") for path in cel_reads(text)
    ]


# Выражение — одно чтение настроек целиком: его тип — тип поля.
_WHOLE_READ = re.compile(r"\s*settings(?:\.[A-Za-z_]\w*|\[\d+\])+\s*")
_WORKING_UNITS = ("workdays", "workhours")


def due_expressions(spec: Any, where: str = "/spec") -> Iterator[tuple[str, str]]:
    """(путь, текст) выражений числа рабочих единиц сроков процесса: ``due`` процесса и
    шагов и их ``warnBefore`` (CP-ADR-0081 Г1)."""
    if isinstance(spec, Mapping):
        for name, item in spec.items():
            here = _join(where, name)
            if name == "due" and isinstance(item, Mapping):
                yield from _amounts(item, here)
                if isinstance(item.get("warnBefore"), Mapping):
                    yield from _amounts(item["warnBefore"], f"{here}/warnBefore")
            else:
                yield from due_expressions(item, here)
    elif isinstance(spec, list):
        for index, item in enumerate(spec):
            yield from due_expressions(item, f"{where}/{index}")


def _amounts(span: Mapping[str, Any], where: str) -> Iterator[tuple[str, str]]:
    for unit in _WORKING_UNITS:
        amount = span.get(unit)
        if isinstance(amount, Mapping) and isinstance(amount.get("expr"), str):
            yield f"{where}/{unit}/expr", amount["expr"]


def due_reads(spec: Mapping[str, Any]) -> list[_Read]:
    """Чтения настроек в сроках процесса; у выражения из одного чтения — место ``count``."""
    return [
        _Read(where, path, "count" if _WHOLE_READ.fullmatch(text) else None)
        for where, text in due_expressions(spec)
        for path in cel_reads(text)
    ]


def _var_read(value: Any, where: str, use: str) -> list[_Read]:
    if isinstance(value, str) and (value == SETTINGS_FIELD or value.startswith("settings.")):
        return [_Read(where, value, use)]
    return []


def _condition_reads(node: Any, where: str) -> list[_Read]:
    """Чтения настроек в условии правила (как ``work_rules._condition_reads`` ядра)."""
    if not isinstance(node, Mapping) or len(node) != 1:
        return []
    ((operator, args),) = node.items()
    here = _join(where, operator)
    found: list[_Read] = []
    if operator in _LOGICAL and isinstance(args, list):
        for index, item in enumerate(args):
            found += _condition_reads(item, f"{here}/{index}")
    elif operator == "not":
        found += _condition_reads(args, here)
    elif operator == "exists":
        found += _var_read(args, here, "exists")
    elif isinstance(args, list) and len(args) == 2:
        uses = (
            ("member", "list")
            if operator == "in"
            else ("order", "order")
            if operator in _ORDERS
            else ("equal", "equal")
        )
        for index, (operand, use) in enumerate(zip(args, uses, strict=True)):
            at = f"{here}/{index}"
            if isinstance(operand, Mapping) and set(operand) == {"var"}:
                found += _var_read(operand["var"], f"{at}/var", use)
            elif isinstance(operand, Mapping) and set(operand) != {"const"}:
                found += _condition_reads(operand, at)
    return found


def _template_reads(value: Any, where: str) -> list[_Read]:
    found: list[_Read] = []
    if isinstance(value, str):
        use = "whole" if _PLACEHOLDER.fullmatch(value.strip()) is not None else "text"
        for match in _PLACEHOLDER.finditer(value):
            found += _var_read(match.group(1).strip(), where, use)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            found += _template_reads(item, _join(where, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += _template_reads(item, f"{where}/{index}")
    return found


def rule_reads(spec: Mapping[str, Any]) -> list[_Read]:
    """Каждое чтение настроек правила с местом (``work_rules.settings_reads`` ядра)."""
    found = _condition_reads(spec.get("condition"), "/spec/condition")
    interpretation = spec.get("interpretation")
    if isinstance(interpretation, Mapping):
        found += _template_reads(interpretation.get("inputs"), "/spec/interpretation/inputs")
    action = spec.get("action")
    if isinstance(action, Mapping):
        found += _var_read(action.get("forEach"), "/spec/action/forEach", "list")
        if "where" in action:
            found += _condition_reads(action["where"], "/spec/action/where")
        for name in ("taskType", "dedupKeyTemplate", "fields", "acceptance", "check"):
            if name in action:
                found += _template_reads(action[name], f"/spec/action/{name}")
    return found


def reads_settings(obj: Obj) -> bool:
    """Объект упоминает ``settings`` (дёшево, до разбора)."""
    return SETTINGS_FIELD in json.dumps(obj.spec, ensure_ascii=False)


def _ref_findings(package: Package, obj: Obj, reads: list[_Read]) -> list[Finding]:
    where = _rel(obj.path)
    schema = declared_schema(package)
    findings: list[Finding] = []
    for read in reads:
        kind = settings_type(schema, read.path)
        if kind is None:
            findings.append(
                Finding(where, REF_UNKNOWN, f"{read.path}: {_unknown_reason(package)}", read.where)
            )
            continue
        fits = _FITS.get(read.use) if read.use is not None else None
        if fits is not None and kind not in fits:
            findings.append(
                Finding(
                    where,
                    REF_TYPE,
                    f"{read.path} is {kind}, which does not fit the place of the read: "
                    f"{_USES[str(read.use)]}",
                    read.where,
                )
            )
    return findings


# --- проверка ядром ---------------------------------------------------------------------------


def core_settings() -> Any:
    """Модули ядра, которые типизируют ``settings`` в выражениях (CP-ADR-0081 §6); None —
    ядро рядом их не знает."""
    try:
        from control_plane.domain import process_definition, settings_refs
    except ImportError:
        return None
    if "settings" not in {f.name for f in fields(process_definition.Catalog)}:
        return None
    try:  # срок выражением (CP-ADR-0081 Г1): ядро старше амендмента его не считает
        from control_plane.domain import process_sla
    except ImportError:
        process_sla = None
    from control_plane.domain import work_rules
    from control_plane.domain.errors import DomainError

    return SimpleNamespace(
        pd=process_definition,
        refs=settings_refs,
        due=core_due_expressions(process_sla),
        rules=work_rules if hasattr(work_rules, "check_settings_refs") else None,
        DomainError=DomainError,
    )


def core_due_expressions(process_sla: Any) -> bool:
    """Считает ли ядро число рабочих единиц срока выражением (CP-ADR-0081 Г1–Г3)."""
    return process_sla is not None and hasattr(process_sla, "amount_of")


def _skill_entry(pd: Any, spec: Mapping[str, Any]) -> Any:
    contract = spec.get("contract")
    if isinstance(contract, Mapping):
        return pd.SkillEntry(contract.get("inputs"), contract.get("outputs"))
    return pd.SkillEntry(spec.get("inputSchema"), spec.get("outputSchema"))


# Находки объявления, после которых схеме нельзя верить как типу ``settings``; подписи и
# раскладка типов не меняют.
_SCHEMA_BROKEN = frozenset({SCHEMA_UNSUPPORTED, DEFAULT_MISSING, DEFAULT_INVALID, SECRET_FIELD})


def schema_broken(findings: list[Finding]) -> bool:
    return any(f.code in _SCHEMA_BROKEN for f in findings)


def scope_of(core: Any, package: Package) -> Any:
    """``SettingsScope`` ядра для объектов пакета: ключ и схема (None — не объявлены или
    схема с ошибками)."""
    schema = declared_schema(package)
    if schema is not None and schema_broken(check_declaration(package)):
        schema = None
    return core.refs.SettingsScope(package.key, dict(schema) if schema is not None else None)


def _core_process(core: Any, scope: Any, obj: Obj, catalog: Any) -> list[Finding]:
    """Находки ссылок процесса проверкой ядра (``check_process`` с типом ``settings``)."""
    pd = core.pd
    try:
        spec = pd.normalized_spec(obj.spec)
    except pd.SpecError:
        return []  # форму процесса проверяют схема и plan
    checked = pd.check_process(obj.key, spec, replace(catalog, settings=scope))
    where = _rel(obj.path)
    return [
        Finding(where, p.code, p.message, p.path or "")
        for p in checked.problems
        if p.code in REF_CODES
    ]


def _field_pointer(field: Any) -> str:
    """Поле ошибки ядра (``condition.and[0].gt[1].var``) → путь находки плана
    (``/spec/condition/and/0/gt/1/var``), как ``package_catalog.finding`` ядра."""
    if not isinstance(field, str) or not field:
        return ""
    field = field.removeprefix("$.").removeprefix("spec.").removeprefix("spec")
    return "/spec" + "".join(
        "/" + part for part in field.replace("[", ".").replace("]", "").split(".") if part
    )


def _core_rule(core: Any, scope: Any, package: Package, obj: Obj) -> list[Finding]:
    """Находка ссылок правила проверкой ядра (``work_rules.check_settings_refs``): первое
    неподходящее чтение, как в ``plan``. Документы, которые ядро не нормализует (их форму
    проверяет ``check`` отдельно), проверяются здесь без ядра."""
    rules = core.rules
    spec = obj.spec if isinstance(obj.spec, Mapping) else {}
    try:
        normalized = rules.normalize_rule_spec(
            trigger=spec.get("trigger"),
            condition=spec.get("condition", True),
            interpretation=spec.get("interpretation"),
            action=spec.get("action"),
        )
    except core.DomainError:
        return _ref_findings(package, obj, rule_reads(spec))
    try:
        rules.check_settings_refs(normalized, scope)
    except core.DomainError as error:
        if error.code not in REF_CODES:
            raise
        field = (error.details or {}).get("field")
        return [Finding(_rel(obj.path), error.code, error.message, _field_pointer(field))]
    return []


def _catalog(core: Any, installation: Installation, package: Package) -> Any:
    visible = installation.visible(package.key)
    pd = core.pd
    return pd.Catalog(
        skills={
            f"{o.key}@{o.spec.get('version')}": _skill_entry(pd, o.spec)
            for o in visible
            if o.kind == "Skill"
        },
        task_types={
            o.key: o.spec.get("fieldSchema") or None for o in visible if o.kind == "TaskType"
        },
        agents=frozenset(o.key for o in visible if o.kind == "Agent"),
        calendars=frozenset(o.key for o in visible if o.kind == "Calendar"),
    )


# --- вход check -------------------------------------------------------------------------------


def check_settings(
    installation: Installation, *, core: Any = None, views: bool = True
) -> tuple[list[str], list[str]]:
    """Ошибки и предупреждения настроек всех пакетов установки.

    ``core`` — модули ядра (:func:`core_settings`); None — ссылки процессов проверяются
    без типов. ``views`` — проверять ли ссылки видов здесь: False, когда их проверяет ядро
    (``screens`` с ``ViewContext.settings``)."""
    errors: list[str] = []
    warnings: list[str] = []
    untyped = untyped_due = False
    for package in installation.packages:
        declared = check_declaration(package)
        for finding in declared:
            (warnings if finding.warning else errors).append(str(finding))
        # схема с ошибками — не тип: находки объявления уже сказали, что не так
        broken = schema_broken(declared)
        catalog = scope = None
        if core is not None:
            catalog = _catalog(core, installation, package)
            schema = declared_schema(package)
            scope = core.refs.SettingsScope(
                package.key, dict(schema) if schema is not None and not broken else None
            )
        for obj in sorted(package.objects, key=lambda o: (o.kind, o.key)):
            if obj.kind not in ("Process", "WorkRule", "View", "Component"):
                continue
            if not reads_settings(obj):
                continue
            found: list[Finding] = []
            if obj.kind == "WorkRule" and core is not None and core.rules is not None:
                if not broken:
                    found = _core_rule(core, scope, package, obj)
            elif obj.kind == "WorkRule":
                found = _ref_findings(package, obj, rule_reads(obj.spec))
            elif obj.kind == "Process" and core is not None:
                if not broken:
                    found = _core_process(core, scope, obj, catalog)
                    if not core.due:
                        # сроки выражением ядро не считает: их места проверяются здесь
                        dues = _outside(due_reads(obj.spec), shadowed(obj.spec))
                        places = {where for where, _text in due_expressions(obj.spec)}
                        found = [f for f in found if f.path not in places]
                        found += _ref_findings(package, obj, dues)
                        untyped_due = untyped_due or any(r.use is None for r in dues)
            elif obj.kind == "Process":
                typed = {(r.where, r.path): r for r in due_reads(obj.spec)}
                reads = [typed.get((r.where, r.path), r) for r in _expression_reads(obj.spec)]
                reads = _outside(reads, shadowed(obj.spec))
                found = _ref_findings(package, obj, reads)
                untyped = untyped or any(r.use is None for r in reads)
            elif views:
                spec = obj.spec if isinstance(obj.spec, Mapping) else {}
                reads = [
                    _Read(where, path)
                    for where, text in _view_expressions(spec)
                    for path in cel_reads(text)
                ]
                found = _ref_findings(package, obj, reads)
            errors += [str(f) for f in found if not f.warning]
    if untyped:
        warnings.append(CORE_TYPES_MISSING)
    if untyped_due:
        warnings.append(CORE_DUE_TYPES_MISSING)
    return errors, warnings


def _view_expressions(spec: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    """(путь, текст) выражений вида или компонента, ``source.filter`` и значения metrics и
    chart тоже."""
    source = spec.get("source")
    if isinstance(source, Mapping) and isinstance(source.get("filter"), str):
        yield "/spec/source/filter", source["filter"]
    layout = spec.get("layout")
    for index, block in enumerate(layout if isinstance(layout, list) else ()):
        if not isinstance(block, Mapping):
            continue
        at = f"/spec/layout/{index}"
        yield from screens._expressions(block, at)
        if block.get("block") == "metrics":
            for number, item in enumerate(block.get("items") or ()):
                if isinstance(item, Mapping) and isinstance(item.get("value"), str):
                    yield f"{at}/items/{number}/value", item["value"]
        if block.get("block") == "chart" and isinstance(block.get("value"), str):
            yield f"{at}/value", block["value"]


# --- describe ---------------------------------------------------------------------------------


def describe(package: Package) -> list[dict[str, Any]]:
    """Поля настроек пакета для ``describe``: путь, тип, обязательность, default, ссылка."""
    schema = declared_schema(package)
    if schema is None:
        return []
    out = []

    def walk(node: Mapping[str, Any], base: str) -> None:
        required = (
            set(node.get("required") or ()) if isinstance(node.get("required"), list) else set()
        )
        properties = node.get("properties")
        for name, child in properties.items() if isinstance(properties, Mapping) else ():
            if not isinstance(child, Mapping):
                continue
            path = f"{base}.{name}" if base else str(name)
            kind = child.get("type")
            if kind == "object":
                walk(child, path)
                continue
            raw_items = child.get("items")
            items: Mapping[str, Any] = raw_items if isinstance(raw_items, Mapping) else {}
            out.append(
                {
                    "name": path,
                    "package": package.key,
                    "type": f"array of {items.get('type')}" if kind == "array" else kind,
                    "required": name in required,
                    "default": child.get("default"),
                    "hasDefault": "default" in child,
                    "enum": child.get("enum"),
                    "ref": child.get(REF) or items.get(REF),
                }
            )

    walk(schema, "")
    return out
