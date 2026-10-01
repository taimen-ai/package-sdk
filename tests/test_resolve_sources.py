"""Where a package of an installation is found (TAI-ADR-0062 п.6, S006)."""

from __future__ import annotations

from pathlib import Path

import pytest

from package_sdk import model


def write_package(directory: Path, key: str, version: str = "0.1.0") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "package.yaml").write_text(
        f"apiVersion: taimen.ai/v1\nkind: Package\nkey: {key}\n"
        f"spec: {{version: {version}, displayName: {key}}}\n",
        encoding="utf-8",
    )


def write_installation(path: Path, packages: str, extra: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "apiVersion: taimen.ai/v1\nkind: Installation\nkey: demo\n"
        f"spec:\n  packages: {packages}\n{extra}",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(model, "ROOT", tmp_path)
    monkeypatch.setattr(model, "PACKAGES_DIR", tmp_path / "packages")
    return tmp_path


def test_package_next_to_the_installation(project: Path) -> None:
    install = write_installation(project / "e2e" / "installation.yaml", "[local]")
    write_package(project / "e2e" / "local", "local")
    assert [p.key for p in model.load_installation(install).packages] == ["local"]


def test_packages_dir_next_to_the_installation(project: Path) -> None:
    install = write_installation(project / "env" / "installation.yaml", "[local]")
    write_package(project / "env" / "packages" / "local", "local")
    assert (
        model.load_installation(install).packages[0].path == project / "env" / "packages" / "local"
    )


def test_packages_dir_of_the_installation(project: Path) -> None:
    install = write_installation(
        project / "env" / "installation.yaml", "[local]", "  packagesDir: ../catalog\n"
    )
    write_package(project / "catalog" / "local", "local")
    assert model.load_installation(install).packages[0].path == project / "catalog" / "local"


def test_installation_package_wins_over_the_project_catalog(project: Path) -> None:
    write_package(project / "packages" / "shared", "shared", "1.0.0")
    install = write_installation(project / "env" / "installation.yaml", "[shared]")
    write_package(project / "env" / "packages" / "shared", "shared", "2.0.0")
    assert model.load_installation(install).packages[0].spec["version"] == "2.0.0"


def test_project_catalog_is_the_fallback(project: Path) -> None:
    write_package(project / "packages" / "shared", "shared")
    install = write_installation(project / "deploy" / "installation.yaml", "[shared]")
    assert model.load_installation(install).packages[0].path == project / "packages" / "shared"


def test_package_by_path(project: Path) -> None:
    write_package(project / "elsewhere" / "local", "local")
    install = write_installation(
        project / "env" / "installation.yaml", "[{key: local, path: ../elsewhere/local}]"
    )
    assert model.load_installation(install).packages[0].path == project / "elsewhere" / "local"


def test_git_source_needs_a_lock(project: Path) -> None:
    install = write_installation(
        project / "env" / "installation.yaml",
        "[{key: remote, git: https://git.example.com/acme/remote.git, ref: v1.0.0}]",
    )
    with pytest.raises(model.PackageError, match="lock"):
        model.load_installation(install)


def test_missing_package_names_every_place_searched(project: Path) -> None:
    install = write_installation(project / "env" / "installation.yaml", "[absent]")
    with pytest.raises(model.PackageError) as error:
        model.load_installation(install)
    assert "env/packages" in str(error.value)
    assert "packages" in str(error.value)
