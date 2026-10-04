"""План правила (WorkRule): файл пакета и ответ ядра сравниваются в одной форме (TASK-001389).

Ядро дописывает в каноническую форму правила значения по умолчанию, которых нет в файле
(``"fields": {}`` у ``complete_work``): без общей формы план бесконечно показывает ``action``.
Каждый сценарий — в обоих режимах: с кодом ядра рядом (extra ``sandbox``) и без него.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from package_sdk import apply as apply_module
from package_sdk.apply import Applier, without_empty
from package_sdk.check import _domain
from package_sdk.model import Obj
from package_sdk.schema import OBJECT, load

WORKSPACE = "aaaaaaaa-0000-4000-8000-000000000001"

SPEC: dict[str, Any] = {
    "description": "Release published: the work is done",
    "trigger": {"kind": "observation", "type": "repo.commit_observed"},
    "condition": {"eq": [{"var": "payload.data.repo"}, "project"]},
    "interpretation": {"skill": "oss.check@2", "inputs": {"ref": "{{payload.data.sha}}"}},
    "action": {
        "kind": "complete_work",
        "forEach": "skill.output.current",
        "dedupKeyTemplate": "oss-check:{{item.component}}",
    },
}


class RulesCore:
    """GET /rules с одним правилом; записи в плане не ожидаются."""

    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    def call(
        self, method: str, path: str, body: Any = None, headers: dict | None = None
    ) -> dict[str, Any]:
        assert method == "GET" and path.startswith("/api/v1/rules?"), (method, path)
        return {"items": [copy.deepcopy(self.row)], "nextCursor": None}


def _row(**over: Any) -> dict[str, Any]:
    row = {
        "id": "rule-1",
        "key": "resolved",
        "workspaceId": WORKSPACE,
        "version": 3,
        "status": "enabled",
        "identity": None,
        **copy.deepcopy(SPEC),
    }
    row.update(over)
    return row


@pytest.fixture(params=["core", "no-core"])
def mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    if request.param == "core":
        if _domain() is None:
            pytest.skip("control-plane is not installed (extra sandbox)")
    else:
        monkeypatch.setattr(apply_module, "_domain", lambda: None)
    return str(request.param)


def _plan(row: dict[str, Any], spec: dict[str, Any] | None = None) -> tuple[list, list[str]]:
    lines: list[str] = []
    applier = Applier(RulesCore(row), {}, dry_run=True, log=lines.append)
    spec = {"workspaceId": WORKSPACE, **copy.deepcopy(SPEC if spec is None else spec)}
    applier._apply_WorkRule(Obj("WorkRule", "resolved", spec, "pkg", Path("rule.yaml")), spec)
    return applier.changes, lines


def test_empty_fields_from_the_core_is_unchanged(mode: str) -> None:
    action = {**SPEC["action"], "fields": {}}
    changes, lines = _plan(_row(action=action))
    assert changes == []
    assert [line.strip() for line in lines] == ["WorkRule/resolved: (plan) v3 unchanged"]


def test_empty_fields_in_the_file_and_none_in_the_core_is_unchanged(mode: str) -> None:
    spec = copy.deepcopy(SPEC)
    spec["action"]["fields"] = {}
    changes, _ = _plan(_row(), spec)
    assert changes == []


def test_empty_interpretation_inputs_and_null_identity_are_unchanged(mode: str) -> None:
    interpretation = {"skill": "oss.check@2", "inputs": {}}
    spec = {**copy.deepcopy(SPEC), "interpretation": {"skill": "oss.check@2"}}
    changes, _ = _plan(_row(interpretation=interpretation), spec)
    assert changes == []


def test_a_real_difference_is_still_seen(mode: str) -> None:
    action = {**SPEC["action"], "fields": {}, "dedupKeyTemplate": "other:{{item.component}}"}
    changes, _ = _plan(_row(action=action, description="Other"))
    assert [(c["operation"], c["fields"]) for c in changes] == [
        ("patch", ["description", "action"])
    ]


def test_another_status_is_still_seen(mode: str) -> None:
    changes, lines = _plan(_row(action={**SPEC["action"], "fields": {}}, status="disabled"))
    assert [c["operation"] for c in changes] == ["enable"]
    assert lines[-1].strip() == "WorkRule/resolved: (plan) → enabled"


def test_a_core_response_the_neighbour_core_rejects_is_compared_as_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Стенд новее ядра рядом (поле, которого ядро рядом не знает): сравнение — без кода ядра,
    но так же без пустых членов, а не ошибка плана."""
    if _domain() is None:
        pytest.skip("control-plane is not installed (extra sandbox)")
    action = {**SPEC["action"], "fields": {}, "futureField": True}
    changes, _ = _plan(_row(action=action))
    assert [c["fields"] for c in changes] == [["action"]]
    changes, _ = _plan(_row(action={**SPEC["action"], "fields": {}}))
    assert changes == []


@pytest.mark.parametrize(
    ("in_file", "in_core"),
    [
        ({"include": []}, {"exclude": []}),
        ({}, {"x": []}),
        ({"ref": "x"}, {"ref": "x", "exclude": []}),
        ({"filters": {}}, {"filters": {"a": []}}),
    ],
)
def test_empty_values_inside_inputs_are_a_real_difference(
    mode: str, in_file: dict[str, Any], in_core: dict[str, Any]
) -> None:
    """inputs схема не описывает: их содержимое — данные автора, пустое в нём значимо."""
    spec = {**copy.deepcopy(SPEC), "interpretation": {"skill": "oss.check@2", "inputs": in_file}}
    interpretation = {"skill": "oss.check@2", "inputs": in_core}
    changes, _ = _plan(_row(interpretation=interpretation), spec)
    assert [(c["operation"], c["fields"]) for c in changes] == [("patch", ["interpretation"])]
    # и в обратную сторону
    spec["interpretation"]["inputs"] = in_core
    changes, _ = _plan(_row(interpretation={**interpretation, "inputs": in_file}), spec)
    assert [(c["operation"], c["fields"]) for c in changes] == [("patch", ["interpretation"])]


def test_empty_values_inside_a_condition_are_a_real_difference(
    mode: str,
) -> None:
    spec = copy.deepcopy(SPEC)
    spec["condition"] = {"in": [{"var": "payload.kind"}, []]}
    changes, _ = _plan(_row(), spec)
    assert [(c["operation"], c["fields"]) for c in changes] == [("patch", ["condition"])]


def test_without_empty_drops_only_members_without_a_default() -> None:
    definitions = load(OBJECT)["$defs"]
    rule = definitions["workRuleSpec"]
    document = {
        "description": "",
        "condition": True,
        "action": {"kind": "ensure_work", "fields": {"relations": {}}, "taskTypes": []},
        "interpretation": {"skill": "s@1", "inputs": {}},
        "identity": None,
    }
    assert without_empty(document, rule, definitions) == {
        "description": "",
        "condition": True,
        "action": {"kind": "ensure_work"},
        "interpretation": {"skill": "s@1"},
        "identity": None,
    }
    # в узлы без properties/items не спускается, необъявленный член остаётся как есть
    document = {
        "condition": {"in": [{"var": "a"}, []]},
        "action": {"kind": "ensure_work", "forEach": [], "acceptance": {"x": {}}},
        "interpretation": {"skill": "s@1", "inputs": {"include": [], "f": {}}},
    }
    assert without_empty(document, rule, definitions) == document
    # пустое значение с default в схеме — значимо: его не выкидывают
    node = {"properties": {"tags": {"type": "array", "default": ["x"]}, "meta": {}}}
    assert without_empty({"tags": [], "meta": {}}, node, definitions) == {"tags": []}
    # без items и properties — как есть; пустой элемент массива не теряется
    assert without_empty([{}, {"a": []}], {}, definitions) == [{}, {"a": []}]
    assert without_empty({"a": []}, {"type": "object"}, definitions) == {"a": []}
    items = {"items": {"properties": {"a": {"type": "array"}}}}
    assert without_empty([{}, {"a": [], "b": []}], items, definitions) == [{}, {"b": []}]
    assert without_empty("x", {"$ref": "#/$defs/missing"}, definitions) == "x"
