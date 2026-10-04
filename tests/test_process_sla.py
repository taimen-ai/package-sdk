"""Сроки процесса (SLA): due процесса и шагов, workingHours календаря, expect.sla теста.

Фикстура — пакет ``tests/fixtures/sla/packages/sla-demo``: календарь с рабочими часами,
процесс со сроком в рабочих днях и шагом со сроком в рабочих часах, сценарий, который
читает состояние сроков. Схема и статика check идут всегда; сценарий исполняет песочница
кодом ядра, если ядро знает сроки (модуль process_sla), иначе он пропускается.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from package_sdk import check, cli, model, schema

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sla" / "packages" / "sla-demo"
PROCESS = "processes/request-sla.yaml"
CALENDAR = "calendars/office.yaml"
TEST = "tests/request-sla.test.yaml"


@pytest.fixture
def package(tmp_path: Path) -> Path:
    """Копия фикстуры, которую тест может портить."""
    root = tmp_path / "packages" / "sla-demo"
    shutil.copytree(FIXTURE, root)
    return root


def _load(path: Path) -> Any:
    """Как читает пакет package-sdk: булевы YAML 1.2, ключ on — строка."""
    return model._read_yaml(path)


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def _check(directory: Path) -> tuple[list[str], list[str]]:
    return check.check(model.resolve_targets([str(directory)]))


# --- схема ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [PROCESS, CALENDAR])
def test_objects_with_deadlines_and_working_hours_match_the_schema(name: str) -> None:
    assert schema.errors(schema.OBJECT, _load(FIXTURE / name)) == []


def test_a_test_reading_the_state_of_deadlines_matches_the_schema() -> None:
    assert schema.errors(schema.TEST, _load(FIXTURE / TEST)) == []


@pytest.mark.parametrize(
    "due",
    [
        "P2D",
        {"at": "timestamp(data.deadline)"},
        {"duration": "PT4H", "warnBefore": "PT1H"},
        {"workdays": 2, "calendar": "office"},
        {"workhours": 1.5, "warnBefore": {"workhours": 0.5}},
        {"workdays": 5, "warnBefore": {"workdays": 1}},
        # число рабочих единиц выражением (CP-ADR-0081 Г1): срока и его warnBefore
        {"workdays": {"expr": "settings.reviewDays"}, "warnBefore": {"workhours": 4}},
        {"workhours": {"expr": "settings.hours * 2"}, "calendar": "office"},
        {"workdays": 5, "warnBefore": {"workdays": {"expr": "settings.warnDays"}}},
        {"duration": "PT4H", "warnBefore": {"workhours": {"expr": "data.warnHours"}}},
    ],
)
def test_every_form_of_a_due_is_accepted_on_a_step_and_the_process(due: Any) -> None:
    process = _load(FIXTURE / PROCESS)
    process["spec"]["due"] = due
    process["spec"]["stages"][0]["steps"][0]["human"]["due"] = due
    assert schema.errors(schema.OBJECT, process) == []


@pytest.mark.parametrize(
    "due",
    [
        {"workdays": 2, "workhours": 4},  # ровно одна единица
        {"warnBefore": "PT1H"},  # без единицы
        {"workdays": 0},
        {"workdays": 1.5},
        {"workhours": 0},
        {"workdays": 1, "warnBefore": {"workdays": 1, "workhours": 1}},
        {"duration": "PT4H", "extra": 1},
        # выражение — только {expr: <строка>} без других членов
        {"workdays": {}},
        {"workdays": {"expr": 3}},
        {"workdays": {"expr": ""}},
        {"workdays": "settings.reviewDays"},
        {"workhours": {"expr": "settings.hours", "calendar": "office"}},
        {"workdays": 1, "warnBefore": {"workhours": {"cel": "settings.hours"}}},
        {"duration": {"expr": "settings.duration"}},
    ],
)
def test_a_malformed_due_is_rejected(due: Any) -> None:
    process = _load(FIXTURE / PROCESS)
    process["spec"]["due"] = due
    assert schema.errors(schema.OBJECT, process)


@pytest.mark.parametrize("verb", ["call", "recall", "listen"])
def test_steps_with_a_timeout_may_have_a_due_too(verb: str) -> None:
    bodies = {
        "call": {"skill": "request.classify@1", "timeout": "PT1H"},
        "recall": {"anchors": [{"kind": "case", "key": "data.request"}], "timeout": "PT1M"},
        "listen": {"any": [{"on": {"observation": "request.updated"}}], "timeout": "P1D"},
    }
    process = _load(FIXTURE / PROCESS)
    process["spec"]["stages"][0]["steps"].insert(
        0, {"id": "extra", verb: {**bodies[verb], "due": {"workhours": 2}}}
    )
    assert schema.errors(schema.OBJECT, process) == []


def test_a_wait_has_no_due() -> None:
    process = _load(FIXTURE / PROCESS)
    process["spec"]["stages"][0]["steps"].insert(0, {"id": "pause", "wait": "PT1H", "due": "P1D"})
    assert schema.errors(schema.OBJECT, process)


@pytest.mark.parametrize(
    "hours,valid",
    [
        ({"intervals": [{"from": "09:00", "to": "13:00"}, {"from": "14:00", "to": "18:00"}]}, True),
        ({"intervals": [{"from": "00:00", "to": "24:00"}]}, True),
        ({"intervals": [{"from": "09:00", "to": "18:00"}], "weekdays": {"5": []}}, True),
        ({"intervals": []}, False),
        ({"weekdays": {"1": [{"from": "09:00", "to": "18:00"}]}}, False),  # intervals обязательны
        ({"intervals": [{"from": "24:00", "to": "24:00"}]}, False),
        ({"intervals": [{"from": "9:00", "to": "18:00"}]}, False),
        ({"intervals": [{"from": "09:00", "to": "18:00"}], "weekdays": {"8": []}}, False),
        ({"intervals": [{"from": "09:00", "to": "18:00"}], "lunch": "PT1H"}, False),
    ],
)
def test_working_hours_of_a_calendar(hours: dict[str, Any], valid: bool) -> None:
    calendar = _load(FIXTURE / CALENDAR)
    calendar["spec"]["workingHours"] = hours
    assert (schema.errors(schema.OBJECT, calendar) == []) is valid


@pytest.mark.parametrize("state", ["unknown", "none", "late"])
def test_the_state_of_a_deadline_in_a_test_is_one_of_four(state: str) -> None:
    test = _load(FIXTURE / TEST)
    test["steps"][1]["expect"]["sla"] = {"review-request": state}
    assert schema.errors(schema.TEST, test)


# --- check ---------------------------------------------------------------------------


def test_check_accepts_a_package_with_deadlines() -> None:
    assert cli.main(["check", "--package", str(FIXTURE)]) == 0
    errors, warnings = _check(FIXTURE)
    assert errors == []
    assert not [w for w in warnings if "calendar" in w], warnings


def test_working_units_need_a_calendar(package: Path) -> None:
    _edit(package / PROCESS, "  calendar: office\n", "")
    errors, _ = _check(package)
    assert any("spec.due" in e and "workdays" in e and "no calendar" in e for e in errors), errors
    assert any("review-request: human.due" in e for e in errors), errors


def test_the_calendar_of_a_due_replaces_the_process_calendar(package: Path) -> None:
    _edit(package / PROCESS, "  calendar: office\n", "")
    _edit(package / PROCESS, "workdays: 3,", "workdays: 3, calendar: office,")
    _edit(package / PROCESS, "workhours: 8,", "workhours: 8, calendar: office,")
    errors, _ = _check(package)
    assert errors == []


def test_workhours_need_a_calendar_with_working_hours(package: Path) -> None:
    _edit(
        package / CALENDAR,
        '  workingHours:\n    intervals: [{from: "09:00", to: "18:00"}]\n'
        "    shortDayReduction: PT1H\n",
        "",
    )
    errors, _ = _check(package)
    assert any("workhours" in e and "workingHours" in e for e in errors), errors
    # рабочие дни календарю без часов считать можно
    assert not any("spec.due" in e for e in errors), errors


def test_a_calendar_outside_the_package_is_a_warning(package: Path) -> None:
    _edit(package / PROCESS, "workhours: 8,", "workhours: 8, calendar: elsewhere,")
    errors, warnings = _check(package)
    assert errors == []
    assert any("calendar 'elsewhere'" in w for w in warnings), warnings


def test_expect_sla_names_a_step_with_a_due_or_the_process(package: Path) -> None:
    _edit(package / TEST, "sla: {review-request: ok, process: ok}", "sla: {review: ok}")
    errors, _ = _check(package)
    assert any("expect.sla 'review'" in e and "has no due" in e for e in errors), errors
    _edit(package / TEST, "sla: {review: ok}", "sla: {no-such-step: ok}")
    errors, _ = _check(package)
    assert any("expect.sla 'no-such-step'" in e for e in errors), errors
    _edit(package / TEST, "sla: {no-such-step: ok}", "sla: {process: ok}")
    _edit(package / PROCESS, "  due: {workdays: 3, warnBefore: {workdays: 1}}\n", "")
    errors, _ = _check(package)
    assert any("expect.sla.process" in e and "spec.due" in e for e in errors), errors


def test_rename_of_a_step_follows_into_expect_sla(package: Path) -> None:
    pytest.importorskip("ruamel.yaml")
    from package_sdk import edit

    process = package / PROCESS
    assert (
        edit.main(["rename", "--file", str(process), "--from", "review-request", "--to", "triage"])
        == 0
    )
    text = (package / TEST).read_text(encoding="utf-8")
    assert "sla: {triage: ok, process: ok}" in text
    assert "review-request" not in text
    assert _check(package)[0] == []


def test_rename_of_a_step_leaves_expect_sla_of_another_process_alone(package: Path) -> None:
    """Шаг с тем же id в другом процессе — другой шаг: rename меняет ключ expect.sla только
    в тестах своего процесса, тест соседнего процесса остаётся байт в байт."""
    pytest.importorskip("ruamel.yaml")
    from package_sdk import edit

    for folder, old_name, new_name in (
        ("processes", "request-sla.yaml", "other-sla.yaml"),
        ("agents", "request-sla-process.yaml", "other-sla-process.yaml"),
        ("tests", "request-sla.test.yaml", "other-sla.test.yaml"),
    ):
        text = (package / folder / old_name).read_text(encoding="utf-8")
        (package / folder / new_name).write_text(
            text.replace("request-sla", "other-sla"), encoding="utf-8"
        )
    other = package / "tests" / "other-sla.test.yaml"
    before = other.read_text(encoding="utf-8")
    assert "process: other-sla" in before and "sla: {review-request: ok, process: ok}" in before
    assert _check(package)[0] == []

    process = package / PROCESS
    assert (
        edit.main(["rename", "--file", str(process), "--from", "review-request", "--to", "triage"])
        == 0
    )
    assert "sla: {triage: ok, process: ok}" in (package / TEST).read_text(encoding="utf-8")
    assert other.read_text(encoding="utf-8") == before
    assert _check(package)[0] == []


# --- песочница ------------------------------------------------------------------------


def _sandbox() -> Any:
    pytest.importorskip("control_plane", reason="сценарии — кодом ядра (extra sandbox)")
    pytest.importorskip(
        "control_plane.domain.process_sla", reason="ядро без сроков процесса (CP-ADR-0078)"
    )
    from package_sdk import sandbox

    return sandbox


def test_the_sandbox_reads_the_state_of_deadlines() -> None:
    sandbox = _sandbox()
    report = sandbox.run_package(FIXTURE, env={})
    assert report["status"] == "passed", report
    assert [t["status"] for t in report["tests"]] == ["passed"]


def test_the_sandbox_fails_a_wrong_expectation_of_a_deadline(package: Path) -> None:
    sandbox = _sandbox()
    _edit(
        package / TEST,
        "sla: {review-request: warning, process: ok}",
        "sla: {review-request: ok, process: ok}",
    )
    report = sandbox.run_package(package, env={})
    assert report["status"] == "failed", report
