"""The moved tests addressed one module ``cp_packages`` of the umbrella; the SDK splits it (S006).

``cp`` forwards attribute reads to the module that defines the name and writes
(monkeypatch) to every module that holds it, so the tests run unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn

import pytest

from package_sdk import apply, check, commands, core, migrate, model, source

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
# Корень по умолчанию — обезличенный снимок пакетов и установок umbrella в фикстурах
# компонента: тесты идут везде (зеркало, CI компонента, рабочая копия раннера).
# PACKAGE_SDK_UMBRELLA направляет их на живое дерево суперпроекта (его CI).
UMBRELLA = Path(os.environ.get("PACKAGE_SDK_UMBRELLA") or FIXTURES / "umbrella")
MODULES: tuple[ModuleType, ...] = (model, source, check, apply, core, migrate, commands)


class _Facade:
    def __getattr__(self, name: str) -> Any:
        for module in MODULES:
            if name in vars(module):
                return vars(module)[name]
        if name == "main":
            return commands.main
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        holders = [m for m in MODULES if name in vars(m)]
        if not holders:
            raise AttributeError(name)
        for module in holders:
            setattr(module, name, value)


cp = _Facade()


# Предупреждения режима перехода (TAI-ADR-0062 п.4, plan Р3): пакеты платформы переходят на
# манифест с variables и knowledge в S014 — до тех пор их check честно о них говорит.
TRANSITION = ("variable_undeclared: пакет прежней формы", "knowledge_undeclared:")


def settled(result: tuple[list[str], list[str]]) -> tuple[list[str], list[str]]:
    """Итог check без предупреждений режима перехода."""
    errors, warnings = result
    return errors, [w for w in warnings if not any(mark in w for mark in TRANSITION)]


# make test (CI компонента и проверки исполнителя) ставит все экстры — ядро-сосед тут есть — и
# выставляет эту переменную: несверенный контракт ядра там провал, а не пропуск (TASK-001186).
REQUIRE_CORE_CONTRACT = os.environ.get("PACKAGE_SDK_REQUIRE_CORE_CONTRACT") == "1"


def core_contract_unavailable(reason: str) -> NoReturn:
    """Сверка с моделями или кодом ядра не идёт: явный пропуск с причиной (виден в -rs), а под
    PACKAGE_SDK_REQUIRE_CORE_CONTRACT=1 — провал. Молча сверку не выключает (TASK-001186)."""
    message = f"контракт ядра не сверен: {reason}"
    if REQUIRE_CORE_CONTRACT:
        pytest.fail(message)
    pytest.skip(message)
