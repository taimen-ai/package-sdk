"""Экраны пакета описанием (TAI-ADR-0066, CP-ADR-0080): виды View и Component, языки
``package.yaml`` и словари ``i18n/<locale>.yaml``.

Форма вида и компонента — побайтная копия схемы ядра (``schema/v1/view.schema.json``).
Проверка — две ступени, как у процессов:

- **ядро** — если код ядра рядом знает экраны (``control_plane.domain.views``), пакет
  проверяется его же проверкой (``check_locales``, ``check_component``, ``check_view``,
  ``unused_messages``) в контексте пакета и его ``requires``: пути ``data.*`` по схеме данных
  процесса, выражения CEL и их типы, форматы, подписи по умолчанию. Своей реализации этого
  здесь нет;
- **статика** — ядро старше экранов: форма по схеме, языки и словари, ключи строк, ссылки
  (процесс источника, ``open.view``, компонент, скилл ``invoke``) и агрегаты вне
  ``metrics``/``chart``. Пути и выражения тогда проверит ядро в ``plan`` — об этом
  предупреждение.

Находка — «<файл>: <код>: <сообщение> [<путь>]»; путь — JSON Pointer от документа, как у
находок плана ядра (``/spec/layout/0/columns/1/label``; у словаря — ключ строки).
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jsonschema

from package_sdk import schema as schema_module
from package_sdk.model import (
    I18N_DIR,
    SYSTEM_TASK_TYPE,
    Installation,
    Obj,
    Package,
    PackageError,
    _read_yaml,
    _rel,
)
from package_sdk.source import package_files

VIEW, COMPONENT = "View", "Component"
LOCALES_FIELD, DEFAULT_LOCALE_FIELD = "locales", "defaultLocale"
# Как у ядра (package_source.KEY_PATTERN, LOCALE_PATTERN, MESSAGE_KEY_PATTERN, MAX_MESSAGE_CHARS).
KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
LOCALE_PATTERN = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8}){0,2}$")
MESSAGE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")
MAX_MESSAGE_CHARS = 2000
MAX_SCHEMA_PROBLEMS = 20
DICTIONARY_SUFFIXES = (".yaml", ".yml")
SOURCES = ("process", "tasks", "knowledge")
AGGREGATE_BLOCKS = frozenset({"metrics", "chart"})
# Вызов агрегата в выражении; метод (``x.max(``) — не вызов, имя в строковом литерале — тоже
# (ядро: views._AGGREGATE_CALL, _STRING_LITERAL).
_AGGREGATE_CALL = re.compile(r"(?<![\w.])(count|sum|avg|min|max)\s*\(")
_STRING_LITERAL = re.compile(
    r"(?<![\w.])(?:[rR][bB]?|[bB][rR]?)?"
    r'(?:"""(?:\\.|[^\\])*?"""'
    r"|'''(?:\\.|[^\\])*?'''"
    r'|"(?:\\.|[^"\\\n])*"'
    r"|'(?:\\.|[^'\\\n])*')",
    re.DOTALL,
)
# Поле источника или параметр, которые читает выражение: data.a.b, param.x.y.
_READ_PATH = re.compile(r"(?<![\w.])(data|param)\.([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)")
_COLUMN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,199}$")

CORE_SCREENS_MISSING = (
    "screens: data paths, CEL expressions, formats and default labels of views are not checked "
    "here — the control-plane code next to the SDK predates CP-ADR-0080 "
    "(control_plane.domain.views); the core checks them in plan"
)


@dataclass(frozen=True)
class Finding:
    file: str
    code: str
    message: str
    path: str = ""
    warning: bool = False

    def __str__(self) -> str:
        return f"{self.file}: {self.code}: {self.message}" + (
            f" [{self.path}]" if self.path else ""
        )


def pointer(*parts: Any) -> str:
    """JSON Pointer частей пути (``~`` и ``/`` экранируются)."""
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def calls_aggregate(text: str) -> bool:
    return _AGGREGATE_CALL.search(_STRING_LITERAL.sub('""', text)) is not None


def message_syntax(text: str) -> str | None:
    """Что не так с подстановками ICU текста; None — скобки аргументов парные (``'`` экранирует,
    как в ICU). Форматирует текст консоль — здесь, как у ядра, только парность."""
    depth = 0
    quoted = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == "'":
            if index + 1 < len(text) and text[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif not quoted and char == "{":
            depth += 1
        elif not quoted and char == "}":
            depth -= 1
            if depth < 0:
                return "a '}' closes no argument"
        index += 1
    if depth:
        return "an argument '{' is not closed"
    return None


# --- словари --------------------------------------------------------------------------------


@dataclass
class Dictionary:
    locale: str
    file: str
    messages: dict[str, str]


def dictionaries(package: Package) -> tuple[dict[str, Dictionary], list[Finding]]:
    """Словари ``i18n/<locale>.yaml`` пакета и находки в них — те же, что у разбора ядра
    (``invalid_dictionary``, ``invalid_message``)."""
    found: dict[str, Dictionary] = {}
    findings: list[Finding] = []
    root = package.path / I18N_DIR
    if not root.is_dir():
        return found, findings
    for path in sorted(root.rglob("*")):
        inner = path.relative_to(package.path)
        if (
            not path.is_file()
            or path.suffix not in DICTIONARY_SUFFIXES
            or any(part.startswith(".") for part in inner.parts)
        ):
            continue
        where = _rel(path)
        locale = path.name.rsplit(".", 1)[0]
        if len(inner.parts) != 2 or not LOCALE_PATTERN.match(locale):
            findings.append(
                Finding(
                    where,
                    "invalid_dictionary",
                    f"a dictionary is {I18N_DIR}/<locale>.yaml with a locale such as en or "
                    f"pt-BR, not {inner.as_posix()}",
                )
            )
            continue
        if locale in found:
            findings.append(
                Finding(
                    where, "invalid_dictionary", f"locale {locale} also has {found[locale].file}"
                )
            )
            continue
        try:
            document = _read_yaml(path)
        except PackageError as error:
            findings.append(Finding(where, "invalid_dictionary", str(error)))
            continue
        if document is None:
            document = {}
        if not isinstance(document, dict):
            findings.append(
                Finding(where, "invalid_dictionary", "a dictionary is a mapping key -> text")
            )
            continue
        messages: dict[str, str] = {}
        for key, text in document.items():
            problem: str | None
            if not isinstance(key, str) or not MESSAGE_KEY_PATTERN.match(key):
                problem = f"key {str(key)[:80]!r} does not match {MESSAGE_KEY_PATTERN.pattern}"
            elif not isinstance(text, str):
                problem = f"the text of {key} is a string, not {type(text).__name__}"
            elif len(text) > MAX_MESSAGE_CHARS:
                problem = f"the text of {key} is longer than {MAX_MESSAGE_CHARS} characters"
            else:
                syntax = message_syntax(text)
                problem = None if syntax is None else f"the text of {key}: {syntax}"
            if problem is None:
                messages[str(key)] = str(text)
            else:
                findings.append(Finding(where, "invalid_message", problem, pointer(key)))
        found[locale] = Dictionary(locale, where, messages)
    return found, findings


def check_locales(package: Package, found: Mapping[str, Dictionary]) -> list[Finding]:
    """``locales`` и ``defaultLocale`` манифеста против словарей (ядро: ``check_locales``)."""
    manifest = _rel(package.path / "package.yaml")
    screens = any(o.kind in (VIEW, COMPONENT) for o in package.objects)
    raw = package.spec.get(LOCALES_FIELD)
    default = package.spec.get(DEFAULT_LOCALE_FIELD)
    findings: list[Finding] = []
    if raw is None and default is None:
        if screens or found:
            findings.append(
                Finding(
                    manifest,
                    "locales_required",
                    "the package has screens or dictionaries: package.yaml declares locales "
                    "and defaultLocale (locales: [en, ru], defaultLocale: en)",
                    "/spec",
                )
            )
        return findings
    if not isinstance(raw, list) or not raw:
        return [
            Finding(
                manifest,
                "invalid_locales",
                "locales is a non-empty list of locales",
                "/spec/locales",
            )
        ]
    seen: set[str] = set()
    for index, locale in enumerate(raw):
        where = pointer("spec", "locales", index)
        if not isinstance(locale, str) or not LOCALE_PATTERN.match(locale):
            findings.append(
                Finding(
                    manifest,
                    "invalid_locales",
                    f"{locale!r} is not a locale such as en or pt-BR",
                    where,
                )
            )
        elif locale in seen:
            findings.append(
                Finding(manifest, "invalid_locales", f"locale {locale} is declared twice", where)
            )
        else:
            seen.add(locale)
            if locale not in found:
                findings.append(
                    Finding(
                        manifest,
                        "missing_dictionary",
                        f"locale {locale} has no dictionary {I18N_DIR}/{locale}.yaml",
                        where,
                    )
                )
    if not isinstance(default, str) or default not in seen:
        findings.append(
            Finding(
                manifest,
                "invalid_default_locale",
                f"defaultLocale {default!r} is not one of the declared locales",
                "/spec/defaultLocale",
            )
        )
    for locale, dictionary in sorted(found.items()):
        if locale not in seen:
            findings.append(
                Finding(
                    dictionary.file,
                    "undeclared_locale",
                    f"locale {locale} is not declared in package.yaml — add it to locales or "
                    "remove the dictionary",
                )
            )
    return findings


def declared_locales(package: Package) -> tuple[str, ...]:
    raw = package.spec.get(LOCALES_FIELD)
    if not isinstance(raw, list):
        return ()
    return tuple(dict.fromkeys(x for x in raw if isinstance(x, str) and LOCALE_PATTERN.match(x)))


# --- форма ---------------------------------------------------------------------------------


def _deepest(error: jsonschema.ValidationError) -> jsonschema.ValidationError:
    if not error.context:
        return error
    best = jsonschema.exceptions.best_match(error.context)
    return _deepest(best) if len(best.absolute_path) >= len(error.absolute_path) else error


def shape_findings(obj: Obj) -> list[Finding]:
    """Форма spec по схеме ядра: ``code`` — ``component_code_not_supported``, блок и формат не
    из набора версии 1 — ``unknown_block``/``unknown_format``, прочее — ``invalid_view`` или
    ``invalid_component`` (ядро: ``_no_code``, ``_shape_problems``)."""
    where = _rel(obj.path)
    noun = "view" if obj.kind == VIEW else "component"
    findings: list[Finding] = []
    if "code" in obj.spec:
        findings.append(
            Finding(
                where,
                "component_code_not_supported",
                f"a {obj.kind} is a description: components with code are not supported — "
                "describe the screen with the blocks of the set",
                "/spec/code",
            )
        )
    spec = {k: v for k, v in obj.spec.items() if k != "code"}
    errors = sorted(
        schema_module.screen_validator(obj.kind).iter_errors(spec),
        key=lambda e: [str(p) for p in e.absolute_path],
    )
    for error in errors[:MAX_SCHEMA_PROBLEMS]:
        cause = _deepest(error)
        parts = list(cause.absolute_path)
        path = pointer("spec", *parts)
        last = parts[-1] if parts else None
        if cause.validator == "enum" and last == "block" and "layout" in parts:
            findings.append(
                Finding(
                    where,
                    "unknown_block",
                    f"block {cause.instance!r} is not in the set of version 1 "
                    f"({', '.join(cause.validator_value)})",
                    path,
                )
            )
        elif cause.validator == "enum" and last == "format":
            findings.append(
                Finding(
                    where,
                    "unknown_format",
                    f"format {cause.instance!r} is not in the set of version 1 "
                    f"({', '.join(cause.validator_value)})",
                    path,
                )
            )
        elif cause.validator == "enum" and parts == ["nav", "group"]:
            findings.append(
                Finding(
                    where,
                    f"invalid_{noun}",
                    f"nav group {cause.instance!r} is not a group of the console menu "
                    f"({', '.join(cause.validator_value)})",
                    path,
                )
            )
        else:
            findings.append(Finding(where, f"invalid_{noun}", cause.message[:500], path))
    return findings


# --- строки -------------------------------------------------------------------------------


def message_places(spec: Mapping[str, Any], base: str = "/spec") -> Iterator[tuple[str, str]]:
    """(путь, ключ) каждого ключа словаря, который написан в spec (ядро: ``message_places``)."""
    for name in ("title", "description"):
        if isinstance(spec.get(name), str):
            yield f"{base}/{name}", spec[name]
    layout = spec.get("layout")
    if isinstance(layout, list):
        for index, block in enumerate(layout):
            if isinstance(block, Mapping):
                yield from _block_messages(block, f"{base}/layout/{index}")


def _block_messages(block: Mapping[str, Any], at: str) -> Iterator[tuple[str, str]]:
    kind = block.get("block")
    # title заголовка — путь источника, а не ключ
    names = ("section", "label") if kind == "header" else ("title", "section", "label")
    for name in names:
        if isinstance(block.get(name), str):
            yield f"{at}/{name}", block[name]
    groups: list[tuple[str, Any, str]] = [
        (f"{at}/columns", block.get("columns"), "label"),
        (f"{at}/items", block.get("items"), "title" if kind == "metrics" else "label"),
    ]
    card = block.get("card")
    if isinstance(card, Mapping):
        groups.append((f"{at}/card/fields", card.get("fields"), "label"))
    for where, items, name in groups:
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if isinstance(item, Mapping) and isinstance(item.get(name), str):
                yield f"{where}/{index}/{name}", item[name]


def _field_name(path: str, process: bool) -> str | None:
    """Имя поля источника в ключах: у процесса ``data.a.b`` — ``a.b``."""
    name = path.removeprefix("data.") if process else path
    return name if _COLUMN_KEY.match(name) else None


def default_label_places(spec: Mapping[str, Any], process: bool) -> Iterator[tuple[str, str, bool]]:
    """(путь, имя поля, обязательна ли подпись) подписей по умолчанию, которые видны без CEL:
    колонки ``field`` без ``label`` у table/list и fields, фильтры и сортировка. Подписи
    колонок ``value`` выводит из выражения ядро."""
    layout = spec.get("layout")
    for index, block in enumerate(layout if isinstance(layout, list) else ()):
        if not isinstance(block, Mapping):
            continue
        at = f"/spec/layout/{index}"
        kind = block.get("block")
        lists = []
        if kind in ("table", "list"):
            lists.append((f"{at}/columns", block.get("columns"), True))
        if kind == "fields":
            lists.append((f"{at}/items", block.get("items"), True))
        card = block.get("card")
        if kind == "board" and isinstance(card, Mapping):
            lists.append((f"{at}/card/fields", card.get("fields"), False))
        for where, items, required in lists:
            for number, column in enumerate(items if isinstance(items, list) else ()):
                if (
                    isinstance(column, Mapping)
                    and "label" not in column
                    and isinstance(column.get("field"), str)
                ):
                    name = _field_name(column["field"], process)
                    if name is not None:
                        yield f"{where}/{number}/label", name, required
        for number, path in enumerate(block.get("filters") or ()):
            if kind in ("table", "list", "board") and isinstance(path, str):
                yield f"{at}/filters/{number}", _field_name(path, process) or path, True
        for number, order in enumerate(block.get("sort") or ()):
            if kind in ("table", "list") and isinstance(order, Mapping):
                path = order.get("field")
                if isinstance(path, str):
                    yield f"{at}/sort/{number}/field", _field_name(path, process) or path, True


# --- выражения и ссылки --------------------------------------------------------------------


def _expressions(block: Mapping[str, Any], at: str) -> Iterator[tuple[str, str]]:
    """(путь, текст) выражений CEL блока, кроме значений metrics и chart."""
    kind = block.get("block")
    groups = [(f"{at}/columns", block.get("columns"))]
    if kind != "metrics":
        groups.append((f"{at}/items", block.get("items")))
    card = block.get("card")
    if isinstance(card, Mapping):
        groups.append((f"{at}/card/fields", card.get("fields")))
    for where, items in groups:
        for index, column in enumerate(items if isinstance(items, list) else ()):
            if isinstance(column, Mapping) and isinstance(column.get("value"), str):
                yield f"{where}/{index}/value", column["value"]
    target = block.get("open")
    if isinstance(target, Mapping):
        if isinstance(target.get("id"), str):
            yield f"{at}/open/id", target["id"]
        for name, text in sorted((target.get("params") or {}).items()):
            if isinstance(text, str):
                yield f"{at}/open/params/{name}", text
    for field_name in ("with", "input"):
        for name, text in sorted((block.get(field_name) or {}).items()):
            if isinstance(text, str):
                yield f"{at}/{field_name}/{name}", text
    knowledge = block.get("knowledge")
    if isinstance(knowledge, Mapping) and isinstance(knowledge.get("key"), str):
        yield f"{at}/knowledge/key", knowledge["key"]


def _all_expressions(spec: Mapping[str, Any]) -> Iterator[str]:
    """Тексты всех выражений spec, значения metrics и chart тоже."""
    source = spec.get("source")
    if isinstance(source, Mapping) and isinstance(source.get("filter"), str):
        yield source["filter"]
    layout = spec.get("layout")
    for index, block in enumerate(layout if isinstance(layout, list) else ()):
        if not isinstance(block, Mapping):
            continue
        for _, text in _expressions(block, f"/spec/layout/{index}"):
            yield text
        if block.get("block") == "metrics":
            for item in block.get("items") or ():
                if isinstance(item, Mapping) and isinstance(item.get("value"), str):
                    yield item["value"]
        if block.get("block") == "chart" and isinstance(block.get("value"), str):
            yield block["value"]


@dataclass(frozen=True)
class Scope:
    """Что видит пакет: его объекты и объекты его ``requires`` (TAI-ADR-0044 п.3)."""

    package: Package
    processes: frozenset[str]
    task_types: frozenset[str]
    roles: frozenset[str]
    skills: frozenset[str]
    views: frozenset[str]
    components: frozenset[str]


def scope(installation: Installation, package: Package) -> Scope:
    visible = installation.visible(package.key)

    def keys(kind: str) -> frozenset[str]:
        return frozenset(o.key for o in visible if o.kind == kind)

    return Scope(
        package=package,
        processes=keys("Process"),
        task_types=keys("TaskType") | {SYSTEM_TASK_TYPE},
        roles=keys("Role"),
        skills=frozenset(f"{o.key}@{o.spec.get('version')}" for o in visible if o.kind == "Skill"),
        views=keys(VIEW),
        components=frozenset(o.key for o in package.objects if o.kind == COMPONENT),
    )


def _layout_findings(obj: Obj, where: str, seen: Scope) -> list[Finding]:
    """Ссылки и агрегаты блоков вида или компонента."""
    findings: list[Finding] = []
    for index, block in enumerate(obj.spec.get("layout") or ()):
        at = f"/spec/layout/{index}"
        kind = block.get("block")
        if kind not in AGGREGATE_BLOCKS:
            for path, text in _expressions(block, at):
                if calls_aggregate(text):
                    findings.append(
                        Finding(
                            where,
                            "aggregate_outside_metrics",
                            "count, sum, avg, min and max are written only in metrics and chart",
                            path,
                        )
                    )
        target = block.get("open")
        if isinstance(target, Mapping) and target.get("view") not in seen.views:
            findings.append(
                Finding(
                    where,
                    "unknown_view",
                    f"there is no view {target.get('view')!r} in package {obj.package} or "
                    "in a package it requires",
                    f"{at}/open/view",
                )
            )
        if kind == "component":
            if obj.kind == COMPONENT:
                findings.append(
                    Finding(
                        where,
                        "nested_component",
                        "a component is not made of components: inline its blocks",
                        at,
                    )
                )
            elif block.get("component") not in seen.components:
                findings.append(
                    Finding(
                        where,
                        "unknown_component",
                        f"package {obj.package} has no component {block.get('component')!r}",
                        f"{at}/component",
                    )
                )
        if kind == "invoke" and block.get("skill") not in seen.skills:
            findings.append(
                Finding(
                    where,
                    "unknown_skill",
                    f"skill {block.get('skill')!r} — no such Skill (name@version) in package "
                    f"{obj.package} and its requires",
                    f"{at}/skill",
                )
            )
    return findings


def _source_findings(obj: Obj, where: str, seen: Scope) -> list[Finding]:
    source = obj.spec["source"]
    named = [name for name in SOURCES if name in source]
    if len(named) != 1:
        return [
            Finding(
                where,
                "invalid_source",
                "the source is exactly one of process, tasks or knowledge",
                "/spec/source",
            )
        ]
    findings: list[Finding] = []
    if named[0] != "process" and ("filter" in source or "instance" in source):
        findings.append(
            Finding(
                where,
                "invalid_source",
                "filter and instance belong to a process source",
                "/spec/source",
            )
        )
    if named[0] == "process":
        if source["process"] not in seen.processes:
            findings.append(
                Finding(
                    where,
                    "unknown_source",
                    f"there is no process {source['process']!r} in package {obj.package} or "
                    "its requires",
                    "/spec/source/process",
                )
            )
        if "instance" in source and "filter" in source:
            findings.append(
                Finding(
                    where,
                    "invalid_source",
                    "a view of one instance has no filter",
                    "/spec/source/filter",
                )
            )
    if named[0] == "tasks" and source["tasks"]["type"] not in seen.task_types:
        findings.append(
            Finding(
                where,
                "unknown_source",
                f"there is no task type {source['tasks']['type']!r} in package {obj.package} "
                "or its requires",
                "/spec/source/tasks/type",
            )
        )
    return findings


def _param_schema_findings(obj: Obj, where: str, package_dir: Path) -> list[Finding]:
    """``params.<name>.schema: {$ref: <файл>#<указатель>}`` компонента ведёт к объекту JSON
    Schema файла пакета (ядро: ``unresolved_schema_ref``; ссылка внутрь документа или во внешний
    мир — ``invalid_component``)."""
    findings: list[Finding] = []
    for name, param in sorted((obj.spec.get("params") or {}).items()):
        schema = param.get("schema") if isinstance(param, Mapping) else None
        ref = (
            schema.get("$ref") if isinstance(schema, Mapping) and set(schema) == {"$ref"} else None
        )
        if not isinstance(ref, str):
            continue
        at = f"/spec/params/{name}/schema/$ref"
        if ref.startswith("#") or "://" in ref:
            findings.append(
                Finding(
                    where,
                    "invalid_component",
                    "the schema of a param is written inline or names a schema file of the "
                    "package: <file>#<pointer>",
                    at,
                )
            )
            continue
        file, _, fragment = ref.partition("#")
        inner = obj.path.relative_to(package_dir).as_posix()
        target = posixpath.normpath(posixpath.join(posixpath.dirname(inner), file))
        problem: str | None = None
        if target.startswith("../") or target == ".." or target.startswith("/"):
            problem = f"$ref {ref!r} leads outside the package"
        elif not (package_dir / target).is_file():
            problem = f"$ref {ref!r}: the package has no file {target}"
        else:
            path = package_dir / target
            try:
                node: Any = (
                    json.loads(path.read_text(encoding="utf-8"))
                    if path.suffix == ".json"
                    else _read_yaml(path)
                )
            except (PackageError, ValueError) as error:
                node = None
                problem = f"$ref {ref!r}: {target} is not YAML or JSON: {error}"
            for part in [p for p in fragment.split("/") if p] if problem is None else ():
                part = part.replace("~1", "/").replace("~0", "~")
                node = node.get(part) if isinstance(node, dict) else None
            if problem is None and not isinstance(node, dict):
                problem = f"$ref {ref!r}: {target} has no JSON Schema object at {fragment or '/'}"
        if problem is not None:
            findings.append(Finding(where, "unresolved_schema_ref", problem, at))
    return findings


def _used_keys(spec: Mapping[str, Any], prefix: str, process: bool) -> tuple[set[str], set[str]]:
    """Ключи словаря, которые показывает spec: написанные и подписи по умолчанию путей и
    полей, которые читают его выражения (точно их считает ядро — по CEL); второе — подписи
    фильтров: у них бывают подписи значений ``<подпись>.<значение>``."""
    used = {key for _, key in message_places(spec)}
    names = {name for _, name, _ in default_label_places(spec, process)}
    for text in _all_expressions(spec):
        names |= {match.group(2) for match in _READ_PATH.finditer(text)}
    used |= {f"{prefix}fields.{name}" for name in names}
    filters = {
        f"{prefix}fields.{name}"
        for path, name, _ in default_label_places(spec, process)
        if "/filters/" in path
    }
    return used, filters


def _static(installation: Installation, package: Package) -> tuple[list[Finding], list[Finding]]:
    """Ошибки и предупреждения экранов пакета без кода ядра."""
    found, errors = dictionaries(package)
    errors += check_locales(package, found)
    warnings: list[Finding] = []
    locales = declared_locales(package)
    seen = scope(installation, package)
    prefix = f"{package.key}."
    # подписи формы настроек — тоже показанные ключи (CP-ADR-0081 п.1)
    used: set[str] = _settings_keys(package)
    filters: set[str] = set()
    for obj in sorted(
        (o for o in package.objects if o.kind in (VIEW, COMPONENT)), key=lambda o: (o.kind, o.key)
    ):
        where = _rel(obj.path)
        shape = shape_findings(obj)
        errors += shape
        if shape:
            continue
        source = obj.spec.get("source") or {}
        process = obj.kind == COMPONENT or "process" in source
        shown, filtered = _used_keys(obj.spec, prefix, process)
        used |= shown
        filters |= filtered
        if obj.kind == VIEW:
            errors += _source_findings(obj, where, seen)
            for index, role in enumerate((obj.spec.get("audience") or {}).get("roles") or ()):
                if role not in seen.roles:
                    warnings.append(
                        Finding(
                            where,
                            "unknown_role",
                            f"role {role!r} is not declared in package {package.key} and its "
                            "requires — it must already exist in the tenant, otherwise the core "
                            "refuses with unknown_role",
                            f"/spec/audience/roles/{index}",
                            warning=True,
                        )
                    )
        else:
            errors += _param_schema_findings(obj, where, package.path)
        errors += _layout_findings(obj, where, seen)
        places = [(path, key) for path, key in message_places(obj.spec)]
        if obj.kind == VIEW:
            places += [
                (path, f"{prefix}fields.{name}")
                for path, name, required in default_label_places(obj.spec, process)
                if required
            ]
        for path, key in places:
            missing = [loc for loc in locales if loc in found and key not in found[loc].messages]
            if missing:
                errors.append(
                    Finding(
                        where,
                        "missing_message",
                        f"{key} is not in the dictionary of {', '.join(missing)} "
                        f"({' '.join(f'{I18N_DIR}/{loc}.yaml' for loc in missing)})",
                        path,
                    )
                )
    for _, dictionary in sorted(found.items()):
        for key in sorted(dictionary.messages):
            if key not in used and not any(key.startswith(f + ".") for f in filters):
                warnings.append(
                    Finding(
                        dictionary.file,
                        "unused_message",
                        f"no view or component of the package shows {key}",
                        pointer(key),
                        warning=True,
                    )
                )
    return errors, warnings


# --- проверка ядром -----------------------------------------------------------------------


def core_views() -> Any:
    """Модули ядра, которые проверяют экраны (CP-ADR-0080); None — ядро их не знает."""
    try:
        from control_plane.domain import package_source, views
        from control_plane.domain.process_definition import package_skill
    except ImportError:
        return None

    return SimpleNamespace(views=views, package_source=package_source, package_skill=package_skill)


def _core(
    installation: Installation, package: Package, env: Mapping[str, str], core: Any
) -> tuple[list[Finding], list[Finding]]:
    """Экраны пакета проверкой ядра: разбор файлов пакета (словари, $ref схем параметров),
    ``check_locales``, ``check_component``, ``check_view``, ``unused_messages`` — в контексте
    пакета и его ``requires``, как ``check_package`` ядра в контексте каталога tenant'а."""
    views = core.views

    def parse(owner: Package) -> Any:
        files = package_files(owner, dict(env), strict=False)
        return core.package_source.parse_package((f["path"], f["content"]) for f in files)

    parsed = parse(package)
    others = [parse(p) for p in installation.required(package.key) if p.key != package.key]
    objects = list(parsed.objects) + [o for other in others for o in other.objects]
    screens_files = {o.file for o in parsed.objects if o.kind in (VIEW, COMPONENT)}
    # находки разбора — у файлов экранов и словарей (и у отвергнутых: invalid_dictionary)
    problems = [
        p
        for p in parsed.problems
        if p.file in screens_files or str(p.file or "").startswith(f"{I18N_DIR}/")
    ]
    problems += views.check_locales(parsed)
    on_screens = parsed.of_kind(VIEW) or parsed.of_kind(COMPONENT) or parsed.dictionaries
    seen = scope(installation, package)
    # Роль аудитории, которой нет в пакете и его requires, может быть у tenant'а: это
    # предупреждение (как identity.roles агента), а не отказ ядра unknown_role.
    audience = {
        role
        for obj in parsed.of_kind(VIEW)
        for role in ((obj.spec.get("audience") or {}).get("roles") or ())
        if isinstance(role, str)
    }
    if on_screens:
        locales, default = views.declared_locales(parsed)
        processes = {o.key: _process_shape(views, o.spec) for o in objects if o.kind == "Process"}
        context = views.ViewContext(
            locales=locales,
            default_locale=default,
            dictionaries={loc: d.messages for loc, d in parsed.dictionaries.items()},
            processes=processes,
            task_types=seen.task_types,
            roles=seen.roles | audience,
            skills={
                f"{o.key}@{o.spec.get('version')}": core.package_skill(o.spec)
                for o in objects
                if o.kind == "Skill"
            },
            views=seen.views,
            component_keys=frozenset(o.key for o in parsed.of_kind(COMPONENT)),
            package=parsed.manifest_object.key if parsed.manifest_object else package.key,
        )
        settings_scope = _settings_scope(views, package)
        if settings_scope is not None:
            context = replace(context, settings=settings_scope)
        usable: dict[str, Any] = {}
        for obj in sorted(parsed.of_kind(COMPONENT), key=lambda o: o.key):
            found = views.check_component(obj, context)
            problems += found
            if not any(p.error for p in found):
                usable[obj.key] = obj
        context = replace(context, components=usable)
        used: set[str] = _settings_keys(package)
        for obj in sorted(parsed.of_kind(VIEW), key=lambda o: o.key):
            checked = views.check_view(obj, context)
            problems += checked.problems
            used |= checked.messages
        for obj in parsed.of_kind(COMPONENT):
            used |= {key for _, key in views.message_places(obj.spec)}
        problems += views.unused_messages(parsed, used)
    errors: list[Finding] = []
    warnings: list[Finding] = []
    for problem in problems:
        file = _rel(package.path / problem.file) if problem.file else _rel(package.path)
        message = problem.message + (f" (hint: {problem.hint})" if problem.hint else "")
        finding = Finding(file, problem.code, message, problem.path or "", not problem.error)
        (warnings if finding.warning else errors).append(finding)
    for obj in parsed.of_kind(VIEW):
        for index, role in enumerate((obj.spec.get("audience") or {}).get("roles") or ()):
            if role not in seen.roles:
                warnings.append(
                    Finding(
                        _rel(package.path / obj.file),
                        "unknown_role",
                        f"role {role!r} is not declared in package {package.key} and its "
                        "requires — it must already exist in the tenant, otherwise the core "
                        "refuses with unknown_role",
                        f"/spec/audience/roles/{index}",
                        warning=True,
                    )
                )
    return errors, warnings


def _settings_keys(package: Package) -> set[str]:
    from package_sdk import settings

    return settings.shown_keys(package)


def settings_aware(views: Any) -> bool:
    """Проверка видов ядра типизирует ``settings`` (CP-ADR-0081 §6): ``ViewContext.settings``."""
    fields = getattr(views.ViewContext, "__dataclass_fields__", {})
    return "settings" in fields


def _settings_scope(views: Any, package: Package) -> Any:
    """``SettingsScope`` пакета для ``ViewContext``; None — ядро настроек не знает."""
    if not settings_aware(views):
        return None
    from package_sdk import settings

    core = settings.core_settings()
    return settings.scope_of(core, package) if core is not None else None


def _process_shape(views: Any, spec: Mapping[str, Any]) -> Any:
    """Что вид читает у процесса: схему его данных и этапы (ядро: ``_process_shape``)."""
    data = spec.get("data")
    stages = tuple(
        str(stage["id"])
        for stage in spec.get("stages") or ()
        if isinstance(stage, Mapping) and isinstance(stage.get("id"), str)
    )
    return views.ProcessShape(data if isinstance(data, Mapping) else None, stages)


def has_screens(package: Package) -> bool:
    return (
        any(o.kind in (VIEW, COMPONENT) for o in package.objects)
        or (package.path / I18N_DIR).is_dir()
        or LOCALES_FIELD in package.spec
        or DEFAULT_LOCALE_FIELD in package.spec
    )


def check_screens(
    installation: Installation, env: Mapping[str, str] | None = None, *, core: Any = None
) -> tuple[list[str], list[str]]:
    """Ошибки и предупреждения экранов всех пакетов установки. core — модули ядра
    (``core_views()``); None — статика и предупреждение, что пути и выражения проверит ядро."""
    errors: list[str] = []
    warnings: list[str] = []
    screened = [p for p in installation.packages if has_screens(p)]
    for package in screened:
        if core is not None:
            found, warned = _core(installation, package, env or {}, core)
        else:
            found, warned = _static(installation, package)
        errors += [str(f) for f in found]
        warnings += [str(f) for f in warned]
    if screened and core is None and any(o.kind == VIEW for p in screened for o in p.objects):
        warnings.append(CORE_SCREENS_MISSING)
    return errors, warnings
