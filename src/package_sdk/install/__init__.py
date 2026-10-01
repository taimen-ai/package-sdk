"""Установка пакетов: источники и их фиксация, единый план и его применение (TAI-ADR-0062
п.6–7, plan Р4–Р5).

    from package_sdk import install

    install.lock(Path("deploy/packages.yaml"))                    # packages.lock
    document = install.plan(Path("deploy/packages.yaml"), target=target, env=env,
                            out=Path("plan.json"))
    install.apply(Path("plan.json"), target=target, env=env, confirm=ask_human)

Инициализация стенда (bootstrap) — единственный путь без подтверждения человека:
``install.apply(document, target=…, env=…, assume_yes=True)``.
"""

from package_sdk.install.apply import apply, install_file
from package_sdk.install.lock import (
    LOCK_FORMAT,
    LOCK_NAME,
    GitCache,
    Pruned,
    Sources,
    find_locks,
    load,
    lock,
    prune,
)
from package_sdk.install.plan import (
    PLAN_FORMAT,
    ConsoleEdit,
    Target,
    console_edits,
    count_changes,
    format_plan,
    overwrites_console,
    plan,
    read_plan,
)

__all__ = [
    "LOCK_FORMAT",
    "LOCK_NAME",
    "PLAN_FORMAT",
    "ConsoleEdit",
    "GitCache",
    "Pruned",
    "Sources",
    "Target",
    "apply",
    "console_edits",
    "count_changes",
    "find_locks",
    "format_plan",
    "install_file",
    "load",
    "lock",
    "overwrites_console",
    "plan",
    "prune",
    "read_plan",
]
