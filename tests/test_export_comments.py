"""Свойство выгрузки (S010): вставка или удаление одного элемента списка меняет в файле
только строки этого элемента, а строки комментария остаются над своим элементом.

Перебираются все блочные списки календаря ru и процесса invoice-payment, на каждом —
удаление первого, среднего, последнего элемента и всего списка, вставка в начало,
середину и конец. Ожидаемый текст считается по исходному файлу:

- удаление — исходный текст без блока элемента: строк комментария и пустых строк прямо
  над ним и его собственных строк; строки после последнего элемента списка стоят перед
  следующим элементом документа и остаются;
- удаление всего списка — то же для всех элементов, строка ключа становится ``key: []``;
- вставка — исходный текст, в который вставлены строки без комментариев: перед блоком
  элемента, который стоял на этом месте, или после последнего элемента.
"""

from __future__ import annotations

import copy
import datetime
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from package_sdk import edit, export
from package_sdk.model import PackageError, load_package, substitute

PACKAGES = Path(__file__).parent / "fixtures" / "umbrella" / "packages"
ENV = {
    "INVOICE_WORKSPACE_ID": "58227afa-0000-4000-8000-000000000001",
    "ACCOUNTING_ROLE_ID": "8110d540-0000-4000-8000-000000000002",
    "FINANCE_DIRECTOR_ROLE_ID": "8654aebd-0000-4000-8000-000000000003",
}
FILES = {
    ("Calendar", "ru"): ("platform-calendars", "calendars/ru.yaml"),
    ("Process", "invoice-payment"): ("invoice-payment", "processes/invoice-payment.yaml"),
}

Path_ = tuple[Any, ...]


def _walk(node: Any, path: Path_ = ()) -> Iterator[tuple[Path_, int]]:
    """Узлы документа по порядку текста: (путь, строка начала)."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield (*path, key), node.lc.key(key)[0]
            yield from _walk(value, (*path, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield (*path, index), node.lc.item(index)[0]
            yield from _walk(value, (*path, index))


def _get(node: Any, path: Path_) -> Any:
    for part in path:
        node = node[part]
    return node


def _filler(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


class Layout:
    """Строки исходного файла и границы блоков элементов."""

    def __init__(self, text: str) -> None:
        self.lines = text.splitlines(keepends=True)
        self.data = edit.Document.parse(text).data
        self.nodes = list(_walk(self.data))

    def block(self, path: Path_) -> tuple[int, int]:
        """[начало, конец) блока элемента: строки комментария над ним, сам элемент."""
        position = next(i for i, (p, _) in enumerate(self.nodes) if p == path)
        start = self.nodes[position][1]
        while start > 0 and _filler(self.lines[start - 1]):
            start -= 1
        after = next(
            (line for p, line in self.nodes[position + 1 :] if p[: len(path)] != path),
            len(self.lines),
        )
        end = after
        while end > start and _filler(self.lines[end - 1]):
            end -= 1
        return start, end


def _lists(node: Any, path: Path_ = ()) -> Iterator[Path_]:
    """Пути блочных списков — значений ключей словарей."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, list) and value and not value.fa.flow_style():
                yield (*path, key)
            yield from _lists(value, (*path, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _lists(value, (*path, index))


def _renamed(value: Any) -> bool:
    """Суффикс -new у id и key элемента и у всех вложенных id (id уникальны в процессе)."""
    found = False
    if isinstance(value, dict):
        for name in ("id", "key"):
            if isinstance(value.get(name), str):
                value[name] += "-new"
                found = True
        for item in value.values():
            found = _renamed(item) or found
    elif isinstance(value, list):
        for item in value:
            found = _renamed(item) or found
    return found


def _fresh(items: list[Any], like: Any) -> Any:
    """Новый элемент по образцу соседа, не совпадающий ни с одним элементом списка."""
    value = copy.deepcopy(like)
    if isinstance(value, dict):
        if _renamed(value):
            return value
        if isinstance(value.get("year"), int):
            value["year"] = max(i["year"] for i in items) + 10
            value["provisional"] = True
            value["holidays"] = [f"{value['year']}-01-01"]
            for name in ("workdays", "shortDays", "source"):
                value.pop(name, None)
            return value
        for name, item in value.items():
            if isinstance(item, int) and not isinstance(item, bool):
                value[name] = item + 1000
                return value
        for name, item in value.items():
            if isinstance(item, str):
                value[name] = "P9D" if item.startswith("P") or name == "after" else item + "_new"
                return value
    if isinstance(value, str):
        try:
            day = datetime.date.fromisoformat(value)
        except ValueError:
            return value + "-new"
        while day.isoformat() in items:
            day += datetime.timedelta(days=1)
        return day.isoformat()
    pytest.skip(f"нет правила нового элемента для {value!r}")


def _cases() -> Iterator[Any]:
    for (kind, key), (package, rel) in FILES.items():
        spec = edit.Document.load(PACKAGES / package / rel).data["spec"]
        for path in _lists(spec):
            size = len(_get(spec, path))
            points = sorted({0, size // 2, size - 1})
            name = "/".join(map(str, path))
            for index in points if size > 1 else []:
                yield pytest.param(kind, key, path, "delete", index, id=f"{key}:{name}:del{index}")
            yield pytest.param(kind, key, path, "clear", None, id=f"{key}:{name}:clear")
            for index in sorted({0, size // 2, size}):
                yield pytest.param(kind, key, path, "insert", index, id=f"{key}:{name}:ins{index}")


@pytest.mark.parametrize("tail", [False, True], ids=["", "tail"])
@pytest.mark.parametrize(("kind", "key", "path", "action", "index"), list(_cases()))
def test_one_element_change_touches_only_its_lines(
    tmp_path: Path, kind: str, key: str, path: Path_, action: str, index: int | None, tail: bool
) -> None:
    """tail — у файла есть строки комментария в конце: они остаются последними."""
    package, rel = FILES[(kind, key)]
    package_dir = tmp_path / package
    shutil.copytree(PACKAGES / package, package_dir)
    file = package_dir / rel
    if tail:
        file.write_text(file.read_text(encoding="utf-8") + "# конец файла\n", encoding="utf-8")
    before = file.read_text(encoding="utf-8")
    layout = Layout(before)
    obj = next(o for o in load_package(package_dir).objects if o.kind == kind and o.key == key)
    spec = substitute(obj.spec, ENV)
    items = _get(spec, path)
    full = ("spec", *path)
    lines = layout.lines
    if action == "delete" and len(items) == 1:
        action = "clear"  # единственный элемент: список становится пустым
    if action == "delete":
        assert index is not None
        del items[index]
        start, end = layout.block((*full, index))
        expected = lines[:start] + lines[end:]
    elif action == "clear":
        count = len(items)
        items.clear()
        key_line = layout.nodes[[p for p, _ in layout.nodes].index(full)][1]
        _, end = layout.block((*full, count - 1))
        expected = [*lines[:key_line], lines[key_line].rstrip("\n") + " []\n", *lines[end:]]
    else:
        assert index is not None
        items.insert(index, _fresh(items, items[min(index, len(items) - 1)]))
        if index < len(items) - 1:
            anchor = layout.block((*full, index))[0]
        else:
            anchor = layout.block((*full, index - 1))[1]
        expected = None

    try:
        export.export_object(package_dir, kind, key, {"key": key, "version": 2, "spec": spec})
    except PackageError as error:
        if "не совпала" in str(error):
            raise
        pytest.skip(f"схема не допускает такой объект: {error}")
    after = file.read_text(encoding="utf-8").splitlines(keepends=True)

    if expected is not None:
        assert "".join(after) == "".join(expected)
    else:
        tail = len(lines) - anchor
        assert after[:anchor] == lines[:anchor]
        assert after[len(after) - tail :] == lines[anchor:]
        inserted = after[anchor : len(after) - tail]
        assert inserted, "вставка не дала строк"
        assert not [line for line in inserted if _filler(line)], inserted

    # повторная выгрузка того же — без изменений
    again = export.export_object(package_dir, kind, key, {"key": key, "version": 2, "spec": spec})
    assert not again.changed


# Конструкции, которых нет в фикстурах пакетов, но которые встречаются в файлах авторов.
IDENTITY = {
    "конец файла после скаляра": "spec:\n  steps:\n    - id: a\n      note: x\n# конец процесса\n",
    "конец файла после flow": "spec:\n  steps:\n    - id: a\n      complete: {x: 1}\n# конец\n",
    "конец файла после пустого flow": "spec:\n  networks:\n    control: {}\n# конец\n",
    "конец файла после блочного скаляра": "spec:\n  note: >-\n    текст\n\n# конец\n",
    "после блочного скаляра": (
        "spec:\n  rules:\n    - note: >-\n        длинный\n        текст\n"
        "      # после скаляра\n      key: 1\n    - note: |-\n        a\n"
        "    # перед вторым\n    - note: z\n"
    ),
    "заголовок блочного скаляра": (
        "spec:\n  a: 1\n  exclude: |  # переводы\n    *.en.md\n\n  # перед b\n  b:\n    c: 2\n"
    ),
}


@pytest.mark.parametrize("text", IDENTITY.values(), ids=list(IDENTITY))
def test_unchanged_text_survives_the_comment_move(text: str) -> None:
    doc = edit.Document.parse(text)
    export._restore_tail(doc.data, export._settle(doc.data))
    assert doc.dumps() == text


def test_end_of_file_comment_stays_at_the_end_after_removing_the_last_element() -> None:
    text = IDENTITY["конец файла после скаляра"].replace(
        "      note: x\n", "      note: x\n    - id: b\n      note: y\n"
    )
    doc = edit.Document.parse(text)
    tail = export._settle(doc.data)
    export._merge_tree(doc.data["spec"], {"steps": [{"id": "a", "note": "x"}]})
    export._restore_tail(doc.data, tail)
    assert doc.dumps() == IDENTITY["конец файла после скаляра"]


@pytest.mark.parametrize(
    ("text", "wanted", "expected"),
    [
        (  # последний контейнер опустел: `key: []` в строке ключа, хвост после
            "spec:\n  days:\n    - a\n# конец\n",
            {"days": []},
            "spec:\n  days: []\n# конец\n",
        ),
        (
            "spec:\n  a: 1\n  m:\n    x: 1\n# конец\n",
            {"a": 1, "m": {}},
            "spec:\n  a: 1\n  m: {}\n# конец\n",
        ),
        (  # скаляр стал контейнером: хвост после его содержимого, а не внутри
            "spec:\n  a: 1\n  f: 2\n# конец\n",
            {"a": 1, "f": {"x": 1}},
            "spec:\n  a: 1\n  f:\n    x: 1\n# конец\n",
        ),
        (  # строка между ключом и заменённым скаляром не пропадает
            "spec:\n  a:\n    # про значение\n    1\n  b: 2\n",
            {"a": 5, "b": 2},
            "spec:\n    # про значение\n  a: 5\n  b: 2\n",
        ),
    ],
)
def test_emptied_or_replaced_last_value_keeps_the_tail(
    text: str, wanted: dict[str, Any], expected: str
) -> None:
    doc = edit.Document.parse(text)
    tail = export._settle(doc.data)
    export._merge_tree(doc.data["spec"], wanted)
    export._restore_tail(doc.data, tail)
    out = doc.dumps()
    assert out == expected
    assert edit.to_plain(edit.Document.parse(out).data)["spec"] == wanted
