"""Алиасы YAML в файлах пакета: разрешены, но их раскрытие ограничено (TASK-001231).

Алиас разделяет узел, а не копирует, и построение документа-бомбы дёшево; дорог каждый
его обход — схема, подстановка переменных, JSON для ядра, дерево правки. Загрузчик пакета
(PyYAML) и дерево правки (ruamel) отвергают бомбу и рекурсивный алиас до обхода.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from package_sdk import edit
from package_sdk.model import (
    YAML_ALIAS_NODES_MAX,
    PackageError,
    _read_yaml,
    load_package,
)

# Девять уровней по десять алиасов: 10⁹ узлов при обходе из полукилобайта текста.
LAUGHS = "\n".join(
    [
        "apiVersion: taimen.ai/v1",
        "kind: Role",
        "key: bomb",
        "spec:",
        '  name: &l0 ["lol"]',
        *(f"  l{i}: &l{i} [{', '.join([f'*l{i - 1}'] * 10)}]" for i in range(1, 10)),
        "  description: *l9",
    ]
)

RECURSIVE = "apiVersion: taimen.ai/v1\nkind: Role\nkey: loop\nspec: &a\n  self: *a\n"

# Как пишет skill-sdk export: повтор вынесен в &id001 — это читается как раньше.
SHARED = """\
apiVersion: taimen.ai/v1
kind: Role
key: shared
spec:
  name: Shared
  first: &id001
    type: string
    enum: [a, b]
  second: *id001
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "role.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_billion_laughs_is_refused_quickly(tmp_path: Path) -> None:
    started = time.monotonic()
    with pytest.raises(PackageError, match="алиасы раскрываются"):
        _read_yaml(_write(tmp_path, LAUGHS))
    assert time.monotonic() - started < 5


def test_a_recursive_alias_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PackageError, match="рекурсивный алиас"):
        _read_yaml(_write(tmp_path, RECURSIVE))


def test_shared_nodes_of_skill_sdk_export_still_load(tmp_path: Path) -> None:
    data = _read_yaml(_write(tmp_path, SHARED))
    assert data["spec"]["second"] == {"type": "string", "enum": ["a", "b"]}


def test_the_limit_counts_nodes_added_by_aliases(tmp_path: Path) -> None:
    """Под пределом — читается, над ним — нет: предел о раскрытии, а не о размере файла."""
    width = YAML_ALIAS_NODES_MAX // 2  # каждый алиас на список из одного скаляра — +2 узла
    under = "base: &b [x]\nitems: [" + ", ".join(["*b"] * (width - 1)) + "]\n"
    over = "base: &b [x]\nitems: [" + ", ".join(["*b"] * (width + 1)) + "]\n"
    assert len(_read_yaml(_write(tmp_path, under))["items"]) == width - 1
    with pytest.raises(PackageError, match="алиасы раскрываются"):
        _read_yaml(_write(tmp_path, over))


def test_a_package_with_a_bomb_does_not_load(tmp_path: Path) -> None:
    package = tmp_path / "demo"
    (package / "roles").mkdir(parents=True)
    (package / "package.yaml").write_text(
        "apiVersion: taimen.ai/v1\nkind: Package\nkey: demo\nspec:\n  version: 0.1.0\n",
        encoding="utf-8",
    )
    (package / "roles" / "bomb.yaml").write_text(LAUGHS, encoding="utf-8")
    with pytest.raises(PackageError, match="алиасы раскрываются"):
        load_package(package)


@pytest.mark.parametrize("text", [LAUGHS, RECURSIVE])
def test_the_edit_tree_refuses_them_too(tmp_path: Path, text: str) -> None:
    with pytest.raises(edit.PkgError, match="алиас") as refused:
        edit.Document.load(_write(tmp_path, text))
    assert refused.value.code == "yaml_invalid"
    assert edit.Document.parse(SHARED).data["spec"]["second"]["enum"] == ["a", "b"]
