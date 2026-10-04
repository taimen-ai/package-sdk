"""Передача работы (амендменты 2026-10-01 CP-ADR-0061, CP-ADR-0073, CP-ADR-0074 и emit.by):
гейт адресован роли пакета ``role:<slug>``, шаг human предзаполняет поля задачи
(``human.customFields``), хост скиллов получает несекретные настройки
(``executor.params.env``), тест называет автора события (``emit.by``).

Фикстура — пакет ``tests/fixtures/handoff/packages/intake-demo``. Схема и статика check идут
всегда; проверку процесса и сценарий исполняет песочница кодом ядра (extra sandbox).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from package_sdk import check, core, model, schema

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "handoff" / "packages" / "intake-demo"
TASK_TYPE = "task-types/request-review.yaml"
RULE = "rules/request-escalated.yaml"
SKILLS_AGENT = "agents/request-skills.yaml"
PROCESS = "processes/request-intake.yaml"
TEST = "tests/request-intake.test.yaml"
GATE_FIELD = "completionSchema.onComplete.actions[0].ensureWork.requestApproval.assignee"


@pytest.fixture
def package(tmp_path: Path) -> Path:
    """Копия фикстуры, которую тест может портить."""
    root = tmp_path / "packages" / "intake-demo"
    shutil.copytree(FIXTURE, root)
    return root


def _load(path: Path) -> Any:
    return model._read_yaml(path)


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def _check(directory: Path) -> tuple[list[str], list[str]]:
    return check.check(model.resolve_targets([str(directory)]))


def _sandbox() -> Any:
    pytest.importorskip("control_plane", reason="сценарии — кодом ядра (extra sandbox)")
    from package_sdk import sandbox

    return sandbox


# --- схема ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [TASK_TYPE, RULE, SKILLS_AGENT, PROCESS])
def test_the_objects_match_the_schema(name: str) -> None:
    assert schema.errors(schema.OBJECT, _load(FIXTURE / name)) == []


def test_the_test_with_an_author_and_prefilled_fields_matches_the_schema() -> None:
    assert schema.errors(schema.TEST, _load(FIXTURE / TEST)) == []


def _with_env(env: Any) -> Any:
    agent = _load(FIXTURE / SKILLS_AGENT)
    agent["spec"]["executor"]["params"] = {"env": env}
    return agent


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"PORTAL_URL": "https://portal.example.com"},
        {"A": ""},
        {"TOKEN_TTL": "60"},  # секрет — по окончанию имени
        {f"SETTING_{i}": str(i) for i in range(50)},
        {"NOTE": "x" * 2000},
    ],
)
def test_params_env_of_a_skills_host_takes_non_secret_settings(env: dict[str, str]) -> None:
    assert schema.errors(schema.OBJECT, _with_env(env)) == []


@pytest.mark.parametrize(
    "env",
    [
        {"PORTAL_TOKEN": "x"},
        {"PORTAL_SECRET": "x"},
        {"DB_PASSWORD": "x"},
        {"PORTAL_API_KEY": "x"},
        {"SIGNING_PRIVATE_KEY": "x"},
        {"CLOUD_CREDENTIALS": "x"},
        {"CONTROL_PLANE_URL": "x"},
        {"IAM_AUDIENCE": "x"},
        {"PYTHONPATH": "x"},
        {"LD_PRELOAD": "x"},
        {"DYLD_INSERT_LIBRARIES": "x"},
        {"GIT_DIR": "x"},
        {"NODE_OPTIONS": "x"},
        {"UV_INDEX_URL": "x"},
        {"PIP_INDEX_URL": "x"},
        {"XDG_CONFIG_HOME": "x"},
        {"PATH": "x"},
        {"HTTP_PROXY": "x"},
        {"HTTPS_PROXY": "x"},
        {"ALL_PROXY": "x"},
        {"NO_PROXY": "x"},
        {"SSL_CERT_FILE": "x"},
        {"SSL_CERT_DIR": "x"},
        {"REQUESTS_CA_BUNDLE": "x"},
        {"CURL_CA_BUNDLE": "x"},
        {"HOME": "x"},
        {"portal_url": "x"},
        {"1PORTAL": "x"},
        {"PORTAL-URL": "x"},
        {"PAGE_LIMIT": 50},
        {"NOTE": "x" * 2001},
        {f"SETTING_{i}": str(i) for i in range(51)},
        "PORTAL_URL=https://portal.example.com",
    ],
)
def test_params_env_refuses_secrets_host_names_and_misfits(env: Any) -> None:
    assert schema.errors(schema.OBJECT, _with_env(env))


def test_a_skills_host_takes_no_other_params() -> None:
    agent = _load(FIXTURE / SKILLS_AGENT)
    agent["spec"]["executor"]["params"]["entrypoint"] = "intake_demo.skills:run"
    assert schema.errors(schema.OBJECT, agent)


@pytest.mark.parametrize(
    "fields,valid",
    [
        ({"request": "data.request"}, True),
        ({"_note": "'fixed'"}, True),
        ({"1request": "data.request"}, False),
        ({"request.id": "data.request"}, False),
        ({"request": ""}, False),
        ({"request": 1}, False),
        ({f"field{i}": "data.request" for i in range(33)}, False),
    ],
)
def test_human_custom_fields_map_a_field_name_to_cel(fields: dict[str, Any], valid: bool) -> None:
    process = _load(FIXTURE / PROCESS)
    process["spec"]["stages"][0]["steps"][0]["human"]["customFields"] = fields
    assert (schema.errors(schema.OBJECT, process) == []) is valid


def _test_doc(step: dict[str, Any]) -> dict[str, Any]:
    return {"process": "request-intake", "name": "t", "steps": [step]}


@pytest.mark.parametrize("by", ["carol", "agent:request-intake-process"])
def test_emit_by_names_the_author_of_the_event(by: str) -> None:
    step = {"emit": {"observation": "request.submitted", "by": by, "payload": {"id": "R-1"}}}
    assert schema.errors(schema.TEST, _test_doc(step)) == []


@pytest.mark.parametrize("by", ["", 7, None])
def test_emit_by_is_a_non_empty_string(by: Any) -> None:
    step = {"emit": {"observation": "request.submitted", "by": by}}
    assert schema.errors(schema.TEST, _test_doc(step))


def test_a_task_type_test_states_the_refusal_of_a_decider() -> None:
    test = {
        "subject": "taskType",
        "taskType": "request-review",
        "name": "only an approver decides",
        "given": {"principals": {"approvers": ["bob"]}},
        "steps": [
            {"approve": {"decision": "approved", "by": "alice", "expectRefused": "not_eligible"}},
            {"approve": {"decision": "approved", "by": "bob"}},
        ],
    }
    assert schema.errors(schema.TEST, test) == []
    test["steps"][0]["approve"]["expectRefused"] = True
    assert schema.errors(schema.TEST, test)


@pytest.mark.parametrize("fields,valid", [({"request": "R-1"}, True), ("R-1", False)])
def test_expect_tasks_compares_the_custom_fields_named(fields: Any, valid: bool) -> None:
    step = {"expect": {"tasks": [{"step": "review-request", "customFields": fields}]}}
    assert (schema.errors(schema.TEST, _test_doc(step)) == []) is valid


# --- check ---------------------------------------------------------------------------


def test_check_accepts_the_package_without_warnings() -> None:
    assert _check(FIXTURE) == ([], [])


@pytest.mark.parametrize(
    "reference",
    [
        "role:Approvers",
        "role:",
        "role:approvers ",
        "role:-approvers",
        "role:a_b",
        "role:approvers\\n",  # `$` у re.match пропустил бы завершающий перевод строки
    ],
)
def test_a_malformed_role_reference_is_unknown_role(package: Path, reference: str) -> None:
    _edit(package / TASK_TYPE, 'assignee: "role:approvers"', f'assignee: "{reference}"')
    errors, _ = _check(package)
    [error] = errors
    found = core.static_error(error)
    assert found["code"] == "unknown_role"
    assert found["file"].endswith(TASK_TYPE)
    written = reference.replace("\\n", "\n")  # escape строки YAML в двойных кавычках
    assert found["message"].startswith(f"{GATE_FIELD} {written!r}")


def test_a_role_outside_the_package_is_a_warning(package: Path) -> None:
    _edit(package / TASK_TYPE, 'assignee: "role:approvers"', 'assignee: "role:auditors"')
    _edit(package / TASK_TYPE, 'approverRole: "role:approvers"', 'approverRole: "role:auditors"')
    _edit(package / RULE, 'approverRole: "role:approvers"', 'approverRole: "role:auditors"')
    errors, warnings = _check(package)
    assert errors == []
    fields = [w.split(": ", 1)[1].split(" ", 1)[0] for w in warnings]
    assert sorted(fields) == sorted(
        [GATE_FIELD, "acceptance[0].spec.approverRole", "action.fields.approverRole"]
    )
    assert all("'auditors'" in w and "unknown_role" in w for w in warnings)


def test_a_role_of_a_required_package_is_known(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    shutil.copytree(FIXTURE, packages / "intake-demo")
    base = packages / "approvals-base"
    shutil.move(packages / "intake-demo" / "roles" / "approvers.yaml", tmp_path / "approvers.yaml")
    (base / "roles").mkdir(parents=True)
    shutil.move(tmp_path / "approvers.yaml", base / "roles" / "approvers.yaml")
    (base / "package.yaml").write_text(
        "apiVersion: taimen.ai/v1\nkind: Package\nkey: approvals-base\n"
        "spec: {version: 0.1.0, displayName: Approvals base}\n",
        encoding="utf-8",
    )
    errors, warnings = _check(packages / "intake-demo")
    assert any("'approvers' is not declared" in w for w in warnings)
    _edit(packages / "intake-demo" / "package.yaml", "requires: []", "requires: [approvals-base]")
    assert _check(packages / "intake-demo") == ([], [])
    assert errors == []


@pytest.mark.parametrize(
    "roles",
    [
        ["approvers", "approvers"],  # повтор
        ["Approvers"],
        ["a"],  # короче slug ядра
        ["a_b"],
        [""],
        [None],
        [1],
        "approvers",
        None,
        ["r" + str(i).zfill(2) for i in range(21)],  # больше 20
    ],
)
def test_the_schema_refuses_malformed_executor_roles(roles: Any) -> None:
    document = _load(FIXTURE / TASK_TYPE)
    document["spec"]["executorRoles"] = roles
    assert schema.errors(schema.OBJECT, document) != []


@pytest.mark.parametrize("roles", [[], ["approvers"], ["r" + str(i).zfill(2) for i in range(20)]])
def test_the_schema_takes_executor_roles(roles: list[str]) -> None:
    document = _load(FIXTURE / TASK_TYPE)
    document["spec"]["executorRoles"] = roles
    assert schema.errors(schema.OBJECT, document) == []


def test_a_task_type_without_executor_roles_passes(package: Path) -> None:
    _edit(package / TASK_TYPE, "  executorRoles: [approvers]\n", "")
    assert _check(package) == ([], [])
    _edit(
        package / TASK_TYPE,
        "completionStatus: done\n",
        "completionStatus: done\n  executorRoles: []\n",
    )
    assert _check(package) == ([], [])


def test_an_executor_role_outside_the_package_is_a_warning(package: Path) -> None:
    _edit(package / TASK_TYPE, "executorRoles: [approvers]", "executorRoles: [approvers, auditors]")
    errors, warnings = _check(package)
    assert errors == []
    [warning] = warnings
    assert TASK_TYPE in warning.split(": ", 1)[0]
    assert "executorRoles[1] 'auditors'" in warning
    assert "unknown_role" in warning


def test_an_executor_role_of_a_required_package_is_known(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    shutil.copytree(FIXTURE, packages / "intake-demo")
    base = packages / "approvals-base"
    (base / "roles").mkdir(parents=True)
    shutil.move(packages / "intake-demo" / "roles" / "approvers.yaml", base / "roles")
    (base / "package.yaml").write_text(
        "apiVersion: taimen.ai/v1\nkind: Package\nkey: approvals-base\n"
        "spec: {version: 0.1.0, displayName: Approvals base}\n",
        encoding="utf-8",
    )
    _, warnings = _check(packages / "intake-demo")
    assert any("executorRoles[0] 'approvers'" in w for w in warnings)
    _edit(packages / "intake-demo" / "package.yaml", "requires: []", "requires: [approvals-base]")
    assert _check(packages / "intake-demo") == ([], [])


@pytest.mark.parametrize(
    "old,new,where",
    [
        # шаблон: роль известна, только когда он отрисуется
        ('assignee: "role:approvers"', 'assignee: "role:$.task.customFields.team"', TASK_TYPE),
        ('approverRole: "role:approvers"', 'approverRole: "role:{{payload.team}}"', RULE),
        # id principal'а и роли — не ссылка на роль пакета
        ('assignee: "role:approvers"', 'assignee: "$.task.assigneeId"', TASK_TYPE),
    ],
)
def test_a_template_or_an_id_is_not_checked_as_a_role(
    package: Path, old: str, new: str, where: str
) -> None:
    _edit(package / where, old, new)
    assert _check(package) == ([], [])


@pytest.mark.parametrize("name", ["GIT_DIR", "HTTPS_PROXY"])
def test_check_refuses_a_host_name_in_params_env(package: Path, name: str) -> None:
    _edit(package / SKILLS_AGENT, "PORTAL_PAGE_LIMIT:", f"{name}:")
    errors, _ = _check(package)
    [error] = errors
    assert error.endswith(
        f"spec/executor/params/env: name {name!r} is not allowed "
        "(looks like a secret or is reserved)"
    )


def test_check_takes_a_setting_in_params_env(package: Path) -> None:
    # PORTAL_URL фикстуры — несекретная настройка, не имя хоста
    assert "PORTAL_URL:" in (package / SKILLS_AGENT).read_text(encoding="utf-8")
    assert _check(package) == ([], [])


def test_an_agent_reference_with_a_trailing_newline_is_refused(package: Path) -> None:
    # `$` у re.match пропустил бы завершающий перевод строки; ядро такую ссылку не примет
    _edit(
        package / RULE,
        'approverRole: "role:approvers"',
        'approverRole: "role:approvers"\n      assignee: "agent:request-intake-process\\n"',
    )
    errors, _ = _check(package)
    assert any(
        "action.fields.assignee 'agent:request-intake-process\\n' — an agent reference is "
        "written agent:<key>" in e
        for e in errors
    ), errors


def test_emit_by_with_a_trailing_newline_is_not_an_agent_reference(package: Path) -> None:
    _edit(package / TEST, "by: carol", 'by: "agent:request-intake-process\\n"')
    _, warnings = _check(package)
    assert "an agent reference is written agent:<key>" in warnings[0]


def test_check_names_a_refused_name_of_params_env(package: Path) -> None:
    _edit(package / SKILLS_AGENT, "PORTAL_PAGE_LIMIT:", "PORTAL_TOKEN:")
    errors, _ = _check(package)
    [error] = errors
    assert error.endswith(
        "spec/executor/params/env: name 'PORTAL_TOKEN' is not allowed "
        "(looks like a secret or is reserved)"
    )


def test_emit_by_an_agent_outside_the_package_is_a_warning(package: Path) -> None:
    _edit(package / TEST, "by: carol", "by: agent:request-intake-process")
    assert _check(package) == ([], [])
    _edit(package / TEST, "by: agent:request-intake-process", "by: agent:ghost")
    errors, warnings = _check(package)
    assert errors == []
    assert [w.split(": ", 1)[1] for w in warnings] == [
        "steps[0].emit.by references agent 'ghost', which is not declared in package "
        "intake-demo and its requires"
    ]
    _edit(package / TEST, "by: agent:ghost", "by: agent:Ghost")
    _, warnings = _check(package)
    assert "an agent reference is written agent:<key>" in warnings[0]


# --- песочница: код ядра --------------------------------------------------------------


def test_the_sandbox_fills_the_task_from_the_case_and_takes_the_author() -> None:
    report = _sandbox().run_package(FIXTURE, env={})
    assert report["status"] == "passed", report
    assert [t["status"] for t in report["tests"]] == ["passed"]


@pytest.mark.parametrize(
    "old,new,code,field",
    [
        ("{request: data.request,", "{requets: data.request,", "unknown_custom_field", "requets"),
        (
            "submittedBy: data.submittedBy}",
            "submittedBy: size(data.request)}",
            "custom_field_type_mismatch",
            "submittedBy",
        ),
    ],
)
def test_prefilled_fields_are_checked_against_the_field_schema(
    package: Path, old: str, new: str, code: str, field: str
) -> None:
    _edit(package / PROCESS, old, new)
    report = _sandbox().run_package(package, env={})
    assert report["status"] != "passed"
    errors = [p for p in report["problems"] if p["severity"] == "error"]
    assert [(p["code"], p["file"], p["path"]) for p in errors] == [
        (code, PROCESS, f"/spec/stages/0/steps/0/human/customFields/{field}")
    ]


def test_the_sandbox_fails_a_wrong_prefilled_field(package: Path) -> None:
    _edit(package / TEST, "customFields: {request: R-1,", "customFields: {request: R-2,")
    report = _sandbox().run_package(package, env={})
    assert report["status"] == "failed"
    [failure] = report["tests"][0]["failures"]
    assert failure["actual"][0]["customFields"] == {"request": "R-1", "submittedBy": "carol"}


def test_the_sandbox_fails_another_author(package: Path) -> None:
    _edit(package / TEST, "by: carol", "by: dave")
    report = _sandbox().run_package(package, env={})
    assert report["status"] == "failed"
