"""Правка пакета с сохранением файла (TAI-ADR-0054 п.12, plan Р10, SC-009).

Загрузка и запись без изменений дают тот же файл байт в байт; операции package-sdk edit
меняют только свои строки; rename обновляет ссылки, тесты, migrations и раскладку.
"""

from __future__ import annotations

import difflib
import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("ruamel.yaml")

from package_sdk import edit as pkg
from tests.umbrella._shim import FIXTURES as COMPONENT_FIXTURES
from tests.umbrella._shim import UMBRELLA as ROOT

FIXTURES = COMPONENT_FIXTURES / "process"
PROCESS = FIXTURES / "purchase.process.yaml"


def _file_id(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else f"fixtures/{path.name}"


FILES = sorted(
    {
        *(ROOT / "packages").rglob("*.yaml"),
        *(ROOT / "packages").rglob("*.yml"),
        *FIXTURES.rglob("*.yaml"),
    }
)


def _multiline_flow(text: str) -> bool:
    """Есть flow-коллекция ({…} или […]), разбитая на несколько строк."""
    depth = 0
    for line in text.splitlines():
        quote = None
        for char in line:
            if quote:
                quote = None if char == quote else quote
            elif char in "'\"":
                quote = char
            elif char == "#" and depth == 0:
                break
            elif char in "{[":
                depth += 1
            elif char in "}]":
                depth -= 1
        if depth > 0:
            return True
    return False


def _diff(before: str, after: str) -> list[str]:
    return [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]


# --- SC-009: загрузка и запись без изменений ------------------------------------


def test_every_package_file_is_found():
    assert len(FILES) > 50 and PROCESS in FILES


@pytest.mark.parametrize("path", FILES, ids=[_file_id(p) for p in FILES])
def test_load_and_save_without_changes_is_byte_identical(path, tmp_path):
    doc = pkg.Document.load(path)
    assert doc.dumps() == doc.text
    copy = tmp_path / path.name
    copy.write_bytes(path.read_bytes())
    assert pkg.Document.load(copy).save() is False
    assert copy.read_bytes() == path.read_bytes()


@pytest.mark.parametrize("path", FILES, ids=[_file_id(p) for p in FILES])
def test_ruamel_itself_reproduces_the_file(path):
    """Стиль подобран так, что ruamel сам пишет файл как есть. Исключение одно: flow-коллекция,
    разбитая на несколько строк, — переносы внутри {…} ruamel не хранит; такие файлы правка
    переносит на исходник слиянием (см. test_edit_keeps_a_multiline_flow_mapping)."""
    doc = pkg.Document.load(path)
    assert doc.exact or _multiline_flow(doc.text), doc.style


def test_style_is_detected_per_file():
    indentless = pkg.Document.parse("a:\n  items:\n  - x\n  - y\n")
    indented = pkg.Document.parse("a:\n  items:\n    - x\n    - y\n")
    assert (indentless.style.offset, indented.style.offset) == (0, 2)
    assert indentless.exact and indented.exact
    explicit_null = pkg.Document.parse("a:\n  default: null\n")
    empty_null = pkg.Document.parse("a:\n  default:\n  b: 1\n")
    assert explicit_null.exact and empty_null.exact


def test_long_folded_string_keeps_its_width():
    text = (
        "spec:\n  description: 'Длинная строка в одинарных кавычках, перенесённая редактором на ширине, "
        "которую ruamel должен угадать\n    — и продолжение на следующей строке.'\n"
    )
    doc = pkg.Document.parse(text)
    assert doc.exact and doc.dumps() == text


def test_yaml_1_2_keeps_on_as_a_string():
    doc = pkg.Document.parse("on: {observation: a.b}\noff: yes\n")
    assert list(doc.data) == ["on", "off"] and doc.data["off"] == "yes"
    assert doc.dumps() == "on: {observation: a.b}\noff: yes\n"


# --- операции меняют только свои строки ---------------------------------------


def _process_copy(tmp_path: Path) -> Path:
    target = tmp_path / "purchase.process.yaml"
    shutil.copy(PROCESS, target)
    return target


def test_add_step_changes_only_the_added_lines(tmp_path):
    path = _process_copy(tmp_path)
    before = path.read_text(encoding="utf-8")
    doc = pkg.Document.load(path)
    pkg.add_step(
        doc,
        "go-no-go",
        pkg.parse_fragment("{id: note, set: {decision: \"'none'\"}}", "step"),
        after="recall-history",
    )
    pkg.validate(doc)
    assert doc.save()
    diff = _diff(before, path.read_text(encoding="utf-8"))
    assert diff == ["+        - {id: note, set: {decision: \"'none'\"}}"]


def test_add_block_step_into_an_exact_file(tmp_path):
    path = tmp_path / "p.yaml"
    text = PROCESS.read_text(encoding="utf-8").replace(
        "            entity: {kind: legal_entity, key: event.payload.winner.inn, name: event.payload.winner.name,\n"
        "                     links:",
        "            entity: {kind: legal_entity, key: event.payload.winner.inn, name: event.payload.winner.name, links:",
    )
    path.write_text(text, encoding="utf-8")
    doc = pkg.Document.load(path)
    assert doc.exact
    pkg.add_step(
        doc,
        "results",
        pkg.parse_fragment("id: log\nremember:\n  facts: {closed: 'true'}\n", "step"),
        before="close",
    )
    doc.save()
    assert _diff(text, path.read_text(encoding="utf-8")) == [
        "+        - id: log",
        "+          remember:",
        "+            facts: {closed: 'true'}",
    ]


def test_edit_keeps_a_multiline_flow_mapping(tmp_path):
    path = _process_copy(tmp_path)
    before = path.read_text(encoding="utf-8")
    doc = pkg.Document.load(path)
    assert not doc.exact  # ruamel склеил бы flow-запись remember-winner в одну строку
    pkg.set_value(doc, "spec.stages[price].steps[approve-price].approve.due", "P3D")
    doc.save()
    after = path.read_text(encoding="utf-8")
    assert _diff(before, after) == ["-            due: P2D", "+            due: P3D"]
    assert "name: event.payload.winner.name,\n                     links:" in after


def test_edit_inside_a_multiline_flow_rewrites_only_that_mapping():
    source = "a: 1\nb: {x: 1,\n    y: 2}\nc: 3\n"
    doc = pkg.Document.parse(source)
    assert not doc.exact
    doc.data["b"]["y"] = 5
    doc.data["c"] = 4
    assert doc.dumps() == "a: 1\nb: {x: 1, y: 5}\nc: 4\n"


def test_add_decision_row_form_field_rule_and_set(tmp_path):
    path = _process_copy(tmp_path)
    before = path.read_text(encoding="utf-8")
    doc = pkg.Document.load(path)
    pkg.add_decision_row(
        doc,
        "approval-level",
        pkg.parse_fragment('{when: {nmck: "[10000000..50000000)"}, then: {approvers: 2}}', "row"),
        index=1,
    )
    pkg.add_form_field(
        doc,
        "decide-participation",
        "reason",
        pkg.parse_fragment("{type: string}", "schema"),
        required=True,
    )
    pkg.add_rule(
        doc,
        on_event=pkg.parse_fragment(
            "{on: {observation: purchase.extended}, do: [{id: extend, set: {extended: 'true'}}]}",
            "rule",
        ),
    )
    pkg.set_value(doc, "spec.displayName", "Участие в закупке")
    pkg.validate(doc)
    doc.save()
    diff = _diff(before, path.read_text(encoding="utf-8"))
    assert '+        - {when: {nmck: "[10000000..50000000)"}, then: {approvers: 2}}' in diff
    assert "+                  reason: {type: string}" in diff
    # новый ключ `on` (YAML 1.1 читает его True) — в двойных кавычках, как в файлах формата
    assert (
        "+    - {\"on\": {observation: purchase.extended}, do: [{id: extend, set: {extended: 'true'}}]}"
        in diff
    )
    assert diff[:2] == [
        "-  displayName: Участие в закупке (пример схемы)",
        "+  displayName: Участие в закупке",
    ]
    assert not [line for line in diff if line.startswith("-") and "displayName" not in line]


def test_operations_refuse_bad_input_machine_readably(tmp_path, capsys):
    path = _process_copy(tmp_path)
    before = path.read_text(encoding="utf-8")
    code = pkg.main(
        [
            "--json",
            "add-step",
            "--file",
            str(path),
            "--in",
            "go-no-go",
            "--step",
            "{id: price, set: {a: '1'}}",
        ]
    )
    error = json.loads(capsys.readouterr().out)["error"]
    assert code == 1 and error["code"] == "element_id_taken" and error["hint"]
    code = pkg.main(
        [
            "--json",
            "add-decision-row",
            "--file",
            str(path),
            "--table",
            "approval-level",
            "--row",
            "{when: {price: '-'}, then: {approvers: 1}}",
        ]
    )
    assert (
        code == 1
        and json.loads(capsys.readouterr().out)["error"]["code"] == "decision_column_unknown"
    )
    # правка, которую не пропускает схема, не записывается
    quorum = "spec.stages[price].steps[approve-price].approve.quorum"
    code = pkg.main(
        ["--json", "set", "--file", str(path), "--path", quorum, "--value", "{atLeast: 0}"]
    )
    error = json.loads(capsys.readouterr().out)["error"]
    assert code == 1 and error["code"] == "schema_violation" and "quorum" in error["path"]
    assert path.read_text(encoding="utf-8") == before


def test_dry_run_prints_a_diff_and_writes_nothing(tmp_path, capsys):
    path = _process_copy(tmp_path)
    before = path.read_text(encoding="utf-8")
    assert (
        pkg.main(
            ["--dry-run", "set", "--file", str(path), "--path", "spec.calendar", "--value", "kz"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "-  calendar: ru" in out and "+  calendar: kz" in out
    assert path.read_text(encoding="utf-8") == before


# --- rename -------------------------------------------------------------------


@pytest.fixture
def package(tmp_path):
    """Пакет с процессом, тестом и раскладкой."""
    root = tmp_path / "demo"
    (root / "processes").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / ".layout").mkdir()
    (root / "package.yaml").write_text(
        "apiVersion: taimen.ai/v1\nkind: Package\nkey: demo\nspec:\n  version: 0.1.0\n  displayName: Demo\n",
        encoding="utf-8",
    )
    shutil.copy(PROCESS, root / "processes" / "purchase-example.yaml")
    shutil.copy(FIXTURES / "purchase.test.yaml", root / "tests" / "purchase.test.yaml")
    layout = {
        "process": "purchase-example",
        "nodes": {
            "go-no-go": {"x": 0, "y": 0},
            "price": {"x": 240, "y": 0},
            "results": {"x": 480, "y": 0},
        },
    }
    (root / ".layout" / "purchase-example.json").write_text(
        json.dumps(layout, indent=2) + "\n", encoding="utf-8"
    )
    return root


def test_rename_updates_references_tests_migrations_and_layout(package, capsys):
    process = package / "processes" / "purchase-example.yaml"
    assert (
        pkg.main(["--json", "rename", "--file", str(process), "--from", "price", "--to", "pricing"])
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["version"] == 2 and result["layout"] is True
    text = process.read_text(encoding="utf-8")
    assert "- id: pricing" in text and "entry: stage.pricing.completed" in text
    # карта 1 → 2 уже была написана — дополняется она же, а не заводится вторая
    assert (
        "- {from: 1, to: 2, policy: migrate, map: {decide-participation: go-decision, price: pricing}}"
        in text
    )
    assert "version: 2" in text
    layout = json.loads((package / ".layout" / "purchase-example.json").read_text())
    assert list(layout["nodes"]) == ["go-no-go", "pricing", "results"]


def test_rename_of_a_step_follows_into_the_package_tests(package):
    process = package / "processes" / "purchase-example.yaml"
    test = package / "tests" / "purchase.test.yaml"
    before = test.read_text(encoding="utf-8")
    assert (
        pkg.main(
            [
                "rename",
                "--file",
                str(process),
                "--from",
                "decide-participation",
                "--to",
                "go-decision",
            ]
        )
        == 0
    )
    diff = _diff(before, test.read_text(encoding="utf-8"))
    assert sorted(diff) == [
        "+      tasks: [{step: go-decision, assignee: alice}]",
        "+  - complete: {step: go-decision, by: alice, output: {decision: no-go}}",
        "-      tasks: [{step: decide-participation, assignee: alice}]",
        "-  - complete: {step: decide-participation, by: alice, output: {decision: no-go}}",
    ]
    # цепочка: карта уже вела decide-participation → go-decision
    assert "map: {decide-participation: go-decision}" in process.read_text(encoding="utf-8")


def test_rename_of_a_decision_table_updates_decide_steps(package):
    process = package / "processes" / "purchase-example.yaml"
    doc = pkg.Document.load(process)
    pkg.rename_element(doc, "approval-level", "approval-tier", migration=False)
    pkg.validate(doc)
    doc.save()
    text = process.read_text(encoding="utf-8")
    assert "decide: {table: approval-tier}" in text and "- id: approval-tier" in text
    assert "version: 1" in text


def test_rename_object_records_renames_and_moves_files(package):
    result = pkg.rename_object(package, "Process", "purchase-example", "procurement")
    assert result["layout"] and (package / "processes" / "procurement.yaml").exists()
    assert (package / ".layout" / "procurement.json").exists()
    manifest = (package / "package.yaml").read_text(encoding="utf-8")
    assert manifest.endswith(
        "  renames:\n    - {kind: Process, from: purchase-example, to: procurement}\n"
    )
    assert "process: procurement" in (package / "tests" / "purchase.test.yaml").read_text(
        encoding="utf-8"
    )
    assert "key: procurement" in (package / "processes" / "procurement.yaml").read_text(
        encoding="utf-8"
    )


def test_layout_lives_next_to_the_package(package):
    process = package / "processes" / "purchase-example.yaml"
    assert (
        pkg.layout_path(process, "purchase-example")
        == package / ".layout" / "purchase-example.json"
    )


def test_logic_edits_do_not_touch_the_layout(package):
    layout = package / ".layout" / "purchase-example.json"
    before = layout.read_text()
    process = package / "processes" / "purchase-example.yaml"
    assert (
        pkg.main(
            [
                "add-stage",
                "--file",
                str(process),
                "--after",
                "price",
                "--stage",
                "{id: review, steps: [{id: check, set: {checked: 'true'}}]}",
            ]
        )
        == 0
    )
    assert layout.read_text() == before
