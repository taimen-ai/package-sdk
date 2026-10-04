"""Edit package files preserving the file (TAI-ADR-0054 item 12, plan R10).

Small operations on a process description and a package that change only what
was asked: comments, key order, quote style, flow and block style of the other
lines stay as they were. Works on ruamel.yaml in round-trip mode; scalars are read
by the rules of the package loader and the core (`package_sdk.yaml12`):
`on`, `off`, `yes`, `no`, `1_000`, `1:30`, `2026-09-30` are strings, `012` is twelve,
`.inf`, `.nan` and an integer longer than a thousand digits are errors. A new string is
written quoted if at least one reader would read it unquoted as a non-string — the core,
YAML 1.1 or core YAML 1.2 of the author's editor (`yaml12.plain_is_string`: `1e3`, `1_000`,
`0o12`, `yes`; a value in single quotes, a key in double quotes, `"on"`); a string that
already stood in the file unquoted stays as it was (TASK-001253).

    package-sdk edit add-step  --file P --in <stage|step> --step '{id: x, set: {a: "1"}}' [--after ID|--before ID]
    package-sdk edit add-stage --file P --stage '<yaml>' [--after ID|--before ID]
    package-sdk edit add-decision-row --file P --table ID --row '{when: {a: "-"}, then: {b: 1}}' [--index N]
    package-sdk edit add-rule  --file P (--table ID --row '<yaml>' | --on-event '<yaml>')
    package-sdk edit add-form-field --file P --step ID --name F --schema '{type: string}' [--required] [--label T]
    package-sdk edit rename    --file P --from OLD --to NEW [--no-migration]
    package-sdk edit rename    --package DIR --kind Process --from OLD --to NEW
    package-sdk edit set       --file P --path 'spec.stages[go-no-go].exit' --value "data.decision != ''"

Each operation is a module function and a CLI command. `--json` prints the result or
the error in machine-readable form: {"ok": false, "error": {"code", "message", "path", "hint"}}.
`--dry-run` prints the diff and writes nothing. Before writing, the document is checked
against the package-sdk format schema (`object.schema.json`, tests — `test.schema.json`):
an invalid edit is not written.

The process diagram layout is kept apart from the logic — `<package>/.layout/<process key>.json`
(element coordinates by id). Logic operations do not touch it; `rename` moves the
coordinates to the new id.

Preservation: the file style (indents, `-` offset, line width, null notation) is
picked at load time so that loading and writing without changes gives the same file
byte for byte. If the file contains something ruamel cannot reproduce (for example a
flow collection split across several lines), the edit is carried over to the source
text by a three-way merge: only the lines the edit touches change, the rest stays
as in the source.
"""

from __future__ import annotations

import argparse
import difflib
import io
import json
import re
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from package_sdk import schema as schema_module
from package_sdk import yaml12
from package_sdk.model import FOLDERS, alias_expansion_problem

try:
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    from ruamel.yaml.constructor import RoundTripConstructor
    from ruamel.yaml.representer import RoundTripRepresenter
    from ruamel.yaml.resolver import VersionedResolver
    from ruamel.yaml.scalarstring import PlainScalarString
except ImportError:  # pragma: no cover - окружение без ruamel.yaml
    YAML = None  # type: ignore[assignment,misc]
    CommentedMap = dict  # type: ignore[assignment,misc]
    CommentedSeq = list  # type: ignore[assignment,misc]
    RoundTripConstructor = object  # type: ignore[assignment,misc]
    RoundTripRepresenter = object  # type: ignore[assignment,misc]
    VersionedResolver = object  # type: ignore[assignment,misc]
    PlainScalarString = str  # type: ignore[assignment,misc]

ELEMENT_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
LAYOUT_DIR = ".layout"
# Ключи spec процесса, под которыми нет элементов процесса: схемы данных и форм,
# карты CEL, проекция в память. Таблицы решений — элементы, их входы и выходы — нет.
_NOT_ELEMENTS = frozenset(
    {
        "data",
        "form",
        "memory",
        "input",
        "set",
        "output",
        "export",
        "migrations",
        "inputs",
        "outputs",
        "rules",
        "governedBy",
        "retrospective",
        "context",
    }
)


class PkgError(Exception):
    """Ошибка операции: код, сообщение, место и подсказка — для человека и для агента."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        hint: str | None = None,
        file: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code, self.message, self.path, self.hint, self.file = code, message, path, hint, file

    def as_dict(self) -> dict[str, Any]:
        return {
            k: v
            for k, v in {
                "code": self.code,
                "message": self.message,
                "file": self.file,
                "path": self.path,
                "hint": self.hint,
            }.items()
            if v is not None
        }


def _require_ruamel() -> None:
    if YAML is None:
        raise PkgError(
            "dependency_missing",
            "ruamel.yaml is required: pip install ruamel.yaml",
            hint="ruamel.yaml is a dependency of package-sdk — reinstall the package",
        )


# --- скаляры как у ядра ------------------------------------------------------


class _Resolver(VersionedResolver):  # type: ignore[misc,valid-type]
    """Неявные теги как у загрузчика пакета и ядра (`yaml12`), при любой директиве %YAML.

    При записи по ним эмиттер решает, можно ли строке файла остаться без кавычек:
    `1_000`, стоявшая в файле без кавычек, остаётся так, потому что ядро прочтёт её
    строкой. Новые строки кавычит представитель (:func:`_represent_str`) — строже."""

    @property
    def versioned_resolver(self) -> Any:
        table = self.__dict__.get("_package_table")
        if table is None:
            table = self.__dict__["_package_table"] = yaml12.implicit_resolvers()
        return table


class _Constructor(RoundTripConstructor):  # type: ignore[misc,valid-type]
    """Целые и числа с точкой — значения ядра. ruamel оставляет свою обёртку (OctalInt,
    ScalarFloat), чтобы запись без изменений дала тот же текст, пока значение то же; где
    ruamel читает иначе (`1:30.5`, директива `%YAML 1.1` у `012`), — значение ядра."""

    def construct_yaml_int(self, node: Any) -> Any:
        expected = yaml12.parse_int(node.value)
        try:
            value = super().construct_yaml_int(node)
        except ValueError:
            return expected
        return value if value == expected else expected

    def construct_yaml_float(self, node: Any) -> Any:
        expected = yaml12.parse_float(node.value)
        try:
            value = super().construct_yaml_float(node)
        except ValueError:
            return expected
        return value if value == expected else expected


_Constructor.add_constructor(yaml12.INT_TAG, _Constructor.construct_yaml_int)
_Constructor.add_constructor(yaml12.FLOAT_TAG, _Constructor.construct_yaml_float)


class _FileConstructor(_Constructor):
    """Дерево файла: строка, которая стоит в файле без кавычек, хотя YAML 1.1 или 1.2
    прочтёт её не строкой (`on:`, `version: 1_000`), помечается `PlainScalarString` —
    запись оставит её без кавычек, и нетронутые строки файла не меняются. Фрагменты CLI
    (`--value 1e3`) читаются без пометки: это новое значение, оно пишется в кавычках."""

    def construct_yaml_str(self, node: Any) -> Any:
        value = super().construct_yaml_str(node)
        if type(value) is str and not node.style and not yaml12.plain_is_string(value):
            return PlainScalarString(value)
        return value


_FileConstructor.add_constructor(yaml12.STR_TAG, _FileConstructor.construct_yaml_str)


# --- стиль файла и round-trip ------------------------------------------------


@dataclass(frozen=True)
class Style:
    mapping: int = 2
    offset: int = 2  # на сколько `-` отстоит от ключа родителя
    width: int = 4096
    null: str = "null"  # как писать None: "null" или пусто
    explicit_start: bool = False

    def yaml(self, *, file: bool = False) -> Any:
        """ruamel с правилами пакета; file — дерево файла (:class:`_FileConstructor`)."""
        _require_ruamel()
        y = YAML(typ="rt")
        y.Resolver = _Resolver
        y.Constructor = _FileConstructor if file else _Constructor
        y.preserve_quotes = True
        y.width = self.width
        y.brace_single_entry_mapping_in_flow_sequence = True
        y.explicit_start = self.explicit_start
        y.indent(mapping=self.mapping, sequence=self.offset + 2, offset=self.offset)
        y.Representer = _representer(self.null)
        return y


_REPRESENTERS: dict[str, Any] = {}


def _represent_str(representer: Any, data: str) -> Any:
    """Новая строка без кавычек — только если все три читателя прочтут её строкой
    (`yaml12.plain_is_string`); иначе в одинарных кавычках, как ruamel кавычит сам."""
    if yaml12.plain_is_string(data):
        return representer.represent_str(data)
    return representer.represent_scalar(yaml12.STR_TAG, data, style="'")


def _represent_new_key(representer: Any, data: Any) -> Any:
    """Новый ключ, который хоть один читатель прочтёт не строкой (`on` правил и
    обработчиков: YAML 1.1 читает его True), — в двойных кавычках, как ключи `"on"` в
    файлах формата и у писателей на PyYAML (`yaml12.SafeDumper`). Ключ, который уже стоит
    в файле, пишется как стоял: без кавычек (`PlainScalarString`) или в своих кавычках."""
    if type(data) is str and not yaml12.plain_is_string(data):
        return representer.represent_scalar(yaml12.STR_TAG, data, style='"')
    return RoundTripRepresenter.represent_key(representer, data)


def _representer(null: str) -> Any:
    """Представитель с заданной записью null и строгими кавычками строк. add_representer
    у подкласса копирует таблицу, поэтому глобальный RoundTripRepresenter не меняется."""
    if null not in _REPRESENTERS:
        cls = type(
            f"_Representer_{'null' if null else 'empty'}",
            (RoundTripRepresenter,),
            {"represent_key": _represent_new_key},
        )
        cls.add_representer(
            type(None), lambda r, _d: r.represent_scalar("tag:yaml.org,2002:null", null)
        )
        cls.add_representer(str, _represent_str)
        _REPRESENTERS[null] = cls
    return _REPRESENTERS[null]


_KEY_LINE = re.compile(r"^(\s*)(?:- )*[^\s#-][^#]*:\s*(#.*)?$")


def _detect(text: str) -> tuple[list[int], list[int], list[str]]:
    """Кандидаты отступа ключей, смещения `-` и записи null — самые частые первыми."""
    maps: Counter[int] = Counter()
    offsets: Counter[int] = Counter()
    previous: tuple[int, str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        column = len(line) - len(line.lstrip(" "))
        if previous is not None and _KEY_LINE.match(previous[1]):
            if stripped == "-" or stripped.startswith("- "):
                offsets[column - previous[0]] += 1
            elif column > previous[0] and not previous[1].lstrip().startswith("- "):
                maps[column - previous[0]] += 1
        previous = (column, line)
    mapping = [m for m, _ in maps.most_common() if m > 0] or [2]
    offset = [o for o, _ in offsets.most_common() if o >= 0]
    offset += [o for o in (2, 0) if o not in offset]
    explicit_null = re.search(r"(?m)(:|^\s*-)\s+(null|~)\s*(#.*)?$", text) is not None
    return mapping[:2], offset[:3], (["null", ""] if explicit_null else ["", "null"])


def _emit(data: Any, style: Style, text: str) -> str:
    out = io.StringIO()
    style.yaml().dump(data, out)
    result = out.getvalue()
    if not text.endswith("\n") and result.endswith("\n"):
        result = result[:-1]
    return result


def _load(text: str, *, file: bool = False) -> Any:
    """Дерево ruamel; file — дерево файла: строки без кавычек остаются без кавычек."""
    _require_ruamel()
    # Раскрытие алиасов ограничено, как у загрузчика пакета (TASK-001231): дерево правки
    # обходится рекурсивно (to_plain, сравнение), бомба алиасов взорвала бы его. Значения,
    # которых ядро не прочтёт (.inf, !!binary, длинное целое), отвергаются так же (TASK-001251).
    node = Style().yaml().compose(text)
    problem = None if node is None else alias_expansion_problem(node) or yaml12.value_problem(node)
    if problem is not None:
        raise ValueError(problem)
    return Style().yaml(file=file).load(text)


def detect_style(text: str, data: Any) -> tuple[Style, bool]:
    """Стиль, при котором запись без изменений даёт исходный текст; (лучший, False) — если такого нет."""
    mappings, offsets, nulls = _detect(text)
    explicit_start = text.lstrip().startswith("---")
    first: Style | None = None
    for mapping in mappings:
        for offset in offsets:
            for null in nulls:
                style = Style(
                    mapping=mapping, offset=offset, null=null, explicit_start=explicit_start
                )
                first = first or style
                if _emit(data, style, text) == text:
                    return style, True
    # Длинные строки, перенесённые при записи: ширина — длина перенесённой строки минус один
    lines = text.splitlines()
    widths = sorted(
        {
            len(line) - 1
            for i, line in enumerate(lines[:-1])
            if len(line) >= 40
            and lines[i + 1].strip()
            and not lines[i + 1].lstrip().startswith(("#", "- "))
        }
    )
    for width in widths:
        style = Style(
            mapping=mappings[0],
            offset=offsets[0],
            null=nulls[0],
            width=width,
            explicit_start=explicit_start,
        )
        if _emit(data, style, text) == text:
            return style, True
    assert first is not None
    return first, False


def merge_text(source: str, base: str, new: str) -> str:
    """Трёхстороннее слияние по строкам: base — запись исходного дерева, new — запись
    изменённого. Правка base→new переносится на source; строки source, которые ruamel
    нормализует (base ≠ source), меняются, только если правка их касается."""
    if base == source:
        return new
    src, old, upd = (t.splitlines(keepends=True) for t in (source, base, new))
    anchored: dict[int, int] = {}  # строка base → та же строка source
    for tag, i1, i2, j1, _j2 in difflib.SequenceMatcher(
        None, old, src, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            anchored.update({i1 + k: j1 + k for k in range(i2 - i1)})
    hunks: list[list[int]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, old, upd, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            continue
        a1, a2 = i1, i2
        # Границы правки — на строках, которые в source те же, что в base
        insertion_ok = i1 == i2 and (i1 in anchored or (i1 - 1) in anchored or i1 in (0, len(old)))
        if not insertion_ok:
            while a1 > 0 and (a1 - 1) not in anchored:
                a1 -= 1
            while a2 < len(old) and a2 not in anchored:
                a2 += 1
        hunk = [a1, a2, j1 - (i1 - a1), j2 + (a2 - i2)]
        if hunks and hunk[0] < hunks[-1][1]:  # перекрылись после расширения — одна правка
            last = hunks[-1]
            if hunk[1] >= last[1]:
                last[1], last[3] = hunk[1], hunk[3]
        else:
            hunks.append(hunk)
    result: list[str] = []
    cursor = 0
    for i1, i2, j1, j2 in hunks:
        if i1 > 0 and (i1 - 1) in anchored:
            s1 = anchored[i1 - 1] + 1
        elif i1 in anchored:
            s1 = anchored[i1]
        else:
            s1 = 0 if i1 == 0 else len(src)
        s2 = s1 if i1 == i2 else (anchored[i2] if i2 in anchored else len(src))
        result.extend(src[cursor:s1])
        result.extend(upd[j1:j2])
        cursor = max(cursor, s2)
    result.extend(src[cursor:])
    return "".join(result)


@dataclass
class Document:
    """YAML-файл пакета: исходный текст, дерево round-trip и подобранный стиль."""

    path: Path
    text: str
    data: Any
    style: Style
    exact: bool
    base: str

    @classmethod
    def load(cls, path: Path | str) -> Document:
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise PkgError("file_not_found", f"no file {path}", file=str(path)) from error
        return cls.parse(text, path)

    @classmethod
    def parse(cls, text: str, path: Path | str = "<text>") -> Document:
        try:
            data = _load(text, file=True)
        except Exception as error:  # ruamel.yaml.YAMLError и потомки
            raise PkgError("yaml_invalid", f"not YAML: {error}", file=str(path)) from error
        style, exact = detect_style(text, data)
        base = text if exact else _emit(data, style, text)
        return cls(Path(path), text, data, style, exact, base)

    def dumps(self) -> str:
        emitted = _emit(self.data, self.style, self.text)
        return emitted if self.exact else merge_text(self.text, self.base, emitted)

    def save(self) -> bool:
        """Записать файл; False — если текст не изменился."""
        text = self.dumps()
        if text == self.text:
            return False
        self.path.write_text(text, encoding="utf-8")
        self.text, self.base = text, (text if self.exact else _emit(self.data, self.style, text))
        return True


def parse_fragment(value: str, what: str) -> Any:
    """Фрагмент YAML из аргумента CLI: flow-запись остаётся flow, block — block."""
    try:
        return _load(value)
    except Exception as error:
        raise PkgError("fragment_invalid", f"{what}: not YAML: {error}") from error


def to_plain(value: Any) -> Any:
    """Дерево ruamel → обычные dict/list/скаляры (для jsonschema и сравнения)."""
    if isinstance(value, dict):
        return {str(k): to_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_plain(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if type(value).__name__ == "ScalarBoolean":
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    return value


# --- элементы процесса --------------------------------------------------------


def iter_elements(spec: Any, path: str = "spec") -> Iterator[tuple[str, Any, str]]:
    """(id, узел, путь) всех элементов процесса: стадии, шаги любой вложенности, вехи,
    таймеры, ветви fork, таблицы решений."""
    if isinstance(spec, dict):
        for key, value in spec.items():
            if key in _NOT_ELEMENTS:
                continue
            if key == "decisions" and isinstance(value, list):
                for index, table in enumerate(value):
                    if isinstance(table, dict) and isinstance(table.get("id"), str):
                        yield table["id"], table, f"{path}.decisions[{index}]"
                continue
            yield from iter_elements(value, f"{path}.{key}")
    elif isinstance(spec, list):
        for index, item in enumerate(spec):
            where = f"{path}[{index}]"
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                yield item["id"], item, where
            yield from iter_elements(item, where)


def element_index(spec: Any) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for element_id, _node, where in iter_elements(spec):
        index.setdefault(element_id, []).append(where)
    return index


def _find_element(spec: Any, element_id: str) -> Any:
    for found, node, _where in iter_elements(spec):
        if found == element_id:
            return node
    raise PkgError(
        "element_not_found",
        f"the process has no element {element_id!r}",
        path=element_id,
        hint="element ids are stages, steps, milestones, timers, branches and decision tables",
    )


def _process_spec(doc: Document) -> Any:
    data = doc.data
    if (
        not isinstance(data, dict)
        or data.get("kind") != "Process"
        or not isinstance(data.get("spec"), dict)
    ):
        raise PkgError(
            "not_a_process",
            "the file does not describe a process (kind: Process)",
            file=str(doc.path),
        )
    return data["spec"]


def _new_id(spec: Any, element_id: Any, where: str) -> str:
    if not isinstance(element_id, str) or not ELEMENT_ID.match(element_id):
        raise PkgError(
            "element_id_invalid",
            f"{where}: id {element_id!r} is not valid",
            path=where,
            hint="id is a slug: lowercase latin letters, digits and '-', up to 63 characters",
        )
    existing = element_index(spec)
    if element_id in existing:
        raise PkgError(
            "element_id_taken",
            f"id {element_id!r} is already taken ({existing[element_id][0]})",
            path=existing[element_id][0],
            hint="an element id is unique within the process",
        )
    return element_id


def _insert(items: Any, item: Any, *, after: str | None, before: str | None, where: str) -> int:
    ids = [i.get("id") if isinstance(i, dict) else None for i in items]
    if after is not None and before is not None:
        raise PkgError("arguments_conflict", "use only one of --after and --before")
    if after is not None or before is not None:
        anchor = after if after is not None else before
        if anchor not in ids:
            raise PkgError("element_not_found", f"{where}: no element {anchor!r}", path=where)
        position = ids.index(anchor) + (1 if after is not None else 0)
    else:
        position = len(items)
    items.insert(position, item)
    return position


# --- операции -----------------------------------------------------------------


def _container(spec: Any, target: str, block: str | None) -> tuple[Any, str]:
    for stage in spec.get("stages") or []:
        if stage.get("id") == target:
            name = block or "steps"
            if name not in ("steps", "discretionary"):
                raise PkgError(
                    "block_invalid", f"a stage has blocks steps and discretionary, not {name!r}"
                )
            if name not in stage:
                stage[name] = CommentedSeq()
            return stage[name], f"stage {target}.{name}"
    node = _find_element(spec, target)
    candidates = [block] if block else ["do", "onCompensate"]
    for name in candidates:
        holder = node.get("try") if name == "do" and isinstance(node.get("try"), dict) else node
        if name in holder and isinstance(holder[name], list):
            return holder[name], f"{target}.{name}"
    raise PkgError(
        "block_not_found",
        f"element {target!r} has no block {'/'.join(candidates)}",
        path=target,
        hint="a step goes into a stage or into the do block of a step, branch or timer",
    )


def add_step(
    doc: Document,
    target: str,
    step: Any,
    *,
    after: str | None = None,
    before: str | None = None,
    block: str | None = None,
) -> dict[str, Any]:
    spec = _process_spec(doc)
    if not isinstance(step, dict):
        raise PkgError("fragment_invalid", "a step is a YAML object with id and one step kind")
    _new_id(spec, step.get("id"), "step")
    items, where = _container(spec, target, block)
    _insert(items, step, after=after, before=before, where=where)
    return {"added": step["id"], "into": where}


def add_stage(
    doc: Document, stage: Any, *, after: str | None = None, before: str | None = None
) -> dict[str, Any]:
    spec = _process_spec(doc)
    if not isinstance(stage, dict):
        raise PkgError("fragment_invalid", "a stage is a YAML object with id and steps")
    _new_id(spec, stage.get("id"), "stage")
    for element_id, _node, where in iter_elements(stage, "stage"):
        if element_id != stage["id"]:
            _new_id(spec, element_id, where)
    _insert(
        spec.setdefault("stages", CommentedSeq()), stage, after=after, before=before, where="stages"
    )
    return {"added": stage["id"], "into": "stages"}


def add_decision_row(
    doc: Document, table: str, row: Any, *, index: int | None = None
) -> dict[str, Any]:
    spec = _process_spec(doc)
    tables = [t for t in spec.get("decisions") or [] if t.get("id") == table]
    if not tables:
        raise PkgError(
            "decision_not_found",
            f"no decision table {table!r}",
            path="spec.decisions",
            hint="tables are declared in spec.decisions",
        )
    if not isinstance(row, dict) or "when" not in row or "then" not in row:
        raise PkgError("fragment_invalid", "a table row is an object {when: {…}, then: {…}}")
    inputs = {i.get("id") for i in tables[0].get("inputs") or []}
    outputs = {o.get("id") for o in tables[0].get("outputs") or []}
    unknown = [k for k in row["when"] if k not in inputs] + [
        k for k in row["then"] if k not in outputs
    ]
    if unknown:
        raise PkgError(
            "decision_column_unknown",
            f"table {table!r} has no columns {unknown}",
            path=f"decisions[{table}]",
            hint=f"inputs: {sorted(inputs)}, outputs: {sorted(outputs)}",
        )
    rules = tables[0].setdefault("rules", CommentedSeq())
    position = len(rules) if index is None else index
    if not 0 <= position <= len(rules):
        raise PkgError("index_out_of_range", f"--index {index}: the table has {len(rules)} rows")
    rules.insert(position, row)
    return {"table": table, "row": position}


def add_rule(
    doc: Document,
    *,
    table: str | None = None,
    row: Any = None,
    on_event: Any = None,
    index: int | None = None,
) -> dict[str, Any]:
    """Правило процесса: строка таблицы решений (--table) или реакция на событие (onEvent)."""
    if table is not None:
        return add_decision_row(doc, table, row, index=index)
    spec = _process_spec(doc)
    if not isinstance(on_event, dict) or "on" not in on_event or "do" not in on_event:
        raise PkgError("fragment_invalid", "an event reaction is an object {on: {…}, do: [steps]}")
    for element_id, _node, where in iter_elements(on_event, "onEvent"):
        _new_id(spec, element_id, where)
    handlers = spec.setdefault("onEvent", CommentedSeq())
    position = len(handlers) if index is None else index
    handlers.insert(position, on_event)
    return {"onEvent": position}


def add_form_field(
    doc: Document,
    step_id: str,
    name: str,
    schema: Any,
    *,
    required: bool = False,
    label: str | None = None,
) -> dict[str, Any]:
    spec = _process_spec(doc)
    step = _find_element(spec, step_id)
    human = step.get("human")
    if not isinstance(human, dict):
        raise PkgError(
            "not_a_human_step",
            f"step {step_id!r} is not a human step (human)",
            path=step_id,
            hint="only a human step has a form",
        )
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        raise PkgError("field_name_invalid", f"field name {name!r}: latin letters, digits and '_'")
    if not isinstance(schema, dict):
        raise PkgError(
            "fragment_invalid", "a field schema is a JSON Schema object, e.g. {type: string}"
        )
    form = human.setdefault("form", CommentedMap([("schema", CommentedMap([("type", "object")]))]))
    form_schema = form.setdefault("schema", CommentedMap([("type", "object")]))
    properties = form_schema.setdefault("properties", CommentedMap())
    if name in properties:
        raise PkgError(
            "field_taken",
            f"field {name!r} already exists in the form of step {step_id!r}",
            path=step_id,
        )
    properties[name] = schema
    if required:
        form_schema.setdefault("required", CommentedSeq()).append(name)
    uischema = form.get("uischema")
    if isinstance(uischema, dict) and isinstance(uischema.get("elements"), list):
        control = CommentedMap([("type", "Control"), ("scope", f"#/properties/{name}")])
        if label:
            control["label"] = label
        uischema["elements"].append(control)
    return {"step": step_id, "field": name}


_PATH_PART = re.compile(r"([^.\[\]]+)|\[([^\]]+)\]")


def _parse_path(path: str) -> list[str | int]:
    parts: list[str | int] = []
    for key, index in _PATH_PART.findall(path):
        if key:
            parts.append(key)
        elif re.fullmatch(r"-?\d+", index):
            parts.append(int(index))
        else:
            parts.append(index.strip("'\""))
    if not parts:
        raise PkgError("path_invalid", f"empty path {path!r}")
    return parts


def set_value(doc: Document, path: str, value: Any) -> dict[str, Any]:
    """Путь от корня документа: spec.stages[go-no-go].exit, spec.decisions[0].hitPolicy.
    В скобках — индекс или id элемента списка."""
    parts = _parse_path(path)
    node = doc.data
    for depth, part in enumerate(parts[:-1]):
        node = _step_into(node, part, parts[: depth + 1], create=isinstance(parts[depth + 1], str))
    last = parts[-1]
    if isinstance(node, list):
        position = _list_position(node, last, path)
        old = node[position]
        node[position] = value
    elif isinstance(node, dict) and isinstance(last, str):
        old = node.get(last)
        node[last] = value
    else:
        raise PkgError(
            "path_invalid", f"{path}: cannot write into {type(node).__name__}", path=path
        )
    return {"path": path, "old": to_plain(old), "new": to_plain(value)}


def _list_position(items: list, part: str | int, path: str) -> int:
    if isinstance(part, int):
        if -len(items) <= part < len(items):
            return part % len(items)
        raise PkgError(
            "path_invalid", f"{path}: index {part} is out of a list of {len(items)}", path=path
        )
    for position, item in enumerate(items):
        if isinstance(item, dict) and item.get("id") == part:
            return position
    raise PkgError("path_invalid", f"{path}: the list has no element with id {part!r}", path=path)


def _step_into(node: Any, part: str | int, trail: list, *, create: bool) -> Any:
    where = ".".join(str(p) for p in trail)
    if isinstance(node, list):
        return node[_list_position(node, part, where)]
    if isinstance(node, dict) and isinstance(part, str):
        if part not in node:
            if not create:
                raise PkgError("path_invalid", f"{where}: no key", path=where)
            node[part] = CommentedMap()
        return node[part]
    raise PkgError("path_invalid", f"{where}: neither an object nor a list", path=where)


# --- переименование -----------------------------------------------------------


def _cel_rename(text: str, old: str, new: str) -> str:
    text = re.sub(
        rf"(?<![\w-])(stage|milestone|timer)\.{re.escape(old)}(?![\w-])", rf"\1.{new}", text
    )
    return re.sub(
        rf"(stage|milestone|timer)\[(['\"]){re.escape(old)}\2\]", rf"\1[\g<2>{new}\g<2>]", text
    )


def _rename_refs(node: Any, old: str, new: str, *, key: str | None = None) -> int:
    """Ссылки на элемент внутри процесса: id, decide.table, compensate, выражения CEL."""
    changed = 0
    if isinstance(node, dict):
        for k in list(node.keys()):
            value = node[k]
            if k in ("id", "table") and value == old:
                node[k] = new
                changed += 1
            elif k == "migrations":
                continue  # карты миграций — история прежних версий
            elif k == "compensate" and isinstance(value, list):
                for i, item in enumerate(value):
                    if item == old:
                        value[i] = new
                        changed += 1
            elif isinstance(value, str) and key != "data":
                renamed = _cel_rename(value, old, new)
                if renamed != value:
                    node[k] = type(value)(renamed) if type(value) is not str else renamed
                    changed += 1
            elif k != "data":
                changed += _rename_refs(value, old, new, key=k)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            if isinstance(item, str):
                renamed = _cel_rename(item, old, new)
                if renamed != item:
                    node[i] = renamed
                    changed += 1
            else:
                changed += _rename_refs(item, old, new, key=key)
    return changed


def _rename_key(mapping: Any, old: str, new: str) -> bool:
    if not isinstance(mapping, dict) or old not in mapping:
        return False
    position = list(mapping.keys()).index(old)
    value = mapping.pop(old)
    if hasattr(mapping, "insert"):
        mapping.insert(position, new, value)
        comments = getattr(mapping, "ca", None)
        if comments is not None and old in comments.items:
            comments.items[new] = comments.items.pop(old)
    else:
        mapping[new] = value
    return True


def _rename_in_test(test: Any, old: str, new: str) -> int:
    changed = 0
    given = test.get("given") or {}
    if given.get("stage") == old:
        given["stage"] = new
        changed += 1
    for answers in ((test.get("mocks") or {}).get("skills") or {}).values():
        changed += _rename_steps(answers, old, new)
    for answers in ((test.get("mocks") or {}).get("agents") or {}).values():
        changed += _rename_steps(answers, old, new)
    changed += _rename_steps((test.get("mocks") or {}).get("recall") or [], old, new)
    for step in test.get("steps") or []:
        for verb in ("complete", "approve"):
            if isinstance(step.get(verb), dict) and step[verb].get("step") == old:
                step[verb]["step"] = new
                changed += 1
        if step.get("advance") == f"until:{old}":
            step["advance"] = f"until:{new}"
            changed += 1
        # наблюдение, привязанное к задаче шага (CP-ADR-0063 Ж5)
        if isinstance(step.get("emit"), dict) and step["emit"].get("task") == old:
            step["emit"]["task"] = new
            changed += 1
        expect = step.get("expect")
        if isinstance(expect, dict):
            changed += _rename_key(expect.get("stages"), old, new)
            changed += _rename_key(expect.get("sla"), old, new)
            changed += _rename_steps(expect.get("tasks") or [], old, new)
            changed += _rename_steps(expect.get("rules") or [], old, new)
            for timer in expect.get("timers") or []:
                if timer.get("id") == old:
                    timer["id"] = new
                    changed += 1
            for name, holder in (("milestones", expect), ("recalled", expect.get("memory") or {})):
                items = holder.get(name) or []
                for i, item in enumerate(items):
                    if item == old:
                        items[i] = new
                        changed += 1
    return changed


def _rename_steps(items: Any, old: str, new: str) -> int:
    changed = 0
    for item in items or []:
        if isinstance(item, dict) and item.get("step") == old:
            item["step"] = new
            changed += 1
    return changed


def _flow_map(pairs: list[tuple[str, Any]]) -> Any:
    mapping = CommentedMap(pairs)
    if hasattr(mapping, "fa"):
        mapping.fa.set_flow_style()
    return mapping


def _record_migration(spec: Any, old: str, new: str) -> dict[str, Any]:
    """Карта миграции открытых экземпляров: запись to == текущая версия дополняется,
    иначе версия поднимается и заводится запись {from: v, to: v+1, policy: migrate}."""
    version = int(spec.get("version") or 1)
    migrations = spec.get("migrations")
    entry = next((m for m in migrations or [] if int(m.get("to", 0)) == version), None)
    if entry is None:
        version += 1
        spec["version"] = version
        # карта к следующей версии могла быть написана заранее — дополняется она же
        entry = next(
            (
                m
                for m in migrations or []
                if int(m.get("from", 0)) == version - 1 and int(m.get("to", 0)) == version
            ),
            None,
        )
    if entry is None:
        if migrations is None:
            spec["migrations"] = migrations = CommentedSeq()
        entry = _flow_map(
            [("from", version - 1), ("to", version), ("policy", "migrate"), ("map", _flow_map([]))]
        )
        migrations.append(entry)
    mapping = entry.setdefault("map", _flow_map([]))
    sources = [k for k, v in mapping.items() if v == old]
    for source in sources:  # цепочка a→b, затем b→c: в карте сразу a→c
        mapping[source] = new
    if not sources:
        mapping[old] = new
    return {"version": version, "migration": f"{entry.get('from')}→{entry.get('to')}"}


def package_root(path: Path) -> Path | None:
    for parent in [path.parent, *path.parents][:4]:
        if (parent / "package.yaml").exists():
            return parent
    return None


def layout_path(process_file: Path, key: str) -> Path:
    """<пакет>/.layout/<ключ процесса>.json; вне пакета — рядом с файлом процесса."""
    root = package_root(process_file)
    return (root if root is not None else process_file.parent) / LAYOUT_DIR / f"{key}.json"


def _dump_layout(layout: Any) -> str:
    return json.dumps(layout, ensure_ascii=False, indent=2) + "\n"


def _layout_rename(path: Path, old: str, new: str) -> bool:
    if not path.exists():
        return False
    layout = json.loads(path.read_text(encoding="utf-8"))
    nodes = layout.get("nodes") if isinstance(layout, dict) else None
    if not isinstance(nodes, dict) or old not in nodes:
        return False
    layout["nodes"] = {(new if k == old else k): v for k, v in nodes.items()}
    path.write_text(_dump_layout(layout), encoding="utf-8")
    return True


def _package_tests(root: Path | None, process_key: str) -> list[Document]:
    if root is None or not (root / "tests").is_dir():
        return []
    tests = []
    for path in sorted((root / "tests").glob("*.test.yaml")):
        doc = Document.load(path)
        if isinstance(doc.data, dict) and doc.data.get("process") == process_key:
            tests.append(doc)
    return tests


def rename_element(
    doc: Document,
    old: str,
    new: str,
    *,
    migration: bool = True,
    tests: list[Document] | None = None,
) -> dict[str, Any]:
    """Переименовать элемент процесса: id, ссылки в процессе и тестах пакета, карта
    migrations (экземпляры переходят на новый id), координаты в раскладке."""
    spec = _process_spec(doc)
    index = element_index(spec)
    if old not in index:
        raise PkgError("element_not_found", f"the process has no element {old!r}", path=old)
    _new_id(spec, new, "--to")
    changed = _rename_refs(spec, old, new)
    result: dict[str, Any] = {"renamed": {old: new}, "references": changed}
    if migration:
        result.update(_record_migration(spec, old, new))
    result["tests"] = [str(t.path) for t in tests or [] if _rename_in_test(t.data, old, new)]
    return result


def rename_object(package_dir: Path, kind: str, old: str, new: str) -> dict[str, Any]:
    """Переименовать объект пакета: key в файле, имя файла, запись renames в package.yaml
    (план переносит объект, а не удаляет и создаёт); у процесса — тесты и раскладка."""
    folder = FOLDERS.get(kind)
    if folder is None:
        raise PkgError(
            "kind_unknown",
            f"kind {kind!r} is not a package object",
            hint=f"kinds: {sorted(FOLDERS)}",
        )
    source = next(
        (p for p in sorted((package_dir / folder).glob("*.yaml")) if _is_object(p, kind, old)), None
    )
    if source is None:
        raise PkgError("object_not_found", f"no {kind}/{old} in {package_dir / folder}")
    manifest = Document.load(package_dir / "package.yaml")
    doc = Document.load(source)
    doc.data["key"] = new
    renames = manifest.data["spec"].setdefault("renames", CommentedSeq())
    renames.append(_flow_map([("kind", kind), ("from", old), ("to", new)]))
    changed = [manifest, doc]
    tests: list[Document] = []
    if kind == "Process":
        tests = _package_tests(package_dir, old)
        for test in tests:
            test.data["process"] = new
        changed += tests
    _validate_all(changed)
    target = source.with_name(f"{new}.yaml") if source.stem == old else source
    for item in changed:
        item.save()
    if target != source:
        source.rename(target)
    moved_layout = False
    if kind == "Process":
        old_layout = package_dir / LAYOUT_DIR / f"{old}.json"
        if old_layout.exists():
            old_layout.rename(package_dir / LAYOUT_DIR / f"{new}.json")
            moved_layout = True
    return {
        "renamed": f"{kind}/{old} → {kind}/{new}",
        "file": str(target),
        "renames": len(renames),
        "tests": [str(t.path) for t in tests],
        "layout": moved_layout,
    }


def _is_object(path: Path, kind: str, key: str) -> bool:
    try:
        data = Document.load(path).data
    except PkgError:
        return False
    return isinstance(data, dict) and data.get("kind") == kind and data.get("key") == key


# --- проверка перед записью ---------------------------------------------------


def _validator(name: str) -> Any:
    return schema_module.validator(name)


def validate(doc: Document) -> None:
    data = to_plain(doc.data)
    is_test = (
        isinstance(data, dict)
        and "apiVersion" not in data
        and ("process" in data or "subject" in data)
    )
    validator = _validator(schema_module.TEST if is_test else schema_module.OBJECT)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        error = errors[0]
        where = "/".join(str(p) for p in error.absolute_path) or "(root)"
        raise PkgError(
            "schema_violation",
            f"{where}: {error.message}",
            path=where,
            file=str(doc.path),
            hint=f"the edit was not written; more errors: {len(errors) - 1}"
            if len(errors) > 1
            else "the edit was not written",
        )
    if isinstance(data, dict) and data.get("kind") == "Process":
        duplicates = {k: v for k, v in element_index(data.get("spec") or {}).items() if len(v) > 1}
        if duplicates:
            element_id, places = next(iter(duplicates.items()))
            raise PkgError(
                "element_id_taken",
                f"id {element_id!r} is repeated: {', '.join(places)}",
                path=places[1],
                file=str(doc.path),
                hint="an element id is unique within the process",
            )


def _validate_all(docs: list[Document]) -> None:
    for doc in docs:
        validate(doc)


# --- CLI ----------------------------------------------------------------------


def _fragment(args: argparse.Namespace, name: str, what: str) -> Any:
    value = getattr(args, name)
    if value is None:
        return None
    if value.startswith("@"):
        value = Path(value[1:]).read_text(encoding="utf-8")
    return parse_fragment(value, what)


def _run(args: argparse.Namespace) -> tuple[dict[str, Any], list[tuple[Path, str, str]]]:
    """Выполнить операцию; вернуть результат и изменения (файл, было, стало) без записи."""
    if args.command == "rename" and args.package:
        if args.dry_run:
            raise PkgError(
                "dry_run_unsupported", "renaming an object moves files — --dry-run is not supported"
            )
        return rename_object(Path(args.package), args.kind or "Process", args.old, args.new), []
    doc = Document.load(args.file)
    extra: list[Document] = []
    if args.command == "add-step":
        result = add_step(
            doc,
            args.target,
            _fragment(args, "step", "--step"),
            after=args.after,
            before=args.before,
            block=args.block,
        )
    elif args.command == "add-stage":
        result = add_stage(
            doc, _fragment(args, "stage", "--stage"), after=args.after, before=args.before
        )
    elif args.command == "add-decision-row":
        result = add_decision_row(
            doc, args.table, _fragment(args, "row", "--row"), index=args.index
        )
    elif args.command == "add-rule":
        result = add_rule(
            doc,
            table=args.table,
            row=_fragment(args, "row", "--row"),
            on_event=_fragment(args, "on_event", "--on-event"),
            index=args.index,
        )
    elif args.command == "add-form-field":
        result = add_form_field(
            doc,
            args.step_id,
            args.name,
            _fragment(args, "schema", "--schema"),
            required=args.required,
            label=args.label,
        )
    elif args.command == "rename":
        if not args.file:
            raise PkgError(
                "arguments_missing", "rename: --file (element) or --package (object) is required"
            )
        extra = _package_tests(package_root(doc.path), str(doc.data.get("key")))
        result = rename_element(
            doc, args.old, args.new, migration=not args.no_migration, tests=extra
        )
        extra = [t for t in extra if str(t.path) in result["tests"]]
    elif args.command == "set":
        value = args.value if args.string else parse_fragment(args.value, "--value")
        result = set_value(doc, args.path, value)
    else:  # pragma: no cover - argparse не пропустит
        raise PkgError("command_unknown", args.command)
    _validate_all([doc, *extra])
    changes = [(d.path, d.text, d.dumps()) for d in (doc, *extra)]
    if not args.dry_run:
        for d in (doc, *extra):
            d.save()
        if args.command == "rename":  # раскладка — после записи логики
            result["layout"] = _layout_rename(
                layout_path(doc.path, str(doc.data.get("key"))), args.old, args.new
            )
    return result, changes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="package-sdk edit",
        # первая строка — назначение команды, как у остальных: со строчной и без точки
        description="edit package files preserving the file style\n\n"
        + (__doc__ or "").split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true", help="result and errors as JSON")
    parser.add_argument("--dry-run", action="store_true", help="show the diff, write nothing")
    sub = parser.add_subparsers(dest="command", required=True)

    def command(
        name: str, help_text: str, *, file_required: bool = True
    ) -> argparse.ArgumentParser:
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument(
            "--file", required=file_required, help="process file (processes/<key>.yaml)"
        )
        cmd.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        cmd.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
        return cmd

    fragment_help = "YAML (flow or block); @file reads it from a file"
    cmd = command("add-step", "add a step to a stage or a step block")
    cmd.add_argument(
        "--in",
        dest="target",
        required=True,
        help="id of a stage, a step with do, a fork branch or a timer",
    )
    cmd.add_argument(
        "--block", help="block name: steps|discretionary for a stage, do|onCompensate for a step"
    )
    cmd.add_argument("--step", required=True, help=fragment_help)
    cmd.add_argument("--after")
    cmd.add_argument("--before")
    cmd = command("add-stage", "add a stage")
    cmd.add_argument("--stage", required=True, help=fragment_help)
    cmd.add_argument("--after")
    cmd.add_argument("--before")
    cmd = command("add-decision-row", "add a decision table row")
    cmd.add_argument("--table", required=True)
    cmd.add_argument("--row", required=True, help=fragment_help)
    cmd.add_argument("--index", type=int, help="row position (default: at the end)")
    cmd = command("add-rule", "add a rule: a table row (--table) or an event reaction (--on-event)")
    cmd.add_argument("--table")
    cmd.add_argument("--row", help=fragment_help)
    cmd.add_argument("--on-event", dest="on_event", help=fragment_help)
    cmd.add_argument("--index", type=int)
    cmd = command("add-form-field", "add a field to the form of a human step")
    cmd.add_argument("--step", dest="step_id", required=True)
    cmd.add_argument("--name", required=True)
    cmd.add_argument("--schema", required=True, help=fragment_help)
    cmd.add_argument("--required", action="store_true")
    cmd.add_argument("--label", help="label in uischema, if the form has uischema.elements")
    cmd = command("rename", "rename a process element or a package object", file_required=False)
    cmd.add_argument(
        "--package", help="package directory — rename an object (renames in package.yaml)"
    )
    cmd.add_argument("--kind", help="object kind for --package (default: Process)")
    cmd.add_argument("--from", dest="old", required=True)
    cmd.add_argument("--to", dest="new", required=True)
    cmd.add_argument(
        "--no-migration",
        action="store_true",
        help="do not append migrations (the process is not published yet)",
    )
    cmd = command("set", "write a value at a path")
    cmd.add_argument(
        "--path", required=True, help="spec.stages[go-no-go].exit; an index or an id in brackets"
    )
    cmd.add_argument("--value", required=True, help="YAML value")
    cmd.add_argument(
        "--string", action="store_true", help="the value is a string as is, without YAML parsing"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result, changes = _run(args)
    except PkgError as error:
        if args.json:
            print(json.dumps({"ok": False, "error": error.as_dict()}, ensure_ascii=False))
        else:
            print(
                f"error: {error.code}: {error.message}"
                + (f" (hint: {error.hint})" if error.hint else ""),
                file=sys.stderr,
            )
        return 1
    if args.dry_run:
        for path, before, after in changes:
            sys.stdout.writelines(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    f"a/{path}",
                    f"b/{path}",
                )
            )
    if args.json:
        print(
            json.dumps(
                {"ok": True, **result, "files": [str(p) for p, b, a in changes if b != a]},
                ensure_ascii=False,
                default=str,
            )
        )
    elif not args.dry_run:
        print("ok:", json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "Document",
    "PkgError",
    "Style",
    "add_decision_row",
    "add_form_field",
    "add_rule",
    "add_stage",
    "add_step",
    "element_index",
    "iter_elements",
    "layout_path",
    "merge_text",
    "parse_fragment",
    "rename_element",
    "rename_object",
    "set_value",
    "to_plain",
    "validate",
]
