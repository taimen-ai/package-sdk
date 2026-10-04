"""Package settings in the format schema: ``spec.settings`` and the test steps (TAI-ADR-0067).

The schema is a closed subset of JSON Schema with bounded depth and size; the form is a
closed subset of JSON Forms the console renders. Everything outside them is refused by
the schema itself, before ``check`` and the core.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from package_sdk import schema

FIXTURES = Path(__file__).parent / "fixtures" / "schema"
UMBRELLA = Path(__file__).parent / "fixtures" / "umbrella" / "packages"


def load(name: str) -> Any:
    return yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))


def manifest(settings: Any) -> dict[str, Any]:
    return {
        "apiVersion": "taimen.ai/v1",
        "kind": "Package",
        "key": "demo",
        "spec": {"version": "0.1.0", "displayName": "Demo", "settings": settings},
    }


def with_schema(properties: dict[str, Any], **root: Any) -> dict[str, Any]:
    return manifest({"schema": {"type": "object", "properties": properties, **root}})


def with_ui(uischema: Any) -> dict[str, Any]:
    document = with_schema(
        {"limit": {"type": "integer", "default": 1}, "on": {"type": "boolean", "default": False}}
    )
    document["spec"]["settings"]["uischema"] = uischema
    return document


def control(**extra: Any) -> dict[str, Any]:
    return {"type": "Control", "scope": "#/properties/limit", **extra}


def errors(document: dict[str, Any]) -> list[str]:
    return schema.errors(schema.OBJECT, document)


# --- the declaration ---


def test_the_full_declaration_is_valid() -> None:
    assert errors(load("package-settings.yaml")) == []


def test_uischema_is_optional() -> None:
    document = load("package-settings.yaml")
    del document["spec"]["settings"]["uischema"]
    assert errors(document) == []


def test_catalog_packages_without_settings_pass_unchanged() -> None:
    manifests = sorted(UMBRELLA.glob("*/package.yaml"))
    assert manifests
    for path in manifests:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "settings" not in document["spec"], path
        assert errors(document) == [], path


@pytest.mark.parametrize(
    "field",
    [
        {"type": "string", "format": "date", "default": "2026-01-01"},
        {"type": "string", "format": "uri", "default": "https://example.com"},
        {"type": "string", "format": "uuid", "x-ref": "workspace", "default": ""},
        {"type": "string", "minLength": 1, "maxLength": 10, "pattern": "^a", "default": "a"},
        {"type": "number", "minimum": 0.5, "maximum": 1.5, "default": 1},
        {"type": "integer", "enum": [1, 2, 3], "default": 1},
        {"type": "array", "items": {"type": "string", "x-ref": "taskType"}, "default": []},
        {"type": "string", "x-ref": "calendar", "default": "default"},
        {"type": "string", "x-ref": "principal", "default": ""},
        {"type": "object", "properties": {"a": {"type": "boolean"}}, "required": ["a"]},
        # CP-ADR-0081 п.1, как у ядра: enum у boolean, пределы массива, массив массивов
        {"type": "boolean", "enum": [True], "default": True},
        {"type": "object", "properties": {"a": {"type": "integer"}}, "additionalProperties": False},
        {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 5},
        {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}, "default": []},
    ],
)
def test_every_keyword_of_the_subset_is_accepted(field: dict[str, Any]) -> None:
    assert errors(with_schema({"field": field})) == []


def test_fields_nest_three_levels_deep_and_no_deeper() -> None:
    def nest(levels: int) -> dict[str, Any]:
        field: dict[str, Any] = {"type": "integer", "default": 1}
        for _ in range(levels - 1):
            field = {"type": "object", "properties": {"x": field}}
        return field

    assert errors(with_schema({"a": nest(3)})) == []
    assert errors(with_schema({"a": nest(4)})) != []


def test_an_array_of_objects_counts_as_a_level() -> None:
    row = {"type": "object", "properties": {"x": {"type": "integer", "default": 0}}}
    assert errors(with_schema({"rows": {"type": "array", "items": row}})) == []
    nested = {"type": "object", "properties": {"rows": {"type": "array", "items": row}}}
    assert errors(with_schema({"a": nested})) != []


def test_one_hundred_properties_are_accepted_and_one_more_is_not() -> None:
    fields = {f"f{i}": {"type": "boolean", "default": False} for i in range(100)}
    assert errors(with_schema(fields)) == []
    fields["f100"] = {"type": "boolean", "default": False}
    assert errors(with_schema(fields)) != []


@pytest.mark.parametrize(
    "settings,fragment",
    [
        (None, "None is not of type 'object'"),
        ({}, "'schema' is a required property"),
        ({"schema": {"type": "object", "properties": {}}}, "should be non-empty"),
        ({"schema": {"type": "array", "items": {"type": "string"}}}, "'object' was expected"),
        ({"schema": {"type": "object"}}, "'properties' is a required property"),
        ({"schema": {"type": "object", "properties": {"a": {"type": "boolean"}}}, "ui": {}}, "ui"),
        (
            {"schema": {"type": "object", "properties": {"a": {"type": "boolean"}}, "$defs": {}}},
            "$defs",
        ),
        (
            {"schema": {"type": "object", "properties": {"Bad Name": {"type": "boolean"}}}},
            "Bad Name",
        ),
        (
            {
                "schema": {
                    "type": "object",
                    "properties": {"a": {"type": "boolean"}},
                    "required": "a",
                }
            },
            "is not of type 'array'",
        ),
    ],
)
def test_a_malformed_settings_root_is_refused(settings: Any, fragment: str) -> None:
    found = errors(manifest(settings))
    assert any(fragment in error for error in found), found


@pytest.mark.parametrize(
    "field",
    [
        {"type": "string", "writeOnly": True},  # признак секрета
        {"type": "string", "format": "password"},  # признак секрета
        {"type": "string", "format": "date-time"},
        {"type": "string", "title": "Threshold"},  # подпись — ключ словаря, не текст схемы
        {"type": "string", "description": "Threshold"},
        {"type": "string", "x-ref": "project"},
        {"type": "string", "$ref": "#/$defs/other"},
        {"type": "string", "const": "x"},
        {"type": "null"},
        {"type": ["string", "null"]},
        {"anyOf": [{"type": "string"}, {"type": "integer"}]},
        {"enum": ["a", "b"]},  # без type
        {"type": "integer", "exclusiveMinimum": 0},
        {"type": "integer", "multipleOf": 5},
        {"type": "array", "items": {"type": "string"}, "minItems": -1},
        {"type": "array", "items": {"type": "string"}, "maxItems": 1.5},
        {"type": "string", "minItems": 1},
        {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": True},
        {"type": "integer", "enum": []},
        {"type": "integer", "enum": [{"a": 1}]},
        {"type": "string", "pattern": ""},
        {"type": "integer", "minLength": 1},  # ограничение не своего типа
        {"type": "string", "additionalProperties": False},
        {"type": "integer", "format": "uuid"},
        {"type": "boolean", "x-ref": "role"},
        {"type": "string", "maximum": 5},
        {"type": "string", "items": {"type": "string"}},
        {"type": "string", "properties": {"a": {"type": "string"}}},
        {"type": "array"},  # массив без items
        {"type": "object"},  # объект без properties
        {"type": "object", "properties": {}},
        {"type": "array", "items": {"type": "array", "items": {"type": "array", "items": {}}}},
    ],
)
def test_a_field_outside_the_subset_is_refused(field: dict[str, Any]) -> None:
    assert errors(with_schema({"field": field})) != []


def test_settings_outside_the_manifest_are_not_a_spec_field_of_other_kinds() -> None:
    role = {
        "apiVersion": "taimen.ai/v1",
        "kind": "Role",
        "key": "reviewer",
        "spec": {"displayName": "Reviewer", "settings": {}},
    }
    assert errors(role) != []


# --- the form: a closed subset of JSON Forms ---


@pytest.mark.parametrize(
    "uischema",
    [
        control(),
        {"type": "VerticalLayout", "elements": [control()]},
        {"type": "HorizontalLayout", "elements": [control(), control()]},
        {"type": "Group", "label": "demo.settings.groups.main", "elements": [control()]},
        {"type": "Label", "text": "demo.settings.notes.limit"},
        control(label="demo.settings.limitShort"),
        {"type": "Control", "scope": "#/properties/a/properties/b/properties/c"},
        *[
            control(
                rule={
                    "effect": effect,
                    "condition": {"scope": "#/properties/on", "schema": {"const": True}},
                }
            )
            for effect in ("SHOW", "HIDE", "ENABLE", "DISABLE")
        ],
        control(
            rule={
                "effect": "SHOW",
                "condition": {
                    "scope": "#/properties/on",
                    "schema": {"enum": [1, 2], "minimum": 0, "maximum": 3},
                    "failWhenUndefined": False,
                },
            }
        ),
        control(
            rule={
                "effect": "HIDE",
                "condition": {
                    "scope": "#/properties/limit",
                    "schema": {"type": "integer", "minimum": 1, "maxItems": 2},
                },
            }
        ),
        control(
            rule={
                "effect": "SHOW",
                "condition": {
                    "scope": "#/properties/on",
                    "schema": {"pattern": "^a", "minLength": 1, "maxLength": 3, "format": "uri"},
                },
            }
        ),
        {
            "type": "VerticalLayout",
            "elements": [{"type": "Label", "text": "demo.settings.a"}],
            "rule": {
                "effect": "HIDE",
                "condition": {"scope": "#/properties/on", "schema": {"const": False}},
            },
        },
    ],
)
def test_the_console_subset_of_json_forms_is_accepted(uischema: dict[str, Any]) -> None:
    assert errors(with_ui(uischema)) == []


GOOD_RULE = {"effect": "SHOW", "condition": {"scope": "#/properties/on", "schema": {"const": True}}}


@pytest.mark.parametrize(
    "uischema,fragment",
    [
        (
            {"type": "Categorization", "elements": [{"type": "Category", "elements": [control()]}]},
            "'Categorization' is not one of",
        ),
        ({"type": "ListWithDetail", "scope": "#/properties/limit"}, "'ListWithDetail' is not one"),
        ({"type": "Category", "label": "demo.settings.a", "elements": [control()]}, "'Category'"),
        ({"type": "MyCustomRenderer"}, "'MyCustomRenderer' is not one of"),
        ({"scope": "#/properties/limit"}, "'type' is a required property"),
        ("VerticalLayout", "is not of type 'object'"),
        ({"type": "VerticalLayout"}, "'elements' is a required property"),
        ({"type": "VerticalLayout", "elements": []}, "should be non-empty"),
        ({"type": "VerticalLayout", "elements": [{"type": "Categorization"}]}, "Categorization"),
        ({"type": "VerticalLayout", "elements": [control()], "label": "demo.a.b"}, "label"),
        ({"type": "VerticalLayout", "elements": [control()], "options": {}}, "options"),
        ({"type": "Group", "elements": [control()]}, "'label' is a required property"),
        ({"type": "Group", "label": "Approval", "elements": [control()]}, "does not match"),
        ({"type": "Group", "label": "demo", "elements": [control()]}, "does not match"),
        ({"type": "Label"}, "'text' is a required property"),
        ({"type": "Label", "text": "Read this first"}, "does not match"),
        ({"type": "Control"}, "'scope' is a required property"),
        (control(scope="#/properties/"), "does not match"),
        (control(scope="#/definitions/limit"), "does not match"),
        (control(scope="/properties/limit"), "does not match"),
        (control(scope="#/properties/a/items/properties/b"), "does not match"),
        (control(scope="#/properties/a/properties/b/properties/c/properties/d"), "does not match"),
        (control(label="Limit"), "does not match"),
        (control(label=False), "is not of type 'string'"),
        # options у Control вне подмножества CP-ADR-0081 п.2, как у ядра
        (control(options={}), "options"),
        (control(options={"multi": True}), "options"),
        (control(options={"format": "radio"}), "options"),
        (control(options={"detail": "GENERATED"}), "options"),
        (control(elements=[control()]), "elements"),
        (control(renderer="custom"), "renderer"),
        (control(rule={**GOOD_RULE, "effect": "SHOWN"}), "'SHOWN' is not one of"),
        (control(rule={"effect": "SHOW"}), "'condition' is a required property"),
        (
            control(
                rule={
                    "effect": "SHOW",
                    "condition": {
                        "type": "LEAF",
                        "scope": "#/properties/on",
                        "expectedValue": True,
                    },
                }
            ),
            "'schema' is a required property",
        ),
        (
            control(rule={"effect": "SHOW", "condition": {"type": "OR", "conditions": []}}),
            "'scope' is a required property",
        ),
        (
            control(
                rule={"effect": "SHOW", "condition": {"scope": "#/properties/on", "schema": {}}}
            ),
            "should be non-empty",
        ),
        (
            control(
                rule={
                    "effect": "SHOW",
                    "condition": {"scope": "#/properties/on", "schema": {"x-ref": "role"}},
                }
            ),
            "x-ref",
        ),
        (
            control(
                rule={
                    "effect": "SHOW",
                    "condition": {"scope": "#/properties/on", "schema": {"default": 1}},
                }
            ),
            "default",
        ),
        (
            control(
                rule={"effect": "SHOW", "condition": {"scope": "#", "schema": {"const": True}}}
            ),
            "does not match",
        ),
    ],
)
def test_a_form_outside_the_console_subset_is_refused(uischema: Any, fragment: str) -> None:
    found = errors(with_ui(uischema))
    assert any(fragment in error for error in found), found


def test_a_refused_element_is_found_at_any_depth() -> None:
    inner = {"type": "Group", "label": "demo.settings.g", "elements": [{"type": "Categorization"}]}
    uischema = {
        "type": "VerticalLayout",
        "elements": [{"type": "HorizontalLayout", "elements": [inner]}],
    }
    assert errors(with_ui(uischema)) != []


# --- the package test: given.settings and the settings step ---


def process_test(**extra: Any) -> dict[str, Any]:
    return {"process": "claim", "name": "n", "steps": [{"expect": {"status": "running"}}], **extra}


def test_a_process_test_sets_and_changes_settings() -> None:
    assert schema.errors(schema.TEST, load("process-settings.test.yaml")) == []


@pytest.mark.parametrize("settings", [{}, {"limit": 1, "nested": {"a": [1, 2]}}])
def test_settings_values_are_any_json_the_core_checks(settings: dict[str, Any]) -> None:
    test = process_test(given={"settings": settings}, steps=[{"settings": settings}])
    assert schema.errors(schema.TEST, test) == []


def test_a_rule_test_may_give_settings() -> None:
    test = {
        "subject": "rule",
        "rule": "r",
        "name": "n",
        "given": {"event": {"type": "task.created"}, "settings": {"limit": 3}},
        "steps": [{"expect": {"result": "matched"}}],
    }
    assert schema.errors(schema.TEST, test) == []


@pytest.mark.parametrize(
    "test",
    [
        process_test(given={"settings": None}),
        process_test(given={"settings": [1]}),
        process_test(given={"settings": {"Bad Name": 1}}),
        process_test(steps=[{"settings": None}]),
        process_test(steps=[{"settings": "limit=1"}]),
        process_test(steps=[{"settings": {"Bad Name": 1}}]),
        # шаг — одно действие: смена настроек не делит шаг с другим действием
        process_test(steps=[{"settings": {"limit": 1}, "advance": "P1D"}]),
        process_test(steps=[{"settings": {"limit": 1}, "expect": {"status": "running"}}]),
    ],
)
def test_malformed_settings_in_a_test_are_refused(test: dict[str, Any]) -> None:
    assert schema.errors(schema.TEST, test) != []


def test_the_settings_step_belongs_to_process_tests() -> None:
    rule = {
        "subject": "rule",
        "rule": "r",
        "name": "n",
        "given": {"event": {"type": "task.created"}},
        "steps": [{"settings": {"limit": 1}}],
    }
    assert schema.errors(schema.TEST, rule) != []
