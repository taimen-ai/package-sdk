"""Плагин Claude Code package-author (S024: FR-030, FR-031; перенос test_process_author_plugin).

Структура плагина валидна: манифест, marketplace в корне репозитория и сервер MCP
плагина разбираются, у каждого скилла есть frontmatter с name и description, хук согласия
отклоняет применение без planHash (и с хэшем другого плана) и спрашивает хост с ним. Всё, на
что ссылаются скиллы, существует: инструменты ``pkg_*`` — в MCP-сервере SDK, инструменты
``cp_*`` — в MCP-сервере ядра (если его код рядом), команды — в CLI package-sdk, пути
эталонов и схем — в этом репозитории. Примеры YAML в скиллах проходят схемы формата.
Имена плагина и скиллов нейтральны (white-label).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from package_sdk import commands, edit, model
from package_sdk import schema as schema_module

REPO = Path(__file__).resolve().parents[1]
MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"
PLUGIN = REPO / "plugin" / "package-author"
SKILLS_DIR = PLUGIN / "skills"
HOOK = PLUGIN / "hooks" / "confirm_apply.py"

PROCESS_AUTHOR_SKILLS = {
    "describe-process",
    "process-from-regulation",
    "write-tests-first",
    "author-package",
    "validate-and-fix",
    "simulate-and-plan",
    "explain-instance",
    "goal-as-process",
    "knowledge-import",
    "knowledge-model",
}
PACKAGE_SKILLS = {
    "author-work",
    "author-rule",
    "author-agent",
    "author-integration",
    "author-notification",
    "release-package",
}
SKILLS = PROCESS_AUTHOR_SKILLS | PACKAGE_SKILLS
# Скиллы, которые зовут pkg_apply: у каждого правило согласия это называет прямо.
APPLYING = {"simulate-and-plan", "knowledge-model", "release-package"}
AUTHOR_TOOLS = {"pkg_check", "pkg_test", "pkg_describe", "pkg_edit", "pkg_plan", "pkg_apply"}
# Инструменты ядра, на которые опираются скиллы (у оператора, плагин control-plane-operator).
CORE_TOOLS = {"cp_recall", "cp_process_get", "cp_process_explain"}
PRODUCT = re.compile(r"taimen", re.IGNORECASE)
KEBAB = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
# Пути этого репозитория, на которые скиллы ссылаются как на эталоны.
PATH_ROOTS = ("schema/", "tests/", "src/", "plugin/", "examples/")
BACKTICK_PATH = re.compile(r"`((?:" + "|".join(re.escape(r) for r in PATH_ROOTS) + r")[^`\s]*)`")
MD_LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
PKG_TOOL = re.compile(r"(?<![\w/.])(pkg_[a-z][a-z0-9_]*[a-z0-9])(?![\w*]|\.py)")
CP_TOOL = re.compile(r"(?<![\w/.])(cp_[a-z][a-z0-9_]*[a-z0-9])(?![\w*]|\.py)")
RULE = "## Правило плагина"


def plugin_files() -> list[Path]:
    return sorted(
        p
        for p in PLUGIN.rglob("*")
        if p.is_file() and p.suffix in {".md", ".json", ".py"} and "__pycache__" not in p.parts
    )


def skill_files() -> dict[str, Path]:
    return {p.parent.name: p for p in SKILLS_DIR.glob("*/SKILL.md")}


def frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    assert match, f"{path}: нет frontmatter между строками ---"
    meta = yaml.load(match.group(1), Loader=model._yaml12_loader())
    assert isinstance(meta, dict), f"{path}: frontmatter — не словарь"
    return meta, match.group(2)


def rule_of(body: str) -> str:
    return body.split(RULE, 1)[1].split("\n## ", 1)[0]


# --- манифест, marketplace, сервер MCP ----------------------------------------------------


def test_plugin_manifest() -> None:
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text("utf-8"))
    assert manifest["name"] == "package-author"
    assert re.match(r"^\d+\.\d+\.\d+$", manifest["version"])
    assert manifest["description"].strip()
    # Скиллы и хуки лежат на местах по умолчанию и подхватываются сами.
    assert (PLUGIN / "hooks" / "hooks.json").is_file()
    assert SKILLS_DIR.is_dir()


def test_marketplace_points_at_plugin() -> None:
    market = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    assert market["name"] and market["owner"]["name"]
    entries = {entry["name"]: entry for entry in market["plugins"]}
    entry = entries["package-author"]
    source = (MARKETPLACE.parent.parent / entry["source"]).resolve()
    assert source == PLUGIN.resolve()
    manifest = json.loads((source / ".claude-plugin" / "plugin.json").read_text("utf-8"))
    assert entry["version"] == manifest["version"]


def test_plugin_brings_the_sdk_mcp_server() -> None:
    servers = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert servers == {"package-sdk": {"command": "package-sdk", "args": ["mcp"]}}
    project = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'package-sdk = "package_sdk.cli:main"' in project
    assert re.search(r'^mcp = \["mcp>=[^"]+", "control-plane-client"\]$', project, re.M)


def test_names_are_neutral() -> None:
    market = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    names = [market["name"], *(e["name"] for e in market["plugins"]), *skill_files()]
    names += [p.name for p in PLUGIN.rglob("*")]
    servers = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    names += list(servers)
    for name in names:
        assert not PRODUCT.search(name), f"имя продукта в имени {name!r} (white-label)"


# --- скиллы ------------------------------------------------------------------------------


def test_skills_present_with_frontmatter() -> None:
    found = skill_files()
    assert set(found) == SKILLS
    for name, path in found.items():
        meta, body = frontmatter(path)
        assert meta.get("name") == name, f"{path}: name {meta.get('name')!r} не совпадает"
        assert KEBAB.match(name), f"{name}: имя скилла — kebab-case"
        description = meta.get("description")
        assert isinstance(description, str) and 40 <= len(description) <= 1024, path
        assert set(meta) <= {"name", "description", "allowed-tools"}, f"{path}: {set(meta)}"
        assert body.lstrip().startswith("# "), f"{path}: после frontmatter — заголовок"


def test_every_skill_states_consent_rule() -> None:
    for name, path in skill_files().items():
        _, body = frontmatter(path)
        assert RULE in body, f"{name}: нет раздела «Правило плагина»"
        rule = rule_of(body)
        assert "согласи" in rule or "«да»" in rule, f"{name}: правило не говорит о согласии"


def test_only_consenting_skills_apply() -> None:
    """pkg_apply зовут только скиллы применения, и их правило называет это условие прямо;
    другие скиллы называют его лишь в правиле."""
    for name, path in skill_files().items():
        _, body = frontmatter(path)
        head, _, rest = body.partition(RULE)
        rule, _, tail = rest.partition("\n## ")
        if name in APPLYING:
            assert "`pkg_apply` вызывается только после" in rule, name
            assert "planHash" in body or "plan_hash" in body, name
        else:
            assert "pkg_apply" not in head + tail, f"{name}: применение — только в {APPLYING}"
    plan = skill_files()["simulate-and-plan"].read_text(encoding="utf-8")
    assert "plan_stale" in plan and "plan_hash_mismatch" in plan


def test_skills_reference_etalons() -> None:
    for name in (
        "describe-process",
        "write-tests-first",
        "author-package",
        "validate-and-fix",
        "simulate-and-plan",
        "goal-as-process",
        "explain-instance",
    ):
        text = skill_files()[name].read_text(encoding="utf-8")
        assert "examples/claims/claims/" in text, name
    for name in ("author-work", "author-rule", "author-agent", "author-integration"):
        text = skill_files()[name].read_text(encoding="utf-8")
        assert "examples/claims/claims/" in text, name
    # Базовая онтология открытой поставки — default@1; закрытый company@1 — оговоркой.
    model_skill = skill_files()["knowledge-model"].read_text(encoding="utf-8")
    assert 'extends: ["default@1"]' in model_skill and "Оговорка" in model_skill
    author = skill_files()["author-package"].read_text(encoding="utf-8")
    for needle in (
        "now()",
        "expression_cost_exceeded",
        "YAML 1.2",
        "package-sdk edit",
        "pkg_edit",
        "memory",
        "recall",
        "remember",
        "context",
        "owner",
        "identity",
        "taimen/1",
    ):
        assert needle in author, f"author-package: нет {needle!r}"


def test_package_skills_cover_the_whole_cycle() -> None:
    """FR-031: плагин ведёт весь цикл пакета, а не только процессы."""
    expected = {
        "author-work": ("TaskType", "Role", "ArtifactType", "subject: taskType"),
        "author-rule": ("WorkRule", "subject: rule", "dedupKeyTemplate", "identity"),
        "author-agent": ("Agent", "identity", "executor", "placement", "secrets"),
        "author-integration": (
            "package_sdk.connector",
            "skill-sdk",
            "package-sdk image",
            "integration/tests",
        ),
        "author-notification": ("NotificationRule", "recipient", "dedupKeyTemplate", "close"),
        "release-package": ("SemVer", "package-sdk lock", "pkg_plan", "pkg_apply", "тег"),
    }
    for name, needles in expected.items():
        text = skill_files()[name].read_text(encoding="utf-8")
        for needle in needles:
            assert needle in text, f"{name}: нет {needle!r}"


# --- хук согласия ----------------------------------------------------------------------


def run_hook(payload: object) -> dict[str, Any] | None:
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    answer: dict[str, Any] | None = json.loads(result.stdout) if result.stdout.strip() else None
    return answer


def test_hooks_json_wires_confirm_apply() -> None:
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    entries = hooks["hooks"]["PreToolUse"]
    assert len(entries) == 1
    matcher = re.compile(entries[0]["matcher"])
    assert matcher.search("mcp__plugin_package-author_package-sdk__pkg_apply")
    assert not matcher.search("mcp__plugin_package-author_package-sdk__pkg_plan")
    assert not matcher.search("mcp__plugin_x_control_plane__cp_pkg_apply_later")
    command = entries[0]["hooks"][0]["command"]
    assert "${CLAUDE_PLUGIN_ROOT}/hooks/confirm_apply.py" in command


TOOL = "mcp__plugin_package-author_package-sdk__pkg_apply"
HASH = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"plan_file": "/p/plan.json"},
        {"plan_file": "/p/plan.json", "plan_hash": ""},
        {"plan_file": "/p/plan.json", "plan_hash": "sha256:short"},
    ],
)
def test_hook_denies_apply_without_plan_hash(arguments: dict[str, Any]) -> None:
    output = run_hook({"tool_name": TOOL, "tool_input": arguments})
    assert output is not None
    decision = output["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"


PLAN = {
    "format": "package-sdk.plan/v1",
    "server": "https://cp.example.com",
    "planHash": HASH,
    "sections": [
        {"kind": "catalog", "changes": [{"kind": "Role"}, {"kind": "Skill"}]},
        {
            "kind": "core",
            "plan": {"changes": [{"action": "create"}, {"action": "unchanged"}]},
        },
        {"kind": "knowledge", "register": [], "enable": []},
        {"kind": "notification-rules", "changes": []},
        {"kind": "retire", "items": [{"kind": "Process"}]},
    ],
}


def written(tmp_path: Path, document: object, name: str = "plan.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def decision_for(arguments: dict[str, Any], **extra: Any) -> dict[str, Any]:
    output = run_hook({"tool_name": TOOL, "tool_input": arguments, **extra})
    assert output is not None
    answer: dict[str, Any] = output["hookSpecificOutput"]
    return answer


def test_hook_asks_human_with_plan_hash(tmp_path: Path) -> None:
    plan = written(tmp_path, PLAN)
    decision = decision_for({"plan_file": str(plan), "plan_hash": HASH})
    assert decision["permissionDecision"] == "ask"
    reason = decision["permissionDecisionReason"]
    assert HASH in reason and str(plan) in reason and "https://cp.example.com" in reason
    # число изменений по секциям — как count_changes SDK
    assert "каталог 2, ядро 1, онтологии 0, уведомления 0, вывод из оборота 1" in reason


def test_hook_shows_whether_console_edits_are_overwritten(tmp_path: Path) -> None:
    fields = [
        {"path": "displayName", "owner": "console", "applies": True},
        {"path": "description", "owner": "package", "applies": True},
    ]
    core = {
        "kind": "core",
        "plan": {"changes": [{"kind": "TaskType", "key": "t", "fields": fields}]},
    }
    overwriting = {**PLAN, "overwriteConsole": True, "sections": [PLAN["sections"][0], core]}
    reason = decision_for({"plan_file": str(written(tmp_path, overwriting)), "plan_hash": HASH})[
        "permissionDecisionReason"
    ]
    assert (
        "ПРАВКИ КОНСОЛИ БУДУТ ПЕРЕЗАПИСАНЫ (overwriteConsole: да): TaskType/t (displayName)"
        in reason
    )
    # без флага (и в плане прежнего формата без поля) — правки сохраняются
    for document in ({**PLAN, "overwriteConsole": False}, PLAN):
        plain = decision_for(
            {"plan_file": str(written(tmp_path, document, "plain.json")), "plan_hash": HASH}
        )
        assert plain["permissionDecision"] == "ask"
        assert (
            "правки консоли сохраняются (overwriteConsole: нет)"
            in plain["permissionDecisionReason"]
        )


def test_hook_denies_another_plan_and_what_it_cannot_show(tmp_path: Path) -> None:
    plan = written(tmp_path, PLAN)
    other = decision_for({"plan_file": str(plan), "plan_hash": "sha256:" + "b" * 64})
    assert other["permissionDecision"] == "deny"
    relative = decision_for({"plan_file": "plan.json", "plan_hash": HASH}, cwd=str(tmp_path))
    assert relative["permissionDecision"] == "deny"
    missing = decision_for({"plan_file": str(tmp_path / "absent.json"), "plan_hash": HASH})
    assert missing["permissionDecision"] == "deny"
    not_a_plan = written(tmp_path, {"planHash": HASH}, "notes.json")
    foreign = decision_for({"plan_file": str(not_a_plan), "plan_hash": HASH})
    assert foreign["permissionDecision"] == "deny"
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert (
        decision_for({"plan_file": str(broken), "plan_hash": HASH})["permissionDecision"] == "deny"
    )
    # хэш — целиком, без хвоста
    tail = decision_for({"plan_file": str(plan), "plan_hash": HASH + "\n"})
    assert tail["permissionDecision"] == "deny"


def test_hook_ignores_other_tools() -> None:
    assert run_hook({"tool_name": "mcp__x__pkg_plan", "tool_input": {}}) is None
    assert run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}) is None


def test_hook_denies_unreadable_input() -> None:
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not json",
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- инструменты SDK и ядра ------------------------------------------------------------


def mentioned(pattern: re.Pattern[str]) -> set[str]:
    names: set[str] = set()
    for path in plugin_files():
        names.update(pattern.findall(path.read_text(encoding="utf-8")))
    return names


def test_mentioned_author_tools_exist_in_the_sdk_server() -> None:
    pytest.importorskip("mcp", reason="MCP-сервер автора — extra mcp")
    from mcp import Client

    from package_sdk.mcp import build_server

    async def names() -> set[str]:
        async with Client(build_server()) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    defined = asyncio.run(names())
    assert defined == AUTHOR_TOOLS
    assert mentioned(PKG_TOOL) == AUTHOR_TOOLS  # каждый инструмент где-то нужен, лишних нет


def core_server_source() -> str | None:
    """server.py MCP-сервера ядра: пакет control_plane_mcp из окружения или сосед
    ../control-plane плоской раскладки."""
    candidates: list[Path] = []
    try:
        spec = importlib.util.find_spec("control_plane_mcp")
    except (ImportError, ValueError):
        spec = None
    if spec is not None and spec.submodule_search_locations:
        candidates += [Path(p) / "server.py" for p in spec.submodule_search_locations]
    candidates.append(REPO.parent / "control-plane" / "src" / "control_plane_mcp" / "server.py")
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return None


def test_mentioned_core_tools_exist_in_the_core_server() -> None:
    tools = mentioned(CP_TOOL)
    assert tools >= CORE_TOOLS
    assert not {t for t in tools if t.startswith("cp_pkg_")}, "cp_pkg_* заменены pkg_*"
    source = core_server_source()
    if source is None:
        pytest.skip("нет control_plane_mcp/server.py (extra sandbox или сосед control-plane)")
    defined = set(re.findall(r"^async def (cp_[a-z0-9_]+)\(", source, re.M))
    missing = tools - defined
    assert not missing, f"плагин ссылается на инструменты, которых нет в ядре: {sorted(missing)}"


# --- команды CLI ------------------------------------------------------------------------


def package_sdk_commands() -> set[str]:
    source = Path(commands.__file__).read_text(encoding="utf-8")
    # корневой CLI раздаёт ещё свои группы (package_sdk/cli.py)
    return set(re.findall(r"sub\.add_parser\(\s*\"([a-z-]+)\"", source)) | {
        "edit",
        "test",
        "sandbox",
        "image",
        "mcp",
    }


def edit_commands() -> set[str]:
    parser = edit.build_parser()
    return {name for action in parser._subparsers._group_actions for name in action.choices}  # type: ignore[union-attr]


def test_mentioned_commands_exist() -> None:
    known = {"package-sdk edit": edit_commands(), "package-sdk": package_sdk_commands()}
    assert {
        "add-step",
        "add-stage",
        "add-decision-row",
        "add-rule",
        "add-form-field",
        "rename",
        "set",
    } <= known["package-sdk edit"]
    assert {"check", "plan", "apply", "lock", "describe", "docs", "init", "add"} <= known[
        "package-sdk"
    ]
    for path in plugin_files():
        # команды — в коде: блоки ``` и `…`, а не слова прозы
        source = path.read_text(encoding="utf-8")
        blocks = re.findall(r"```[a-z]*\n(.*?)```", source, re.S)
        spans = re.findall(r"`([^`\n]+)`", re.sub(r"```.*?```", "", source, flags=re.S))
        text = "\n|\n".join([*blocks, *spans])  # фрагменты не склеиваются в одну команду
        for script, found in known.items():
            pattern = re.escape(script) + r"(?:\s+--[a-z-]+)*\s+([a-z][a-z-]*)"
            for command in re.findall(pattern, text):
                if script == "package-sdk" and command == "edit":
                    continue
                assert command in found, f"{path.name}: {script} {command} — такой команды нет"


def test_edit_operations_of_pkg_edit_are_the_cli_ones() -> None:
    """pkg_edit принимает операции package-sdk edit: имена в описании инструмента — те же."""
    from package_sdk.mcp import TOOL_DESCRIPTIONS

    described = set(re.findall(r"\b(add-[a-z-]+|rename|set)\b", TOOL_DESCRIPTIONS["pkg_edit"]))
    assert described == edit_commands()


# --- пути эталонов и схем ----------------------------------------------------------------


def referenced_paths() -> dict[str, set[str]]:
    """Путь от корня репозитория → файлы плагина, которые на него ссылаются."""
    refs: dict[str, set[str]] = {}
    for path in plugin_files():
        text = path.read_text(encoding="utf-8")
        for ref in BACKTICK_PATH.findall(text):
            if any(mark in ref for mark in ("<", "*", "…", "{", "$")):
                continue
            refs.setdefault(ref.rstrip(".,:;"), set()).add(path.name)
        if path.suffix == ".md":
            for link in MD_LINK.findall(text):
                if "://" in link:
                    continue
                target = (path.parent / link).resolve()
                try:
                    ref = target.relative_to(REPO.resolve()).as_posix()
                except ValueError:
                    pytest.fail(f"{path}: ссылка {link} ведёт за пределы репозитория")
                refs.setdefault(ref, set()).add(path.name)
    return refs


def test_referenced_paths_exist() -> None:
    refs = referenced_paths()
    for expected in (
        "tests/fixtures/process/purchase.process.yaml",
        "tests/fixtures/process/purchase.test.yaml",
        "schema/v1/object.schema.json",
        "schema/v1/test.schema.json",
        # эталон — сквозной пример claims; фикстуры — только для конструкций вне него
        "examples/claims/claims/",
        "examples/claims/claims/processes/claim.yaml",
        "examples/claims/claims/rules/claim-reopened.yaml",
        "examples/claims/claims/task-types/claim-reply.yaml",
        "examples/claims/claims/knowledge-packs/claims.yaml",
        "examples/claims/packages.yaml",
    ):
        assert expected in refs, expected
    for ref, where in sorted(refs.items()):
        assert (REPO / ref).exists(), f"{sorted(where)}: путь {ref} не существует"


def test_etalon_processes_follow_the_schema() -> None:
    for ref in (
        "tests/fixtures/process/purchase.process.yaml",
        "examples/claims/claims/processes/claim.yaml",
    ):
        document = yaml.load(
            (REPO / ref).read_text(encoding="utf-8"), Loader=model._yaml12_loader()
        )
        assert document.get("kind") == "Process", ref
        assert schema_module.errors(schema_module.OBJECT, document) == [], ref


# --- примеры YAML в скиллах -------------------------------------------------------------


def test_yaml_examples_follow_schemas() -> None:
    checked = {"test": 0, "process": 0, "object": 0}
    for name, path in skill_files().items():
        for block in re.findall(r"```yaml\n(.*?)```", path.read_text(encoding="utf-8"), re.S):
            data = yaml.load(block, Loader=model._yaml12_loader())
            if not isinstance(data, dict):
                continue
            if "steps" in data and ("process" in data or "subject" in data):
                errors = schema_module.errors(schema_module.TEST, data)
                checked["test"] += 1
            elif "kind" in data and "apiVersion" in data and "spec" in data:
                if data["kind"] == "Process" and "start" not in data["spec"]:
                    continue  # фрагмент заголовка процесса, не целый объект
                errors = schema_module.errors(schema_module.OBJECT, data)
                checked["object"] += 1
            elif isinstance(data.get("spec"), dict) and "start" in data["spec"]:
                document = {
                    "apiVersion": model.API_VERSION,
                    "kind": "Process",
                    "key": "example",
                    **data,
                }
                errors = schema_module.errors(schema_module.OBJECT, document)
                checked["process"] += 1
            else:
                continue
            assert not errors, f"{name}: пример не по схеме: {errors[:3]}"
    assert all(count >= 1 for count in checked.values()), checked


def test_knowledge_skills_write_only_after_consent() -> None:
    """Решение по загрузке и регистрация онтологии — только на «да» показанному плану.
    Онтологию регистрирует и включает установка (KnowledgePack и Installation.spec.knowledge):
    pkg_apply — только после «да» плану pkg_plan. Загрузка таблиц — инструментами платформы
    (скилл knowledge.template@1 и консоль), а не локальными скриптами её исходников."""
    importer = skill_files()["knowledge-import"].read_text(encoding="utf-8")
    rule = importer.split(RULE, 1)[1].split("\n## ", 1)[0]
    assert "`cp_approve`" in rule and "«да»" in rule
    knowledge = skill_files()["knowledge-model"].read_text(encoding="utf-8")
    rule = knowledge.split(RULE, 1)[1].split("\n## ", 1)[0]
    assert "`pkg_apply`" in rule and "`pkg_plan`" in rule and "«да»" in rule
    assert "knowledge.template@1" in importer and "`cp_invoke_skill`" in importer
    for name in ("knowledge-import", "knowledge-model"):
        text = skill_files()[name].read_text(encoding="utf-8")
        for gone in ("pack-register", "pack-enable", "knowledge.py", "integrations/"):
            assert gone not in text, (name, gone)


def test_plugin_does_not_refer_to_records_outside_the_sdk() -> None:
    """Открытый SDK не ссылается на решения и требования дерева платформы: ссылка в никуда."""
    record = re.compile(r"\b(?:TAI|CP|MEM|PC)-ADR-\d+|\bFR-\d+|\bSC-\d+")
    for path in plugin_files():
        found = record.findall(path.read_text(encoding="utf-8"))
        assert not found, f"{path.relative_to(REPO)}: {found}"
