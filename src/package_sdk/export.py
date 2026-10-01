"""Выгрузка процессов и календарей стенда в пакет (FR-008, TAI-ADR-0062).

Процесс и календарь применяет ядро по плану, и правят их на стенде в консоли. Выгрузка
возвращает правку в git так, чтобы файл пакета изменился ровно там, где стенд
отличается от него:

- существующий файл правится деревом ``edit`` (ruamel round-trip): комментарии,
  порядок ключей и стиль нетронутых строк остаются;
- строка файла с ``${ПЕРЕМЕННОЙ}`` остаётся, если значение стенда получается из неё
  подстановкой — значения установки в пакет не попадают;
- ``data: {$ref: <файл пакета>}`` остаётся, если раскрытая схема совпадает со схемой
  стенда;
- поля, которые на стенде правил человек (владелец ``console`` в плане ядра),
  перечисляются в выводе: после выгрузки они — поля пакета.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from package_sdk import edit
from package_sdk.model import (
    API,
    API_VERSION,
    ENV_REF,
    FOLDERS,
    Package,
    PackageError,
    _read_yaml,
    _rel,
    expand_data_ref,
    load_package,
)

# Маршрут чтения по виду: ключ (новейшая версия) или ключ@версия.
ROUTES = {"Process": "/process-definitions", "Calendar": "/calendars"}


@dataclass
class Exported:
    """Итог выгрузки одного объекта."""

    kind: str
    key: str
    version: Any
    path: Path
    created: bool
    changed: bool
    console_fields: list[str] = field(default_factory=list)
    #: переменные установки, в которые вернулись значения стенда
    substituted: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def line(self) -> str:
        what = "записан" if self.created else ("обновлён" if self.changed else "без изменений")
        text = f"{what} {_rel(self.path)} (v{self.version})"
        if self.console_fields:
            text += "; правлено в консоли: " + ", ".join(self.console_fields)
        if self.substituted:
            text += "; значения стенда → " + ", ".join(f"${{{n}}}" for n in self.substituted)
        return "\n".join([text, *(f"предупреждение: {w}" for w in self.warnings)])


def fetch(
    http: Any, headers: dict[str, str], kind: str, key: str, version: str | None
) -> dict[str, Any]:
    """Версия объекта на стенде: GET /process-definitions/{ref} или /calendars/{ref}."""
    ref = f"{key}@{version}" if version else key
    try:
        body: dict[str, Any] = http.call("GET", f"{API}{ROUTES[kind]}/{ref}", None, headers)
        return body
    except Exception as error:  # 404 — не найден, остальное пусть видно как есть
        status = getattr(error, "status", None)
        if status != 404 and (status is not None or "HTTP 404" not in str(error)):
            raise
        raise PackageError(f"{kind}/{ref} не найден") from error


def _template(text: str) -> re.Pattern[str]:
    """Строка пакета с ${ПЕРЕМЕННЫМИ} → шаблон значения после подстановки."""
    parts = ENV_REF.split(text)
    # split с группой: чётные — литералы, нечётные — имена переменных
    return re.compile(
        "".join(re.escape(part) if i % 2 == 0 else "(?s:.*)" for i, part in enumerate(parts))
    )


# Короче этого значение переменной обратно в ${ИМЯ} не превращается: «1» или «ru»
# встретятся где угодно.
_MIN_REVERSE = 8


def unsubstitute(value: Any, variables: dict[str, str], found: set[str] | None = None) -> Any:
    """Значения переменных установки в значении стенда → ${ИМЯ}: и целиком, и внутри
    строки (выражение CEL с id роли). Длинные значения заменяются первыми; ``found``
    собирает имена сделанных подстановок — выгрузка их печатает."""
    if isinstance(value, str):
        for name, known in sorted(variables.items(), key=lambda item: -len(item[1])):
            if known in value:
                value = value.replace(known, f"${{{name}}}")
                if found is not None:
                    found.add(name)
        return value
    if isinstance(value, dict):
        return {name: unsubstitute(item, variables, found) for name, item in value.items()}
    if isinstance(value, list):
        return [unsubstitute(item, variables, found) for item in value]
    return value


def _item_key(item: Any) -> str:
    """Чем элемент списка узнаётся при сравнении: стадия и шаг — по id, год календаря —
    по year, правило или ключ — по key; остальное — по значению."""
    if isinstance(item, dict):
        for name in ("id", "year", "key"):
            if name in item and isinstance(item[name], (str, int)):
                return f"{name}:{item[name]}"
    return json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)


def _align(old: list[Any], new: list[Any]) -> Sequence[tuple[str, int, int, int, int]]:
    old_keys = [_item_key(item) for item in old]
    new_keys = [_item_key(item) for item in new]
    return difflib.SequenceMatcher(None, old_keys, new_keys, autojunk=False).get_opcodes()


def reconcile(
    local: Any,
    server: Any,
    variables: dict[str, str] | None = None,
    found: set[str] | None = None,
) -> Any:
    """Значение стенда, в котором сохранено то, что в файле выражено иначе, но значит то
    же: ${ПЕРЕМЕННЫЕ} установки. Новое для файла значение, содержащее значение известной
    переменной, записывается ссылкой на неё. Элементы списков сопоставляются по
    :func:`_item_key`: вставка стадии не отменяет ${…} в соседних."""
    variables = variables or {}
    if isinstance(local, str) and isinstance(server, str) and ENV_REF.search(local):
        if _template(local).fullmatch(server):
            return local
        return unsubstitute(server, variables, found)
    if isinstance(local, dict) and isinstance(server, dict):
        return {
            name: reconcile(local[name], value, variables, found)
            if name in local
            else unsubstitute(value, variables, found)
            for name, value in server.items()
        }
    if isinstance(local, list) and isinstance(server, list):
        result: list[Any] = []
        for tag, i1, i2, j1, j2 in _align(local, server):
            if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
                result += [
                    reconcile(local[i1 + k], server[j1 + k], variables, found)
                    for k in range(j2 - j1)
                ]
            elif tag in ("replace", "insert"):
                result += [unsubstitute(item, variables, found) for item in server[j1:j2]]
        return result
    if local == server:
        return local
    return unsubstitute(server, variables, found)


def package_variables(package: Package, env: dict[str, str]) -> dict[str, str]:
    """Переменные, которые пакет использует или объявляет, с их значениями установки."""
    names: set[str] = set(package.spec.get("variables") or {})

    def walk(value: Any) -> None:
        if isinstance(value, str):
            names.update(ENV_REF.findall(value))
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for obj in package.objects:
        walk(obj.spec)
    return {
        name: env[name] for name in sorted(names) if name in env and len(env[name]) >= _MIN_REVERSE
    }


# --- комментарии перед элементами -----------------------------------------------
#
# ruamel прикрепляет строки комментария, стоящие перед элементом (заголовок года
# календаря, пояснение перед стадией), к тому, что стоит перед ними: к слоту последнего
# скаляра предыдущего элемента, к ``ca.end`` блочного списка, который кончается
# flow-значением, к ключу родителя — для первого элемента. При вставке и удалении
# элементов такие строки теряются или переезжают к соседу. Поэтому перед слиянием
# каждая строка переносится в слот «перед элементом» (``ca.items[i][1]``) того
# элемента, перед которым стоит в тексте, — самого внешнего из начинающихся с неё.
# Дальше строки ходят вместе с элементом: удалён — удалены, вставлен сосед — остались
# над своим. В конце строки остаётся только комментарий этой строки.


def _comment_lines(token: Any, column: int | None = None) -> list[str]:
    """Строки токена с отступом; column — колонка первой строки (у pre-токена)."""
    value = str(token.value)
    if column is None:
        column = token.column if value.strip() and not value.startswith("\n") else 0
    lines = value.splitlines(keepends=True)
    if lines and lines[0].strip():
        lines[0] = " " * column + lines[0].lstrip(" ")
    return lines


def _tokens(lines: list[str]) -> list[Any]:
    from ruamel.yaml.error import CommentMark
    from ruamel.yaml.tokens import CommentToken

    tokens = []
    for line in lines:
        text = line.strip(" \n")
        indent = len(line) - len(line.lstrip(" "))
        tokens.append(CommentToken(text + "\n" if text else "\n", CommentMark(indent), None))
    return tokens


def _split_eol(entry: list[Any] | None, index: int, *, block_scalar: bool = False) -> list[str]:
    """Из слота «после значения» оставить только комментарий этой строки; вернуть
    строки после неё. После блочного скаляра (``>-``, ``|``) строки своей нет: перевод
    строки его последней строки — часть скаляра, и всё в слоте — строки после него."""
    if not entry or len(entry) <= index or entry[index] is None:
        return []
    token = entry[index]
    value = str(token.value)
    if block_scalar:
        entry[index] = None
        return _comment_lines(token)
    cut = value.find("\n")
    head, rest = (value, "") if cut == -1 else (value[: cut + 1], value[cut + 1 :])
    if head.strip():
        token.value = head
    else:
        entry[index] = None
    return rest.splitlines(keepends=True)


def _pre_lines(tokens: list[Any] | None) -> list[str]:
    lines: list[str] = []
    for token in tokens or []:
        lines += _comment_lines(token, token.column if str(token.value).strip() else 0)
    return lines


def _block_scalar(value: Any) -> bool:
    from ruamel.yaml.scalarstring import FoldedScalarString, LiteralScalarString

    return isinstance(value, (FoldedScalarString, LiteralScalarString))


def _block(node: Any) -> bool:
    return (
        isinstance(node, (dict, list))
        and hasattr(node, "ca")
        and not (hasattr(node, "fa") and node.fa.flow_style())
    )


def _hoist(node: Any, pending: list[str], native: list[str] | None = None) -> list[str]:
    """Перенести строки комментариев блочного контейнера node в слоты «перед
    элементом». pending — строки, стоящие перед node; native — строки перед первым
    элементом, которые ruamel держит у ключа родителя. Возвращает строки после node."""
    first = True
    keys = list(node) if isinstance(node, dict) else list(range(len(node)))
    for key in keys:
        entry = node.ca.items.get(key)
        before = pending + _pre_lines(entry[1] if entry and len(entry) > 1 else None)
        pending = []
        if first:
            before += (native or []) + _pre_lines(node.ca.comment[1] if node.ca.comment else None)
            if node.ca.comment:
                node.ca.comment[1] = []
            first = False
        value = node[key]
        inner_native: list[str] = []
        # у блочного скаляра в entry[3] — комментарий его заголовка (``|  # …``), не
        # строки перед элементом: он остаётся на месте
        if isinstance(node, dict) and entry and len(entry) > 3 and entry[3] and _block(value):
            # перед первым элементом значения; те же токены ruamel держит и в
            # ca.comment[1] самого значения — берутся один раз
            shared = value.ca.comment[1] if _block(value) and value.ca.comment else None
            if shared is not entry[3] and not (shared and shared[0] is entry[3][0]):
                inner_native = _pre_lines(entry[3])
            entry[3] = None
        if before:
            entry = node.ca.items.setdefault(key, [None, None, None, None])
            while len(entry) < 2:
                entry.append(None)
            entry[1] = _tokens(before)
        elif entry and len(entry) > 1:
            entry[1] = None
        slot = 2 if isinstance(node, dict) else 0
        if _block(value) and value:
            # комментарий строки ключа и строки после него — перед первым элементом
            after_key = _split_eol(node.ca.items.get(key), slot)
            pending = _hoist(value, [], after_key + inner_native)
        else:
            pending = inner_native + _split_eol(
                node.ca.items.get(key), slot, block_scalar=_block_scalar(value)
            )
    if node.ca.end:
        taken = {id(token) for token in node.ca.end}
        for token in node.ca.end:
            pending += _comment_lines(token)
        node.ca.end = []
        # те же токены ruamel держит и в ca.comment[2] корня — иначе строка выйдет дважды
        comment = node.ca.comment
        if comment and len(comment) > 2 and comment[2]:
            comment[2] = [token for token in comment[2] if id(token) not in taken] or None
    return pending


def _settle(root: Any) -> list[str]:
    """Все строки комментариев документа — в слоты «перед элементом». Строки в конце
    файла возвращаются: после слияния их ставит :func:`_restore_tail` (ruamel не
    выводит ``ca.end`` корневого словаря)."""
    return _hoist(root, [], None)


def _restore_tail(root: Any, lines: list[str]) -> None:
    """Строки конца файла — после последнего значения документа, в его слот: туда их
    кладёт и сам ruamel при чтении."""
    if not lines:
        return
    from ruamel.yaml.error import CommentMark
    from ruamel.yaml.tokens import CommentToken

    node = root
    while True:
        keys = list(node) if isinstance(node, dict) else list(range(len(node)))
        if not keys:
            node.ca.end = _tokens(lines)
            return
        key = keys[-1]
        value = node[key]
        if isinstance(value, (dict, list)) and not hasattr(value, "ca"):
            value = node[key] = _like(value, None)  # значение слияния без ruamel-обёртки
        if _block(value) and value:
            node = value
            continue
        if isinstance(value, (dict, list)) and not value:
            # пустой контейнер пишется `key: []` в строку ключа; блочный стиль вывел бы
            # слот ключа между ключом и `[]` — невалидный YAML
            getattr(value, "fa").set_flow_style()  # noqa: B009 — у ruamel-контейнера
        slot = 2 if isinstance(node, dict) else 0
        entry = node.ca.items.setdefault(key, [None] * (slot + 2))
        while len(entry) <= slot:
            entry.append(None)
        text = "".join(lines)
        token = entry[slot]
        if token is not None:
            value_text = str(token.value)
            token.value = (value_text if value_text.endswith("\n") else value_text + "\n") + text
        else:
            # после блочного скаляра своей строки нет: слот начинается прямо со строк
            lead = "" if _block_scalar(value) else "\n"
            entry[slot] = CommentToken(lead + text, CommentMark(0), None)
        return


def _flow_safe(text: str) -> bool:
    """Строка читается без кавычек внутри flow-коллекции тем же загрузчиком, что и
    пакет (PyYAML строже ruamel: ``a.?b`` после запятой для него — ошибка)."""
    import yaml

    from package_sdk.model import _yaml12_loader

    try:
        return bool(yaml.load(f"{{k: [{text}]}}", Loader=_yaml12_loader()) == {"k": [text]})
    except yaml.YAMLError:
        return False


def _peer(sibling: Any, index: int, item: Any) -> Any:
    """Образец стиля для элемента списка: элемент соседа с тем же ключом, иначе с тем же
    номером, иначе последний."""
    if not isinstance(sibling, list) or not sibling:
        return None
    key = _item_key(item)
    return next(
        (peer for peer in sibling if _item_key(peer) == key),
        sibling[index] if index < len(sibling) else sibling[-1],
    )


def _like(value: Any, sibling: Any, flow: bool = False) -> Any:
    """Новое значение в стиле соседа: ruamel-контейнеры (к ним цепляются комментарии),
    flow-стиль контейнера и кавычки строки — как у соседнего элемента. Строка внутри
    flow-коллекции, которую без кавычек не прочитать, пишется в кавычках."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString, ScalarString

    if isinstance(value, (dict, list)):
        flow = flow or (
            isinstance(sibling, (dict, list))
            and hasattr(sibling, "fa")
            and bool(sibling.fa.flow_style())
        )
    if isinstance(value, dict):
        peers = sibling if isinstance(sibling, dict) else {}
        result: Any = CommentedMap((k, _like(v, peers.get(k), flow)) for k, v in value.items())
    elif isinstance(value, list):
        result = CommentedSeq(_like(v, _peer(sibling, i, v), flow) for i, v in enumerate(value))
    elif isinstance(value, str) and not isinstance(value, ScalarString):
        if isinstance(sibling, DoubleQuotedScalarString) or (flow and not _flow_safe(value)):
            return DoubleQuotedScalarString(value)
        return value
    else:
        return value
    if flow:
        result.fa.set_flow_style()
    return result


def _lift_value_comment(node: Any, name: Any) -> None:
    """Строки между ключом и значением на отдельной строке (``key:\n  # …\n  1``)
    ruamel выводит только при прежнем значении: при замене они переходят над ключом."""
    from ruamel.yaml.tokens import CommentToken

    entry = node.ca.items.get(name) if hasattr(node, "ca") else None
    if not entry or len(entry) < 4 or not entry[3]:
        return
    if not all(isinstance(token, CommentToken) for token in entry[3]):
        return  # заголовок блочного скаляра (`|  # …`) — строки, остаётся при значении
    lines = _pre_lines(entry[3])
    entry[3] = None
    entry[1] = [*(entry[1] or []), *_tokens(lines)]


def _merge_tree(node: Any, value: Any) -> Any:
    """Вписать значение в дерево ruamel на месте: совпадающее не трогается, словари
    сливаются по ключам, списки — по элементам (:func:`_align`) со вставкой и удалением на
    месте. Строки комментария перед элементом (после :func:`_settle`) ходят вместе с
    ним."""
    if isinstance(node, dict) and isinstance(value, dict):
        for name in [n for n in node if n not in value]:
            del node[name]
            if hasattr(node, "ca"):
                node.ca.items.pop(name, None)
        for name, item in value.items():
            if name in node:
                merged = _merge_tree(node[name], item)
                if merged is not node[name]:
                    _lift_value_comment(node, name)
                node[name] = merged
                continue
            keys = list(node)
            node[name] = _like(item, node[keys[-1]] if keys else None)
        return node
    if isinstance(node, list) and isinstance(value, list):
        # с конца, чтобы индексы ещё не тронутой части оставались верными
        for tag, i1, i2, j1, j2 in reversed(_align(edit.to_plain(node), value)):
            if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
                for k in range(i2 - i1):
                    node[i1 + k] = _merge_tree(node[i1 + k], value[j1 + k])
                continue
            sibling = node[i1 - 1] if i1 > 0 else (node[i2] if i2 < len(node) else None)
            for index in range(i2 - 1, i1 - 1, -1):
                del node[index]
            for offset, item in enumerate(value[j1:j2]):
                node.insert(i1 + offset, _like(item, sibling))
        return node
    if edit.to_plain(node) == value:
        return node
    # новый контейнер — ruamel-обёрткой: к нему цепляются строки комментариев
    return _like(value, None) if isinstance(value, (dict, list)) else value


def _object_file(package: Package, kind: str, key: str) -> Path | None:
    return next(
        (o.path for o in package.objects if o.kind == kind and o.key == key),
        None,
    )


def _keep_data_ref(
    path: Path, local_spec: Any, spec: dict[str, Any], package_dir: Path
) -> str | None:
    """data: {$ref} файла остаётся, если схема по ссылке та же, что на стенде; иначе —
    предупреждение: ссылка заменится встроенной схемой стенда."""
    data = local_spec.get("data") if isinstance(local_spec, dict) else None
    if not (isinstance(data, dict) and set(data) == {"$ref"}):
        return None
    try:
        expanded = expand_data_ref(dict(local_spec), path, package_dir).get("data")
    except PackageError:
        return None
    if expanded == spec.get("data"):
        spec["data"] = data
        return None
    return (
        f"data: схема данных на стенде отличается от {data['$ref']} — ссылка заменена "
        "встроенной схемой; перенесите правку в файл схемы и верните $ref"
    )


def console_fields(plan: dict[str, Any], kind: str, key: str) -> list[str]:
    """Поля объекта, которые в плане ядра принадлежат консоли (правил человек)."""
    for change in plan.get("changes") or []:
        if change.get("kind") == kind and change.get("key") == key:
            return [
                str(f.get("path"))
                for f in change.get("fields") or []
                if f.get("owner") == "console"
            ]
    return []


def export_object(
    package_dir: Path,
    kind: str,
    key: str,
    body: dict[str, Any],
    *,
    plan: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    schema_rel: str | None = None,
) -> Exported:
    """Записать объект стенда в пакет: правкой существующего файла или новым файлом.
    env — значения установки: по ним значения стенда возвращаются в ${ПЕРЕМЕННЫЕ}."""
    if kind not in ROUTES:
        raise PackageError(f"{kind}: выгрузка этим путём — только Process и Calendar")
    spec = dict(body.get("spec") or {})
    marked = console_fields(plan or {}, kind, key)
    package = load_package(package_dir)
    variables = package_variables(package, env or {})
    found: set[str] = set()
    path = _object_file(package, kind, key)
    if path is None:
        from package_sdk.apply import dump_document

        path = package_dir / FOLDERS[kind] / f"{key}.yaml"
        document = {
            "apiVersion": API_VERSION,
            "kind": kind,
            "key": key,
            "spec": unsubstitute(spec, variables, found),
        }
        text = dump_document(document, schema_rel or _schema_rel(path))
        try:
            edit.validate(edit.Document.parse(text, path))
        except edit.PkgError as error:
            raise PackageError(
                f"{_rel(path)}: {kind}/{key} не записан — {error.message}. Значения установки "
                "в пакет не пишутся: задайте их переменные (--env), чтобы выгрузка вернула ${…}"
            ) from error
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return Exported(kind, key, body.get("version"), path, True, True, marked, sorted(found))
    raw = _read_yaml(path)
    local_spec = raw.get("spec") if isinstance(raw, dict) else None
    wanted = reconcile(local_spec, spec, variables, found)
    warning = _keep_data_ref(path, local_spec, wanted, package_dir)
    try:
        doc = edit.Document.load(path)
        tail = _settle(doc.data)
        _merge_tree(doc.data["spec"], wanted)
        _restore_tail(doc.data, tail)
        text = doc.dumps()
        # Записывается только текст, который разбирается обратно в ту же выгрузку и
        # проходит схему: проверяется записываемое, а не дерево в памяти.
        reparsed = edit.Document.parse(text, path)
        edit.validate(reparsed)
    except edit.PkgError as error:
        raise PackageError(f"{_rel(path)}: {kind}/{key} не записан — {error.message}") from error
    if edit.to_plain(reparsed.data).get("spec") != wanted or _package_read(text) != wanted:
        raise PackageError(
            f"{_rel(path)}: {kind}/{key} не записан — запись файла не совпала с выгрузкой "
            "(стиль файла не воспроизводится); выгрузите в новый файл и перенесите правку"
        )
    changed = text != doc.text
    if changed:
        path.write_text(text, encoding="utf-8")
    return Exported(
        kind,
        key,
        body.get("version"),
        path,
        False,
        changed,
        marked,
        sorted(found),
        [warning] if warning else [],
    )


def _package_read(text: str) -> Any:
    """spec записываемого текста так, как его прочтёт загрузчик пакета."""
    import yaml

    from package_sdk.model import _yaml12_loader

    try:
        data = yaml.load(text, Loader=_yaml12_loader())
    except yaml.YAMLError:
        return None
    return data.get("spec") if isinstance(data, dict) else None


def _schema_rel(path: Path) -> str:
    import os

    from package_sdk import schema as schema_module

    return os.path.relpath(schema_module.schema_dir() / "object.schema.json", path.parent)
