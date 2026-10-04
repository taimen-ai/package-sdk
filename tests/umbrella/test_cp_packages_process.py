"""package-sdk для процессов (TAI-ADR-0054, plan Р8–Р9): виды Process и Calendar, тесты
пакета, проверка ядром, test, plan --out и apply --plan — на поддельном ядре.

Формы запросов и ответов — контракт CP-ADR-0074 §10–11 (схемы Package*Request/Out
control-plane); если control-plane их знает, запросы сверяются его моделями."""

from __future__ import annotations

import copy
import json
import shutil
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.umbrella._shim import FIXTURES as COMPONENT_FIXTURES
from tests.umbrella._shim import REQUIRE_CORE_CONTRACT, core_contract_unavailable, cp, settled
from tests.umbrella._shim import UMBRELLA as ROOT
from tests.umbrella.test_cp_packages import FakeControlPlane, FakeNotificationService

FIXTURES = COMPONENT_FIXTURES / "process"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "packages"
    shutil.copytree(ROOT / "packages", root)
    monkeypatch.setattr(cp, "PACKAGES_DIR", root)
    return root


def _task_type(key: str) -> str:
    lifecycle = cp._read_yaml(ROOT / "packages" / "selfdev" / "task-types" / "devops.yaml")["spec"][
        "lifecycleSchema"
    ]
    return json.dumps(
        {
            "apiVersion": cp.API_VERSION,
            "kind": "TaskType",
            "key": key,
            "spec": {"displayName": key, "lifecycleSchema": lifecycle},
        },
        ensure_ascii=False,
    )


@pytest.fixture
def procdemo(sandbox):
    """Пакет с процессом-примером схемы, календарём, типами задач, личностью и тестом."""
    root = sandbox / "procdemo"
    for folder in ("processes", "calendars", "task-types", "agents", "tests", ".layout"):
        (root / folder).mkdir(parents=True)
    (root / "package.yaml").write_text(
        "apiVersion: taimen.ai/v1\nkind: Package\nkey: procdemo\nspec:\n  version: 0.1.0\n"
        "  displayName: Process demo\n  requires: [notify]\n",
        encoding="utf-8",
    )
    shutil.copy(FIXTURES / "purchase.process.yaml", root / "processes" / "purchase-example.yaml")
    shutil.copy(FIXTURES / "ru.calendar.yaml", root / "calendars" / "ru.yaml")
    shutil.copy(FIXTURES / "purchase.test.yaml", root / "tests" / "purchase.test.yaml")
    for key in ("go-no-go", "lessons-review"):
        (root / "task-types" / f"{key}.yaml").write_text(_task_type(key), encoding="utf-8")
    (root / "agents" / "example-process.yaml").write_text(
        json.dumps(
            {
                "apiVersion": cp.API_VERSION,
                "kind": "Agent",
                "key": "example-process",
                "spec": {
                    "displayName": "Process",
                    "identity": {"kind": "service", "permissions": ["tasks.write"]},
                    "placement": "none",
                },
            }
        ),
        encoding="utf-8",
    )
    (root / ".layout" / "purchase-example.json").write_text('{"nodes": {}}\n', encoding="utf-8")
    return root


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


# --- загрузка и статическая проверка -----------------------------------------


def test_calendar_goes_before_process_and_process_after_task_types_and_agents():
    kinds = cp.CATALOG_KINDS
    assert kinds.index("Calendar") < kinds.index("Process")
    assert kinds.index("Process") > max(
        kinds.index("TaskType"), kinds.index("Agent"), kinds.index("Skill")
    )
    assert {"Process", "Calendar"} <= set(cp.RETIRABLE)
    assert (cp.FOLDERS["Process"], cp.FOLDERS["Calendar"]) == ("processes", "calendars")


def test_process_package_loads_objects_and_tests_and_passes_check(procdemo):
    installation = cp.resolve(["procdemo"])
    kinds = sorted(o.kind for o in installation.packages[-1].objects)
    assert kinds == ["Agent", "Calendar", "Process", "TaskType", "TaskType"]
    assert [t.name for t in installation.tests] == ["отказ от участия после прошлого проигрыша"]
    assert settled(cp.check(installation)) == ([], [])


def test_process_without_owner_is_a_warning_and_owner_agent_a_reference(procdemo):
    process = procdemo / "processes" / "purchase-example.yaml"
    _edit(process, "  owner: [{role: purchase-lead}]\n", "")
    errors, warnings = cp.check(cp.resolve(["procdemo"]))
    assert errors == [] and any("owner is not set" in w for w in warnings)
    process.write_text(
        process.read_text(encoding="utf-8").replace(
            "  identity: {agent: example-process}\n",
            "  identity: {agent: example-process}\n  owner: [{agent: nobody}]\n",
        ),
        encoding="utf-8",
    )
    errors, _ = cp.check(cp.resolve(["procdemo"]))
    assert any("owner.agent" in e and "nobody" in e for e in errors)


def test_data_ref_is_expanded_from_a_package_file(procdemo):
    (procdemo / "schemas").mkdir()
    process = procdemo / "processes" / "purchase-example.yaml"
    data = cp._read_yaml(process)["spec"]["data"]
    (procdemo / "schemas" / "purchase.schema.json").write_text(json.dumps(data), encoding="utf-8")
    text = process.read_text(encoding="utf-8")
    start, end = text.index("  data:\n"), text.index("  start:\n")
    process.write_text(
        text[:start] + "  data: {$ref: ../schemas/purchase.schema.json}\n" + text[end:],
        encoding="utf-8",
    )
    obj = next(o for o in cp.resolve(["procdemo"]).objects if o.kind == "Process")
    assert obj.spec["data"] == data
    assert cp.check(cp.resolve(["procdemo"]))[0] == []
    _edit(process, "../schemas/purchase.schema.json", "../../selfdev/package.yaml")
    with pytest.raises(cp.PackageError, match="points outside the package"):
        cp.resolve(["procdemo"])


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("taskType: go-no-go", "taskType: go-no-goo", "human.taskType 'go-no-goo'"),
        ('skill: "notify.send@1"', 'skill: "notify.send@9"', "call.skill 'notify.send@9'"),
        (
            "decide: {table: approval-level}",
            "decide: {table: approval-levels}",
            "decide.table 'approval-levels'",
        ),
        ("- id: decline", "- id: level", "id 'level' is already taken"),
        (
            "identity: {agent: example-process}",
            "identity: {agent: nobody}",
            "references agent 'nobody'",
        ),
    ],
)
def test_check_catches_what_is_visible_without_the_core(procdemo, old, new, expected):
    _edit(procdemo / "processes" / "purchase-example.yaml", old, new)
    errors, _ = cp.check(cp.resolve(["procdemo"]))
    assert any(expected in e for e in errors), errors


def test_check_validates_package_tests(procdemo):
    test = procdemo / "tests" / "purchase.test.yaml"
    _edit(test, "complete: {step: decide-participation,", "complete: {step: decide,")
    errors, _ = cp.check(cp.resolve(["procdemo"]))
    assert any("complete.step 'decide'" in e for e in errors)
    _edit(test, "  - advance: P10D\n", "  - advance: P10D\n    extra: 1\n")
    errors, _ = cp.check(cp.resolve(["procdemo"]))
    assert any("purchase.test.yaml" in e and "extra" in e for e in errors)


def test_renames_must_name_an_object_of_the_package(procdemo):
    _edit(
        procdemo / "package.yaml",
        "  requires: [notify]\n",
        "  requires: [notify]\n  renames:\n    - {kind: Process, from: purchase, to: purchase-example}\n"
        "    - {kind: Process, from: old, to: missing}\n",
    )
    errors, _ = cp.check(cp.resolve(["procdemo"]))
    renames = [e for e in errors if "renames" in e]
    assert len(renames) == 1 and renames[0].endswith(
        "renames: Process/missing — no such object in the package"
    )


# --- поддельное ядро ------------------------------------------------------------


CONTRACT_NAMES = ("PackageTestRequest", "PackagePlanRequest", "PackageApplyRequest")


def _core_schemas() -> tuple[Any, str]:
    """Модуль control_plane.api.v1.schemas и причина, если он не импортируется."""
    try:
        from control_plane.api.v1 import schemas
    except ImportError as error:
        return None, (
            f"control_plane.api.v1.schemas не импортируется ({error}); "
            "нужен сосед control-plane (extra sandbox)"
        )
    return schemas, ""


def _contract_models() -> tuple[dict[str, Any] | None, str]:
    """Модели запросов ядра (CP-ADR-0074 §10–11) и причина, если их взять не удалось."""
    schemas, reason = _core_schemas()
    if schemas is None:
        return None, reason
    missing = [name for name in CONTRACT_NAMES if not hasattr(schemas, name)]
    if missing:
        return None, f"в control_plane.api.v1.schemas нет {', '.join(missing)}"
    return {name: getattr(schemas, name) for name in CONTRACT_NAMES}, ""


MODELS, MODELS_UNAVAILABLE = _contract_models()


def test_fake_core_requests_are_checked_against_core_models():
    """Сверка запросов FakeCore с моделями ядра не выключается молча: без моделей — явный
    пропуск с причиной, а под PACKAGE_SDK_REQUIRE_CORE_CONTRACT=1 (make test) — провал
    (TASK-001186)."""
    if MODELS is None:
        core_contract_unavailable(MODELS_UNAVAILABLE)
    core = FakeCore()
    body = {"package": {"files": [{"path": "package.yaml", "content": "x"}]}}
    core._validate("/api/v1/packages:plan", body)  # правильное тело модель пропускает
    # форму ключей FakeCore проверяет сам, а значение поля — только модель ядра
    with pytest.raises(Exception, match="validation error"):
        core._validate("/api/v1/packages:plan", {**body, "workspaceId": "не-uuid"})


PLAN_HASH_PREFIX = "sha256:"


class FakeCore:
    """Маршруты /packages:test, :plan и :apply по контракту CP-ADR-0074 §10–11: тело — один
    пакет файлами; ответы — PackageTestOut, PackagePlanOut, PackageApplyOut."""

    def __init__(self) -> None:
        self.status: int | None = None  # 404 или 501 — маршрута нет или он ещё не реализован
        self.problems: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.etag = "sha256:" + "e" * 64

    def plan_hash(self, package: dict) -> str:
        return cp.request_hash({"package": package, "etag": self.etag})

    def _validate(self, path: str, body: dict) -> None:
        route = path.split("?")[0].rsplit(":", 1)[1]
        keys = {
            "test": {"package", "tests", "workspaceId"},
            "plan": {"package", "workspaceId", "replayLimit"},
            "apply": {"package", "planHash", "workspaceId"},
        }[route]
        assert set(body) <= keys and set(body["package"]) == {"files"}, body.keys()
        assert all(
            set(f) == {"path", "content"} and not f["path"].startswith((".", "/"))
            for f in body["package"]["files"]
        )
        if MODELS is not None:  # control-plane с контрактом — сверка моделью ядра
            MODELS[f"Package{route.capitalize()}Request"].model_validate(body)

    def call(self, method, path, body=None, headers=None):
        assert method == "POST" and headers["Authorization"] == "Bearer t"
        self.calls.append((path, copy.deepcopy(body)))
        self._validate(path, body)
        if self.status == 404:
            raise cp.HttpError(
                f"POST {path}: HTTP 404", 404, {"error": {"code": "not_found", "message": "-"}}
            )
        if self.status == 501:
            raise cp.HttpError(
                f"POST {path}: HTTP 501",
                501,
                {
                    "error": {
                        "code": "not_implemented",
                        "message": "not implemented yet",
                        "details": {"adr": "CP-ADR-0074", "implementedBy": "process-packages P013"},
                    }
                },
            )
        files = [f["path"] for f in body["package"]["files"]]
        if path.startswith("/api/v1/packages:test"):
            check_only = path.endswith("?checkOnly=true")
            if check_only or "tests/purchase.test.yaml" not in files:
                return {
                    "status": "invalid" if self.problems else "passed",
                    "checkOnly": check_only,
                    "problems": self.problems,
                    "tests": [],
                    "coverage": [],
                    "durationMs": 3,
                }
            return {
                "status": "failed",
                "checkOnly": False,
                "problems": [],
                "durationMs": 15,
                "tests": [
                    {
                        "file": "tests/purchase.test.yaml",
                        "name": "отказ от участия после прошлого проигрыша",
                        "process": "purchase-example",
                        "status": "failed",
                        "durationMs": 12,
                        "failures": [
                            {
                                "step": 3,
                                "message": "эскалации не было",
                                "expected": ["process.escalated"],
                                "actual": [],
                            }
                        ],
                    }
                ],
                "coverage": [
                    {
                        "process": "purchase-example",
                        "version": 1,
                        "elements": {"covered": 7, "total": 20, "missing": ["price", "results"]},
                        "transitions": {"covered": 0, "total": 0, "missing": []},
                        "decisionRows": {"covered": 0, "total": 2, "missing": ["approval-level#1"]},
                        "handlers": {"covered": 0, "total": 0, "missing": []},
                    }
                ],
            }
        key = "procdemo" if "processes/purchase-example.yaml" in files else "notify"
        if path == "/api/v1/packages:plan":
            processes = (
                []
                if key == "notify"
                else [
                    {
                        "key": "purchase-example",
                        "fromVersion": 1,
                        "toVersion": 2,
                        "behaviour": {
                            "replayed": 5,
                            "diverged": 1,
                            "instanceIds": ["0b7c9c5e-0000-4000-8000-000000000001"],
                        },
                        "instances": [
                            {"version": 1, "open": 3, "fate": "migrate", "migrationRequired": False}
                        ],
                    }
                ]
            )
            changes = (
                [
                    {
                        "kind": "Skill",
                        "key": "notify.send",
                        "action": "unchanged",
                        "renamedFrom": None,
                        "fields": [],
                    }
                ]
                if key == "notify"
                else [
                    {
                        "kind": "Calendar",
                        "key": "ru",
                        "action": "create",
                        "renamedFrom": None,
                        "fields": [],
                    },
                    {
                        "kind": "Process",
                        "key": "purchase-example",
                        "action": "rename",
                        "renamedFrom": "purchase",
                        "fields": [],
                    },
                    {
                        "kind": "TaskType",
                        "key": "go-no-go",
                        "action": "update",
                        "renamedFrom": None,
                        "fields": [
                            {
                                "path": "/description",
                                "before": "a",
                                "after": "b",
                                "owner": "console",
                                "applies": False,
                            }
                        ],
                    },
                ]
            )
            return {
                "planHash": self.plan_hash(body["package"]),
                "catalogEtag": self.etag,
                "package": {"key": key, "version": "0.1.0"},
                "changes": changes,
                "processes": processes,
                "regulationCoverage": [],
                "problems": self.problems,
                "createdAt": "2026-09-28T10:00:00Z",
            }
        if path == "/api/v1/packages:apply":
            if body["planHash"] != self.plan_hash(body["package"]):
                raise cp.HttpError(
                    "POST: HTTP 409", 409, {"error": {"code": "plan_stale", "message": "stale"}}
                )
            return {
                "planHash": body["planHash"],
                "catalogEtag": self.etag,
                "applied": [
                    {"kind": "Process", "key": "purchase-example", "action": "create", "version": 1}
                ]
                if key == "procdemo"
                else [],
            }
        raise AssertionError(path)


@pytest.fixture
def core(monkeypatch):
    fake = FakeCore()
    monkeypatch.setattr(cp, "Http", lambda _base: fake)
    monkeypatch.setattr(cp, "_bearer", lambda _server: "t")
    return fake


SERVER = "https://platform.example.com"
# переменные установки пакета notify (requires процесса-примера)
NOTIFY_ENV = {
    "TASK_URL_BASE": "https://platform.example.com/tasks",
    "NOTIFICATION_SERVICE_URL": "https://platform.example.com/notify",
}


def _set_env(monkeypatch) -> None:
    for name, value in NOTIFY_ENV.items():
        monkeypatch.setenv(name, value)


# --- check --server --------------------------------------------------------------


def test_check_asks_the_core_and_prints_its_problems_in_place(procdemo, core, capsys):
    core.problems = [
        {
            "code": "unknown_data_field",
            "severity": "error",
            "path": "/spec/stages/0/exit",
            "line": 83,
            "file": "processes/purchase-example.yaml",
            "message": "нет поля data.decison",
            "hint": "может быть, data.decision?",
        },
        {
            "code": "unused_field",
            "severity": "warning",
            "path": "/spec/data",
            "line": None,
            "file": "processes/purchase-example.yaml",
            "message": "поле history не читается",
            "hint": None,
        },
    ]
    assert cp.main(["check", "--package", "procdemo", "--server", SERVER]) == 1
    out = capsys.readouterr().out
    assert (
        "error: processes/purchase-example.yaml:83: unknown_data_field: нет поля data.decison "
        "[/spec/stages/0/exit] (hint: может быть, data.decision?)"
    ) in out
    assert "warning: processes/purchase-example.yaml: unused_field: поле history не читается" in out
    path, body = core.calls[0]
    assert (
        path == "/api/v1/packages:test?checkOnly=true" and len(core.calls) == 1
    )  # только названный пакет
    files = [f["path"] for f in body["package"]["files"]]
    assert {
        "package.yaml",
        "processes/purchase-example.yaml",
        "calendars/ru.yaml",
        "tests/purchase.test.yaml",
    } <= set(files)
    assert not any(name.startswith(".layout") for name in files)
    assert cp.main(["check", "--package", "procdemo", "--server", SERVER, "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["core"] == "checked" and [p["code"] for p in report["errors"]] == [
        "unknown_data_field"
    ]


@pytest.mark.parametrize(
    ("status", "said"),
    [
        (404, "does not know /packages:test"),
        (501, "does not implement /packages:test yet (process-packages P013)"),
    ],
)
def test_check_against_a_core_without_processes_falls_back_to_the_schema(
    procdemo, core, capsys, status, said
):
    core.status = status
    assert cp.main(["check", "--package", "procdemo", "--server", SERVER]) == 0
    out = capsys.readouterr().out
    assert (
        "the core does not support checking processes" in out
        and said in out
        and "only the schema was checked" in out
    )


def test_check_with_unreachable_core_is_not_a_failure(procdemo, monkeypatch, capsys):
    class Down:
        def call(self, *_a, **_k):
            raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(cp, "Http", lambda _base: Down())
    monkeypatch.setattr(cp, "_bearer", lambda _server: "t")
    assert cp.main(["check", "--package", "procdemo", "--server", SERVER]) == 0
    assert "the core is unreachable" in capsys.readouterr().out


# --- test --------------------------------------------------------------------------


def test_test_command_runs_the_package_tests_and_prints_coverage(procdemo, core, capsys):
    assert (
        cp.main(
            [
                "test",
                "--package",
                str(procdemo),
                "--server",
                SERVER,
                "--workspace",
                "0b7c9c5e-0000-4000-8000-00000000000a",
            ]
        )
        == 1
    )
    out = capsys.readouterr().out
    assert (
        "FAIL tests/purchase.test.yaml: отказ от участия после прошлого проигрыша [purchase-example] (12 ms)"
        in out
    )
    assert "step 3: эскалации не было" in out
    assert "coverage purchase-example v1: elements 7/20, decisionRows 0/2" in out
    assert "not covered (decisionRows): approval-level#1" in out
    assert "failed (failed): tests 1, passed 0" in out
    path, body = core.calls[0]
    assert (
        path == "/api/v1/packages:test" and len(core.calls) == 1
    )  # notify из requires не отправляется
    assert body["tests"] == ["tests/purchase.test.yaml"]
    assert body["workspaceId"] == "0b7c9c5e-0000-4000-8000-00000000000a"


def test_test_command_without_tests_or_with_a_static_error_does_not_call_the_core(
    procdemo, core, capsys
):
    assert (
        cp.main(["test", "--package", "procdemo", "--test", "нет такого", "--server", SERVER]) == 1
    )
    assert "no tests" in capsys.readouterr().out
    _edit(
        procdemo / "processes" / "purchase-example.yaml", "taskType: go-no-go", "taskType: unknown"
    )
    assert cp.main(["test", "--package", "procdemo", "--server", SERVER]) == 1
    assert "tests were not run" in capsys.readouterr().out
    assert core.calls == []


# --- единый план: plan --out и apply --plan (S013) ------------------------------------------


class StandFake(FakeControlPlane):
    """Стенд единого плана: каталог установщика (FakeControlPlane), /packages:* ядра (FakeCore),
    сервис уведомлений и :retire процессов с dryRun."""

    def __init__(self, core: FakeCore) -> None:
        super().__init__()
        self.core = core
        self.notify = FakeNotificationService()
        self.processes: dict[str, dict[str, Any]] = {}

    def call(self, method, path, body=None, headers=None):
        if path.startswith(("/api/v1/packages:plan", "/api/v1/packages:apply")):
            return self.core.call(method, path, body, headers)
        if path.startswith("/api/v1/notification-rules"):
            return self.notify.call(method, path, body, headers)
        if path.startswith("/api/v1/process-definitions/"):
            key, _, verb = path.split("/")[4].partition(":")
            row = self.processes.get(key)
            if row is None:
                raise cp.HttpError(f"{method} {path}: HTTP 404", 404, {"error": {"code": "x"}})
            if method == "GET":
                return {"key": key, "status": row["status"]}
            if "dryRun=true" not in verb:
                row["status"] = "retired"
            return {
                "key": key,
                "status": "retired",
                "retired": {"at": "2026-09-30T10:00:00Z", "by": "x", "reason": body["reason"]},
                "openInstances": row["open"],
                "byVersion": [{"version": 1, "openInstances": row["open"]}],
            }
        return super().call(method, path, body, headers)


@pytest.fixture
def stand(core, monkeypatch):
    fake = StandFake(core)
    monkeypatch.setattr(cp, "Http", lambda _base: fake)
    monkeypatch.setenv("NOTIFY_TOKEN", "n")
    return fake


@pytest.fixture
def install(procdemo, tmp_path):
    path = tmp_path / "install.yaml"
    path.write_text(
        "apiVersion: taimen.ai/v1\nkind: Installation\nkey: demo\nspec:\n  packages: [procdemo]\n",
        encoding="utf-8",
    )
    _edit(
        procdemo / "package.yaml",
        "  requires: [notify]\n",
        "  requires: [notify]\n  renames:\n    - {kind: Process, from: purchase, to: purchase-example}\n",
    )
    return path


def _plan_args(install, tmp_path, *extra) -> list[str]:
    return [
        "plan",
        "--install",
        str(install),
        "--server",
        SERVER,
        "--out",
        str(tmp_path / "plan.json"),
        "--env",
        str(tmp_path / "none.env"),
        *extra,
    ]


def test_plan_prints_the_diff_and_saves_the_plan_with_its_hash(
    install, core, stand, tmp_path, capsys, monkeypatch
):
    _set_env(monkeypatch)
    assert cp.main(_plan_args(install, tmp_path, "--replay-limit", "20")) == 0
    out = capsys.readouterr().out
    assert "+ Calendar/ru" in out and "→ Process/purchase → Process/purchase-example" in out
    assert "~ TaskType/go-no-go: /description (edited in the console, not overwritten)" in out
    assert "process purchase-example: v1 → v2" in out
    assert "behaviour (replay): instances 5, diverged 1" in out
    assert "open instances v1: 3 → migrate" in out
    # план ядра — только у пакета с процессами; renames — в package.yaml среди файлов
    assert [path for path, _b in core.calls] == ["/api/v1/packages:plan"]
    request = core.calls[0][1]
    assert request["replayLimit"] == 20
    manifest = next(
        f["content"] for f in request["package"]["files"] if f["path"] == "package.yaml"
    )
    assert "{kind: Process, from: purchase, to: purchase-example}" in manifest
    saved = json.loads((tmp_path / "plan.json").read_text())
    assert [s["kind"] for s in saved["sections"]] == [
        "catalog",
        "core",
        "knowledge",
        "notification-rules",
        "retire",
    ]
    core_section = saved["sections"][1]
    assert core_section["package"] == "procdemo" and core_section["replayLimit"] == 20
    assert core_section["planHash"] == core.plan_hash(request["package"])
    assert saved["server"] == SERVER
    # скилл notify — установщику, правила уведомлений — сервису; ${TASK_URL_BASE}
    # подставлен в то, что сервис проверяет
    assert [(c["kind"], c["key"]) for c in saved["sections"][0]["changes"]] == [
        ("Skill", "notify.send")
    ]
    assert len(saved["sections"][3]["changes"]) == 3
    assert stand.notify.writes == [] and stand.writes == []


def test_plan_with_migration_required_is_not_saved(
    install, core, stand, tmp_path, capsys, monkeypatch
):
    _set_env(monkeypatch)
    core.problems = [
        {
            "code": "migration_required",
            "severity": "error",
            "path": "/spec/stages/1",
            "line": 80,
            "file": "processes/purchase-example.yaml",
            "message": "на price стоят 3 экземпляра",
            "hint": "добавьте migrations",
        }
    ]
    assert cp.main(_plan_args(install, tmp_path)) == 1
    assert "migration_required" in capsys.readouterr().out
    assert not (tmp_path / "plan.json").exists()


# снимок PackagePlanOut ядра с processes[].deadlines (P017, FR-023; TASK-001162)
_PLAN_WITH_DEADLINES: dict[str, Any] = {
    "planHash": "sha256:" + "a" * 64,
    "catalogEtag": "sha256:" + "e" * 64,
    "package": {"key": "procdemo", "version": "0.2.0"},
    "changes": [],
    "processes": [
        {
            "key": "purchase-example",
            "fromVersion": 1,
            "toVersion": 2,
            "behaviour": None,
            "instances": [{"version": 1, "open": 3, "fate": "migrate", "migrationRequired": False}],
            "deadlines": [
                {
                    "instanceId": "0b7c9c5e-0000-4000-8000-000000000001",
                    "element": "price",
                    "previousDueAt": "2026-10-01T10:00:00Z",
                    "dueAt": "2026-10-03T10:00:00Z",
                    "breached": False,
                },
                {
                    "instanceId": "0b7c9c5e-0000-4000-8000-000000000002",
                    "element": None,
                    "previousDueAt": None,
                    "dueAt": "2026-09-29T10:00:00Z",
                    "breached": True,
                },
                {
                    "instanceId": "0b7c9c5e-0000-4000-8000-000000000003",
                    "element": "approval",
                    "previousDueAt": "2026-10-05T10:00:00Z",
                    "dueAt": None,
                    "breached": False,
                },
            ],
        }
    ],
    "regulationCoverage": [],
    "problems": [],
    "createdAt": "2026-09-30T10:00:00Z",
}


def _cut_plan() -> dict[str, Any]:
    """Снимок усечённого раздела: ядро отдаёт не больше 200 записей и всех — в deadlinesTotal
    (TASK-001161, TASK-001183)."""
    response = copy.deepcopy(_PLAN_WITH_DEADLINES)
    response["processes"][0]["deadlinesTotal"] = 250
    return response


def _printed_plan(response: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    assert cp.print_plan(response, log=lines.append)
    return lines


@pytest.mark.parametrize("snapshot", [_PLAN_WITH_DEADLINES, _cut_plan()], ids=["full", "cut"])
def test_plan_snapshots_follow_the_core_contract(snapshot):
    """Снимки ответа ядра ниже — по модели PackagePlanOut; без модели — явный пропуск, под
    PACKAGE_SDK_REQUIRE_CORE_CONTRACT=1 — провал, а не молчаливая сверка (TASK-001186)."""
    schemas, reason = _core_schemas()
    if schemas is None:
        core_contract_unavailable(reason)
    process_out = getattr(schemas, "PlanProcessOut", None)
    if process_out is None or "deadlines_total" not in process_out.model_fields:
        core_contract_unavailable("PlanProcessOut ядра без deadlines/deadlinesTotal (TASK-001161)")
    schemas.PackagePlanOut.model_validate(snapshot)


def test_plan_prints_the_deadlines_the_migration_recomputes():
    lines = _printed_plan(_PLAN_WITH_DEADLINES)
    start = lines.index("  deadlines: instances 3, already breached 1")
    assert lines[start - 1] == "  open instances v1: 3 → migrate"
    assert lines[start + 1 : start + 4] == [
        "    0b7c9c5e-0000-4000-8000-000000000001, step price: "
        "2026-10-01T10:00:00Z → 2026-10-03T10:00:00Z",
        "    0b7c9c5e-0000-4000-8000-000000000002, whole case: "
        "none → 2026-09-29T10:00:00Z — already breached",
        "    0b7c9c5e-0000-4000-8000-000000000003, step approval: 2026-10-05T10:00:00Z → removed",
    ]


def test_plan_with_a_cut_deadline_list_prints_the_total_from_the_core():
    response = _cut_plan()
    lines = _printed_plan(response)
    start = lines.index(
        "  deadlines: instances 250, shown 3 of 250, already breached 1 among shown"
    )
    assert [line[:17] for line in lines[start + 1 : start + 4]] == ["    0b7c9c5e-0000"] * 3

    response["processes"][0]["deadlinesTotal"] = 3  # не усечён — заголовок как без поля
    assert "  deadlines: instances 3, already breached 1" in _printed_plan(response)


@pytest.mark.parametrize("deadlines", [[], None, "absent"])
def test_plan_without_deadlines_prints_no_deadline_section(deadlines):
    response = copy.deepcopy(_PLAN_WITH_DEADLINES)
    process = response["processes"][0]
    if deadlines == "absent":
        del process["deadlines"]
    else:
        process["deadlines"] = deadlines
    lines = _printed_plan(response)
    assert not any("deadline" in line for line in lines)
    assert lines[-1] == "  open instances v1: 3 → migrate"


def _core_with_deadlines(core, monkeypatch) -> list[dict[str, Any]]:
    deadlines = _PLAN_WITH_DEADLINES["processes"][0]["deadlines"]
    plan = core.call

    def call_with_deadlines(method, path, body=None, headers=None):
        response = plan(method, path, body, headers)
        for process in response.get("processes") or []:
            process["deadlines"] = copy.deepcopy(deadlines)
        return response

    monkeypatch.setattr(core, "call", call_with_deadlines)
    return deadlines


def test_plan_json_and_plan_file_keep_deadlines_as_is(
    install, core, stand, tmp_path, capsys, monkeypatch
):
    _set_env(monkeypatch)
    deadlines = _core_with_deadlines(core, monkeypatch)
    assert cp.main(_plan_args(install, tmp_path, "--json")) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["sections"][1]["plan"]["processes"][0]["deadlines"] == deadlines
    saved = json.loads((tmp_path / "plan.json").read_text())
    assert saved["sections"][1]["plan"]["processes"][0]["deadlines"] == deadlines


def test_plan_prints_deadlines_under_the_process(
    install, core, stand, tmp_path, capsys, monkeypatch
):
    _set_env(monkeypatch)
    _core_with_deadlines(core, monkeypatch)
    assert cp.main(_plan_args(install, tmp_path)) == 0
    out = capsys.readouterr().out.splitlines()
    start = out.index("    deadlines: instances 3, already breached 1")
    assert out[start - 1] == "    open instances v1: 3 → migrate"
    assert out[start + 2].endswith("whole case: none → 2026-09-29T10:00:00Z — already breached")


def test_install_retire_of_a_process_is_part_of_the_plan(
    install, core, stand, tmp_path, capsys, monkeypatch
):
    _set_env(monkeypatch)
    stand.processes["old-process"] = {"status": "active", "open": 1}
    install.write_text(
        install.read_text() + "  retire:\n    Process: [old-process]\n", encoding="utf-8"
    )
    assert cp.main(_plan_args(install, tmp_path)) == 0
    assert "- Process/old-process: new instances do not start, live ones run to completion: 1" in (
        capsys.readouterr().out
    )
    saved = json.loads((tmp_path / "plan.json").read_text())
    (item,) = saved["sections"][-1]["items"]
    assert (item["kind"], item["key"], item["openInstances"]) == ("Process", "old-process", 1)
    assert stand.processes["old-process"]["status"] == "active"  # план — dryRun


def test_plan_needs_every_install_variable(install, core, stand, tmp_path, monkeypatch, capsys):
    _set_env(monkeypatch)
    monkeypatch.delenv("TASK_URL_BASE", raising=False)
    assert cp.main(_plan_args(install, tmp_path)) == 1
    assert "TASK_URL_BASE" in capsys.readouterr().err
    assert core.calls == []


def _plan(install, tmp_path, monkeypatch) -> Path:
    _set_env(monkeypatch)
    assert cp.main(_plan_args(install, tmp_path)) == 0
    return tmp_path / "plan.json"


def _apply_args(plan_file: Path, tmp_path: Path, *extra: str) -> list[str]:
    return [
        "apply",
        "--plan",
        str(plan_file),
        "--server",
        SERVER,
        "--env",
        str(tmp_path / "none.env"),
        *extra,
    ]


def test_apply_plan_applies_exactly_the_saved_plan(
    install, core, stand, tmp_path, monkeypatch, capsys
):
    plan_file = _plan(install, tmp_path, monkeypatch)
    capsys.readouterr()
    monkeypatch.setattr(cp, "_ask", lambda _question: True)
    assert cp.main(_apply_args(plan_file, tmp_path)) == 0
    assert "Process/purchase-example: create v1" in capsys.readouterr().out
    saved = json.loads(plan_file.read_text())
    planned = [b for p, b in core.calls if p == "/api/v1/packages:plan"]
    applied = [b for p, b in core.calls if p == "/api/v1/packages:apply"]
    # план ядра перед записью строится заново из тех же файлов, применяется его хэш
    assert len(planned) == 2 and planned[0] == planned[1]
    assert applied == [
        {"package": planned[0]["package"], "planHash": saved["sections"][1]["planHash"]}
    ]
    assert ("POST", "/skills") in stand.writes
    assert len(stand.notify.writes) == 3


def test_apply_of_a_stale_plan_is_refused_clearly(
    install, core, stand, tmp_path, monkeypatch, capsys
):
    plan_file = _plan(install, tmp_path, monkeypatch)
    core.etag = "sha256:" + "f" * 64  # каталог стенда изменился после построения плана
    monkeypatch.setattr(cp, "_ask", lambda _question: True)
    assert cp.main(_apply_args(plan_file, tmp_path)) == 1
    assert "the plan is stale" in capsys.readouterr().err
    assert stand.writes == [] and stand.notify.writes == []
    assert [p for p, _b in core.calls].count("/api/v1/packages:apply") == 0


def test_apply_refuses_an_edited_plan_file_and_another_server(
    install, core, stand, tmp_path, monkeypatch, capsys
):
    plan_file = _plan(install, tmp_path, monkeypatch)
    document = json.loads(plan_file.read_text())
    assert cp.main(_apply_args(plan_file, tmp_path, "--server", "https://other.example.com")) == 1
    assert "the plan was built for" in capsys.readouterr().err
    document["sections"][1]["plan"]["changes"] = []
    plan_file.write_text(json.dumps(document))
    assert cp.main(_apply_args(plan_file, tmp_path)) == 1
    assert "planHash does not match" in capsys.readouterr().err
    assert [p for p, _b in core.calls].count("/api/v1/packages:apply") == 0


@pytest.mark.parametrize(
    ("status", "said"),
    [
        (404, "the core does not know /packages:plan"),
        (501, "the core does not implement /packages:plan yet"),
    ],
)
def test_plan_against_a_core_without_processes_says_so(
    install, core, stand, tmp_path, monkeypatch, capsys, status, said
):
    core.status = status
    _set_env(monkeypatch)
    assert cp.main(_plan_args(install, tmp_path)) == 1
    assert said in capsys.readouterr().err
    assert not (tmp_path / "plan.json").exists()


# --- migrate-expr ---------------------------------------------------------------------


@dataclass(frozen=True)
class _Legacy:
    pointer: str
    syntax: str
    source: Any


@dataclass(frozen=True)
class _Translation:
    expression: str
    bindings: tuple[str, ...] = ()


class FakeTranslator:
    """Поддельный cel_profile ядра: пара legacy_expressions(kind, spec) и translate(expression)."""

    def __init__(self) -> None:
        self.kinds: list[str] = []
        self.translated: list[_Legacy] = []

    def legacy_expressions(self, kind: str, spec: Any):
        self.kinds.append(kind)
        if kind == "WorkRule" and spec.get("condition") is not None:
            yield _Legacy("/spec/condition", "condition", spec["condition"])
        if kind == "TaskType" and spec.get("approvalSchema") is not None:
            yield _Legacy("/spec/approvalSchema/gates/de~1fault", "path", "нет такого ключа")

    def translate(self, expression: _Legacy) -> _Translation:
        if expression.syntax == "path":
            raise ValueError("outside the grammar")
        self.translated.append(expression)
        return _Translation(f"cel({expression.syntax})", ("trigger",))


def test_migrate_expr_asks_the_core_where_and_how_and_writes_only_with_write(sandbox):
    pytest.importorskip("ruamel.yaml")  # diff и запись идут через package-sdk edit
    package = cp.resolve(["selfdev"]).packages[-1]
    rule = sandbox / "selfdev" / "rules" / "ci-red.yaml"
    before = rule.read_text(encoding="utf-8")
    core = FakeTranslator()

    lines: list[str] = []
    total = cp.migrate_expressions(package, core=core, log=lines.append)
    assert total == len(core.translated) > 0
    assert {"WorkRule", "TaskType", "Agent"} <= set(
        core.kinds
    )  # вид объекта ядро получает от установщика
    # spec — обычные dict/list, как ядро его читает из каталога, не дерево ruamel
    assert all(type(item.source) in (dict, list, bool, str) for item in core.translated)
    report = "\n".join(lines)
    assert (
        "+  condition: cel(condition)" in report
        and "-      - {eq: [{var: payload.data.branch}, main]}" in report
    )
    assert "reads variables beyond the profile: trigger" in report
    assert (
        "/spec/approvalSchema/gates/de~1fault (path) cannot be translated: outside the grammar"
        in report
    )
    assert rule.read_text(encoding="utf-8") == before

    cp.migrate_expressions(package, core=FakeTranslator(), write=True, log=lines.append)
    after = rule.read_text(encoding="utf-8")
    assert "  condition: cel(condition)\n" in after and after.startswith(
        before.split("apiVersion")[0]
    )


def test_migrate_expr_replaces_by_json_pointer_inside_lists_and_escaped_keys():
    document = {"spec": {"a/b": [{"x": 1}, {"when": ["$.p"]}], "c~d": {"y": 2}}}
    cp._replace_at(document, "/spec/a~1b/1/when", "cel")
    cp._replace_at(document, "/spec/c~0d/y", "other")
    assert document == {"spec": {"a/b": [{"x": 1}, {"when": "cel"}], "c~d": {"y": "other"}}}
    with pytest.raises(cp.PackageError, match="JSON pointer"):
        cp._replace_at(document, "spec/x", "cel")


def _core_translator() -> tuple[Any, str]:
    """Перевод прежних синтаксисов ядра (CP-ADR-0075 Р7) и причина, если его нет."""
    try:
        from control_plane.domain import cel_profile
    except ImportError as error:
        return None, f"control_plane.domain.cel_profile не импортируется ({error})"
    if not callable(getattr(cel_profile, "legacy_expressions", None)):
        return None, "control-plane без перевода прежних синтаксисов (CP-ADR-0075 Р7)"
    return cel_profile, ""


def test_migrate_expr_needs_the_core_translator(sandbox, monkeypatch):
    translator, reason = _core_translator()
    if translator is None:
        if REQUIRE_CORE_CONTRACT:  # ядро-сосед обязано переводить — ветка без него не годится
            core_contract_unavailable(reason)
        with pytest.raises(cp.PackageError, match="cel_profile"):
            cp.migrate_expressions(cp.resolve(["selfdev"]).packages[-1], log=lambda _m: None)
        return
    import control_plane.domain.cel_profile as cel_profile

    monkeypatch.delattr(cel_profile, "legacy_expressions")
    with pytest.raises(cp.PackageError, match="legacy_expressions"):
        cp.migrate_expressions(cp.resolve(["selfdev"]).packages[-1], log=lambda _m: None)


def test_migrate_expr_with_the_real_core_translates_selfdev_without_writing(sandbox):
    pytest.importorskip("ruamel.yaml")
    translator, reason = _core_translator()
    if translator is None:
        core_contract_unavailable(reason)
    rule = sandbox / "selfdev" / "rules" / "ci-red.yaml"
    before = rule.read_text(encoding="utf-8")
    lines: list[str] = []
    total = cp.migrate_expressions(cp.resolve(["selfdev"]).packages[-1], log=lines.append)
    report = "\n".join(lines)
    assert total > 0 and "cannot be translated" not in report
    assert 'event.payload.?data.?branch.orValue(null) == "main"' in report
    assert rule.read_text(encoding="utf-8") == before
