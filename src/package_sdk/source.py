"""Тело запросов /packages:* ядра из файлов пакета — одна сборка для CLI, песочницы и MCP
(TAI-ADR-0062 п.2)."""

from __future__ import annotations

import hashlib
from typing import Any

from package_sdk.manifest import missing_variable, package_env
from package_sdk.model import (
    DEFAULT_REPLAY_LIMIT,
    Package,
    PackageError,
    PackageTest,
    canonical,
    substitute,
)

# Файлы пакета, которые уходят ядру: описания, схемы данных, тесты. Раскладка схемы
# (.layout) логики не несёт и не отправляется.
_SENT_SUFFIXES = (".yaml", ".yml", ".json")
# Виды, которые регистрируются не в каталоге ядра, а своим вызовом.
_NOT_CORE_KINDS = frozenset({"KnowledgePack"})


def package_files(package: Package, env: dict[str, str], *, strict: bool) -> list[dict[str, str]]:
    """PackageSource.files: [{path, content}] с подставленными ${ПЕРЕМЕННЫМИ} установки —
    значения установки поверх default манифеста, как у check и apply (FR-010).
    strict — незаданная переменная без default — ошибка с её описанием (план и
    применение); иначе остаётся как есть (тесты и песочница)."""
    values = package_env(package, env)

    def missing(name: str) -> str:
        if strict:
            raise PackageError(missing_variable(package, name))
        return "${" + name + "}"

    # KnowledgePack ядро не разбирает: онтология регистрируется в памяти своим вызовом
    # (POST /knowledge/packs), в тест и план ядра её файлы не уходят.
    own = {o.path.resolve() for o in package.objects if o.kind in _NOT_CORE_KINDS}
    files: list[dict[str, str]] = []
    for path in sorted(package.path.rglob("*")):
        if path.resolve() in own:
            continue
        inner = path.relative_to(package.path)
        if (
            not path.is_file()
            or path.suffix not in _SENT_SUFFIXES
            or any(part.startswith(".") for part in inner.parts)
        ):
            continue
        text = path.read_text(encoding="utf-8")
        files.append(
            {
                "path": inner.as_posix(),
                "content": substitute(text, values, missing=missing),
            }
        )
    return files


def package_source(package: Package, env: dict[str, str], *, strict: bool) -> dict[str, Any]:
    return {"files": package_files(package, env, strict=strict)}


def test_request(
    package: Package,
    env: dict[str, str],
    *,
    tests: list[PackageTest] | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """PackageTestRequest; tests — фильтр путей файлов тестов (None — все)."""
    body: dict[str, Any] = {"package": package_source(package, env, strict=False)}
    if tests is not None:
        body["tests"] = [t.path.relative_to(package.path).as_posix() for t in tests]
    if workspace:
        body["workspaceId"] = workspace
    return body


def plan_request(
    package: Package,
    env: dict[str, str],
    *,
    workspace: str | None = None,
    replay_limit: int = DEFAULT_REPLAY_LIMIT,
    overwrite_console: bool = False,
) -> dict[str, Any]:
    """Тело ``POST /packages:plan``. ``overwrite_console`` — перезаписать поля, которые человек
    правил в консоли после прошлого применения (``overwriteConsole`` ядра); без флага поле не
    отправляется и ядро такие поля сохраняет."""
    body: dict[str, Any] = {
        "package": package_source(package, env, strict=True),
        "replayLimit": replay_limit,
    }
    if workspace:
        body["workspaceId"] = workspace
    if overwrite_console:
        body["overwriteConsole"] = True
    return body


def request_hash(body: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical(body).encode()).hexdigest()
