"""Проверка, тесты, план и применение процессов и календарей ядром (/packages:test|plan|apply)."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from package_sdk.apply import HttpError, HttpLike
from package_sdk.model import API, PackageError
from package_sdk.source import test_request

PACKAGES_TEST = "/packages:test"


PACKAGES_PLAN = "/packages:plan"


PACKAGES_APPLY = "/packages:apply"


# Виды, которые ядро планирует и ставит по плану пакета (PLANNED_KINDS control-plane,
# CP-ADR-0074 п.11, амендмент 2026-09-29; View — CP-ADR-0080): у пакета с процессами,
# календарями или экранами их ставит секция core единого плана, а не установщик.
CORE_PLANNED_KINDS = ("TaskType", "Agent", "Calendar", "Process", "WorkRule", "View")


PLAN_STALE = "plan_stale"


class CoreUnsupported(PackageError):
    """Ядро не знает маршрута (404) или ещё не реализует его (501 not_implemented)."""


test_request.__test__ = False  # type: ignore[attr-defined]  # не тест pytest


def _error_envelope(body: Any) -> dict[str, Any]:
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        envelope: dict[str, Any] = body["error"]
        return envelope
    return body if isinstance(body, dict) else {}


@dataclass
class ProcessApi:
    """Вызовы ядра для пакетов с процессами; ошибки HTTP → понятные исключения."""

    http: HttpLike
    headers: dict[str, str] = field(repr=False)  # может нести Authorization — не в repr

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {**self.headers, "Idempotency-Key": str(uuid.uuid4())}
        try:
            return self.http.call("POST", API + path, body, headers)
        except HttpError as error:
            route = path.split("?", 1)[0]
            envelope = _error_envelope(error.body)
            if error.status == 404:
                raise CoreUnsupported(
                    f"the core does not know {route} — processes are not rolled out on it yet"
                ) from error
            if error.status == 501:
                step = (envelope.get("details") or {}).get("implementedBy")
                raise CoreUnsupported(
                    f"the core does not implement {route} yet" + (f" ({step})" if step else "")
                ) from error
            if envelope.get("code") == PLAN_STALE:
                raise PackageError(
                    "plan is stale: the stand catalog changed after the plan was built — "
                    "build the plan again (package-sdk plan … --out) and apply the new one"
                ) from error
            problems = core_errors(error.body)
            if problems:
                raise CoreRejected(problems) from error
            raise

    def test(self, body: dict[str, Any], *, check_only: bool = False) -> dict[str, Any]:
        return self._post(PACKAGES_TEST + ("?checkOnly=true" if check_only else ""), body)

    def plan(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._post(PACKAGES_PLAN, body)

    def apply(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._post(PACKAGES_APPLY, body)


class CoreRejected(PackageError):
    """Ядро отвергло пакет: находки проверки в машиночитаемом виде."""

    def __init__(self, problems: list[dict[str, Any]]) -> None:
        super().__init__(
            "the core rejects the package:\n  "
            + "\n  ".join(format_core_error(p) for p in problems)
        )
        self.errors = problems


def core_problems(body: Any) -> list[dict[str, Any]]:
    """Все находки ответа ядра (ProcessProblemOut): problems в ответе или в details ошибки."""
    if not isinstance(body, dict):
        return []
    if isinstance(body.get("problems"), list):
        return [p for p in body["problems"] if isinstance(p, dict)]
    details = _error_envelope(body).get("details")
    if isinstance(details, dict) and isinstance(details.get("problems"), list):
        return [p for p in details["problems"] if isinstance(p, dict)]
    return []


def core_errors(body: Any) -> list[dict[str, Any]]:
    """Находки с severity error (без severity — тоже ошибка)."""
    return [p for p in core_problems(body) if p.get("severity", "error") == "error"]


def core_warnings(body: Any) -> list[dict[str, Any]]:
    return [p for p in core_problems(body) if p.get("severity") == "warning"]


def format_core_error(error: dict[str, Any]) -> str:
    """ProcessProblemOut → «файл:строка: код: сообщение [путь] (подсказка: …)»."""
    place = error.get("file") or ""
    if place and error.get("line") is not None:
        place += f":{error['line']}"
    text = (
        f"{place + ': ' if place else ''}{error.get('code', 'error')}: {error.get('message', '')}"
    )
    if error.get("path"):
        text += f" [{error['path']}]"
    if error.get("hint"):
        text += f" (hint: {error['hint']})"
    return text


_STATIC_CODE = re.compile(r"[a-z]+(?:_[a-z]+)+")
# Путь находки в документе (JSON Pointer) в конце сообщения: «… [/spec/layout/0/title]».
_STATIC_PATH = re.compile(r"\s\[(/[^\]\s]*)\]$")


def static_error(message: str) -> dict[str, Any]:
    """Ошибка статической проверки в форме находки ядра."""
    file, _, rest = message.partition(": ")
    if not (rest and ("/" in file or file.endswith((".yaml", ".yml")))):
        file, rest = "", message
    # Правила манифеста называют себя кодом: «<файл>: variable_undeclared: …»
    code, _, text = rest.partition(": ")
    found: dict[str, Any] = {"code": "static_check", "severity": "error", "message": rest}
    if _STATIC_CODE.fullmatch(code) and text:
        found.update(code=code, message=text)
    path = _STATIC_PATH.search(found["message"])
    if path:
        found.update(message=found["message"][: path.start()], path=path.group(1))
    if file:
        found["file"] = file
    return found


_COUNTERS = ("elements", "transitions", "decisionRows", "handlers")
# Счётчики покрытия правил и типов задач (RuleCoverage, TaskTypeCoverage ядра, CP-ADR-0074 Z3).
RULE_COUNTERS = ("branches", "outcomes")
TASK_TYPE_COUNTERS = ("outcomes", "preconditions", "completion", "acceptance")


def _subject(result: dict[str, Any]) -> str:
    """Что проверяет тест: процесс — его ключом, правило и тип задачи — видом и ключом."""
    if result.get("process"):
        return str(result["process"])
    subject, key = result.get("subject"), result.get("object")
    return f"{subject} {key}" if subject and key else str(key or "")


def _counters(
    title: str, coverage: dict[str, Any], names: tuple[str, ...], log: Callable[[str], None]
) -> None:
    parts = [
        f"{name} {(coverage.get(name) or {}).get('covered', 0)}/{coverage[name]['total']}"
        for name in names
        if isinstance(coverage.get(name), dict) and coverage[name].get("total")
    ]
    log(f"coverage {title}: {', '.join(parts) or 'no data'}")
    for name in names:
        missing = (coverage.get(name) or {}).get("missing") or []
        if missing:
            log(f"   not covered ({name}): {', '.join(str(m) for m in missing)}")


def print_test_report(response: dict[str, Any], log: Callable[[str], None] = print) -> bool:
    """PackageTestOut: находки, результаты тестов, покрытие; True — status passed."""
    for problem in core_errors(response):
        log(f"error: {format_core_error(problem)}")
    for problem in core_warnings(response):
        log(f"warning: {format_core_error(problem)}")
    results = response.get("tests") or []
    for result in results:
        status = result.get("status", "error")
        mark = {"passed": "ok  ", "failed": "FAIL", "error": "ERR ", "skipped": "SKIP"}.get(
            status, status
        )
        took = f" ({result['durationMs']} ms)" if result.get("durationMs") is not None else ""
        log(f"{mark} {result.get('file', '')}: {result.get('name', '')} [{_subject(result)}]{took}")
        for failure in result.get("failures") or []:
            log(f"     step {failure.get('step')}: {failure.get('message', '')}")
            if failure.get("expected") is not None or failure.get("actual") is not None:
                log(
                    f"       expected: {json.dumps(failure.get('expected'), ensure_ascii=False)}; "
                    f"actual: {json.dumps(failure.get('actual'), ensure_ascii=False)}"
                )
    for coverage in response.get("coverage") or []:
        _counters(f"{coverage.get('process')} v{coverage.get('version')}", coverage, _COUNTERS, log)
    for coverage in response.get("ruleCoverage") or []:
        title = f"rule {coverage.get('rule')} (tests {coverage.get('tests', 0)})"
        _counters(title, coverage, RULE_COUNTERS, log)
    for coverage in response.get("taskTypeCoverage") or []:
        title = (
            f"task type {coverage.get('taskType')} v{coverage.get('version')} "
            f"(tests {coverage.get('tests', 0)})"
        )
        _counters(title, coverage, TASK_TYPE_COUNTERS, log)
    status = response.get("status", "invalid")
    passed = sum(r.get("status") == "passed" for r in results)
    log(
        f"{'ok' if status == 'passed' else 'failed'} ({status}): tests {len(results)}, passed {passed}"
    )
    return bool(status == "passed")


def print_plan_deadlines(
    deadlines: list[dict[str, Any]],
    log: Callable[[str], None] = print,
    total: int | None = None,
) -> None:
    """PlanProcessOut.deadlines: сроки экземпляров, которые миграция ставит, сдвигает или снимает;
    пустой список — раздела нет. Ядро отдаёт список с потолком, а всех — в deadlinesTotal
    (total): при усечении заголовок говорит «показано N из M» (TASK-001162, TASK-001183)."""
    total = max(total or 0, len(deadlines))
    if not total:
        return
    breached = sum(bool(d.get("breached")) for d in deadlines)
    cut = len(deadlines) < total
    header = f"  deadlines: instances {total}"
    if cut:
        header += f", shown {len(deadlines)} of {total}"
    if breached:
        header += f", already breached {breached}" + (" among shown" if cut else "")
    log(header)
    for deadline in deadlines:
        element = deadline.get("element")
        scope = f"step {element}" if element else "whole case"
        before = deadline.get("previousDueAt") or "none"
        after = deadline.get("dueAt") or "removed"
        mark = " — already breached" if deadline.get("breached") else ""
        log(f"    {deadline.get('instanceId')}, {scope}: {before} → {after}{mark}")


def print_plan(response: dict[str, Any], log: Callable[[str], None] = print) -> bool:
    """PackagePlanOut: структурный и поведенческий diff, судьба экземпляров, покрытие
    регламентов; False — в плане ошибки (например migration_required)."""
    marks = {"create": "+", "update": "~", "retire": "-", "rename": "→", "unchanged": "="}
    package = response.get("package") or {}
    log(
        f"plan {package.get('key', '?')} {package.get('version', '')}: {response.get('planHash', '?')} "
        f"(catalog {response.get('catalogEtag', '?')})"
    )
    for change in response.get("changes") or []:
        ref = f"{change.get('kind')}/{change.get('key')}"
        if change.get("action") == "rename":
            ref = f"{change.get('kind')}/{change.get('renamedFrom')} → {ref}"
        line = f"  {marks.get(change.get('action'), '?')} {ref}"
        fields = change.get("fields") or []
        if fields:
            line += ": " + "; ".join(
                str(f.get("path"))
                + (
                    ""
                    if f.get("owner") != "console"
                    else " (edited in the console, "
                    + ("will be overwritten)" if f.get("applies") else "not overwritten)")
                )
                for f in fields
            )
        log(line)
    for process in response.get("processes") or []:
        before = process.get("fromVersion")
        log(
            f"process {process.get('key')}: "
            + (f"v{before} → " if before is not None else "new, ")
            + f"v{process.get('toVersion')}"
        )
        behaviour = process.get("behaviour")
        if behaviour:
            diverged = behaviour.get("instanceIds") or []
            log(
                f"  behaviour (replay): instances {behaviour.get('replayed', 0)}, diverged "
                f"{behaviour.get('diverged', 0)}"
                + (f": {', '.join(str(i) for i in diverged)}" if diverged else "")
            )
        for group in process.get("instances") or []:
            blocked = (
                " — migration required (migration_required)"
                if group.get("migrationRequired")
                else ""
            )
            log(
                f"  open instances v{group.get('version')}: {group.get('open', 0)} → {group.get('fate')}{blocked}"
            )
        print_plan_deadlines(process.get("deadlines") or [], log, process.get("deadlinesTotal"))
    for coverage in response.get("regulationCoverage") or []:
        if not coverage.get("found"):
            log(f"regulation {coverage.get('document')}: not in memory")
            continue
        uncovered = coverage.get("uncovered") or []
        log(
            f"regulation {coverage.get('document')}: sections with elements {len(coverage.get('covered') or {})}"
            + (f", without elements: {', '.join(uncovered)}" if uncovered else "")
        )
    errors = core_errors(response)
    for problem in errors:
        log(f"error: {format_core_error(problem)}")
    for problem in core_warnings(response):
        log(f"warning: {format_core_error(problem)}")
    return not errors and not any(
        g.get("migrationRequired")
        for p in response.get("processes") or []
        for g in p.get("instances") or []
    )
