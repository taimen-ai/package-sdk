"""Манифест раскладки установки и workflow CI автора по нему (TAI-ADR-0064, фазы 0 и 2).

Поставляемый манифест — раскладка umbrella (фаза 2): ``services/``, ``sdk/``. Плоская
раскладка — прежний workflow байт в байт (эталоны ``fixtures/workflows/flat-*.yml``
сняты с init до манифеста); раскладка ``services/``, ``sdk/`` — пути с сегментами в
``.platform/``. Перегенерация (``package-sdk workflow``) собирает файл по манифесту
установки и сохраняет то, что автор дописал по подсказке шаблона.
"""

from __future__ import annotations

import json
import posixpath
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from package_sdk import cli, layout, scaffold
from package_sdk.layout import Layout, LayoutError
from package_sdk.model import PackageError

REPO = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "workflows"
FLAT = layout.parse(
    {
        "version": 1,
        "components": {
            name: name
            for name in ("package-sdk", "control-plane", "platform-auth-sdk", "skill-sdk")
        },
    }
)
MOVED = layout.parse(
    {
        "version": 1,
        "components": {
            "package-sdk": "sdk/package-sdk",
            "control-plane": "services/control-plane",
            "platform-auth-sdk": "sdk/platform-auth-sdk",
            "skill-sdk": "sdk/skill-sdk",
            "memory-service": "services/memory-service",
        },
    }
)


def _release(name: str) -> scaffold.Source:
    return scaffold.Source(repository=f"https://git.example/platform/{name}", tag="v0.1.4")


@pytest.fixture(autouse=True)
def _pinned_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ревизии — как у установки с тега выпуска, одинаковые на любой машине."""
    monkeypatch.setattr(scaffold, "revision", _release)


# --- манифест --------------------------------------------------------------------------


def test_the_shipped_manifest_is_the_umbrella_layout_and_knows_every_workflow_component() -> None:
    current = layout.current()
    assert not current.flat  # фаза 2: services/, sdk/
    assert dict(current.components) == dict(MOVED.components)
    assert set(scaffold.WORKFLOW_COMPONENTS) <= set(current.components)


def _path_sources() -> dict[str, str]:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    sources = data["tool"]["uv"]["sources"]
    return {name: source["path"] for name, source in sources.items() if "path" in source}


def test_the_manifest_matches_the_path_dependencies_of_the_sdk() -> None:
    """Манифест и path-зависимости pyproject.toml меняются вместе: каждая ведёт в каталог
    компонента манифеста (клиент ядра — внутрь control-plane)."""
    current = layout.current()
    sdk = current.path("package-sdk")
    sources = _path_sources()
    assert sources
    for name, path in sources.items():
        target = posixpath.normpath(posixpath.join(sdk, path))
        owners = [
            component
            for component, where in current.components.items()
            if target == where or target.startswith(where + "/")
        ]
        assert owners, f"{name}: {path} leads to {target!r}, which no component of the layout has"
        if name in current.components:
            # ../skill-sdk внутри sdk/ и ../../sdk/skill-sdk — один и тот же каталог
            via_layout = posixpath.join(sdk, current.relative("package-sdk", name))
            assert posixpath.normpath(via_layout) == target == current.path(name)


def test_the_manifest_ships_in_the_wheel_next_to_the_module() -> None:
    assert layout.MANIFEST.parent == Path(layout.__file__).parent
    assert json.loads(layout.MANIFEST.read_text(encoding="utf-8"))["version"] == layout.VERSION


def test_relative_paths_between_components_follow_the_layout() -> None:
    assert FLAT.relative("package-sdk", "control-plane") == "../control-plane"
    assert MOVED.relative("package-sdk", "control-plane") == "../../services/control-plane"
    assert MOVED.relative("control-plane", "platform-auth-sdk") == "../../sdk/platform-auth-sdk"
    assert MOVED.relative("skill-sdk", "platform-auth-sdk") == "../../sdk/platform-auth-sdk"


def test_the_root_and_the_neighbours_are_found_by_depth(tmp_path: Path) -> None:
    sdk = tmp_path / "sdk" / "package-sdk"
    sdk.mkdir(parents=True)
    assert MOVED.root(sdk) == tmp_path.resolve()
    assert MOVED.locate("control-plane", tmp_path) == tmp_path / "services" / "control-plane"
    assert FLAT.root(sdk) == sdk.parent.resolve()
    assert layout.neighbour("control-plane") == REPO.parents[1] / "services" / "control-plane"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/control-plane",
        "../control-plane",
        "services/../control-plane",
        "./control-plane",
        "services//control-plane",
        "services/control-plane/",
        "services\\control-plane",
        "Services/control-plane",
        "services/.git",
        "control-plane\n",
        "a" * 101,
        "/".join(["a" * 60] * 4),
        None,
        1,
        ["services", "control-plane"],
    ],
)
def test_a_path_outside_the_relative_form_is_rejected(path: Any) -> None:
    assert layout.path_error(path)
    with pytest.raises(LayoutError, match="control-plane"):
        layout.parse({"version": 1, "components": {"control-plane": path}})


@pytest.mark.parametrize("path", ["control-plane", "services/control-plane", "a/b/c", "0.x_y-z"])
def test_a_flat_or_segmented_path_is_accepted(path: str) -> None:
    assert layout.path_error(path) is None
    assert (
        layout.parse({"version": 1, "components": {"control-plane": path}}).path("control-plane")
        == path
    )


@pytest.mark.parametrize(
    "document, message",
    [
        (None, "not an object"),
        ([], "not an object"),
        ({"components": {"a": "a"}}, "version None"),
        ({"version": 2, "components": {"a": "a"}}, "version 2"),
        ({"version": 1}, "non-empty"),
        ({"version": 1, "components": {}}, "non-empty"),
        ({"version": 1, "components": ["a"]}, "non-empty"),
        ({"version": 1, "components": {"Bad Name": "a"}}, "component name"),
        ({"version": 1, "components": {"a": "x", "b": "x"}}, "share the directory 'x'"),
        ({"version": 1, "components": {"a": "sdk", "b": "sdk/b"}}, "b ('sdk/b') lies inside a"),
    ],
)
def test_an_inconsistent_manifest_is_rejected(document: Any, message: str) -> None:
    with pytest.raises(LayoutError, match=message.replace("(", r"\(").replace(")", r"\)")):
        layout.parse(document)


def test_siblings_with_a_common_prefix_are_not_nested() -> None:
    parsed = layout.parse({"version": 1, "components": {"a": "sdk/a", "b": "sdk/a-b"}})
    assert not parsed.flat


def test_an_unknown_component_is_an_error_not_a_guess() -> None:
    with pytest.raises(LayoutError, match="no component 'fleet'"):
        FLAT.path("fleet")


def test_an_unreadable_manifest_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(LayoutError, match="cannot read"):
        layout.load(tmp_path / "layout.json")
    (tmp_path / "layout.json").write_text("{", encoding="utf-8")
    with pytest.raises(LayoutError, match="cannot read"):
        layout.load(tmp_path / "layout.json")


# --- workflow по раскладке --------------------------------------------------------------


COMBINATIONS = [(i, d) for i in (False, True) for d in (False, True)]


def _golden(integration: bool, database: bool) -> str:
    name = (
        f"flat-{'integration' if integration else 'catalog'}-"
        f"{'database' if database else 'nodb'}.yml"
    )
    return (GOLDEN / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("integration, database", COMBINATIONS)
def test_the_flat_layout_gives_the_previous_workflow_byte_for_byte(
    integration: bool, database: bool
) -> None:
    text = scaffold.workflow(
        key="demo-pack", integration=integration, database=database, layout=FLAT
    )
    assert text == _golden(integration, database)
    # манифест установки — раскладка umbrella: по умолчанию workflow с сегментами
    assert scaffold.workflow(key="demo-pack", integration=integration, database=database) != text


def _script(text: str) -> str:
    document: dict[Any, Any] = yaml.safe_load(text)
    return "\n".join(step.get("run", "") for step in document["jobs"]["check"]["steps"])


@pytest.mark.parametrize("integration, database", COMBINATIONS)
def test_the_moved_layout_clones_into_segmented_paths(integration: bool, database: bool) -> None:
    text = scaffold.workflow(
        key="demo-pack", integration=integration, database=database, layout=MOVED
    )
    # манифест установки — раскладка umbrella: по умолчанию тот же файл
    assert scaffold.workflow(key="demo-pack", integration=integration, database=database) == text
    script = _script(text)
    clones = [line.split() for line in script.splitlines() if line.strip().startswith("clone ")]
    expected = [
        ["clone", "package-sdk", "sdk/package-sdk", '"$PACKAGE_SDK_REF"'],
        ["clone", "control-plane", "services/control-plane", '"$CONTROL_PLANE_REF"'],
        ["clone", "platform-auth-sdk", "sdk/platform-auth-sdk", '"$PLATFORM_AUTH_SDK_REF"'],
    ]
    if integration:
        expected.append(["clone", "skill-sdk", "sdk/skill-sdk", '"$SKILL_SDK_REF"'])
    assert clones == expected
    assert 'uv tool install "./sdk/package-sdk[' in script
    assert "./package-sdk[" not in script
    assert "sibling directories" not in text
    assert "in the layout of the installation" in text
    # всё, кроме раскладки, — как у плоской: env, база, шаги пирамиды
    flat = yaml.safe_load(_golden(integration, database))
    moved = yaml.safe_load(text)
    assert moved["jobs"]["check"]["env"] == flat["jobs"]["check"]["env"]
    assert moved["jobs"]["check"].get("services") == flat["jobs"]["check"].get("services")
    assert moved["jobs"]["check"]["steps"][-2:] == flat["jobs"]["check"]["steps"][-2:]


def _bare_repository(root: Path, name: str) -> None:
    work = root / "work" / name
    work.mkdir(parents=True)
    git = ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@example.org"]
    subprocess.run(["git", "init", "--quiet", str(work)], check=True)
    (work / "README").write_text(name, encoding="utf-8")
    subprocess.run([*git, "add", "README"], check=True)
    subprocess.run([*git, "commit", "--quiet", "-m", name], check=True)
    subprocess.run([*git, "tag", "v0.1.4"], check=True)
    subprocess.run(
        ["git", "clone", "--quiet", "--bare", str(work), str(root / "git" / f"{name}.git")],
        check=True,
    )


@pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None, reason="no git")
@pytest.mark.parametrize("chosen", [FLAT, MOVED], ids=["flat", "moved"])
def test_the_clone_step_lays_the_components_out_by_the_layout(
    tmp_path: Path, chosen: Layout
) -> None:
    """Шаг клонирования исполняется как есть (без установки SDK): компоненты оказываются
    там, где их ждут path-зависимости раскладки."""
    for name in scaffold.WORKFLOW_COMPONENTS:
        _bare_repository(tmp_path, name)
    text = scaffold.workflow(key="demo-pack", integration=True, layout=chosen)
    document: dict[Any, Any] = yaml.safe_load(text)
    step = next(s for s in document["jobs"]["check"]["steps"] if "clone()" in s.get("run", ""))
    script = step["run"].split("          # third-party", 1)[0].split("uv tool install", 1)[0]
    env = {
        "PATH": str(Path(shutil.which("git") or "git").parent) + ":/usr/bin:/bin",
        "HOME": str(tmp_path),
        "GIT_TERMINAL_PROMPT": "0",
        "PLATFORM_GIT": (tmp_path / "git").as_uri(),
        **{variable: "v0.1.4" for variable in scaffold.WORKFLOW_COMPONENTS.values()},
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["bash", "-e", "-c", script], cwd=workspace, env=env, check=True)
    platform = workspace / scaffold.WORKFLOW_PLATFORM_DIR
    for name in scaffold.WORKFLOW_COMPONENTS:
        placed = chosen.locate(name, platform)
        assert (placed / "README").read_text(encoding="utf-8") == name
    sdk = chosen.locate("package-sdk", platform)
    core = (sdk / chosen.relative("package-sdk", "control-plane")).resolve()
    assert core == chosen.locate("control-plane", platform).resolve()


# --- перегенерация ----------------------------------------------------------------------


def _init(tmp_path: Path, **options: Any) -> Path:
    directory = tmp_path / "demo-pack"
    scaffold.init(directory, **options)
    return directory


@pytest.mark.parametrize("integration, database", COMBINATIONS)
def test_regenerating_on_the_flat_layout_changes_nothing(
    tmp_path: Path, integration: bool, database: bool
) -> None:
    directory = _init(tmp_path, integration=integration, database=database)
    path = directory / scaffold.WORKFLOW_PATH
    # init пишет workflow по манифесту установки (раскладка umbrella); на плоскую — прежний файл
    assert scaffold.regenerate(directory, layout=FLAT).changed
    assert path.read_text(encoding="utf-8") == _golden(integration, database)
    result = scaffold.regenerate(directory, layout=FLAT)
    assert not result.changed and result.warnings == []
    assert path.read_text(encoding="utf-8") == _golden(integration, database)


@pytest.mark.parametrize("integration, database", COMBINATIONS)
def test_regenerating_on_the_moved_layout_writes_segmented_paths(
    tmp_path: Path, integration: bool, database: bool
) -> None:
    directory = _init(tmp_path, integration=integration, database=database)
    path = directory / scaffold.WORKFLOW_PATH
    # init уже написал раскладку umbrella; с плоской — обратно на сегменты
    assert not scaffold.regenerate(directory, layout=MOVED).changed
    assert scaffold.regenerate(directory, layout=FLAT).changed
    result = scaffold.regenerate(directory, layout=MOVED)
    assert result.changed and result.path == path
    text = path.read_text(encoding="utf-8")
    assert text == scaffold.workflow(
        key="demo-pack", integration=integration, database=database, layout=MOVED
    )
    # повтор ничего не меняет, обратно на плоскую — прежний файл
    assert not scaffold.regenerate(directory, layout=MOVED).changed
    assert scaffold.regenerate(directory, layout=FLAT).changed
    assert path.read_text(encoding="utf-8") == _golden(integration, database)


def test_regeneration_keeps_the_with_options_the_author_added(tmp_path: Path) -> None:
    directory = _init(tmp_path, integration=True)
    path = directory / scaffold.WORKFLOW_PATH
    text = path.read_text(encoding="utf-8")
    edited = text.replace("--with pytest\n", "--with pytest --with httpx --with 'lxml>=5'\n")
    assert edited != text
    path.write_text(edited, encoding="utf-8")
    assert not scaffold.regenerate(directory, layout=MOVED).changed
    tail = "\" --with pytest --with httpx --with 'lxml>=5'\n"
    scaffold.regenerate(directory, layout=FLAT)
    assert "./package-sdk[sandbox,skills,connector]" + tail in path.read_text(encoding="utf-8")
    scaffold.regenerate(directory, layout=MOVED)
    assert "./sdk/package-sdk[sandbox,skills,connector]" + tail in path.read_text(encoding="utf-8")


def test_regeneration_keeps_the_database_the_author_enabled(tmp_path: Path) -> None:
    directory = _init(tmp_path, database=False)
    assert scaffold.enable_database(directory)
    scaffold.regenerate(directory, layout=MOVED)
    assert scaffold.database_enabled(directory)


def test_regeneration_adds_what_the_package_grew_since_init(tmp_path: Path) -> None:
    directory = _init(tmp_path)
    (directory / "integration").mkdir()
    (directory / "rules").mkdir(exist_ok=True)
    (directory / "rules" / "x.yaml").write_text("{}\n", encoding="utf-8")
    assert scaffold.regenerate(directory, layout=FLAT).changed
    assert (directory / scaffold.WORKFLOW_PATH).read_text(encoding="utf-8") == _golden(True, True)


def test_check_mode_writes_nothing(tmp_path: Path) -> None:
    directory = _init(tmp_path)
    path = directory / scaffold.WORKFLOW_PATH
    before = path.read_bytes()
    assert scaffold.regenerate(directory, layout=FLAT, check=True).changed
    assert path.read_bytes() == before


def test_regeneration_without_a_workflow_creates_it(tmp_path: Path) -> None:
    directory = _init(tmp_path)
    (directory / scaffold.WORKFLOW_PATH).unlink()
    result = scaffold.regenerate(directory, layout=FLAT)
    assert result.changed and any("did not exist" in w for w in result.warnings)
    assert result.path.read_text(encoding="utf-8") == _golden(False, False)


def test_a_workflow_not_made_by_init_is_replaced_with_a_warning(tmp_path: Path) -> None:
    directory = _init(tmp_path)
    (directory / scaffold.WORKFLOW_PATH).write_text("name: mine\n", encoding="utf-8")
    result = scaffold.regenerate(directory, layout=FLAT)
    assert result.changed and any("not made by package-sdk init" in w for w in result.warnings)


def test_empty_revisions_are_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = _init(tmp_path)
    monkeypatch.setattr(scaffold, "revision", lambda _name: scaffold.Source())
    result = scaffold.regenerate(directory, layout=FLAT)
    assert any("PLATFORM_GIT" in w and "CONTROL_PLANE_REF" in w for w in result.warnings)


@pytest.mark.parametrize(
    "manifest, message",
    [
        (None, "no package.yaml"),
        ("kind: Package\n", "no package key"),
        ("[]\n", "no package key"),
        ("key: Not A Key\n", "no package key"),
        ("key: [1]\n", "no package key"),
    ],
)
def test_regeneration_needs_a_package(tmp_path: Path, manifest: str | None, message: str) -> None:
    if manifest is not None:
        (tmp_path / "package.yaml").write_text(manifest, encoding="utf-8")
    with pytest.raises(PackageError, match=message):
        scaffold.regenerate(tmp_path, layout=FLAT)
    assert not (tmp_path / scaffold.WORKFLOW_PATH).exists()


def test_the_cli_regenerates_and_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _init(tmp_path)
    path = directory / scaffold.WORKFLOW_PATH
    assert cli.main(["workflow", str(directory), "--check"]) == 0
    assert "unchanged" in capsys.readouterr().out
    moved = path.read_text(encoding="utf-8")
    assert moved == scaffold.workflow(key="demo-pack", integration=False, layout=MOVED)
    monkeypatch.setattr(layout, "current", lambda: FLAT)
    assert cli.main(["workflow", str(directory), "--check"]) == 1
    assert "differs from the workflow of this installation" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == moved
    monkeypatch.chdir(directory)
    assert cli.main(["workflow"]) == 0
    assert "changed" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == _golden(False, False)
    assert cli.main(["workflow", str(tmp_path / "none")]) == 1
    assert "no package.yaml" in capsys.readouterr().err
