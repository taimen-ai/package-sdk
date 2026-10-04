"""Манифест раскладки установки (TAI-ADR-0064): где в дереве установки лежит компонент.

Компонент адресуется стабильным именем (``control-plane``), а путь — данными манифеста:
``control-plane`` в плоской раскладке, ``services/control-plane`` после переезда. Манифест
``layout.json`` лежит рядом с модулем и меняется вместе с path-зависимостями
``pyproject.toml`` SDK (их сверяет тест): по нему шаблон CI автора раскладывает
``.platform/`` (:func:`package_sdk.scaffold.workflow`), а тесты SDK находят соседей.

Путь — относительный, из сегментов ``[a-z0-9][a-z0-9._-]*`` через ``/``, как
``workingCopyPath`` схемы пакетов: без ``.`` и ``..``, абсолютных путей, пустых сегментов и
обратных слэшей.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any

from package_sdk.model import PackageError

MANIFEST = Path(__file__).with_name("layout.json")
VERSION = 1
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
# Как $defs/workingCopyPath в object.schema.json
PATH = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}(?:/[a-z0-9][a-z0-9._-]{0,99})*")
PATH_MAX = 200
# Корень репозитория SDK, когда модуль работает из исходников (src/package_sdk/layout.py)
SOURCE_ROOT = Path(__file__).resolve().parents[2]
SDK = "package-sdk"


class LayoutError(PackageError):
    """Манифест раскладки не читается или противоречив."""


def path_error(path: object) -> str | None:
    """Почему ``path`` — не путь компонента в дереве установки; None — путь годится."""
    if not isinstance(path, str):
        return f"{path!r} is not a string"
    if len(path) > PATH_MAX or not PATH.fullmatch(path):
        return (
            f"{path!r} is not a relative path of segments [a-z0-9][a-z0-9._-]* joined by / "
            f"(no . or .., no leading, trailing or double /, no backslash; up to {PATH_MAX} "
            "characters)"
        )
    return None


@dataclass(frozen=True)
class Layout:
    """Компонент → путь в дереве установки."""

    components: Mapping[str, str]

    @property
    def flat(self) -> bool:
        """Плоская раскладка: каждый компонент — каталог своего имени в корне установки."""
        return all(path == name for name, path in self.components.items())

    def path(self, name: str) -> str:
        try:
            return self.components[name]
        except KeyError:
            raise LayoutError(f"the layout of the installation has no component {name!r}") from None

    def relative(self, source: str, target: str) -> str:
        """Путь от каталога компонента ``source`` к каталогу ``target`` (``../control-plane``,
        ``../../services/control-plane``) — так компоненты ссылаются друг на друга."""
        up = len(PurePosixPath(self.path(source)).parts)
        return "/".join([".."] * up + [self.path(target)])

    def root(self, location: Path, name: str = SDK) -> Path:
        """Корень установки, если компонент ``name`` лежит в ``location``."""
        depth = len(PurePosixPath(self.path(name)).parts)
        return location.resolve().parents[depth - 1]

    def locate(self, name: str, root: Path) -> Path:
        """Каталог компонента ``name`` в установке с корнем ``root``."""
        return root.joinpath(*PurePosixPath(self.path(name)).parts)


def parse(document: Any, where: str = "layout") -> Layout:
    """Манифест из разобранного JSON: версия, имена и пути, ни один каталог не делят и не
    вкладывают друг в друга два компонента."""
    if not isinstance(document, dict):
        raise LayoutError(f"{where}: not an object")
    if document.get("version") != VERSION:
        raise LayoutError(f"{where}: version {document.get('version')!r}, expected {VERSION}")
    components = document.get("components")
    if not isinstance(components, dict) or not components:
        raise LayoutError(f"{where}: components — a non-empty object name → path")
    errors: list[str] = []
    for name, path in components.items():
        if not NAME.fullmatch(name):
            errors.append(f"component name {name!r} is not a lowercase name with hyphens")
        problem = path_error(path)
        if problem:
            errors.append(f"{name}: {problem}")
    if not errors:
        for name, path in components.items():
            for other, other_path in components.items():
                if other == name:
                    continue
                if path == other_path and name < other:
                    errors.append(f"{name} and {other} share the directory {path!r}")
                elif path.startswith(other_path + "/"):
                    errors.append(f"{name} ({path!r}) lies inside {other} ({other_path!r})")
    if errors:
        raise LayoutError(f"{where}: " + "; ".join(errors))
    return Layout(dict(components))


def load(path: Path = MANIFEST) -> Layout:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LayoutError(f"{path.name}: cannot read the layout manifest: {error}") from None
    return parse(document, path.name)


@cache
def current() -> Layout:
    """Раскладка установки, в которой работает этот package-sdk."""
    return load()


def neighbour(name: str, layout: Layout | None = None) -> Path:
    """Каталог соседа ``name`` рядом с исходниками SDK — по манифесту, а не по плоскому
    ``../<name>``. Есть ли он там — решает вызывающий."""
    layout = layout or current()
    return layout.locate(name, layout.root(SOURCE_ROOT))
