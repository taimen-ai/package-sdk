"""Пирамида тестов пакета одной командой — `package-sdk test` (FR-015–FR-017, TAI-ADR-0062).

Ступени, по порядку:

1. **check** — схема, замкнутость ссылок, валидаторы ядра (как `package-sdk check`);
   если она не прошла, выше не идём — тесты не запускаются;
2. **skills** — контракты скиллов кода интеграции против файлов пакета
   (`skill-sdk export --check`, TAI-ADR-0045): код лежит в `integration/src/`
   (или `integration/`), скиллы в нём ищет skill-sdk;
3. **integration** — pytest кода интеграции (`integration/tests/`), если он есть;
4. **scenarios** — сценарии пакета `tests/*.test.yaml`: процессы, правила и типы задач.
   Исполняет их код ядра — сервер (`--server`, `POST /packages:test`) или песочница
   в процессе (по умолчанию, `package-sdk[sandbox]`; тестам правил и типов задач нужна
   база — `--database-url`).

Отчёт общий: ступени с их итогами и покрытие процессов, правил и типов задач с
перечнем того, что не покрыто ни одним сценарием; `--json` — он же документом.
Ступень без предмета (нет кода интеграции, нет его тестов) — `skipped` и не валит
прогон; ступень, которой нечем исполниться (нет skill-sdk, pytest, кода ядра), —
`error`: молча её не пропускаем (FR-014).

    package-sdk test packages/<пакет>
    package-sdk test --package <пакет> --server https://cp.example.com --json
    package-sdk test . --test cancel --database-url postgresql://…/sandbox

Код выхода 0 — все ступени зелёные (или пропущены за отсутствием предмета).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from package_sdk import model
from package_sdk.check import CORE_MISSING, check
from package_sdk.core import CoreUnsupported, print_test_report, static_error
from package_sdk.source import test_request

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
ERROR = "error"
STAGES = ("check", "skills", "integration", "scenarios")
# Сколько последних строк вывода подпроцесса попадает в отчёт.
OUTPUT_TAIL = 40
# Каталог кода интеграции в пакете (package-sdk init --integration).
INTEGRATION = "integration"
# pytest: «тестов не найдено» — не провал, а пустая ступень.
PYTEST_NO_TESTS = 5


class CoreApi(Protocol):
    """То, что пирамиде нужно от сервера: `POST /packages:test` (core.ProcessApi)."""

    def test(self, body: dict[str, Any], *, check_only: bool = False) -> dict[str, Any]: ...


@dataclass
class Step:
    """Итог ступени для одного пакета."""

    package: str
    status: str
    detail: str = ""
    report: dict[str, Any] | None = None
    modules: list[str] = field(default_factory=list)

    def out(self) -> dict[str, Any]:
        out: dict[str, Any] = {"package": self.package, "status": self.status}
        if self.detail:
            out["detail"] = self.detail
        if self.modules:
            out["modules"] = self.modules
        if self.report is not None:
            out["report"] = self.report
        return out


@dataclass
class Stage:
    name: str
    status: str = SKIPPED
    detail: str = ""
    steps: list[Step] = field(default_factory=list)
    problems: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def settle(self) -> None:
        """Итог ступени по итогам пакетов: хуже всех — error, потом failed."""
        statuses = {step.status for step in self.steps}
        for status in (ERROR, FAILED, PASSED):
            if status in statuses:
                self.status = status
                return
        self.status = SKIPPED

    def out(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "stage": self.name,
            "status": self.status,
            "durationMs": self.duration_ms,
        }
        if self.detail:
            out["detail"] = self.detail
        if self.name == "check":
            out["problems"] = self.problems
            out["warnings"] = self.warnings
        else:
            out["packages"] = [step.out() for step in self.steps]
        return out


def _tail(text: str) -> str:
    lines = text.strip().splitlines()
    return "\n".join(lines[-OUTPUT_TAIL:])


def _has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def integration_root(package: model.Package) -> Path | None:
    """Где лежат модули кода интеграции: `integration/src` или сам `integration/`."""
    base = package.path / INTEGRATION
    if not base.is_dir():
        return None
    return base / "src" if (base / "src").is_dir() else base


def integration_modules(root: Path) -> list[str]:
    """Модули верхнего уровня кода интеграции: пакеты с `__init__.py` и файлы `*.py`."""
    found = []
    for path in sorted(root.iterdir()):
        if path.name.startswith((".", "_")) or path.name in ("tests", "conftest.py"):
            continue
        if path.is_dir() and (path / "__init__.py").is_file():
            found.append(path.name)
        elif path.is_file() and path.suffix == ".py":
            found.append(path.stem)
    return found


# Чего код интеграции в подпроцессах не получает: учётки ядра и сервисов, адреса баз и
# любые переменные, похожие на секрет. Его тестам стенд и база не нужны.
_SECRET_NAME = re.compile(
    r"TOKEN|SECRET|PASSW|PASSPHRASE|CREDENTIAL|PRIVATE|API_?KEY|ACCESS_KEY|"
    r"(^|_)KEY$|(^|_)PAT$|_AUTH$|_PEM$|(^|_)DSN$|DATABASE_URL|"
    r"^CP_|^CONTROL_PLANE_|^IAM_|^PACKAGE_SDK_",
    re.IGNORECASE,
)
# Доступ к чужим учёткам без секрета в самом значении: агент ssh, кластеры Kubernetes,
# реестры Docker.
_SECRET_NAMES = frozenset({"SSH_AUTH_SOCK", "KUBECONFIG", "DOCKER_AUTH_CONFIG"})
# Адрес с паролем внутри (`схема://пользователь:пароль@хост`, `redis://:пароль@хост`) —
# в любой переменной, как бы она ни называлась: POSTGRES_URL, REDIS_URL, MONGODB_URI, AMQP_URL.
# Пароль — до последнего «@», с «/», «#» и «?» внутри: лишний раз убрать переменную
# безопаснее, чем пропустить пароль.
_URL_WITH_PASSWORD = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/?#@\s:]*:\S*@")
# Строки подключения с секретом: `AccountKey=` (Azure Storage), `Password=` и `Pwd=`
# (ADO.NET, ODBC, JDBC-параметры).
_CONNECTION_SECRET = re.compile(r"(^|[;&?\s])(AccountKey|Password|Pwd)\s*=", re.IGNORECASE)


def _secret(name: str, value: str) -> bool:
    return (
        name.upper() in _SECRET_NAMES
        or bool(_SECRET_NAME.search(name))
        or bool(_URL_WITH_PASSWORD.search(value))
        or bool(_CONNECTION_SECRET.search(value))
    )


def _environ(root: Path) -> dict[str, str]:
    """Окружение подпроцесса кода интеграции: без секретов, с его модулями в PYTHONPATH.

    Это не песочница: `HOME` сохраняется, и файлы учётных данных в нём (`~/.aws`,
    `~/.ssh`, `~/.docker/config.json`, `~/.netrc`) коду интеграции доступны."""
    env = {name: value for name, value in os.environ.items() if not _secret(name, value)}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(root), env.get("PYTHONPATH")]))
    env.pop("PYTEST_ADDOPTS", None)
    # байт-код кода интеграции в дереве пакета не нужен: прогон ничего там не пишет
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


Runner = Callable[[list[str], Path, dict[str, str]], "subprocess.CompletedProcess[str]"]


def _run(command: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def skills_step(package: model.Package, run: Runner = _run) -> Step:
    """Ступень 2: код интеграции ↔ YAML `kind: Skill` пакета (`skill-sdk export --check`)."""
    root = integration_root(package)
    if root is None:
        return Step(package.key, SKIPPED, "no integration code (integration/)")
    modules = integration_modules(root)
    if not modules:
        return Step(package.key, SKIPPED, f"no modules in {model._rel(root)}")
    if not _has("skill_sdk"):
        return Step(
            package.key,
            ERROR,
            "skill contract check requires skill-sdk — install package-sdk[skills]",
            modules=modules,
        )
    command = [
        sys.executable,
        "-c",
        "import sys; from skill_sdk.cli import main; sys.exit(main(sys.argv[1:]))",
        "export",
        "--package",
        str(package.path),
        "--check",
        *modules,
    ]
    done = run(command, root, _environ(root))
    output = _tail(done.stdout + done.stderr)
    return Step(package.key, PASSED if done.returncode == 0 else FAILED, output, modules=modules)


def integration_step(package: model.Package, run: Runner = _run) -> Step:
    """Ступень 3: pytest кода интеграции (`integration/tests/`)."""
    root = integration_root(package)
    tests = package.path / INTEGRATION / "tests"
    if root is None or not tests.is_dir():
        return Step(package.key, SKIPPED, "no integration code tests (integration/tests/)")
    if not _has("pytest"):
        return Step(package.key, ERROR, "integration code tests require pytest")
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tests)]
    done = run(command, package.path / INTEGRATION, _environ(root))
    output = _tail(done.stdout + done.stderr)
    if done.returncode == PYTEST_NO_TESTS:
        return Step(package.key, SKIPPED, output or "pytest found no tests")
    return Step(package.key, PASSED if done.returncode == 0 else FAILED, output)


def scenario_status(report: dict[str, Any]) -> str:
    """Итог ступени сценариев пакета по PackageTestOut: без сценариев — skipped."""
    if report.get("status") != PASSED:
        return FAILED
    return PASSED if report.get("tests") else SKIPPED


@dataclass
class Pyramid:
    """Прогон пирамиды: какие пакеты, чем исполнять сценарии, с какими переменными."""

    installation: model.Installation
    named: list[str]
    env: dict[str, str]
    # сценарии по имени или файлу: один (--test) или несколько (MCP-инструмент pkg_test)
    test: str | Sequence[str] | None = None
    server: str | None = None
    workspace: str | None = None
    database: str | None = None
    api: CoreApi | None = None
    # как запускать подпроцессы ступеней skills и integration (None — subprocess.run)
    run: Runner | None = None
    stages: list[Stage] = field(default_factory=list)

    @property
    def runner(self) -> str:
        return "server" if self.server or self.api is not None else "sandbox"

    def packages(self) -> list[model.Package]:
        return [p for p in self.installation.packages if p.key in self.named]

    def wanted(self) -> list[str]:
        """Имена или файлы сценариев, которыми сужен прогон; пусто — все."""
        if self.test is None:
            return []
        return [self.test] if isinstance(self.test, str) else list(self.test)

    def selected(self, package: model.Package) -> list[model.PackageTest]:
        wanted = self.wanted()
        return [
            t
            for t in package.tests
            if not wanted
            or any(w in (t.name, t.path.name, t.path.stem.removesuffix(".test")) for w in wanted)
        ]

    def _check(self) -> Stage:
        stage = Stage("check")
        errors, warnings = check(self.installation, env=self.env)
        if CORE_MISSING in warnings and self.runner == "sandbox":
            # сценарии песочницы — кодом ядра: без него пирамида не исполнится (FR-014)
            warnings.remove(CORE_MISSING)
            errors.append(CORE_MISSING)
        stage.problems = [static_error(e) for e in errors]
        stage.warnings = warnings
        stage.status = FAILED if errors else PASSED
        return stage

    def _scenarios(self) -> Stage:
        stage = Stage("scenarios", detail=self.runner)
        for package in self.packages():
            chosen = self.selected(package)
            if self.wanted() and not chosen:
                names = ", ".join(repr(w) for w in self.wanted())
                stage.steps.append(Step(package.key, SKIPPED, f"no test {names}"))
                continue
            try:
                report = self._scenario_report(package, chosen)
            except CoreUnsupported as error:
                stage.steps.append(Step(package.key, ERROR, str(error)))
                continue
            except (urllib.error.URLError, OSError) as error:
                stage.steps.append(Step(package.key, ERROR, f"the core is unreachable: {error}"))
                continue
            except model.PackageError as error:
                stage.steps.append(Step(package.key, ERROR, str(error)))
                continue
            stage.steps.append(Step(package.key, scenario_status(report), report=report))
        return stage

    def _scenario_report(
        self, package: model.Package, chosen: list[model.PackageTest]
    ) -> dict[str, Any]:
        """PackageTestOut пакета: сервер или песочница, одни и те же файлы сценариев."""
        if self.runner == "server":
            api = self.api
            if api is None:
                from package_sdk.commands import _process_api

                api = _process_api(str(self.server))
            body = test_request(package, self.env, tests=chosen, workspace=self.workspace)
            return api.test(body)
        from package_sdk import sandbox

        try:
            return sandbox.run_installation(
                self.installation,
                package,
                tests=[t.path.relative_to(package.path).as_posix() for t in chosen],
                env=self.env,
                database=self.database,
            )
        except sandbox.CoreMissing as error:
            raise CoreUnsupported(str(error)) from error

    def execute(self) -> dict[str, Any]:
        started = time.monotonic()
        self.stages = []
        stages: list[tuple[str, Callable[[], Stage]]] = [
            ("check", self._check),
            ("skills", lambda: self._each("skills", skills_step)),
            ("integration", lambda: self._each("integration", integration_step)),
            ("scenarios", self._scenarios),
        ]
        blocked = ""
        for name, runner in stages:
            if blocked:
                self.stages.append(Stage(name, SKIPPED, blocked))
                continue
            began = time.monotonic()
            stage = runner()
            if stage.steps:
                stage.settle()
            stage.duration_ms = int((time.monotonic() - began) * 1000)
            self.stages.append(stage)
            if name == "check" and stage.status != PASSED:
                blocked = "static check failed — tests were not run"
        return self.report(int((time.monotonic() - started) * 1000))

    def _each(self, name: str, step: Callable[[model.Package, Runner], Step]) -> Stage:
        stage = Stage(name)
        stage.steps = [step(package, self.run or _run) for package in self.packages()]
        return stage

    def report(self, duration_ms: int) -> dict[str, Any]:
        failed = any(stage.status in (FAILED, ERROR) for stage in self.stages)
        return {
            "status": FAILED if failed else PASSED,
            "runner": self.runner,
            "packages": self.named,
            "stages": [stage.out() for stage in self.stages],
            "coverage": coverage(self.stages),
            "durationMs": duration_ms,
        }


def _counter_sum(items: Sequence[dict[str, Any]], names: Sequence[str]) -> dict[str, Any]:
    total: dict[str, Any] = {}
    for name in names:
        covered = sum((item.get(name) or {}).get("covered", 0) for item in items)
        whole = sum((item.get(name) or {}).get("total", 0) for item in items)
        total[name] = {"covered": covered, "total": whole}
    return total


def coverage(stages: Sequence[Stage]) -> dict[str, Any]:
    """Покрытие по всем пакетам: процессы, правила, типы задач и что без сценариев вовсе.

    «Без сценариев» — у объекта нет ни одного файла сценария (исполненного или нет):
    это то, что требует SC-008, а не итог прогона."""
    processes: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []
    task_types: list[dict[str, Any]] = []
    tested: set[tuple[str, str, str]] = set()
    for stage in stages:
        if stage.name != "scenarios":
            continue
        for step in stage.steps:
            report = step.report or {}
            processes += [{"package": step.package, **c} for c in report.get("coverage") or []]
            rules += [{"package": step.package, **c} for c in report.get("ruleCoverage") or []]
            task_types += [
                {"package": step.package, **c} for c in report.get("taskTypeCoverage") or []
            ]
            for result in report.get("tests") or []:
                subject = str(result.get("subject") or "process")
                key = result.get("object") or result.get("process")
                tested.add((step.package, subject, str(key)))
    untested = sorted(
        [
            f"{c['package']}: Process/{c['process']}"
            for c in processes
            if (c["package"], "process", c["process"]) not in tested
        ]
        + [
            f"{c['package']}: WorkRule/{c['rule']}"
            for c in rules
            if (c["package"], "rule", c["rule"]) not in tested
        ]
        + [
            f"{c['package']}: TaskType/{c['taskType']}"
            for c in task_types
            if (c["package"], "taskType", c["taskType"]) not in tested
        ]
    )
    return {
        "processes": processes,
        "rules": rules,
        "taskTypes": task_types,
        "totals": {
            "processes": _counter_sum(
                processes, ("elements", "transitions", "decisionRows", "handlers")
            ),
            "rules": _counter_sum(rules, ("branches", "outcomes")),
            "taskTypes": _counter_sum(
                task_types, ("outcomes", "preconditions", "completion", "acceptance")
            ),
        },
        "untested": untested,
    }


# Что в PackageTestOut зависит не от кода ядра, а от прогона: время.
_VOLATILE = frozenset({"durationMs"})


def _normalized(value: Any) -> Any:
    """Отчёт без времени и без пустых (None) полей: ответ сервера проходит модель
    PackageTestOut, и поле со значением None в нём может быть, а может не быть."""
    if isinstance(value, dict):
        return {k: _normalized(v) for k, v in value.items() if k not in _VOLATILE and v is not None}
    if isinstance(value, list):
        return [_normalized(item) for item in value]
    return value


def divergence(sandbox: dict[str, Any], server: dict[str, Any]) -> list[str]:
    """Расхождения PackageTestOut песочницы и сервера на одних и тех же файлах пакета.

    Пустой список — песочница и ядро на стенде судят пакет одинаково (FR-015): тот же
    итог, те же находки, те же результаты сценариев с их провалами и то же покрытие.
    Время прогона не сравнивается."""
    left, right = _normalized(sandbox), _normalized(server)
    found: list[str] = []
    for field_name in ("status", "checkOnly"):
        mine, theirs = left.get(field_name), right.get(field_name)
        if mine != theirs:
            found.append(f"{field_name}: sandbox {mine!r}, server {theirs!r}")
    for field_name in ("problems", "coverage", "ruleCoverage", "taskTypeCoverage"):
        mine = sorted(left.get(field_name) or [], key=lambda item: json.dumps(item, sort_keys=True))
        theirs = sorted(
            right.get(field_name) or [], key=lambda item: json.dumps(item, sort_keys=True)
        )
        if mine != theirs:
            found.append(
                f"{field_name}: sandbox {json.dumps(mine, ensure_ascii=False)}, "
                f"server {json.dumps(theirs, ensure_ascii=False)}"
            )
    tests = {t.get("file"): t for t in left.get("tests") or []}
    served = {t.get("file"): t for t in right.get("tests") or []}
    for file in sorted(set(tests) | set(served), key=str):
        if tests.get(file) != served.get(file):
            found.append(
                f"{file}: sandbox {json.dumps(tests.get(file), ensure_ascii=False)}, "
                f"server {json.dumps(served.get(file), ensure_ascii=False)}"
            )
    return found


_MARKS = {PASSED: "ok  ", FAILED: "FAIL", ERROR: "ERR ", SKIPPED: "SKIP"}
_TITLES = {
    "check": "check: schema, references, core validators",
    "skills": "skill contracts (skill-sdk export --check)",
    "integration": "integration code tests (pytest)",
    "scenarios": "package scenarios",
}


def print_report(report: dict[str, Any], log: Callable[[str], None] = print) -> None:
    for stage in report["stages"]:
        title = _TITLES[stage["stage"]]
        if stage["stage"] == "scenarios" and stage.get("detail") in ("server", "sandbox"):
            title += " — " + ("server" if stage["detail"] == "server" else "core sandbox")
        log(f"{_MARKS.get(stage['status'], stage['status'])} {title}")
        if stage["stage"] == "check":
            for warning in stage.get("warnings") or []:
                log(f"   warning: {warning}")
            for problem in stage.get("problems") or []:
                where = f"{problem['file']}: " if problem.get("file") else ""
                log(f"   error: {where}{problem.get('message', '')}")
            continue
        if stage.get("detail") and stage["status"] == SKIPPED and not stage.get("packages"):
            log(f"   {stage['detail']}")
        for step in stage.get("packages") or []:
            mark = _MARKS.get(step["status"], step["status"])
            if step.get("report") is not None:
                log(f"== {step['package']}")
                print_test_report(step["report"], log=log)
                continue
            lines = (step.get("detail") or "").splitlines()
            if step["status"] in (FAILED, ERROR) and len(lines) > 1:
                log(f"   {mark} {step['package']}:")
                for line in lines:
                    log(f"      {line}")
            else:
                # итог подпроцесса — в его последней строке («1 passed in …», «ok»)
                log(f"   {mark} {step['package']}: " + (lines[-1] if lines else ""))
    totals = report["coverage"]["totals"]
    parts = []
    for group, title in (
        ("processes", "processes"),
        ("rules", "rules"),
        ("taskTypes", "task types"),
    ):
        counters = [
            f"{name} {value['covered']}/{value['total']}"
            for name, value in totals[group].items()
            if value["total"]
        ]
        if counters:
            parts.append(f"{title}: {', '.join(counters)}")
    if parts:
        log("coverage — " + "; ".join(parts))
    for item in report["coverage"]["untested"]:
        log(f"   no scenarios: {item}")
    log(
        ("ok" if report["status"] == PASSED else "failed")
        + f": test pyramid of packages {', '.join(report['packages'])} ({report['durationMs']} ms)"
    )


def _installation(args: argparse.Namespace) -> tuple[model.Installation, list[str]]:
    """Установка и ключи пакетов, которые тестируются: --install, пути/ключи или все."""
    if args.install:
        from package_sdk import install

        # источники git — по lock, из кэша; сверка с источником и lock — дело плана
        installation = install.load(args.install, strict=False).installation
        return installation, [p.key for p in installation.packages]
    values = [*(args.paths or []), *(args.package or [])]
    if not values:
        installation = model.resolve([d.name for d in model.all_package_dirs()])
        return installation, [p.key for p in installation.packages]
    installation = model.resolve_targets(values)
    names = [
        model.package_name(v) if (Path(v) / "package.yaml").is_file() else Path(v).name
        for v in values
    ]
    return installation, names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="package-sdk test",
        description="package test pyramid in one command: check, skills, integration, scenarios",
    )
    parser.add_argument("paths", nargs="*", help="packages: directories with package.yaml or keys")
    parser.add_argument(
        "--package", action="append", help="package (directory or key); may be repeated"
    )
    parser.add_argument("--install", type=Path, help="installation file: all of its packages")
    parser.add_argument("--test", help="only the scenario with this name or file")
    parser.add_argument(
        "--server", help="Control Plane: the server runs the scenarios; without it — the sandbox"
    )
    parser.add_argument("--env", type=Path, default=Path(".env"), help="installation variables")
    parser.add_argument(
        "--workspace",
        help="with --server: the workspace whose roles, calendars and instances the run reads",
    )
    parser.add_argument(
        "--database-url",
        help="sandbox: an empty PostgreSQL database for rule and task type scenarios "
        "(or PACKAGE_SDK_SANDBOX_DATABASE_URL)",
    )
    parser.add_argument("--json", action="store_true", help="pyramid report as a JSON document")
    args = parser.parse_args(argv)
    from package_sdk import sandbox

    try:
        installation, named = _installation(args)
        pyramid = Pyramid(
            installation,
            named,
            {**model.read_env_file(args.env), **os.environ},
            test=args.test,
            server=args.server,
            workspace=args.workspace,
            database=sandbox.database_url(args.database_url),
        )
        if args.test and not any(pyramid.selected(p) for p in pyramid.packages()):
            print(f"no tests: packages {', '.join(named)} have no scenario {args.test!r}")
            return 1
        report = pyramid.execute()
    except (model.PackageError, RuntimeError) as error:
        print("error:", error, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)
    return 0 if report["status"] == PASSED else 1


if __name__ == "__main__":
    sys.exit(main())
