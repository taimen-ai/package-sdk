"""MCP-сервер автора `package-sdk mcp` (S024: FR-030, plan Р10).

Инструменты проверяются против настоящего сервера SDK — в процессе (клиент MCP к
``build_server()``) и по stdio (``package-sdk mcp`` подпроцессом), а не против стенда
ядра: план и применение идут на поддельное ядро тестов установки (``FakeCore``).

Приёмка: применение только по хэшу показанного плана — без ``plan_hash`` и с чужим
хэшем отказ до стенда, с тем же хэшем план применяется, повторный план пуст; подмена файла
плана после проверки хэша не меняет применяемый план. Токен уходит только на стенды из
``PACKAGE_SDK_SERVERS``; план пишется только в ``<корень>/.package-sdk/`` и чужих файлов не
перезаписывает; правка — только внутри корня сессии.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("mcp", reason="MCP-сервер автора — extra mcp")

import mcp_types
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from package_sdk import commands, install, scaffold
from package_sdk import mcp as author
from tests.test_install import (  # noqa: F401  — фикстуры тестов установки
    ENV,
    SERVER,
    FakeCore,
    cli_core,
    core,
    notify,
    project,
)

REPO = Path(__file__).resolve().parents[1]
PYRAMID = REPO / "tests" / "fixtures" / "pyramid" / "packages"
TOOLS = {"pkg_check", "pkg_test", "pkg_describe", "pkg_edit", "pkg_plan", "pkg_apply"}


def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Один вызов инструмента через клиента MCP к серверу в процессе; ответ — JSON."""

    async def run() -> dict[str, Any]:
        async with Client(author.build_server()) as client:
            result = await client.call_tool(tool, arguments)
        assert not result.is_error, result
        text = result.content[0].text  # type: ignore[union-attr]
        answer: dict[str, Any] = json.loads(text)
        return answer

    return asyncio.run(run())


@pytest.fixture(autouse=True)
def no_configured_stands(monkeypatch: pytest.MonkeyPatch) -> None:
    """Окружение разработчика не влияет на тесты: корень и стенды задаёт сам тест."""
    monkeypatch.delenv(author.ROOT_ENV, raising=False)
    monkeypatch.delenv(author.SERVERS_ENV, raising=False)


@pytest.fixture
def packages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Копия пакетов пирамиды, которую тест может править; корень сессии — tmp_path."""
    root = tmp_path / "packages"
    shutil.copytree(PYRAMID, root, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setenv(author.ROOT_ENV, str(tmp_path))
    return root


@pytest.fixture
def stand(project: Path, cli_core: FakeCore, monkeypatch: pytest.MonkeyPatch) -> FakeCore:
    """Корень сессии — проект установки, разрешённый стенд — поддельное ядро."""
    monkeypatch.setenv(author.ROOT_ENV, str(project))
    monkeypatch.setenv(author.SERVERS_ENV, SERVER)
    return cli_core


def no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Токен стенда не должен даже запрашиваться."""

    def refuse(server: str) -> str:
        raise AssertionError(f"токен запрошен для {server}")

    monkeypatch.setattr(commands, "_bearer", refuse)


def plan_of(
    project: Path, name: str = "plan.json", install_file: str = "packages.yaml"
) -> dict[str, Any]:
    return call(
        "pkg_plan",
        {"server": SERVER, "install": str(project / install_file), "out": f".package-sdk/{name}"},
    )


# --- сервер ---------------------------------------------------------------------------


def test_server_lists_the_author_tools() -> None:
    async def run() -> Any:
        async with Client(author.build_server()) as client:
            return (await client.list_tools()).tools

    tools = {tool.name: tool for tool in asyncio.run(run())}
    assert set(tools) == TOOLS
    apply = tools["pkg_apply"]
    assert apply.input_schema["required"] == ["plan_file", "plan_hash"]
    assert apply.annotations is not None and apply.annotations.destructive_hint is True
    for name in ("pkg_check", "pkg_test", "pkg_describe"):
        annotations = tools[name].annotations
        assert annotations is not None and annotations.read_only_hint is True, name


def test_stdio_server_answers_as_the_cli_entry_point() -> None:
    """``package-sdk mcp`` — сервер по stdio: список инструментов и проверка пакета."""
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "package_sdk.cli", "mcp"], cwd=str(REPO)
    )

    async def run() -> tuple[set[str], dict[str, Any]]:
        async with Client(params) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            result = await client.call_tool(
                "pkg_check", {"path": str(PYRAMID / "review-flow"), "schema_only": True}
            )
        return names, json.loads(result.content[0].text)  # type: ignore[union-attr]

    names, report = asyncio.run(run())
    assert names == TOOLS
    assert report["ok"] is True and report["packages"] == 2


# --- проверка, описание, тесты ---------------------------------------------------------


def test_check_reports_findings_in_the_form_of_the_cli() -> None:
    pytest.importorskip("control_plane", reason="проверка кодом ядра — extra sandbox")
    report = call("pkg_check", {"path": str(PYRAMID / "review-flow")})
    assert report["ok"] is True and report["core"] == "skipped"
    assert report["errors"] == []

    broken = call(
        "pkg_check", {"path": str(REPO / "tests" / "fixtures" / "manifest" / "acme-claims")}
    )
    assert broken["ok"] is False
    finding = broken["errors"][0]
    assert {"code", "severity", "message", "file"} <= set(finding)
    assert finding["code"] == "knowledge_unknown"


def test_check_needs_exactly_one_target() -> None:
    assert call("pkg_check", {})["error"] == "arguments_invalid"
    both = call("pkg_check", {"path": str(PYRAMID / "review-flow"), "install": "x.yaml"})
    assert both["error"] == "arguments_invalid"


def test_describe_with_the_variables_template() -> None:
    info = call(
        "pkg_describe",
        {
            "path": str(REPO / "tests" / "fixtures" / "manifest" / "acme-claims"),
            "env_example": True,
        },
    )
    assert info["package"] == "acme-claims"
    assert {v["name"] for v in info["variables"]} >= {"CLAIMS_WORKSPACE_ID", "HELPDESK_URL"}
    assert "CLAIMS_WORKSPACE_ID=" in info["envExample"]


def test_test_runs_the_pyramid_narrowed_to_named_scenarios(packages: Path) -> None:
    pytest.importorskip("control_plane", reason="сценарии — кодом ядра (extra sandbox)")
    report = call("pkg_test", {"path": str(packages / "review-flow"), "tests": ["request-intake"]})
    assert report["status"] == "passed", report
    assert [s["stage"] for s in report["stages"]] == ["check", "skills", "integration", "scenarios"]
    scenarios = report["stages"][-1]["packages"][0]["report"]["tests"]
    assert [t["file"] for t in scenarios] == ["tests/request-intake.test.yaml"]

    missing = call("pkg_test", {"path": str(packages / "review-flow"), "tests": ["nothing"]})
    assert missing["error"] == "test_not_found"


# --- правка -------------------------------------------------------------------------


def test_edit_keeps_the_file_on_dry_run_and_writes_otherwise(packages: Path) -> None:
    process = packages / "review-flow" / "processes" / "request-intake.yaml"
    before = process.read_text(encoding="utf-8")
    options = {"file": str(process), "path": "spec.displayName", "value": "Intake", "string": True}

    preview = call("pkg_edit", {"operation": "set", "options": options, "dry_run": True})
    assert preview["ok"] is True and preview["dryRun"] is True
    assert "+  displayName: Intake" in preview["diff"]
    assert process.read_text(encoding="utf-8") == before

    done = call("pkg_edit", {"operation": "set", "options": options})
    assert done["ok"] is True and done["files"] == [str(process.resolve())]
    after = process.read_text(encoding="utf-8")
    assert "displayName: Intake" in after
    # остальные строки файла не тронуты
    assert [line for line in after.splitlines() if "displayName" not in line] == [
        line for line in before.splitlines() if "displayName" not in line
    ]


def test_edit_refusals_are_machine_readable(packages: Path) -> None:
    process = packages / "review-flow" / "processes" / "request-intake.yaml"
    unknown = call("pkg_edit", {"operation": "nothing", "options": {}})
    assert unknown["error"] == "arguments_invalid"
    missing = call("pkg_edit", {"operation": "set", "options": {"file": str(process)}})
    assert missing["error"] == "arguments_invalid"
    refused = call(
        "pkg_edit",
        {"operation": "rename", "options": {"file": str(process), "from": "absent", "to": "x"}},
    )
    assert refused["ok"] is False and refused["error"]["code"]


def test_edit_stays_inside_the_session_root(packages: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.yaml"
    outside.write_text("key: x\n", encoding="utf-8")
    try:
        escaped = call(
            "pkg_edit",
            {"operation": "set", "options": {"file": str(outside), "path": "key", "value": "y"}},
        )
        assert escaped["error"] == "path_outside_root"
        process = packages / "review-flow" / "processes" / "request-intake.yaml"
        fragment = call(
            "pkg_edit",
            {
                "operation": "add-stage",
                "options": {"file": str(process), "stage": f"@{outside}"},
                "dry_run": True,
            },
        )
        assert fragment["error"] == "path_outside_root"
        assert outside.read_text(encoding="utf-8") == "key: x\n"
    finally:
        outside.unlink()


def test_edit_resolves_relative_paths_and_fragments_from_the_root(packages: Path) -> None:
    stage = packages.parent / "stage.yaml"
    stage.write_text("{id: extra, steps: [{id: note, set: {note: \"'x'\"}}]}\n", "utf-8")
    process = "packages/review-flow/processes/request-intake.yaml"
    done = call(
        "pkg_edit",
        {"operation": "add-stage", "options": {"file": process, "stage": "@stage.yaml"}},
    )
    assert done["ok"] is True, done
    assert "id: extra" in (packages.parent / process).read_text(encoding="utf-8")


def test_session_root_from_the_client_roots(
    packages: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без PACKAGE_SDK_ROOT корень сессии — корень file:// клиента MCP, а не cwd сервера."""
    monkeypatch.delenv(author.ROOT_ENV)
    root = packages.parent

    async def roots(_context: Any) -> mcp_types.ListRootsResult:
        return mcp_types.ListRootsResult(roots=[mcp_types.Root(uri=root.as_uri())])

    async def run() -> dict[str, Any]:
        # корни — запрос сервера клиенту: он есть у соединения с рукопожатием (legacy)
        async with Client(
            author.build_server(), list_roots_callback=roots, mode="legacy"
        ) as client:
            result = await client.call_tool(
                "pkg_edit",
                {
                    "operation": "set",
                    "options": {
                        "file": "packages/review-flow/processes/request-intake.yaml",
                        "path": "spec.displayName",
                        "value": "From root",
                        "string": True,
                    },
                },
            )
        answer: dict[str, Any] = json.loads(result.content[0].text)  # type: ignore[union-attr]
        return answer

    done = asyncio.run(run())
    assert done["ok"] is True, done
    process = root / "packages" / "review-flow" / "processes" / "request-intake.yaml"
    assert "displayName: From root" in process.read_text(encoding="utf-8")


def test_init_ignores_the_work_directory_of_the_server(tmp_path: Path) -> None:
    scaffold.init(tmp_path / "pkg")
    assert ".package-sdk/" in (tmp_path / "pkg" / ".gitignore").read_text(encoding="utf-8")


# --- стенды -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured", "server"),
    [
        ("", "https://platform.example.com"),
        ("https://platform.example.com", "https://collector.example.net"),
        ("https://platform.example.com", "http://platform.example.com"),
        ("http://platform.example.com", "http://platform.example.com"),
        ("https://platform.example.com", "https://platform.example.com.example.net"),
    ],
)
def test_token_goes_only_to_a_configured_stand(
    configured: str, server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(author.SERVERS_ENV, configured)
    no_token(monkeypatch)
    package = str(PYRAMID / "review-flow")
    for tool, arguments in (
        ("pkg_check", {"path": package, "server": server}),
        ("pkg_test", {"path": package, "server": server}),
        ("pkg_plan", {"path": package, "server": server}),
    ):
        refused = call(tool, arguments)
        assert refused["error"] == "server_not_allowed", (tool, refused)


def test_apply_refuses_a_plan_for_a_stand_no_longer_configured(
    project: Path, stand: FakeCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = plan_of(project)
    monkeypatch.setenv(author.SERVERS_ENV, "https://other.example.com")
    no_token(monkeypatch)
    refused = call("pkg_apply", {"plan_file": shown["planFile"], "plan_hash": shown["planHash"]})
    assert refused["error"] == "server_not_allowed"
    assert stand.writes == [] and stand.core_writes == []


def test_the_only_configured_stand_is_the_default(project: Path, stand: FakeCore) -> None:
    shown = call("pkg_plan", {"install": "packages.yaml"})  # относительный — от корня сессии
    assert shown["server"] == SERVER, shown
    assert shown["planFile"] == str((project / ".package-sdk" / "plan.json").resolve())


# --- план и применение ------------------------------------------------------------------


def test_apply_only_by_the_hash_of_the_shown_plan(project: Path, stand: FakeCore) -> None:
    shown = plan_of(project)
    plan_file = Path(shown["planFile"])
    assert plan_file == (project / ".package-sdk" / "plan.json").resolve() and plan_file.is_file()
    assert shown["planHash"].startswith("sha256:") and shown["server"] == SERVER
    assert shown["changes"] > 0
    assert any("Role/claims-officer" in line for line in shown["lines"])
    assert shown["plan"]["sections"][0]["kind"] == "catalog"
    assert stand.writes == [] and stand.core_writes == []  # план ничего не пишет

    without = call("pkg_apply", {"plan_file": str(plan_file), "plan_hash": ""})
    assert without["error"] == "plan_hash_required"
    other = call("pkg_apply", {"plan_file": str(plan_file), "plan_hash": "sha256:" + "0" * 64})
    assert other["error"] == "plan_hash_mismatch"
    assert stand.writes == [] and stand.core_writes == []

    applied = call("pkg_apply", {"plan_file": str(plan_file), "plan_hash": shown["planHash"]})
    assert applied["planHash"] == shown["planHash"], applied
    assert applied["applied"]["core"] and stand.writes
    assert any(line.startswith("применён план") for line in applied["log"])

    assert plan_of(project)["changes"] == 0


def test_plan_overwrite_console_is_in_the_plan_and_the_apply(
    project: Path, stand: FakeCore
) -> None:
    from tests.test_install import _console_edit

    _console_edit(stand)
    kept = plan_of(project, "kept.json")
    assert kept["overwriteConsole"] is False and kept["plan"]["overwriteConsole"] is False
    assert kept["consoleEdits"] == [
        {
            "package": "acme-claims",
            "kind": "TaskType",
            "key": "claim-review",
            "overwritten": [],
            "kept": ["displayName"],
        }
    ]
    shown = call(
        "pkg_plan",
        {
            "server": SERVER,
            "install": str(project / "packages.yaml"),
            "out": ".package-sdk/plan.json",
            "overwrite_console": True,
        },
    )
    assert shown["overwriteConsole"] is True and shown["planHash"] != kept["planHash"]
    assert shown["consoleEdits"][0]["overwritten"] == ["displayName"]
    assert "  ! TaskType/claim-review (acme-claims): displayName" in shown["lines"]
    applied = call("pkg_apply", {"plan_file": shown["planFile"], "plan_hash": shown["planHash"]})
    assert applied["planHash"] == shown["planHash"], applied
    assert [b.get("overwriteConsole") for b in stand.apply_calls] == [True]
    assert stand.console == {}


def test_plan_file_replaced_after_the_hash_check_is_not_applied(
    project: Path, stand: FakeCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Регрессия: план читается один раз. Подмена файла между проверкой хэша и применением
    (другой план, другая установка) не приводит к применению подменённого плана."""
    other = yaml.safe_load((project / "packages.yaml").read_text(encoding="utf-8"))
    del other["spec"]["retire"]["TaskType"]  # другая установка — другой план
    (project / "other.yaml").write_text(yaml.safe_dump(other), encoding="utf-8")
    swapped = plan_of(project, "other.json", "other.yaml")
    shown = plan_of(project)
    assert "planHash" in swapped, swapped
    assert swapped["planHash"] != shown["planHash"]
    plan_file = Path(shown["planFile"])
    replacement = Path(swapped["planFile"]).read_text(encoding="utf-8")
    original = install.read_plan

    def read_then_swap(path: Path) -> dict[str, Any]:
        document = original(path)
        plan_file.write_text(replacement, encoding="utf-8")  # подмена сразу после чтения
        return document

    monkeypatch.setattr(install, "read_plan", read_then_swap)
    applied = call("pkg_apply", {"plan_file": str(plan_file), "plan_hash": shown["planHash"]})
    assert applied["planHash"] == shown["planHash"], applied
    assert applied["applied"]["planHash"] == shown["planHash"]
    assert json.loads(plan_file.read_text(encoding="utf-8"))["planHash"] == swapped["planHash"]


def test_stand_changed_after_the_plan_is_plan_stale(project: Path, stand: FakeCore) -> None:
    shown = plan_of(project)
    # кто-то поставил тип задачи пакета, пока план ждал применения: план ядра другой
    stand.published["TaskType/claim-review"] = "sha256:other"
    stale = call("pkg_apply", {"plan_file": shown["planFile"], "plan_hash": shown["planHash"]})
    assert stale["error"] == "plan_stale"
    assert stand.writes == [] and stand.core_writes == []


def test_env_of_the_plan_and_apply_is_the_same(project: Path, stand: FakeCore) -> None:
    """Значения переменных входят в план хэшем: другие значения при применении — plan_stale."""
    assert ENV  # переменные установки заданы окружением (фикстура cli_core)
    shown = plan_of(project)
    (project / "other.env").write_text("CLAIMS_REFUND_THRESHOLD=1\n", encoding="utf-8")
    refused = call(
        "pkg_apply",
        {"plan_file": shown["planFile"], "plan_hash": shown["planHash"], "env_file": "other.env"},
    )
    assert refused["error"] == "plan_stale", refused
    assert "variablesHash" in refused["message"]
    assert stand.writes == [] and stand.core_writes == []


def test_plan_of_one_package_directory(project: Path, stand: FakeCore) -> None:
    shown = call(
        "pkg_plan",
        {"path": "packages/acme-claims", "out": ".package-sdk/claims/plan.json"},
    )
    assert shown["planHash"].startswith("sha256:"), shown
    plan_file = Path(shown["planFile"])
    generated = plan_file.with_name("plan.install.yaml")
    assert generated.read_text(encoding="utf-8").startswith(author.GENERATED)
    first = json.loads(plan_file.read_text(encoding="utf-8"))
    assert first["install"] == generated.name
    # повторный план перезаписывает свою же установку и строится тем же; planHash сравнивать
    # нельзя — в нём createdAt с точностью до секунды
    again = call(
        "pkg_plan", {"path": "packages/acme-claims", "out": ".package-sdk/claims/plan.json"}
    )
    assert again["planFile"] == shown["planFile"], again
    second = json.loads(plan_file.read_text(encoding="utf-8"))
    assert second["planHash"] == again["planHash"]
    stable = ("install", "lockHash", "variablesHash", "overwriteConsole", "sections")
    assert {k: second[k] for k in stable} == {k: first[k] for k in stable}
    applied = call("pkg_apply", {"plan_file": str(plan_file), "plan_hash": again["planHash"]})
    assert applied["planHash"] == again["planHash"], applied


def test_plan_is_written_only_under_the_work_directory(project: Path, stand: FakeCore) -> None:
    install_file = str(project / "packages.yaml")
    for out in (
        "plan.json",
        "../plan.json",
        str(project.parent / "plan.json"),
        ".package-sdk/p.txt",
    ):
        refused = call("pkg_plan", {"install": install_file, "out": out})
        assert refused["error"] == "out_not_allowed", (out, refused)
    assert not (project / "plan.json").exists() and not (project.parent / "plan.json").exists()


def test_plan_does_not_overwrite_files_that_are_not_plans(project: Path, stand: FakeCore) -> None:
    work = project / ".package-sdk"
    work.mkdir()
    notes = work / "notes.json"
    notes.write_text('{"keep": true}\n', encoding="utf-8")
    refused = call("pkg_plan", {"install": "packages.yaml", "out": ".package-sdk/notes.json"})
    assert refused["error"] == "out_not_a_plan"
    assert notes.read_text(encoding="utf-8") == '{"keep": true}\n'

    foreign = work / "claims.install.yaml"
    foreign.write_text("# своя установка\n", encoding="utf-8")
    kept = call("pkg_plan", {"path": "packages/acme-claims", "out": ".package-sdk/claims.json"})
    assert kept["error"] == "install_file_exists"
    assert foreign.read_text(encoding="utf-8") == "# своя установка\n"


def test_work_directory_linked_outside_the_root_is_refused(
    project: Path, stand: FakeCore, tmp_path_factory: pytest.TempPathFactory
) -> None:
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    (project / ".package-sdk").symlink_to(elsewhere, target_is_directory=True)
    refused = call("pkg_plan", {"install": "packages.yaml"})
    assert refused["error"] == "out_not_allowed"
    assert list(elsewhere.iterdir()) == []


def test_plan_needs_one_target(project: Path, stand: FakeCore) -> None:
    assert call("pkg_plan", {})["error"] == "arguments_invalid"
    both = call("pkg_plan", {"install": "packages.yaml", "path": "packages/acme-claims"})
    assert both["error"] == "arguments_invalid"


# --- адреса и учётки только из окружения процесса ---------------------------------------


def listening() -> tuple[Any, list[tuple[str, str, str | None]]]:
    """HTTP-сервер на localhost, который записывает, кто и с каким Authorization пришёл."""
    seen: list[tuple[str, str, str | None]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def answer(self) -> None:
            seen.append((self.command, self.path, self.headers.get("Authorization")))
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = do_PATCH = answer

        def log_message(self, *_args: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def test_notification_address_and_token_are_not_taken_from_env_file(
    project: Path, stand: FakeCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    listener, seen = listening()
    try:
        monkeypatch.delenv("NOTIFICATION_SERVICE_URL", raising=False)
        monkeypatch.delenv("NOTIFY_TOKEN", raising=False)
        address = f"http://127.0.0.1:{listener.server_port}/notify"
        (project / ".env").write_text(
            f"NOTIFICATION_SERVICE_URL={address}\nNOTIFY_TOKEN=notify-secret\n", encoding="utf-8"
        )
        refused = call("pkg_plan", {"install": "packages.yaml"})
        assert "error" in refused and "NOTIFICATION_SERVICE_URL" in refused["message"], refused

        # тот же адрес в окружении процесса, но не под разрешённым стендом — отказ до токена
        monkeypatch.setenv("NOTIFICATION_SERVICE_URL", address)
        monkeypatch.setenv("NOTIFY_TOKEN", "notify-secret")
        unlisted = call("pkg_plan", {"install": "packages.yaml"})
        assert unlisted["error"] == "server_not_allowed", unlisted
        assert seen == []
    finally:
        listener.shutdown()


def test_env_file_stays_inside_the_root(
    project: Path, stand: FakeCore, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("env") / "stand.env"
    outside.write_text("CLAIMS_REFUND_THRESHOLD=1\n", encoding="utf-8")
    refused = call("pkg_plan", {"install": "packages.yaml", "env_file": str(outside)})
    assert refused["error"] == "path_outside_root"


def test_install_file_link_next_to_the_plan_is_refused(
    project: Path, stand: FakeCore, tmp_path_factory: pytest.TempPathFactory
) -> None:
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    work = project / ".package-sdk"
    work.mkdir()
    (work / "b.install.yaml").symlink_to(elsewhere / "b.install.yaml")  # висячая ссылка
    refused = call("pkg_plan", {"path": "packages/acme-claims", "out": ".package-sdk/b.json"})
    assert refused["error"] == "install_file_exists"
    assert list(elsewhere.iterdir()) == []
