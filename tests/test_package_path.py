"""Каталог пакета, названный относительным путём (TASK-001164): «.», «./», «../pkg» и
символическая ссылка. Ключ пакета каталога равен имени каталога, а у «.» имени нет
(Path(".").name == "") — поэтому каждая команда, принимающая путь пакета, сверяет имя
только после resolve().

Решение для символической ссылки: сверяется имя ЦЕЛИ, то есть настоящего каталога с
package.yaml, а не имя ссылки. Так же ключ пакета по пути берёт resolve_targets, так же
считаются хэш содержимого и границы data-ссылок — у одного каталога одно имя, как бы его
ни назвали в команде. Следствие: ссылка «packages/<ключ>» на клон с другим именем каталога
ключ не подменяет — такой пакет ставится по пути или из git, где ключ берётся из
манифеста (TAI-ADR-0062 п.6)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from package_sdk import cli, scaffold
from package_sdk.model import PackageError, load_package, package_name, package_path

KEY = "acme-claims"


@pytest.fixture
def package(tmp_path: Path) -> Path:
    directory = tmp_path / KEY
    scaffold.init(directory, integration=True)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "link").symlink_to(directory, target_is_directory=True)
    return directory


# (каталог, из которого зовут команду, относительно tmp_path; путь пакета в команде)
FORMS = {
    "dot": (KEY, "."),
    "dot-slash": (KEY, "./"),
    "parent": ("elsewhere", f"../{KEY}"),
    "symlink": (".", "link"),
    "inside-symlink": ("link", "."),
}


@pytest.fixture(params=sorted(FORMS))
def form(request: pytest.FixtureRequest, package: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    cwd, value = FORMS[request.param]
    monkeypatch.chdir(package.parent / cwd)
    return value


def test_name_comes_from_the_resolved_directory(package: Path, form: str) -> None:
    assert Path(form).name in ("", KEY, "link")
    assert package_name(form) == KEY
    assert package_path(form) == package.resolve()
    assert load_package(Path(form)).key == KEY


def test_describe_and_docs(form: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["describe", form]) == 0
    assert cli.main(["describe", form, "--json"]) == 0
    assert cli.main(["docs", form]) == 0
    assert cli.main(["docs", form, "--write"]) == 0
    assert cli.main(["docs", form, "--check"]) == 0
    assert "does not match the directory name" not in capsys.readouterr().err


def test_add_and_edit_rename(package: Path, form: str) -> None:
    assert cli.main(["add", "rule", "on-request", "--package", form]) == 0
    assert (package / "rules" / "on-request.yaml").is_file()
    rename = ["edit", "rename", "--package", form, "--kind", "WorkRule"]
    assert cli.main([*rename, "--from", "on-request", "--to", "on-demand"]) == 0
    assert (package / "rules" / "on-demand.yaml").is_file()


def test_image(package: Path, form: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["image", "observer", "--package", form]) == 0
    assert KEY in capsys.readouterr().out


def test_check_test_and_migrate(form: str, capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip("control_plane", reason="check — кодом ядра (extra sandbox)")
    assert cli.main(["check", "--package", form]) == 0
    assert "ok: packages 1" in capsys.readouterr().out
    assert cli.main(["test", form]) == 0
    assert cli.main(["migrate-expr", "--package", form]) == 0


def test_symlink_named_as_the_key_does_not_rename_its_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout = tmp_path / "checkout-123"
    scaffold.init(tmp_path / KEY)
    (tmp_path / KEY).rename(checkout)  # клон под чужим именем каталога
    (tmp_path / KEY).symlink_to(checkout, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(PackageError, match="'checkout-123'"):
        load_package(Path(KEY))
    assert cli.main(["describe", KEY]) == 1
    assert "does not match the directory name 'checkout-123'" in capsys.readouterr().err
    assert os.path.islink(KEY)
