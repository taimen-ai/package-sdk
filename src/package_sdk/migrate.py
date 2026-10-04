"""Перевод прежних выражений пакета в CEL функциями ядра с сохранением файла."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Protocol

try:
    import yaml
except ImportError:  # pragma: no cover - окружение без PyYAML
    yaml = None

from package_sdk.check import _domain
from package_sdk.model import PLAN_KINDS, Package, PackageError, _rel


class ExpressionTranslator(Protocol):
    def legacy_expressions(self, kind: str, spec: Any) -> Iterable[Any]: ...

    def translate(self, expression: Any) -> Any: ...


def _translator() -> ExpressionTranslator:
    _domain()  # кладёт control-plane/src в sys.path
    try:
        from control_plane.domain import cel_profile
    except ImportError as error:
        raise PackageError(
            "expression translation to CEL comes from the core (control_plane.domain.cel_profile), "
            "but its module does not import — control-plane with the CEL profile taimen/1 is "
            "required (CP-ADR-0075)"
        ) from error
    missing = [
        name
        for name in ("legacy_expressions", "translate")
        if not callable(getattr(cel_profile, name, None))
    ]
    if missing:
        raise PackageError(
            f"control_plane.domain.cel_profile has no {', '.join(missing)} — control-plane with "
            "translation of legacy syntaxes is required (CP-ADR-0075 R7)"
        )
    return cel_profile  # type: ignore[return-value]


def _pointer_parts(pointer: str) -> list[str]:
    if not pointer.startswith("/"):
        raise PackageError(f"expression pointer {pointer!r} is not a JSON pointer")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def _replace_at(document: Any, pointer: str, value: Any) -> None:
    """Заменить значение по JSON pointer в разобранном файле (ruamel: списки — по номеру)."""
    parts = _pointer_parts(pointer)
    holder = document
    for part in parts[:-1]:
        holder = holder[int(part)] if isinstance(holder, list) else holder[part]
    last = parts[-1]
    if isinstance(holder, list):
        holder[int(last)] = value
    else:
        holder[last] = value


def migrate_expressions(
    package: Package,
    *,
    write: bool = False,
    log: Callable[[str], None] = print,
    core: ExpressionTranslator | None = None,
) -> int:
    """Перевести прежние выражения пакета в CEL переводом ядра: печать diff по файлам;
    write — записать с сохранением файла (package_sdk.edit). Возвращает число переведённых
    выражений; то, что не переводится, печатается и остаётся как было."""
    import difflib

    from package_sdk import edit as pkg

    core = core or _translator()
    total = 0
    for obj in package.objects:
        if obj.kind in PLAN_KINDS:
            continue  # процессы и календари уже на CEL
        doc = pkg.Document.load(obj.path)
        spec = pkg.to_plain(doc.data.get("spec"))
        found = list(core.legacy_expressions(obj.kind, spec if isinstance(spec, dict) else {}))
        changed = 0
        for expression in found:
            where = f"{_rel(obj.path)}: {expression.pointer} ({expression.syntax})"
            try:
                translation = core.translate(expression)
            except Exception as error:
                log(f"   ! {where} cannot be translated: {error}")
                continue
            _replace_at(doc.data, expression.pointer, translation.expression)
            changed += 1
            bindings = tuple(getattr(translation, "bindings", ()) or ())
            if bindings:
                log(f"   {where}: reads variables beyond the profile: {', '.join(bindings)}")
        if not changed:
            continue
        total += changed
        after = doc.dumps()
        log(
            "".join(
                difflib.unified_diff(
                    doc.text.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    f"a/{_rel(obj.path)}",
                    f"b/{_rel(obj.path)}",
                )
            ).rstrip("\n")
        )
        if write:
            doc.save()
    log(
        f"{'written' if write else 'to translate'}: expressions {total}"
        + ("" if write or not total else " (--write to write)")
    )
    return total
