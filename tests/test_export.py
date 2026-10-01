"""Выгрузка процессов и календарей стенда в пакет (S010, FR-008) на поддельном ядре."""

from __future__ import annotations

import argparse
import copy
import difflib
import shutil
from pathlib import Path
from typing import Any

import pytest

from package_sdk import commands, export
from package_sdk.check import check
from package_sdk.model import PackageError, expand_data_ref, load_package, resolve, substitute

PACKAGES = Path(__file__).parent / "fixtures" / "umbrella" / "packages"
ENV = {
    "INVOICE_WORKSPACE_ID": "58227afa-0000-4000-8000-000000000001",
    "ACCOUNTING_ROLE_ID": "8110d540-0000-4000-8000-000000000002",
    "FINANCE_DIRECTOR_ROLE_ID": "8654aebd-0000-4000-8000-000000000003",
    "TENDERS_WORKSPACE_ID": "58227afa-0000-4000-8000-000000000004",
    "TENDERS_COMPANY_INN": "7700000000",
    "TENDERS_ESCALATION_PRINCIPAL_ID": "58227afa-0000-4000-8000-000000000005",
}


class FakeCore:
    """GET /process-definitions/{ref}, GET /calendars/{ref}, POST /packages:plan."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.console: dict[tuple[str, str], list[str]] = {}
        self.plans: list[dict] = []
        self.plan_error: Exception | None = None

    def publish(self, kind: str, key: str, spec: dict[str, Any], version: int = 1) -> None:
        self.objects[(kind, key)] = {"key": key, "version": version, "spec": copy.deepcopy(spec)}

    def call(self, method: str, path: str, body: Any = None, headers: Any = None) -> dict:
        if method == "POST" and path == "/api/v1/packages:plan":
            if self.plan_error is not None:
                raise self.plan_error
            self.plans.append(body)
            return {
                "changes": [
                    {
                        "kind": kind,
                        "key": key,
                        "action": "update",
                        "fields": [
                            {"path": f"/spec/{name}", "owner": "console", "applies": False}
                            for name in names
                        ]
                        + [{"path": "/spec/version", "owner": "package", "applies": True}],
                    }
                    for (kind, key), names in self.console.items()
                ]
            }
        assert method == "GET", (method, path)
        route, _, ref = path.rpartition("/")
        kind = {"/api/v1/process-definitions": "Process", "/api/v1/calendars": "Calendar"}[route]
        key = ref.partition("@")[0]
        if (kind, key) not in self.objects:
            raise RuntimeError(f"GET {path}: HTTP 404: not_found")
        return copy.deepcopy(self.objects[(kind, key)])


def _copy(tmp_path: Path, key: str) -> Path:
    target = tmp_path / "packages" / key
    shutil.copytree(PACKAGES / key, target)
    return target


def _as_core_stores(package_dir: Path, kind: str, key: str) -> dict[str, Any]:
    """spec так, как его хранит ядро: переменные подставлены, data раскрыт."""
    obj = next(o for o in load_package(package_dir).objects if o.kind == kind and o.key == key)
    return substitute(obj.spec, ENV)


def _export(
    core: FakeCore, package_dir: Path, kind: str, key: str, env: dict[str, str] | None = None
) -> export.Exported:
    body = export.fetch(core, {}, kind, key, None)
    return export.export_object(package_dir, kind, key, body, env=ENV if env is None else env)


@pytest.mark.parametrize(
    ("package", "kind", "key", "file"),
    [
        ("invoice-payment", "Process", "invoice-payment", "processes/invoice-payment.yaml"),
        ("tenders", "Process", "tender", "processes/tender.yaml"),
        ("platform-calendars", "Calendar", "ru", "calendars/ru.yaml"),
    ],
)
def test_unchanged_object_exports_without_diff(
    tmp_path: Path, package: str, kind: str, key: str, file: str
) -> None:
    """Значения установки и раскрытая data: {$ref} на стенде — не повод менять файл."""
    package_dir = _copy(tmp_path, package)
    before = (package_dir / file).read_text(encoding="utf-8")
    core = FakeCore()
    core.publish(kind, key, _as_core_stores(package_dir, kind, key))
    result = _export(core, package_dir, kind, key)
    assert not result.created and not result.changed
    assert (package_dir / file).read_text(encoding="utf-8") == before


def test_tender_data_ref_is_kept(tmp_path: Path) -> None:
    package_dir = _copy(tmp_path, "tenders")
    spec = _as_core_stores(package_dir, "Process", "tender")
    raw = load_package(package_dir)
    obj = next(o for o in raw.objects if o.key == "tender")
    assert isinstance(obj.spec["data"], dict) and "$ref" not in obj.spec["data"]  # раскрыт
    core = FakeCore()
    core.publish("Process", "tender", {**spec, "displayName": "Тендер (правка консоли)"})
    result = _export(core, package_dir, "Process", "tender")
    assert result.changed
    text = (package_dir / "processes" / "tender.yaml").read_text(encoding="utf-8")
    assert "$ref" in text and "Тендер (правка консоли)" in text


def test_console_edit_changes_only_its_lines_and_keeps_comments(tmp_path: Path) -> None:
    package_dir = _copy(tmp_path, "invoice-payment")
    path = package_dir / "processes" / "invoice-payment.yaml"
    before = path.read_text(encoding="utf-8")
    spec = _as_core_stores(package_dir, "Process", "invoice-payment")
    spec["displayName"] = "Оплата счёта (правка консоли)"
    spec["version"] = 3
    core = FakeCore()
    core.publish("Process", "invoice-payment", spec, version=3)

    result = _export(core, package_dir, "Process", "invoice-payment")
    after = path.read_text(encoding="utf-8")
    assert result.changed and result.version == 3
    changed = [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    assert changed == [
        "-  version: 2",
        "-  displayName: Оплата счёта поставщика",
        "+  version: 3",
        "+  displayName: Оплата счёта (правка консоли)",
    ]
    assert "${INVOICE_WORKSPACE_ID}" in after  # значение установки в файл не попало
    assert ENV["INVOICE_WORKSPACE_ID"] not in after
    assert after.count("\n#") == before.count("\n#")  # комментарии на месте

    # повторная выгрузка — без изменений
    assert not _export(core, package_dir, "Process", "invoice-payment").changed
    assert path.read_text(encoding="utf-8") == after


def test_variable_kept_only_when_the_value_comes_from_it(tmp_path: Path) -> None:
    package_dir = _copy(tmp_path, "invoice-payment")
    spec = _as_core_stores(package_dir, "Process", "invoice-payment")
    spec["workspaceId"] = "00000000-0000-4000-8000-00000000abcd"  # другой workspace, не шаблон
    core = FakeCore()
    core.publish("Process", "invoice-payment", spec)
    _export(core, package_dir, "Process", "invoice-payment")
    text = (package_dir / "processes" / "invoice-payment.yaml").read_text(encoding="utf-8")
    # ${…} в строке-шаблоне подходит под любое значение — стоит только пустой литерал вокруг
    assert "workspaceId: ${INVOICE_WORKSPACE_ID}" in text


def test_new_value_equal_to_a_variable_becomes_the_variable(tmp_path: Path) -> None:
    package_dir = _copy(tmp_path, "invoice-payment")
    spec = _as_core_stores(package_dir, "Process", "invoice-payment")
    spec["description"] = ENV["INVOICE_WORKSPACE_ID"]
    spec["displayName"] = "ru"  # короткое значение не угадывается как переменная
    core = FakeCore()
    core.publish("Process", "invoice-payment", spec)
    _export(core, package_dir, "Process", "invoice-payment")
    process = next(o for o in load_package(package_dir).objects if o.key == "invoice-payment")
    assert process.spec["description"] == "${INVOICE_WORKSPACE_ID}"
    assert process.spec["displayName"] == "ru"


def test_reconcile_keeps_templates_and_takes_other_values() -> None:
    local = {"a": "${X}", "b": "role:${R}:x", "c": ["${Y}", "keep"], "d": "old"}
    server = {"a": "1", "b": "role:7:y", "c": ["2", "keep"], "d": "new", "e": 5}
    assert export.reconcile(local, server) == {
        "a": "${X}",
        "b": "role:7:y",  # суффикс не совпал — берётся значение стенда
        "c": ["${Y}", "keep"],
        "d": "new",
        "e": 5,
    }


def test_new_process_becomes_a_file_that_passes_check(tmp_path: Path) -> None:
    package_dir = _copy(tmp_path, "invoice-payment")
    _copy(tmp_path, "notify")  # requires пакета
    spec = _as_core_stores(package_dir, "Process", "invoice-payment")
    spec["displayName"] = "Копия оплаты"
    core = FakeCore()
    core.publish("Process", "invoice-copy", spec)
    with pytest.raises(PackageError, match="задайте их переменные"):
        _export(core, package_dir, "Process", "invoice-copy", env={})  # без значений установки
    assert not (package_dir / "processes" / "invoice-copy.yaml").exists()
    result = _export(core, package_dir, "Process", "invoice-copy")
    assert result.created and result.path == package_dir / "processes" / "invoice-copy.yaml"
    text = result.path.read_text(encoding="utf-8")
    assert text.startswith("# yaml-language-server: $schema=")
    assert "workspaceId: ${INVOICE_WORKSPACE_ID}" in text  # значение стенда → переменная
    assert ENV["INVOICE_WORKSPACE_ID"] not in text
    installation = resolve(["invoice-payment"], packages_dir=package_dir.parent)
    assert {o.key for o in installation.objects if o.kind == "Process"} == {
        "invoice-payment",
        "invoice-copy",
    }
    errors, _warnings = check(installation, env=ENV)
    assert errors == []


def test_calendar_edit_roundtrip(tmp_path: Path) -> None:
    package_dir = _copy(tmp_path, "platform-calendars")
    spec = _as_core_stores(package_dir, "Calendar", "ru")
    spec["displayName"] = "Производственный календарь РФ (стенд)"
    core = FakeCore()
    core.publish("Calendar", "ru", spec, version=4)
    assert _export(core, package_dir, "Calendar", "ru").changed
    assert not _export(core, package_dir, "Calendar", "ru").changed
    assert load_package(package_dir).objects[0].spec["displayName"].endswith("(стенд)")


def test_unknown_object_is_a_clear_error() -> None:
    with pytest.raises(PackageError, match="Process/missing не найден"):
        export.fetch(FakeCore(), {}, "Process", "missing", None)


def test_other_kinds_go_their_own_way(tmp_path: Path) -> None:
    with pytest.raises(PackageError, match="только Process и Calendar"):
        export.export_object(_copy(tmp_path, "notify"), "TaskType", "x", {"spec": {}})


def _args(package_dir: Path, kind: str, key: str) -> argparse.Namespace:
    return argparse.Namespace(package=package_dir, kind=kind, key=[key], version=None)


def test_console_fields_are_marked_from_the_core_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package_dir = _copy(tmp_path, "invoice-payment")
    spec = _as_core_stores(package_dir, "Process", "invoice-payment")
    spec["displayName"] = "Правка консоли"
    core = FakeCore()
    core.publish("Process", "invoice-payment", spec)
    core.console[("Process", "invoice-payment")] = ["displayName"]
    env = {**ENV, "NOTIFICATION_SERVICE_URL": "https://notify.example", "TASK_URL_BASE": "x"}
    assert (
        commands._export_planned(_args(package_dir, "Process", "invoice-payment"), core, {}, env)
        == 0
    )  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert "обновлён" in out and "правлено в консоли: /spec/displayName" in out
    # план строился по пакету до выгрузки: файлы пакета, а не стенда
    (plan,) = core.plans
    files = {f["path"] for f in plan["package"]["files"]}
    assert "processes/invoice-payment.yaml" in files


def test_export_works_when_the_plan_cannot_be_built(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package_dir = _copy(tmp_path, "platform-calendars")
    core = FakeCore()
    core.publish("Calendar", "ru", _as_core_stores(package_dir, "Calendar", "ru"))
    core.plan_error = RuntimeError("POST /api/v1/packages:plan: HTTP 403: forbidden")
    assert commands._export_planned(_args(package_dir, "Calendar", "ru"), core, {}, {}) == 0  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert "план ядра не построен" in out and "без изменений" in out


def test_expand_data_ref_is_what_the_core_would_store(tmp_path: Path) -> None:
    """Фикстура теста: раскрытие data совпадает с тем, что делает загрузка пакета."""
    package_dir = _copy(tmp_path, "tenders")
    import yaml

    raw = yaml.safe_load((package_dir / "processes" / "tender.yaml").read_text(encoding="utf-8"))
    expanded = expand_data_ref(raw["spec"], package_dir / "processes" / "tender.yaml", package_dir)
    obj = next(o for o in load_package(package_dir).objects if o.key == "tender")
    assert expanded["data"] == obj.spec["data"]


def _changed_lines(before: str, after: str) -> list[str]:
    return [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]


def test_added_stage_changes_only_its_own_lines(tmp_path: Path) -> None:
    """Стадия, добавленная на стенде, вставляется на место: остальной файл, его
    комментарии и flow-стиль не трогаются (FR-007)."""
    package_dir = _copy(tmp_path, "invoice-payment")
    path = package_dir / "processes" / "invoice-payment.yaml"
    before = path.read_text(encoding="utf-8")
    spec = _as_core_stores(package_dir, "Process", "invoice-payment")
    extra = {"id": "archive", "steps": [{"id": "archive-close", "complete": {"outcome": "paid"}}]}
    spec["stages"] = [*spec["stages"], extra]
    core = FakeCore()
    core.publish("Process", "invoice-payment", spec)
    assert _export(core, package_dir, "Process", "invoice-payment").changed
    after = path.read_text(encoding="utf-8")
    changed = _changed_lines(before, after)
    assert changed and all(line.startswith("+") for line in changed), changed
    assert len(changed) <= 6
    assert after.count("#") == before.count("#")
    process = next(o for o in load_package(package_dir).objects if o.key == "invoice-payment")
    assert process.spec["stages"][-1]["id"] == "archive"
    assert not _export(core, package_dir, "Process", "invoice-payment").changed


def test_removed_stage_removes_only_its_lines(tmp_path: Path) -> None:
    package_dir = _copy(tmp_path, "invoice-payment")
    path = package_dir / "processes" / "invoice-payment.yaml"
    before = path.read_text(encoding="utf-8")
    spec = _as_core_stores(package_dir, "Process", "invoice-payment")
    removed = spec["stages"].pop()
    core = FakeCore()
    core.publish("Process", "invoice-payment", spec)
    _export(core, package_dir, "Process", "invoice-payment")
    changed = _changed_lines(before, path.read_text(encoding="utf-8"))
    assert changed and all(line.startswith("-") for line in changed), changed
    assert any(removed["id"] in line for line in changed)


def test_added_holiday_keeps_the_calendar_valid(tmp_path: Path) -> None:
    package_dir = _copy(tmp_path, "platform-calendars")
    path = package_dir / "calendars" / "ru.yaml"
    before = path.read_text(encoding="utf-8")
    spec = _as_core_stores(package_dir, "Calendar", "ru")
    year = spec["years"][0]
    year["holidays"] = sorted({*year.get("holidays", []), f"{year['year']}-12-30"})
    core = FakeCore()
    core.publish("Calendar", "ru", spec)
    assert _export(core, package_dir, "Calendar", "ru").changed
    calendar = load_package(package_dir).objects[0]
    assert f"{year['year']}-12-30" in calendar.spec["years"][0]["holidays"]
    assert len(_changed_lines(before, path.read_text(encoding="utf-8"))) <= 3


def test_replaced_data_ref_is_a_warning(tmp_path: Path) -> None:
    package_dir = _copy(tmp_path, "tenders")
    spec = _as_core_stores(package_dir, "Process", "tender")
    spec["data"] = {**spec["data"], "description": "правка схемы на стенде"}
    core = FakeCore()
    core.publish("Process", "tender", spec)
    result = _export(core, package_dir, "Process", "tender")
    assert result.warnings and "ссылка заменена" in result.warnings[0]
    assert "предупреждение: data:" in result.line()


def test_substitutions_are_reported(tmp_path: Path) -> None:
    package_dir = _copy(tmp_path, "invoice-payment")
    spec = _as_core_stores(package_dir, "Process", "invoice-payment")
    spec["description"] = f"see {ENV['ACCOUNTING_ROLE_ID']}"
    core = FakeCore()
    core.publish("Process", "invoice-payment", spec)
    result = _export(core, package_dir, "Process", "invoice-payment")
    assert result.substituted == ["ACCOUNTING_ROLE_ID"]
    assert "значения стенда → ${ACCOUNTING_ROLE_ID}" in result.line()


def _calendar_export(tmp_path: Path, mutate: Any) -> tuple[str, str, dict[str, Any]]:
    package_dir = _copy(tmp_path, "platform-calendars")
    path = package_dir / "calendars" / "ru.yaml"
    before = path.read_text(encoding="utf-8")
    spec = _as_core_stores(package_dir, "Calendar", "ru")
    mutate(spec)
    core = FakeCore()
    core.publish("Calendar", "ru", spec)
    _export(core, package_dir, "Calendar", "ru")
    assert load_package(package_dir).objects[0].spec == spec
    assert not _export(core, package_dir, "Calendar", "ru").changed
    return before, path.read_text(encoding="utf-8"), spec


def test_removing_the_last_short_day_keeps_the_next_year_header(tmp_path: Path) -> None:
    """Заголовок «# 2026: …» прикреплён к последнему shortDay 2025 — он остаётся."""
    before, after, _spec = _calendar_export(
        tmp_path, lambda spec: spec["years"][0]["shortDays"].pop()
    )
    assert _changed_lines(before, after) == ['-        - "2025-11-01"']
    assert "# 2026: 247 рабочих дней" in after


def test_removing_a_year_keeps_the_following_year_header(tmp_path: Path) -> None:
    before, after, _spec = _calendar_export(tmp_path, lambda spec: spec["years"].pop(1))
    changed = _changed_lines(before, after)
    assert changed and all(line.startswith("-") for line in changed)
    assert any("# 2026:" in line for line in changed)  # заголовок удалённого года — с ним
    assert "# 2027: 247 рабочих дней" in after
    lines = after.splitlines()
    header = next(i for i, line in enumerate(lines) if "# 2027:" in line)
    assert lines[header - 1] == "" and "2025-11-01" in lines[header - 2]


def test_short_day_added_at_the_end_stays_before_the_next_header(tmp_path: Path) -> None:
    before, after, _spec = _calendar_export(
        tmp_path, lambda spec: spec["years"][0]["shortDays"].append("2025-12-30")
    )
    assert _changed_lines(before, after) == ['+        - "2025-12-30"']
    lines = after.splitlines()
    added = lines.index('        - "2025-12-30"')
    assert lines[added + 1] == "" and "# 2026:" in lines[added + 2]


def test_year_added_at_the_end_takes_the_neighbours_style(tmp_path: Path) -> None:
    year = {"year": 2028, "provisional": True, "holidays": ["2028-01-01"]}
    before, after, _spec = _calendar_export(tmp_path, lambda spec: spec["years"].append(year))
    changed = _changed_lines(before, after)
    assert all(line.startswith("+") for line in changed)
    assert '+        - "2028-01-01"' in changed  # двойные кавычки, как у соседних дат


def test_removing_a_middle_stage_keeps_the_comments_of_its_neighbours(tmp_path: Path) -> None:
    package_dir = _copy(tmp_path, "invoice-payment")
    path = package_dir / "processes" / "invoice-payment.yaml"
    before = path.read_text(encoding="utf-8")
    spec = _as_core_stores(package_dir, "Process", "invoice-payment")
    removed = spec["stages"].pop(len(spec["stages"]) // 2)
    core = FakeCore()
    core.publish("Process", "invoice-payment", spec)
    _export(core, package_dir, "Process", "invoice-payment")
    after = path.read_text(encoding="utf-8")
    changed = _changed_lines(before, after)
    assert changed and all(line.startswith("-") for line in changed)
    assert any(removed["id"] in line for line in changed)
    # комментарии, кроме стоявших внутри и перед удалённой стадией, на месте
    kept = [line for line in before.splitlines() if line.lstrip().startswith("#")]
    gone = [line[1:] for line in changed if line[1:].lstrip().startswith("#")]
    assert sorted(line for line in after.splitlines() if line.lstrip().startswith("#")) == sorted(
        line for line in kept if line not in gone
    )
