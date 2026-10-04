"""Заготовки пакета и объектов (S009, FR-005/006, SC-001): init и add дают пакет, который
проходит check и свой тест в песочнице без стенда."""

from __future__ import annotations

import json
import posixpath
import py_compile
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from package_sdk import cli, layout, manifest, sandbox, scaffold
from package_sdk.check import CORE_MISSING, check
from package_sdk.model import CATALOG_KINDS, FOLDERS, PackageError, resolve_targets

pytest.importorskip("control_plane", reason="check и песочница — кодом ядра (extra sandbox)")

REPO = Path(__file__).resolve().parents[1]
CLAIMS = REPO / "examples" / "claims" / "claims"
REVISION = scaffold.revision  # настоящий, до подмены autouse-фикстурой


@pytest.fixture(autouse=True)
def _sdk_without_a_release_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """По умолчанию SDK и компоненты — не с тега выпуска: ссылки на схему относительные,
    ревизии в workflow пусты. Иначе прогон на теге v* самого SDK менял бы ожидания."""
    monkeypatch.setattr(scaffold, "revision", lambda _name: scaffold.Source())


def _check(directory: Path) -> tuple[list[str], list[str]]:
    errors, warnings = check(resolve_targets([str(directory)]))
    assert CORE_MISSING not in warnings
    return errors, warnings


def test_init_creates_a_package_that_passes_check_and_its_test(tmp_path: Path) -> None:
    directory = tmp_path / "demo-pack"
    created = scaffold.init(directory).created
    names = {p.relative_to(directory).as_posix() for p in created}
    assert names == {
        "package.yaml",
        "processes/demo-pack.yaml",
        "tests/demo-pack.test.yaml",
        "agents/demo-pack-process.yaml",
        "roles/demo-pack-owner.yaml",
        ".github/workflows/package.yml",
        ".gitignore",
        "README.md",
    }
    manifest = yaml.safe_load((directory / "package.yaml").read_text(encoding="utf-8"))
    assert manifest["key"] == "demo-pack"
    assert manifest["spec"]["version"] == "0.1.0"
    assert re.fullmatch(r">=\d+\.\d+,<\d+(\.\d+)?", manifest["spec"]["engines"]["control-plane"])
    assert _check(directory)[0] == []
    report = sandbox.run_package(directory)
    assert report["status"] == "passed", report
    assert (
        report["coverage"][0]["elements"]["covered"] == report["coverage"][0]["elements"]["total"]
    )
    readme = (directory / "README.md").read_text(encoding="utf-8")
    assert "<!-- package-sdk:docs -->" in readme and "<!-- /package-sdk:docs -->" in readme
    workflow = (directory / ".github" / "workflows" / "package.yml").read_text(encoding="utf-8")
    assert "package-sdk test ." in workflow and "package-sdk sandbox" not in workflow


@pytest.mark.parametrize("kind", [k for k in CATALOG_KINDS if k != "Process"])
def test_add_of_every_kind_keeps_the_package_green(tmp_path: Path, kind: str) -> None:
    directory = tmp_path / "demo"
    scaffold.init(directory)
    key = "sample.thing" if kind == "Skill" else "sample-thing"
    (path,) = scaffold.add(directory, kind, key).created
    assert path == directory / FOLDERS[kind] / f"{key}.yaml"
    errors, _ = _check(directory)
    assert errors == [], errors
    assert sandbox.run_package(directory)["status"] == "passed"


def test_add_process_brings_its_identity_owner_and_test(tmp_path: Path) -> None:
    directory = tmp_path / "demo"
    scaffold.init(directory)
    created = scaffold.add(directory, "process", "intake").created
    assert {p.relative_to(directory).as_posix() for p in created} == {
        "processes/intake.yaml",
        "agents/intake-process.yaml",
        "roles/intake-owner.yaml",
        "tests/intake.test.yaml",
    }
    assert _check(directory)[0] == []
    report = sandbox.run_package(directory)
    assert report["status"] == "passed" and len(report["tests"]) == 2


def test_object_carries_the_schema_link_and_hints(tmp_path: Path) -> None:
    directory = tmp_path / "demo"
    scaffold.init(directory)
    (path,) = scaffold.add(directory, "task-type", "review").created
    text = path.read_text(encoding="utf-8")
    link = re.match(r"# yaml-language-server: \$schema=(\S+)\n", text)
    assert link and (path.parent / link.group(1)).resolve().is_file()
    assert "# Optional fields of spec" in text
    # подсказки — из описаний схемы: поле, которого нет в заготовке, с его описанием
    assert re.search(r"^#   execution", text, re.M)
    assert re.search(r"^#   completionSchema: Work after the task completes", text, re.M)


def test_integration_and_image(tmp_path: Path) -> None:
    directory = tmp_path / "acme-sync"
    created = scaffold.init(directory, integration=True, image=True).created
    names = {p.relative_to(directory).as_posix() for p in created}
    assert {
        "agents/acme-sync-observer.yaml",
        "integration/pyproject.toml",
        "integration/src/acme_sync/__init__.py",
        "integration/src/acme_sync/observer.py",
        "Dockerfile",
    } <= names
    py_compile.compile(str(directory / "integration/src/acme_sync/observer.py"), doraise=True)
    agent = yaml.safe_load((directory / "agents/acme-sync-observer.yaml").read_text("utf-8"))
    assert agent["spec"]["executor"]["params"]["entrypoint"] == "acme_sync.observer:observe"
    assert agent["spec"]["placement"]["secrets"] == ["acme-sync-token"]
    assert "acme_sync.observer" in (directory / "Dockerfile").read_text(encoding="utf-8")
    assert _check(directory)[0] == []


def test_refusals(tmp_path: Path) -> None:
    directory = tmp_path / "demo"
    scaffold.init(directory)
    with pytest.raises(PackageError, match="already exist"):
        scaffold.init(directory)
    with pytest.raises(PackageError, match="already exist"):
        scaffold.add(directory, "Process", "demo")
    with pytest.raises(PackageError, match="unknown kind"):
        scaffold.add(directory, "Widget", "x")
    with pytest.raises(PackageError, match="lowercase Latin letters"):
        scaffold.add(directory, "Role", "Bad Key")
    with pytest.raises(PackageError, match=r"no package\.yaml"):
        scaffold.add(tmp_path / "empty", "Role", "x")
    with pytest.raises(PackageError, match="--integration"):
        scaffold.init(tmp_path / "other", image=True)
    with pytest.raises(PackageError, match="package key"):
        scaffold.init(tmp_path / "Bad_Name")


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("TaskType", "TaskType"),
        ("task-type", "TaskType"),
        ("task-types", "TaskType"),
        ("rule", "WorkRule"),
        ("process", "Process"),
        ("capability", "Capability"),
        ("notification-rule", "NotificationRule"),
    ],
)
def test_kind_names(value: str, kind: str) -> None:
    assert scaffold.resolve_kind(value) == kind


def test_cli_init_add_check_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "flow"]) == 0
    assert cli.main(["add", "rule", "on-request", "--package", "flow"]) == 0
    out = capsys.readouterr().out
    assert "created flow/package.yaml" in out and "created flow/rules/on-request.yaml" in out
    assert cli.main(["check", "--package", "flow"]) == 0
    assert "ok: packages 1" in capsys.readouterr().out
    assert cli.main(["sandbox", "flow"]) == 0
    assert "ok (passed)" in capsys.readouterr().out


def test_init_in_a_fresh_clone_keeps_readme_and_gitignore(tmp_path: Path) -> None:
    """Клон с README из шаблона хостинга: текст автора остаётся, а сгенерированный раздел
    дописывается в конец — иначе docs --check в CI красный с первого push, и add этого не
    чинит (он обновляет только раздел, который уже есть)."""
    directory = tmp_path / "clone"
    directory.mkdir()
    (directory / "README.md").write_text("# Project\n\nAbout it.\n", encoding="utf-8")
    (directory / ".gitignore").write_text("*.log\n", encoding="utf-8")
    result = scaffold.init(directory)
    assert [p.name for p in result.skipped] == [".gitignore"]
    assert result.updated == [directory / "README.md"]
    assert (directory / ".gitignore").read_text(encoding="utf-8") == "*.log\n"
    readme = (directory / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# Project\n\nAbout it.\n\n<!-- package-sdk:docs -->\n")
    assert readme.endswith("<!-- /package-sdk:docs -->\n")
    assert (directory / "package.yaml").exists()
    assert _check(directory)[0] == []
    assert cli.main(["docs", str(directory), "--check"]) == 0
    # add обновляет дописанный раздел, и CI остаётся зелёным
    assert scaffold.add(directory, "role", "reviewer").updated == [directory / "README.md"]
    assert cli.main(["docs", str(directory), "--check"]) == 0
    assert "# Project\n\nAbout it.\n" in (directory / "README.md").read_text(encoding="utf-8")


def test_init_keeps_a_readme_whose_section_is_fresh(tmp_path: Path) -> None:
    """README, в котором раздел уже свежий (повторный init после очистки), не меняется."""
    first = tmp_path / "first" / "demo"
    scaffold.init(first)
    again = tmp_path / "again" / "demo"
    again.mkdir(parents=True)
    shutil.copy(first / "README.md", again / "README.md")
    result = scaffold.init(again)
    assert again / "README.md" in result.skipped and result.updated == []
    assert (again / "README.md").read_text(encoding="utf-8") == (first / "README.md").read_text(
        encoding="utf-8"
    )


def test_init_writes_nothing_when_a_target_exists(tmp_path: Path) -> None:
    """Проверка всех путей до первой записи: init не обрывается на полпути."""
    directory = tmp_path / "half"
    (directory / "processes").mkdir(parents=True)
    (directory / "processes" / "half.yaml").write_text("x: 1\n", encoding="utf-8")
    with pytest.raises(PackageError, match=r"processes/half\.yaml"):
        scaffold.init(directory)
    assert sorted(p.name for p in directory.rglob("*") if p.is_file()) == ["half.yaml"]


def test_templates_never_install_the_sdk_from_the_public_index(tmp_path: Path) -> None:
    directory = tmp_path / "acme-sync"
    scaffold.init(directory, integration=True, image=True)
    pyproject = (directory / "integration" / "pyproject.toml").read_text(encoding="utf-8")
    assert "dependencies = []" in pyproject
    dockerfile = (directory / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG BASE_IMAGE\n" in dockerfile and "python:3.12-slim" not in dockerfile
    assert "import package_sdk.connector, control_plane_client" in dockerfile
    assert "COPY integration/src ./integration/src" in dockerfile
    assert (directory / ".dockerignore").read_text(encoding="utf-8").splitlines()[1] == "**"
    workflow = (directory / ".github" / "workflows" / "package.yml").read_text(encoding="utf-8")
    # SDK — из клона на закреплённой ревизии рядом с соседями, не по имени из индекса
    assert f'uv tool install "./{layout.current().path("package-sdk")}[' in workflow
    assert not re.search(r"(pip install|uvx --from|uv tool install) \"?package-sdk", workflow)
    assert "::error::not set:" in workflow
    # actions закреплены по SHA
    assert re.search(r"actions/checkout@[0-9a-f]{40}", workflow)
    assert re.search(r"astral-sh/setup-uv@[0-9a-f]{40}", workflow)


def _component_of(source: str, path: str) -> str | None:
    """Компонент раскладки установки, в каталог которого ведёт path-источник ``path``
    компонента ``source`` (``../../services/control-plane/client`` → ``control-plane``)."""
    current = layout.current()
    target = posixpath.normpath(posixpath.join(current.path(source), path))
    for name, where in current.components.items():
        if target == where or target.startswith(where + "/"):
            return name
    return None


def _path_neighbours(pyproject: Path, extras: set[str]) -> set[str]:
    """Компоненты-соседи, которые нужны дополнениям: path-источники [tool.uv.sources]."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    optional = data["project"].get("optional-dependencies") or {}
    sources = (data.get("tool") or {}).get("uv", {}).get("sources") or {}
    names: set[str] = set()
    pending = set(extras)
    while pending:
        extra = pending.pop()
        for requirement in optional.get(extra, []):
            name = re.split(r"[\[<>=!~ ;]", requirement, maxsplit=1)[0]
            nested = re.match(r"^[\w.-]+\[([^\]]+)\]", requirement)
            if name == data["project"]["name"] and nested:
                pending |= {e.strip() for e in nested.group(1).split(",")}
            else:
                names.add(name)
    neighbours = set()
    for name in names:
        path = (sources.get(name) or {}).get("path")
        if path and path.startswith("../"):
            component = _component_of("package-sdk", path)
            assert component, f"{name}: {path} leads to no component of the layout manifest"
            neighbours.add(component)
    return neighbours


def _subjects(package: Path) -> set[str]:
    subjects = set()
    for path in (package / "tests").glob("*.test.yaml"):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        subjects.add(str(document.get("subject") or "process"))
    return subjects


def test_workflow_for_a_package_with_integration_like_the_claims_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Эталон — пример claims: код интеграции с тестами, сценарии правил и типов задач.
    Workflow выкачивает пакет в каталог с именем ключа, ставит SDK со всеми соседями его
    дополнений, pytest для тестов кода интеграции, поднимает базу песочницы и проверяет
    раздел README."""
    key = yaml.safe_load((CLAIMS / "package.yaml").read_text(encoding="utf-8"))["key"]
    integration = (CLAIMS / "integration").is_dir()
    database = scaffold.needs_database(CLAIMS)
    assert _subjects(CLAIMS) & {"rule", "taskType"} and database
    assert integration and (CLAIMS / "integration" / "tests").is_dir()
    text = scaffold.workflow(key=key, integration=integration, database=database)
    document: dict[Any, Any] = yaml.safe_load(text)
    job = document["jobs"]["check"]
    runs = [step.get("run", "") for step in job["steps"]]
    script = "\n".join(runs)

    sdk_path = re.escape(layout.current().path("package-sdk"))
    install = re.search(rf'uv tool install "\./{sdk_path}\[([^\]]+)\]"(.*)', script)
    assert install is not None
    extras = {e.strip() for e in install.group(1).split(",")}
    assert {"sandbox", "skills", "connector"} <= extras
    assert "--with pytest" in install.group(2)

    # соседи, которых требуют дополнения: path-зависимости SDK и ядра (platform-auth-sdk)
    needed = _path_neighbours(REPO / "pyproject.toml", extras)
    assert "control-plane" in needed
    # соседи ядра — из его pyproject.toml; ядра рядом нет — явный провал, а не догадка
    # (TAI-ADR-0064 правило 4): extra sandbox ставит его из этого каталога
    core = layout.neighbour("control-plane") / "pyproject.toml"
    assert core.is_file(), f"no {core}: the core is not next to the SDK in the layout manifest"
    core_data = tomllib.loads(core.read_text(encoding="utf-8"))
    for source in ((core_data.get("tool") or {}).get("uv", {}).get("sources") or {}).values():
        if str(source.get("path", "")).startswith("../"):
            component = _component_of("control-plane", source["path"])
            assert component, f"{source['path']} of the core leads to no component of the layout"
            needed.add(component)
    assert "platform-auth-sdk" in needed
    # clone <имя> "$REF" в плоской раскладке, clone <имя> <путь> "$REF" — с сегментами
    clones = re.findall(
        r'^\s*clone ([a-z-]+)(?: ([a-z0-9][a-z0-9/._-]*))? "\$([A-Z_]+)"$', script, re.M
    )
    for name, path, _var in clones:
        assert (path or name) == layout.current().path(name)
    cloned = {(name, variable) for name, _path, variable in clones}
    assert {name for name, _var in cloned} == needed | {"package-sdk"}

    # у каждого компонента одна переменная ревизии, заданная один раз в env job и больше
    # нигде: шаги ссылаются на неё, а не повторяют значение
    env = job["env"]
    for name, variable in cloned:
        assert scaffold.WORKFLOW_COMPONENTS[name] == variable
        assert variable in env
        assert len(re.findall(rf"^\s*{variable}:", text, re.M)) == 1
    assert not re.search(r"--branch|@v\d|package-sdk\.git@", script)

    # база песочницы для сценариев правил и типов задач
    assert "postgres" in job["services"]
    assert env["PACKAGE_SDK_SANDBOX_DATABASE_URL"].startswith("postgresql://")

    # пакет — в каталоге с именем ключа, пирамида и раздел README — из него
    checkout = next(
        s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["path"] == key
    steps = {step.get("run"): step for step in job["steps"] if "run" in step}
    assert steps["package-sdk test ."]["working-directory"] == key
    assert steps["package-sdk docs . --check"]["working-directory"] == key
    # компоненты — в своём каталоге: с ключом пакета он совпасть не может
    assert re.search(rf"cd {re.escape(scaffold.WORKFLOW_PLATFORM_DIR)}\n", script)
    assert not scaffold.KEY.match(scaffold.WORKFLOW_PLATFORM_DIR)

    # так, как в CI: копия пакета в каталоге checkout, команды — из working-directory
    # (сценарии правил без базы не идут — вместо test здесь его первая ступень, check)
    package = tmp_path / checkout["with"]["path"]
    shutil.copytree(CLAIMS, package, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.chdir(package)
    assert cli.main(["check", "--package", "."]) == 0
    assert cli.main(["docs", ".", "--check"]) == 0


def test_workflow_without_integration_and_database() -> None:
    text = scaffold.workflow(key="demo", integration=False)
    job = yaml.safe_load(text)["jobs"]["check"]
    assert "services" not in job and "# services:" in text
    assert "PACKAGE_SDK_SANDBOX_DATABASE_URL" not in job["env"]
    assert "SKILL_SDK_REF" not in job["env"] and "clone skill-sdk" not in text
    assert f'uv tool install "./{layout.current().path("package-sdk")}[sandbox]"\n' in text


def test_workflow_pins_the_revisions_of_the_installation(monkeypatch: pytest.MonkeyPatch) -> None:
    revisions = {
        "package-sdk": scaffold.Source("https://git.example/acme/package-sdk", "v0.2.0", "a" * 40),
        "control-plane": scaffold.Source("https://git.example/acme/control-plane", "v0.9.3"),
        "platform-auth-sdk": scaffold.Source(None, None, "c" * 40),
        "skill-sdk": scaffold.Source(),
    }
    monkeypatch.setattr(scaffold, "revision", revisions.__getitem__)
    env = yaml.safe_load(scaffold.workflow(key="demo", integration=True))["jobs"]["check"]["env"]
    assert env["PLATFORM_GIT"] == "https://git.example/acme"
    assert env["PACKAGE_SDK_REF"] == "v0.2.0"
    assert env["CONTROL_PLANE_REF"] == "v0.9.3"
    assert env["PLATFORM_AUTH_SDK_REF"] == "c" * 40
    assert env["SKILL_SDK_REF"] == ""  # пусто — job остановит шаг «Pinned revisions»


def test_init_with_integration_writes_that_workflow_and_a_fresh_readme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "acme-sync"
    scaffold.init(directory, integration=True)
    workflow = (directory / ".github" / "workflows" / "package.yml").read_text(encoding="utf-8")
    assert workflow == scaffold.workflow(key="acme-sync", integration=True)
    assert yaml.safe_load(workflow)["jobs"]["check"]["steps"][1]["with"]["path"] == "acme-sync"
    monkeypatch.chdir(directory)  # как в CI: из каталога пакета
    assert cli.main(["check", "--package", "."]) == 0
    assert cli.main(["docs", ".", "--check"]) == 0


def _workflow_of(directory: Path) -> str:
    return (directory / ".github" / "workflows" / "package.yml").read_text(encoding="utf-8")


@pytest.mark.parametrize("kind", ["rule", "task-type"])
def test_add_of_a_rule_or_task_type_enables_the_database(tmp_path: Path, kind: str) -> None:
    directory = tmp_path / "demo"
    scaffold.init(directory)
    assert "services" not in yaml.safe_load(_workflow_of(directory))["jobs"]["check"]
    result = scaffold.add(directory, kind, "sample")
    assert result.updated == [
        directory / "README.md",
        directory / ".github" / "workflows" / "package.yml",
    ]
    assert cli.main(["docs", str(directory), "--check"]) == 0  # раздел README свежий
    # тот же файл, что init написал бы для пакета с базой
    assert _workflow_of(directory) == scaffold.workflow(
        key="demo", integration=False, database=True
    )
    # база уже включена — меняется только раздел README, предупреждать не о чем
    result = scaffold.add(directory, "role", "other")
    assert result.updated == [directory / "README.md"] and result.warnings == []


def test_add_leaves_an_edited_workflow_alone(tmp_path: Path) -> None:
    directory = tmp_path / "demo"
    scaffold.init(directory)
    path = directory / ".github" / "workflows" / "package.yml"
    edited = _workflow_of(directory).replace("#     image: postgres:16", "#     image: mine")
    path.write_text(edited, encoding="utf-8")
    result = scaffold.add(directory, "rule", "sample")
    assert result.updated == [directory / "README.md"]
    assert _workflow_of(directory) == edited
    # пакету теперь нужна база, а блок правили руками: add не молчит
    assert len(result.warnings) == 1
    assert ".github/workflows/package.yml" in result.warnings[0]
    assert "enable the PostgreSQL database in the workflow manually" in result.warnings[0]


def test_add_does_not_warn_when_the_author_enabled_the_database_by_hand(tmp_path: Path) -> None:
    """Блок правили (образ закреплён по digest), но базу включили сами — подсказка лишняя."""
    directory = tmp_path / "demo"
    scaffold.init(directory, database=True)
    path = directory / ".github" / "workflows" / "package.yml"
    edited = _workflow_of(directory).replace("image: postgres:16", "image: postgres:16@sha256:0")
    path.write_text(edited, encoding="utf-8")
    result = scaffold.add(directory, "task-type", "sample")
    assert result.warnings == [] and _workflow_of(directory) == edited


def test_add_without_a_workflow_does_not_warn(tmp_path: Path) -> None:
    """Пакет проверяется не заготовкой init (workflow удалён) — подсказывать нечего."""
    directory = tmp_path / "demo"
    scaffold.init(directory)
    (directory / ".github" / "workflows" / "package.yml").unlink()
    assert scaffold.add(directory, "rule", "sample").warnings == []


def test_cli_add_prints_the_database_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "flow"]) == 0
    path = tmp_path / "flow" / ".github" / "workflows" / "package.yml"
    path.write_text(
        _workflow_of(tmp_path / "flow").replace("#     image: postgres:16", "#     image: mine"),
        encoding="utf-8",
    )
    capsys.readouterr()
    assert cli.main(["add", "rule", "on-request", "--package", "flow"]) == 0
    out = capsys.readouterr().out
    assert "warning: flow/.github/workflows/package.yml: " in out
    assert "enable the PostgreSQL database in the workflow manually" in out


def test_init_detects_the_database_and_takes_the_flag(tmp_path: Path) -> None:
    clone = tmp_path / "flow"
    (clone / "tests").mkdir(parents=True)
    (clone / "tests" / "on-request.test.yaml").write_text(
        "subject: rule\nrule: on-request\nname: x\n", encoding="utf-8"
    )
    assert scaffold.needs_database(clone)
    scaffold.init(clone)
    assert "postgres" in yaml.safe_load(_workflow_of(clone))["jobs"]["check"]["services"]
    scaffold.init(tmp_path / "plain", database=True)
    assert "services" in yaml.safe_load(_workflow_of(tmp_path / "plain"))["jobs"]["check"]
    scaffold.init(tmp_path / "bare")
    assert not scaffold.needs_database(tmp_path / "bare")


def test_cli_init_database_flag_and_add_reports_the_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "flow"]) == 0
    assert cli.main(["add", "rule", "on-request", "--package", "flow"]) == 0
    assert "changed flow/.github/workflows/package.yml" in capsys.readouterr().out
    assert cli.main(["init", "other", "--database"]) == 0
    assert "services" in yaml.safe_load(_workflow_of(tmp_path / "other"))["jobs"]["check"]


def _direct_url(monkeypatch: pytest.MonkeyPatch, info: dict[str, Any]) -> None:
    class Dist:
        def read_text(self, name: str) -> str:
            assert name == "direct_url.json"
            return json.dumps(info)

    monkeypatch.setattr("importlib.metadata.distribution", lambda _name: Dist())
    # autouse-фикстура подменила revision — здесь нужен настоящий, без кэша прошлых вызовов
    monkeypatch.setattr(scaffold, "revision", REVISION)
    REVISION.cache_clear()


def test_revision_from_a_git_install(monkeypatch: pytest.MonkeyPatch) -> None:
    info = {
        "url": "https://git.example/acme/package-sdk.git",
        "vcs_info": {"vcs": "git", "commit_id": "a" * 40, "requested_revision": "v0.1.0"},
    }
    _direct_url(monkeypatch, info)
    assert scaffold.revision("package-sdk") == scaffold.Source(
        "https://git.example/acme/package-sdk", "v0.1.0", "a" * 40
    )
    info["vcs_info"]["requested_revision"] = "main"  # type: ignore[index]
    _direct_url(monkeypatch, info)
    assert scaffold.revision("package-sdk").ref == "a" * 40


def test_revision_from_a_clone_on_a_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clone = tmp_path / "package-sdk"
    clone.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(clone), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    git(*identity, "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "x")
    commit = git("rev-parse", "HEAD")
    _direct_url(monkeypatch, {"url": clone.as_uri(), "dir_info": {"editable": True}})
    assert scaffold.revision("package-sdk") == scaffold.Source(None, None, commit)
    git("tag", "v0.2.0")
    git("remote", "add", "origin", "git@git.example:acme/package-sdk.git")
    _direct_url(monkeypatch, {"url": clone.as_uri(), "dir_info": {}})
    assert scaffold.revision("package-sdk") == scaffold.Source(
        "https://git.example/acme/package-sdk", "v0.2.0", commit
    )


@pytest.mark.parametrize(
    ("remote", "web"),
    [
        ("https://git.example/acme/package-sdk.git", "https://git.example/acme/package-sdk"),
        ("https://user:secret@git.example:8443/acme/sdk/", "https://git.example:8443/acme/sdk"),
        ("git@git.example:acme/package-sdk.git", "https://git.example/acme/package-sdk"),
        ("ssh://git@git.example:2222/acme/package-sdk.git", "https://git.example/acme/package-sdk"),
        ("file:///srv/package-sdk", None),
        ("http://git.example/acme/package-sdk", None),
    ],
)
def test_web_url(remote: str, web: str | None) -> None:
    assert scaffold.web_url(remote) == web


def test_schema_link_is_the_release_url_on_a_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = scaffold.Source("https://git.example/acme/package-sdk", "v0.2.0", "a" * 40)
    monkeypatch.setattr(scaffold, "revision", lambda _name: source)
    directory = tmp_path / "demo"
    scaffold.init(directory)
    base = "https://git.example/acme/package-sdk/raw/v0.2.0/"
    for path, name in (
        (directory / "package.yaml", "object.schema.json"),
        (directory / "processes" / "demo.yaml", "object.schema.json"),
        (directory / "tests" / "demo.test.yaml", "test.schema.json"),
    ):
        link = re.match(r"# yaml-language-server: \$schema=(\S+)\n", path.read_text("utf-8"))
        assert link and link.group(1).startswith(base), path
        # путь в репозитории выпуска — тот, где схема лежит в этом репозитории
        relative = link.group(1).removeprefix(base)
        assert relative == f"schema/v1/{name}" and (REPO / relative).is_file()


@pytest.mark.parametrize(
    "source",
    [
        scaffold.Source("https://git.example/acme/package-sdk", None, "a" * 40),  # dev
        scaffold.Source(None, "v0.2.0", "a" * 40),  # адрес репозитория неизвестен
    ],
)
def test_schema_link_falls_back_to_the_installed_schema_without_a_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: scaffold.Source
) -> None:
    monkeypatch.setattr(scaffold, "revision", lambda _name: source)
    directory = tmp_path / "demo"
    scaffold.init(directory)
    text = (directory / "package.yaml").read_text(encoding="utf-8")
    link = re.match(r"# yaml-language-server: \$schema=(\S+)\n", text)
    assert link and "://" not in link.group(1)
    assert (directory / link.group(1)).resolve().is_file()


def test_license_is_not_assumed(tmp_path: Path) -> None:
    scaffold.init(tmp_path / "p1")
    assert "license" not in yaml.safe_load((tmp_path / "p1" / "package.yaml").read_text())["spec"]
    scaffold.init(tmp_path / "p2", license="MIT")
    assert (
        yaml.safe_load((tmp_path / "p2" / "package.yaml").read_text())["spec"]["license"] == "MIT"
    )


def test_long_process_key_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "demo"
    scaffold.init(directory)
    with pytest.raises(PackageError, match="is longer than"):
        scaffold.add(directory, "process", "p" * 56)
    scaffold.add(directory, "process", "p" * scaffold.PROCESS_KEY_MAX)
    assert _check(directory)[0] == []


def test_hints_are_single_line_comments(tmp_path: Path) -> None:
    directory = tmp_path / "demo"
    scaffold.init(directory)
    for kind in CATALOG_KINDS:
        key = "hint.check" if kind == "Skill" else f"hint-{kind.lower()}"
        (path, *_rest) = scaffold.add(directory, kind, key).created
        text = path.read_text(encoding="utf-8")
        yaml.safe_load(text)  # комментарии не ломают YAML
        for line in text.splitlines():
            assert not line.startswith((" ", "\t")) or not line.lstrip().startswith("#   ")


# Буквы кириллицы: текст генератора в пакете автора — английский (TASK-001198).
CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def test_the_generated_files_speak_english(tmp_path: Path) -> None:
    """Что пишет сам генератор (комментарии, заготовки значений, шаблоны кода, CI, образ,
    README вместе с разделом package-sdk docs), идёт по-английски. По-русски остаются только
    строки из описаний схемы (описание вида и подсказки полей)."""
    directory = tmp_path / "acme-sync"
    created = scaffold.init(directory, integration=True, image=True).created
    for path in created:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".yaml":
            kind = yaml.safe_load(text).get("kind", "")
            text = "\n".join(
                line
                for line in text.splitlines()
                if not line.startswith((f"# {kind}: ", "#   "))  # из описаний схемы
            )
        assert not CYRILLIC.search(text), (path.relative_to(directory), CYRILLIC.search(text))


def test_the_env_example_speaks_english(tmp_path: Path) -> None:
    """Заготовку describe --env-example автор сохраняет в файл пакета (TASK-001252): текст
    генератора в ней по-английски. Описания и примеры переменных — из пакета, здесь они
    английские, поэтому кириллицы во всей заготовке быть не должно. Покрыты все ветки:
    обязательная с примером, необязательная с default, без описания, не объявленная."""
    directory = tmp_path / "acme-sync"
    scaffold.init(directory)
    manifest_path = directory / "package.yaml"
    document = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    document["spec"]["variables"] = {
        "ACME_SYNC_URL": {
            "kind": "url",
            "description": "API the observer polls",
            "example": "https://acme.example.com/api",
        },
        "ACME_SYNC_LIMIT": {"kind": "integer", "description": "Batch size", "default": "50"},
        "ACME_SYNC_REGION": {"kind": "string"},
    }
    manifest_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    role_path = directory / "roles" / "acme-sync-owner.yaml"
    role = yaml.safe_load(role_path.read_text(encoding="utf-8"))
    role["spec"]["name"] = "Owner of ${ACME_SYNC_TEAM}"  # используется, но не объявлена
    role_path.write_text(yaml.safe_dump(role, sort_keys=False), encoding="utf-8")

    package, installation, problem = manifest.load_for_describe(directory)
    assert problem is None
    example = manifest.env_example(manifest.describe(package, installation))
    assert "# API the observer polls [url, required]\n" in example
    assert "# example: https://acme.example.com/api\nACME_SYNC_URL=\n" in example
    assert "# Batch size [integer, optional]\nACME_SYNC_LIMIT=50\n" in example
    assert "# (no description) [string, required]\nACME_SYNC_REGION=\n" in example
    assert "[string, required, not declared in spec.variables]\nACME_SYNC_TEAM=\n" in example
    assert not CYRILLIC.search(example), CYRILLIC.search(example)
