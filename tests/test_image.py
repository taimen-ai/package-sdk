"""Dockerfile образов пакета интеграции (S018, FR-027): наблюдатель с единой точкой
входа и хост скиллов. Сборка образа — при PACKAGE_SDK_DOCKER=1 (CI с docker)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from package_sdk import cli, image, layout
from package_sdk.model import PackageError

FEED = Path(__file__).parent / "fixtures" / "connector" / "sample-feed"
SDK = Path(__file__).resolve().parents[1]


def test_observer_dockerfile_has_one_entrypoint_and_checks_the_observer() -> None:
    text = image.observer_dockerfile(
        FEED, entrypoint="sample_feed.observer:observe", base="registry.example/observer:0.1"
    )
    assert "ARG BASE_IMAGE=registry.example/observer:0.1" in text and "FROM ${BASE_IMAGE}" in text
    assert "COPY integration/pyproject.toml ./integration/pyproject.toml" in text
    assert "COPY integration/src ./integration/src" in text
    assert "--constraint /tmp/platform-constraints.txt ./integration" in text
    assert "load_entrypoint('sample_feed.observer:observe')" in text
    assert text.rstrip().endswith('ENTRYPOINT ["python", "-m", "package_sdk.connector"]')
    assert "USER connector" in text and "CONNECTOR_DATA_DIR=/data" in text
    # без --entrypoint проверки при сборке нет: наблюдателя найдёт процесс по ревизии
    assert "load_entrypoint" not in image.observer_dockerfile(FEED)


def test_platform_components_never_come_from_the_public_index() -> None:
    """SDK и клиент ядра — только из базового образа поставки (dependency confusion)."""
    for text in (
        image.observer_dockerfile(FEED),
        image.skills_dockerfile(FEED, modules=["sample_feed.skills"]),
    ):
        installs = [line for line in text.splitlines() if "pip install" in line]
        assert installs and all("package-sdk" not in line for line in installs), installs
        # версии платформы из базового образа — ограничения установки интеграции
        assert all("--constraint /tmp/platform-constraints.txt" in line for line in installs)
        assert "PACKAGE_SDK" not in text
        assert "import package_sdk.connector, control_plane_client" in text or (
            "import skill_sdk, control_plane_client" in text
        )
    # базовый образ по умолчанию не задан: ни python:3.12-slim, ни иного
    assert "ARG BASE_IMAGE\n" in image.observer_dockerfile(FEED)
    assert "--build-arg BASE_IMAGE=<image>" in image.observer_dockerfile(FEED)
    assert "ARG RUNNER_IMAGE\n" in image.skills_dockerfile(FEED, modules=["a"])


def test_build_context_holds_only_the_integration() -> None:
    ignore = image.dockerignore(FEED)
    assert ignore.splitlines()[1:6] == [
        "**",
        "!integration/pyproject.toml",
        "!integration/README.md",
        "!integration/src/**",
        "**/__pycache__",
    ]
    text = image.observer_dockerfile(FEED)
    assert "COPY . " not in text and "COPY integration/ " not in text


def test_integration_at_the_package_root(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    text = image.observer_dockerfile(tmp_path)
    assert "COPY pyproject.toml ./integration/pyproject.toml" in text
    assert "COPY README.md ./integration/README.md" in text
    assert "COPY src ./integration/src" in text
    assert "!pyproject.toml" in image.dockerignore(tmp_path)


def test_skills_dockerfile() -> None:
    text = image.skills_dockerfile(FEED, modules=["sample_feed.skills", "sample_feed.x:run"])
    assert "FROM ${RUNNER_IMAGE}" in text
    assert 'CONTROL_PLANE_SKILLS_LOCAL_PACKAGES="sample_feed.skills,sample_feed.x:run"' in text
    assert "COPY integration/src /opt/package/integration/src" in text
    assert "RUNNER_MODE=skills" in text and text.rstrip().endswith("USER 10001")


def test_refusals(tmp_path: Path) -> None:
    with pytest.raises(PackageError, match=r"no pyproject\.toml"):
        image.observer_dockerfile(tmp_path)
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    with pytest.raises(PackageError, match="src/"):
        image.observer_dockerfile(tmp_path)
    with pytest.raises(PackageError, match="module:function"):
        image.observer_dockerfile(FEED, entrypoint="not-an-entrypoint")
    with pytest.raises(PackageError, match="at least one"):
        image.skills_dockerfile(FEED, modules=[])
    with pytest.raises(PackageError, match="not an image reference"):
        image.observer_dockerfile(FEED, base="bad image; rm -rf /")


def test_cli_writes_the_file_and_the_dockerignore(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = tmp_path / "sample-feed"
    shutil.copytree(FEED, package)
    out = package / "Dockerfile"
    args = ["image", "observer", "--package", str(package), "--out", str(out)]
    assert cli.main(args) == 0
    assert out.read_text(encoding="utf-8").startswith(
        "# The observer image of the package sample-feed"
    )
    assert (package / ".dockerignore").read_text(encoding="utf-8") == image.dockerignore(package)
    (package / ".dockerignore").write_text("*.log\n", encoding="utf-8")
    assert cli.main(args) == 0  # чужой .dockerignore не перезаписывается — предупреждение
    assert "differs from the required one" in capsys.readouterr().err
    assert cli.main(["image", "skills", "--package", str(FEED), "--modules", "a.b"]) == 0
    assert "CONTROL_PLANE_SKILLS_LOCAL_PACKAGES" in capsys.readouterr().out
    assert cli.main(["image", "observer", "--package", str(tmp_path / "none")]) == 1


@pytest.mark.skipif(
    os.environ.get("PACKAGE_SDK_DOCKER") != "1" or shutil.which("docker") is None,
    reason="сборка образа — PACKAGE_SDK_DOCKER=1 и docker",
)
def test_observer_image_of_the_fixture_builds(tmp_path: Path) -> None:
    """Образ наблюдателя-фикстуры собирается: SDK и клиент ядра — колёсами из исходников
    (базовый образ с ними), наблюдатель проверяется при сборке."""
    wheels = tmp_path / "base" / "wheels"
    wheels.mkdir(parents=True)
    for source in (SDK, layout.neighbour("control-plane") / "client"):
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(wheels), str(source)], check=True
        )
    (tmp_path / "base" / "Dockerfile").write_text(
        "FROM python:3.12-slim\nCOPY wheels /wheels\n"
        "RUN pip install --no-cache-dir hatchling /wheels/*.whl\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["docker", "build", "-t", "package-sdk-test-base", str(tmp_path / "base")], check=True
    )
    context = tmp_path / "package"
    shutil.copytree(FEED, context)
    (context / ".env").write_text("SECRET=must-not-leak\n", encoding="utf-8")
    assert (
        cli.main(
            [
                "image",
                "observer",
                "--package",
                str(context),
                "--entrypoint",
                "sample_feed.observer:observe",
                "--base",
                "package-sdk-test-base",
                "--out",
                str(context / "Dockerfile"),
            ]
        )
        == 0
    )
    subprocess.run(["docker", "build", "-t", "package-sdk-test-observer", str(context)], check=True)
    listing = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "ls", "package-sdk-test-observer", "-a", "/srv"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert ".env" not in listing


def test_the_claims_example_is_what_the_generator_writes() -> None:
    """Образы примера претензий — вывод генератора байт в байт (TASK-001198): пример и
    генератор говорят по-английски одними словами и не расходятся."""
    package = SDK / "examples" / "claims" / "claims"
    observer = image.observer_dockerfile(package, entrypoint="claims_helpdesk.observer:observe")
    skills = image.skills_dockerfile(
        package, modules=["claims_helpdesk.skills"], dockerfile="Dockerfile.skills"
    )
    assert observer == (package / "Dockerfile").read_text(encoding="utf-8")
    assert skills == (package / "Dockerfile.skills").read_text(encoding="utf-8")
    assert image.dockerignore(package) == (package / ".dockerignore").read_text(encoding="utf-8")
