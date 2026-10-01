#!/usr/bin/env python3
"""PreToolUse-хук плагина package-author: применение плана — только с согласия человека.

Перед вызовом ``pkg_apply`` (MCP-сервер ``package-sdk mcp``) хук

- отклоняет вызов (``deny``), если в нём нет ``plan_hash`` вида ``sha256:<64 hex>``:
  план применяется только по хэшу, который видел человек;
- отклоняет вызов, если ``plan_file`` — не абсолютный путь, не читается как план или
  несёт другой ``planHash``: человек подтверждает только план, который хук смог показать;
- иначе требует подтверждения хоста (``ask``): человек видит файл плана, стенд, хэш,
  число изменений по секциям и флаг ``overwriteConsole`` — перезапишет ли применение правки,
  которые люди сделали в консоли, и в каких объектах, — и сам решает, применять ли.

Хук — второй рубеж. Первый — правило скиллов: агент не зовёт ``pkg_apply``, пока человек
не ответил «да» на показанный план. Третий — сам SDK: он читает план один раз, применяет
только документ с подтверждённым хэшем (``plan_hash_mismatch`` иначе) и отказывает
``plan_stale`` до первой записи, если стенд изменился после плана.

Вход — JSON события PreToolUse на stdin, выход — решение в ``hookSpecificOutput``.
Для любого другого инструмента хук молчит. Зависимостей, кроме стандартной библиотеки, нет.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

TOOL = re.compile(r"(^|__)pkg_apply$")
PLAN_HASH = re.compile(r"sha256:[0-9a-f]{64}")
PLAN_FORMAT = "package-sdk.plan/v1"
TITLES = {
    "catalog": "каталог",
    "core": "ядро",
    "knowledge": "онтологии",
    "notification-rules": "уведомления",
    "retire": "вывод из оборота",
}


def _plan(name: str) -> dict[str, Any] | None:
    """Файл плана как документ package-sdk.plan/v1 или None."""
    try:
        document = json.loads(Path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("format") != PLAN_FORMAT:
        return None
    return document


def _count(section: dict[str, Any]) -> int:
    """Изменения секции — как count_changes package-sdk."""
    kind = section.get("kind")
    if kind in ("catalog", "notification-rules"):
        return len(section.get("changes") or [])
    if kind == "core":
        plan = section.get("plan") or {}
        return sum(
            1
            for change in plan.get("changes") or []
            if change.get("action") != "unchanged" or change.get("deprecates")
        )
    if kind == "knowledge":
        return len(section.get("register") or []) + len(section.get("enable") or [])
    if kind == "retire":
        return len(section.get("items") or [])
    return 0


def changes(document: dict[str, Any]) -> str:
    counts: dict[str, int] = {}
    for section in document.get("sections") or []:
        if isinstance(section, dict):
            title = TITLES.get(str(section.get("kind")), str(section.get("kind")))
            counts[title] = counts.get(title, 0) + _count(section)
    return ", ".join(f"{title} {count}" for title, count in counts.items()) or "секций нет"


def console(document: dict[str, Any]) -> str:
    """Флаг overwriteConsole плана и объекты, чьи правки консоли применение перезапишет."""
    if document.get("overwriteConsole") is not True:
        return "правки консоли сохраняются (overwriteConsole: нет)"
    objects: list[str] = []
    for section in document.get("sections") or []:
        if not isinstance(section, dict) or section.get("kind") != "core":
            continue
        for change in (section.get("plan") or {}).get("changes") or []:
            fields = [
                str(item.get("path"))
                for item in change.get("fields") or []
                if isinstance(item, dict)
                and item.get("owner") == "console"
                and item.get("applies") is not False
            ]
            if fields:
                objects.append(f"{change.get('kind')}/{change.get('key')} ({', '.join(fields)})")
    return "ПРАВКИ КОНСОЛИ БУДУТ ПЕРЕЗАПИСАНЫ (overwriteConsole: да)" + (
        f": {'; '.join(objects)}" if objects else " — в объектах плана их сейчас нет"
    )


def decide(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Решение хука для события PreToolUse или ``None``, если инструмент не наш."""
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or not TOOL.search(tool):
        return None
    arguments = payload.get("tool_input")
    arguments = arguments if isinstance(arguments, dict) else {}
    plan_hash = arguments.get("plan_hash")
    plan_file = arguments.get("plan_file")
    if not isinstance(plan_hash, str) or not PLAN_HASH.fullmatch(plan_hash):
        return _output(
            "deny",
            "План применяется только по planHash плана, показанного человеку: сначала "
            "pkg_plan, показать план целиком, получить явное «да», затем pkg_apply с "
            "plan_file и planHash этого плана.",
        )
    if not isinstance(plan_file, str) or not Path(plan_file).is_absolute():
        return _output(
            "deny",
            "plan_file — абсолютный путь к файлу плана из ответа pkg_plan (planFile).",
        )
    document = _plan(plan_file)
    if document is None:
        return _output(
            "deny",
            f"{plan_file} не читается как план {PLAN_FORMAT}: показать его человеку нельзя — "
            "постройте план заново (pkg_plan).",
        )
    if document.get("planHash") != plan_hash:
        return _output(
            "deny",
            f"В файле {plan_file} план {document.get('planHash')}, а не {plan_hash}: "
            "применяется только тот план, который видел человек. Покажите этот план и "
            "спросите снова или постройте план заново.",
        )
    return _output(
        "ask",
        f"Применить план {plan_file} на стенд {document.get('server')} ({plan_hash}); "
        f"изменений: {changes(document)}; {console(document)}? Подтверждайте, только если "
        "этот план показан вам целиком и вы согласны с ним.",
    )


def _output(decision: str, reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        # Непонятный вход — отказ: применение без проверки не пропускается.
        payload = {"tool_name": "pkg_apply", "tool_input": {}}
    output = decide(payload if isinstance(payload, dict) else {})
    if output is not None:
        print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
