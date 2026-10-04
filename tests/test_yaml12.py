"""Скаляры файла пакета читаются так же, как их читает ядро при публикации (TASK-001251).

Одна таблица строк YAML прогоняется через загрузчик пакета (PyYAML: check, test, plan),
дерево правки (ruamel: edit) и — когда сосед ``control-plane`` доступен и его загрузчик
знает целые YAML 1.2 (TASK-001247) — через загрузчик самого ядра.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
import yaml

from package_sdk import edit, yaml12
from package_sdk.model import PackageError, _read_yaml, _yaml12_loader

REFUSED = object()

LONG = "1" * (yaml12.MAX_INT_DIGITS + 1)
LONGEST = "1" * yaml12.MAX_INT_DIGITS

# Строка YAML → значение, как его читает ядро; REFUSED — ядро отвергает файл.
TABLE: list[tuple[str, Any]] = [
    ("012", 12),
    ("0o12", 10),
    ("0x1A", 26),
    ("1_000", "1_000"),
    ("1:30", "1:30"),
    (".inf", REFUSED),
    ("-.inf", REFUSED),
    (".nan", REFUSED),
    (LONG, REFUSED),
    (f"-{LONG}", REFUSED),
    (LONGEST, int(LONGEST)),
    ("+1", 1),
    ("-0", 0),
    ("1e3", "1e3"),
    ("1.0e+3", 1000.0),
    ("1e999", "1e999"),
    ("~", None),
    ("null", None),
    ("Null", None),
    ("NULL", None),
    ("0b101", "0b101"),
    ("-0x1A", "-0x1A"),
    ("+0o7", "+0o7"),
    ("1:30.5", 90.5),
    ("1_000.5", 1000.5),
    ("2026-09-30", "2026-09-30"),
    ("yes", "yes"),
    ("on", "on"),
    ("true", True),
    ("FALSE", False),
    ("{012: a}", {12: "a"}),
    ("!!int x", REFUSED),
    ("!!int 0b1", REFUSED),
    ("!!int 1:30", REFUSED),
    ("!!bool yes", REFUSED),
    ("!!float x", REFUSED),
    ("!!float .inf", REFUSED),
    ("!!float 1e3", 1000.0),
    ("!!float 1_0", 10.0),
    ("!!null x", None),
    ("!!timestamp 2026-09-30", REFUSED),
    ("!!binary aGk=", REFUSED),
    ("=", REFUSED),
    ('"\\ud800"', REFUSED),
]

IDS = [text if len(text) < 30 else f"{text[:12]}…({len(text)})" for text, _ in TABLE]


def _typed(value: Any) -> Any:
    """Значение с типами: 1, 1.0 и True различаются, ruamel-обёртки сняты."""
    if isinstance(value, dict):
        return {_typed(k): _typed(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_typed(v) for v in value]
    if isinstance(value, bool) or type(value).__name__ == "ScalarBoolean":
        return ("bool", bool(value))
    if value is None:
        return ("NoneType", None)
    if isinstance(value, int):
        return ("int", int(value))
    if isinstance(value, float):
        return ("float", float(value))
    if isinstance(value, str):
        return ("str", str(value))
    return (type(value).__name__, value)


def _document(text: str) -> str:
    return f"k: {text}\nother: 1\n"


@pytest.mark.parametrize(("text", "expected"), TABLE, ids=IDS)
def test_package_loader_reads_as_the_core(tmp_path: Path, text: str, expected: Any) -> None:
    path = tmp_path / "doc.yaml"
    path.write_text(_document(text), encoding="utf-8")
    if expected is REFUSED:
        with pytest.raises(PackageError, match="not YAML"):
            _read_yaml(path)
    else:
        assert _typed(_read_yaml(path)["k"]) == _typed(expected)


@pytest.mark.parametrize(("text", "expected"), TABLE, ids=IDS)
def test_edit_tree_reads_as_the_core(text: str, expected: Any) -> None:
    if expected is REFUSED:
        with pytest.raises(edit.PkgError) as error:
            edit.Document.parse(_document(text))
        assert error.value.code == "yaml_invalid"
    else:
        assert _typed(edit._load(_document(text))["k"]) == _typed(expected)


@pytest.mark.parametrize(
    ("text", "expected"), [row for row in TABLE if row[1] is not REFUSED], ids=lambda v: str(v)
)
def test_an_edit_keeps_the_meaning_of_other_values(text: str, expected: Any) -> None:
    document = edit.Document.parse(_document(text))
    document.data["other"] = 2
    written = document.dumps()
    read = yaml.load(written, Loader=_yaml12_loader())
    assert read["other"] == 2
    assert _typed(read["k"]) == _typed(expected)


@pytest.mark.parametrize(
    "value", ["1_000", "012", "yes", "1:30", "2026-09-30", "1e3", ".inf", "0b101", "true", "~"]
)
def test_a_string_written_by_edit_stays_a_string(value: str) -> None:
    document = edit.Document.parse("k: x\n")
    document.data["k"] = value
    assert yaml.load(document.dumps(), Loader=_yaml12_loader()) == {"k": value}


def test_a_yaml_1_1_directive_does_not_change_the_rules(tmp_path: Path) -> None:
    text = "%YAML 1.1\n---\nk: [012, 1:30, yes, 0b1]\n"
    path = tmp_path / "doc.yaml"
    path.write_text(text, encoding="utf-8")
    expected = [12, "1:30", "yes", "0b1"]
    assert _read_yaml(path)["k"] == expected
    assert edit.to_plain(edit._load(text))["k"] == expected


def test_the_core_loader_reads_the_same_table() -> None:
    """Прямая сверка: та же таблица через загрузчик ядра соседа ``control-plane``."""
    try:
        from control_plane.domain import package_source as core
    except ImportError:
        pytest.skip("нет соседа control-plane (extra sandbox): сверка с ядром не идёт")
    if not hasattr(core, "MAX_INT_DIGITS") or not hasattr(core, "load_yaml"):
        pytest.skip(
            "загрузчик ядра соседа старше TASK-001247 (нет целых YAML 1.2): сверять не с чем"
        )
    assert yaml12.MAX_INT_DIGITS == core.MAX_INT_DIGITS
    diverged = []
    for text, expected in TABLE:
        try:
            got: Any = core.load_yaml(_document(text))[0]["k"]
        except core.SourceError:
            got = REFUSED
        theirs = "отказ" if got is REFUSED else _typed(got)
        ours = "отказ" if expected is REFUSED else _typed(expected)
        if theirs != ours:
            diverged.append((text[:30], ours, theirs))
    assert diverged == []


def test_parse_float_refuses_what_json_has_not() -> None:
    assert yaml12.parse_float("1.5") == 1.5
    for text in (".inf", "-.inf", ".nan", "1" + "0" * 400 + ".0"):
        with pytest.raises(ValueError):
            yaml12.parse_float(text)
    assert not math.isnan(yaml12.parse_float("-0.0"))
