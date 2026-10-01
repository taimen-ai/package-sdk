"""Пирамида тестов пакета и песочница с полным каталогом (S021: FR-015–FR-017).

Фикстура — два пакета в ``tests/fixtures/pyramid/packages``: ``review-flow`` (процесс,
правило вывода работы, тип задачи с гейтом и критерием приёмки, сценарий на каждого,
код интеграции со скиллом и его unit-тестом) и ``review-base`` из его ``requires`` (роль
процесса и скилл правила) — без него каталог песочницы неполон.

Сценарии правил и типов задач исполняет прикладной код ядра в откатываемой транзакции
PostgreSQL. Базы и стенда в тестах SDK нет, поэтому:

- без базы проверяется, что песочница такие сценарии не исполняет молча — они
  ``skipped`` с находкой ``sandbox_database_required``, а прогон не зелёный;
- сверка песочницы с сервером — функция ``testing.divergence``: её падение на расхождении
  проверяется всегда, а сам прогон одних и тех же сценариев обоими путями — при
  ``PACKAGE_SDK_SANDBOX_DATABASE_URL`` (сервер — ядро в процессе, приложение FastAPI
  control-plane на той же базе) и ещё при ``PACKAGE_SDK_TEST_SERVER`` (живой стенд).
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from package_sdk import cli, model, sandbox, source, testing

pytest.importorskip("control_plane", reason="сценарии — кодом ядра (extra sandbox)")

PACKAGES = Path(__file__).resolve().parent / "fixtures" / "pyramid" / "packages"
FLOW = PACKAGES / "review-flow"
SCENARIOS = {
    "tests/request-intake.test.yaml": ("process", "request-intake"),
    "tests/request-reopened.test.yaml": ("rule", "request-reopened"),
    "tests/request-reopened-internal.test.yaml": ("rule", "request-reopened"),
    "tests/request-approval-approved.test.yaml": ("taskType", "request-approval"),
}
SERVER_ENV = "PACKAGE_SDK_TEST_SERVER"


@pytest.fixture
def packages(tmp_path: Path) -> Path:
    """Копия фикстуры, которую тест может портить."""
    root = tmp_path / "packages"
    shutil.copytree(PACKAGES, root, ignore=shutil.ignore_patterns("__pycache__"))
    return root


def _pyramid(directory: Path, **options: Any) -> testing.Pyramid:
    installation = model.resolve_targets([str(directory)])
    return testing.Pyramid(installation, [directory.name], {}, **options)


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def _no_subprocess(command: list[str], cwd: Path, env: dict[str, str]) -> Any:
    raise AssertionError(f"ступень не должна была запускаться: {command}")


# --- песочница: каталог всех видов пакета и его requires --------------------------------


def test_sandbox_runs_processes_and_does_not_skip_rule_and_task_type_tests_silently() -> None:
    report = sandbox.run_package(FLOW, env={})

    tests = {t["file"]: t for t in report["tests"]}
    assert {f: (t["subject"], t["object"]) for f, t in tests.items()} == SCENARIOS
    assert tests["tests/request-intake.test.yaml"]["status"] == "passed"
    skipped = {f for f, t in tests.items() if t["status"] == sandbox.SKIPPED}
    assert skipped == set(SCENARIOS) - {"tests/request-intake.test.yaml"}
    # Молча не пропускается (FR-014): находка с подсказкой и не зелёный прогон.
    (problem,) = report["problems"]
    assert problem["code"] == sandbox.DATABASE_REQUIRED and problem["severity"] == "warning"
    assert sandbox.DATABASE_ENV in problem["hint"]
    assert report["status"] == "failed"
    # Покрытие правил и типов задач считает ядро — и без базы видно, что в них объявлено.
    (rule,) = report["ruleCoverage"]
    assert (rule["rule"], rule["tests"], rule["outcomes"]["total"]) == ("request-reopened", 0, 4)
    (task_type,) = report["taskTypeCoverage"]
    assert task_type["taskType"] == "request-approval"
    assert "default/rejected" in task_type["outcomes"]["missing"]
    (process,) = report["coverage"]
    assert process["elements"] == {"covered": 4, "total": 4, "missing": []}


def test_the_catalog_of_the_sandbox_holds_the_objects_of_requires(packages: Path) -> None:
    """Скилл, который зовёт процесс, лежит в review-base: без него процесс не собрать."""
    intake = "tests/request-intake.test.yaml"
    tests = {t["file"]: t for t in sandbox.run_package(packages / "review-flow", env={})["tests"]}
    assert tests[intake]["status"] == "passed"
    _edit(
        packages / "review-base" / "skills" / "request.classify.yaml",
        "key: request.classify",
        "key: request.sort",
    )

    report = sandbox.run_package(packages / "review-flow", env={})

    assert report["status"] == "invalid" and report["tests"] == []
    errors = [p for p in report["problems"] if p["severity"] == "error"]
    assert errors and all(p["file"] == "processes/request-intake.yaml" for p in errors)
    assert "request.classify@1" in json.dumps(errors, ensure_ascii=False)


def _stranger(packages: Path) -> Path:
    """Пакет той же установки, от которого review-flow не зависит: скилл request.classify
    переезжает к нему из review-base, а сам он несёт тип задачи с незаданной переменной."""
    root = packages / "stranger"
    (root / "skills").mkdir(parents=True)
    (root / "task-types").mkdir()
    (root / "package.yaml").write_text(
        "apiVersion: taimen.ai/v1\nkind: Package\nkey: stranger\n"
        "spec:\n  version: 0.1.0\n  displayName: Stranger (test fixture)\n",
        encoding="utf-8",
    )
    skill = packages / "review-base" / "skills" / "request.classify.yaml"
    skill.rename(root / "skills" / skill.name)
    (root / "task-types" / "stranger-task.yaml").write_text(
        "apiVersion: taimen.ai/v1\nkind: TaskType\nkey: stranger-task\n"
        "spec:\n  displayName: Stranger task\n",
        encoding="utf-8",
    )
    return root


def test_the_catalog_of_a_package_is_itself_and_its_requires_not_the_whole_installation(
    packages: Path,
) -> None:
    """Прогон по нескольким пакетам: каталог пакета — он сам и его requires, как на стенде.
    Скилл пакета, от которого review-flow не зависит, процесс не находит."""
    _stranger(packages)
    installation = model.resolve_targets(
        [str(packages / "review-flow"), str(packages / "stranger")]
    )
    assert [p.key for p in installation.packages] == ["review-base", "review-flow", "stranger"]
    flow = installation.packages[1]
    assert [p.key for p in installation.required(flow.key)] == [
        "review-base",
        "review-flow",
    ]

    report = sandbox.run_installation(installation, flow, env={})

    assert report["status"] == "invalid" and report["tests"] == []
    errors = [p for p in report["problems"] if p["severity"] == "error"]
    assert errors and all(p["file"] == "processes/request-intake.yaml" for p in errors)
    assert "request.classify@1" in json.dumps(errors, ensure_ascii=False)


def test_rule_and_task_type_scenarios_see_only_the_package_and_its_requires(
    packages: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """В транзакцию сценариев правил и типов задач публикуются объекты пакета и его requires,
    а не всей установки: чужие объекты давали ложные находки."""
    _stranger(packages)
    # скилл остаётся в review-base: процесс review-flow собирается
    (packages / "stranger" / "skills" / "request.classify.yaml").rename(
        packages / "review-base" / "skills" / "request.classify.yaml"
    )
    installation = model.resolve_targets(
        [str(packages / "review-flow"), str(packages / "stranger")]
    )
    seen: list[tuple[str, str]] = []

    def subject_tests(d: dict[str, Any], url: str, package: Any, tests: list[Any]) -> Any:
        seen.extend((obj.ref, obj.file) for obj in package.objects)
        return SimpleNamespace(problems=[], tests=[], rule_coverage=[], task_type_coverage=[])

    monkeypatch.setattr(sandbox, "subject_tests", subject_tests)
    sandbox.run_installation(
        installation, installation.packages[1], env={}, database="postgresql+psycopg://sandbox"
    )

    files = {file for _ref, file in seen}
    assert "review-base/skills/request.classify.yaml" in files
    assert not any(file.startswith("stranger/") for file in files), files
    assert not any("stranger-task" in ref for ref, _file in seen)


def test_rules_and_task_types_are_checked_by_the_core_in_the_sandbox(packages: Path) -> None:
    _edit(
        packages / "review-flow" / "rules" / "request-reopened.yaml",
        "kind: ensure_work",
        "kind: nothing_at_all",
    )

    report = sandbox.run_package(packages / "review-flow", env={})

    assert report["status"] == "invalid"
    codes = {(p["code"], p["file"]) for p in report["problems"] if p["severity"] == "error"}
    assert any(
        code.startswith("invalid_rule") and file == "rules/request-reopened.yaml"
        for code, file in codes
    ), codes
    assert report["tests"] == []  # с ошибкой в пакете сценарии не исполняются, как в ядре


def test_database_url_takes_the_driver_of_the_core(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(sandbox.DATABASE_ENV, raising=False)
    assert sandbox.database_url(None) is None
    assert sandbox.database_url("postgresql://u@h/db") == "postgresql+psycopg://u@h/db"
    monkeypatch.setenv(sandbox.DATABASE_ENV, "postgres://u@h/db")
    assert sandbox.database_url(None) == "postgresql+psycopg://u@h/db"


def test_the_sandbox_writes_only_to_an_empty_database_or_its_own() -> None:
    own = {"public.alembic_version", "public.tenants", "public.tasks"}
    assert sandbox.database_refusal(set(), [], sandbox_tenant=False) is None
    assert sandbox.database_refusal(own, [], sandbox_tenant=True) is None
    refusal = sandbox.database_refusal(own, ["acme"], sandbox_tenant=True)
    assert refusal is not None and "acme" in refusal
    refusal = sandbox.database_refusal({"public.notes"}, [], sandbox_tenant=False)
    assert refusal is not None and "public.notes" in refusal
    assert (
        sandbox.database_refusal(
            {"public.alembic_version", "other.users"}, [], sandbox_tenant=False
        )
        is not None
    )


def test_a_core_schema_without_the_sandbox_tenant_is_not_the_sandbox_database() -> None:
    """Схема ядра без tenant'ов — база стенда до bootstrap или чужого инструмента."""
    core = {"public.alembic_version", "public.tenants", "public.tasks"}
    refusal = sandbox.database_refusal(core, [], sandbox_tenant=False)
    assert refusal is not None and sandbox.SANDBOX_TENANT in refusal
    assert "пересоздайте базу" in refusal  # в том числе после прерванного первого прогона


# --- пирамида ---------------------------------------------------------------------------


def test_the_pyramid_runs_every_stage_and_reports_coverage() -> None:
    pytest.importorskip("skill_sdk", reason="контракты скиллов — skill-sdk (extra skills)")

    report = _pyramid(FLOW).execute()

    stages = {stage["stage"]: stage for stage in report["stages"]}
    assert list(stages) == list(testing.STAGES)
    assert stages["check"]["status"] == "passed" and stages["check"]["problems"] == []
    (skills,) = stages["skills"]["packages"]
    assert (skills["status"], skills["modules"]) == ("passed", ["review_flow"]), skills
    (unit,) = stages["integration"]["packages"]
    assert unit["status"] == "passed" and "1 passed" in unit["detail"], unit
    (scenarios,) = stages["scenarios"]["packages"]
    assert stages["scenarios"]["detail"] == "sandbox"
    # без базы сценарии правил и типов не исполнены — ступень и прогон не зелёные
    assert scenarios["status"] == "failed" and report["status"] == "failed"
    coverage = report["coverage"]
    assert [c["rule"] for c in coverage["rules"]] == ["request-reopened"]
    assert [c["taskType"] for c in coverage["taskTypes"]] == ["request-approval"]
    assert coverage["totals"]["processes"]["elements"] == {"covered": 4, "total": 4}
    assert coverage["untested"] == []  # у каждого объекта есть сценарий
    json.dumps(report)


def test_cli_test_prints_the_pyramid_and_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []

    def fake(command: list[str], cwd: Path, env: dict[str, str]) -> Any:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "ok\n", "")

    monkeypatch.setattr(testing, "_run", fake)
    monkeypatch.delenv(sandbox.DATABASE_ENV, raising=False)

    assert cli.main(["test", str(FLOW), "--env", os.devnull]) == 1
    out = capsys.readouterr().out
    assert "ok   контракты скиллов (skill-sdk export --check)" in out
    assert "SKIP tests/request-reopened.test.yaml" in out and "[rule request-reopened]" in out
    assert "ok   tests/request-intake.test.yaml" in out
    assert "покрытие правила request-reopened (тестов 0)" in out
    assert "не пройдено: пирамида пакетов review-flow" in out
    assert len(calls) == 2  # skill-sdk export --check и pytest кода интеграции

    assert cli.main(["test", str(FLOW), "--env", os.devnull, "--test", "request-intake"]) == 0
    capsys.readouterr()
    assert cli.main(["test", "--package", str(FLOW), "--env", os.devnull, "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["runner"] == "sandbox" and report["packages"] == ["review-flow"]

    assert cli.main(["test", str(FLOW), "--env", os.devnull, "--test", "no-such"]) == 1
    assert "нет тестов" in capsys.readouterr().out


def test_a_skill_that_drifted_from_its_code_fails_the_skills_stage(packages: Path) -> None:
    pytest.importorskip("skill_sdk", reason="контракты скиллов — skill-sdk (extra skills)")
    flow = packages / "review-flow"
    _edit(flow / "skills" / "request.summarize.yaml", "riskLevel: low", "riskLevel: high")

    step = testing.skills_step(model.load_package(flow))

    assert step.status == "failed"
    assert "request.summarize" in step.detail and "riskLevel" in step.detail


def test_a_failing_integration_test_fails_its_stage(packages: Path) -> None:
    flow = packages / "review-flow"
    _edit(flow / "integration" / "tests" / "test_skills.py", "request R-1", "request R-2")

    step = testing.integration_step(model.load_package(flow))

    assert step.status == "failed" and "1 failed" in step.detail


def test_stages_without_a_subject_are_skipped_and_without_tools_are_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = model.load_package(PACKAGES / "review-base")
    assert testing.skills_step(base, _no_subprocess).status == "skipped"
    assert testing.integration_step(base, _no_subprocess).status == "skipped"

    monkeypatch.setattr(testing, "_has", lambda module: False)
    flow = model.load_package(FLOW)
    step = testing.skills_step(flow, _no_subprocess)
    assert step.status == "error" and "package-sdk[skills]" in step.detail
    assert testing.integration_step(flow, _no_subprocess).status == "error"


def test_integration_code_gets_no_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in (
        "CP_TOKEN",
        "NOTIFY_TOKEN",
        sandbox.DATABASE_ENV,
        "CP_DATABASE_URL",
        "CONTROL_PLANE_IAM_URL",
        "IAM_PLATFORM_ACCESS_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "SOME_API_KEY",
        "DB_PASSWORD",
    ):
        monkeypatch.setenv(name, "secret")
    monkeypatch.setenv("LANG", "C.UTF-8")

    env = testing._environ(tmp_path)

    assert not any(value == "secret" for value in env.values()), sorted(env)
    assert env["LANG"] == "C.UTF-8" and "PATH" in env
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(tmp_path)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        # адрес с паролем внутри — в переменной с любым именем
        ("POSTGRES_URL", "postgresql://app:hunter2@db:5432/app"),
        ("REDIS_URL", "redis://:hunter2@cache:6379/0"),
        ("MONGODB_URI", "mongodb+srv://app:hunter2@cluster.example.com/db"),
        ("AMQP_URL", "amqp://guest:hunter2@broker/vhost"),
        ("UPSTREAM", "https://bot:hunter2@api.example.com/v1"),
        ("SLASHED_URL", "postgresql://app:hun/ter2@db:5432/app"),  # «/» и «#» в пароле
        ("HASHED_URL", "redis://:hun#ter2@cache:6379/0"),
        # строки подключения с секретом
        ("STORAGE", "DefaultEndpointsProtocol=https;AccountName=a;AccountKey=abc==;"),
        ("SQL_CONNECTION", "Server=db;Database=app;User Id=app;Password=hunter2;"),
        ("ODBC_CONNECTION", "Driver={PostgreSQL};Server=db;Uid=app;Pwd=hunter2"),
        ("JDBC", "jdbc:postgresql://db/app?user=app&password=hunter2"),
        # персональные токены и заголовки авторизации
        ("GITHUB_PAT", "ghp_x"),
        ("PAT", "x"),
        ("REGISTRY_AUTH", "dXNlcjpwYXNz"),
        # имена ключей
        ("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE"),
        ("MAPS_APIKEY", "k"),
        ("apikey_payments", "k"),
        ("GPG_PASSPHRASE", "p"),
        ("SIGNING_KEY_PASSPHRASE_FILE", "/run/p"),
        ("TLS_CLIENT_PEM", "-----BEGIN"),
        # доступ к учёткам без секрета в значении
        ("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock"),
        ("KUBECONFIG", "/home/me/.kube/config"),
        ("DOCKER_AUTH_CONFIG", '{"auths": {}}'),
    ],
)
def test_integration_code_gets_no_credential_of_any_kind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    assert name not in testing._environ(tmp_path)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SERVICE_URL", "https://api.example.com:8443/v1"),
        ("PROXY_USER_URL", "http://user@proxy.example.com:3128"),  # без пароля
        ("KEYBOARD_LAYOUT", "us"),
        ("PEM_DIR", "/etc/ssl"),
        ("PATH_EXTRA", "/opt/bin"),
        ("AUTHOR", "someone"),
        ("DOCS_URL", "https://docs.example.com/guide?page=1#top"),
    ],
)
def test_ordinary_variables_reach_integration_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    assert testing._environ(tmp_path)[name] == value


def test_a_failed_check_stops_the_pyramid(packages: Path) -> None:
    flow = packages / "review-flow"
    _edit(flow / "tests" / "request-reopened.test.yaml", "rule: request-reopened", "rule: other")

    report = _pyramid(flow, run=_no_subprocess).execute()

    statuses = [(s["stage"], s["status"]) for s in report["stages"]]
    assert statuses == [
        ("check", "failed"),
        ("skills", "skipped"),
        ("integration", "skipped"),
        ("scenarios", "skipped"),
    ]
    (problem,) = report["stages"][0]["problems"]
    assert "rule 'other' — такого WorkRule нет в пакете review-flow" in problem["message"]
    assert "тесты не запускались" in report["stages"][-1]["detail"]


class FakeCore:
    """`POST /packages:test`: отвечает заданным отчётом и запоминает тела запросов."""

    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.bodies: list[dict[str, Any]] = []

    def test(self, body: dict[str, Any], *, check_only: bool = False) -> dict[str, Any]:
        assert not check_only
        self.bodies.append(body)
        return copy.deepcopy(self.answer)


def test_the_server_runs_the_same_scenarios_as_the_sandbox() -> None:
    answer = sandbox.run_package(FLOW, env={})
    core = FakeCore(answer)

    report = _pyramid(FLOW, api=core, run=_no_subprocess_ok).execute()

    (body,) = core.bodies  # review-base из requires ядру не отправляется: он на стенде
    assert sorted(body["tests"]) == sorted(SCENARIOS)
    assert {f["path"] for f in body["package"]["files"]} >= set(SCENARIOS)
    (scenarios,) = report["stages"][-1]["packages"]
    assert report["runner"] == "server" and scenarios["report"] == answer
    assert report["coverage"]["rules"][0]["rule"] == "request-reopened"


def _no_subprocess_ok(command: list[str], cwd: Path, env: dict[str, str]) -> Any:
    return subprocess.CompletedProcess(command, 0, "ok\n", "")


def test_a_divergence_of_the_sandbox_and_the_server_is_a_failure() -> None:
    sandboxed = sandbox.run_package(FLOW, env={})
    served = copy.deepcopy(sandboxed)
    served["durationMs"] += 100
    for result in served["tests"]:
        result["durationMs"] += 5
    for result in served["tests"]:
        if result["process"] is None:
            del result["process"]  # поле None модель ответа может опустить
    assert testing.divergence(sandboxed, served) == []

    for change in (
        lambda r: r["tests"][1].update(status="failed"),
        lambda r: r["tests"][1]["failures"].append({"step": 1, "message": "x"}),
        lambda r: r["ruleCoverage"][0]["outcomes"]["missing"].pop(),
        lambda r: r["coverage"][0]["elements"].update(covered=2),
        lambda r: r["problems"].clear(),
        lambda r: r["tests"].pop(),
        lambda r: r.update(status="passed"),
    ):
        diverged = copy.deepcopy(sandboxed)
        change(diverged)
        assert testing.divergence(sandboxed, diverged) != [], change


# --- песочница против сервера на одних и тех же сценариях --------------------------------


@pytest.fixture
def database() -> Iterator[str]:
    """Пустая база на сервере PACKAGE_SDK_SANDBOX_DATABASE_URL; удаляется после теста."""
    base = sandbox.database_url(None)
    if base is None:
        pytest.skip(f"нет {sandbox.DATABASE_ENV}: сценарии правил и типов задач — на PostgreSQL")
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    url = make_url(base)
    name = f"package_sdk_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield url.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _served_files(env: dict[str, str]) -> list[dict[str, str]]:
    """Файлы пакета для сервера — вместе с объектами его requires: так стенду не нужен
    установленный review-base, а каталог сценариев тот же, что у песочницы."""
    installation = model.resolve_targets([str(FLOW)])
    *required, flow = installation.packages
    files = source.package_files(flow, env, strict=False)
    for package in required:
        files += [
            f
            for f in source.package_files(package, env, strict=False)
            if f["path"] != "package.yaml" and not f["path"].startswith("tests/")
        ]
    return files


async def _in_process_core(url: str, files: list[dict[str, str]]) -> dict[str, Any]:
    """`POST /packages:test` приложения control-plane в процессе, на той же базе."""
    import httpx
    from control_plane.config import Settings
    from control_plane.main import create_app

    token = "package-sdk-test-bootstrap"
    settings = Settings(
        database_url=url, bootstrap_token=token, log_level="WARNING", _env_file=None
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://core") as client:
            boot = await client.post(
                "/api/v1/bootstrap",
                json={
                    "tenantSlug": sandbox.SANDBOX_TENANT,
                    "tenantName": "Package sandbox",
                    "adminDisplayName": "package-sdk sandbox",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert boot.status_code == 201, boot.text
            key = boot.json()["apiKey"]["key"]
            answer = await client.post(
                "/api/v1/packages:test",
                json={"package": {"files": files}},
                headers={"Authorization": f"Bearer {key}", "Idempotency-Key": str(uuid.uuid4())},
            )
            assert answer.status_code == 200, answer.text
            result: dict[str, Any] = answer.json()
            return result


def test_the_sandbox_and_the_core_in_process_agree(database: str) -> None:
    # схема ядра без проверки песочницы: tenant песочницы заводит bootstrap сервера
    sandbox._migrate(database)
    served = asyncio.run(_in_process_core(database, _served_files({})))
    # тот же tenant: сервер завёл его при bootstrap, песочница берёт его же
    sandboxed = sandbox.run_package(FLOW, env={}, database=database)

    assert sandboxed["status"] == "passed", json.dumps(sandboxed, ensure_ascii=False, indent=1)
    assert {t["file"]: t["status"] for t in sandboxed["tests"]} == dict.fromkeys(
        SCENARIOS, "passed"
    )
    assert testing.divergence(sandboxed, served) == []


def test_the_sandbox_and_a_live_server_agree(database: str) -> None:
    server = os.environ.get(SERVER_ENV)
    if not server:
        pytest.skip(f"нет {SERVER_ENV} (и CP_TOKEN или credential): живой стенд")
    from package_sdk.commands import _process_api

    served = _process_api(server).test({"package": {"files": _served_files({})}})
    sandboxed = sandbox.run_package(FLOW, env={}, database=database)

    assert testing.divergence(sandboxed, served) == []


# --- база песочницы: чужую не трогаем -------------------------------------------------------


def _schema(url: str) -> dict[str, Any]:
    """Ревизия alembic и все колонки public — то, что миграции ядра поменяли бы."""
    from sqlalchemy import create_engine, text

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            tables = set(
                conn.scalars(
                    text(
                        "SELECT table_name FROM information_schema.tables"
                        " WHERE table_schema = 'public'"
                    )
                )
            )
            revision = (
                list(conn.scalars(text("SELECT version_num FROM alembic_version")))
                if "alembic_version" in tables
                else None
            )
            columns = sorted(
                conn.execute(
                    text(
                        "SELECT table_name, column_name FROM information_schema.columns"
                        " WHERE table_schema = 'public'"
                    )
                ).all()
            )
        return {"revision": revision, "columns": columns}
    finally:
        engine.dispose()


def _execute(url: str, *statements: str) -> None:
    from sqlalchemy import create_engine, text

    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            for statement in statements:
                conn.execute(text(statement))
    finally:
        engine.dispose()


def _refused(url: str) -> str:
    with pytest.raises(model.PackageError) as refused:
        sandbox.run_package(FLOW, env={}, database=url)
    return str(refused.value)


def test_a_database_with_foreign_tables_is_refused_untouched(database: str) -> None:
    _execute(database, "CREATE TABLE notes (id int)", "INSERT INTO notes VALUES (1)")
    before = _schema(database)

    assert "public.notes" in _refused(database)

    assert _schema(database) == before and before["revision"] is None


def test_a_stand_database_is_refused_before_any_migration(database: str) -> None:
    """База как у стенда: схема ядра старше закреплённой и tenant не песочницы."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(sandbox._migrations()))
    head = ScriptDirectory.from_config(config).get_current_head()
    previous = ScriptDirectory.from_config(config).get_revision(head).down_revision
    sandbox._migrate(database, str(previous))
    _execute(
        database,
        "INSERT INTO tenants (id, slug, name, created_at, updated_at)"
        f" VALUES ('{uuid.uuid4()}', 'acme', 'Acme', now(), now())",
    )
    before = _schema(database)
    assert before["revision"] == [previous]

    message = _refused(database)

    assert "acme" in message and "ничего не изменено" in message
    assert _schema(database) == before  # ни ревизии, ни колонок миграции не тронули


def test_a_core_schema_without_tenants_is_refused_untouched(database: str) -> None:
    sandbox._migrate(database)  # как база стенда до bootstrap
    before = _schema(database)

    message = _refused(database)

    assert sandbox.SANDBOX_TENANT in message and "ничего не изменено" in message
    assert _schema(database) == before
    from sqlalchemy import create_engine, text

    engine = create_engine(database)
    try:
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM tenants")) == 0
    finally:
        engine.dispose()


def test_two_first_runs_on_an_empty_database_do_not_race(database: str) -> None:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(2) as pool:
        for future in [pool.submit(sandbox.prepare_database, database) for _ in range(2)]:
            future.result()
    assert _schema(database)["revision"] is not None


def test_a_database_newer_than_the_core_is_a_clear_error(database: str) -> None:
    sandbox.prepare_database(database)
    _execute(database, "UPDATE alembic_version SET version_num = 'f0f0f0f0f0f0'")

    assert "новее ядра" in _refused(database)
