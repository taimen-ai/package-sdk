"""Пакеты процессов (TAI-ADR-0054): контракт каталога — виды Process и Calendar
и формат тестов пакета. Схема проверяет форму; типы выражений CEL, достижимость и
ссылки на скиллы и таблицы проверяет ядро (CP-ADR-0074, CP-ADR-0075)."""

from __future__ import annotations

import copy

import jsonschema
import pytest

from package_sdk import schema as schema_module
from tests.umbrella._shim import FIXTURES as COMPONENT_FIXTURES
from tests.umbrella._shim import UMBRELLA as ROOT
from tests.umbrella._shim import cp

FIXTURES = COMPONENT_FIXTURES / "process"
TEST_SCHEMA = schema_module.load(schema_module.TEST)


def errors(document: dict) -> list[str]:
    return [cp._format_schema_error(e) for e in cp._schema_validator().iter_errors(document)]


def test_errors(document: dict) -> list[str]:
    validator = jsonschema.Draft202012Validator(TEST_SCHEMA)
    return [e.message for e in validator.iter_errors(document)]


test_errors.__test__ = False  # помощник, не тест


def process() -> dict:
    return cp._read_yaml(FIXTURES / "purchase.process.yaml")


def test_the_example_process_is_valid():
    assert errors(process()) == []


def test_the_example_calendar_is_valid():
    assert errors(cp._read_yaml(FIXTURES / "ru.calendar.yaml")) == []


def test_the_example_package_test_is_valid():
    assert test_errors(cp._read_yaml(FIXTURES / "purchase.test.yaml")) == []


def test_a_step_has_exactly_one_kind():
    doc = process()
    step = doc["spec"]["stages"][0]["steps"][1]
    step["set"] = {"decision": "'go'"}
    assert errors(doc)


def test_element_ids_are_stable_slugs():
    doc = process()
    doc["spec"]["stages"][0]["id"] = "Go No Go"
    assert errors(doc)


def test_a_process_needs_data_start_and_stages():
    for field in ("data", "start", "stages"):
        doc = process()
        del doc["spec"][field]
        assert errors(doc), field


def test_quorum_is_all_any_at_least_or_percent():
    doc = process()
    approve = doc["spec"]["stages"][1]["steps"][1]["approve"]
    for quorum in ("all", "any", {"atLeast": 2}, {"percent": 50}):
        approve["quorum"] = quorum
        assert errors(doc) == [], quorum
    approve["quorum"] = {"atLeast": 0}
    assert errors(doc)


def test_an_assignee_names_one_kind():
    doc = process()
    step = doc["spec"]["stages"][0]["steps"][1]["human"]
    step["assign"] = [{"role": "a", "agent": "b"}]
    assert errors(doc)


def test_recall_starts_from_anchors_and_remember_writes_facts_or_an_entity():
    doc = process()
    recall = doc["spec"]["stages"][0]["steps"][0]["recall"]
    recall["anchors"] = []
    assert errors(doc)
    doc = process()
    remember = doc["spec"]["stages"][2]["steps"][1]["remember"]
    remember["facts"] = {"x": "1"}
    assert errors(doc), "facts и entity вместе"


def test_the_memory_projection_names_the_case_key():
    doc = process()
    del doc["spec"]["memory"]["case"]["key"]
    assert errors(doc)


def test_owner_is_an_assign_chain_and_optional():
    doc = process()
    assert doc["spec"]["owner"] == [{"role": "purchase-lead"}]
    doc["spec"]["owner"] = [{"principal": "${OWNER_ID}"}, {"agent": "example-process"}]
    assert errors(doc) == []
    doc["spec"]["owner"] = []
    assert errors(doc), "пустая цепочка"
    doc["spec"]["owner"] = [{"role": "a", "agent": "b"}]
    assert errors(doc), "кандидат одного вида"
    del doc["spec"]["owner"]
    assert errors(doc) == [], "владелец в схеме необязателен — предупреждает проверка пакета"


def test_governed_by_names_a_document():
    doc = process()
    doc["spec"]["governedBy"] = [{"section": "1"}]
    assert errors(doc)


def test_a_migration_has_a_policy():
    doc = process()
    del doc["spec"]["migrations"][0]["policy"]
    assert errors(doc)


def test_durations_are_iso_8601():
    doc = process()
    doc["spec"]["stages"][1]["steps"][1]["approve"]["due"] = "2 days"
    assert errors(doc)


def test_a_package_may_declare_renames():
    doc = cp._read_yaml(ROOT / "packages" / "notify" / "package.yaml")
    doc = copy.deepcopy(doc)
    doc["spec"]["renames"] = [
        {"kind": "WorkRule", "from": "invoice-received", "to": "invoice-payment"}
    ]
    assert errors(doc) == []


def test_a_test_step_has_exactly_one_action():
    doc = cp._read_yaml(FIXTURES / "purchase.test.yaml")
    doc["steps"][0]["advance"] = "P1D"
    assert test_errors(doc)


def test_a_mock_answers_with_output_error_or_timeout():
    doc = cp._read_yaml(FIXTURES / "purchase.test.yaml")
    doc["mocks"]["recall"][0] = {"step": "recall-history"}
    assert test_errors(doc)


@pytest.mark.parametrize("path", sorted((ROOT / "packages").glob("*/**/*.yaml")), ids=str)
def test_existing_packages_still_pass_the_schema(path):
    document = cp._read_yaml(path)
    if isinstance(document, dict) and "kind" in document:
        assert errors(document) == [], path
