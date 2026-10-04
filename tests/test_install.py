"""Источники, lock и единый план установки на поддельном ядре (S013, TAI-ADR-0062 п.6–7).

Приёмка: повторный план после применения пуст (SC-002); изменение стенда между планом и
применением — plan_stale до первой записи; подмена содержимого под тегом ловится;
несовместимая версия ядра отвергается до записи."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import shutil
import subprocess
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from package_sdk import cli, install, model
from package_sdk.apply import HttpError
from package_sdk.core import CORE_PLANNED_KINDS
from package_sdk.install.lock import GitCache
from package_sdk.install.plan import Target, count_changes, document_hash
from package_sdk.model import PackageError
from tests.umbrella.test_cp_packages import FakeControlPlane, FakeNotificationService

FIXTURES = Path(__file__).parent / "fixtures" / "manifest"
SERVER = "https://platform.example.com"
WORKSPACE = "11111111-2222-4333-8444-555555555555"
ENV = {"CLAIMS_WORKSPACE_ID": WORKSPACE, "HELPDESK_URL": "https://helpdesk.example.com/api"}
COMPANY = {"name": "company", "version": 1, "kinds": [{"kind": "customer"}]}
CLAIMS = {
    "name": "claims",
    "version": 1,
    "extends": ["company@1"],
    "kinds": [{"kind": "case"}, {"kind": "claim_outcome"}],
    "relations": [{"relation": "filed_by"}, {"relation": "resolved_by"}],
}
RULE = {
    "description": "Назначенному — запрос решения",
    "on": {"type": "approval.requested"},
    "recipient": {"kind": "assigned"},
    "notification": {"type": "approval.requested", "title": "Нужно решение: {{task.title}}"},
    "dedupKeyTemplate": "approval:{{event.entityId}}",
    "close": {"on": ["approval.approved", "approval.rejected"]},
}


def _load(text: str) -> Any:
    """YAML 1.2, как у ядра: ключ on — строка, а не True."""
    return yaml.load(text, Loader=model._yaml12_loader())


def _digest(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


class FakeCore(FakeControlPlane):
    """Каталог установщика (FakeControlPlane) плюс то, что план берёт у ядра: версия в
    openapi.json, /packages:plan и :apply с planHash и 409 plan_stale, :retire процессов и
    календарей с dryRun, онтологии и их наборы, чтение пространств работы.

    Правки консоли (``console``: «Kind/key» → {поле: значение, которое поставил человек}) план
    отдаёт полями ``owner: console`` с ``applies`` = ``overwriteConsole`` запроса, как ядро
    (``diff_fields``); флаг входит в planHash; применение с флагом их перезаписывает, без
    флага — сохраняет."""

    def __init__(self) -> None:
        super().__init__()
        assert self.openapi is not None
        self.openapi["info"] = {"title": "control-plane", "version": "0.9.0"}
        self.published: dict[str, str] = {}  # "Kind/key" → хэш тела, что поставил план ядра
        self.plan_calls: list[dict[str, Any]] = []
        self.apply_calls: list[dict[str, Any]] = []
        self.packs: dict[str, dict[str, Any]] = {}
        self.enabled: dict[str, dict[str, Any]] = {}
        self.workspaces = {WORKSPACE}
        self.processes: dict[str, dict[str, Any]] = {}
        self.calendars: dict[str, dict[str, Any]] = {}
        self.core_writes: list[tuple[str, str]] = []
        self.console: dict[str, dict[str, Any]] = {}

    # -- план ядра --

    @staticmethod
    def _specs(files: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
        specs = {}
        for item in files:
            document = _load(item["content"])
            if isinstance(document, dict) and document.get("kind") in CORE_PLANNED_KINDS:
                specs[f"{document['kind']}/{document['key']}"] = document.get("spec") or {}
        return specs

    def _planned(self, files: list[dict[str, str]]) -> list[tuple[str, str, str]]:
        found = []
        for item in files:
            document = _load(item["content"])
            if isinstance(document, dict) and document.get("kind") in CORE_PLANNED_KINDS:
                found.append((document["kind"], document["key"], _digest(document.get("spec"))))
        return sorted(found)

    def _plan(self, body: dict[str, Any]) -> dict[str, Any]:
        files = body["package"]["files"]
        manifest = next(_load(f["content"]) for f in files if f["path"] == "package.yaml")
        planned = self._planned(files)
        specs = self._specs(files)
        overwrite = body.get("overwriteConsole") is True

        def action(ref: str, spec: str) -> str:
            if ref not in self.published:
                return "create"
            return (
                "unchanged"
                if self.published[ref] == spec and not self.console.get(ref)
                else "update"
            )

        changes = [
            {
                "kind": kind,
                "key": key,
                "action": action(f"{kind}/{key}", spec),
                "renamedFrom": None,
                "fields": [
                    {
                        "path": path,
                        "before": value,
                        "after": specs[f"{kind}/{key}"].get(path),
                        "owner": "console",
                        "applies": overwrite,
                    }
                    for path, value in sorted(self.console.get(f"{kind}/{key}", {}).items())
                ],
                "deprecates": [],
            }
            for kind, key, spec in planned
        ]
        state = {f"{k}/{key}": self.published.get(f"{k}/{key}") for k, key, _s in planned}
        console = {ref: self.console[ref] for ref in state if ref in self.console}
        hashed: dict[str, Any] = {"files": files, "state": state}
        if console or overwrite:
            hashed.update(console=console, overwriteConsole=overwrite)
        return {
            "planHash": _digest(hashed),
            "catalogEtag": _digest(state),
            "package": {"key": manifest["key"], "version": str(manifest["spec"]["version"])},
            "changes": changes,
            "outside": [],
            "processes": [],
            "regulationCoverage": [],
            "problems": [],
            "createdAt": "2026-09-30T10:00:00Z",
        }

    def _packages(self, route: str, body: dict[str, Any]) -> dict[str, Any]:
        if route == "/packages:plan":
            self.plan_calls.append(copy.deepcopy(body))
            return self._plan(body)
        self.apply_calls.append(copy.deepcopy(body))
        current = self._plan(body)
        if body["planHash"] != current["planHash"]:
            raise HttpError(
                "POST /api/v1/packages:apply: HTTP 409",
                409,
                {"error": {"code": "plan_stale", "message": "stale"}},
            )
        applied = []
        for kind, key, spec in self._planned(body["package"]["files"]):
            self.published[f"{kind}/{key}"] = spec
            if body.get("overwriteConsole") is True:
                self.console.pop(f"{kind}/{key}", None)
            if kind == "Process":
                self.processes.setdefault(key, {"key": key, "status": "active", "open": 0})
            applied.append({"kind": kind, "key": key, "action": "create", "version": 1})
        return {"planHash": body["planHash"], "catalogEtag": "x", "applied": applied}

    # -- :retire процессов и календарей --

    def _retire(self, method: str, route: str, query: dict[str, str]) -> dict[str, Any]:
        collection, _, rest = route.strip("/").partition("/")
        key, _, verb = urllib.parse.unquote(rest).partition(":")
        rows = self.processes if collection == "process-definitions" else self.calendars
        row = rows.get(key)
        if row is None:
            raise HttpError(f"{method} {route}: HTTP 404", 404, {"error": {"code": "not_found"}})
        if method == "GET":
            return {"key": key, "status": row["status"]}
        assert verb == "retire"
        dry = query.get("dryRun") == "true"
        if collection == "calendars":
            users = [
                p
                for p in self.processes.values()
                if key in p.get("calendars", []) and (p["status"] == "active" or p["open"])
            ]
            if users:
                raise HttpError(
                    f"POST {route}: HTTP 409",
                    409,
                    {
                        "error": {
                            "code": "calendar_in_use",
                            "details": {
                                "calendar": key,
                                "processes": [
                                    {"key": p["key"], "version": 1, "openInstances": p["open"]}
                                    for p in users
                                ],
                                "total": len(users),
                            },
                        }
                    },
                )
        if not dry:
            self.core_writes.append(("POST", route))
            row["status"] = "retired"
        retired = {"at": "2026-09-30T10:00:00Z", "by": WORKSPACE, "reason": "r"}
        answer: dict[str, Any] = {"key": key, "status": "retired", "retired": retired}
        if collection == "process-definitions":
            answer["openInstances"] = row["open"]
            answer["byVersion"] = (
                [{"version": 1, "openInstances": row["open"]}] if row["open"] else []
            )
        return answer

    def call(self, method, path, body=None, headers=None):  # type: ignore[no-untyped-def]
        if path.startswith("/api/v1/"):
            parsed = urllib.parse.urlparse(path[len("/api/v1") :])
            route, query = parsed.path, dict(urllib.parse.parse_qsl(parsed.query))
            if route in ("/packages:plan", "/packages:apply"):
                assert "Idempotency-Key" in (headers or {})
                return self._packages(route, body)
            if route.startswith(("/process-definitions/", "/calendars/")):
                return self._retire(method, route, query)
            if route == "/knowledge/packs" and method == "POST":
                self.core_writes.append((method, route))
                self.packs[f"{body['name']}@{body['version']}"] = copy.deepcopy(body)
                return {"name": body["name"], "version": str(body["version"])}
            if route.startswith("/knowledge/packs/"):
                ref = urllib.parse.unquote(route.rsplit("/", 1)[1])
                if ref not in self.packs:
                    raise HttpError(f"GET {route}: HTTP 404", 404, {"error": {"code": "not_found"}})
                return copy.deepcopy(self.packs[ref])
            if route.endswith("/knowledge-packs"):
                workspace = route.split("/")[2]
                if method == "PUT":
                    self.core_writes.append((method, route))
                    self.enabled[workspace] = copy.deepcopy(body)
                    return {"settings": body}
                current = self.enabled.get(workspace)
                return {
                    "workspaceId": workspace,
                    "rootWorkspaceId": workspace,
                    "configured": current is not None,
                    "packs": (current or {}).get("packs", []),
                    "strict": (current or {}).get("strict", False),
                    "effective": [],
                }
            if route.startswith("/workspaces/") and method == "GET":
                workspace = route.split("/")[2]
                if workspace not in self.workspaces:
                    raise HttpError(f"GET {route}: HTTP 404", 404, {"error": {"code": "not_found"}})
                return {"id": workspace}
        return super().call(method, path, body, headers)


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), "utf-8")


def _object(kind: str, key: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {"apiVersion": "taimen.ai/v1", "kind": kind, "key": key, "spec": spec}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(model, "ROOT", tmp_path)
    monkeypatch.setattr(model, "PACKAGES_DIR", tmp_path / "packages")
    monkeypatch.setenv("PACKAGE_SDK_CACHE", str(tmp_path / "cache"))
    packages = tmp_path / "packages"
    shutil.copytree(FIXTURES, packages)
    _write(
        packages / "acme-base" / "knowledge-packs" / "company.yaml",
        _object("KnowledgePack", "company", COMPANY),
    )
    _write(
        packages / "acme-claims" / "knowledge-packs" / "claims.yaml",
        _object("KnowledgePack", "claims", CLAIMS),
    )
    _write(
        packages / "acme-base" / "notification-rules" / "approval-requested.yaml",
        _object("NotificationRule", "approval-requested", RULE),
    )
    _write(
        tmp_path / "packages.yaml",
        {
            "apiVersion": "taimen.ai/v1",
            "kind": "Installation",
            "key": "acme-prod",
            "spec": {
                "packages": ["acme-claims"],
                "knowledge": [
                    {"workspace": "${CLAIMS_WORKSPACE_ID}", "packs": ["company@1", "claims@1"]}
                ],
                "retire": {
                    "TaskType": ["legacy-claim"],
                    "Process": ["claim-v0", "claim-draft"],
                    "Calendar": ["old-calendar"],
                },
            },
        },
    )
    return tmp_path


@pytest.fixture
def core() -> FakeCore:
    fake = FakeCore()
    fake.rows["task-types"].append(
        {"id": "tt-legacy", "key": "legacy-claim", "version": 1, "status": "active"}
    )
    fake.processes["claim-v0"] = {"key": "claim-v0", "status": "active", "open": 2}
    # календарь нужен только черновику, который план тоже выводит: освободится после него
    fake.processes["claim-draft"] = {
        "key": "claim-draft",
        "status": "active",
        "open": 0,
        "calendars": ["old-calendar"],
    }
    fake.calendars["old-calendar"] = {"key": "old-calendar", "status": "active"}
    return fake


@pytest.fixture
def notify() -> FakeNotificationService:
    return FakeNotificationService()


def _target(core: FakeCore, notify: FakeNotificationService | None = None) -> Target:
    return Target(
        server=SERVER,
        http=core,
        headers={"Authorization": "Bearer t"},
        notify=(notify, {"Authorization": "Bearer n"}) if notify is not None else None,
    )


def _plan(
    project: Path, core: FakeCore, notify: FakeNotificationService, **kwargs: Any
) -> dict[str, Any]:
    lines: list[str] = []
    document = install.plan(
        project / "packages.yaml",
        target=_target(core, notify),
        env=kwargs.pop("env", ENV),
        out=kwargs.pop("out", project / "plan.json"),
        log=lines.append,
        **kwargs,
    )
    document["_lines"] = lines  # для проверок вывода; в файл не попадает
    return document


def _apply(
    project: Path, core: FakeCore, notify: FakeNotificationService, **kwargs: Any
) -> dict[str, Any]:
    kwargs.setdefault("assume_yes", True)
    return install.apply(
        project / "plan.json",
        target=_target(core, notify),
        env=kwargs.pop("env", ENV),
        log=kwargs.pop("log", lambda _m: None),
        **kwargs,
    )


# --- план --------------------------------------------------------------------------------


def test_plan_is_one_document_of_every_kind(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    _plan(project, core, notify)
    saved = json.loads((project / "plan.json").read_text(encoding="utf-8"))
    assert install.read_plan(project / "plan.json") == saved
    assert [s["kind"] for s in saved["sections"]] == [
        "catalog",
        "core",
        "knowledge",
        "notification-rules",
        "retire",
    ]
    assert saved["engines"] == {"control-plane": "0.9.0"}
    assert saved["installation"] == "acme-prod" and saved["install"] == "packages.yaml"
    assert saved["planHash"] == document_hash(saved)
    catalog, core_section, knowledge, rules, retire = saved["sections"]
    # роль — установщику; типы задач, агенты и процессы пакета с процессом — ядру
    assert [(c["kind"], c["key"], c["operation"]) for c in catalog["changes"]] == [
        ("Role", "claims-officer", "create")
    ]
    assert core_section["package"] == "acme-claims"
    assert {c["kind"] for c in core_section["plan"]["changes"]} == {"TaskType", "Agent", "Process"}
    assert [c["key"] for c in knowledge["register"]] == ["company", "claims"]
    assert knowledge["enable"][0]["packs"] == ["company@1", "claims@1"]
    assert [(c["key"], c["operation"]) for c in rules["changes"]] == [
        ("approval-requested", "create")
    ]
    assert [(i["kind"], i["key"], i["operation"]) for i in retire["items"]] == [
        ("TaskType", "legacy-claim", "deprecate"),
        ("Process", "claim-v0", "retire"),
        ("Process", "claim-draft", "retire"),
        ("Calendar", "old-calendar", "retire"),
    ]
    assert retire["items"][1]["openInstances"] == 2
    assert retire["items"][3]["after"] == ["Process/claim-draft"]
    # значения переменных в план не пишутся — только их хэш (пространство работы включения
    # онтологий — топология, его план называет)
    assert "helpdesk.example.com" not in (project / "plan.json").read_text(encoding="utf-8")
    # план ничего не пишет
    assert core.writes == [] and core.core_writes == [] and notify.writes == []
    assert core.apply_calls == []


def test_repeated_plan_after_apply_is_empty(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    first = _plan(project, core, notify)
    assert count_changes(first) > 0
    _apply(project, core, notify)
    assert core.processes["claim-v0"]["status"] == "retired"
    assert core.calendars["old-calendar"]["status"] == "retired"
    assert core.enabled[WORKSPACE] == {"packs": ["company@1", "claims@1"], "strict": False}
    assert set(core.published) == {
        "Agent/helpdesk-observer",
        "Process/claim",
        "TaskType/claim-review",
    }
    again = _plan(project, core, notify)
    assert count_changes(again) == 0
    assert again["_lines"][-1] == "no changes"


def test_stand_changed_between_plan_and_apply_is_plan_stale_before_any_write(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    _plan(project, core, notify)
    # кто-то поставил тип задачи пакета, пока план ждал применения: план ядра другой
    core.published["TaskType/claim-review"] = "sha256:other"
    with pytest.raises(PackageError, match="plan_stale"):
        _apply(project, core, notify)
    assert core.writes == [] and core.core_writes == [] and notify.writes == []
    assert [b["planHash"] for b in core.apply_calls] == []


def test_catalog_changed_between_plan_and_apply_is_plan_stale(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    _plan(project, core, notify)
    core.rows["roles"].append(
        {
            "id": "r1",
            "slug": "claims-officer",
            "name": "Someone else",
            "description": "",
            "version": 3,
            "workspaceId": None,
        }
    )
    with pytest.raises(PackageError, match=r"plan_stale.*catalog.*Role/claims-officer"):
        _apply(project, core, notify)
    assert core.writes == [] and core.core_writes == []


def test_core_refuses_a_plan_that_went_stale_during_the_apply(
    project: Path, core: FakeCore, notify: FakeNotificationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Секцию core сверяет само ядро: 409 plan_stale — понятный отказ, а не трассировка."""
    _plan(project, core, notify)
    original = core._packages

    def racing(route: str, body: dict[str, Any]) -> dict[str, Any]:
        if route == "/packages:apply":
            core.published["Process/claim"] = "sha256:raced"
        return original(route, body)

    monkeypatch.setattr(core, "_packages", racing)
    with pytest.raises(PackageError, match="plan is stale"):
        _apply(project, core, notify)


def test_edited_plan_file_is_refused(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    _plan(project, core, notify)
    path = project / "plan.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["sections"][0]["changes"] = []
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(PackageError, match="planHash does not match"):
        _apply(project, core, notify)
    assert core.writes == [] and core.apply_calls == []


# --- правки консоли (overwriteConsole) ------------------------------------------------


def _console_edit(core: FakeCore) -> None:
    """Пакет уже применён, после чего человек поправил в консоли название типа задачи."""
    for kind, key in (
        ("TaskType", "claim-review"),
        ("Agent", "helpdesk-observer"),
        ("Process", "claim"),
    ):
        core.published[f"{kind}/{key}"] = "applied"
    core.console["TaskType/claim-review"] = {"displayName": "Claim review (console)"}


def _yes(asked: list[str]) -> Callable[[str], bool]:
    """Человек отвечает «да»; вопросы копятся в asked."""

    def confirm(question: str) -> bool:
        asked.append(question)
        return True

    return confirm


def test_without_the_flag_console_edits_are_kept(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    _console_edit(core)
    document = _plan(project, core, notify)
    assert document["overwriteConsole"] is False
    assert all("overwriteConsole" not in body for body in core.plan_calls)
    assert install.console_edits(document) == [
        install.ConsoleEdit("acme-claims", "TaskType", "claim-review", (), ("displayName",))
    ]
    lines = document["_lines"]
    assert "console edits: kept (to overwrite — plan --overwrite-console)" in lines
    assert "  = TaskType/claim-review (acme-claims): displayName" in lines
    assert "  will be overwritten:" not in lines
    asked: list[str] = []
    _apply(project, core, notify, assume_yes=False, confirm=_yes(asked))
    assert asked == [f"Apply plan {document['planHash']}?"]
    assert [b.get("overwriteConsole") for b in core.apply_calls] == [None]
    assert core.console == {"TaskType/claim-review": {"displayName": "Claim review (console)"}}


def test_with_the_flag_console_edits_are_overwritten(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    _console_edit(core)
    kept = _plan(project, core, notify, out=project / "kept.json")
    document = _plan(project, core, notify, overwrite_console=True)
    saved = json.loads((project / "plan.json").read_text(encoding="utf-8"))
    assert saved["overwriteConsole"] is True and saved["planHash"] == document_hash(saved)
    # флаг — часть плана ядра: у ядра другой planHash; остальное в документе то же
    assert kept["overwriteConsole"] is False
    assert saved["sections"][1]["planHash"] != kept["sections"][1]["planHash"]
    assert [s for i, s in enumerate(saved["sections"]) if i != 1] == [
        s for i, s in enumerate(kept["sections"]) if i != 1
    ]
    assert core.plan_calls[-1]["overwriteConsole"] is True
    assert install.console_edits(document) == [
        install.ConsoleEdit("acme-claims", "TaskType", "claim-review", ("displayName",), ())
    ]
    lines = document["_lines"]
    assert "console edits: overwritten (overwriteConsole)" in lines
    assert "  ! TaskType/claim-review (acme-claims): displayName" in lines
    asked: list[str] = []
    _apply(project, core, notify, assume_yes=False, confirm=_yes(asked))
    assert asked == [
        f"Apply plan {document['planHash']} overwriting console edits (overwriteConsole)?"
    ]
    # план ядра перед записью строится заново с тем же флагом, и с ним же идёт apply
    assert core.plan_calls[-1]["overwriteConsole"] is True
    assert [b.get("overwriteConsole") for b in core.apply_calls] == [True]
    assert core.console == {}
    assert count_changes(_plan(project, core, notify)) == 0


def test_the_flag_is_under_the_plan_hash(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    _console_edit(core)
    _plan(project, core, notify)
    path = project / "plan.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["overwriteConsole"] = True
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(PackageError, match="planHash does not match"):
        _apply(project, core, notify)
    # и с пересчитанным хэшем документа флаг не проходит: план ядра с ним — другой
    document["planHash"] = document_hash(document)
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(PackageError, match=r"plan_stale.*core plan of package acme-claims"):
        _apply(project, core, notify)
    assert core.apply_calls == [] and core.writes == [] and core.core_writes == []
    assert core.console == {"TaskType/claim-review": {"displayName": "Claim review (console)"}}


def test_a_plan_without_the_field_is_applied_as_before(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    """План прежнего формата (без overwriteConsole) — без перезаписи правок консоли."""
    _console_edit(core)
    _plan(project, core, notify)
    path = project / "plan.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["overwriteConsole"]
    document["planHash"] = document_hash(document)
    path.write_text(json.dumps(document), encoding="utf-8")
    _apply(project, core, notify)
    assert [b.get("overwriteConsole") for b in core.apply_calls] == [None]
    assert "TaskType/claim-review" in core.console


def test_changed_package_or_variables_after_plan_are_refused(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    _plan(project, core, notify)
    with pytest.raises(PackageError, match="variablesHash"):
        _apply(project, core, notify, env={**ENV, "HELPDESK_URL": "https://other.example.com"})
    role = project / "packages" / "acme-base" / "roles" / "claims-officer.yaml"
    role.write_text(role.read_text(encoding="utf-8").replace("Claims officer", "Officer"), "utf-8")
    with pytest.raises(PackageError, match="lockHash"):
        _apply(project, core, notify)
    assert core.writes == []


def test_apply_needs_a_human_yes(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    _plan(project, core, notify)
    asked: list[str] = []
    with pytest.raises(PackageError, match="requires confirmation by a human"):
        _apply(project, core, notify, assume_yes=False)
    with pytest.raises(PackageError, match="the plan was not confirmed"):
        _apply(project, core, notify, assume_yes=False, confirm=lambda q: asked.append(q) or False)
    assert asked and "Apply plan sha256:" in asked[0]
    assert core.writes == [] and core.core_writes == []
    lines: list[str] = []
    _apply(project, core, notify, assume_yes=False, confirm=lambda _q: True, log=lines.append)
    assert not any("assume_yes" in line for line in lines)
    assert any(line.startswith("installation plan acme-prod") for line in lines)


def test_bootstrap_path_is_marked_in_the_log(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    _plan(project, core, notify)
    lines: list[str] = []
    _apply(project, core, notify, log=lines.append)
    assert any("without confirmation by a human (assume_yes)" in line for line in lines)


def test_incompatible_core_version_is_refused_before_any_write(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    assert core.openapi is not None
    core.openapi["info"]["version"] = "0.12.0"
    with pytest.raises(PackageError, match=r"engines_incompatible.*<0\.11.*0\.12\.0"):
        _plan(project, core, notify)
    assert core.plan_calls == [] and core.writes == []
    assert not (project / "plan.json").exists()


def test_core_update_after_the_plan_is_plan_stale(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    _plan(project, core, notify)
    assert core.openapi is not None
    core.openapi["info"]["version"] = "0.10.0"
    with pytest.raises(PackageError, match=r"the stand's core was updated: 0.9.0 → 0.10.0"):
        _apply(project, core, notify)


def test_missing_required_variable_stops_the_plan_with_its_description(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    env = {k: v for k, v in ENV.items() if k != "HELPDESK_URL"}
    with pytest.raises(
        PackageError, match="HELPDESK_URL is not set — package acme-claims: Helpdesk API"
    ):
        _plan(project, core, notify, env=env)
    assert core.plan_calls == []


def test_variable_of_a_stand_object_must_exist_there(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    core.workspaces.clear()
    with pytest.raises(
        PackageError,
        match=f"CLAIMS_WORKSPACE_ID of package acme-claims: workspace {WORKSPACE} not found",
    ):
        _plan(project, core, notify)


def test_calendar_still_in_use_after_the_plan_is_refused(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    core.processes["other"] = {
        "key": "other",
        "status": "active",
        "open": 0,
        "calendars": ["old-calendar"],
    }
    with pytest.raises(PackageError, match=r"calendar_in_use.*other v1"):
        _plan(project, core, notify)


def test_rules_need_the_notification_service(project: Path, core: FakeCore) -> None:
    with pytest.raises(PackageError, match="notification service"):
        install.plan(project / "packages.yaml", target=_target(core), env=ENV, log=lambda _m: None)


def test_registered_ontology_with_other_content_needs_a_new_version(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    core.packs["claims@1"] = {**CLAIMS, "kinds": [{"kind": "case"}]}
    with pytest.raises(PackageError, match="bump version"):
        _plan(project, core, notify)


# --- CLI ---------------------------------------------------------------------------------


@pytest.fixture
def cli_core(
    core: FakeCore, notify: FakeNotificationService, monkeypatch: pytest.MonkeyPatch
) -> FakeCore:
    from package_sdk import commands

    monkeypatch.setattr(commands, "Http", lambda base: notify if "notify" in base else core)
    monkeypatch.setattr(commands, "_bearer", lambda _server: "t")
    monkeypatch.setenv("NOTIFY_TOKEN", "n")
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "https://platform.example.com/notify")
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    return core


def test_cli_plan_needs_out_and_apply_only_takes_a_plan(
    project: Path, cli_core: FakeCore, capsys: pytest.CaptureFixture[str]
) -> None:
    install_file = str(project / "packages.yaml")
    with pytest.raises(SystemExit):
        cli.main(["plan", "--install", install_file, "--server", SERVER])
    with pytest.raises(SystemExit):
        cli.main(["apply", "--install", install_file, "--server", SERVER])
    capsys.readouterr()
    plan_file = project / "plan.json"
    args = ["plan", "--install", install_file, "--server", SERVER, "--out", str(plan_file)]
    assert cli.main([*args, "--env", str(project / "none.env")]) == 0
    out = capsys.readouterr().out
    assert "  + Role/claims-officer (acme-base)" in out
    assert "  - Process/claim-v0: new instances do not start, live ones run to completion: 2" in out
    assert f"plan saved: {plan_file}" in out
    # без терминала подтверждения нет — ничего не пишется
    applying = ["apply", "--plan", str(plan_file), "--server", SERVER]
    assert cli.main([*applying, "--env", str(project / "none.env")]) == 1
    assert cli_core.writes == [] and cli_core.core_writes == []
    assert (
        cli.main(["apply", "--plan", str(plan_file), "--server", "https://other.example.com"]) == 1
    )
    assert "the plan was built for" in capsys.readouterr().err


def test_cli_plan_overwrite_console(
    project: Path, cli_core: FakeCore, capsys: pytest.CaptureFixture[str]
) -> None:
    _console_edit(cli_core)
    plan_file = project / "plan.json"
    args = ["plan", "--install", str(project / "packages.yaml"), "--server", SERVER]
    args += ["--out", str(plan_file), "--env", str(project / "none.env")]
    assert cli.main([*args, "--overwrite-console"]) == 0
    out = capsys.readouterr().out
    assert "  ! TaskType/claim-review (acme-claims): displayName" in out
    assert json.loads(plan_file.read_text(encoding="utf-8"))["overwriteConsole"] is True
    assert cli.main(args) == 0
    assert "  = TaskType/claim-review (acme-claims): displayName" in capsys.readouterr().out
    assert json.loads(plan_file.read_text(encoding="utf-8"))["overwriteConsole"] is False


def test_cli_apply_with_a_yes(
    project: Path,
    cli_core: FakeCore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from package_sdk import commands

    plan_file = project / "plan.json"
    none = str(project / "none.env")
    install_file = str(project / "packages.yaml")
    assert (
        cli.main(
            [
                "plan",
                "--install",
                install_file,
                "--server",
                SERVER,
                "--out",
                str(plan_file),
                "--env",
                none,
            ]
        )
        == 0
    )
    monkeypatch.setattr(commands, "_ask", lambda _q: True)
    assert cli.main(["apply", "--plan", str(plan_file), "--server", SERVER, "--env", none]) == 0
    assert "applied plan sha256:" in capsys.readouterr().out
    assert (
        cli.main(
            [
                "plan",
                "--install",
                install_file,
                "--server",
                SERVER,
                "--out",
                str(plan_file),
                "--env",
                none,
            ]
        )
        == 0
    )
    assert "no changes" in capsys.readouterr().out


# --- источники git и lock ----------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Репозиторий пакета «у интегратора»: https-адрес ведёт в локальный каталог."""
    origin = tmp_path / "remote" / "extra"
    package = origin / "pkg"
    _write(
        package / "package.yaml",
        _object("Package", "acme-extra", {"version": "0.1.0", "displayName": "Extra"}),
    )
    _write(
        package / "roles" / "extra-officer.yaml",
        _object("Role", "extra-officer", {"name": "Extra officer"}),
    )
    _git("init", "--quiet", "-b", "main", cwd=origin)
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "add", ".", cwd=origin)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "--quiet",
        "-m",
        "v0.1.0",
        cwd=origin,
    )
    _git("tag", "v0.1.0", cwd=origin)
    # только в тесте: SDK разрешает git лишь https и ssh, а адрес подменён на file://
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{(tmp_path / 'remote').as_uri()}/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://git.example.com/acme/")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "always")
    return origin


@pytest.fixture
def git_project(project: Path, remote: Path) -> Path:
    path = project / "packages.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["spec"]["packages"].append(
        {
            "key": "acme-extra",
            "git": "https://git.example.com/acme/extra",
            "ref": "v0.1.0",
            "path": "pkg",
        }
    )
    _write(path, document)
    return project


def test_git_source_needs_a_lock(
    git_project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    with pytest.raises(PackageError, match=r"lock_required.*acme-extra"):
        _plan(git_project, core, notify)


def test_lock_pins_commit_and_content_and_the_plan_uses_it(
    git_project: Path, remote: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    lines: list[str] = []
    document = install.lock(git_project / "packages.yaml", log=lines.append)
    lock = json.loads((git_project / "packages.lock").read_text(encoding="utf-8"))
    assert lock == document and lock["format"] == "package-sdk.lock/v1"
    assert lock["installation"] == "acme-prod"
    extra = next(e for e in lock["packages"] if e["key"] == "acme-extra")
    assert extra["commit"] == _git("rev-parse", "v0.1.0", cwd=remote)
    assert extra["source"] == {
        "git": "https://git.example.com/acme/extra",
        "ref": "v0.1.0",
        "path": "pkg",
    }
    local = next(e for e in lock["packages"] if e["key"] == "acme-base")
    assert local["source"] == {"path": "packages/acme-base"} and "commit" not in local
    from package_sdk import schema

    assert schema.errors(schema.LOCK, lock) == []
    planned = _plan(git_project, core, notify)
    catalog = planned["sections"][0]["changes"]
    assert ("Role", "extra-officer") in {(c["kind"], c["key"]) for c in catalog}


def test_content_substituted_under_the_tag_is_caught(
    git_project: Path, remote: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    install.lock(git_project / "packages.yaml", log=lambda _m: None)
    role = remote / "pkg" / "roles" / "extra-officer.yaml"
    role.write_text(role.read_text(encoding="utf-8").replace("Extra officer", "Root"), "utf-8")
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "--quiet",
        "-am",
        "swap",
        cwd=remote,
    )
    _git("tag", "-f", "v0.1.0", cwd=remote)
    with pytest.raises(PackageError, match=r"source_ref_moved.*acme-extra"):
        _plan(git_project, core, notify)
    assert core.plan_calls == [] and core.writes == []


def test_cache_is_used_only_while_its_content_matches_the_lock(
    git_project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    install.lock(git_project / "packages.yaml", log=lambda _m: None)
    cache = GitCache()
    checkouts = list(cache.root.glob("*/checkouts/*/*/[0-9a-f]*/roles/extra-officer.yaml"))
    assert len(checkouts) == 1
    checkouts[0].write_text("tampered: true\n", encoding="utf-8")
    planned = _plan(git_project, core, notify)  # выгрузка сверена с lock и выгружена заново
    assert ("Role", "extra-officer") in {
        (c["kind"], c["key"]) for c in planned["sections"][0]["changes"]
    }
    assert "tampered" not in checkouts[0].read_text(encoding="utf-8")

    path = git_project / "packages.lock"
    lock = json.loads(path.read_text(encoding="utf-8"))
    extra = next(e for e in lock["packages"] if e["key"] == "acme-extra")
    extra["contentHash"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(PackageError, match=r"content_mismatch.*acme-extra"):
        _plan(git_project, core, notify)


def test_lock_of_local_packages_is_checked_when_present(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    install.lock(project / "packages.yaml", log=lambda _m: None)
    _plan(project, core, notify)
    manifest = project / "packages" / "acme-base" / "package.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "# правка\n", "utf-8")
    with pytest.raises(PackageError, match=r"content_mismatch.*acme-base"):
        _plan(project, core, notify)


def test_unreachable_source_falls_back_to_a_matching_cache(
    git_project: Path, remote: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    install.lock(git_project / "packages.yaml", log=lambda _m: None)
    shutil.rmtree(remote)
    planned = _plan(git_project, core, notify)
    assert any("is unavailable" in line for line in planned["_lines"])


def test_path_and_git_packages_take_their_key_from_the_manifest(project: Path) -> None:
    elsewhere = project / "vendor" / "some-dir"
    _write(
        elsewhere / "package.yaml",
        _object("Package", "vendored", {"version": "1.0.0", "displayName": "V"}),
    )
    installation = model.resolve(
        [{"key": "vendored", "path": "vendor/some-dir"}], path=project / "x.yaml"
    )
    assert [p.key for p in installation.packages] == ["vendored"]
    with pytest.raises(PackageError, match="does not match the package key in the installation"):
        model.resolve([{"key": "other", "path": "vendor/some-dir"}], path=project / "x.yaml")


def test_layout_is_not_part_of_the_content_hash(project: Path) -> None:
    directory = project / "packages" / "acme-base"
    before = model.install_hash(directory)
    (directory / ".layout").mkdir()
    (directory / ".layout" / "x.json").write_text("{}", encoding="utf-8")
    assert model.install_hash(directory) == before


# --- источники git: что до git не доходит (ревью S013) --------------------------------------


def _installation_with(project: Path, entry: dict[str, Any]) -> Path:
    path = project / "packages.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["spec"]["packages"] = [entry]
    _write(path, document)
    return path


@pytest.mark.parametrize(
    "entry",
    [
        {"git": "{remote}/extra", "ref": "v0.1.0"},  # локальный путь
        {"git": "file://{remote}/extra", "ref": "v0.1.0"},
        {"git": "https://token@git.example.com/acme/extra", "ref": "v0.1.0"},
        {"git": "--upload-pack=touch {pwned}", "ref": "v0.1.0"},
        {"git": "https://git.example.com/acme/extra", "ref": "--output={pwned}"},
        {"git": "https://git.example.com/acme/extra", "ref": "v0.1.0", "path": "{remote}/extra"},
        {"git": "https://git.example.com/acme/extra", "ref": "v0.1.0", "path": "../../outside"},
        {"git": "https://git.example.com/acme/extra", "ref": "v0.1.0", "path": "pkg/../.."},
    ],
)
def test_unsafe_git_source_is_refused_before_git(
    project: Path, remote: Path, entry: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    pwned = project / "pwned"
    values = {k: v.format(remote=remote.parent, pwned=pwned) for k, v in entry.items()}
    path = _installation_with(project, {"key": "acme-extra", **values})
    called: list[Any] = []
    monkeypatch.setattr(GitCache, "_run", lambda *a, **k: called.append(a))
    with pytest.raises(PackageError):
        install.lock(path, log=lambda _m: None)
    assert called == [] and not pwned.exists()
    assert not (project / "packages.lock").exists()


def test_tag_only_refs_and_sha_commits(git_project: Path, remote: Path) -> None:
    """ref — только тег: ветка main тегом не считается, даже если есть в источнике."""
    path = git_project / "packages.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["spec"]["packages"][-1]["ref"] = "main"
    _write(path, document)
    with pytest.raises(PackageError, match="main"):
        install.lock(path, log=lambda _m: None)


def test_lock_extracts_the_commit_again_instead_of_trusting_the_cache(
    git_project: Path,
) -> None:
    first = install.lock(git_project / "packages.yaml", log=lambda _m: None)
    clean = next(e for e in first["packages"] if e["key"] == "acme-extra")["contentHash"]
    (checkout,) = GitCache().root.glob("*/checkouts/*/*/[0-9a-f]*/package.yaml")
    checkout.write_text(checkout.read_text(encoding="utf-8") + "# tampered\n", "utf-8")
    (git_project / "packages.lock").unlink()
    again = install.lock(git_project / "packages.yaml", log=lambda _m: None)
    assert next(e for e in again["packages"] if e["key"] == "acme-extra")["contentHash"] == clean
    assert "tampered" not in checkout.read_text(encoding="utf-8")


def test_symlinks_in_a_git_package_are_refused(git_project: Path, remote: Path) -> None:
    (remote / "pkg" / "roles" / "link.yaml").symlink_to("/etc/hosts")
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "add", ".", cwd=remote)
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "link", cwd=remote)
    _git("tag", "-f", "v0.1.0", cwd=remote)
    with pytest.raises(PackageError, match="symbolic link"):
        install.lock(git_project / "packages.yaml", log=lambda _m: None)
    assert not (git_project / "packages.lock").exists()


def test_content_hash_ignores_the_checkout_settings_of_git(
    git_project: Path, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Блобы как в git: autocrlf и export-subst пользователя хэш не меняют."""
    (remote / ".gitattributes").write_text("* export-subst\n", encoding="utf-8")
    role = remote / "pkg" / "roles" / "extra-officer.yaml"
    role.write_text(role.read_text(encoding="utf-8") + "# $Format:%H$\n", "utf-8")
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "add", ".", cwd=remote)
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "s", cwd=remote)
    _git("tag", "-f", "v0.1.0", cwd=remote)
    first = install.lock(git_project / "packages.yaml", log=lambda _m: None)
    (checkout,) = GitCache().root.glob("*/checkouts/*/*/[0-9a-f]*/roles/extra-officer.yaml")
    assert "$Format:%H$" in checkout.read_text(encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "3")
    monkeypatch.setenv("GIT_CONFIG_KEY_2", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_2", "true")
    again = install.lock(git_project / "packages.yaml", log=lambda _m: None)
    assert again["packages"] == first["packages"]


def test_plan_sections_must_come_in_order(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    _plan(project, core, notify)
    path = project / "plan.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["sections"] = [s for s in document["sections"] if s["kind"] != "knowledge"]
    document["planHash"] = document_hash(document)
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(PackageError, match="expected catalog, core, knowledge"):
        _apply(project, core, notify)


def test_stand_over_plain_http_is_only_localhost(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    target = Target(server="http://platform.example.com", http=core, headers={})
    with pytest.raises(PackageError, match="https"):
        install.plan(project / "packages.yaml", target=target, env=ENV, log=lambda _m: None)
    assert core.plan_calls == []


# --- кэш: неизменяемые выгрузки, поддерево пакета, разбор дерева (TASK-001149) ----------------


def _only_git(project: Path, name: str) -> Path:
    """Установка из одного пакета git в своём каталоге (свой packages.lock)."""
    path = project / name / "packages.yaml"
    _write(
        path,
        {
            "apiVersion": "taimen.ai/v1",
            "kind": "Installation",
            "key": name,
            "spec": {
                "packages": [
                    {
                        "key": "acme-extra",
                        "git": "https://git.example.com/acme/extra",
                        "ref": "v0.1.0",
                        "path": "pkg",
                    }
                ]
            },
        },
    )
    return path


def _bulk(remote: Path, count: int) -> None:
    for index in range(count):
        _write(
            remote / "pkg" / "roles" / f"role-{index:03}.yaml",
            _object("Role", f"role-{index:03}", {"name": f"Role {index}"}),
        )
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "add", ".", cwd=remote)
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "b", cwd=remote)
    _git("tag", "-f", "v0.1.0", cwd=remote)


def test_parallel_locks_and_plan_loads_never_see_a_half_written_checkout(
    project: Path, remote: Path
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    _bulk(remote, 120)
    reader = _only_git(project, "reader")
    install.lock(reader, log=lambda _m: None)
    writers = [_only_git(project, f"writer-{n}") for n in range(3)]

    def locking(path: Path) -> int:
        for _ in range(4):
            install.lock(path, log=lambda _m: None)
        return 0

    def loading(_n: int) -> int:
        seen = 0
        for _ in range(4):
            sources = install.load(reader, strict=True, log=lambda _m: None)
            seen = len(sources.installation.packages[0].objects)
            assert seen == 121
        return seen

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(locking, path) for path in writers]
        futures += [pool.submit(loading, n) for n in range(3)]
        results = [future.result() for future in futures]
    assert results[3:] == [121, 121, 121]


def test_the_same_content_is_not_replaced_under_a_reader(git_project: Path) -> None:
    install.lock(git_project / "packages.yaml", log=lambda _m: None)
    (checkout,) = GitCache().root.glob("*/checkouts/*/*/[0-9a-f]*/package.yaml")
    before = checkout.parent.stat().st_ino
    install.lock(git_project / "packages.yaml", log=lambda _m: None)
    assert checkout.exists() and checkout.parent.stat().st_ino == before
    assert not list(GitCache().root.glob("*/checkouts/*/*/.*"))  # временных не осталось


def test_only_the_package_subtree_is_read(git_project: Path, remote: Path) -> None:
    """Символическая ссылка и прочее вне каталога пакета пакет не отвергают."""
    (remote / "README-link").symlink_to("/etc/hosts")
    _write(remote / "other" / "big.yaml", {"x": 1})
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "add", ".", cwd=remote)
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "o", cwd=remote)
    _git("tag", "-f", "v0.1.0", cwd=remote)
    install.lock(git_project / "packages.yaml", log=lambda _m: None)
    (checkout,) = GitCache().root.glob("*/checkouts/*/*/[0-9a-f]*/package.yaml")
    assert sorted(p.name for p in checkout.parent.iterdir()) == ["package.yaml", "roles"]


def _tree(*entries: tuple[str, bytes]) -> bytes:
    blob = "a" * 40
    return b"".join(f"{mode} blob {blob}\t".encode() + name + b"\0" for mode, name in entries)


@pytest.mark.parametrize(
    ("entries", "said"),
    [
        (
            [("100644", b"pkg/roles/Case.yaml"), ("100644", b"pkg/roles/case.yaml")],
            "differ only in case",
        ),
        # каталоги: Roles/ и roles/ на файловой системе без учёта регистра сольются
        ([("100644", b"pkg/Roles/a.yaml"), ("100644", b"pkg/roles/b.yaml")], "the same directory"),
        ([("100644", b"pkg/roles"), ("100644", b"pkg/Roles/b.yaml")], "differ only in case"),
        ([("100644", b"pkg/\xff.yaml")], "is not UTF-8"),
        ([("100664", b"pkg/package.yaml")], "mode 100664"),
    ],
)
def test_tree_entries_a_package_cannot_have(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entries: list[tuple[str, bytes]], said: str
) -> None:
    monkeypatch.setattr(GitCache, "_run", staticmethod(lambda *_a, **_k: _tree(*entries)))
    with pytest.raises(PackageError, match=said):
        GitCache(tmp_path).extract(
            "https://git.example.com/acme/extra", "b" * 40, "pkg", tmp_path / "out"
        )


@pytest.mark.parametrize(
    "url", ["git@-oProxyCommand=x:repo", "https://-x.example.com/repo", "git@host:-x"]
)
def test_host_or_path_that_reads_as_an_option_is_refused(url: str) -> None:
    from package_sdk.install.lock import check_url

    with pytest.raises(PackageError):
        check_url(url)


# --- без fcntl (Windows): гонка «проверка → rename» (TASK-001155) ---------------------------


def _checkout(path: Path, text: str) -> Path:
    path.mkdir(parents=True)
    (path / "package.yaml").write_text(text, encoding="utf-8")
    return path


def _rename_raises(monkeypatch: pytest.MonkeyPatch, error: OSError) -> None:
    """rename как на Windows: каталог назначения уже есть — FileExistsError."""

    def rename(self: Path, target: Any) -> Path:
        raise error

    monkeypatch.setattr(Path, "rename", rename)


def test_a_checkout_another_process_published_first_is_a_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = _checkout(tmp_path / ".staging-x", "same\n")
    actual = model.install_hash(staging)
    target = _checkout(tmp_path / "ready", "same\n")
    _rename_raises(monkeypatch, FileExistsError(17, "exists"))

    assert GitCache._publish(staging, target, actual) == target


@pytest.mark.parametrize("other", ["other\n", None])
def test_a_lost_rename_race_is_a_package_error_not_file_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, other: str | None
) -> None:
    staging = _checkout(tmp_path / ".staging-x", "same\n")
    actual = model.install_hash(staging)
    target = tmp_path / "ready"
    if other is not None:
        _checkout(target, other)
    _rename_raises(monkeypatch, FileExistsError(17, "exists"))

    with pytest.raises(PackageError):
        GitCache._publish(staging, target, actual)


# --- предупреждения — в stderr: stdout занят --json (TASK-001155) ----------------------------


def _source_just_went_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Источник недоступен, а коммит появляется в кэше (его выкачал соседний процесс):
    загрузка берёт содержимое из кэша и предупреждает об этом."""
    real = GitCache.has
    calls: list[int] = []

    def has(self: GitCache, url: str, commit: str) -> bool:
        calls.append(1)
        return len(calls) > 1 and real(self, url, commit)

    def fetch(self: GitCache, url: str) -> None:
        raise PackageError("git fetch: could not resolve host")

    monkeypatch.setattr(GitCache, "has", has)
    monkeypatch.setattr(GitCache, "fetch", fetch)


def test_the_cache_warning_of_load_goes_to_stderr(
    project: Path, remote: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _only_git(project, "solo")
    install.lock(path, log=lambda _m: None)
    _source_just_went_down(monkeypatch)
    capsys.readouterr()

    install.load(path, strict=False)

    captured = capsys.readouterr()
    assert captured.out == "" and "from the cache" in captured.err


def test_test_install_json_is_clean_json_when_the_cache_is_used(
    project: Path, remote: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _only_git(project, "solo")
    install.lock(path, log=lambda _m: None)
    _source_just_went_down(monkeypatch)
    monkeypatch.chdir(project)
    capsys.readouterr()

    cli.main(["test", "--install", str(path), "--json"])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["packages"] == ["acme-extra"]
    assert "from the cache" in captured.err


# --- уборка кэша: cache prune и брошенные временные выгрузки (TASK-001155) --------------------


def _retag(remote: Path, name: str) -> None:
    role = remote / "pkg" / "roles" / "extra-officer.yaml"
    role.write_text(role.read_text(encoding="utf-8").replace("Extra officer", name), "utf-8")
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", name, cwd=remote)
    _git("tag", "-f", "v0.1.0", cwd=remote)


def _checkouts() -> list[Path]:
    return sorted(p.parent for p in GitCache().root.glob("*/checkouts/*/*/[0-9a-f]*/package.yaml"))


def _age(*paths: Path) -> None:
    """Выгрузка давно не использовалась: prune трогает только такие."""
    import os
    import time

    past = time.time() - 2 * 60 * 60
    for path in paths:
        os.utime(path, (past, past))


def test_cache_prune_removes_only_checkouts_no_lock_refers_to(
    project: Path, remote: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    one = _only_git(project, "one")
    install.lock(one, log=lambda _m: None)
    (old,) = _checkouts()
    _retag(remote, "Second")
    two = _only_git(project, "two")
    install.lock(two, log=lambda _m: None)
    install.lock(one, log=lambda _m: None)  # и one теперь на новом коммите
    assert len(_checkouts()) == 2
    _age(*_checkouts())
    monkeypatch.chdir(project)

    assert cli.main(["cache", "prune"]) == 0

    (kept,) = _checkouts()
    assert kept != old and not old.exists()
    assert "checkouts removed: 1" in capsys.readouterr().out
    assert list(GitCache().root.glob("*/repo.git/HEAD"))  # зеркало осталось
    # установка по-прежнему грузится из кэша, без сети
    shutil.rmtree(remote)
    assert install.load(one, strict=False).installation.packages[0].key == "acme-extra"


def test_cache_prune_keeps_what_the_named_locks_refer_to(
    project: Path, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    one = _only_git(project, "one")
    install.lock(one, log=lambda _m: None)
    (first,) = _checkouts()
    _retag(remote, "Second")
    install.lock(_only_git(project, "two"), log=lambda _m: None)
    _age(*_checkouts())
    monkeypatch.chdir(project / "two")  # под текущим каталогом только lock «two»

    assert cli.main(["cache", "prune", "--lock", str(project / "one" / "packages.lock")]) == 0

    assert _checkouts() == [first]


def test_cache_prune_all_removes_the_whole_cache(
    git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install.lock(git_project / "packages.yaml", log=lambda _m: None)
    monkeypatch.chdir(git_project)

    assert cli.main(["cache", "prune", "--all"]) == 0

    assert _checkouts() == [] and not list(GitCache().root.glob("*/repo.git"))


def test_cache_prune_refuses_a_broken_lock_before_removing_anything(
    git_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install.lock(git_project / "packages.yaml", log=lambda _m: None)
    before = _checkouts()
    broken = git_project / "broken" / "packages.lock"
    broken.parent.mkdir()
    broken.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(git_project)

    assert cli.main(["cache", "prune"]) == 1

    assert "package-sdk.lock/v1" in capsys.readouterr().err
    assert _checkouts() == before


def test_abandoned_staging_checkouts_are_swept_when_the_cache_is_used(
    git_project: Path,
) -> None:
    import os
    import time

    install.lock(git_project / "packages.yaml", log=lambda _m: None)
    (ready,) = _checkouts()
    old = ready.parent / ".staging-abandoned"
    (old / "roles").mkdir(parents=True)
    hour_ago = time.time() - 61 * 60
    os.utime(old, (hour_ago, hour_ago))
    young = ready.parent / ".staging-in-progress"
    young.mkdir()

    install.lock(git_project / "packages.yaml", log=lambda _m: None)

    assert not old.exists() and young.exists() and ready.exists()


def test_cache_prune_spares_a_recently_used_checkout_nobody_locked_yet(
    project: Path, remote: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Выгрузку только что прочитал план или lock другого каталога — ссылок на неё нет."""
    one = _only_git(project, "one")
    install.lock(one, log=lambda _m: None)
    (checkout,) = _checkouts()
    (project / "one" / "packages.lock").unlink()
    elsewhere = _only_git(project, "elsewhere")
    install.lock(elsewhere, log=lambda _m: None)  # та же выгрузка, отмечена «в работе»
    (project / "elsewhere" / "packages.lock").rename(project / "other.lock.json")
    lone = project / "lone"
    lone.mkdir()
    (lone / "packages.lock").write_text(
        json.dumps({"format": "package-sdk.lock/v1", "packages": []}), encoding="utf-8"
    )
    monkeypatch.chdir(lone)

    assert cli.main(["cache", "prune"]) == 0

    assert checkout.exists() and "recently used: 1" in capsys.readouterr().out
    _age(checkout)
    assert cli.main(["cache", "prune"]) == 0
    assert not checkout.exists()


def _prune_during_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """cache prune без единой ссылки — между чтением установки и записью lock."""
    lock_module = importlib.import_module("package_sdk.install.lock")

    real = lock_module.load_installation

    def load_then_prune(*args: Any, **kwargs: Any) -> Any:
        installation = real(*args, **kwargs)
        install.prune([], log=lambda _m: None)
        return installation

    monkeypatch.setattr(lock_module, "load_installation", load_then_prune)


def test_prune_between_reading_and_writing_the_lock_spares_the_checkout(
    project: Path, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _only_git(project, "one")
    clean = install.lock(path, log=lambda _m: None)["packages"][0]["contentHash"]
    _retag(remote, "Second")
    _age(*_checkouts())  # прежняя выгрузка стара — её уборка уберёт
    _prune_during_lock(monkeypatch)

    document = install.lock(path, log=lambda _m: None)

    (entry,) = document["packages"]
    assert entry["contentHash"] != clean
    (checkout,) = _checkouts()
    assert "sha256:" + checkout.name == entry["contentHash"]
    assert install.load(path, strict=False).installation.packages[0].key == "acme-extra"


def test_a_checkout_removed_while_locking_is_not_pinned(
    project: Path, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Выгрузку всё же удалили (уборка без защиты по времени) — lock не пишется: хэш пустого
    каталога зафиксировал бы не содержимое тега, и каждый load падал бы content_mismatch."""
    lock_module = importlib.import_module("package_sdk.install.lock")

    path = _only_git(project, "one")
    _prune_during_lock(monkeypatch)
    monkeypatch.setattr(lock_module, "STAGING_MAX_AGE", 0)

    with pytest.raises(PackageError, match="changed while locking"):
        install.lock(path, log=lambda _m: None)

    assert not (project / "one" / "packages.lock").exists()


def test_a_checkout_pruned_while_its_hash_is_checked_is_a_clear_retry(
    git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Быстрый путь по хэшу из lock читает выгрузку без блокировки: `cache prune --all`
    удаляет и свежую — отказ «повторите», а не трассировка FileNotFoundError."""
    path = git_project / "packages.yaml"
    install.lock(path, log=lambda _m: None)
    (checkout,) = _checkouts()
    real = model._hashed_files

    def listed_then_pruned(directory: Path) -> list[tuple[str, Path]]:
        files = real(directory)
        if directory == checkout:
            install.prune(None, log=lambda _m: None)  # --all между обходом и чтением файлов
        return files

    monkeypatch.setattr(model, "_hashed_files", listed_then_pruned)

    with pytest.raises(PackageError, match=r"was removed while being read.*run the command again"):
        install.load(path, strict=False)

    assert not checkout.exists()


def test_cache_prune_does_not_follow_symlinks_out_of_the_cache(
    git_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install.lock(git_project / "packages.yaml", log=lambda _m: None)
    outside = tmp_path / "outside"
    victim = outside / "checkouts" / ("c" * 40) / "tree" / "precious"
    victim.mkdir(parents=True)
    _age(victim)
    root = GitCache().root
    (root / "linked-source").symlink_to(outside)
    (source,) = [p for p in root.iterdir() if not p.is_symlink()]
    other = root / ("f" * 64)
    other.mkdir()
    (other / "checkouts").symlink_to(outside / "checkouts")
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "packages.lock").write_text(
        json.dumps({"format": "package-sdk.lock/v1", "packages": []}), encoding="utf-8"
    )
    monkeypatch.chdir(empty)

    assert cli.main(["cache", "prune"]) == 0
    assert cli.main(["cache", "prune", "--all"]) == 0

    assert victim.is_dir() and source.is_dir()


def test_cache_prune_needs_a_lock_or_all(
    git_project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install.lock(git_project / "packages.yaml", log=lambda _m: None)
    before = _checkouts()
    _age(*before)
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    monkeypatch.chdir(nowhere)

    assert cli.main(["cache", "prune"]) == 1
    assert "--all" in capsys.readouterr().err
    assert cli.main(["cache", "prune", "--lock", str(nowhere / "packages.lock")]) == 1
    assert "no lock file" in capsys.readouterr().err
    assert _checkouts() == before


def test_locks_are_not_searched_from_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from package_sdk.install.lock import find_locks

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with pytest.raises(PackageError, match="--lock"):
        find_locks(tmp_path)
    deep = tmp_path / "project" / "a" / "b" / "c" / "d" / "e" / "f" / "g"
    deep.mkdir(parents=True)
    (deep / "packages.lock").write_text("{}", encoding="utf-8")
    (tmp_path / "project" / "packages.lock").write_text("{}", encoding="utf-8")
    assert find_locks(tmp_path / "project") == [tmp_path / "project" / "packages.lock"]
