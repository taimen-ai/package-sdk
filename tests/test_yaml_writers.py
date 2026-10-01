"""Писатели файла пакета кавычат строку, которую хоть один читатель прочтёт не строкой
(TASK-001253).

Файл пакета читают трое: ядро при публикации (``yaml12``, загрузчик пакета), инструменты
на YAML 1.1 (PyYAML ``SafeLoader``) и редактор автора по схеме core YAML 1.2
(``yaml-language-server``; здесь — ruamel с YAML 1.2 без правил пакета). Таблица строк
пишется каждым писателем пакета — ``edit`` (ruamel), ``export``/``pull`` новым файлом
(``apply.dump_document``), заготовки (``scaffold``) и установка MCP (``yaml12.dump``) — и
читается каждым читателем: строка остаётся той же строкой. Обычные строки пишутся без
кавычек, чтобы дифф правки реального пакета не рос.
"""

from __future__ import annotations

import difflib
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml
from ruamel.yaml import YAML

from package_sdk import edit, export, scaffold, yaml12
from package_sdk.apply import dump_document
from package_sdk.model import API_VERSION, _yaml12_loader, load_package, substitute
from tests.test_export import ENV, FakeCore, _as_core_stores, _copy

# Строки, которые хоть один читатель без кавычек прочтёт не строкой.
AMBIGUOUS = [
    "012",  # ядро и YAML 1.2 — 12, YAML 1.1 — 10
    "0o12",  # ядро и YAML 1.2 — 10
    "0x1A",
    "-0x1A",  # YAML 1.1 — -26
    "1_000",  # YAML 1.1 — 1000
    "1:30",  # YAML 1.1 — 90
    "1e3",  # YAML 1.2 — 1000.0
    ".inf",
    "2026-09-30",  # YAML 1.1 — дата
    "yes",  # YAML 1.1 — True
    "on",
    "null",
    "~",
    "true",
    "",
    "=",  # YAML 1.1 — !!value
    "<<",  # YAML 1.1 — ключ слияния
    "y",  # YAML 1.1 по спецификации (go-yaml v2) — True; PyYAML — строка
    "Y",
    "n",
    "N",
]
# Обычные строки — без кавычек у всех писателей.
WORDS = ["claim", "Customer claim", "refund-approved", "x:y", "yy", "1.2.3", "v1"]
STRINGS = AMBIGUOUS + WORDS


def _ruamel_12(text: str) -> Any:
    return YAML(typ="safe", pure=True).load(text)


class _Yaml11Spec(yaml.SafeLoader):
    """YAML 1.1 по спецификации (yaml.org/type/bool): ``y``/``n`` — булевы, как у go-yaml v2."""

    bool_values: ClassVar[dict[str, bool]] = {**yaml.SafeLoader.bool_values, "y": True, "n": False}


_Yaml11Spec.add_implicit_resolver(yaml12.BOOL_TAG, re.compile(r"^(?:y|Y|n|N)$"), list("yYnN"))

READERS: dict[str, Callable[[str], Any]] = {
    "ядро": lambda text: yaml.load(text, Loader=_yaml12_loader()),
    "YAML 1.1": yaml.safe_load,
    "YAML 1.1 (спецификация)": lambda text: yaml.load(text, Loader=_Yaml11Spec),
    "YAML 1.2": _ruamel_12,
}


def _core_neighbour() -> Callable[[str], Any] | None:
    """Загрузчик самого ядра, когда сосед control-plane есть и знает YAML 1.2 (TASK-001247)."""
    try:
        from control_plane.domain import package_source as core
    except ImportError:
        return None
    if not hasattr(core, "MAX_INT_DIGITS"):
        return None
    return lambda text: core.load_yaml(text)[0]


if (_neighbour := _core_neighbour()) is not None:
    READERS["ядро (control-plane)"] = _neighbour


def _payload(value: str) -> dict[str, Any]:
    """Строка значением, элементом списка и ключом."""
    return {"k": value, "list": [value, "word"], "keys": {value: "key"}}


def _by_edit(value: str) -> tuple[str, Callable[[Any], Any]]:
    document = edit.Document.parse("k: x\nlist: [a]\nkeys: {}\n")
    document.data["k"] = value
    document.data["list"] = [value, "word"]
    document.data["keys"] = {value: "key"}
    return document.dumps(), lambda read: read


def _by_dump_document(value: str) -> tuple[str, Callable[[Any], Any]]:
    document = {"apiVersion": API_VERSION, "kind": "TaskType", "key": "x", "spec": _payload(value)}
    return dump_document(document, "object.schema.json"), lambda read: read["spec"]


def _by_scaffold(value: str) -> tuple[str, Callable[[Any], Any]]:
    return scaffold._dump(_payload(value)), lambda read: read


def _by_yaml12_dump(value: str) -> tuple[str, Callable[[Any], Any]]:
    return yaml12.dump(_payload(value), sort_keys=False), lambda read: read


WRITERS: dict[str, Callable[[str], tuple[str, Callable[[Any], Any]]]] = {
    "edit": _by_edit,
    "export/pull (dump_document)": _by_dump_document,
    "scaffold": _by_scaffold,
    "mcp (yaml12.dump)": _by_yaml12_dump,
}


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize("value", STRINGS, ids=lambda v: repr(v))
def test_a_written_string_is_read_back_as_the_same_string(
    value: str, writer: str, reader: str
) -> None:
    text, extract = WRITERS[writer](value)
    assert extract(READERS[reader](text)) == _payload(value), text


@pytest.mark.parametrize("value", AMBIGUOUS, ids=lambda v: repr(v))
def test_the_table_is_ambiguous_without_quotes(value: str) -> None:
    """Каждая строка таблицы без кавычек хоть одним читателем читается не строкой —
    иначе её кавычки ничего не проверяют."""
    misread = [name for name, read in READERS.items() if not _reads_string(read, value)]
    assert misread, f"{value!r} без кавычек все читают строкой"
    assert not yaml12.plain_is_string(value)


def _reads_string(read: Callable[[str], Any], value: str) -> bool:
    try:
        document = read(f"k: {value}\n")
    except Exception:  # ядро отвергает .inf и =: это тоже «не строка»
        return False
    return isinstance(document, dict) and document.get("k") == value


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize("value", WORDS)
def test_an_ordinary_string_is_written_without_quotes(value: str, writer: str) -> None:
    assert yaml12.plain_is_string(value)
    text, _extract = WRITERS[writer](value)
    assert f"k: {value}\n" in text


# --- export и pull дважды -------------------------------------------------------------


def _with_codes(spec: dict[str, Any], codes: list[str]) -> dict[str, Any]:
    spec = dict(spec)
    data = dict(spec["data"])
    data["properties"] = {
        **data["properties"],
        "code": {"type": "string", "enum": list(codes), "default": codes[0]},
    }
    spec["data"] = data
    return spec


def _codes(package_dir: Path, key: str) -> list[str]:
    obj = next(o for o in load_package(package_dir).objects if o.key == key)
    return list(obj.spec["data"]["properties"]["code"]["enum"])


def test_export_twice_keeps_strings_strings(tmp_path: Path) -> None:
    """Новый процесс (dump_document), повторная выгрузка без изменений и с правкой консоли
    (дерево edit): строки остаются строками у ядра, YAML 1.1 и YAML 1.2."""
    package_dir = _copy(tmp_path, "invoice-payment")
    _copy(tmp_path, "notify")
    spec = _with_codes(_as_core_stores(package_dir, "Process", "invoice-payment"), AMBIGUOUS)
    core = FakeCore()
    core.publish("Process", "invoice-copy", spec)

    def export_once() -> export.Exported:
        body = export.fetch(core, {}, "Process", "invoice-copy", None)
        return export.export_object(package_dir, "Process", "invoice-copy", body, env=ENV)

    def check_file(expected: list[str]) -> str:
        text = (package_dir / "processes" / "invoice-copy.yaml").read_text(encoding="utf-8")
        assert _codes(package_dir, "invoice-copy") == expected
        for name, read in READERS.items():
            assert read(text)["spec"]["data"]["properties"]["code"]["enum"] == expected, name
        obj = next(o for o in load_package(package_dir).objects if o.key == "invoice-copy")
        assert substitute(obj.spec, ENV) == core.objects[("Process", "invoice-copy")]["spec"]
        return text

    assert export_once().created
    first = check_file(AMBIGUOUS)

    assert not export_once().changed  # вторая выгрузка того же — файл не меняется
    assert check_file(AMBIGUOUS) == first

    codes = [*AMBIGUOUS, "0o17", "1e9", "no"]
    core.publish("Process", "invoice-copy", _with_codes(spec, codes), version=2)
    assert export_once().changed  # правка консоли — деревом edit
    check_file(codes)
    assert not export_once().changed


def test_pull_twice_writes_the_same_file() -> None:
    """pull (export видов каталога): файл, прочитанный ядром и записанный снова, тот же."""
    document = {
        "apiVersion": API_VERSION,
        "kind": "TaskType",
        "key": "x",
        "spec": _payload("0o12") | {"codes": STRINGS},
    }
    first = dump_document(document, "object.schema.json")
    again = dump_document(READERS["ядро"](first), "object.schema.json")
    assert again == first
    for name, read in READERS.items():
        assert read(first)["spec"]["codes"] == STRINGS, name


# --- правка реального пакета ----------------------------------------------------------

CLAIMS = Path(__file__).parents[1] / "examples" / "claims" / "claims"


def _changed(before: str, after: str) -> list[str]:
    return [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="")
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]


@pytest.mark.parametrize(
    ("value", "written"),
    [
        ("Customer claim v2", "displayName: Customer claim v2"),
        ("2026-09-30", "displayName: '2026-09-30'"),
        ("1e3", "displayName: '1e3'"),
    ],
)
def test_editing_the_example_package_changes_one_line(
    tmp_path: Path, value: str, written: str
) -> None:
    package_dir = tmp_path / "claims"
    shutil.copytree(CLAIMS, package_dir)
    path = package_dir / "processes" / "claim.yaml"
    before = path.read_text(encoding="utf-8")
    document = edit.Document.load(path)
    edit.set_value(document, "spec.displayName", value)
    after = document.dumps()
    assert _changed(before, after) == ["-  displayName: Customer claim", f"+  {written}"]
    assert READERS["YAML 1.2"](after)["spec"]["displayName"] == value


def test_example_package_files_round_trip_unchanged() -> None:
    """Загрузка и запись без правки дают файл байт в байт: кавычки новых строк не трогают
    строки, которые уже стоят в файле."""
    for path in sorted(CLAIMS.rglob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        assert edit.Document.parse(text, path).dumps() == text, path


def test_a_plain_string_already_in_the_file_stays_plain() -> None:
    """``on:`` и ``1_000`` без кавычек ядро читает строками: правка соседнего значения их
    не кавычит, а новое значение с той же записью — кавычит."""
    text = "on: x\nversion: 1_000\nk: a\n"
    document = edit.Document.parse(text)
    document.data["k"] = "1_000"
    assert document.dumps() == "on: x\nversion: 1_000\nk: '1_000'\n"


def test_a_value_given_on_the_command_line_is_quoted() -> None:
    """``edit set --value 1e3``: фрагмент CLI — новое значение, пишется в кавычках."""
    document = edit.Document.parse("k: a\n")
    edit.set_value(document, "k", edit.parse_fragment("1e3", "--value"))
    assert document.dumps() == "k: '1e3'\n"


@pytest.mark.parametrize("writer", WRITERS)
def test_a_new_on_key_is_double_quoted_and_its_value_single_quoted(writer: str) -> None:
    """Ключ ``on`` правил и обработчиков (TAI-ADR-0054) YAML 1.1 читает True: новый пишется
    ``"on":``, как в файлах формата, одинаково у всех писателей; значение ``on`` — ``'on'``."""
    text, _extract = WRITERS[writer]("on")
    assert '"on": key' in text
    assert "k: 'on'" in text


def test_a_rule_added_by_edit_has_a_quoted_on_key() -> None:
    """``edit add-rule --on-event``: фрагмент CLI с ``on:`` без кавычек — новый ключ, он
    пишется ``"on":``."""
    document = edit.Document.parse("k: a\n")
    document.data["rule"] = edit.parse_fragment(
        "{on: {observation: x.y}, do: [{id: s, set: {mode: on}}]}", "--on-event"
    )
    assert document.dumps() == (
        "k: a\nrule: {\"on\": {observation: x.y}, do: [{id: s, set: {mode: 'on'}}]}\n"
    )


def test_an_existing_plain_on_key_stays_as_it_was() -> None:
    """``on:`` без кавычек, который уже стоит в файле, правка соседнего ключа не трогает."""
    text = "on: {observation: x.y}\ndo: []\n"
    document = edit.Document.parse(text)
    document.data["do"] = [{"id": "s"}]
    assert document.dumps() == "on: {observation: x.y}\ndo:\n  - id: s\n"
