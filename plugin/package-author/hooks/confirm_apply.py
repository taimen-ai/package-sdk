#!/usr/bin/env python3
"""PreToolUse hook of the package-author plugin: a plan is applied only with human consent.

Before a ``pkg_apply`` call (MCP server ``package-sdk mcp``) the hook

- denies the call (``deny``) if it carries no ``plan_hash`` of the form ``sha256:<64 hex>``:
  a plan is applied only by the hash the human has seen;
- denies the call if ``plan_file`` is not an absolute path, does not read as a plan or
  carries another ``planHash``: the human confirms only a plan the hook was able to show;
- otherwise asks the host for confirmation (``ask``): the human sees the plan file, the
  server, the hash, the number of changes per section and the ``overwriteConsole`` flag —
  whether applying overwrites edits people made in the console, and in which objects — and
  decides whether to apply.

The hook is the second line of defence. The first is the skills' rule: the agent does not
call ``pkg_apply`` until the human has answered "yes" to the plan shown. The third is the SDK
itself: it reads the plan once, applies only the document with the confirmed hash
(``plan_hash_mismatch`` otherwise) and refuses with ``plan_stale`` before the first write if
the server changed after the plan.

Input is the PreToolUse event JSON on stdin, output is the decision in ``hookSpecificOutput``.
For any other tool the hook stays silent. No dependencies beyond the standard library.
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
    "catalog": "catalog",
    "core": "core",
    "knowledge": "ontologies",
    "notification-rules": "notification rules",
    "retire": "retirement",
}


def _plan(name: str) -> dict[str, Any] | None:
    """The plan file as a package-sdk.plan/v1 document, or None."""
    try:
        document = json.loads(Path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("format") != PLAN_FORMAT:
        return None
    return document


def _count(section: dict[str, Any]) -> int:
    """Changes of a section — as package-sdk count_changes counts them."""
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
    return ", ".join(f"{title} {count}" for title, count in counts.items()) or "no sections"


def console(document: dict[str, Any]) -> str:
    """The plan's overwriteConsole flag and the objects whose console edits applying overwrites."""
    if document.get("overwriteConsole") is not True:
        return "console edits are kept (overwriteConsole: no)"
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
    return "CONSOLE EDITS WILL BE OVERWRITTEN (overwriteConsole: yes)" + (
        f": {'; '.join(objects)}" if objects else " — the plan's objects have none at the moment"
    )


def decide(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The hook's decision for a PreToolUse event, or ``None`` if the tool is not ours."""
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
            "A plan is applied only by the planHash of the plan shown to the human: first "
            'pkg_plan, show the whole plan, get an explicit "yes", then pkg_apply with '
            "plan_file and planHash of that plan.",
        )
    if not isinstance(plan_file, str) or not Path(plan_file).is_absolute():
        return _output(
            "deny",
            "plan_file must be the absolute path to the plan file from the pkg_plan response "
            "(planFile).",
        )
    document = _plan(plan_file)
    if document is None:
        return _output(
            "deny",
            f"{plan_file} does not read as a {PLAN_FORMAT} plan: it cannot be shown to the "
            "human — build the plan again (pkg_plan).",
        )
    if document.get("planHash") != plan_hash:
        return _output(
            "deny",
            f"{plan_file} holds plan {document.get('planHash')}, not {plan_hash}: "
            "only the plan the human has seen is applied. Show this plan and ask again "
            "or build the plan again.",
        )
    return _output(
        "ask",
        f"Apply plan {plan_file} to server {document.get('server')} ({plan_hash}); "
        f"changes: {changes(document)}; {console(document)}? Confirm only if this whole "
        "plan has been shown to you and you agree with it.",
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
        # Unreadable input is a denial: applying without the check is never let through.
        payload = {"tool_name": "pkg_apply", "tool_input": {}}
    output = decide(payload if isinstance(payload, dict) else {})
    if output is not None:
        print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
