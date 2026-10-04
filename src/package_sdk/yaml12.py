"""Скаляры файла пакета так, как их читает ядро при публикации (TASK-001251).

Ядро разбирает пакет своим загрузчиком (control-plane, ``domain/package_source.py``,
``yaml12_loader`` и ``load_yaml``; TASK-001231, TASK-001247). ``check``, ``test`` и
``plan`` на машине автора и правка файла (``edit``, ruamel) обязаны видеть те же
значения, иначе автор проверяет не тот пакет, который опубликует. Импортировать ядро
нельзя — для package-sdk оно зависимость только песочницы, — поэтому правила повторены
здесь, а тест сверки (``tests/test_yaml12.py``) прогоняет одну таблицу через оба
загрузчика, когда сосед ``control-plane`` доступен.

Правила ядра:

- **bool** — только ``true``/``false`` (и ``True``, ``TRUE``, ``False``, ``FALSE``):
  ``on``, ``off``, ``yes``, ``no`` — строки; ``!!bool yes`` — ошибка.
- **int** — схема core YAML 1.2: десятичное со знаком, ``0o`` и ``0x`` без знака. ``012`` —
  двенадцать; ``1_000``, ``0b101``, ``1:30`` (шестидесятеричное YAML 1.1), ``-0x1A`` —
  строки. Больше :data:`MAX_INT_DIGITS` цифр — ошибка; ``!!int x`` — ошибка.
- **float** — распознавание PyYAML (YAML 1.1) как есть: ``1e3`` без точки — строка,
  ``1.0e+3`` — тысяча, ``1_000.5`` и ``1:30.5`` — числа. Значение, которого нет в JSON
  (``.inf``, ``-.inf``, ``.nan``, ``!!float 1e999``), — ошибка; ``!!float x`` — ошибка.
- **null** — ``~``, ``null``, ``Null``, ``NULL`` и пусто, как в YAML 1.2.
- **timestamp** не распознаётся: ``2026-09-30`` — строка, как дату держит JSON.
- Тег, который не даёт значения JSON (``!!binary``, ``!!timestamp``, ``!!set``, ``=`` —
  ``!!value``), и строка с одиночным суррогатом (``"\\ud800"``) — ошибка.

Запись (TASK-001253). Файл пакета читает не только ядро: редактор автора
(``yaml-language-server``) — по схеме core YAML 1.2, а многие инструменты — по YAML 1.1.
Строка пишется без кавычек, только если все три читателя прочтут её строкой
(:func:`plain_is_string`): ``1e3`` для YAML 1.2 — число, ``1_000``, ``yes`` и
``2026-09-30`` для YAML 1.1 — число, булево и дата, ``0o12`` для ядра — целое. Писатели
пакета — :class:`SafeDumper` и :func:`dump` (PyYAML: ``export``/``pull`` новым файлом,
заготовки, установка MCP) и представитель строк ``edit`` (ruamel) — спрашивают
:func:`plain_is_string`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from functools import cache
from typing import Any

import yaml

# Цифр в целом: как в ядре (MAX_INT_DIGITS), далеко выше любого числа пакета и далеко
# ниже квадратичного int() и предела записи JSON (4300 цифр).
MAX_INT_DIGITS = 1000

_TAG = "tag:yaml.org,2002:"
BOOL_TAG, INT_TAG, FLOAT_TAG = f"{_TAG}bool", f"{_TAG}int", f"{_TAG}float"
STR_TAG = f"{_TAG}str"
# Теги, которые дают значение JSON; остальные (!!binary, !!timestamp, !!set, !!omap,
# !!value) ядро отвергает.
JSON_TAGS = frozenset(
    f"{_TAG}{name}" for name in ("null", "bool", "int", "float", "str", "seq", "map", "merge")
)
_BOOLS = {
    word: word.lower() == "true" for word in ("true", "True", "TRUE", "false", "False", "FALSE")
}
_INT = re.compile(r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$")
_SURROGATE = re.compile("[\ud800-\udfff]")
# Распознавание SafeLoader, которое ядро снимает: на месте bool и int — свои, timestamp нет.
_DROPPED = (BOOL_TAG, INT_TAG, f"{_TAG}timestamp")


def _shown(value: str) -> str:
    """Скаляр, как его называет сообщение: длинный — обрезан."""
    return value if len(value) <= 40 else f"{value[:40]}..."


def parse_int(text: str) -> int:
    """Целое схемы core YAML 1.2 (целые JSON среди них), как его читает ядро.

    ValueError — для другой записи (``1:30``, ``1_000``, ``0b1``) и для больше
    :data:`MAX_INT_DIGITS` цифр, до того как платить за ``int()``."""
    if not _INT.match(text):
        raise ValueError(f"{_shown(text)!r} is not a YAML 1.2 integer")
    base = {"0o": 8, "0x": 16}.get(text[:2], 10)
    digits = text.lstrip("+-") if base == 10 else text[2:]
    if len(digits) > MAX_INT_DIGITS:
        raise ValueError(f"integer has more than {MAX_INT_DIGITS} digits")
    return -int(digits, base) if text.startswith("-") else int(digits, base)


def parse_bool(text: str) -> bool:
    """Булево YAML 1.2; ValueError — для другой записи (``yes``, ``on``)."""
    if text not in _BOOLS:
        raise ValueError(f"{_shown(text)!r} is not a YAML 1.2 boolean")
    return _BOOLS[text]


def parse_float(text: str) -> float:
    """Число с плавающей точкой, как его читает ядро: конструктором PyYAML, только конечное.

    ValueError — для записи, которую PyYAML не читает (``!!float x``), и для значения,
    которого нет в JSON (``.inf``, ``.nan``, ``1e999``)."""
    try:
        value = yaml.constructor.SafeConstructor().construct_yaml_float(
            yaml.ScalarNode(FLOAT_TAG, text)
        )
    except (ValueError, IndexError):
        # float() слова или пустой !!float у PyYAML.
        raise ValueError(f"{_shown(text)!r} is not a floating-point number") from None
    except OverflowError:
        # Шестидесятеричное YAML 1.1 за пределами float: 60 ** n как целое.
        value = math.inf
    if not math.isfinite(value):
        raise ValueError(f"value {_shown(text)!r}: a package holds only JSON values")
    return float(value)


def _children(node: Any) -> Iterator[Any]:
    """Дочерние узлы узла PyYAML или ruamel: у отображения — ключи и значения."""
    value = node.value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, tuple):
                yield from item
            else:
                yield item


def scalar_problem(tag: str, value: str) -> str | None:
    """Почему скаляр с этим тегом ядро не прочтёт; None — прочтёт."""
    if _SURROGATE.search(value):
        return "string contains a lone surrogate (\\ud800-\\udfff): this is not Unicode text"
    parse = {BOOL_TAG: parse_bool, INT_TAG: parse_int, FLOAT_TAG: parse_float}.get(tag)
    if parse is not None:
        try:
            parse(value)
        except ValueError as error:
            return str(error)
    return None


def value_problem(root: Any) -> str | None:
    """Почему дерево узлов YAML (PyYAML или ruamel) ядро не прочтёт: тег без значения JSON,
    скаляр, который его тег не читает, одиночный суррогат; None — прочтёт.

    Каждый узел смотрится один раз, сколько бы алиасов на него ни ссылалось; рекурсивный
    алиас и бомбу алиасов отвергает раньше ``model.alias_expansion_problem``."""
    seen: set[int] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        tag = str(node.tag)
        if tag not in JSON_TAGS:
            problem: str | None = f"value tagged {tag}: a package holds only JSON values"
        elif isinstance(node.value, str):
            problem = scalar_problem(tag, node.value)
        else:
            stack.extend(_children(node))
            continue
        if problem is not None:
            mark = getattr(node, "start_mark", None)
            return f"line {mark.line + 1}: {problem}" if mark is not None else problem
    return None


@cache
def _resolvers() -> tuple[tuple[str, tuple[tuple[str, re.Pattern[str]], ...]], ...]:
    table: dict[str, list[tuple[str, re.Pattern[str]]]] = {}
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items():
        kept = [(tag, regexp) for tag, regexp in resolvers if tag not in _DROPPED]
        if kept:
            table[first] = kept
    bools = re.compile(f"^(?:{'|'.join(_BOOLS)})$")
    for first in "tTfF":
        table.setdefault(first, []).append((BOOL_TAG, bools))
    for first in "-+0123456789":
        table.setdefault(first, []).append((INT_TAG, _INT))
    return tuple((first, tuple(rules)) for first, rules in table.items())


def implicit_resolvers() -> dict[str, list[tuple[str, re.Pattern[str]]]]:
    """Распознавание неявных тегов, как у загрузчика ядра: SafeLoader без bool, int и
    timestamp YAML 1.1, плюс bool и int YAML 1.2. Новая таблица на каждый вызов — её
    можно отдать загрузчику, который правит её на месте."""
    return {first: list(rules) for first, rules in _resolvers()}


# --- запись -------------------------------------------------------------------

# Схема core YAML 1.2 (спецификация 1.2.2, 10.3.2) — так читает редактор автора
# (yaml-language-server): null, bool, int (десятичное со знаком, 0o, 0x), float (с
# экспонентой без точки, .inf, .nan).
_CORE_SCHEMA = re.compile(
    r"""~|null|Null|NULL
    |true|True|TRUE|false|False|FALSE
    |[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+
    |[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?
    |[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)""",
    re.VERBOSE,
)


# Булевы YAML 1.1 по спецификации, которых нет в распознавании PyYAML.
_YAML11_SHORT_BOOLS = frozenset({"y", "Y", "n", "N"})


@cache
def _core_table() -> dict[str, list[tuple[str, re.Pattern[str]]]]:
    return implicit_resolvers()


def _resolve(table: Any, value: str) -> str:
    """Неявный тег скаляра без кавычек по таблице распознавания, как его ищет PyYAML
    (таблицу не меняет)."""
    rules = [*table.get(value[0] if value else "", []), *table.get(None, [])]
    return next((tag for tag, regexp in rules if regexp.match(value)), STR_TAG)


def plain_is_string(value: str) -> bool:
    """Прочтут ли строку без кавычек строкой все три читателя файла пакета: ядро (правила
    этого модуля), YAML 1.1 (PyYAML ``SafeLoader``: ``yes``, ``1_000``, ``2026-09-30``)
    и схема core YAML 1.2 (редактор автора: ``1e3``). False — писать в кавычках.

    YAML 1.1 — по спецификации (yaml.org/type/bool): ``y``, ``Y``, ``n``, ``N`` — булевы
    (так читает go-yaml v2), хотя PyYAML их не распознаёт.

    Пусто — null у всех трёх. Можно ли строку вообще записать без кавычек (``: ``,
    ``#``, ведущий ``-``), решает эмиттер: здесь — только чтение значения."""
    return (
        value != ""
        and _resolve(_core_table(), value) == STR_TAG
        and _resolve(yaml.SafeLoader.yaml_implicit_resolvers, value) == STR_TAG
        and value not in _YAML11_SHORT_BOOLS
        and _CORE_SCHEMA.fullmatch(value) is None
    )


class SafeDumper(yaml.SafeDumper):
    """SafeDumper, который пишет без кавычек только строку, которую все три читателя
    прочтут строкой (:func:`plain_is_string`); остальные — в кавычках.

    Решает распознавание при записи: сериализатор PyYAML разрешает скаляр без кавычек,
    только если неявный тег текста — тег узла. Для строки, которую хоть один читатель
    прочтёт иначе, тег здесь не ``str`` — и эмиттер берёт кавычки, какой бы представитель
    строк ни был у наследника (``apply.dump_document`` пишет многострочные блоком).
    Значение — в одинарных кавычках, ключ — в двойных (``"on":``, как в файлах формата
    и у ``edit``)."""

    def represent_mapping(self, tag: str, mapping: Any, flow_style: Any = None) -> Any:
        node = super().represent_mapping(tag, mapping, flow_style)
        for key, _value in node.value:
            if (
                isinstance(key, yaml.ScalarNode)
                and key.tag == STR_TAG
                and key.style is None
                and not plain_is_string(key.value)
            ):
                key.style = '"'
        return node

    def resolve(self, kind: Any, value: Any, implicit: Any) -> Any:
        tag = super().resolve(kind, value, implicit)  # type: ignore[no-untyped-call]
        if (
            kind is yaml.ScalarNode
            and implicit[0]
            and tag == STR_TAG
            and not plain_is_string(value)
        ):
            return f"{_TAG}ambiguous"
        return tag


def dump(data: Any, **options: Any) -> str:
    """YAML файла пакета писателем :class:`SafeDumper` (параметры — как у ``yaml.dump``)."""
    return str(yaml.dump(data, Dumper=SafeDumper, **options))
