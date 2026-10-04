"""Вид каталога ConnectionType и Agent.spec.connections (TAI-ADR-0061, CP-ADR-0079 §2, §8;
фича integrations-connections): проверка без стенда, единый план, установка, retire и
выгрузка — на поддельном ядре с ``/connection-types``; песочница с пакетом провайдера.

Перенесено из ``tools/tests/test_connection_types.py`` суперпроекта (TASK-001146).
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

import pytest

from package_sdk import install, sandbox, scaffold
from tests.umbrella import test_cp_packages as umbrella
from tests.umbrella._shim import UMBRELLA, cp, settled
from tests.umbrella.test_cp_packages import FakeControlPlane, plan_only, run_apply

CONNECTION_TYPES = "connection-types"


class FakeCore(FakeControlPlane):
    """Поддельное ядро с типами подключений (CP-ADR-0079 §2, §17): пара (key, version)
    неизменяема — повтор с тем же телом отвечает существующей версией, другое тело — 409;
    PATCH key@version меняет только статус, If-Match "connection-type-<rowVersion>". null у
    незаданных полей подставляет тест, которому это нужно (null_fields) — сверх ADR поддельное
    ядро их не отдаёт."""

    def __init__(self) -> None:
        super().__init__()
        self.rows[CONNECTION_TYPES] = []
        # поля spec, которые ядро отдаёт null, если они не заданы (путь через точку)
        self.null_fields: tuple[str, ...] = ()

    def call(self, method, path, body=None, headers=None):
        parsed = (
            urllib.parse.urlparse(path[len("/api/v1") :]) if path.startswith("/api/v1/") else None
        )
        parts = parsed.path.strip("/").split("/") if parsed else []
        if not parts or parts[0] != CONNECTION_TYPES:
            return super().call(method, path, body, headers)
        if method != "GET":
            self.writes.append((method, parsed.path))
        query = dict(urllib.parse.parse_qsl(parsed.query))
        return self._connection_types(method, parts, query, body, headers)

    def _connection_types(self, method, parts, query, body, headers):
        rows = self.rows[CONNECTION_TYPES]
        if method == "GET" and len(parts) == 1:
            items = [
                r
                for r in rows
                if all(str(r.get(k)) == v for k, v in query.items() if k in ("key", "status"))
            ]
            return {"items": copy.deepcopy(items), "nextCursor": None}
        if method == "POST" and len(parts) == 1:
            assert set(body) == {"key", "version", "spec"} and isinstance(body["version"], int)
            assert "version" not in body["spec"]
            spec = copy.deepcopy(body["spec"])
            same = next(
                (r for r in rows if (r["key"], r["version"]) == (body["key"], body["version"])),
                None,
            )
            if same is not None:
                if cp._without_nulls(same["spec"]) != spec:
                    raise cp.HttpError(
                        "POST /api/v1/connection-types: HTTP 409: connection_type_version_exists",
                        409,
                    )
                return copy.deepcopy(same)
            row = {
                "id": str(uuid.uuid4()),
                "key": body["key"],
                "version": body["version"],
                "status": "active",
                "spec": spec,
                "package": None,
                "rowVersion": 1,
            }
            for dotted in self.null_fields:  # как ядро, отдающее null у незаданного поля
                *parents, name = dotted.split(".")
                node = row["spec"]
                for parent in parents:
                    node = node.get(parent) if isinstance(node, dict) else None
                if isinstance(node, dict):
                    node.setdefault(name, None)
            rows.append(row)
            return copy.deepcopy(row)
        key, _, version = parts[1].partition("@")
        if method == "GET":
            found = [
                r
                for r in rows
                if r["key"] == key
                and (str(r["version"]) == version if version else r["status"] == "active")
            ]
            if not found:
                raise cp.HttpError(
                    f"GET /api/v1/connection-types/{parts[1]}: HTTP 404: not found", 404
                )
            return copy.deepcopy(max(found, key=lambda r: r["version"]))
        if method == "PATCH" and version:
            row = next(r for r in rows if r["key"] == key and str(r["version"]) == version)
            if (headers or {}).get("If-Match") != f'"connection-type-{row["rowVersion"]}"':
                raise RuntimeError("HTTP 412: precondition failed")
            assert set(body) == {"status"} and body["status"] in (
                "active",
                "deprecated",
                "disabled",
            )
            row.update(status=body["status"], rowVersion=row["rowVersion"] + 1)
            return copy.deepcopy(row)
        raise AssertionError(f"not supported: {method} connection-types/{parts[1]}")


@pytest.fixture
def packages(tmp_path, monkeypatch):
    """Копия каталога пакетов во временном каталоге — чтобы добавлять пакеты в тестах."""
    root = tmp_path / "packages"
    shutil.copytree(UMBRELLA / "packages", root)
    monkeypatch.setattr(cp, "PACKAGES_DIR", root)
    return root


def _package(root: Path, name: str, docs: list[dict], requires: list[str] | None = None) -> Path:
    directory = root / name
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir()
    manifest = {
        "apiVersion": cp.API_VERSION,
        "kind": "Package",
        "key": name,
        "spec": {"version": "0.1.0", "displayName": name, "requires": requires or []},
    }
    (directory / "package.yaml").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    for doc in docs:
        folder = directory / cp.FOLDERS[doc["kind"]]
        folder.mkdir(exist_ok=True)
        (folder / f"{doc['key']}.yaml").write_text(
            json.dumps({"apiVersion": cp.API_VERSION, **doc}, ensure_ascii=False), encoding="utf-8"
        )
    return directory


def _connection_type(**over: Any) -> dict:
    """Тип подключения провайдера CRM (адрес — нейтральный пример)."""
    spec = {
        "version": 1,
        "displayName": "CRM",
        "auth": ["oauth2", "token"],
        "oauth2": {
            "authorizeUrl": "https://www.crm.example/oauth",
            "tokenUrlTemplate": "https://{account}/oauth2/access_token",
            "accountParam": "referer",
            "authStyle": "in_params",
            "scopes": [],
        },
        "accountField": {"title": "Portal address", "pattern": "^[a-z0-9-]+\\.crm\\.example$"},
        "settingsSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "stageRoles": {"type": "object", "additionalProperties": {"type": "string"}},
                "fields": {"type": "object", "properties": {"inn": {"type": "integer"}}},
            },
        },
        "defaultKey": "crm",
    }
    spec.update(over)
    return {"kind": "ConnectionType", "key": "crm", "spec": spec}


def _connector(connections: Any = ("crm",)) -> dict:
    return {
        "kind": "Agent",
        "key": "crm-connector",
        "spec": {
            "displayName": "CRM connector",
            "identity": {"kind": "service", "permissions": ["observations.write"]},
            "executor": {
                "kind": "observer",
                "params": {"entrypoint": "crm_connector.observer:run"},
            },
            "connections": list(connections),
        },
    }


def _errors(name: str) -> list[str]:
    return cp.check(cp.resolve([name]))[0]


def _sections(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if re.match(r"^ +\[[A-Za-z]+\]$", line)]


# --- вид в SDK ----------------------------------------------------------------------


def test_connection_type_is_a_catalog_kind_right_after_capability():
    assert cp.CATALOG_KINDS.index("ConnectionType") == cp.CATALOG_KINDS.index("Capability") + 1
    assert cp.CATALOG_KINDS.index("ConnectionType") < cp.CATALOG_KINDS.index("Agent")
    assert cp.IDENTITY["ConnectionType"] == "key"
    assert cp.FOLDERS["ConnectionType"] == "connection-types"
    assert "ConnectionType" in cp.RETIRABLE
    document = cp._schema_validator().schema
    assert "ConnectionType" in document["properties"]["kind"]["enum"]
    assert "connectionTypeSpec" in document["$defs"]
    assert "connections" in document["$defs"]["agentSpec"]["properties"]
    assert (
        "ConnectionType"
        in document["$defs"]["installationSpec"]["properties"]["retire"]["properties"]
    )


def test_the_ref_of_a_connection_type_carries_its_version(packages):
    _package(packages, "crm-provider", [_connection_type()])
    [obj] = cp.resolve(["crm-provider"]).objects
    assert obj.ref == "ConnectionType/crm@1"


def test_repository_packages_stay_valid_without_edits():
    """Пакеты каталога не знают о подключениях — схема v1 только добавила вид и поле."""
    installation = cp.resolve([d.name for d in cp.all_package_dirs()])
    assert cp.check(installation)[0] == []
    assert not any(
        o.kind == "ConnectionType" or "connections" in o.spec for o in installation.objects
    )


# --- схема и проверки без ядра -------------------------------------------------------


def test_a_provider_package_passes_check(packages):
    _package(packages, "crm-provider", [_connection_type(), _connector()])
    errors, warnings = cp.check(cp.resolve(["crm-provider"]))
    assert errors == []
    assert not any("connections" in w and "defaultKey" in w for w in warnings)


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"auth": ["oauth2"], "oauth2": None}, "oauth2"),  # oauth2 без описания потока
        ({"auth": ["token"], "oauth2": None, "accountField": None}, "accountField"),
        ({"auth": []}, "auth"),
        ({"auth": ["oauth2", "oauth2"]}, "auth"),
        ({"auth": ["password"]}, "password"),
        ({"version": "1"}, "version"),
        ({"version": 0}, "version"),
        ({"defaultKey": "Crm_Main"}, "defaultKey"),
        ({"settingsSchema": {"type": "array"}}, "settingsSchema"),
        ({"clientSecret": "x"}, "clientSecret"),  # лишнее поле вида
    ],
)
def test_schema_rejects_malformed_connection_types(packages, over, needle):
    doc = _connection_type(**over)
    doc["spec"] = {k: v for k, v in doc["spec"].items() if v is not None}
    _package(packages, "crm-provider", [doc])
    errors = _errors("crm-provider")
    assert errors and any(needle in e for e in errors), errors


def test_account_placeholder_needs_account_param_and_account_field(packages):
    doc = _connection_type(auth=["oauth2"])
    del doc["spec"]["accountField"]
    del doc["spec"]["oauth2"]["accountParam"]
    _package(packages, "crm-provider", [doc])
    errors = _errors("crm-provider")
    assert any("accountField" in e for e in errors), errors
    assert any("accountParam" in e for e in errors), errors
    # без {account} в адресе обмена ни поле, ни параметр не нужны
    doc["spec"]["oauth2"]["tokenUrlTemplate"] = "https://auth.crm.example/oauth2/token"
    _package(packages, "crm-provider", [doc])
    assert _errors("crm-provider") == []


@pytest.mark.parametrize(
    "template",
    [
        "https://localhost/oauth2/token",  # одна метка — имя внутренней сети
        "https://vault/v1",
        "https://10.0.0.1/token",  # IP-литерал: последняя метка числовая
        "https://0x7f.1/token",
        "https://a..b/token",
        "https://crm.example:8443/token",  # порта нет
        "https://user@crm.example/token",  # userinfo нет
        "https://x{account}/oauth2/access_token",  # плейсхолдер — метка целиком
        "https://{account}.-bad.example/token",
        "https://{account}/{tenant}/token",  # единственный плейсхолдер — {account}
    ],
)
def test_token_url_template_host_is_an_external_dns_name(packages, template):
    oauth2 = {
        "authorizeUrl": "https://www.crm.example/oauth",
        "tokenUrlTemplate": template,
        "accountParam": "referer",
        "authStyle": "in_header",
        "scopes": ["crm"],
    }
    _package(packages, "crm-provider", [_connection_type(oauth2=oauth2)])
    errors = _errors("crm-provider")
    assert any("invalid_connection_type" in e and "tokenUrlTemplate" in e for e in errors), errors


@pytest.mark.parametrize(
    "template",
    [
        "https://{account}/oauth2/access_token",
        "https://{account}.crm.example/oauth2/access_token",
        "https://auth.crm.example/oauth2/token?account={account}",
    ],
)
def test_token_url_template_allows_the_account_placeholder(packages, template):
    oauth2 = {
        "authorizeUrl": "https://www.crm.example/oauth",
        "tokenUrlTemplate": template,
        "accountParam": "referer",
        "authStyle": "in_params",
        "scopes": [],
    }
    _package(packages, "crm-provider", [_connection_type(oauth2=oauth2)])
    assert _errors("crm-provider") == []


def test_account_pattern_must_compile(packages):
    field = {"title": "Portal", "pattern": "^([a-z$"}
    _package(packages, "crm-provider", [_connection_type(accountField=field)])
    assert any("accountField.pattern" in e for e in _errors("crm-provider"))


def test_secret_material_is_rejected_in_settings_and_strings(packages):
    """reject_secret_material над settingsSchema и проверка материала над строками spec
    (CP-ADR-0079 §2) — кодом ядра рядом."""
    if cp._domain() is None:
        pytest.skip("the core's domain validators are not importable")
    settings = {"type": "object", "properties": {"apiToken": {"type": "string"}}}
    _package(packages, "crm-provider", [_connection_type(settingsSchema=settings)])
    assert any("secret_material_rejected" in e and "apiToken" in e for e in _errors("crm-provider"))

    leaked = "https://www.crm.example/oauth?client=ghp_" + "a" * 36
    oauth2 = {
        "authorizeUrl": leaked,
        "tokenUrlTemplate": "https://{account}/oauth2/access_token",
        "accountParam": "referer",
        "authStyle": "in_params",
        "scopes": [],
    }
    _package(packages, "crm-provider", [_connection_type(oauth2=oauth2)])
    assert any("secret_material_rejected" in e for e in _errors("crm-provider"))


def test_secret_text_check_without_the_core_module_is_a_warning(packages, monkeypatch):
    """Без модуля redaction ядра строки не проверяются молча — check предупреждает."""
    domain = cp._domain()
    if domain is None:
        pytest.skip("the core's domain validators are not importable")
    domain.redaction = None
    monkeypatch.setattr(cp, "_domain", lambda: domain)
    _package(packages, "crm-provider", [_connection_type()])
    errors, warnings = cp.check(cp.resolve(["crm-provider"]))
    assert errors == []
    assert any("redaction" in w and "reject_secret_text" in w for w in warnings)


def test_agent_connections_are_a_list_of_unique_keys(packages):
    for connections in (["Crm"], ["crm", "crm"], [f"c{i}" for i in range(21)]):
        _package(packages, "crm-provider", [_connection_type(), _connector(connections)])
        assert any("connections" in e for e in _errors("crm-provider")), connections


def test_agent_connection_outside_default_keys_is_a_warning(packages):
    """Подключение заводит администратор, ядро его не ищет — только предупреждение об опечатке."""
    _package(packages, "crm-provider", [_connection_type(), _connector(["crm", "crm-archive"])])
    errors, warnings = cp.check(cp.resolve(["crm-provider"]))
    assert errors == []
    assert any("'crm-archive'" in w and "defaultKey" in w for w in warnings)
    assert not any("'crm'" in w for w in warnings)


def test_a_dependent_package_sees_default_keys_of_its_requires(packages):
    _package(packages, "crm-provider", [_connection_type()])
    _package(packages, "crm-bot", [_connector()], requires=["crm-provider"])
    errors, warnings = cp.check(cp.resolve(["crm-bot"]))
    assert errors == [] and not any("defaultKey" in w for w in warnings)


def test_agent_connections_before_the_core_knows_the_field(packages):
    """Ядро рядом без подключений поля не знает: проверка моделью ядра идёт без него и
    предупреждает; ядро с подключениями проверяет поле своей моделью."""
    domain = cp._domain()
    if domain is None or domain.agent_spec is None:
        pytest.skip("the core's AgentSpec model is not importable")
    _package(packages, "crm-provider", [_connection_type(), _connector()])
    errors, warnings = cp.check(cp.resolve(["crm-provider"]))
    assert errors == []
    knows = "connections" in domain.agent_spec.model_fields
    assert any("CP-ADR-0079 §8" in w for w in warnings) is not knows


def test_retire_of_a_declared_connection_type_is_an_error(packages):
    _package(packages, "crm-provider", [_connection_type()])
    errors, _ = cp.check(cp.resolve(["crm-provider"], {"ConnectionType": ["crm"]}))
    assert any("both declared in the package and retired" in e for e in errors)


# --- единый план, установка, retire, выгрузка ----------------------------------------------


def test_apply_publishes_once_and_is_idempotent(packages):
    _package(packages, "crm-provider", [_connection_type()])
    fake = FakeCore()
    document, _ = plan_only(fake, cp.resolve(["crm-provider"]))
    [change] = umbrella.catalog_changes(document)
    assert (change["kind"], change["key"], change["operation"], change["version"]) == (
        "ConnectionType",
        "crm",
        "create",
        1,
    )
    result, lines = run_apply(fake, cp.resolve(["crm-provider"]))
    [row] = fake.rows[CONNECTION_TYPES]
    assert (row["key"], row["version"], row["status"]) == ("crm", 1, "active")
    assert "version" not in row["spec"] and row["spec"]["defaultKey"] == "crm"
    assert result["ConnectionType/crm@1"] == {"id": row["id"], "version": 1}
    assert "   ConnectionType/crm@1: published (not in tenant)" in lines

    writes = list(fake.writes)
    document, _ = plan_only(fake, cp.resolve(["crm-provider"]))
    assert install.count_changes(document) == 0  # повторный план пуст
    _, lines = run_apply(fake, cp.resolve(["crm-provider"]))
    assert fake.writes == writes
    assert "   ConnectionType/crm@1: unchanged" in lines


def test_nested_nulls_from_the_core_are_unset_fields(packages):
    """Ядро, отдающее null у незаданных вложенных полей (oauth2.accountParam,
    accountField.description): повторная установка — без изменений, выгрузка — без null."""
    doc = _connection_type(
        oauth2={
            "authorizeUrl": "https://www.crm.example/oauth",
            "tokenUrlTemplate": "https://auth.crm.example/oauth2/token",
            "authStyle": "in_header",
            "scopes": ["crm"],
        }
    )
    _package(packages, "crm-provider", [doc])
    fake = FakeCore()
    fake.null_fields = ("description", "oauth2.accountParam", "accountField.description")
    run_apply(fake, cp.resolve(["crm-provider"]))
    stored = fake.rows[CONNECTION_TYPES][0]["spec"]
    assert stored["oauth2"]["accountParam"] is None
    assert stored["accountField"]["description"] is None
    writes = list(fake.writes)
    _, lines = run_apply(fake, cp.resolve(["crm-provider"]))
    assert fake.writes == writes and "   ConnectionType/crm@1: unchanged" in lines

    applier = cp.Applier(fake, {}, log=lambda _m: None)
    exported = cp.to_document(
        "ConnectionType", "crm", cp._fetch(applier, "ConnectionType", "crm", None)
    )
    assert exported == {"apiVersion": cp.API_VERSION, **copy.deepcopy(doc)}
    assert not list(cp._schema_validator().iter_errors(exported))


def test_a_changed_version_is_refused_before_any_write(packages):
    _package(packages, "crm-provider", [_connection_type()])
    fake = FakeCore()
    run_apply(fake, cp.resolve(["crm-provider"]))
    _package(packages, "crm-provider", [_connection_type(displayName="CRM (renamed)")])
    writes = list(fake.writes)
    with pytest.raises(cp.PackageError, match=r"displayName.*bump spec\.version"):
        plan_only(fake, cp.resolve(["crm-provider"]))
    assert fake.writes == writes

    _package(packages, "crm-provider", [_connection_type(version=2, displayName="CRM (renamed)")])
    run_apply(fake, cp.resolve(["crm-provider"]))
    assert [(r["version"], r["status"]) for r in fake.rows[CONNECTION_TYPES]] == [
        (1, "active"),
        (2, "active"),
    ]


def test_plan_shows_connection_types_as_their_own_section(packages):
    _package(
        packages,
        "crm-provider",
        [
            {"kind": "Capability", "key": "crm.read", "spec": {"description": "Read the CRM"}},
            _connection_type(),
            {"kind": "Role", "key": "crm-manager", "spec": {"name": "CRM manager"}},
        ],
    )
    fake = FakeCore()
    document, _ = plan_only(fake, cp.resolve(["crm-provider"]))
    assert fake.writes == []
    lines = install.format_plan(document)
    start = lines.index("catalog:")
    catalog = lines[start + 1 : lines.index("ontologies: no changes")]
    assert _sections(catalog) == ["[Capability]", "[ConnectionType]", "[Role]"]
    at = catalog.index("  [ConnectionType]")
    assert catalog[at + 1] == (
        "  + ConnectionType/crm@1 (crm-provider): version 1 will be published (not in tenant)"
    )

    _, applied = run_apply(fake, cp.resolve(["crm-provider"]))
    assert _sections(applied) == ["[Capability]", "[ConnectionType]", "[Role]"]


def test_deprecated_version_returns_and_disabled_stays(packages):
    _package(packages, "crm-provider", [_connection_type()])
    fake = FakeCore()
    run_apply(fake, cp.resolve(["crm-provider"]))
    row = fake.rows[CONNECTION_TYPES][0]

    row["status"] = "deprecated"
    document, _ = plan_only(fake, cp.resolve(["crm-provider"]))
    [change] = umbrella.catalog_changes(document)
    assert (change["operation"], change["fields"]) == ("patch", ["status"])
    _, lines = run_apply(fake, cp.resolve(["crm-provider"]))
    assert row["status"] == "active" and "   ConnectionType/crm@1: deprecated → active" in lines

    row["status"] = "disabled"
    writes = list(fake.writes)
    document, _ = plan_only(fake, cp.resolve(["crm-provider"]))
    assert install.count_changes(document) == 0
    _, lines = run_apply(fake, cp.resolve(["crm-provider"]))
    assert row["status"] == "disabled" and fake.writes == writes
    assert any("disabled in tenant" in line for line in lines)


def test_a_status_change_after_the_plan_makes_it_stale(packages, tmp_path):
    """План помнит rowVersion версии, которую возвращает в оборот: правка статуса на стенде
    после плана — plan_stale, а не запись поверх."""
    _package(packages, "crm-provider", [_connection_type()])
    fake = FakeCore()
    run_apply(fake, cp.resolve(["crm-provider"]))
    row = fake.rows[CONNECTION_TYPES][0]
    row["status"] = "deprecated"
    path = umbrella._install_file(tmp_path, cp.resolve(["crm-provider"]))
    target, env = umbrella._target(fake), umbrella.SELFDEV_ENV
    document = install.plan(path, target=target, env=env, log=lambda _m: None)
    row["rowVersion"] += 1  # статус правили в консоли после плана
    writes = list(fake.writes)
    with pytest.raises(cp.PackageError, match="plan_stale"):
        install.apply(
            document, target=target, env=env, install=path, assume_yes=True, log=lambda _m: None
        )
    assert fake.writes == writes and row["status"] == "deprecated"


def test_retire_deprecates_every_active_version_once(packages):
    _package(packages, "crm-provider", [_connection_type()])
    fake = FakeCore()
    run_apply(fake, cp.resolve(["crm-provider"]))
    _package(packages, "crm-provider", [_connection_type(version=2)])
    run_apply(fake, cp.resolve(["crm-provider"]))
    _package(packages, "crm-provider", [])
    retiring = cp.resolve(["crm-provider"], {"ConnectionType": ["crm"]})

    document, _ = plan_only(fake, retiring)
    assert all(r["status"] == "active" for r in fake.rows[CONNECTION_TYPES])
    lines = install.format_plan(document)
    start = lines.index("retirement:")
    assert lines[start + 1 : start + 4] == [
        "  [ConnectionType]",
        "  - ConnectionType/crm@1: v1 → deprecated (retire): no new connections, "
        "existing ones keep working",
        "  - ConnectionType/crm@2: v2 → deprecated (retire): no new connections, "
        "existing ones keep working",
    ]

    run_apply(fake, retiring)
    assert [r["status"] for r in fake.rows[CONNECTION_TYPES]] == ["deprecated", "deprecated"]
    writes = list(fake.writes)
    _, lines = run_apply(fake, retiring)
    assert fake.writes == writes and "   ConnectionType/crm: already retired" in lines


def test_export_round_trips_the_package_file(packages):
    """Выгрузка из ядра совпадает с применённым файлом пакета (как у остальных видов)."""
    doc = _connection_type()
    _package(packages, "crm-provider", [doc])
    fake = FakeCore()
    run_apply(fake, cp.resolve(["crm-provider"]))
    applier = cp.Applier(fake, {}, log=lambda _m: None)
    body = cp._fetch(applier, "ConnectionType", "crm", None)
    assert cp.to_document("ConnectionType", "crm", body) == {
        "apiVersion": cp.API_VERSION,
        **copy.deepcopy(doc),
    }
    assert cp._fetch(applier, "ConnectionType", "crm", "1") == body
    with pytest.raises(cp.PackageError, match="not found"):
        cp._fetch(applier, "ConnectionType", "crm", "7")


def test_agent_with_connections_goes_to_the_core_as_described(packages):
    """Поле толкует ядро (CP-ADR-0079 §8): установщик передаёт его в описании как есть."""
    _package(packages, "crm-provider", [_connection_type(), _connector()])
    fake = FakeCore()
    _, lines = run_apply(fake, cp.resolve(["crm-provider"]))
    [(path, body)] = fake.agent_requests[-1:]
    assert path == "/agents" and body["spec"]["connections"] == ["crm"]
    sections = _sections(lines)
    assert sections.index("[ConnectionType]") < sections.index("[Agent]")


def test_the_link_to_the_package_is_recorded_when_the_core_records_the_kind(packages):
    """Ядро с подключениями записывает ConnectionType через packages:record (RecordedKind);
    ядро без вида — нет, и установщик говорит, что связь не записана."""
    _package(packages, "crm-provider", [_connection_type(), _connector()])
    fake = FakeCore()
    _, lines = run_apply(fake, cp.resolve(["crm-provider"]))
    assert not any("does not link" in line for line in lines)
    assert {"kind": "ConnectionType", "key": "crm"} in fake.records[-1]["objects"]

    def without_kind(node: Any) -> None:
        if isinstance(node, dict):
            if "ConnectionType" in (node.get("enum") or []):
                node["enum"].remove("ConnectionType")
            for child in node.values():
                without_kind(child)
        elif isinstance(node, list):
            for child in node:
                without_kind(child)

    without_kind(fake.openapi)
    _, lines = run_apply(fake, cp.resolve(["crm-provider"]))
    assert any("does not link ConnectionType" in line for line in lines)
    assert {o["kind"] for o in fake.records[-1]["objects"]} == {"Agent"}


# --- песочница ------------------------------------------------------------------------------


def test_sandbox_runs_a_package_that_carries_a_connection_type(packages):
    """Пакет провайдера с типом подключения тестируется песочницей ядра, даже если разбор
    пакетов ядра рядом вид ещё не знает."""
    try:
        domain = sandbox._domain()
    except sandbox.CoreMissing as error:
        pytest.skip(str(error))
    shutil.copytree(packages / "invoice-payment", packages / "invoice-crm")
    manifest = packages / "invoice-crm" / "package.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("key: invoice-payment", "key: invoice-crm"),
        encoding="utf-8",
    )
    folder = packages / "invoice-crm" / "connection-types"
    folder.mkdir()
    (folder / "crm.yaml").write_text(
        json.dumps({"apiVersion": cp.API_VERSION, **_connection_type()}), encoding="utf-8"
    )
    package = cp.load_package(packages / "invoice-crm")
    sent = {path for path, _text in sandbox._files(package, {}, domain["kinds"])}
    assert ("connection-types/crm.yaml" in sent) == ("ConnectionType" in domain["kinds"])
    assert "connection-types/crm.yaml" in {p for p, _t in sandbox._files(package, {}, None)}

    report = sandbox.run_package(
        packages / "invoice-crm",
        env={
            "NOTIFICATION_SERVICE_URL": "https://notify.example",
            "INVOICE_WORKSPACE_ID": "00000000-0000-0000-0000-00000000000b",
            "ACCOUNTING_ROLE_ID": "00000000-0000-0000-0000-00000000000d",
        },
    )
    assert report["status"] == "passed", report["problems"]
    assert report["tests"]


def test_the_scaffold_of_a_connection_type_passes_check(packages):
    _package(packages, "crm-provider", [])
    spec = scaffold.minimal_spec("ConnectionType", "crm")
    _package(packages, "crm-provider", [{"kind": "ConnectionType", "key": "crm", "spec": spec}])
    assert settled(cp.check(cp.resolve(["crm-provider"]))) == ([], [])
