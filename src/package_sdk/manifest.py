"""Манифест пакета (TAI-ADR-0062 п.4): переменные установки, совместимость, требования к
пакетам, онтологии; предпосылки установки (``describe``) и сгенерированные разделы README
(``docs``).

Правила манифеста — часть ``check``. Пакет без поля ``variables`` — пакет прежней формы:
необъявленные переменные у него только предупреждение (режим перехода, plan Р3).
"""

from __future__ import annotations

import functools
import re
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from package_sdk.model import (
    ENV_REF,
    PLAN_KINDS,
    Installation,
    Obj,
    Package,
    PackageError,
    _rel,
    load_package,
    resolve,
)

# Онтологии, которые приносит сама память, а не пакет каталога (memory-service,
# встроенный пакет default): их виды и связи отсюда не видны.
PLATFORM_ONTOLOGIES = frozenset({"default"})

# Вид узла дела по умолчанию у проекции процесса (memory.case.kind, TAI-ADR-0054 Р15).
DEFAULT_CASE_KIND = "case"

# Где процесс обращается к памяти: проекция дела, запрос, запись, контекст шага.
_MEMORY_KEYS = ("memory", "recall", "remember", "context")
# Поля внутри обращений к памяти, которые не называют ни видов, ни связей.
_OPAQUE_KEYS = frozenset(
    {"facts", "where", "key", "name", "title", "text", "query", "onTimeout", "retrospective"}
)
_KIND_FIELDS = ("kind",)
# `rel` сущности дела (memory.entities) — предикат факта о деле, а не связь онтологии:
# память его не сверяет с включёнными онтологиями
_RELATION_FIELDS = ("relation", "via")


# --- SemVer и диапазоны -------------------------------------------------------

_VERSION = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
# Условие диапазона — та же грамматика, что semverRange схемы: операторы >=, >, <=, <, =,
# ^, ~ или без оператора; версия до трёх частей с пре-релизом, без метаданных сборки.
_CONDITION = re.compile(r"^(>=|<=|>|<|=|\^|~)?\s*(\d+(?:\.\d+){0,2}(?:-[0-9A-Za-z.-]+)?)$")


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    # Предварительная версия младше выпуска: пустой кортеж сортируется последним
    pre: tuple[tuple[int, int | str], ...] = ()

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        if (self.major, self.minor, self.patch) != (other.major, other.minor, other.patch):
            return (self.major, self.minor, self.patch) < (other.major, other.minor, other.patch)
        if not self.pre or not other.pre:
            return bool(self.pre) and not other.pre
        return self.pre < other.pre

    def __le__(self, other: object) -> bool:
        return self == other or self.__lt__(other)

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return other.__lt__(self)

    def __ge__(self, other: object) -> bool:
        return self == other or self.__gt__(other)


def _pre(text: str | None) -> tuple[tuple[int, int | str], ...]:
    if not text:
        return ()
    # Числовые части сравниваются как числа и младше буквенных (SemVer 2.0 §11)
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in text.split("."))


def parse_version(text: str) -> tuple[Version, int]:
    """Версия и число указанных частей (1.2 → 2): частичная версия в диапазоне — префикс."""
    match = _VERSION.match(text.strip())
    if not match:
        raise PackageError(f"версия {text!r} — не SemVer (major.minor.patch)")
    major, minor, patch, pre = match.groups()
    given = 1 + (minor is not None) + (patch is not None)
    return Version(int(major), int(minor or 0), int(patch or 0), _pre(pre)), given


def _bump(version: Version, part: int) -> Version:
    if part == 0:
        return Version(version.major + 1, 0, 0)
    if part == 1:
        return Version(version.major, version.minor + 1, 0)
    return Version(version.major, version.minor, version.patch + 1)


def _condition(text: str) -> list[tuple[str, Version]]:
    """Одно условие диапазона → пары (оператор, граница)."""
    text = text.strip()
    if text == "*":
        return []
    match = _CONDITION.match(text)
    if not match:
        raise PackageError(f"условие {text!r} — не диапазон версий")
    op, value = match.group(1) or "", match.group(2)
    version, given = parse_version(value)
    if op == "^":
        # ^1.2.3 → <2.0.0; ^0.2.3 → <0.3.0; ^0.0.3 → <0.0.4 (первая ненулевая часть)
        part = 0 if version.major or given == 1 else 1 if version.minor or given == 2 else 2
        return [(">=", version), ("<", _bump(version, part))]
    if op == "~":
        # ~1.2.3 → <1.3.0; ~1 → <2.0.0
        return [(">=", version), ("<", _bump(version, 0 if given == 1 else 1))]
    if given < 3:
        # частичная версия — префикс, как в npm: 1.2 = >=1.2.0,<1.3.0; >1.2 = >=1.3.0;
        # <=1.2 = <1.3.0; <1.2 = <1.2.0; >=1.2 = >=1.2.0
        upper = _bump(version, given - 1)
        if op in ("", "="):
            return [(">=", version), ("<", upper)]
        if op == ">":
            return [(">=", upper)]
        if op == "<=":
            return [("<", upper)]
    return [("==" if op in ("", "=") else op, version)]


def satisfies(version: str, spec: str) -> bool:
    """Версия пакета в диапазоне: условия через запятую, все выполняются (``>=0.9,<0.11``).

    Частичные версии — префиксы, как в npm. Пре-релизы сравниваются по старшинству SemVer
    2.0 (1.0.0-rc.1 < 1.0.0) и, в отличие от npm, не исключаются из диапазонов без
    пре-релиза: пакеты каталога выпускают релизными версиями."""
    current, _ = parse_version(version)
    for part in spec.split(","):
        for op, bound in _condition(part):
            ok = {
                ">=": current >= bound,
                "<=": current <= bound,
                ">": current > bound,
                "<": current < bound,
                "==": current == bound,
            }[op]
            if not ok:
                return False
    return True


# --- переменные ---------------------------------------------------------------


def variable_uses(package: Package) -> dict[str, list[Obj]]:
    """Имя ${ПЕРЕМЕННОЙ} → объекты пакета, в строках которых она встречается."""
    uses: dict[str, list[Obj]] = {}

    def walk(value: Any, obj: Obj) -> None:
        if isinstance(value, str):
            for name in ENV_REF.findall(value):
                holders = uses.setdefault(name, [])
                if obj not in holders:
                    holders.append(obj)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item, obj)
        elif isinstance(value, list):
            for item in value:
                walk(item, obj)

    for obj in package.objects:
        walk(obj.spec, obj)
    return uses


def variable_value_error(kind: str, value: str) -> str | None:
    """Почему значение не подходит виду переменной; None — подходит. Существование UUID на
    стенде проверяет plan, здесь — только форма."""
    if ENV_REF.search(value):
        return None
    if kind == "url":
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return "нужен абсолютный URL http(s)://…"
    elif kind in ("workspace", "project", "principal", "role"):
        try:
            uuid.UUID(value)
        except ValueError:
            return f"нужен UUID ({kind} стенда)"
    elif kind == "integer" and not re.fullmatch(r"-?\d+", value.strip()):
        return "нужно целое число"
    return None


def package_env(package: Package, env: dict[str, str]) -> dict[str, str]:
    """Окружение для объектов пакета: значения установки поверх default объявленных
    переменных; необязательная переменная без значения — пустая строка."""
    resolved: dict[str, str] = {}
    for name, declared in (package.spec.get("variables") or {}).items():
        if not isinstance(declared, dict):
            continue
        if "default" in declared:
            resolved[name] = str(declared["default"])
        elif declared.get("required") is False:
            resolved[name] = ""
    return {**resolved, **env}


def missing_variable(package: Package, name: str) -> str:
    """Текст ошибки о незаданной переменной — с её описанием из манифеста."""
    declared = (package.spec.get("variables") or {}).get(name)
    if isinstance(declared, dict) and declared.get("description"):
        example = f"; пример: {declared['example']}" if declared.get("example") else ""
        return (
            f"переменная {name} не задана — пакет {package.key}: {declared['description']} "
            f"({declared.get('kind', 'string')}{example})"
        )
    return f"переменная окружения {name} не задана (нужна пакету {package.key})"


def _variable_rules(package: Package) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    where = _rel(package.path / "package.yaml")
    uses = variable_uses(package)
    declared = package.spec.get("variables")
    if declared is None:
        # режим перехода закрыт (S014): пакеты платформы объявили переменные, и пакет
        # без spec.variables с ${…} — такая же ошибка, как необъявленная переменная
        if uses:
            errors.append(
                f"{where}: variable_undeclared: переменные не объявлены в spec.variables: "
                f"{', '.join(sorted(uses))}"
            )
        return errors, warnings
    for name in sorted(set(uses) - set(declared)):
        refs = ", ".join(sorted({o.ref for o in uses[name]}))
        errors.append(
            f"{where}: variable_undeclared: ${{{name}}} использована ({refs}), но не объявлена "
            "в spec.variables"
        )
    for name in sorted(set(declared) - set(uses)):
        errors.append(
            f"{where}: variable_unused: {name} объявлена в spec.variables, но ни один объект "
            "пакета её не использует"
        )
    for name, spec in sorted(declared.items()):
        if not isinstance(spec, dict):
            continue
        kind = str(spec.get("kind", "string"))
        for field_name in ("default", "example"):
            value = spec.get(field_name)
            if isinstance(value, str):
                problem = variable_value_error(kind, value)
                if problem:
                    errors.append(
                        f"{where}: variable_invalid_value: {name}.{field_name} {value!r} — "
                        + problem
                    )
        if spec.get("required") is True and "default" in spec:
            warnings.append(
                f"{where}: variable_required_with_default: {name}: required: true и default "
                "вместе — default делает переменную необязательной, уберите одно из двух"
            )
    return errors, warnings


# --- требования к пакетам -----------------------------------------------------


def _range_error(spec: str) -> str | None:
    """Почему диапазон не разбирается; None — разбирается."""
    try:
        for part in spec.split(","):
            _condition(part)
    except PackageError as error:
        return str(error)
    return None


def local_versions() -> dict[str, str]:
    """Версии компонентов платформы, код которых стоит рядом (extra sandbox)."""
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover
        return {}
    found: dict[str, str] = {}
    for component in ("control-plane",):
        try:
            found[component] = version(component)
        except PackageNotFoundError:
            continue
    return found


def _engines_rules(package: Package, local: dict[str, str]) -> tuple[list[str], list[str]]:
    """engines: диапазон разбирается; код ядра рядом (которым идут check и тесты) — в нём.
    Версию стенда сверяет plan."""
    errors: list[str] = []
    warnings: list[str] = []
    where = _rel(package.path / "package.yaml")
    for component, spec in (package.spec.get("engines") or {}).items():
        problem = _range_error(str(spec))
        if problem:
            errors.append(f"{where}: engines_invalid: {component} {spec!r}: {problem}")
            continue
        current = local.get(component)
        if current is not None and not satisfies(current, str(spec)):
            warnings.append(
                f"{where}: engines_mismatch: пакет объявляет {component} {spec}, а проверка "
                f"и тесты идут кодом {component} {current}"
            )
    return errors, warnings


def _requires_rules(package: Package, versions: dict[str, str]) -> list[str]:
    errors: list[str] = []
    where = _rel(package.path / "package.yaml")
    for key, spec in package.requirements:
        if spec is None:
            continue
        problem = _range_error(spec)
        if problem:
            errors.append(f"{where}: requires_version_invalid: {key} {spec!r}: {problem}")
            continue
        if key not in versions:
            continue
        ok = satisfies(versions[key], spec)
        if not ok:
            errors.append(
                f"{where}: requires_version_mismatch: нужен {key} {spec}, в установке {key} "
                f"{versions[key]}"
            )
    return errors


# --- онтологии ----------------------------------------------------------------


def _ontology_ref(value: str) -> tuple[str, int]:
    name, _, major = value.removeprefix("tenant:").partition("@")
    return name, int(major)


def _major(version: Any) -> int | None:
    try:
        return int(str(version).split(".")[0])
    except ValueError:
        return None


@dataclass
class KnowledgeUse:
    """Виды и связи памяти, к которым обращаются процессы пакета."""

    kinds: dict[str, list[str]] = field(default_factory=dict)
    relations: dict[str, list[str]] = field(default_factory=dict)

    def add(self, table: dict[str, list[str]], name: str, ref: str) -> None:
        holders = table.setdefault(name, [])
        if ref not in holders:
            holders.append(ref)

    def __bool__(self) -> bool:
        return bool(self.kinds or self.relations)


def knowledge_uses(package: Package) -> KnowledgeUse:
    """Виды и связи из memory, recall, remember и context процессов пакета."""
    use = KnowledgeUse()

    def collect(node: Any, ref: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _OPAQUE_KEYS:
                    continue
                if key in _KIND_FIELDS and isinstance(value, str):
                    use.add(use.kinds, value, ref)
                elif key == "kinds" and isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            use.add(use.kinds, item, ref)
                elif key in _RELATION_FIELDS and isinstance(value, str):
                    use.add(use.relations, value, ref)
                else:
                    collect(value, ref)
        elif isinstance(node, list):
            for item in node:
                collect(item, ref)

    def find(node: Any, ref: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _MEMORY_KEYS and isinstance(value, dict):
                    if key == "memory" and isinstance(value.get("case"), dict):
                        use.add(use.kinds, value["case"].get("kind") or DEFAULT_CASE_KIND, ref)
                    collect(value, ref)
                else:
                    find(value, ref)
        elif isinstance(node, list):
            for item in node:
                find(item, ref)

    for obj in package.objects:
        if obj.kind == "Process":
            find(obj.spec, obj.ref)
    return use


@functools.cache
def _platform_packs() -> dict[tuple[str, int], Any]:
    """Онтологии памяти платформы (default@1): снимок memory-service в пакете SDK —
    виды и связи проверяются и против них; сверку снимка с памятью держит
    contract-тест."""
    import json
    import types

    packs: dict[tuple[str, int], Any] = {}
    folder = Path(__file__).parent / "platform_ontologies"
    for path in sorted(folder.glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        major = _major(spec.get("version"))
        if major is not None:
            packs[(str(spec["name"]), major)] = types.SimpleNamespace(key=spec["name"], spec=spec)
    return packs


def _knowledge_packs(objects: list[Obj]) -> dict[tuple[str, int], Obj]:
    packs: dict[tuple[str, int], Obj] = dict(_platform_packs())
    for obj in objects:
        if obj.kind != "KnowledgePack":
            continue
        name, major = obj.spec.get("name") or obj.key, _major(obj.spec.get("version"))
        if major is not None:
            packs[(str(name), major)] = obj
    return packs


def _ontology_terms(
    ref: tuple[str, int], packs: dict[tuple[str, int], Obj], seen: set[tuple[str, int]]
) -> tuple[set[str], set[str]] | None:
    """Виды и связи онтологии с её extends; None — содержимое не видно (онтология памяти
    или extends вне пакетов)."""
    if ref in seen:
        return set(), set()
    seen.add(ref)
    obj = packs.get(ref)
    if obj is None:
        return None
    kinds: set[str] = set()
    relations: set[str] = set()
    for item in obj.spec.get("kinds") or []:
        if isinstance(item, dict) and item.get("kind"):
            kinds.add(str(item["kind"]))
            kinds.update(str(a) for a in item.get("kindAliases") or [])
    for item in obj.spec.get("relations") or []:
        if isinstance(item, dict) and item.get("relation"):
            relations.add(str(item["relation"]))
    for base in obj.spec.get("extends") or []:
        name, _, version = str(base).partition("@")
        major = _major(version)
        if major is None:
            return None
        inner = _ontology_terms((name, major), packs, seen)
        if inner is None:
            return None
        kinds |= inner[0]
        relations |= inner[1]
    return kinds, relations


def _knowledge_rules(package: Package, visible: list[Obj]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    where = _rel(package.path / "package.yaml")
    use = knowledge_uses(package)
    declared = package.spec.get("knowledge")
    if declared is None:
        if use:
            warnings.append(
                f"{where}: knowledge_undeclared: процессы пакета обращаются к памяти "
                f"(виды: {', '.join(sorted(use.kinds)) or '—'}), а spec.knowledge не объявлен"
            )
        return errors, warnings
    packs = _knowledge_packs(visible)
    kinds: set[str] = set()
    relations: set[str] = set()
    opaque: list[str] = []
    for value in declared:
        ref = _ontology_ref(value)
        if ref not in packs and ref[0] in PLATFORM_ONTOLOGIES:
            opaque.append(value)
            continue
        if ref not in packs:
            errors.append(
                f"{where}: knowledge_unknown: онтология {value} не объявлена KnowledgePack "
                f"ни в пакете {package.key}, ни в его requires"
            )
            continue
        terms = _ontology_terms(ref, packs, set())
        if terms is None:
            opaque.append(value)
            continue
        kinds |= terms[0]
        relations |= terms[1]
    if len(errors) or not use:
        # неизвестная онтология уже названа — разбор видов по ней был бы шумом
        return errors, warnings
    unknown = [
        (label, name, refs)
        for label, table, known in (
            ("вид", use.kinds, kinds),
            ("связь", use.relations, relations),
        )
        for name, refs in sorted(table.items())
        if name not in known
    ]
    for label, name, refs in unknown:
        message = (
            f"{where}: knowledge_term_unknown: {label} {name!r} ({', '.join(refs)}) нет в "
            f"онтологиях spec.knowledge ({', '.join(declared) or '—'})"
        )
        if opaque:
            # содержимое части онтологий не видно — решает память при регистрации
            warnings.append(message + f"; не проверено против {', '.join(opaque)}")
        else:
            errors.append(message)
    return errors, warnings


def check_manifest(installation: Installation) -> tuple[list[str], list[str]]:
    """Правила манифеста всех пакетов установки: переменные, requires, онтологии."""
    errors: list[str] = []
    warnings: list[str] = []
    versions = {p.key: str(p.spec.get("version")) for p in installation.packages}
    local = local_versions()

    for package in installation.packages:
        found_errors, found_warnings = _variable_rules(package)
        errors += found_errors
        warnings += found_warnings
        errors += _requires_rules(package, versions)
        found_errors, found_warnings = _engines_rules(package, local)
        errors += found_errors
        warnings += found_warnings
        found_errors, found_warnings = _knowledge_rules(package, installation.visible(package.key))
        errors += found_errors
        warnings += found_warnings
        errors += _pack_rules(package, installation.visible(package.key))
    errors += _pack_conflicts(installation)
    warnings += _installation_knowledge(installation)
    return errors, warnings


def _pack_rules(package: Package, visible: list[Obj]) -> list[str]:
    """KnowledgePack пакета: ключ — имя онтологии, extends — в пакете, его requires или
    онтология памяти."""
    errors: list[str] = []
    packs = _knowledge_packs(visible)
    for obj in package.objects:
        if obj.kind != "KnowledgePack":
            continue
        where = _rel(obj.path)
        name = obj.spec.get("name")
        if name != obj.key:
            errors.append(
                f"{where}: knowledge_pack_key: key {obj.key!r} — имя онтологии spec.name "
                f"({name!r}): у KnowledgePack они совпадают"
            )
        for base in obj.spec.get("extends") or []:
            base_name, _, version = str(base).partition("@")
            major = _major(version)
            if base_name in PLATFORM_ONTOLOGIES:
                continue
            if major is None or (base_name, major) not in packs:
                errors.append(
                    f"{where}: knowledge_extends_unknown: extends {base} — такой онтологии нет "
                    f"ни в пакете {package.key}, ни в его requires"
                )
    return errors


def _pack_conflicts(installation: Installation) -> list[str]:
    """Одна онтология name@major в двух местах установки — только одинаковой."""
    errors: list[str] = []
    seen: dict[tuple[str, str], Obj] = {}
    for obj in installation.objects:
        if obj.kind != "KnowledgePack":
            continue
        ident = (str(obj.spec.get("name")), str(obj.spec.get("version")))
        first = seen.setdefault(ident, obj)
        if first is not obj and first.spec != obj.spec:
            errors.append(
                f"{_rel(obj.path)}: knowledge_pack_conflict: {ident[0]}@{ident[1]} уже "
                f"объявлена в {_rel(first.path)} с другим содержимым — версия онтологии "
                "неизменяема, поднимите version"
            )
    return errors


def _installation_knowledge(installation: Installation) -> list[str]:
    """Включаемые онтологии, которых нет в пакетах установки, должны уже быть на стенде."""
    warnings: list[str] = []
    packs = _knowledge_packs(installation.objects)
    for entry in installation.knowledge:
        for value in entry.get("packs") or []:
            name, major = _ontology_ref(str(value))
            if name in PLATFORM_ONTOLOGIES or (name, major) in packs:
                continue
            warnings.append(
                f"knowledge: {value} для {entry.get('workspace')} — нет KnowledgePack в пакетах "
                "установки: онтология должна уже быть зарегистрирована на стенде"
            )
    return warnings


# --- describe: предпосылки установки -----------------------------------------


def load_for_describe(directory: Path) -> tuple[Package, Installation | None, str | None]:
    """Пакет по каталогу и, если найдутся, его requires (соседние каталоги, packages/ рядом)."""
    package = load_package(directory)
    try:
        installation = resolve(
            [{"key": package.key, "path": str(directory.resolve())}],
            packages_dir=directory.resolve().parent,
        )
    except PackageError as error:
        return package, None, str(error)
    return installation.packages[-1], installation, None


def describe(package: Package, installation: Installation | None = None) -> dict[str, Any]:
    """Что нужно стенду, чтобы поставить пакет: выводится из объектов, значений секретов нет."""
    spec = package.spec
    packages = installation.packages if installation else [package]
    versions = {p.key: str(p.spec.get("version")) for p in packages}
    variables = []
    for owner in packages:
        uses = variable_uses(owner)
        declared = owner.spec.get("variables")
        names = sorted(set(declared or {}) | set(uses))
        for name in names:
            item = (declared or {}).get(name) or {}
            variables.append(
                {
                    "name": name,
                    "package": owner.key,
                    "declared": name in (declared or {}),
                    "kind": item.get("kind"),
                    "required": bool(item.get("required", True)) and "default" not in item,
                    "default": item.get("default"),
                    "example": item.get("example"),
                    "description": item.get("description"),
                    "usedBy": sorted({o.ref for o in uses.get(name, [])}),
                }
            )
    agents = []
    for owner in packages:
        for obj in owner.objects:
            if obj.kind != "Agent":
                continue
            placement = obj.spec.get("placement")
            executor = obj.spec.get("executor") or {}
            agents.append(
                {
                    "agent": obj.key,
                    "package": owner.key,
                    "executor": executor.get("kind"),
                    "image": executor.get("image"),
                    "placement": "none" if placement == "none" else "node",
                    "nodeLabels": list((placement or {}).get("requires") or [])
                    if isinstance(placement, dict)
                    else [],
                    "nodeSecrets": list((placement or {}).get("secrets") or [])
                    if isinstance(placement, dict)
                    else [],
                    "roles": list((obj.spec.get("identity") or {}).get("roles") or []),
                }
            )
    provided = sorted(
        f"{name}@{major}"
        for (name, major) in _knowledge_packs([o for p in packages for o in p.objects])
    )
    return {
        "package": package.key,
        "version": spec.get("version"),
        "displayName": spec.get("displayName"),
        "license": spec.get("license"),
        "authors": list(spec.get("authors") or []),
        "homepage": spec.get("homepage"),
        "engines": dict(spec.get("engines") or {}),
        "requires": [
            {
                "package": key,
                "version": rng,
                "resolved": versions.get(key),
            }
            for key, rng in package.requirements
        ],
        "variables": variables,
        "agents": agents,
        "knowledge": {
            "declared": list(spec.get("knowledge") or []),
            "provided": provided,
            "used": {
                "kinds": sorted(knowledge_uses(package).kinds),
                "relations": sorted(knowledge_uses(package).relations),
            },
        },
        "objects": _object_counts(package),
        "planKinds": sorted({o.kind for o in package.objects if o.kind in PLAN_KINDS}),
    }


def _object_counts(package: Package) -> dict[str, int]:
    counts: dict[str, int] = {}
    for obj in package.objects:
        counts[obj.kind] = counts.get(obj.kind, 0) + 1
    return dict(sorted(counts.items()))


def env_example(info: dict[str, Any]) -> str:
    """Заготовка файла переменных установки: описание, вид, пример; значения пустые, если
    default нет. Секретов в пакете нет — и в заготовке их нет. Автор обычно сохраняет её в
    файл пакета, поэтому текст генератора — по-английски (TASK-001252); описания переменных
    приходят из пакета как есть."""
    lines = [
        f"# Installation variables of the package {info['package']} {info['version']}"
        " and of its requires.",
        "# Generated by package-sdk describe --env-example. No secret values go here:",
        "# the secrets of agents live on the fleet nodes (placement.secrets).",
    ]
    current = None
    for variable in info["variables"]:
        if variable["package"] != current:
            current = variable["package"]
            lines += ["", f"# --- {current}"]
        facts = [variable["kind"] or "string"]
        facts.append("required" if variable["required"] else "optional")
        if not variable["declared"]:
            facts.append("not declared in spec.variables")
        lines.append(f"# {variable['description'] or '(no description)'} [{', '.join(facts)}]")
        if variable["example"]:
            lines.append(f"# example: {variable['example']}")
        value = variable["default"] if variable["default"] is not None else ""
        lines.append(f"{variable['name']}={value}")
    return "\n".join(lines) + "\n"


def format_describe(info: dict[str, Any], problem: str | None = None) -> str:
    out = [f"{info['package']} {info['version']} — {info['displayName']}"]
    if info["license"]:
        out.append(f"лицензия: {info['license']}")
    if info["authors"]:
        out.append(f"авторы: {', '.join(info['authors'])}")
    out.append("")
    out.append("совместимость:")
    out += [f"  {name} {rng}" for name, rng in info["engines"].items()] or ["  (не объявлена)"]
    out.append("зависимости:")
    out += [
        f"  {r['package']} {r['version'] or '*'}"
        + (f" (найдена {r['resolved']})" if r["resolved"] else " (не найдена рядом)")
        for r in info["requires"]
    ] or ["  (нет)"]
    if problem:
        out.append(f"  ! {problem}")
    out.append("переменные:")
    for v in info["variables"]:
        flag = "обязательна" if v["required"] else f"по умолчанию {v['default']!r}"
        if not v["declared"]:
            flag = "НЕ ОБЪЯВЛЕНА"
        out.append(f"  {v['name']} [{v['kind'] or '?'}, {flag}] ({v['package']})")
        if v["description"]:
            out.append(f"      {v['description']}")
    if not info["variables"]:
        out.append("  (нет)")
    out.append("агенты и узлы:")
    for a in info["agents"]:
        labels = ", ".join(a["nodeLabels"]) or "—"
        secrets = ", ".join(a["nodeSecrets"]) or "—"
        where = (
            "без процесса (только личность)"
            if a["placement"] == "none"
            else f"метки {labels}; секреты {secrets}"
        )
        image = f", образ {a['image']}" if a["image"] else ""
        owner = f" [{a['package']}]" if a["package"] != info["package"] else ""
        out.append(f"  {a['agent']}{owner} ({a['executor'] or '—'}{image}): {where}")
    if not info["agents"]:
        out.append("  (нет)")
    k = info["knowledge"]
    out.append("онтологии:")
    out.append(f"  объявлены: {', '.join(k['declared']) or '—'}")
    out.append(f"  в пакетах: {', '.join(k['provided']) or '—'}")
    out.append(f"  виды процессов: {', '.join(k['used']['kinds']) or '—'}")
    out.append("объекты: " + ", ".join(f"{kind} {n}" for kind, n in info["objects"].items()))
    return "\n".join(out) + "\n"


# --- docs: разделы README пакета ---------------------------------------------

DOCS_BEGIN = "<!-- package-sdk:docs -->"
DOCS_END = "<!-- /package-sdk:docs -->"


def render_docs(info: dict[str, Any], package: Package) -> str:
    """Разделы README, которые выводятся из пакета: объекты, переменные, требования, агенты.

    Текст раздела — английский, как всё, что генератор пишет в пакет автора (init, image):
    раздел попадает в README пакета, который читают за пределами платформы. Описания
    переменных идут из манифеста как есть — на языке автора."""
    out = [DOCS_BEGIN, "_Generated by `package-sdk docs`: do not edit by hand._", ""]
    out += ["### Objects", "", "| Kind | Key | File |", "|---|---|---|"]
    for obj in sorted(package.objects, key=lambda o: (o.kind, o.key)):
        out.append(
            f"| {obj.kind} | `{obj.key}` | `{obj.path.relative_to(package.path).as_posix()}` |"
        )
    out += ["", "### Installation variables", ""]
    own = [v for v in info["variables"] if v["package"] == package.key]
    if own:
        out += [
            "| Variable | Kind | Required | Default | Description |",
            "|---|---|---|---|---|",
        ]
        for v in own:
            default = f"`{v['default']}`" if v["default"] is not None else "—"
            out.append(
                f"| `{v['name']}` | {v['kind'] or '—'} | {'yes' if v['required'] else 'no'} "
                f"| {default} | {v['description'] or '—'} |"
            )
    else:
        out.append("None.")
    out += ["", "### Requirements", ""]
    engines = [f"`{name} {rng}`" for name, rng in info["engines"].items()]
    requires = [f"`{r['package']} {r['version'] or '*'}`" for r in info["requires"]]
    out.append(f"- Platform components: {', '.join(engines) or '—'}")
    out.append(f"- Packages: {', '.join(requires) or '—'}")
    out.append(f"- Ontologies: {', '.join(info['knowledge']['declared']) or '—'}")
    agents = [a for a in info["agents"] if a["package"] == package.key]
    out += ["", "### Agents", ""]
    if agents:
        out += ["| Agent | Executor | Node labels | Node secrets |", "|---|---|---|---|"]
        for a in agents:
            placed = a["placement"] != "none"
            out.append(
                f"| `{a['agent']}` | {a['executor'] or '—'} "
                f"| {(', '.join(a['nodeLabels']) or '—') if placed else 'no process'} "
                f"| {', '.join(a['nodeSecrets']) or '—'} |"
            )
    else:
        out.append("None.")
    out += ["", DOCS_END]
    return "\n".join(out) + "\n"


def apply_docs(readme: str, section: str) -> str:
    """README с обновлённым разделом: между метками — заменить, меток нет — дописать в конец."""
    if DOCS_BEGIN in readme and DOCS_END in readme:
        head, _, rest = readme.partition(DOCS_BEGIN)
        _, _, tail = rest.partition(DOCS_END)
        return head + section.rstrip("\n") + tail
    return (readme.rstrip("\n") + "\n\n" if readme.strip() else "") + section
