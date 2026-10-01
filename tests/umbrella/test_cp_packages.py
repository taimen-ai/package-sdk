"""Пакеты каталога (TAI-ADR-0044): проверка без стенда и установка на фейковом Control Plane —
единым планом, как ставит bootstrap: ``install.plan()`` → ``install.apply(assume_yes=True)``.

python3 -m pytest -q tools/tests
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import tempfile
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

from package_sdk import install
from package_sdk.install.plan import Target
from tests.umbrella._shim import FIXTURES, cp, settled
from tests.umbrella._shim import UMBRELLA as ROOT

# Снимок OpenAPI ядра: POST /packages:record и его схемы (control-plane, CP-ADR-0074 §11)
OPENAPI_SNAPSHOT = FIXTURES / "control-plane-openapi-packages-record.json"
# Ядро рядом с компонентом (path-зависимость ../control-plane): его RecordedKind сверяется
# с запасным перечнем видов
CORE_SOURCE = Path(__file__).resolve().parents[3] / "control-plane" / "src"
# Где ядро ищет объект вида для packages:record: коллекция и поле идентичности
RECORD_LOOKUP = {
    "ArtifactType": ("artifact-types", "key"),
    "TaskType": ("task-types", "key"),
    "ProjectTemplate": ("project-templates", "key"),
    "WorkspaceType": ("workspace-types", "key"),
    "Role": ("roles", "slug"),
    "Capability": ("capabilities", "name"),
    "Skill": ("skills", "name"),
    "WorkRule": ("rules", "key"),
    "Agent": ("agents", "key"),
}

# --- фейковый Control Plane ---------------------------------------------------


class FakeControlPlane:
    """Ровно та часть API каталога, которой пользуется установщик, с его инвариантами:
    версии типов и шаблонов неизменяемы, PATCH требует If-Match."""

    GLOBAL_MAX_BYTES = 100 * 1024 * 1024
    STAND = ("workspaces", "projects", "principals", "roles")

    def __init__(self) -> None:
        self.rows: dict[str, list[dict]] = {
            k: []
            for k in (
                "artifact-types",
                "task-types",
                "project-templates",
                "workspace-types",
                "roles",
                "capabilities",
                "skills",
                "rules",
            )
        }
        self.writes: list[tuple[str, str]] = []
        # OpenAPI ядра (снимок control-plane); None — /openapi.json не отдаётся
        self.openapi: dict | None = json.loads(OPENAPI_SNAPSHOT.read_text(encoding="utf-8"))
        # POST /packages:record: тела запросов по порядку и связи (kind, key) → пакет
        self.records: list[dict] = []
        self.links: dict[tuple[str, str], dict] = {}
        self.record_error: Exception | None = None
        self.agent_requests: list[tuple[str, dict]] = []
        # онтологии памяти через ядро (TAI-ADR-0062 п.5): "name@version" → тело регистрации;
        # включённые наборы деревьев workspace: id → {packs, strict}
        self.knowledge_packs: dict[str, dict] = {}
        self.workspace_packs: dict[str, dict] = {}

    def call(self, method, path, body=None, headers=None):
        if path == cp.OPENAPI_PATH:
            if self.openapi is None:
                raise cp.HttpError("GET /openapi.json: HTTP 404: not found", 404)
            return copy.deepcopy(self.openapi)
        assert path.startswith("/api/v1/")
        parsed = urllib.parse.urlparse(path[len("/api/v1") :])
        query = dict(urllib.parse.parse_qsl(parsed.query))
        parts = parsed.path.strip("/").split("/")
        collection = parts[0]
        if parsed.path == cp.PACKAGES_RECORD:
            assert method == "POST" and "Idempotency-Key" in (headers or {})
            return self._record(copy.deepcopy(body))
        if collection.startswith("agents"):
            if method != "GET" and parsed.path != "/agents:validate":  # :validate ничего не меняет
                self.writes.append((method, parsed.path))
            if method == "POST":
                self.agent_requests.append((parsed.path, copy.deepcopy(body)))
            return self._agents(method, parts, body)
        if collection == "knowledge" or parsed.path.endswith("/knowledge-packs"):
            return self._knowledge(method, parsed.path, body)
        if method == "GET" and len(parts) == 2 and collection in self.STAND:
            # переменные установки вида workspace/project/principal/role план сверяет со
            # стендом (plan Р3): объекты, созданные не пакетами, на этом стенде есть
            found = [r for r in self.rows.get(collection, []) if r["id"] == parts[1]]
            return copy.deepcopy(found[0]) if found else {"id": parts[1]}
        rows = self.rows[collection]
        if method != "GET":
            self.writes.append((method, parsed.path))
        if collection == "rules":
            return self._rules(method, parts, query, body, headers)
        if method == "GET" and len(parts) == 1:
            items = [
                r
                for r in rows
                if all(
                    str(r.get(k)) == v for k, v in query.items() if k in ("key", "status", "name")
                )
            ]
            return {"items": copy.deepcopy(items), "nextCursor": None}
        if method == "GET":
            return copy.deepcopy(self._find(rows, parts[1]))
        if method == "POST" and len(parts) == 1:
            return copy.deepcopy(self._create(collection, rows, body))
        if method == "POST" and parts[1].endswith(":deprecate"):
            row = self._find(rows, parts[1].split(":")[0])
            if (
                row["key"] == "task"
                and sum(r["key"] == "task" and r["status"] == "active" for r in rows) <= 1
            ):
                raise RuntimeError("HTTP 422: system_task_type_required")
            row["status"] = "deprecated"
            return copy.deepcopy(row)
        if method == "PATCH":
            row = self._find(rows, parts[1])
            counter = "rowVersion" if collection == "skills" else "version"
            entity = {"skills": "skill", "roles": "role", "workspace-types": "workspace_type"}[
                collection
            ]
            if (headers or {}).get("If-Match") != f'"{entity}-{row[counter]}"':
                raise RuntimeError("HTTP 412: precondition failed")
            body = dict(body)
            if collection == "skills" and "endpoint" in body:  # амендмент ADR-0056 от 2026-09-29
                row["contract"]["implementation"]["endpoint"] = body.pop("endpoint")
            row.update(body)
            row[counter] += 1
            return copy.deepcopy(row)
        raise AssertionError(f"не поддержано: {method} {path}")

    def _rules(self, method, parts, query, body, headers):
        """Правила (CP-ADR-0063): изменяемые, If-Match "rule-<v>", DELETE — архив."""
        rows = self.rows["rules"]
        if method == "GET" and len(parts) == 1:
            items = [
                r
                for r in rows
                if all(str(r.get(k)) == v for k, v in query.items() if k in ("key", "status"))
            ]
            return {"items": copy.deepcopy(items), "nextCursor": None}
        if method == "POST" and len(parts) == 1:
            assert not any(r["key"] == body["key"] and r["status"] != "archived" for r in rows), (
                "rule_key_taken"
            )
            from control_plane.domain.work_rules import normalize_rule_spec

            spec = normalize_rule_spec(
                trigger=body["trigger"],
                condition=body.get("condition"),
                interpretation=body.get("interpretation"),
                action=body["action"],
            )
            row = {
                "id": str(uuid.uuid4()),
                "key": body["key"],
                "description": body.get("description", ""),
                "workspaceId": body.get("workspaceId"),
                "version": 1,
                "status": body.get("status", "enabled"),
                "trigger": spec.trigger,
                "condition": spec.condition,
                "interpretation": spec.interpretation,
                "action": spec.action,
                # CP-ADR-0063, амендмент 2026-09-27 (Г1): личность — как прислана, иначе null
                "identity": copy.deepcopy(body.get("identity")),
            }
            rows.append(row)
            return copy.deepcopy(row)
        rule_id, _, verb = parts[1].partition(":")
        row = self._find(rows, rule_id)
        if method == "PATCH":
            if (headers or {}).get("If-Match") != f'"rule-{row["version"]}"':
                raise RuntimeError("HTTP 412: precondition failed")
            row.update(copy.deepcopy(body))
            row["version"] += 1
        elif method == "POST" and verb in ("enable", "disable"):
            row["status"] = f"{verb}d"
        elif method == "DELETE":
            row["status"] = "archived"
        else:
            raise AssertionError(f"не поддержано: {method} rules/{parts[1]}")
        return copy.deepcopy(row)

    def _agents(self, method, parts, body):
        """Агенты (CP-ADR-0073): ревизия — по хэшу описания без state и replicas."""
        agents = self.rows.setdefault("agents", [])

        def split(spec):
            revision = copy.deepcopy(spec)
            state = revision.pop("state", "running")
            placement = revision.get("placement")
            replicas = placement.pop("replicas", 1) if isinstance(placement, dict) else 0
            return revision, state, replicas

        def find(key):
            return next((a for a in agents if a["key"] == key), None)

        if method == "POST" and parts[0] in ("agents", "agents:validate") and len(parts) == 1:
            revision, state, replicas = split(body["spec"])
            current = find(body["key"])
            new_revision = current is None or current["revision"]["spec"] != revision
            changes_state = current is None or (current["state"], current["replicas"]) != (
                state,
                replicas,
            )
            if parts[0] == "agents:validate":
                return {
                    "key": body["key"],
                    "specHash": "sha256:x",
                    "wouldCreateRevision": new_revision,
                    "wouldChangeState": changes_state,
                    "currentRevision": current["currentRevision"] if current else None,
                }
            if current is None:
                current = {
                    "id": str(uuid.uuid4()),
                    "key": body["key"],
                    "status": "active",
                    "currentRevision": 0,
                }
                agents.append(current)
            if new_revision:
                current["currentRevision"] += 1
                current["revision"] = {
                    "revision": current["currentRevision"],
                    "spec": revision,
                    # источник ревизии: пакет установщика или ручная правка
                    "package": copy.deepcopy(body.get("package")),
                }
            current.update(state=state, replicas=replicas)
            return copy.deepcopy(current)
        key, _, verb = parts[1].partition(":")
        agent = find(key)
        if agent is None:
            raise RuntimeError("HTTP 404: agent_not_found")
        if method == "GET":
            return copy.deepcopy(agent)
        if method == "POST" and verb == "retire":
            agent["status"] = "retired"
            return copy.deepcopy(agent)
        raise AssertionError(f"не поддержано: {method} agents/{parts[1]}")

    def _knowledge(self, method, path, body):
        """POST /knowledge/packs: версия онтологии неизменяема — то же тело идемпотентно, другое
        — 409; PUT /workspaces/{id}/knowledge-packs: набор дерева заменяется целиком.
        В writes попадает только то, что изменило состояние. GET — то, что читает план."""
        if method == "GET" and path.startswith("/knowledge/packs/"):
            ref = urllib.parse.unquote(path.rsplit("/", 1)[1])
            if ref not in self.knowledge_packs:
                raise cp.HttpError(f"GET /api/v1{path}: HTTP 404: not_found", 404)
            return copy.deepcopy(self.knowledge_packs[ref])
        if method == "POST" and path == "/knowledge/packs":
            ref = f"{body['name']}@{body['version']}"
            known = self.knowledge_packs.get(ref)
            if known is not None and known != body:
                raise cp.HttpError(f"POST /api/v1{path}: HTTP 409: pack_version_conflict", 409)
            if known is None:
                self.knowledge_packs[ref] = copy.deepcopy(body)
                self.writes.append((method, path))
            # ответ памяти как есть (memory-service, domain/registry.py Registry.register)
            return {"status": "unchanged" if known else "created", "pack": copy.deepcopy(body)}
        parts = path.strip("/").split("/")
        if method == "GET" and parts[0] == "workspaces" and len(parts) == 3:
            current = self.workspace_packs.get(parts[1])
            return {
                "workspaceId": parts[1],
                "configured": current is not None,
                "packs": (current or {}).get("packs", []),
                "strict": (current or {}).get("strict", False),
            }
        if method == "PUT" and parts[0] == "workspaces" and len(parts) == 3:
            assert set(body) == {"packs", "strict"}, body
            if self.workspace_packs.get(parts[1]) != body:
                self.workspace_packs[parts[1]] = copy.deepcopy(body)
                self.writes.append((method, path))
            return copy.deepcopy(body)
        raise AssertionError(f"не поддержано: {method} {path}")

    def _record(self, body):
        """POST /packages:record (CP-ADR-0074 §11, амендмент TASK-000904) с отказами ядра:
        вид вне RecordedKind — 400, объекта нет в tenant — 422 unknown_object."""
        if self.record_error is not None:
            raise self.record_error
        # ядро, OpenAPI которого не отдаётся или не разбирается, принимает свой RecordedKind —
        # как в снимке
        try:
            allowed = cp.recorded_kinds_from_openapi(self.openapi or {})
        except cp.PackageError:
            allowed = None
        if allowed is None:
            allowed = cp.recorded_kinds_from_openapi(
                json.loads(OPENAPI_SNAPSHOT.read_text(encoding="utf-8"))
            )
        assert set(body) <= {"package", "installHash", "objects"}
        assert set(body["package"]) == {"key", "version"}
        assert 1 <= len(body["objects"]) <= 1000
        refused = [o for o in body["objects"] if o["kind"] not in allowed]
        if refused:
            raise cp.HttpError(f"POST /api/v1/packages:record: HTTP 400: {refused}", 400)
        missing = [
            o
            for o in body["objects"]
            if not any(
                row.get(RECORD_LOOKUP[o["kind"]][1]) == o["key"]
                for row in self.rows.get(RECORD_LOOKUP[o["kind"]][0], [])
            )
        ]
        if missing:
            raise cp.HttpError(
                f"POST /api/v1/packages:record: HTTP 422: unknown_object {missing}", 422
            )
        self.records.append(body)
        for item in body["objects"]:
            self.links[(item["kind"], item["key"])] = {
                **body["package"],
                "installHash": body.get("installHash"),
            }
        return {
            "package": body["package"],
            "installHash": body.get("installHash"),
            "recorded": body["objects"],
        }

    @staticmethod
    def _find(rows, row_id):
        return next(r for r in rows if r["id"] == row_id)

    def _create(self, collection, rows, body):
        row = {"id": str(uuid.uuid4()), **copy.deepcopy(body)}
        if collection == "artifact-types":  # CP-ADR-0072 §6: версии без :deprecate
            from control_plane.domain.artifact_type import validate_artifact_type_definition

            definition = validate_artifact_type_definition(
                metadata_schema=body.get("metadataSchema"),
                media_types=body["mediaTypes"],
                max_bytes=body.get("maxBytes"),
                global_max_bytes=self.GLOBAL_MAX_BYTES,
            )
            row.update(
                version=1 + max((r["version"] for r in rows if r["key"] == body["key"]), default=0),
                status="active",
                description=body.get("description", ""),
                metadataSchema=definition.metadata_schema,
                mediaTypes=definition.media_types,
                maxBytes=definition.max_bytes,
            )
        elif collection in ("task-types", "project-templates"):
            row["version"] = 1 + max(
                (r["version"] for r in rows if r["key"] == body["key"]), default=0
            )
            row["status"] = "active"
            row.setdefault("lifecycleSchema", {"statuses": []})
            if collection == "task-types":
                row.setdefault("fieldSchema", {})
                row.setdefault("approvalSchema", {})
                row.setdefault("description", "")
                if row.get("execution"):
                    row["execution"].setdefault("inputs", "$.customFields")
        elif collection == "skills":
            assert not any(
                r["name"] == body["name"] and r["version"] == body["version"] for r in rows
            )
            row.update(status="active", rowVersion=1)
            if row.get("contract"):  # ядро хранит контракт нормализованным (CP-ADR-0056 §1)
                from control_plane.domain.skill_contract import normalize_contract

                row["contract"] = normalize_contract(row["contract"])
        else:
            row.setdefault("description", "")
            if collection == "roles":
                row.update(version=1, workspaceId=None)
            if collection == "workspace-types":
                row.update(version=1, status="active")
                row.setdefault("fieldSchema", {})
                row.setdefault("allowedChildTypes", [])
        rows.append(row)
        return row


SELFDEV_ENV = {
    "SELFDEV_WORKSPACE_ID": "aaaaaaaa-0000-4000-8000-000000000001",
    "SELFDEV_SUPERPROJECT_URL": "https://git.example/superproject.git",
    "SELFDEV_REVIEWER_PRINCIPAL": "aaaaaaaa-0000-4000-8000-000000000002",
    "SELFDEV_SKILLS_EXECUTOR": "aaaaaaaa-0000-4000-8000-000000000003",
    # адрес платформы: http-скиллы основного кодера ходят только туда (агенты selfdev)
    "TAIMEN_PUBLIC_URL": "https://platform.example.com",
}


SERVER = "https://platform.example.com"


def _install_file(directory: Path, installation: Any) -> Path:
    """Файл установки по установке теста: пакеты по ключам каталога, retire и онтологии.
    Установку теста нельзя править в памяти — план читает пакеты из файлов, поэтому
    расхождение файла и объектов теста — ошибка самого теста."""
    spec: dict[str, Any] = {"packages": [p.key for p in installation.packages]}
    if installation.retire:
        spec["retire"] = {k: list(v) for k, v in installation.retire.items()}
    if installation.knowledge:
        spec["knowledge"] = copy.deepcopy(installation.knowledge)
    path = directory / "packages.yaml"
    document = {"apiVersion": cp.API_VERSION, "kind": "Installation", "key": "test", "spec": spec}
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    loaded = cp.load_installation(path)
    assert [(o.ref, o.spec) for o in loaded.objects] == [
        (o.ref, o.spec) for o in installation.objects
    ], "установку теста правили в памяти — правьте файлы пакета"
    return path


def _target(fake, notify=None) -> Target:
    return Target(SERVER, fake, {"Authorization": "Bearer t"}, notify)


def plan_only(fake, installation, *, env=None, notify=None):
    """Единый план установки (install.plan): ничего не пишет; документ и журнал плана."""
    lines: list[str] = []
    with tempfile.TemporaryDirectory() as directory:
        document = install.plan(
            _install_file(Path(directory), installation),
            target=_target(fake, notify),
            env=SELFDEV_ENV if env is None else env,
            log=lines.append,
        )
    return document, lines


def run_apply(fake, installation, *, env=None, notify=None):
    """Установка как у bootstrap: план, затем его применение без подтверждения человека.
    Итог — что поставила секция catalog, и журнал применения (журнал плана — в plan_only)."""
    env = SELFDEV_ENV if env is None else env
    with tempfile.TemporaryDirectory() as directory:
        path = _install_file(Path(directory), installation)
        target = _target(fake, notify)
        document = install.plan(path, target=target, env=env, log=lambda _m: None)
        lines: list[str] = []
        applied = install.apply(document, target=target, env=env, assume_yes=True, log=lines.append)
    return applied.get("catalog", {}), lines


def catalog_changes(document) -> list[dict]:
    return next(s for s in document["sections"] if s["kind"] == "catalog")["changes"]


def _object_lines(lines: list[str]) -> list[str]:
    """Строки журнала установщика об объектах: «   Kind/key: …»."""
    return [line for line in lines if re.match(r"^   [A-Z][A-Za-z]+/\S+: ", line)]


# --- пакеты в репозитории -----------------------------------------------------


def test_repository_packages_pass_check():
    installation = cp.resolve([d.name for d in cp.all_package_dirs()])
    errors, warnings = cp.check(installation)
    assert errors == []
    assert not any("не импортируются" in w for w in warnings), (
        "доменные валидаторы должны работать в CI"
    )


@pytest.mark.parametrize("install", ["deploy/packages.yaml", "deploy/staging/packages.yaml"])
def test_installation_files_pass_check(install):
    installation = cp.load_installation(ROOT / install)
    assert settled(cp.check(installation)) == ([], [])


# --- проверка -----------------------------------------------------------------


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Копия packages/ во временном каталоге — чтобы портить файлы в тестах."""
    root = tmp_path / "packages"
    shutil.copytree(ROOT / "packages", root)
    monkeypatch.setattr(cp, "PACKAGES_DIR", root)
    return root


def _edit(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    new = re.sub(pattern, replacement, text, count=1, flags=re.MULTILINE)
    assert new != text, pattern
    path.write_text(new, encoding="utf-8")


def test_rule_fields_workspace_is_a_template_checked_before_the_core_knows_it(sandbox):
    """fields.workspaceId — амендмент CP-ADR-0063 (process-packages P012): пока ядро поля не
    знает, шаблон проверяет package-sdk, а остальное действие — ядро."""
    rule = sandbox / "process-knowledge" / "rules" / "regulation-drift.yaml"
    assert settled(cp.check(cp.resolve(["process-knowledge"]))) == ([], [])
    _edit(
        rule, r'workspaceId: "\{\{item\.workspaceId\}\}"', 'workspaceId: "{{nowhere.workspaceId}}"'
    )
    errors, _ = cp.check(cp.resolve(["process-knowledge"]))
    assert any("action.fields.workspaceId" in e and "unknown root" in e for e in errors)


def test_check_rejects_dangling_reference(sandbox):
    _edit(
        sandbox / "sdd" / "task-types" / "feature-design.yaml",
        r"type: feature-tasks",
        "type: feature-taks",
    )
    errors, _ = cp.check(cp.resolve(["sdd"]))
    assert any("feature-taks" in e for e in errors)  # ссылка внутри onSuccess


def test_check_rejects_dangling_reference_in_completion_work(sandbox):
    # completionSchema в пакетах больше нет (TAI-ADR-0053) — проверка ссылок держится на фикстуре
    _package_with(
        sandbox,
        "demo",
        [
            {
                "kind": "TaskType",
                "key": "work",
                "spec": {
                    "displayName": "Work",
                    "lifecycleSchema": cp._read_yaml(
                        ROOT / "packages" / "selfdev" / "task-types" / "devops.yaml"
                    )["spec"]["lifecycleSchema"],
                    "completionSchema": {
                        "onComplete": {
                            "actions": [
                                {
                                    "ensureWork": {
                                        "type": "code-reviw",
                                        "key": "review:$.task.id!",
                                        "title": "Ревью $.task.publicId!",
                                    }
                                }
                            ]
                        }
                    },
                },
            }
        ],
    )
    errors, _ = cp.check(cp.resolve(["demo"]))
    assert any("code-reviw" in e for e in errors)


def test_check_rejects_a_templated_rule_type_outside_its_list(sandbox):
    _edit(
        sandbox / "sdd" / "rules" / "feature-expand.yaml",
        r"taskTypes: \[coding-task, feature-converge\]",
        "taskTypes: [coding-task, feature-convrge]",
    )
    errors, _ = cp.check(cp.resolve(["sdd"]), env=SELFDEV_ENV)
    assert any("action.taskTypes 'feature-convrge'" in e for e in errors)
    assert not any("'{{item.type}}'" in e for e in errors)


def test_check_rejects_invalid_lifecycle_with_core_validator(sandbox):
    _edit(
        sandbox / "selfdev" / "task-types" / "devops.yaml",
        r"completionStatus: done",
        "completionStatus: cancelled",
    )
    errors, _ = cp.check(cp.resolve(["selfdev"]))
    assert any("invalid_lifecycle_schema" in e and "completionStatus" in e for e in errors)


def test_task_field_named_like_a_secret_is_refused_before_the_core(sandbox):
    """Ядро отвергает ключ customFields со словом token (secret_material_rejected) при записи
    задачи, а не при публикации типа: с полем stateToken тип knowledge-import ставился, а план
    в задачу не записывался (TASK-001190). Поле stateRef ядро пропускает."""
    task_type = sandbox / "company-knowledge" / "task-types" / "knowledge-import.yaml"
    fields = cp._read_yaml(task_type)["spec"]["fieldSchema"]["properties"]
    assert "stateRef" in fields and not [name for name in fields if "token" in name.lower()]
    domain = cp._domain()
    if domain is None:
        pytest.skip("доменные валидаторы control-plane не импортируются (нет экстры sandbox)")
    # тот документ, который пишет импортёр, ядро принимает
    domain.work_item.validate_task_custom_fields(
        cp._read_yaml(task_type)["spec"]["fieldSchema"],
        {"pack": "company@1", "kind": "offering", "planStatus": "ok", "stateRef": "st:1"},
    )
    assert settled(cp.check(cp.resolve(["company-knowledge"]), env=SELFDEV_ENV))[0] == []
    _edit(task_type, r"^      stateRef:", "      stateToken:")
    errors, _ = cp.check(cp.resolve(["company-knowledge"]), env=SELFDEV_ENV)
    assert any("secret_material_rejected" in e and "stateToken" in e for e in errors)


def test_check_rejects_schema_violation(sandbox):
    _edit(
        sandbox / "selfdev" / "task-types" / "devops.yaml",
        r"^  displayName: DevOps",
        "  displayName: DevOps\n  colour: red",
    )
    errors, _ = cp.check(cp.resolve(["selfdev"]))
    assert any("colour" in e for e in errors)


def test_check_rejects_duplicates_and_retiring_system_type(sandbox):
    shutil.copy(
        sandbox / "selfdev" / "task-types" / "devops.yaml",
        sandbox / "selfdev" / "task-types" / "devops-copy.yaml",
    )
    installation = cp.resolve(["selfdev"], {"TaskType": ["task", "devops"]})
    errors, _ = cp.check(installation)
    assert any("уже объявлен" in e for e in errors)
    assert any("системный тип task" in e for e in errors)
    assert any("TaskType/devops одновременно" in e for e in errors)


def test_requires_pulls_dependencies_in_order():
    installation = cp.resolve(["sdd"])
    assert [p.key for p in installation.packages] == ["selfdev", "sdd"]


def test_env_substitution():
    assert cp.substitute({"a": ["${X}/api"]}, {"X": "http://svc"}) == {"a": ["http://svc/api"]}
    with pytest.raises(cp.PackageError, match="Y не задана"):
        cp.substitute("${Y}", {})
    assert cp.substitute("$.task.id!", {}) == "$.task.id!"  # выражения исходов не трогаются


# --- применение ---------------------------------------------------------------


def test_apply_is_idempotent():
    fake = FakeControlPlane()
    installation = cp.resolve(["selfdev"])
    result, _ = run_apply(fake, installation)
    assert {
        "TaskType/devops",
        "TaskType/coding-task",
        "TaskType/submodule-bump",
        "Agent/selfdev-rules",
        "Skill/git.merge@1",
        "WorkRule/docs-drift",
    } <= set(result)
    first_writes = len(fake.writes)
    # по одной записи на объект пакета: типы, правила, скиллы, агенты и онтологии
    kinds = [
        o.kind
        for o in installation.objects
        if o.kind in ("TaskType", "WorkRule", "Skill", "Agent", "KnowledgePack")
    ]
    assert first_writes == len(kinds) and kinds.count("TaskType") == 4

    document, _ = plan_only(fake, installation)
    assert install.count_changes(document) == 0  # повторный план пуст
    _, lines = run_apply(fake, installation)
    assert len(fake.writes) == first_writes, lines
    assert _object_lines(lines) and all("без изменений" in line for line in _object_lines(lines))


def test_changed_type_gets_new_version_and_old_versions_are_deprecated(sandbox):
    fake = FakeControlPlane()
    run_apply(fake, cp.resolve(["selfdev"]))
    # на стенде руками опубликована лишняя версия — она тоже уходит из оборота
    fake.call("POST", "/api/v1/task-types", {"key": "devops", "displayName": "manual"})
    _edit(
        sandbox / "selfdev" / "task-types" / "devops.yaml",
        r"^  description: >-\n(?:    .*\n)+",
        "  description: другое описание\n",
    )

    result, lines = run_apply(fake, cp.resolve(["selfdev"]))
    versions = {r["version"]: r["status"] for r in fake.rows["task-types"] if r["key"] == "devops"}
    assert versions == {1: "deprecated", 2: "deprecated", 3: "active"}
    assert result["TaskType/devops"]["version"] == 3
    assert any("изменились" in line and "description" in line for line in lines)


def test_plan_writes_nothing():
    fake = FakeControlPlane()
    fake.call("POST", "/api/v1/task-types", {"key": "ops", "displayName": "Ops"})
    fake.writes.clear()
    document, lines = plan_only(fake, cp.resolve(["selfdev"], {"TaskType": ["ops"]}))
    assert fake.writes == [] and fake.records == []
    devops = next(c for c in catalog_changes(document) if c["key"] == "devops")
    assert (devops["operation"], devops["detail"]) == ("create", "новая версия (нет в tenant)")
    (retire,) = next(s for s in document["sections"] if s["kind"] == "retire")["items"]
    assert (retire["kind"], retire["key"], retire["operation"]) == ("TaskType", "ops", "deprecate")
    assert any("TaskType/devops (selfdev): новая версия (нет в tenant)" in line for line in lines)


def test_retire_deprecates_every_active_version():
    fake = FakeControlPlane()
    for _ in range(2):
        fake.call("POST", "/api/v1/task-types", {"key": "ops", "displayName": "Ops"})
    run_apply(fake, cp.resolve(["selfdev"], {"TaskType": ["ops"]}))
    assert {r["status"] for r in fake.rows["task-types"] if r["key"] == "ops"} == {"deprecated"}


def _package_with(sandbox: Path, name: str, docs: list[dict]) -> None:
    directory = sandbox / name
    directory.mkdir()
    manifest = {
        "apiVersion": cp.API_VERSION,
        "kind": "Package",
        "key": name,
        "spec": {"version": "0.1.0", "displayName": name, "requires": []},
    }
    # ${…} объектов объявлены в манифесте: необъявленная переменная — ошибка check
    used = sorted(set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", json.dumps(docs))))
    if used:
        manifest["spec"]["variables"] = {
            name: {"kind": "string", "description": "переменная теста"} for name in used
        }
    (directory / "package.yaml").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )  # JSON — подмножество YAML
    for index, doc in enumerate(docs):
        (directory / f"obj{index}.yaml").write_text(
            json.dumps({"apiVersion": cp.API_VERSION, **doc}, ensure_ascii=False), encoding="utf-8"
        )


def test_mutable_kinds_patch_only_differences(sandbox):
    _package_with(
        sandbox,
        "demo",
        [
            {"kind": "Role", "key": "reviewer", "spec": {"name": "Reviewer"}},
            {"kind": "WorkspaceType", "key": "team", "spec": {"displayName": "Team"}},
            {"kind": "Capability", "key": "code.review", "spec": {"description": "Ревью кода"}},
        ],
    )
    fake = FakeControlPlane()
    run_apply(fake, cp.resolve(["demo"]))
    role = fake.rows["roles"][0]
    assert (role["slug"], role["name"]) == ("reviewer", "Reviewer")

    _edit(sandbox / "demo" / "obj0.yaml", r'"Reviewer"', '"Code reviewer"')
    _edit(sandbox / "demo" / "obj2.yaml", r"Ревью кода", "Другое")
    before = len(fake.writes)
    _, lines = run_apply(fake, cp.resolve(["demo"]))
    assert fake.writes[before:] == [("PATCH", f"/roles/{role['id']}")]
    assert fake.rows["roles"][0]["name"] == "Code reviewer"
    assert any("описание в tenant отличается" in line for line in lines)


def test_skill_contract_is_immutable_but_description_is_patched(sandbox):
    fake = FakeControlPlane()
    run_apply(fake, cp.resolve(["selfdev"]))
    path = sandbox / "selfdev" / "skills" / "git.merge.yaml"
    _edit(path, r"^  description: .*$", "  description: Слить ветку")
    run_apply(fake, cp.resolve(["selfdev"]))
    merge = next(r for r in fake.rows["skills"] if r["name"] == "git.merge")
    assert merge["description"] == "Слить ветку" and merge["rowVersion"] == 2

    _edit(path, r"timeoutSeconds: 300", "timeoutSeconds: 120")
    with pytest.raises(cp.PackageError, match="поднимите spec.version"):
        run_apply(fake, cp.resolve(["selfdev"]))


def test_skill_endpoint_moves_without_a_new_version():
    """Адрес реализации — свойство инсталляции (амендмент ADR-0056 от 2026-09-29):
    notify.send@1 переезжает на новый хост PATCH'ем, а не новой версией."""
    fake, notify = FakeControlPlane(), FakeNotificationService()
    env = {**INVOICE_ENV, "TASK_URL_BASE": NOTIFY_ENV["TASK_URL_BASE"]}
    run_notify(fake, notify, cp.resolve(["notify"]), env=env)
    skill = next(r for r in fake.rows["skills"] if r["name"] == "notify.send")
    moved = {**env, "NOTIFICATION_SERVICE_URL": "https://moved.example"}

    before = len(fake.writes)
    _, lines = run_notify(fake, notify, cp.resolve(["notify"]), env=moved)
    assert ("PATCH", f"/skills/{skill['id']}") in fake.writes[before:]
    skill = next(r for r in fake.rows["skills"] if r["name"] == "notify.send")
    assert (
        skill["contract"]["implementation"]["endpoint"]
        == "https://moved.example/api/v1/skills/notify.send"
    )
    assert skill["version"] == "1"
    assert any("notify.send" in line and "endpoint" in line for line in lines), lines

    before = len(fake.writes)
    run_notify(fake, notify, cp.resolve(["notify"]), env=moved)
    assert not [w for w in fake.writes[before:] if w[1].startswith("/skills")]


# --- WorkRule (CP-ADR-0063 §11) -----------------------------------------------


def _rule(fake, key):
    return next(r for r in fake.rows["rules"] if r["key"] == key and r["status"] != "archived")


def test_work_rule_is_created_then_left_alone(sandbox):
    fake = FakeControlPlane()
    run_apply(fake, cp.resolve(["selfdev"]))
    # все правила пакета — по файлам, чтобы тест не расходился с составом пакета
    expected = sorted(o.key for o in cp.resolve(["selfdev"]).objects if o.kind == "WorkRule")
    assert sorted(r["key"] for r in fake.rows["rules"]) == expected
    assert {
        "docs-drift",
        "submodule-lag",
        "oss-sync",
        "oss-release-sync",
        "oss-check-red",
        "oss-check-resolved",
    } <= set(expected)
    assert _rule(fake, "adr-conformance")["status"] == "enabled"  # включено владельцем 2026-09-25
    rule = _rule(fake, "docs-drift")
    assert rule["key"] == "docs-drift" and rule["status"] == "enabled"
    assert rule["workspaceId"] == SELFDEV_ENV["SELFDEV_WORKSPACE_ID"]
    assert rule["interpretation"]["inputs"]["repository"] == SELFDEV_ENV["SELFDEV_SUPERPROJECT_URL"]
    # шаблоны правила ${…} не трогает: {{…}} остаются ядру
    assert rule["interpretation"]["inputs"]["ref"] == "{{payload.data.sha}}"

    before = len(fake.writes)
    _, lines = run_apply(fake, cp.resolve(["selfdev"]))
    assert fake.writes[before:] == []
    assert any("WorkRule/docs-drift: v1 без изменений" in line for line in lines)


def test_work_rule_change_is_patched_and_status_follows_the_file(sandbox):
    fake = FakeControlPlane()
    run_apply(fake, cp.resolve(["selfdev"]))
    rule_id = _rule(fake, "docs-drift")["id"]
    path = sandbox / "selfdev" / "rules" / "docs-drift.yaml"
    _edit(
        path,
        r"^    kind: ensure_work$",
        "    kind: ensure_work\n    where: {ne: [{var: item.kind}, deprecated_ref]}",
    )
    _edit(path, r"^  trigger:", "  status: disabled\n  trigger:")

    before = len(fake.writes)
    run_apply(fake, cp.resolve(["selfdev"]))
    assert fake.writes[before:] == [
        ("PATCH", f"/rules/{rule_id}"),
        ("POST", f"/rules/{rule_id}:disable"),
    ]
    rule = _rule(fake, "docs-drift")
    assert rule["version"] == 2 and rule["status"] == "disabled" and "where" in rule["action"]


def test_work_rule_workspace_cannot_move(sandbox):
    fake = FakeControlPlane()
    run_apply(fake, cp.resolve(["selfdev"]))
    moved = {**SELFDEV_ENV, "SELFDEV_WORKSPACE_ID": "aaaaaaaa-0000-4000-8000-000000000002"}
    with pytest.raises(cp.PackageError, match="workspaceId правила неизменяем"):
        run_apply(fake, cp.resolve(["selfdev"]), env=moved)


def _drop_unused_variables(directory: Path) -> None:
    """Объявления переменных, которые после правки пакета никто не использует, —
    вон из манифеста (иначе variable_unused)."""
    manifest = directory / "package.yaml"
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    used = {
        name
        for path in directory.rglob("*.yaml")
        if path != manifest
        for name in re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", path.read_text(encoding="utf-8"))
    }
    variables = document["spec"].get("variables") or {}
    document["spec"]["variables"] = {k: v for k, v in variables.items() if k in used}
    manifest.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")


def test_work_rule_can_be_retired(sandbox):
    fake = FakeControlPlane()
    run_apply(fake, cp.resolve(["selfdev"]))
    shutil.rmtree(sandbox / "selfdev" / "rules")
    # сценарии правил без самих правил — ошибка check; у живого selfdev они есть (S023)
    shutil.rmtree(sandbox / "selfdev" / "tests", ignore_errors=True)
    _drop_unused_variables(sandbox / "selfdev")
    retire = {"WorkRule": [r["key"] for r in fake.rows["rules"]]}
    run_apply(fake, cp.Installation(cp.resolve(["selfdev"]).packages, retire))
    assert {r["status"] for r in fake.rows["rules"]} == {"archived"}


def test_work_rule_references_and_grammar_are_checked(sandbox):
    path = sandbox / "selfdev" / "rules" / "docs-drift.yaml"
    _edit(path, r"skill: docs\.drift_check@2", "skill: docs.drift_check@9")
    _edit(path, r"condition: \{eq:", "condition: {like:")
    errors, _ = cp.check(cp.resolve(["selfdev"]))
    assert any("interpretation.skill 'docs.drift_check@9'" in e for e in errors)
    assert any("invalid_rule_condition" in e for e in errors)


# --- фикстура второго домена (spec notifications, сценарий 6; SC-006) ----------

INVOICE_ENV = {
    "NOTIFICATION_SERVICE_URL": "https://notify.example",
    "INVOICE_WORKSPACE_ID": str(uuid.UUID(int=11)),
    "FINANCE_DIRECTOR_ROLE_ID": str(uuid.UUID(int=12)),
    "ACCOUNTING_ROLE_ID": str(uuid.UUID(int=13)),
    # база знаний компании: invoice-payment на живом дереве требует company-knowledge (S014)
    "KNOWLEDGE_WORKSPACE_ID": str(uuid.UUID(int=14)),
}
# Понятия разработки, которых в пакете другого домена быть не должно.
DEV_VOCABULARY = re.compile(
    r"\b(git|branch|commit|merge|repositor\w*|code-review|coding-task)\b", re.I
)


def test_invoice_payment_is_data_of_another_domain():
    installation = cp.resolve(["invoice-payment"])
    order = [p.key for p in installation.packages]
    # requires раньше пакета; состав зависимостей — данные дерева, не контракт установщика
    assert order[-1] == "invoice-payment" and "notify" in order
    assert settled(cp.check(installation, env=INVOICE_ENV)) == ([], [])
    for path in (ROOT / "packages" / "invoice-payment").rglob("*.yaml"):
        assert not DEV_VOCABULARY.search(path.read_text(encoding="utf-8")), path


def _core_with_processes():
    """Ядро с планом пакетов (POST /packages:plan и :apply) — поддельное ядро единого плана."""
    from tests.test_install import FakeCore

    core = FakeCore()
    core.workspaces |= {INVOICE_ENV["INVOICE_WORKSPACE_ID"], INVOICE_ENV["KNOWLEDGE_WORKSPACE_ID"]}
    return core


INVOICE_INSTALL_ENV = {**INVOICE_ENV, "TASK_URL_BASE": "https://console.example/tasks"}


def test_invoice_payment_installs_its_catalog_and_leaves_the_process_to_the_plan():
    """0.3.0: согласование и уведомление бухгалтерии — шаги процесса invoice-payment
    (tools/tests/test_invoice_payment_process.py). Каталог, который процесс называет (скиллы,
    роли, типы артефактов), ставит установщик; типы задач, агентов и сам процесс у пакета с
    процессом ставит план ядра (CORE_PLANNED_KINDS)."""
    fake, notify = _core_with_processes(), FakeNotificationService()
    installation = cp.resolve(["invoice-payment"])
    result, _ = run_notify(fake, notify, installation, env=INVOICE_INSTALL_ENV)
    assert {"Skill/notify.send@1", "Role/finance-director", "Role/accounting"} <= set(result)
    owned = {
        o.ref
        for o in installation.objects
        if o.package == "invoice-payment" and o.kind in cp.CORE_PLANNED_KINDS
    }
    assert owned and not owned & set(result)
    assert {
        "TaskType/invoice-payment",
        "TaskType/invoice-review",
        "Process/invoice-payment",
    } <= set(fake.published)
    assert len(fake.apply_calls) == 1  # план ядра — один, у пакета с процессом
    assert "WorkRule/invoice-received" not in fake.published

    skill = next(r for r in fake.rows["skills"] if r["name"] == "notify.send")
    auth = skill["contract"]["implementation"]["auth"]
    assert (
        skill["contract"]["implementation"]["endpoint"]
        == "https://notify.example/api/v1/skills/notify.send"
    )
    assert auth == {"audience": "notification-service", "scopes": ["notifications:send"]}

    task_type = next(o for o in installation.objects if o.ref == "TaskType/invoice-payment")
    assert not task_type.spec.get("approvalSchema")


# --- открытая поставка (фича oss-sync) ------------------------------------------


def test_oss_rules_pair_on_one_key_and_feed_oss_publish_its_inputs():
    installation = cp.resolve(["selfdev"])
    objects = {(o.kind, o.key): o.spec for o in installation.objects}
    red = objects[("WorkRule", "oss-check-red")]["action"]
    resolved = objects[("WorkRule", "oss-check-resolved")]["action"]
    assert red["dedupKeyTemplate"] == resolved["dedupKeyTemplate"] == "oss-check:{{item.component}}"
    assert (red["forEach"], resolved["forEach"]) == ("skill.output.failing", "skill.output.current")

    publish_type = objects[("TaskType", "oss-publish")]
    assert publish_type["execution"] == {
        "skill": "oss.publish",
        "version": "2",
        "inputs": "$.customFields",
    }
    publish_skill = objects[("Skill", "oss.publish")]
    required = set(publish_skill["contract"]["inputs"]["required"])
    assert set(publish_type["fieldSchema"]["required"]) == required
    assert set(publish_type["fieldSchema"]["properties"]) == set(
        publish_skill["contract"]["inputs"]["properties"]
    )
    for rule in ("oss-sync", "oss-check-red", "oss-check-resolved", "oss-release-sync"):
        assert objects[("WorkRule", rule)]["interpretation"]["skill"] == "oss.check@2", rule

    # релиз, поставленный после закрепления указателя, подхватывает расписание:
    # то же действие и тот же dedup-ключ — одна работа на релиз от двух правил
    sync = objects[("WorkRule", "oss-sync")]
    scheduled = objects[("WorkRule", "oss-release-sync")]
    assert scheduled["trigger"] == {"kind": "schedule", "type": "interval", "everySeconds": 3600}
    assert "ref" not in scheduled["interpretation"]["inputs"]
    assert scheduled["action"]["fields"]["customFields"] == sync["action"]["fields"]["customFields"]
    for key in ("kind", "taskType", "forEach", "dedupKeyTemplate"):
        assert scheduled["action"][key] == sync["action"][key], key
    assert sync["action"]["dedupKeyTemplate"] == "oss-publish:{{item.component}}:{{item.sha}}"
    fed = set(sync["action"]["fields"]["customFields"])
    assert required <= fed <= set(publish_skill["contract"]["inputs"]["properties"])
    assert "ossBranch" not in fed


# --- типы артефактов (CP-ADR-0072, artifact-handoff A008) ----------------------


def _lifecycle() -> dict:
    devops = cp.load_package(ROOT / "packages" / "selfdev")
    return copy.deepcopy(
        next(o for o in devops.objects if o.key == "devops").spec["lifecycleSchema"]
    )


def _artifact_package(
    sandbox: Path,
    *,
    slot_type: str = "report-document",
    slot_media: list[str] | None = None,
    media: list[str] | None = None,
) -> None:
    output = {"key": "report", "type": slot_type, "required": True}
    if slot_media:
        output["mediaTypes"] = slot_media
    _package_with(
        sandbox,
        "docs",
        [
            {
                "kind": "ArtifactType",
                "key": "report-document",
                "spec": {
                    "displayName": "Отчёт",
                    "mediaTypes": media or ["text/markdown", "application/pdf"],
                    "metadataSchema": {
                        "type": "object",
                        "properties": {"period": {"type": "string"}},
                    },
                },
            },
            {
                "kind": "TaskType",
                "key": "write-report",
                "spec": {
                    "displayName": "Написать отчёт",
                    "lifecycleSchema": _lifecycle(),
                    "artifactSchema": {"outputs": [output]},
                },
            },
        ],
    )


def test_artifact_types_pass_check_and_go_before_task_types(sandbox):
    _artifact_package(sandbox, slot_media=["text/markdown"])
    installation = cp.resolve(["docs"])
    assert cp.check(installation)[0] == []
    fake = FakeControlPlane()
    result, _ = run_apply(fake, installation)
    assert fake.writes[:2] == [("POST", "/artifact-types"), ("POST", "/task-types")]
    assert result["ArtifactType/report-document"]["version"] == 1
    task_type = fake.rows["task-types"][0]
    assert task_type["artifactSchema"]["outputs"][0]["type"] == "report-document"


def test_artifact_schema_references_are_checked(sandbox):
    _artifact_package(sandbox, slot_type="report-doc")
    errors, _ = cp.check(cp.resolve(["docs"]))
    assert any("'report-doc'" in e and "не объявлен" in e for e in errors)


def test_output_media_types_must_narrow_the_artifact_type(sandbox):
    _artifact_package(sandbox, slot_media=["image/png"])
    errors, _ = cp.check(cp.resolve(["docs"]))
    assert any("image/png" in e and "шире" in e for e in errors)


def test_artifact_type_definition_is_checked_by_the_core_validator(sandbox):
    _artifact_package(sandbox, media=["markdown"])
    errors, _ = cp.check(cp.resolve(["docs"]))
    assert any("mediaTypes" in e for e in errors)


def test_artifact_type_apply_is_idempotent_and_changes_publish_a_version(sandbox):
    _artifact_package(sandbox)
    installation = cp.resolve(["docs"])
    fake = FakeControlPlane()
    run_apply(fake, installation)
    writes = len(fake.writes)
    _, lines = run_apply(fake, installation)
    assert len(fake.writes) == writes, lines  # maxBytes по умолчанию (лимит стенда) — не отличие
    _edit(sandbox / "docs" / "obj0.yaml", r'"mediaTypes":', '"maxBytes": 1024, "mediaTypes":')
    result, lines = run_apply(fake, cp.resolve(["docs"]))
    versions = {r["version"]: r["status"] for r in fake.rows["artifact-types"]}
    assert versions == {1: "active", 2: "active"}  # у типов артефактов нет :deprecate
    assert result["ArtifactType/report-document"]["version"] == 2
    assert any("изменились maxBytes" in line for line in lines)


def test_artifact_type_is_exported_as_a_package_document():
    body = {
        "key": "report-document",
        "version": 2,
        "displayName": "Отчёт",
        "description": "",
        "metadataSchema": {},
        "mediaTypes": ["text/markdown"],
        "maxBytes": 1024,
        "status": "active",
    }
    document = cp.to_document("ArtifactType", "report-document", body)
    assert document["spec"] == {
        "displayName": "Отчёт",
        "mediaTypes": ["text/markdown"],
        "maxBytes": 1024,
    }
    assert cp.FOLDERS["ArtifactType"] == "artifact-types"


def test_sdd_hands_documents_on_as_artifacts_not_paths():
    """SC-001 artifact-handoff: feature-tasks gets spec and plan as inputs, no path in git."""
    objects = {(o.kind, o.key): o for o in cp.resolve(["sdd"]).objects}
    design = objects[("TaskType", "feature-design")].spec["artifactSchema"]
    assert {o["key"]: o["type"] for o in design["outputs"]} == {
        "spec": "spec-document",
        "plan": "plan-document",
    }
    tasks = objects[("TaskType", "feature-tasks")].spec
    assert {(i["key"], i["from"], i["required"]) for i in tasks["artifactSchema"]["inputs"]} == {
        ("spec", "spawned_by", True),
        ("plan", "spawned_by", True),
    }
    # spec и plan — только входы; в ветку фичи коммитится лишь сам документ задач: его
    # форму читает tasks.check@1 (TAI-ADR-0053, отступление C008 от «читает артефакт»)
    for document in ("spec.md", "plan.md"):
        assert f"specs/<featureSlug>/{document}" not in tasks["instructions"], document
    assert "specs/" not in tasks["description"]
    converge = objects[("TaskType", "feature-converge")].spec
    assert {i["key"] for i in converge["artifactSchema"]["inputs"]} == {"spec", "tasks"}
    assert "specs/<slug>" not in converge["instructions"]
    # описание feature-tasks, которое пишет исход ворот дизайна, тоже без пути
    outcome = objects[("TaskType", "feature-design")].spec["approvalSchema"]
    assert "specs/" not in str(outcome["gates"]["default"]["outcomes"]["approved"])


def test_invoice_payment_hands_a_review_pdf_to_the_payment():
    objects = {(o.kind, o.key): o for o in cp.resolve(["invoice-payment"]).objects}
    assert objects[("ArtifactType", "invoice-review")].spec["mediaTypes"] == ["application/pdf"]
    review = objects[("TaskType", "invoice-review")].spec["artifactSchema"]["outputs"]
    assert review == [{"key": "review", "type": "invoice-review", "required": True}]
    payment = objects[("TaskType", "invoice-payment")].spec["artifactSchema"]["inputs"]
    assert payment == [
        {"key": "review", "type": "invoice-review", "from": "depends_on", "required": False}
    ]


# --- агенты (TAI-ADR-0052, CP-ADR-0073, declarative-agents D011) ----------------


def _agent(fake: FakeControlPlane, key: str) -> dict:
    return next(a for a in fake.rows["agents"] if a["key"] == key)


def _agent_doc(**over) -> dict:
    spec = {
        "displayName": "Coder",
        "identity": {"kind": "agent", "permissions": ["tasks.read", "tasks.claim"]},
        "work": {"workspace": "${SELFDEV_WORKSPACE_ID}", "taskTypes": ["coding-task"]},
        "executor": {"kind": "claude-code", "params": {"model": "model-a"}},
        "placement": {"requires": ["claude-subscription"], "secrets": ["claude-oauth-token"]},
    }
    spec.update(over)
    return {"kind": "Agent", "key": "probe-coder", "spec": spec}


def _agent_package(sandbox: Path, **over) -> None:
    _package_with(sandbox, "crew", [_agent_doc(**over)])
    manifest = sandbox / "crew" / "package.yaml"
    manifest.write_text(
        manifest.read_text().replace('"requires": []', '"requires": ["selfdev"]'), encoding="utf-8"
    )


def test_agent_goes_after_task_types_and_checks_their_references(sandbox):
    assert cp.CATALOG_KINDS.index("Agent") > cp.CATALOG_KINDS.index("TaskType")
    _agent_package(sandbox)
    assert cp.check(cp.resolve(["crew"]), env=SELFDEV_ENV)[0] == []
    _agent_package_path = sandbox / "crew"
    shutil.rmtree(_agent_package_path)
    _agent_package(
        sandbox, work={"workspace": "${SELFDEV_WORKSPACE_ID}", "taskTypes": ["codin-task"]}
    )
    errors, _ = cp.check(cp.resolve(["crew"]), env=SELFDEV_ENV)
    assert any("codin-task" in e for e in errors)


def test_agent_apply_is_idempotent_and_state_does_not_make_a_revision(sandbox):
    _agent_package(sandbox)
    fake = FakeControlPlane()
    installation = cp.resolve(["crew"])
    result, _ = run_apply(fake, installation)
    assert result["Agent/probe-coder"]["revision"] == 1
    writes = list(fake.writes)
    _, lines = run_apply(fake, installation)
    assert fake.writes == writes  # only :validate the second time — it changes nothing
    assert any("ревизия 1 без изменений" in line for line in lines)

    placement = {**_agent_doc()["spec"]["placement"], "replicas": 2}
    shutil.rmtree(sandbox / "crew")
    _agent_package(sandbox, state="stopped", placement=placement)
    result, _ = run_apply(fake, cp.resolve(["crew"]))
    stored = _agent(fake, "probe-coder")
    assert result["Agent/probe-coder"]["revision"] == 1 and (
        stored["state"],
        stored["replicas"],
    ) == ("stopped", 2)

    shutil.rmtree(sandbox / "crew")
    _agent_package(
        sandbox,
        state="stopped",
        placement=placement,
        executor={"kind": "claude-code", "params": {"model": "model-b"}},
    )
    result, _ = run_apply(fake, cp.resolve(["crew"]))
    assert result["Agent/probe-coder"]["revision"] == 2


def test_agent_plan_writes_nothing_but_validate(sandbox):
    _agent_package(sandbox)
    fake = FakeControlPlane()
    document, _ = plan_only(fake, cp.resolve(["crew"]))
    assert fake.writes == []  # the plan only validates
    assert ("/agents:validate", "probe-coder") in [
        (path, body["key"]) for path, body in fake.agent_requests
    ]
    change = next(c for c in catalog_changes(document) if c["key"] == "probe-coder")
    assert (change["kind"], change["operation"]) == ("Agent", "create")


def test_agent_retire_stops_it_once(sandbox):
    _agent_package(sandbox)
    fake = FakeControlPlane()
    run_apply(fake, cp.resolve(["crew"]))
    shutil.rmtree(sandbox / "crew")
    _package_with(sandbox, "crew", [])
    _, lines = run_apply(fake, cp.resolve(["crew"], {"Agent": ["probe-coder"]}))
    assert _agent(fake, "probe-coder")["status"] == "retired"
    _, lines = run_apply(fake, cp.resolve(["crew"], {"Agent": ["probe-coder"]}))
    assert any("уже выведен" in line for line in lines)


@pytest.mark.parametrize(
    "executor",
    [
        {
            "kind": "claude-code",
            "params": {"model": "model-a", "permissionMode": "bypassPermissions"},
        },
        {"kind": "codex", "params": {"sandbox": "read-only"}},
        {"kind": "skills"},
    ],
)
def test_agent_export_round_trips_the_package_file(sandbox, executor):
    """SC-007: выгрузка из ядра совпадает с применённым файлом пакета."""
    doc = _agent_doc(
        executor=executor,
        state="stopped",
        work={"workspace": "${SELFDEV_WORKSPACE_ID}", "taskTypes": ["task"]},
    )
    doc["spec"]["placement"]["replicas"] = 3
    _package_with(sandbox, "crew", [doc])
    fake = FakeControlPlane()
    run_apply(
        fake,
        cp.resolve(["crew"]),
        env={**SELFDEV_ENV, "SELFDEV_WORKSPACE_ID": "${SELFDEV_WORKSPACE_ID}"},
    )
    exported = cp.to_document("Agent", "probe-coder", cp.agent_body(_agent(fake, "probe-coder")))
    assert exported == {"apiVersion": cp.API_VERSION, **doc}


def test_identity_only_agent_round_trips(sandbox):
    doc = {
        "kind": "Agent",
        "key": "bridge",
        "spec": {
            "displayName": "Bridge",
            "identity": {"kind": "service", "permissions": ["tasks.read"]},
            "placement": "none",
        },
    }
    _package_with(sandbox, "crew", [doc])
    fake = FakeControlPlane()
    run_apply(fake, cp.resolve(["crew"]))
    assert cp.to_document("Agent", "bridge", cp.agent_body(_agent(fake, "bridge"))) == {
        "apiVersion": cp.API_VERSION,
        **doc,
    }


# --- правила уведомлений (TAI-ADR-0053, ADR-0005 notification-service; C011) ---


class FakeNotificationService:
    """Та часть API правил сервиса уведомлений, которой пользуется установщик (ADR-0005 §5–7):
    версия — по равенству спецификации, :validate ничего не пишет, :retire выводит ключ."""

    UNKNOWN_EVENT = "no.such_event"  # такого типа нет в каталоге событий ядра

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.writes: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str]] = []

    def _newest(self, key):
        versions = [r for r in self.rows if r["key"] == key]
        return max(versions, key=lambda r: r["version"]) if versions else None

    def call(self, method, path, body=None, headers=None):
        assert path.startswith("/api/v1/notification-rules"), path
        assert (headers or {}).get("Authorization") == "Bearer n", (
            "токен audience notification-service"
        )
        parsed = urllib.parse.urlparse(path[len("/api/v1") :])
        query = dict(urllib.parse.parse_qsl(parsed.query))
        route = parsed.path
        self.calls.append((method, route))
        if method == "GET" and route == "/notification-rules":
            states = ("active", "retired") if query.get("includeRetired") == "true" else ("active",)
            heads = [self._newest(k) for k in sorted({r["key"] for r in self.rows})]
            items = [
                h for h in heads if h["state"] in states and query.get("key", h["key"]) == h["key"]
            ]
            return {"items": copy.deepcopy(items), "nextCursor": None}
        if method == "POST" and route == "/notification-rules:validate":
            if body["spec"]["on"]["type"] == self.UNKNOWN_EVENT:
                raise RuntimeError(
                    "POST /api/v1/notification-rules:validate: HTTP 422: invalid_notification_rule"
                )
            current = self._newest(body["key"])
            same = (
                current is not None
                and current["state"] == "active"
                and current["spec"] == body["spec"]
            )
            return {"valid": True, "specHash": "sha256:x", "changed": not same}
        self.writes.append((method, route))
        if method == "POST" and route == "/notification-rules":
            current = self._newest(body["key"])
            if current is not None and current["state"] == "active":
                if current["spec"] == body["spec"]:
                    return copy.deepcopy(current)
                current["state"] = "superseded"
            row = {
                "key": body["key"],
                "version": (current["version"] + 1) if current else 1,
                "spec": copy.deepcopy(body["spec"]),
                "specHash": "sha256:x",
                "state": "active",
            }
            self.rows.append(row)
            return copy.deepcopy(row)
        if method == "POST" and route.endswith(":retire"):
            current = self._newest(route.split("/")[2].rsplit(":", 1)[0])
            if current is None:
                raise RuntimeError("HTTP 404: notification rule not found")
            if current["state"] == "active":
                current["state"] = "retired"
            return copy.deepcopy(current)
        raise AssertionError(f"не поддержано: {method} {path}")


NOTIFY_ENV = {"TASK_URL_BASE": "https://console.example/tasks", **SELFDEV_ENV}


def _notification_rule_doc(key: str = "approval-requested", **over) -> dict:
    spec = {
        "description": "Назначенному решающему — запрос решения с кнопками",
        "on": {"type": "approval.requested"},
        "recipient": {"kind": "assigned"},
        "notification": {
            "type": "approval.requested",
            "title": "Нужно решение: {{task.publicId}} {{task.title}}",
            "links": [{"label": "Открыть задачу", "url": "${TASK_URL_BASE}/{{task.publicId}}"}],
            "actions": ["approvalDecide"],
        },
        "dedupKeyTemplate": "control-plane:approval:{{event.entityId}}",
        "close": {"on": ["approval.approved", "approval.rejected", "approval.cancelled"]},
    }
    spec.update(over)
    return {"kind": "NotificationRule", "key": key, "spec": spec}


def run_notify(fake, notify, installation, *, env=NOTIFY_ENV):
    return run_apply(fake, installation, env=env, notify=(notify, {"Authorization": "Bearer n"}))


def plan_notify(fake, notify, installation, *, env=NOTIFY_ENV):
    return plan_only(fake, installation, env=env, notify=(notify, {"Authorization": "Bearer n"}))


def _rule_version(notify: FakeNotificationService, key: str) -> int:
    return max(r["version"] for r in notify.rows if r["key"] == key and r["state"] == "active")


def test_notification_rule_is_a_catalog_kind_applied_last(sandbox):
    assert cp.CATALOG_KINDS[-1] == "NotificationRule"
    assert (
        cp.FOLDERS["NotificationRule"] == "notification-rules"
        and "NotificationRule" in cp.RETIRABLE
    )
    _package_with(sandbox, "alerts", [_notification_rule_doc()])
    assert settled(cp.check(cp.resolve(["alerts"]), env=NOTIFY_ENV)) == ([], [])


def test_notification_rule_is_checked_by_the_schema_and_the_condition_grammar(sandbox):
    """$defs.notificationRuleSpec: лишнее поле и неизвестный адресат — отказ; on.when —
    грамматика правил ядра с корнями payload, event, task."""
    _package_with(
        sandbox,
        "alerts",
        [
            _notification_rule_doc("extra", extra=True),
            _notification_rule_doc("nobody", recipient={"kind": "everyone"}),
            _notification_rule_doc(
                "bad-when", on={"type": "approval.requested", "when": {"like": [1, 2]}}
            ),
            _notification_rule_doc(
                "bad-root", on={"type": "approval.requested", "when": {"var": "item.x"}}
            ),
        ],
    )
    errors, _ = cp.check(cp.resolve(["alerts"]), env=NOTIFY_ENV)
    assert any("obj0.yaml" in e and "extra" in e for e in errors)
    assert any("obj1.yaml" in e and "everyone" in e for e in errors)
    assert any("obj2.yaml" in e and "invalid_rule_condition" in e for e in errors)
    assert any("obj3.yaml" in e and "invalid_rule_condition" in e for e in errors)


def test_notification_rule_is_validated_then_published_only_when_changed(sandbox):
    _package_with(sandbox, "alerts", [_notification_rule_doc(), _notification_rule_doc("second")])
    fake, notify = FakeControlPlane(), FakeNotificationService()
    installation = cp.resolve(["alerts"])
    _, lines = run_notify(fake, notify, installation)
    # сначала :validate всех правил пакета, потом запись
    assert notify.calls[:2] == [("POST", "/notification-rules:validate")] * 2
    assert notify.writes == [("POST", "/notification-rules")] * 2
    assert _rule_version(notify, "approval-requested") == 1
    stored = notify.rows[0]["spec"]
    assert (
        stored["notification"]["links"][0]["url"]
        == "https://console.example/tasks/{{task.publicId}}"
    )
    assert fake.writes == []  # в ядро правило уведомления не пишется
    assert any(
        "NotificationRule/approval-requested: опубликована v1 (нет в сервисе)" in line
        for line in lines
    )

    _, lines = run_notify(fake, notify, installation)
    assert len(notify.writes) == 2  # без изменений — только :validate и чтение
    assert any("NotificationRule/approval-requested: v1 без изменений" in line for line in lines)

    changed = _notification_rule_doc()
    changed["spec"]["notification"]["title"] = "Решение: {{task.title}}"
    shutil.rmtree(sandbox / "alerts")
    _package_with(sandbox, "alerts", [changed, _notification_rule_doc("second")])
    _, lines = run_notify(fake, notify, cp.resolve(["alerts"]))
    assert _rule_version(notify, "approval-requested") == 2
    assert any("опубликована v2 (изменилась спецификация)" in line for line in lines)


def test_notification_rule_the_service_refuses_stops_the_installation_before_writes(sandbox):
    _package_with(
        sandbox,
        "alerts",
        [
            {"kind": "Role", "key": "on-call", "spec": {"name": "On call"}},
            _notification_rule_doc(),
            _notification_rule_doc("broken", on={"type": FakeNotificationService.UNKNOWN_EVENT}),
        ],
    )
    fake, notify = FakeControlPlane(), FakeNotificationService()
    with pytest.raises(
        cp.PackageError, match="NotificationRule/broken: сервис уведомлений не принимает"
    ):
        run_notify(fake, notify, cp.resolve(["alerts"]))
    assert fake.writes == [] and notify.writes == []


def test_notification_rule_plan_only_validates(sandbox):
    _package_with(sandbox, "alerts", [_notification_rule_doc()])
    fake, notify = FakeControlPlane(), FakeNotificationService()
    document, _ = plan_notify(fake, notify, cp.resolve(["alerts"]))
    assert notify.writes == [] and ("POST", "/notification-rules:validate") in notify.calls
    (change,) = next(s for s in document["sections"] if s["kind"] == "notification-rules")[
        "changes"
    ]
    assert (change["key"], change["operation"], change["detail"]) == (
        "approval-requested",
        "create",
        "новая версия (нет в сервисе)",
    )


def test_notification_rule_can_be_retired(sandbox):
    _package_with(sandbox, "alerts", [_notification_rule_doc()])
    fake, notify = FakeControlPlane(), FakeNotificationService()
    run_notify(fake, notify, cp.resolve(["alerts"]))
    shutil.rmtree(sandbox / "alerts")
    _package_with(sandbox, "alerts", [])
    retire = {"NotificationRule": ["approval-requested", "never-applied"]}
    assert cp.check(cp.resolve(["alerts"], retire))[0] == []
    _, lines = run_notify(fake, notify, cp.resolve(["alerts"], retire))
    assert notify.rows[0]["state"] == "retired"
    assert notify.writes[-1] == ("POST", "/notification-rules/approval-requested:retire")
    assert any("never-applied: нет в сервисе уведомлений" in line for line in lines)
    before = len(notify.writes)
    _, lines = run_notify(fake, notify, cp.resolve(["alerts"], retire))
    assert len(notify.writes) == before and any("уже выведено из оборота" in line for line in lines)


def test_notification_rule_without_the_service_is_refused_before_any_write(sandbox):
    """Прежний путь без плана пропускал правила с предупреждением и ставил остальное; единый
    план без сервиса уведомлений не строится вовсе — ни каталог, ни правила не пишутся."""
    _package_with(
        sandbox,
        "alerts",
        [{"kind": "Role", "key": "on-call", "spec": {"name": "On call"}}, _notification_rule_doc()],
    )
    fake = FakeControlPlane()
    for retire in ({}, {"NotificationRule": ["old"]}):
        with pytest.raises(cp.PackageError, match="нужен сервис уведомлений"):
            run_apply(fake, cp.resolve(["alerts"], retire), env=NOTIFY_ENV)
    assert fake.writes == [] and fake.records == []


def test_notification_rule_export_round_trips_the_package_file(sandbox):
    """FR-016: выгрузка из сервиса совпадает с файлом пакета; ключ on — в кавычках (YAML 1.1)."""
    doc = _notification_rule_doc(
        status="disabled",
        recipient={"kind": "role", "ref": "payload.role", "fallback": "taskOwner"},
    )
    _package_with(sandbox, "alerts", [doc])
    fake, notify = FakeControlPlane(), FakeNotificationService()
    run_notify(
        fake,
        notify,
        cp.resolve(["alerts"]),
        env={**NOTIFY_ENV, "TASK_URL_BASE": "${TASK_URL_BASE}"},
    )
    applier = cp.Applier(
        fake, {}, log=lambda _m: None, notify=notify, notify_headers={"Authorization": "Bearer n"}
    )
    body = cp._fetch(applier, "NotificationRule", "approval-requested", None)
    exported = cp.to_document("NotificationRule", "approval-requested", body)
    assert exported == {"apiVersion": cp.API_VERSION, **doc}
    text = cp.dump_document(exported, "../../schema/v1/object.schema.json")
    # ключ `on` — в двойных кавычках, как в файлах формата (TASK-001253)
    assert '"on":' in text and cp.yaml.safe_load(text) == exported
    with pytest.raises(cp.PackageError, match="не найден"):
        cp._fetch(applier, "NotificationRule", "missing", None)


def test_notification_token_is_the_same_credential_for_another_audience(monkeypatch):
    """Переменная NOTIFY_TOKEN — как CP_TOKEN; без неё — обмен IAM credential клиента ядра
    на audience notification-service и scope notifications:admin."""
    static = cp._bearer_for(
        "notification-service",
        cp.NOTIFY_SCOPES,
        fallback="NOTIFY_TOKEN",
        environ={"NOTIFY_TOKEN": "t"},
    )
    assert static.token() == "t" and static.static
    iam = pytest.importorskip("control_plane_client.iam")
    seen = {}

    class Credential:
        async def token(self):
            return "exchanged"

    def from_environment(environ):
        seen.update(environ)
        return Credential()

    monkeypatch.setattr(iam, "iam_credential_from_environment", from_environment)
    token = cp._bearer_for(
        "notification-service",
        cp.NOTIFY_SCOPES,
        fallback="NOTIFY_TOKEN",
        environ={"CONTROL_PLANE_IAM_URL": "https://iam.example"},
    ).token()
    assert token == "exchanged"
    assert seen[iam.ENV_IAM_AUDIENCE] == "notification-service"
    assert seen[iam.ENV_IAM_SCOPES] == "notifications:admin"


# --- личность правила и agent:<key> (CP-ADR-0063 Г1, CP-ADR-0073 А1; C011) -----


def _rules_agent_doc(key: str = "rules-bot") -> dict:
    return {
        "kind": "Agent",
        "key": key,
        "spec": {
            "displayName": "Rules",
            "identity": {"kind": "service", "permissions": ["tasks.read", "tasks.write"]},
            "placement": "none",
        },
    }


def _identity_rule_doc(**over) -> dict:
    spec = cp._read_yaml(ROOT / "packages" / "selfdev" / "rules" / "docs-drift.yaml")["spec"]
    spec.pop("identity", None)  # у правил selfdev своя личность; фикстура задаёт её сама
    spec.update(over)
    return {"kind": "WorkRule", "key": "docs-drift-bot", "spec": spec}


def _crew_with(sandbox: Path, docs: list[dict]) -> None:
    _package_with(sandbox, "crew", docs)
    manifest = sandbox / "crew" / "package.yaml"
    manifest.write_text(
        manifest.read_text().replace('"requires": []', '"requires": ["selfdev"]'), encoding="utf-8"
    )


def test_work_rule_identity_is_sent_compared_and_exported(sandbox):
    doc = _identity_rule_doc(identity={"agent": "rules-bot"})
    _crew_with(sandbox, [_rules_agent_doc(), doc])
    assert cp.check(cp.resolve(["crew"]), env=SELFDEV_ENV)[0] == []
    fake = FakeControlPlane()
    run_apply(fake, cp.resolve(["crew"]))
    rule = _rule(fake, "docs-drift-bot")
    assert rule["identity"] == {"agent": "rules-bot"}

    before = len(fake.writes)
    run_apply(fake, cp.resolve(["crew"]))
    assert not [w for w in fake.writes[before:] if w[1].startswith("/rules")]  # без изменений

    exported = cp.to_document("WorkRule", "docs-drift-bot", rule)
    assert exported["spec"]["identity"] == {"agent": "rules-bot"}

    # личность снята из файла — PATCH с identity: null (правило снова действует полномочиями включившего)
    shutil.rmtree(sandbox / "crew")
    _crew_with(sandbox, [_rules_agent_doc(), _identity_rule_doc()])
    before = len(fake.writes)
    run_apply(fake, cp.resolve(["crew"]))
    assert [w for w in fake.writes[before:] if w[1].startswith("/rules")] == [
        ("PATCH", f"/rules/{rule['id']}")
    ]
    assert _rule(fake, "docs-drift-bot")["identity"] is None
    assert (
        "identity"
        not in cp.to_document("WorkRule", "docs-drift-bot", _rule(fake, "docs-drift-bot"))["spec"]
    )


def test_work_rule_identity_must_name_an_agent_of_the_package(sandbox):
    _crew_with(sandbox, [_rules_agent_doc(), _identity_rule_doc(identity={"agent": "ghost"})])
    errors, _ = cp.check(cp.resolve(["crew"]), env=SELFDEV_ENV)
    assert any("identity.agent ссылается на агента 'ghost'" in e for e in errors)

    shutil.rmtree(sandbox / "crew")
    _crew_with(sandbox, [_rules_agent_doc(), _identity_rule_doc(identity={"agent": "rules-bot"})])
    errors, _ = cp.check(cp.resolve(["crew"], {"Agent": ["rules-bot"]}), env=SELFDEV_ENV)
    assert any("'rules-bot'" in e and "retire" in e for e in errors)


def test_work_rule_identity_before_the_core_implements_it_is_a_clear_error(sandbox):
    _crew_with(sandbox, [_rules_agent_doc(), _identity_rule_doc(identity={"agent": "rules-bot"})])

    class PendingCore(FakeControlPlane):
        def call(self, method, path, body=None, headers=None):
            if (
                method == "POST"
                and path == "/api/v1/rules"
                and body.get("identity") == {"agent": "rules-bot"}
            ):
                raise RuntimeError("POST /api/v1/rules: HTTP 501: not_implemented identity")
            return super().call(method, path, body, headers)

    with pytest.raises(cp.PackageError, match="WorkRule/docs-drift-bot: ядро ещё не исполняет"):
        run_apply(PendingCore(), cp.resolve(["crew"]))


def test_agent_reference_in_an_assignment_must_name_a_known_agent(sandbox):
    spec = _identity_rule_doc()["spec"]
    known = copy.deepcopy(spec)
    known["action"]["fields"]["assignee"] = "agent:rules-bot"
    unknown = copy.deepcopy(spec)
    unknown["action"]["fields"]["assignee"] = "agent:ghost"
    templated = copy.deepcopy(spec)
    templated["action"]["fields"]["assignee"] = "agent:{{item.agent}}"
    _crew_with(
        sandbox,
        [
            _rules_agent_doc(),
            {"kind": "WorkRule", "key": "known", "spec": known},
            {"kind": "WorkRule", "key": "unknown", "spec": unknown},
            {"kind": "WorkRule", "key": "templated", "spec": templated},
        ],
    )
    errors, _ = cp.check(cp.resolve(["crew"]), env=SELFDEV_ENV)
    # ошибка — только у ссылки на неизвестного агента; шаблон разрешит ядро при исполнении
    assert [e.split(":")[0].rsplit("/", 1)[-1] for e in errors] == ["obj2.yaml"]
    assert "action.fields.assignee ссылается на агента 'ghost'" in errors[0]

    fake = FakeControlPlane()
    shutil.rmtree(sandbox / "crew")
    _crew_with(sandbox, [_rules_agent_doc(), {"kind": "WorkRule", "key": "known", "spec": known}])
    run_apply(fake, cp.resolve(["crew"]))
    assert (
        _rule(fake, "known")["action"]["fields"]["assignee"] == "agent:rules-bot"
    )  # ядру — как есть


def test_agent_reference_in_approval_outcome_work_is_checked(sandbox):
    path = sandbox / "sdd" / "task-types" / "feature-design.yaml"
    _edit(path, r'assignee: "\$\.task\.assigneeId"', 'assignee: "agent:ghost"')
    errors, _ = cp.check(cp.resolve(["sdd"]), env=SELFDEV_ENV)
    assert any(
        "feature-design.yaml" in e and "ensureWork.assignee" in e and "'ghost'" in e for e in errors
    )


def test_agent_cpus_are_whole_numbers():
    """Ядро каноникализирует описание агента без чисел с плавающей точкой
    (non_canonical_value на $.spec.placement.resources.cpus): доли процессора
    не пишем, пока ядро их не принимает."""
    for path in (ROOT / "packages").glob("*/agents/*.yaml"):
        spec = cp._read_yaml(path)["spec"]
        placement = spec.get("placement")
        cpus = (
            ((placement or {}).get("resources") or {}).get("cpus")
            if isinstance(placement, dict)
            else None
        )
        assert cpus is None or isinstance(cpus, int), path


def test_agents_assigned_execution_work_can_invoke_the_skill():
    """Работу с execution (скилл типа задачи) агент исполняет вызовом POST
    /skills/{ref}:invoke: ядро требует право skills.invoke и назначение скилла
    principal'у агента — его делает реестр по spec.skills.invoke (CP-ADR-0073,
    амендмент 2026-09-28). Без них — 403 на каждом прогоне."""
    executions = {}
    for path in (ROOT / "packages").glob("*/task-types/*.yaml"):
        doc = cp._read_yaml(path)
        execution = doc["spec"].get("execution")
        if execution:
            executions[doc["key"]] = f"{execution['skill']}@{execution['version']}"
    assert executions.get("oss-publish") == "oss.publish@2"
    doc = cp._read_yaml(ROOT / "packages" / "selfdev" / "agents" / "oss-publisher.yaml")
    assert "skills.invoke" in doc["spec"]["identity"]["permissions"]
    assert executions["oss-publish"] in doc["spec"]["skills"]["invoke"]


# --- связь объектов с пакетом (CP-ADR-0074 §11, амендмент TASK-000904; TASK-000919) ---


def _expected_record(package: cp.Package, kinds=cp.RECORDED_KINDS_FALLBACK) -> list[dict]:
    """Все объекты пакета видов, которые ядро записывает, по разу на (kind, key)."""
    expected: list[dict] = []
    for obj in package.objects:
        item = {"kind": obj.kind, "key": obj.key}
        if obj.kind in kinds and obj.kind not in cp.PLAN_KINDS and item not in expected:
            expected.append(item)
    return expected


def test_record_is_called_once_per_package_with_every_object_including_unchanged():
    fake = FakeControlPlane()
    installation = cp.resolve(["sdd"])
    # адреса репозиториев, которые проверяют скиллы цикла spec-driven
    env = {
        **SELFDEV_ENV,
        **{
            f"SELFDEV_{name}_URL": f"https://git.example/{name.lower()}.git"
            for name in (
                "CONTROL_PLANE",
                "FLEET",
                "HUMAN_HARNESS",
                "IAM_SERVICE",
                "MEMORY_SERVICE",
                "NOTIFICATION_SERVICE",
                "PACKAGE_SDK",
                "SKILL_SDK",
            )
        },
    }
    run_apply(fake, installation, env=env)
    first = list(fake.records)
    assert [r["package"]["key"] for r in first] == [
        "selfdev",
        "sdd",
    ]  # по разу, в порядке зависимостей
    for record, package in zip(first, installation.packages):
        assert record["package"] == {"key": package.key, "version": str(package.spec["version"])}
        assert record["installHash"] == cp.install_hash(package.path)
        assert record["objects"] == _expected_record(package)
    kinds = {item["kind"] for record in first for item in record["objects"]}
    assert {"TaskType", "WorkRule", "Skill", "Agent"} <= kinds

    # повторная установка ничего не меняет, а связь всё равно называет каждый объект
    writes = list(fake.writes)
    _, lines = run_apply(fake, installation, env=env)
    assert fake.writes == writes
    assert fake.records[2:] == first
    assert any("sdd " in line and "связь с пакетом записана" in line for line in lines)
    assert fake.links[("TaskType", "feature-design")]["key"] == "sdd"
    assert fake.links[("TaskType", "coding-task")]["key"] == "selfdev"


def test_plan_neither_records_nor_asks_the_core_for_kinds():
    fake = FakeControlPlane()
    # ядро без packages:record: план всё равно строится (из OpenAPI он берёт только версию)
    fake.openapi = {"info": {"version": "0.9.0"}, "paths": {}}
    plan_only(fake, cp.resolve(["selfdev"]))
    assert fake.records == [] and fake.writes == []


def test_processes_and_calendars_are_left_to_the_plan_apply():
    fake, notify = _core_with_processes(), FakeNotificationService()
    installation = cp.resolve(["invoice-payment"])
    assert any(o.kind == "Process" for o in installation.objects)
    run_notify(fake, notify, installation, env=INVOICE_INSTALL_ENV)
    # связь записывается по пакету в порядке установки; пакетам без объектов для записи
    # ядра (онтология, правила уведомлений) записывать нечего
    recorded = [r["package"]["key"] for r in fake.records]
    assert recorded[-1] == "invoice-payment" and "notify" in recorded
    assert recorded == [p.key for p in installation.packages if p.key in recorded]
    sent = {item["kind"] for record in fake.records for item in record["objects"]}
    assert not sent & set(cp.PLAN_KINDS) and "NotificationRule" not in sent
    # у пакета с процессом всё, что ставит план ядра, связывает его :apply, а не record
    own = next(r for r in fake.records if r["package"]["key"] == "invoice-payment")
    assert own["objects"] and not {o["kind"] for o in own["objects"]} & set(cp.CORE_PLANNED_KINDS)
    # даже если ядро перечислит их среди допустимых, установщик их не называет
    package = installation.packages[-1]
    assert not {
        o["kind"] for o in cp.record_objects(package, [*cp.RECORDED_KINDS_FALLBACK, "Process"])
    } & set(cp.PLAN_KINDS)

    fake = _core_with_processes()
    _, lines = run_apply(fake, cp.resolve(["platform-calendars"]))
    assert fake.records == []  # в пакете одни календари — связывать через record нечего
    assert any("нечего" in line for line in lines)
    assert len(fake.apply_calls) == 1 and "Calendar/ru" in fake.published


def test_record_error_breaks_apply_with_a_clear_message():
    fake = FakeControlPlane()
    fake.record_error = cp.HttpError("POST /api/v1/packages:record: HTTP 403: forbidden", 403)
    installation = cp.resolve(["selfdev"], {"TaskType": ["ops"]})
    fake.call("POST", "/api/v1/task-types", {"key": "ops", "displayName": "Ops"})
    with pytest.raises(
        cp.PackageError,
        match=r"пакет selfdev .*: объекты применены, но ядро не записало их "
        r"связь с пакетом \(POST /api/v1/packages:record\): .*HTTP 403",
    ):
        run_apply(fake, installation)
    # установка остановлена на связи: retire после неё не выполнялся
    assert {r["status"] for r in fake.rows["task-types"] if r["key"] == "ops"} == {"active"}


def test_record_kinds_come_from_the_core(sandbox):
    """После TASK-000903 ядро планирует TaskType, Agent и WorkRule само и RecordedKind
    сужается: установщик берёт перечень из OpenAPI ядра, а не из своей константы."""
    fake = FakeControlPlane()
    kind = fake.openapi["components"]["schemas"]["PackageRecordedObject"]["properties"]["kind"]
    kind["enum"] = [k for k in kind["enum"] if k not in ("TaskType", "Agent", "WorkRule")]
    installation = cp.resolve(["selfdev"])
    _, lines = run_apply(fake, installation)
    (record,) = fake.records
    assert record["objects"] == _expected_record(installation.packages[0], kind["enum"])
    assert {item["kind"] for item in record["objects"]} == {"Skill"}
    assert any(
        "Agent, TaskType, WorkRule ядро через /packages:record не связывает" in line
        for line in lines
    )


def test_core_without_record_stops_before_any_write():
    fake = FakeControlPlane()
    fake.openapi = {
        "openapi": "3.1.0",
        "info": {"version": "0.9.0"},
        "paths": {"/api/v1/task-types": {}},
    }
    with pytest.raises(cp.PackageError, match="нет POST /api/v1/packages:record.*1a4b4c2"):
        run_apply(fake, cp.resolve(["selfdev"]))
    assert fake.writes == [] and fake.records == []


def test_unreadable_openapi_falls_back_to_the_known_kinds():
    """Без OpenAPI ядра план не строится (версия ядра — из него); перечень видов для
    packages:record, который из OpenAPI не разобрать, — запасной, с предупреждением."""
    fake = FakeControlPlane()
    fake.openapi = None
    with pytest.raises(cp.PackageError, match="версия ядра не прочитана"):
        run_apply(fake, cp.resolve(["selfdev"]))
    assert fake.writes == []

    fake = FakeControlPlane()
    kind = fake.openapi["components"]["schemas"]["PackageRecordedObject"]["properties"]["kind"]
    kind["enum"] = []
    installation = cp.resolve(["selfdev"])
    _, lines = run_apply(fake, installation)
    assert any("OpenAPI ядра не прочитан" in line for line in lines)
    assert fake.records[0]["objects"] == _expected_record(installation.packages[0])


def _recorded_kind_literal() -> tuple[str, ...] | None:
    """RecordedKind из исходника ядра рядом с компонентом (модели ядра тестам без pydantic не импортировать)."""
    import ast

    source = CORE_SOURCE / "control_plane" / "api" / "v1" / "schemas.py"
    if not source.exists():
        return None
    for node in ast.parse(source.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "RecordedKind" for t in node.targets
        ):
            return tuple(ast.literal_eval(elt) for elt in node.value.slice.elts)
    return None


def test_fallback_kinds_and_openapi_snapshot_match_the_core():
    """Запасной перечень видов и снимок OpenAPI сверяются с RecordedKind ядра рядом с компонентом:
    ядро сузило или расширило перечень — обновить константу и снимок."""
    snapshot = json.loads(OPENAPI_SNAPSHOT.read_text(encoding="utf-8"))
    assert cp.recorded_kinds_from_openapi(snapshot) == cp.RECORDED_KINDS_FALLBACK
    core = _recorded_kind_literal()
    if core is None:
        pytest.skip("рядом нет control-plane с RecordedKind")
    assert core == cp.RECORDED_KINDS_FALLBACK


def test_openapi_kind_may_be_a_reference_or_a_union():
    document = {
        "paths": {
            "/api/v1/packages:record": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Req"}}
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "Req": {
                    "properties": {
                        "objects": {"type": "array", "items": {"$ref": "#/components/schemas/Obj"}}
                    }
                },
                "Obj": {"properties": {"kind": {"$ref": "#/components/schemas/RecordedKind"}}},
                "RecordedKind": {"anyOf": [{"const": "Role"}, {"enum": ["Skill", "Role"]}]},
            }
        },
    }
    assert cp.recorded_kinds_from_openapi(document) == ("Role", "Skill")
    assert cp.recorded_kinds_from_openapi({"paths": {}}) is None


def test_install_hash_is_stable_and_covers_paths_and_contents(tmp_path):
    source = ROOT / "packages" / "selfdev"
    first = cp.install_hash(source)
    assert first == cp.install_hash(source) and re.fullmatch(r"sha256:[0-9a-f]{64}", first)

    copy_dir = tmp_path / "elsewhere" / "selfdev"
    shutil.copytree(source, copy_dir)  # другое место, другие метки времени
    for path in copy_dir.rglob("*"):
        if path.is_file():
            path.touch()
    (copy_dir / ".DS_Store").write_bytes(b"finder")  # служебные файлы ОС не в счёт
    assert cp.install_hash(copy_dir) == first

    manifest = copy_dir / "package.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    changed = cp.install_hash(copy_dir)
    assert changed != first
    (copy_dir / "task-types" / "devops.yaml").rename(copy_dir / "task-types" / "devops-2.yaml")
    assert cp.install_hash(copy_dir) != changed  # путь входит в хэш


def test_install_hash_canon(tmp_path):
    """Канон хэша (его же возьмёт contentHash lock package-sdk): отсортированные пути,
    на файл — путь, NUL, длина, NUL, байты."""
    (tmp_path / "b").mkdir()
    (tmp_path / "package.yaml").write_bytes(b"key: demo\n")
    (tmp_path / "b" / "x.yaml").write_bytes(b"")
    expected = hashlib.sha256(
        b"b/x.yaml\x000\x00" + b"package.yaml\x0010\x00key: demo\n"
    ).hexdigest()
    assert cp.install_hash(tmp_path) == "sha256:" + expected


def test_agent_publish_names_its_package(sandbox):
    _agent_package(sandbox)
    fake = FakeControlPlane()
    run_apply(fake, cp.resolve(["crew"]))
    assert _agent(fake, "probe-coder")["revision"]["package"] == {"key": "crew", "version": "0.1.0"}
    requests = [(path, body) for path, body in fake.agent_requests if body["key"] == "probe-coder"]
    assert {path for path, _body in requests} == {"/agents:validate", "/agents"}
    assert all(body["package"] == {"key": "crew", "version": "0.1.0"} for _path, body in requests)
    selfdev = cp.package_ref(cp.resolve(["selfdev"]).packages[0])
    assert all(
        body["package"] == selfdev
        for _path, body in fake.agent_requests
        if body["key"] != "probe-coder"
    )
    assert ("Agent", "probe-coder") in fake.links
