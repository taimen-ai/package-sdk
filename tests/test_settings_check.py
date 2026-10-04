"""Package settings in ``check``, the sandbox and ``describe`` (TAI-ADR-0067, CP-ADR-0081).

The fixture is ``tests/fixtures/settings/packages/claims-intake``: a declaration with a
layout and labels in two languages, a process and a work rule that read the settings,
and process tests that save them. Every broken declaration below is a copy of the
fixture with one change, and each finding is checked with its code and path.

Process expressions are typed by the core's code only: the tests of ``settings_ref_type``
in processes and of the sandbox with settings run when the core next to the SDK knows
package settings (``control_plane.domain.settings_refs``) and are skipped otherwise; with
an older core the sandbox must refuse loudly instead.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from package_sdk import check, cli, manifest, model, sandbox, settings

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "settings" / "packages" / "claims-intake"
CALENDAR = (
    Path(__file__).resolve().parent / "fixtures" / "sla" / "packages" / "sla-demo" / "calendars"
)
UMBRELLA = Path(__file__).resolve().parent / "fixtures" / "umbrella" / "packages"
KEY = "claims-intake"
FIELDS = "/spec/settings/schema/properties"


@pytest.fixture
def package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of the fixture a test may break; finding paths are relative to tmp_path."""
    monkeypatch.setattr(model, "ROOT", tmp_path)
    root = tmp_path / "packages" / KEY
    shutil.copytree(FIXTURE, root)
    return root


def _read(path: Path) -> Any:
    # YAML 1.2, as the SDK reads packages: the key "on" of a process stays a string
    return model._read_yaml(path)


def _write(path: Path, document: Any) -> None:
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), "utf-8")


def _change(
    directory: Path, edit: Callable[[dict[str, Any]], None], name: str = "package.yaml"
) -> None:
    document = _read(directory / name)
    edit(document)
    _write(directory / name, document)


def _check(directory: Path) -> tuple[list[str], list[str]]:
    return check.check(model.resolve_targets([str(directory)]))


def _codes(found: list[str]) -> list[tuple[str, str, str]]:
    """(file in the package, code, path) of each finding that has a code."""
    out = []
    for line in found:
        match = re.match(
            r"^(?P<file>[^:]+): (?P<code>[a-z_]+): .*?(?: \[(?P<path>[^\]]*)\])?$", line
        )
        if match is None:
            continue
        out.append((match["file"].split(f"{KEY}/", 1)[-1], match["code"], match["path"] or ""))
    return out


def _settings_codes(found: list[str]) -> list[tuple[str, str, str]]:
    return [c for c in _codes(found) if c[1].startswith("settings_")]


def _schema(document: dict[str, Any]) -> dict[str, Any]:
    schema: dict[str, Any] = document["spec"]["settings"]["schema"]
    return schema


def _props(document: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = _schema(document)["properties"]
    return properties


def _add_labels(directory: Path, *keys: str) -> None:
    for locale in ("en", "ru"):
        path = directory / "i18n" / f"{locale}.yaml"
        messages = _read(path)
        messages.update({key: key.rsplit(".", 1)[-1] for key in keys})
        _write(path, messages)


def _add_field(name: str, field: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    def edit(document: dict[str, Any]) -> None:
        _props(document)[name] = field
        controls = document["spec"]["settings"]["uischema"]["elements"]
        controls.append({"type": "Control", "scope": f"#/properties/{name}"})

    return edit


# --- the fixture ---------------------------------------------------------------------------


def test_the_fixture_passes_check(package: Path) -> None:
    errors, _warnings = _check(package)
    assert errors == []


def test_check_is_repeatable(package: Path) -> None:
    assert _check(package) == _check(package)


def test_the_fixture_passes_check_from_the_cli(
    package: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["check", "--package", str(package)]) == 0
    out = capsys.readouterr().out
    assert "ok: packages 1" in out
    assert not re.search(r": settings_[a-z_]+: ", out), out


# --- broken declarations: one change, one finding with its path ----------------------------

# (id, the change of package.yaml, the code, the path of the finding)
BROKEN: list[tuple[str, Callable[[dict[str, Any]], None], str, str]] = [
    (
        "secret-name-token",
        _add_field("apiToken", {"type": "string", "default": ""}),
        settings.SECRET_FIELD,
        f"{FIELDS}/apiToken",
    ),
    (
        "secret-name-password",
        _add_field("smtpPassword", {"type": "string", "default": ""}),
        settings.SECRET_FIELD,
        f"{FIELDS}/smtpPassword",
    ),
    (
        "secret-name-api-key",
        _add_field("apiKey", {"type": "string", "default": ""}),
        settings.SECRET_FIELD,
        f"{FIELDS}/apiKey",
    ),
    (
        "secret-name-client-secret",
        _add_field("client_secret", {"type": "string", "default": ""}),
        settings.SECRET_FIELD,
        f"{FIELDS}/client_secret",
    ),
    (
        "secret-write-only",
        lambda d: _props(d)["channel"].update({"writeOnly": True}),
        settings.SECRET_FIELD,
        f"{FIELDS}/channel/writeOnly",
    ),
    (
        "secret-format-password",
        lambda d: _props(d)["intake"]["properties"]["mailbox"].update({"format": "password"}),
        settings.SECRET_FIELD,
        f"{FIELDS}/intake/properties/mailbox/format",
    ),
    (
        "secret-material-in-default",
        lambda d: _props(d)["intake"]["properties"].update(
            {"note": {"type": "string", "default": "Bearer " + "a1b2c3d4e5" * 3}}
        ),
        settings.SECRET_FIELD,
        f"{FIELDS}/intake/properties/note/default",
    ),
    (
        "required-names-a-missing-field",
        lambda d: _schema(d)["required"].append("refundLimit"),
        settings.SCHEMA_UNSUPPORTED,
        "/spec/settings/schema/required/1",
    ),
    (
        "required-of-a-nested-object-names-a-missing-field",
        lambda d: _props(d)["intake"].update({"required": ["inbox"]}),
        settings.SCHEMA_UNSUPPORTED,
        f"{FIELDS}/intake/required/0",
    ),
    (
        "default-of-another-type",
        lambda d: _props(d)["refundThreshold"].update({"default": "x"}),
        settings.DEFAULT_INVALID,
        f"{FIELDS}/refundThreshold/default",
    ),
    (
        "default-boolean-for-an-integer",
        lambda d: _props(d)["refundThreshold"].update({"default": True}),
        settings.DEFAULT_INVALID,
        f"{FIELDS}/refundThreshold/default",
    ),
    (
        "default-above-maximum",
        lambda d: _props(d)["refundThreshold"].update({"default": 20000000}),
        settings.DEFAULT_INVALID,
        f"{FIELDS}/refundThreshold/default",
    ),
    (
        "default-outside-enum",
        lambda d: _props(d)["channel"].update({"default": "fax"}),
        settings.DEFAULT_INVALID,
        f"{FIELDS}/channel/default",
    ),
    (
        "default-item-against-the-pattern",
        lambda d: _props(d)["tags"].update({"default": ["Urgent"]}),
        settings.DEFAULT_INVALID,
        f"{FIELDS}/tags/default",
    ),
    (
        "default-not-an-email",
        lambda d: _props(d)["intake"]["properties"]["mailbox"].update({"default": "claims"}),
        settings.DEFAULT_INVALID,
        f"{FIELDS}/intake/properties/mailbox/default",
    ),
    (
        "optional-field-without-default",
        lambda d: _props(d)["channel"].pop("default"),
        settings.DEFAULT_MISSING,
        f"{FIELDS}/channel",
    ),
    (
        "arrays-of-arrays-nested-too-deep",
        lambda d: _props(d)["intake"]["properties"].update(
            {
                "grid": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "array", "items": {}}},
                    "default": [],
                }
            }
        ),
        settings.SCHEMA_UNSUPPORTED,
        f"{FIELDS}/intake/properties/grid/items/items",
    ),
    (
        "enum-on-an-array",
        lambda d: _props(d)["tags"].update({"enum": ["a"]}),
        settings.SCHEMA_UNSUPPORTED,
        f"{FIELDS}/tags/enum",
    ),
    (
        "min-items-not-a-count",
        lambda d: _props(d)["tags"].update({"minItems": -1}),
        settings.SCHEMA_UNSUPPORTED,
        f"{FIELDS}/tags/minItems",
    ),
    (
        "max-items-on-a-string",
        lambda d: _props(d)["channel"].update({"maxItems": 1}),
        settings.SCHEMA_UNSUPPORTED,
        f"{FIELDS}/channel/maxItems",
    ),
    (
        "default-below-min-items",
        lambda d: _props(d)["tags"].update({"minItems": 1}),
        settings.DEFAULT_INVALID,
        f"{FIELDS}/tags/default",
    ),
    (
        "write-only-false-is-outside-the-subset",
        lambda d: _props(d)["channel"].update({"writeOnly": False}),
        settings.SCHEMA_UNSUPPORTED,
        f"{FIELDS}/channel/writeOnly",
    ),
    (
        "enum-on-an-object",
        lambda d: _props(d)["intake"].update({"enum": ["a"]}),
        settings.SCHEMA_UNSUPPORTED,
        f"{FIELDS}/intake/enum",
    ),
    (
        "title-in-the-schema",
        lambda d: _props(d)["channel"].update({"title": "Channel"}),
        settings.SCHEMA_UNSUPPORTED,
        f"{FIELDS}/channel/title",
    ),
    (
        "x-ref-on-an-integer",
        lambda d: _props(d)["refundThreshold"].update({"x-ref": "role"}),
        settings.SCHEMA_UNSUPPORTED,
        f"{FIELDS}/refundThreshold/x-ref",
    ),
    (
        "objects-nested-too-deep",
        lambda d: _props(d)["intake"]["properties"].update(
            {
                "limits": {
                    "type": "object",
                    "properties": {
                        "daily": {
                            "type": "object",
                            "properties": {"max": {"type": "integer", "default": 1}},
                        }
                    },
                }
            }
        ),
        settings.SCHEMA_UNSUPPORTED,
        f"{FIELDS}/intake/properties/limits/properties/daily",
    ),
    (
        "root-is-not-an-object",
        lambda d: _schema(d).update({"type": "array"}),
        settings.SCHEMA_UNSUPPORTED,
        "/spec/settings/schema/type",
    ),
    (
        "scope-of-a-missing-property",
        lambda d: d["spec"]["settings"]["uischema"]["elements"][1].update(
            {"scope": "#/properties/missing"}
        ),
        settings.UISCHEMA_UNSUPPORTED,
        "/spec/settings/uischema/elements/1/scope",
    ),
    (
        "two-controls-on-one-property",
        lambda d: d["spec"]["settings"]["uischema"]["elements"].append(
            {"type": "Control", "scope": "#/properties/channel"}
        ),
        settings.UISCHEMA_UNSUPPORTED,
        "/spec/settings/uischema/elements/4/scope",
    ),
    (
        "group-label-outside-settings-groups",
        lambda d: d["spec"]["settings"]["uischema"]["elements"][0].update(
            {"label": f"{KEY}.approval"}
        ),
        settings.UISCHEMA_UNSUPPORTED,
        "/spec/settings/uischema/elements/0/label",
    ),
    (
        "options-of-a-control",
        lambda d: d["spec"]["settings"]["uischema"]["elements"][1].update(
            {"options": {"format": "radio"}}
        ),
        settings.UISCHEMA_UNSUPPORTED,
        "/spec/settings/uischema/elements/1/options",
    ),
    (
        "x-ref-in-a-rule-condition",
        lambda d: d["spec"]["settings"]["uischema"]["elements"][0]["elements"][2]["rule"][
            "condition"
        ].update({"schema": {"x-ref": "role"}}),
        settings.UISCHEMA_UNSUPPORTED,
        "/spec/settings/uischema/elements/0/elements/2/rule/condition/schema",
    ),
    (
        "categorization-is-not-rendered",
        lambda d: d["spec"]["settings"]["uischema"].update({"type": "Categorization"}),
        settings.UISCHEMA_UNSUPPORTED,
        "/spec/settings/uischema/type",
    ),
]


@pytest.mark.parametrize(
    ("change", "code", "path"), [case[1:] for case in BROKEN], ids=[case[0] for case in BROKEN]
)
def test_a_broken_declaration_is_one_finding_with_its_path(
    package: Path, change: Callable[[dict[str, Any]], None], code: str, path: str
) -> None:
    _change(package, change)
    errors, _warnings = _check(package)
    assert ("package.yaml", code, path) in _settings_codes(errors), errors
    # the format schema does not repeat the finding without a code
    assert not [e for e in errors if "spec/settings" in e and ": settings_" not in e], errors


def test_there_are_at_least_ten_broken_declarations() -> None:
    assert len(BROKEN) >= 10
    assert {code for _, _, code, _ in BROKEN} >= {
        settings.SCHEMA_UNSUPPORTED,
        settings.DEFAULT_MISSING,
        settings.DEFAULT_INVALID,
        settings.SECRET_FIELD,
        settings.UISCHEMA_UNSUPPORTED,
    }


@pytest.mark.parametrize("name", ["secretRef", "routingKey", "keyword", "tokenizerMode"])
def test_names_outside_the_secret_list_are_fields(package: Path, name: str) -> None:
    """The list and the exceptions are the core's: secretRef is fine, and so is a name that
    merely ends with "key" — but a name holding "token" anywhere is a secret."""
    _change(package, _add_field(name, {"type": "string", "default": ""}))
    _add_labels(package, f"{KEY}.settings.{name}")
    errors, _warnings = _check(package)
    found = [c for c in _settings_codes(errors) if c[1] == settings.SECRET_FIELD]
    if name == "tokenizerMode":
        assert found == [("package.yaml", settings.SECRET_FIELD, f"{FIELDS}/{name}")]
    else:
        assert found == [] and errors == []


def test_a_required_field_needs_no_default(package: Path) -> None:
    def edit(document: dict[str, Any]) -> None:
        _props(document)["channel"].pop("default")
        _schema(document)["required"].append("channel")

    _change(package, edit)
    assert _check(package)[0] == []


def test_an_optional_object_needs_no_default(package: Path) -> None:
    assert "default" not in _props(_read(package / "package.yaml"))["intake"]
    assert _check(package)[0] == []


def test_a_default_of_a_broken_field_is_not_checked_against_it(package: Path) -> None:
    """The field itself is the finding: its default is not compared with a wrong schema."""
    _change(package, lambda d: _props(d)["refundThreshold"].update({"minimum": "zero"}))
    codes = [c[1] for c in _settings_codes(_check(package)[0])]
    assert codes == [settings.SCHEMA_UNSUPPORTED]


def test_every_finding_of_one_declaration_is_reported(package: Path) -> None:
    def edit(document: dict[str, Any]) -> None:
        _props(document)["refundThreshold"]["default"] = "x"
        _props(document)["channel"].pop("default")
        _schema(document)["required"].append("missing")

    _change(package, edit)
    assert sorted(_settings_codes(_check(package)[0])) == sorted(
        [
            ("package.yaml", settings.DEFAULT_INVALID, f"{FIELDS}/refundThreshold/default"),
            ("package.yaml", settings.DEFAULT_MISSING, f"{FIELDS}/channel"),
            ("package.yaml", settings.SCHEMA_UNSUPPORTED, "/spec/settings/schema/required/1"),
        ]
    )


@pytest.mark.parametrize("value", [None, [], "schema", 1])
def test_a_declaration_that_is_not_a_mapping(package: Path, value: Any) -> None:
    _change(package, lambda d: d["spec"].update({"settings": value}))
    codes = [c for c in _settings_codes(_check(package)[0]) if c[0] == "package.yaml"]
    assert codes == [("package.yaml", settings.SCHEMA_UNSUPPORTED, "/spec/settings")]


def test_an_empty_schema_is_unsupported(package: Path) -> None:
    _change(package, lambda d: _schema(d).update({"properties": {}, "required": []}))
    codes = _settings_codes(_check(package)[0])
    assert (
        "package.yaml",
        settings.SCHEMA_UNSUPPORTED,
        "/spec/settings/schema/properties",
    ) in codes


def test_a_field_without_a_control_is_a_warning(package: Path) -> None:
    _change(package, lambda d: d["spec"]["settings"]["uischema"]["elements"].pop())
    errors, warnings = _check(package)
    assert errors == []
    uncovered = [w for w in warnings if settings.UISCHEMA_UNCOVERED in w]
    assert _settings_codes(uncovered) == [
        ("package.yaml", settings.UISCHEMA_UNCOVERED, "/spec/settings/uischema")
    ]
    assert "intake.mailbox" in uncovered[0]


def test_without_a_layout_every_field_is_shown(package: Path) -> None:
    _change(package, lambda d: d["spec"]["settings"].pop("uischema"))
    _add_labels(package)
    errors, warnings = _check(package)
    assert errors == []
    # the group label is no longer shown by the settings and no view shows it either
    assert _settings_codes(warnings) == []


# --- labels --------------------------------------------------------------------------------


def test_a_label_missing_in_one_language(package: Path) -> None:
    path = package / "i18n" / "ru.yaml"
    messages = _read(path)
    del messages[f"{KEY}.settings.refundThreshold"]
    _write(path, messages)
    errors, _warnings = _check(package)
    assert _settings_codes(errors) == [
        ("package.yaml", settings.LABEL_MISSING, f"{FIELDS}/refundThreshold")
    ]
    assert "of ru" in errors[0] and f"{KEY}.settings.refundThreshold" in errors[0]


def test_labels_of_nested_fields_the_package_and_the_layout_are_required(package: Path) -> None:
    for locale in ("en", "ru"):
        path = package / "i18n" / f"{locale}.yaml"
        messages = _read(path)
        for key in (
            f"{KEY}.title",
            f"{KEY}.settings.intake.mailbox",
            f"{KEY}.settings.groups.approval",
        ):
            del messages[key]
        _write(path, messages)
    found = sorted(set(_settings_codes(_check(package)[0])))
    assert found == [
        ("package.yaml", settings.LABEL_MISSING, "/spec/settings"),
        ("package.yaml", settings.LABEL_MISSING, f"{FIELDS}/intake/properties/mailbox"),
        ("package.yaml", settings.LABEL_MISSING, "/spec/settings/uischema/elements/0/label"),
    ]


def test_settings_without_languages_are_a_missing_label(package: Path) -> None:
    def edit(document: dict[str, Any]) -> None:
        document["spec"].pop("locales")
        document["spec"].pop("defaultLocale")

    _change(package, edit)
    shutil.rmtree(package / "i18n")
    errors, _warnings = _check(package)
    assert _settings_codes(errors) == [("package.yaml", settings.LABEL_MISSING, "/spec/settings")]


def test_the_labels_of_the_settings_are_used_keys(package: Path) -> None:
    """A dictionary of a package without views shows the settings: no unused_message."""
    _errors, warnings = _check(package)
    assert not [w for w in warnings if "unused_message" in w], warnings


# --- references settings.<path> -----------------------------------------------------------


def _set_when(directory: Path, expression: str) -> None:
    def edit(document: dict[str, Any]) -> None:
        document["spec"]["stages"][0]["steps"][0]["when"] = expression

    _change(directory, edit, "processes/claim.yaml")


def test_a_process_reads_an_undeclared_field(package: Path) -> None:
    _set_when(package, "data.amount > settings.refundLimit")
    errors, _warnings = _check(package)
    assert _settings_codes(errors) == [
        ("processes/claim.yaml", settings.REF_UNKNOWN, "/spec/stages/0/steps/0/when")
    ]
    assert "settings.refundLimit" in "".join(errors)


def test_a_process_of_a_package_without_settings(package: Path) -> None:
    _change(package, lambda d: d["spec"].pop("settings"))
    errors, _warnings = _check(package)
    unknown = [c for c in _settings_codes(errors) if c[1] == settings.REF_UNKNOWN]
    assert ("processes/claim.yaml", settings.REF_UNKNOWN, "/spec/stages/0/steps/0/when") in unknown
    assert any("declares no settings" in e for e in errors)


def test_a_string_literal_and_a_data_field_named_settings_are_no_reads(package: Path) -> None:
    _set_when(package, "data.amount > 0 && 'settings.x' != data.ticketId")
    assert _check(package)[0] == []


def test_only_the_do_of_a_catch_named_settings_hides_the_settings() -> None:
    """Inside a catch branch named settings the name is the error, not the settings (Б1)."""
    spec = {
        "stages": [
            {
                "steps": [
                    {
                        "try": {
                            "do": [{"when": "settings.a"}],
                            "catch": [
                                {"as": "error", "do": [{"when": "settings.b"}]},
                                {"as": "settings", "do": [{"when": "settings.type"}]},
                            ],
                        }
                    }
                ]
            }
        ]
    }
    assert settings.shadowed(spec) == ["/spec/stages/0/steps/0/try/catch/1/do"]
    assert settings.shadowed({"catch": [{"as": "error"}], "when": "settings.a"}) == []
    assert settings.shadowed({"catch": "settings", "as": "settings"}) == []
    # a catch inside a branch named otherwise is found at its depth
    inner = {"try": {"catch": [{"as": "settings", "do": []}]}}
    outer = {"try": {"catch": [{"as": "error", "do": [inner]}]}}
    assert settings.shadowed(outer) == ["/spec/try/catch/0/do/0/try/catch/0/do"]


def _with_catch(directory: Path, outside: str, inside: str = "string(settings.type)") -> None:
    """A guarded step whose do reads ``outside`` and whose catch named settings — ``inside``."""

    def edit(document: dict[str, Any]) -> None:
        document["spec"]["data"]["properties"]["note"] = {"type": "string"}
        document["spec"]["stages"][0]["steps"].insert(
            1,
            {
                "id": "guarded",
                "try": {
                    "do": [{"id": "inner", "set": {"note": outside}}],
                    "catch": [
                        {"as": "settings", "do": [{"id": "handled", "set": {"note": inside}}]}
                    ],
                },
            },
        )

    _change(directory, edit, "processes/claim.yaml")


@pytest.mark.parametrize("mode", ["core", "none"])
def test_a_catch_named_settings_leaves_the_other_reads_checked(
    package: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """A catch branch named settings hides its own reads only, with the core or without it."""
    if mode == "core" and settings.core_settings() is None:
        pytest.skip("the core next to the SDK predates CP-ADR-0081")
    if mode == "none":
        monkeypatch.setattr(settings, "core_settings", lambda: None)
    _with_catch(package, "string(settings.refundLimit)")
    errors, _warnings = _check(package)
    assert _settings_codes(errors) == [
        (
            "processes/claim.yaml",
            settings.REF_UNKNOWN,
            "/spec/stages/0/steps/1/try/do/0/set/note",
        )
    ], errors


@pytest.mark.parametrize("mode", ["core", "none"])
def test_the_reads_of_a_catch_named_settings_are_the_error(
    package: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    if mode == "core" and settings.core_settings() is None:
        pytest.skip("the core next to the SDK predates CP-ADR-0081")
    if mode == "none":
        monkeypatch.setattr(settings, "core_settings", lambda: None)
    _with_catch(package, "string(settings.refundThreshold)", "string(settings.nothing)")
    assert _settings_codes(_check(package)[0]) == []


def test_a_process_reads_a_field_of_a_wrong_type(package: Path) -> None:
    if settings.core_settings() is None:
        pytest.skip("the core next to the SDK predates CP-ADR-0081: process types are the plan's")
    _set_when(package, "data.amount > settings.channel")
    errors, _warnings = _check(package)
    assert _settings_codes(errors) == [
        ("processes/claim.yaml", settings.REF_TYPE, "/spec/stages/0/steps/0/when")
    ]


def test_without_the_core_process_types_are_left_to_the_plan(
    package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "core_settings", lambda: None)
    _set_when(package, "data.amount > settings.channel")
    errors, warnings = _check(package)
    assert errors == []
    assert settings.CORE_TYPES_MISSING in warnings


# --- a deadline counted by an expression (CP-ADR-0081 amendment G1–G2) -----------------------

DUE = "/spec/stages/0/steps/0/human/due"


def _with_due(directory: Path, due: dict[str, Any]) -> None:
    """The step decide-claim gets the due, the process the calendar office of the SLA fixture,
    the settings the integer fields reviewDays and warnHours and the number field ratio."""
    shutil.copytree(CALENDAR, directory / "calendars")
    for name, field in (
        ("reviewDays", {"type": "integer", "minimum": 0, "maximum": 30, "default": 1}),
        ("warnHours", {"type": "integer", "minimum": 0, "maximum": 8, "default": 2}),
        ("ratio", {"type": "number", "default": 0.5}),
    ):
        _change(directory, _add_field(name, field))
        _add_labels(directory, f"{KEY}.settings.{name}")

    def edit(document: dict[str, Any]) -> None:
        document["spec"]["calendar"] = "office"
        document["spec"]["stages"][0]["steps"][0]["human"]["due"] = due

    _change(directory, edit, "processes/claim.yaml")


def _without_core_due(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """core — the core next to the SDK as it is; older — a core that knows the settings but
    not deadline expressions; none — no core at all."""
    if mode == "core":
        core = settings.core_settings()
        if core is None or not core.due:
            pytest.skip("the core next to the SDK does not count deadline expressions")
    elif mode == "older":
        core = settings.core_settings()
        if core is None:
            pytest.skip("the core next to the SDK predates CP-ADR-0081")
        older = SimpleNamespace(**{**vars(core), "due": False})
        monkeypatch.setattr(settings, "core_settings", lambda: older)
    else:
        monkeypatch.setattr(settings, "core_settings", lambda: None)


def test_the_core_due_expressions_are_told_by_amount_of() -> None:
    assert not settings.core_due_expressions(None)
    assert not settings.core_due_expressions(SimpleNamespace())
    assert settings.core_due_expressions(SimpleNamespace(amount_of=lambda *a: 0))


@pytest.mark.parametrize("mode", ["core", "older", "none"])
def test_a_due_reading_declared_integers_passes(
    package: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _without_core_due(monkeypatch, mode)
    _with_due(
        package,
        {
            "workdays": {"expr": "settings.reviewDays"},
            "warnBefore": {"workhours": {"expr": "settings.warnHours"}},
        },
    )
    errors, warnings = _check(package)
    assert errors == []
    assert settings.CORE_DUE_TYPES_MISSING not in warnings


@pytest.mark.parametrize("mode", ["core", "older", "none"])
@pytest.mark.parametrize(
    ("due", "code", "path"),
    [
        ({"workdays": {"expr": "settings.reviewDayz"}}, settings.REF_UNKNOWN, "/workdays/expr"),
        ({"workhours": {"expr": "settings.channel"}}, settings.REF_TYPE, "/workhours/expr"),
        ({"workdays": {"expr": " settings.ratio "}}, settings.REF_TYPE, "/workdays/expr"),
        (
            {"workdays": 2, "warnBefore": {"workhours": {"expr": "settings.nothing + 1"}}},
            settings.REF_UNKNOWN,
            "/warnBefore/workhours/expr",
        ),
        (
            {"workdays": 2, "warnBefore": {"workdays": {"expr": "settings.intake"}}},
            settings.REF_TYPE,
            "/warnBefore/workdays/expr",
        ),
    ],
    ids=["undeclared", "string", "number", "undeclared-in-warn-before", "object-in-warn-before"],
)
def test_a_due_reads_settings(
    package: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    due: dict[str, Any],
    code: str,
    path: str,
) -> None:
    """The same finding with the same path whether the core checks the due or check does."""
    _without_core_due(monkeypatch, mode)
    _with_due(package, due)
    errors, _warnings = _check(package)
    assert _settings_codes(errors) == [("processes/claim.yaml", code, DUE + path)], errors


def test_a_due_of_the_process_reads_settings(
    package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_core_due(monkeypatch, "none")
    _with_due(package, {"workdays": 1})
    _change(
        package,
        lambda d: d["spec"].update({"due": {"workdays": {"expr": "settings.missing"}}}),
        "processes/claim.yaml",
    )
    errors, _warnings = _check(package)
    assert _settings_codes(errors) == [
        ("processes/claim.yaml", settings.REF_UNKNOWN, "/spec/due/workdays/expr")
    ]


def test_an_older_core_leaves_the_types_of_a_compound_due_to_the_plan(
    package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_core_due(monkeypatch, "older")
    _with_due(package, {"workdays": {"expr": "settings.channel + 1"}})
    errors, warnings = _check(package)
    assert errors == []
    assert settings.CORE_DUE_TYPES_MISSING in warnings


def test_due_expressions_are_found_wherever_a_due_stands() -> None:
    spec = {
        "due": {"workdays": {"expr": "a"}, "warnBefore": {"workhours": {"expr": "b"}}},
        "stages": [
            {"steps": [{"id": "x", "approve": {"due": {"workhours": {"expr": "c"}}}}]},
            {"steps": [{"id": "y", "human": {"due": "P1D"}}, {"id": "z", "call": {"due": None}}]},
        ],
        "data": {"properties": {"due": {"type": "string"}}},
    }
    assert list(settings.due_expressions(spec)) == [
        ("/spec/due/workdays/expr", "a"),
        ("/spec/due/warnBefore/workhours/expr", "b"),
        ("/spec/stages/0/steps/0/approve/due/workhours/expr", "c"),
    ]
    assert list(settings.due_expressions({"due": {"workdays": 3, "warnBefore": "PT1H"}})) == []
    assert list(settings.due_expressions({"due": {"workdays": {"expr": 3}}})) == []
    assert list(settings.due_expressions(None)) == []


def test_only_a_whole_read_of_a_due_is_typed_here() -> None:
    spec = {
        "due": {
            "workdays": {"expr": "settings.a"},
            "warnBefore": {"workdays": {"expr": "settings.b + settings.c[0]"}},
        }
    }
    assert [(r.path, r.use) for r in settings.due_reads(spec)] == [
        ("settings.a", "count"),
        ("settings.b", None),
        ("settings.c[0]", None),
    ]
    assert settings.due_reads({"due": {"workdays": {"expr": "'settings.a'.size()"}}}) == []


def _set_rule(directory: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    _change(directory, lambda d: edit(d["spec"]), "rules/claim-escalated.yaml")


@pytest.mark.parametrize(
    ("edit", "code", "path"),
    [
        (
            lambda s: s["condition"]["and"][0]["gt"].__setitem__(1, {"var": "settings.limit"}),
            settings.REF_UNKNOWN,
            "/spec/condition/and/0/gt/1/var",
        ),
        (
            lambda s: s["condition"]["and"][0]["gt"].__setitem__(1, {"var": "settings.intake"}),
            settings.REF_TYPE,
            "/spec/condition/and/0/gt/1/var",
        ),
        (
            lambda s: s["condition"]["and"][1].update({"eq": [{"var": "settings.intake"}, 1]}),
            settings.REF_TYPE,
            "/spec/condition/and/1/eq/0/var",
        ),
        (
            lambda s: s["condition"]["and"].append(
                {"in": [{"var": "payload.tag"}, {"var": "settings.channel"}]}
            ),
            settings.REF_TYPE,
            "/spec/condition/and/2/in/1/var",
        ),
        (
            lambda s: s["condition"]["and"].append({"exists": "settings.intake.inbox"}),
            settings.REF_UNKNOWN,
            "/spec/condition/and/2/exists",
        ),
        (
            lambda s: s["action"]["fields"].update({"title": "Claim {{settings.intake}}"}),
            settings.REF_TYPE,
            "/spec/action/fields/title",
        ),
        (
            lambda s: s["action"].update({"dedupKeyTemplate": "claim:{{settings.nothing}}"}),
            settings.REF_UNKNOWN,
            "/spec/action/dedupKeyTemplate",
        ),
    ],
    ids=[
        "undeclared",
        "object-in-a-comparison",
        "object-in-eq",
        "string-as-a-list",
        "undeclared-in-exists",
        "object-inside-text",
        "undeclared-in-a-template",
    ],
)
@pytest.mark.parametrize("mode", ["core", "static"])
def test_a_rule_reads_settings(
    package: Path,
    monkeypatch: pytest.MonkeyPatch,
    edit: Callable[[dict[str, Any]], None],
    code: str,
    path: str,
    mode: str,
) -> None:
    """The same finding with the same path whether the core checks the rule or check does."""
    _rule_mode(monkeypatch, mode)
    _set_rule(package, edit)
    errors, _warnings = _check(package)
    assert _settings_codes(errors) == [("rules/claim-escalated.yaml", code, path)], errors


def _rule_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """core — the core's check_settings_refs; static — check's own reading of the rule."""
    if mode == "core":
        core = settings.core_settings()
        if core is None or core.rules is None:
            pytest.skip("the core next to the SDK does not check the settings of rules")
    else:
        monkeypatch.setattr(settings, "core_settings", lambda: None)


def test_with_the_core_a_rule_is_checked_by_check_settings_refs(
    package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = settings.core_settings()
    if core is None or core.rules is None:
        pytest.skip("the core next to the SDK does not check the settings of rules")
    calls: list[Any] = []
    original = core.rules.check_settings_refs

    def spy(spec: Any, scope: Any) -> None:
        calls.append(scope)
        original(spec, scope)

    monkeypatch.setattr(core.rules, "check_settings_refs", spy)
    assert _check(package)[0] == []
    assert [(s.package, s.schema is not None) for s in calls] == [(KEY, True)]


def test_without_the_core_a_rule_reports_every_read(
    package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core stops at the first read of a rule that does not fit; check alone reports all."""

    def edit(spec: dict[str, Any]) -> None:
        spec["condition"]["and"][0]["gt"][1] = {"var": "settings.limit"}
        spec["action"]["dedupKeyTemplate"] = "claim:{{settings.nothing}}"

    _set_rule(package, edit)
    unknown = ("rules/claim-escalated.yaml", settings.REF_UNKNOWN)
    monkeypatch.setattr(settings, "core_settings", lambda: None)
    assert _settings_codes(_check(package)[0]) == [
        (*unknown, "/spec/condition/and/0/gt/1/var"),
        (*unknown, "/spec/action/dedupKeyTemplate"),
    ]
    monkeypatch.undo()
    core = settings.core_settings()
    if core is None or core.rules is None:
        return
    assert _settings_codes(_check(package)[0]) == [(*unknown, "/spec/condition/and/0/gt/1/var")]


def test_the_field_of_a_core_error_is_the_path_of_the_plan() -> None:
    assert settings._field_pointer("condition.and[0].gt[1].var") == (
        "/spec/condition/and/0/gt/1/var"
    )
    assert settings._field_pointer("action.fields.title") == "/spec/action/fields/title"
    assert settings._field_pointer(None) == ""
    assert settings._field_pointer("") == ""


def test_a_rule_may_read_any_field_whole_and_tags_as_a_list(package: Path) -> None:
    def edit(spec: dict[str, Any]) -> None:
        spec["condition"]["and"].append({"in": [{"var": "payload.tag"}, {"var": "settings.tags"}]})
        spec["condition"]["and"].append({"exists": "settings.intake"})
        spec["action"]["fields"]["description"] = "{{settings.intake}}"

    _set_rule(package, edit)
    assert _check(package)[0] == []


@pytest.mark.parametrize("mode", ["static", "core"])
def test_a_view_reads_an_undeclared_field(
    package: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """The core's view check finds it when it knows settings; the SDK's static check
    otherwise — never both."""
    from package_sdk import screens

    if mode == "static":
        monkeypatch.setattr(screens, "core_views", lambda: None)
    else:
        views = screens.core_views()
        if views is None or not screens.settings_aware(views.views):
            pytest.skip("the core next to the SDK predates CP-ADR-0081 views")
    (package / "views").mkdir()
    view = {
        "apiVersion": "taimen.ai/v1",
        "kind": "View",
        "key": "claim-list",
        "spec": {
            "title": f"{KEY}.list.title",
            "source": {"process": "claim", "filter": "data.amount > settings.refundLimit"},
            "layout": [{"block": "table", "columns": [{"field": "data.ticketId"}]}],
        },
    }
    _write(package / "views" / "claim-list.yaml", view)
    _add_labels(package, f"{KEY}.list.title", f"{KEY}.fields.ticketId")
    errors, _warnings = _check(package)
    assert [c for c in _settings_codes(errors) if c[0].startswith("views/")] == [
        ("views/claim-list.yaml", settings.REF_UNKNOWN, "/spec/source/filter")
    ], errors


def test_the_catalog_packages_have_no_settings_findings() -> None:
    """Acceptance: no package of the catalog snapshot gets a new finding."""
    for directory in sorted(p.parent for p in UMBRELLA.glob("*/package.yaml")):
        installation = model.resolve_targets([str(directory)])
        assert settings.check_settings(installation) == ([], []), directory.name


# --- describe ------------------------------------------------------------------------------


def test_describe_lists_the_settings_next_to_the_variables(package: Path) -> None:
    described, installation, _problem = manifest.load_for_describe(package)
    info = manifest.describe(described, installation)
    fields = {f["name"]: f for f in info["settings"]}
    assert list(fields) == [
        "refundThreshold",
        "escalate",
        "escalationRole",
        "channel",
        "tags",
        "intake.mailbox",
    ]
    assert fields["escalationRole"] == {
        "name": "escalationRole",
        "package": KEY,
        "type": "string",
        "required": True,
        "default": None,
        "hasDefault": False,
        "enum": None,
        "ref": "role",
    }
    assert fields["tags"]["type"] == "array of string"
    text = manifest.format_describe(info)
    assert text.index("variables:") < text.index("settings (") < text.index("agents and nodes:")
    assert "  refundThreshold [integer, default 50000] (claims-intake)" in text
    assert "  escalate [boolean, default true] (claims-intake)" in text
    assert "  escalationRole [string, ref role, required] (claims-intake)" in text


def test_describe_of_a_package_without_settings(package: Path) -> None:
    _change(package, lambda d: d["spec"].pop("settings"))
    described, installation, _problem = manifest.load_for_describe(package)
    info = manifest.describe(described, installation)
    assert info["settings"] == []
    assert "settings (changed by an administrator in the live system):\n  (none)" in (
        manifest.format_describe(info)
    )


def test_describe_json_from_the_cli(package: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["describe", str(package), "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert [f["name"] for f in info["settings"]][:2] == ["refundThreshold", "escalate"]


# --- the sandbox ---------------------------------------------------------------------------


def _core_has_settings() -> bool:
    pytest.importorskip("control_plane", reason="the sandbox runs the core's code (extra sandbox)")
    return sandbox._domain()["package_settings"] is not None


def test_the_sandbox_passes_the_settings_to_the_core(package: Path) -> None:
    if not _core_has_settings():
        pytest.skip("the core next to the SDK predates CP-ADR-0081")
    report = sandbox.run_package(package, env={})
    assert report["status"] == "passed", report
    assert {t["file"]: t["status"] for t in report["tests"]} == {
        "tests/claim.test.yaml": "passed",
        "tests/claim-threshold-raised.test.yaml": "passed",
    }


def test_the_sandbox_checks_the_saved_values(package: Path) -> None:
    if not _core_has_settings():
        pytest.skip("the core next to the SDK predates CP-ADR-0081")
    test = _read(package / "tests" / "claim.test.yaml")
    test["given"]["settings"]["refundThreshold"] = "high"
    _write(package / "tests" / "claim.test.yaml", test)
    report = sandbox.run_package(package, tests=["tests/claim.test.yaml"], env={})
    assert report["status"] == "failed"
    assert "settings_invalid" in json.dumps(report["tests"][0]["failures"])


@pytest.mark.parametrize(("value", "status"), [("claim-decision", "passed"), ("missing", "failed")])
def test_an_x_ref_to_a_task_type_must_name_one_of_the_catalog(
    package: Path, value: str, status: str
) -> None:
    if not _core_has_settings():
        pytest.skip("the core next to the SDK predates CP-ADR-0081")
    _change(
        package,
        _add_field(
            "followUp", {"type": "string", "x-ref": "taskType", "default": "claim-decision"}
        ),
    )
    _add_labels(package, f"{KEY}.settings.followUp")
    test = _read(package / "tests" / "claim.test.yaml")
    test["given"]["settings"]["followUp"] = value
    _write(package / "tests" / "claim.test.yaml", test)
    report = sandbox.run_package(package, tests=["tests/claim.test.yaml"], env={})
    assert report["status"] == status, report
    if status == "failed":
        assert "unknown_ref" in json.dumps(report["tests"][0]["failures"])


def _core_counts_due_expressions() -> bool:
    core = settings.core_settings() if _core_has_settings() else None
    return core is not None and bool(core.due)


def _due_scenario(package: Path, steps: list[dict[str, Any]]) -> None:
    """Monday 10:00, office hours 09:00–18:00: a review of reviewDays workdays with a warning
    warnHours working hours before it."""
    _with_due(
        package,
        {
            "workdays": {"expr": "settings.reviewDays"},
            "warnBefore": {"workhours": {"expr": "settings.warnHours"}},
        },
    )
    test = _read(package / "tests" / "claim.test.yaml")
    test["given"]["clock"] = "2026-10-05T10:00:00+03:00"
    test["given"]["settings"].update({"reviewDays": 1, "warnHours": 2})
    test["steps"] = [*steps, *test["steps"]]
    _write(package / "tests" / "claim.test.yaml", test)


def test_the_sandbox_counts_a_due_from_the_settings(package: Path) -> None:
    """One workday from Monday 10:00 is Tuesday 10:00; two working hours before it — Monday
    17:00: the warning comes after seven hours, the breach after the next seventeen."""
    if not _core_counts_due_expressions():
        pytest.skip("the core next to the SDK does not count deadline expressions")
    _due_scenario(package, [])
    test = _read(package / "tests" / "claim.test.yaml")
    test["steps"] += [
        {"advance": "PT6H"},
        {"expect": {"sla": {"decide-claim": "ok"}}},
        {"advance": "PT1H"},
        {"expect": {"events": ["process.sla_warning"], "sla": {"decide-claim": "warning"}}},
        {"advance": "PT17H"},
        {"expect": {"events": ["process.sla_breached"], "sla": {"decide-claim": "breached"}}},
    ]
    _write(package / "tests" / "claim.test.yaml", test)
    report = sandbox.run_package(package, tests=["tests/claim.test.yaml"], env={})
    assert report["status"] == "passed", report


def test_settings_saved_before_the_step_move_its_due(package: Path) -> None:
    if not _core_counts_due_expressions():
        pytest.skip("the core next to the SDK does not count deadline expressions")
    saved = {"escalationRole": "claims", "refundThreshold": 50000, "reviewDays": 2, "warnHours": 2}
    _due_scenario(package, [{"settings": saved}])
    test = _read(package / "tests" / "claim.test.yaml")
    test["steps"] += [
        {"advance": "PT24H"},
        {"expect": {"sla": {"decide-claim": "ok"}}},
    ]
    _write(package / "tests" / "claim.test.yaml", test)
    report = sandbox.run_package(package, tests=["tests/claim.test.yaml"], env={})
    assert report["status"] == "passed", report


def test_settings_saved_after_the_step_leave_its_due(package: Path) -> None:
    """The due was computed at the step's entry: a later save does not move it."""
    if not _core_counts_due_expressions():
        pytest.skip("the core next to the SDK does not count deadline expressions")
    saved = {"escalationRole": "claims", "refundThreshold": 50000, "reviewDays": 2, "warnHours": 2}
    _due_scenario(package, [])
    test = _read(package / "tests" / "claim.test.yaml")
    test["steps"] += [
        {"settings": saved},
        {"advance": "PT24H"},
        {"expect": {"sla": {"decide-claim": "breached"}}},
    ]
    _write(package / "tests" / "claim.test.yaml", test)
    report = sandbox.run_package(package, tests=["tests/claim.test.yaml"], env={})
    assert report["status"] == "passed", report


def test_an_older_core_refuses_a_due_expression_loudly(package: Path) -> None:
    if not _core_has_settings():
        pytest.skip("the core next to the SDK predates CP-ADR-0081")
    if _core_counts_due_expressions():
        pytest.skip("the core next to the SDK counts deadline expressions")
    _due_scenario(package, [])
    report = sandbox.run_package(package, tests=["tests/claim.test.yaml"], env={})
    assert report["status"] == "invalid"
    assert [p["path"] for p in report["problems"]] == [DUE], report


def test_an_older_core_refuses_settings_loudly(package: Path) -> None:
    if _core_has_settings():
        pytest.skip("the core next to the SDK knows package settings")
    report = sandbox.run_package(package, env={})
    assert report["status"] == "invalid"
    codes = [p["code"] for p in report["problems"]]
    assert sandbox.SETTINGS_UNSUPPORTED in codes


def test_tests_that_save_settings_are_collected() -> None:
    test = model.PackageTest(
        KEY,
        Path("tests/x.test.yaml"),
        {"given": {"settings": {"a": 1}}, "steps": [{"settings": {"a": 2}}, {"emit": {}}]},
    )
    assert sandbox._test_settings(test) == [{"a": 1}, {"a": 2}]
    assert sandbox._test_settings(model.PackageTest(KEY, Path("t"), None)) == []
    assert sandbox._test_settings(model.PackageTest(KEY, Path("t"), {"given": None})) == []


# --- contracts with the core ---------------------------------------------------------------


def test_the_secret_names_are_the_cores() -> None:
    project = pytest.importorskip("control_plane.domain.project")
    assert settings.SECRET_KEY_HINTS == project._SECRET_KEY_HINTS
    assert settings.SECRET_KEY_ALLOWED == project._SECRET_KEY_ALLOWED
    for name in ("apiToken", "secretRef", "routingKey", "privateKey", "client-secret"):
        assert settings.secret_key_name(name) == project.secret_key_name(name), name


def test_the_secret_material_is_the_cores() -> None:
    redaction = pytest.importorskip("control_plane.domain.redaction")
    ours = [(kind, p.pattern, p.flags) for kind, p in settings.SECRET_MATERIAL_PATTERNS]
    theirs = [(kind, p.pattern, p.flags) for kind, p in redaction._SECRET_MATERIAL_PATTERNS]
    assert ours == theirs


def _core_declaration() -> Any:
    try:
        from control_plane.domain import package_settings, package_source
    except ImportError:
        pytest.skip("the core next to the SDK predates CP-ADR-0081")
    return package_settings, package_source


# Cases the core finds with the same code and path (the others differ in the path only: the
# core names a too deep object by its field, a secret name by its field, and so on).
CORE_AGREES = {
    "arrays-of-arrays-nested-too-deep",
    "enum-on-an-array",
    "min-items-not-a-count",
    "max-items-on-a-string",
    "default-below-min-items",
    "write-only-false-is-outside-the-subset",
    "options-of-a-control",
    "x-ref-in-a-rule-condition",
    "secret-name-token",
    "secret-name-password",
    "secret-write-only",
    "secret-format-password",
    "secret-material-in-default",
    "required-names-a-missing-field",
    "default-of-another-type",
    "default-above-maximum",
    "default-outside-enum",
    "default-item-against-the-pattern",
    "optional-field-without-default",
    "enum-on-an-object",
    "title-in-the-schema",
    "x-ref-on-an-integer",
    "root-is-not-an-object",
    "scope-of-a-missing-property",
    "two-controls-on-one-property",
    "group-label-outside-settings-groups",
}


@pytest.mark.parametrize("case", [c for c in BROKEN if c[0] in CORE_AGREES], ids=lambda c: c[0])
def test_the_core_finds_the_same(package: Path, case: Any) -> None:
    """The plan of the core repeats check with the same codes and paths (CP-ADR-0081 §1)."""
    package_settings, package_source = _core_declaration()
    _name, change, code, path = case
    _change(package, change)
    files = [
        (p.relative_to(package).as_posix(), p.read_text(encoding="utf-8"))
        for p in sorted(package.rglob("*.yaml"))
    ]
    found = package_settings.check_declaration(package_source.parse_package(files)).problems
    assert (code, path) in {(p.code, p.path) for p in found}


def test_the_core_accepts_the_fixture() -> None:
    package_settings, package_source = _core_declaration()
    files = [
        (p.relative_to(FIXTURE).as_posix(), p.read_text(encoding="utf-8"))
        for p in sorted(FIXTURE.rglob("*.yaml"))
    ]
    declaration = package_settings.check_declaration(package_source.parse_package(files))
    assert [p for p in declaration.problems if p.error] == []
    assert declaration.declared is not None


# The subset of CP-ADR-0081 §1–2: the sets of keywords and elements of check, of the format
# schema and of the core are one rule (amendment А1: the SDK catches up with the ADR).


def _format_defs() -> dict[str, Any]:
    from package_sdk import schema as schema_module

    defs: dict[str, Any] = schema_module.load(schema_module.OBJECT)["$defs"]
    return defs


def _ui_fields_of_the_format() -> dict[str, frozenset[str]]:
    found: dict[str, frozenset[str]] = {}
    for branch in _format_defs()["settingsUiElement"]["allOf"]:
        kind = branch["if"]["properties"]["type"]
        for name in kind.get("enum") or [kind.get("const")]:
            found[name] = frozenset(branch["then"]["properties"])
    return found


def test_the_subset_of_check_is_the_format_schemas() -> None:
    defs = _format_defs()
    assert _ui_fields_of_the_format() == settings._UI_FIELDS
    rule = defs["settingsUiRule"]["properties"]
    assert set(rule["effect"]["enum"]) == set(settings._RULE_EFFECTS)
    assert frozenset(rule["condition"]["properties"]["schema"]["properties"]) == (
        settings._RULE_SCHEMA_KEYWORDS
    )
    keywords = defs["settingsKeywords"]["properties"]
    assert frozenset(keywords) == settings._ALL_KEYWORDS
    assert set(keywords["type"]["enum"]) == set(settings.TYPES)
    assert set(keywords["format"]["enum"]) == set(settings.FORMATS)
    assert set(keywords["x-ref"]["enum"]) == set(settings.REF_KINDS)


def test_the_subset_of_check_is_the_cores() -> None:
    """Contract with the core next to the SDK: the same keywords by type, layout elements and
    their fields, rule conditions and limits (CP-ADR-0081 §1–2)."""
    core, _package_source = _core_declaration()
    assert settings._COMMON == core._COMMON
    assert settings._BY_TYPE == core._BY_TYPE
    assert settings._LABEL_KEYWORDS == core._LABEL_KEYWORDS
    assert settings._UI_FIELDS == core._UI_FIELDS
    assert settings._UI_ROOTS == core._UI_ROOTS
    assert settings._LAYOUTS == core._LAYOUTS
    assert settings._RULE_EFFECTS == core._RULE_EFFECTS
    assert settings._RULE_SCHEMA_KEYWORDS == core._RULE_SCHEMA_KEYWORDS
    for name in (
        "REF",
        "REF_KINDS",
        "TYPES",
        "SCALARS",
        "FORMATS",
        "MAX_OBJECT_DEPTH",
        "MAX_PROPERTIES",
        "MAX_ENUM",
        "MAX_UI_DEPTH",
        "MAX_UI_ELEMENTS",
        "SCOPE_PREFIX",
        "SCOPE_STEP",
    ):
        assert getattr(settings, name) == getattr(core, name), name
    assert settings.FIELD_NAME.pattern == core.FIELD_NAME.pattern
    assert settings.LABEL_KEY.pattern == core.LABEL_KEY.pattern


# Declarations the core accepts and check must accept too: where the SDK used to be stricter.
ACCEPTED: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
    ("enum-on-a-boolean", lambda d: _props(d)["escalate"].update({"enum": [True, False]})),
    ("min-and-max-items", lambda d: _props(d)["tags"].update({"minItems": 0, "maxItems": 5})),
    (
        "an-array-of-arrays",
        lambda d: _props(d)["tags"].update(
            {"items": {"type": "array", "items": {"type": "string"}}, "default": [["a"]]}
        ),
    ),
    (
        "arrays-of-arrays-of-scalars-at-the-deepest-level",
        # intake.mailbox — a labelled field at the deepest level of objects
        lambda d: _props(d)["intake"]["properties"].update(
            {
                "mailbox": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "integer"}},
                    "default": [],
                }
            }
        ),
    ),
    (
        "additional-properties-false",
        lambda d: _props(d)["intake"].update({"additionalProperties": False}),
    ),
    (
        "a-rule-condition-with-the-keywords-of-a-field",
        lambda d: d["spec"]["settings"]["uischema"]["elements"][0]["elements"][2]["rule"][
            "condition"
        ].update({"schema": {"type": "boolean", "enum": [True]}}),
    ),
]


@pytest.mark.parametrize("change", [c[1] for c in ACCEPTED], ids=[c[0] for c in ACCEPTED])
def test_check_accepts_what_the_core_accepts(
    package: Path, change: Callable[[dict[str, Any]], None]
) -> None:
    _change(package, change)
    errors, _warnings = _check(package)
    assert errors == []
    try:
        core, package_source = _core_declaration()
    except pytest.skip.Exception:
        return
    files = [
        (p.relative_to(package).as_posix(), p.read_text(encoding="utf-8"))
        for p in sorted(package.rglob("*.yaml"))
    ]
    found = core.check_declaration(package_source.parse_package(files)).problems
    assert [p for p in found if p.error] == []
