"""Фильтр автора и закрытие задачи наблюдения в правилах вывода работы (CP-ADR-0063 Ж1, Ж2,
Ж5, Ж6; фича integrations-connections): ``trigger.agent``, ``action.target: task``, в тестах
процесса — ``emit.task`` и ``expect.rules``.

Перенесено из ветки feature/integrations-connections суперпроекта (схемы
``packages/schema/v1``, ``tools/cp_packages.py``) — TASK-001146.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from package_sdk import edit, schema
from tests.umbrella._shim import UMBRELLA, cp, settled

OBSERVER = {
    "kind": "Agent",
    "key": "helpdesk-observer",
    "spec": {
        "displayName": "Helpdesk observer",
        "identity": {"kind": "service", "permissions": ["observations.write"]},
        "placement": "none",
    },
}


@pytest.fixture
def packages(tmp_path, monkeypatch):
    root = tmp_path / "packages"
    shutil.copytree(UMBRELLA / "packages", root)
    monkeypatch.setattr(cp, "PACKAGES_DIR", root)
    return root


def _package(root: Path, docs: list[dict], variables: dict | None = None) -> None:
    directory = root / "helpdesk"
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir()
    spec: dict[str, Any] = {"version": "0.1.0", "displayName": "helpdesk", "requires": []}
    if variables:
        spec["variables"] = variables
    manifest = {"apiVersion": cp.API_VERSION, "kind": "Package", "key": "helpdesk", "spec": spec}
    (directory / "package.yaml").write_text(json.dumps(manifest), encoding="utf-8")
    for doc in docs:
        folder = directory / cp.FOLDERS[doc["kind"]]
        folder.mkdir(exist_ok=True)
        (folder / f"{doc['key']}.yaml").write_text(
            json.dumps({"apiVersion": cp.API_VERSION, **doc}), encoding="utf-8"
        )


def _rule(agent: str = "helpdesk-observer", **action: Any) -> dict:
    return {
        "kind": "WorkRule",
        "key": "helpdesk-ticket-closed",
        "spec": {
            "description": "Closes the task the ticket is bound to when the ticket is closed",
            "trigger": {"kind": "observation", "type": "helpdesk.ticket_closed", "agent": agent},
            "action": {
                "kind": "complete_work",
                "target": "task",
                "taskTypes": ["task"],
                **action,
            },
        },
    }


def _knows_author_filter() -> bool:
    domain = cp._domain()
    return domain is not None and hasattr(domain.work_rules, "author_matches")


# --- схема --------------------------------------------------------------------------------


@pytest.mark.parametrize("agent", ["helpdesk-observer", "${HELPDESK_OBSERVER}"])
def test_schema_accepts_an_agent_key_or_an_installation_variable(agent):
    assert schema.errors(schema.OBJECT, {"apiVersion": cp.API_VERSION, **_rule(agent)}) == []


@pytest.mark.parametrize(
    ("over", "needle"),
    [({"agent": "Bad Key"}, "agent"), ({"target": "anything"}, "target")],
)
def test_schema_rejects_a_malformed_author_or_target(over, needle):
    doc = _rule()
    if "agent" in over:
        doc["spec"]["trigger"]["agent"] = over["agent"]
    else:
        doc["spec"]["action"]["target"] = over["target"]
    errors = schema.errors(schema.OBJECT, {"apiVersion": cp.API_VERSION, **doc})
    assert errors and any(needle in str(e) for e in errors), errors


def test_the_test_schema_binds_an_observation_to_a_step_and_expects_rule_decisions():
    test = {
        "process": "p",
        "name": "n",
        "steps": [
            {"emit": {"observation": "helpdesk.ticket_closed", "task": "check", "payload": {}}},
            {
                "expect": {
                    "rules": [
                        {
                            "rule": "helpdesk-ticket-closed",
                            "action": "complete_work",
                            "step": "check",
                            "result": "matched",
                        }
                    ]
                }
            },
        ],
    }
    assert schema.errors(schema.TEST, test) == []
    test["steps"][1]["expect"]["rules"][0]["result"] = "maybe"
    assert schema.errors(schema.TEST, test)


# --- check --------------------------------------------------------------------------------


def test_a_literal_author_is_an_agent_of_the_package(packages):
    _package(packages, [OBSERVER, _rule()])
    errors, _ = cp.check(cp.resolve(["helpdesk"]))
    assert errors == []
    _package(packages, [OBSERVER, _rule("helpdesk-nobody")])
    errors, _ = cp.check(cp.resolve(["helpdesk"]))
    assert any("trigger.agent references agent 'helpdesk-nobody'" in e for e in errors), errors


def test_an_author_named_by_the_installation_is_checked_without_its_value(packages):
    """${ПЕРЕМЕННАЯ} без значения проверяется ключом-заглушкой (адрес-заглушку ядро отвергло бы
    по форме), заданная — как есть."""
    variables = {"HELPDESK_OBSERVER": {"kind": "string", "description": "observer agent"}}
    _package(packages, [_rule("${HELPDESK_OBSERVER}")], variables)
    errors, warnings = cp.check(cp.resolve(["helpdesk"]), env={})
    assert errors == []
    if _knows_author_filter():
        errors, _ = cp.check(cp.resolve(["helpdesk"]), env={"HELPDESK_OBSERVER": "Bad Key"})
        assert any("invalid_rule_trigger" in e for e in errors), errors
    elif cp._domain() is not None:
        assert any("trigger.agent" in w and "Ж6" in w for w in warnings), warnings


def test_a_core_without_the_author_filter_leaves_the_rule_to_the_stand(packages):
    """Ядро рядом без CP-ADR-0063 Ж1, Ж6: правило с target: task не нормализуется ядром, check
    предупреждает; ядро с ними проверяет правило целиком."""
    if cp._domain() is None:
        pytest.skip("the core's domain validators are not importable")
    _package(packages, [OBSERVER, _rule()])
    errors, warnings = settled(cp.check(cp.resolve(["helpdesk"])))
    assert errors == []
    unchecked = [w for w in warnings if "action.target" in w]
    assert bool(unchecked) is not _knows_author_filter()
    if not _knows_author_filter():
        assert "the rest of the rule is not checked" in unchecked[0]


def test_target_task_is_checked_by_a_core_that_knows_it(packages):
    if not _knows_author_filter():
        pytest.skip("the core next to the SDK predates target: task (CP-ADR-0063 Ж1)")
    rule = _rule()
    del rule["spec"]["trigger"]["agent"]
    _package(packages, [OBSERVER, rule])
    errors, _ = cp.check(cp.resolve(["helpdesk"]))
    assert any("invalid_rule_trigger" in e and "trigger.agent" in e for e in errors), errors
    _package(packages, [OBSERVER, _rule(dedupKeyTemplate="x:{{payload.id}}")])
    errors, _ = cp.check(cp.resolve(["helpdesk"]))
    assert any("dedupKeyTemplate" in e for e in errors), errors


def _with_steps(packages: Path, *lines: str) -> None:
    path = packages / "invoice-payment" / "tests" / "below-threshold.test.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "".join(lines), encoding="utf-8")


def test_emit_task_and_expected_rule_steps_are_steps_of_the_process(packages):
    assert cp.check(cp.resolve(["invoice-payment"]))[0] == []
    _with_steps(
        packages,
        "  - emit: {observation: helpdesk.ticket_closed, task: check-invoice, payload: {}}\n",
        "  - expect: {rules: [{rule: r, step: check-invoice, result: not_matched}]}\n",
    )
    assert cp.check(cp.resolve(["invoice-payment"]))[0] == []
    _with_steps(
        packages,
        "  - emit: {observation: helpdesk.ticket_closed, task: no-such-step, payload: {}}\n",
        "  - expect: {rules: [{rule: r, step: nowhere, result: skipped}]}\n",
        "  - emit: {event: task.updated, task: check-invoice, payload: {}}\n",
    )
    errors = cp.check(cp.resolve(["invoice-payment"]))[0]
    assert any("emit.task 'no-such-step'" in e for e in errors), errors
    assert any("expect.rules step 'nowhere'" in e for e in errors), errors
    assert any("only an observation is bound" in e for e in errors), errors


def test_renaming_a_step_renames_emit_task_and_expected_rule_steps():
    test = {
        "steps": [
            {"emit": {"observation": "o", "task": "check", "payload": {}}},
            {"expect": {"rules": [{"rule": "r", "step": "check", "result": "matched"}]}},
        ]
    }
    assert edit._rename_in_test(test, "check", "review") == 2
    assert test["steps"][0]["emit"]["task"] == "review"
    assert test["steps"][1]["expect"]["rules"][0]["step"] == "review"


def test_a_rule_with_target_task_is_applied_once(packages):
    """Ядро с фильтром автора хранит trigger.agent и target как есть: повторная установка —
    без записи (сравнение нормализованным правилом ядра)."""
    if not _knows_author_filter():
        pytest.skip("the core next to the SDK predates target: task (CP-ADR-0063 Ж1)")
    from tests.umbrella.test_cp_packages import FakeControlPlane, run_apply

    _package(packages, [OBSERVER, _rule()])
    fake = FakeControlPlane()
    run_apply(fake, cp.resolve(["helpdesk"]), env={})
    [row] = fake.rows["rules"]
    assert row["trigger"]["agent"] == "helpdesk-observer" and row["action"]["target"] == "task"
    writes = list(fake.writes)
    _, lines = run_apply(fake, cp.resolve(["helpdesk"]), env={})
    assert fake.writes == writes
    assert any("WorkRule/helpdesk-ticket-closed" in line and "unchanged" in line for line in lines)
