"""Секреты узла у наблюдателя — по канону skill-sdk (TASK-001170).

Одна таблица примеров гоняется по чтению коннектора и по канону
``skill_sdk.secrets.read_secret_file`` (если skill-sdk стоит — extra ``skills``):
расхождение правила в любой из двух реализаций роняет общий пример.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from package_sdk.connector import CYCLE_FAILED, SECRET_MISSING, ObserveContext, observer
from package_sdk.connector.secrets import (
    MAX_SECRET_FILE_BYTES,
    SWAP_ATTEMPTS,
    SecretRejected,
    read_secret_file,
)
from package_sdk.connector.testing import run_once

# Исход чтения: значение, None (файла нет) или (код отказа, причина).
Outcome = str | None | tuple[str, str | None]
Arrange = Callable[[Path, Path, pytest.MonkeyPatch], None]


@dataclass(frozen=True)
class Example:
    id: str
    name: str
    arrange: Arrange
    expected: Outcome


def _file(content: str | bytes, name: str = "token") -> Arrange:
    def arrange(secrets: Path, _tmp: Path, _mp: pytest.MonkeyPatch) -> None:
        data = content.encode("utf-8") if isinstance(content, str) else content
        (secrets / name).write_bytes(data)

    return arrange


def _nothing(_secrets: Path, _tmp: Path, _mp: pytest.MonkeyPatch) -> None:
    pass


def _dangling(secrets: Path, _tmp: Path, _mp: pytest.MonkeyPatch) -> None:
    (secrets / "token").symlink_to(secrets / "gone")


def _inside_link(secrets: Path, _tmp: Path, _mp: pytest.MonkeyPatch) -> None:
    (secrets / "real").write_text("inside", encoding="utf-8")
    (secrets / "token").symlink_to(secrets / "real")


def _outside_link(secrets: Path, tmp: Path, _mp: pytest.MonkeyPatch) -> None:
    (tmp / "outside").write_text("stolen", encoding="utf-8")
    (secrets / "token").symlink_to(tmp / "outside")


def _file_link(target: str) -> Arrange:
    def arrange(secrets: Path, tmp: Path, _mp: pytest.MonkeyPatch) -> None:
        (tmp / "outside").write_text("stolen", encoding="utf-8")
        (secrets / "token").symlink_to(target)

    return arrange


def _directory(secrets: Path, _tmp: Path, _mp: pytest.MonkeyPatch) -> None:
    (secrets / "token").mkdir()


def _fifo(secrets: Path, _tmp: Path, _mp: pytest.MonkeyPatch) -> None:
    os.mkfifo(secrets / "token")


def _locked(secrets: Path, _tmp: Path, _mp: pytest.MonkeyPatch) -> None:
    (secrets / "token").write_text("v", encoding="utf-8")
    (secrets / "token").chmod(0)


def _kubernetes(secrets: Path, _tmp: Path, _mp: pytest.MonkeyPatch) -> None:
    """Раскладка Kubernetes: ``token -> ..data/token``, ``..data -> ..v1``."""
    (secrets / "..v1").mkdir()
    (secrets / "..v1" / "token").write_text("old", encoding="utf-8")
    (secrets / "..data").symlink_to("..v1")
    (secrets / "token").symlink_to("..data/token")


def _race(
    monkeypatch: pytest.MonkeyPatch,
    swap: Callable[[], None],
    restore: Callable[[], None] | None = None,
    times: int | None = None,
    on: str | None = None,
) -> None:
    """Подменить путь во время чтения: ``swap`` перед ``open`` (только для пути ``on``,
    если задан; первые ``times`` раз, если задано), ``restore`` — сразу после. Обе
    реализации зовут ``os.open`` модуля ``os``, поэтому подмена действует на любую."""
    real_open = os.open
    swaps = 0

    def racing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swaps
        if (on is not None and os.fspath(path) != on) or (times is not None and swaps >= times):
            return real_open(path, flags, *args, **kwargs)
        swaps += 1
        swap()
        try:
            return real_open(path, flags, *args, **kwargs)
        finally:
            if restore is not None:
                restore()

    monkeypatch.setattr(os, "open", racing_open)


def _rotate(secrets: Path) -> None:
    """Ротация Kubernetes: новая версия, атомарная замена ``..data``, старая удаляется."""
    (secrets / "..v2").mkdir()
    (secrets / "..v2" / "token").write_text("new", encoding="utf-8")
    (secrets / "..data_tmp").symlink_to("..v2")
    (secrets / "..data_tmp").rename(secrets / "..data")
    (secrets / "..v1" / "token").unlink()
    (secrets / "..v1").rmdir()


def _rotated_before_open(secrets: Path, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``..data`` прочитан как ``..v1``, а открыть ``..v1`` не успели — его удалили."""
    _kubernetes(secrets, tmp, monkeypatch)
    _race(monkeypatch, lambda: _rotate(secrets), times=1, on="..v1")


def _rotated_after_open(secrets: Path, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Старый каталог уже открыт, и ротация удаляет из него файл до чтения."""
    _kubernetes(secrets, tmp, monkeypatch)
    real_open = os.open
    rotated = False

    def open_then_rotate(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal rotated
        fd = real_open(path, flags, *args, **kwargs)
        if os.fspath(path) == "..v1" and not rotated:
            rotated = True
            _rotate(secrets)
        return fd

    monkeypatch.setattr(os, "open", open_then_rotate)


def _secrets_dir_swapped_and_put_back(
    secrets: Path, tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Каталог секретов подменён на время ``open`` и возвращён: путь до и после тот же,
    подмену видит только сверка inode дескриптора с путём."""
    (secrets / "token").write_text("inside", encoding="utf-8")
    outside = tmp / "outside"
    outside.mkdir()
    (outside / "token").write_text("stolen", encoding="utf-8")
    parked = tmp / "parked"

    def swap() -> None:
        secrets.rename(parked)
        secrets.symlink_to(outside)

    def restore() -> None:
        secrets.unlink()
        parked.rename(secrets)

    _race(monkeypatch, swap, restore, on=str(secrets))


def _file_swapped_for_link(secrets: Path, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token = secrets / "token"
    token.write_text("inside", encoding="utf-8")
    (tmp / "outside").write_text("stolen", encoding="utf-8")

    def swap() -> None:
        token.unlink()
        token.symlink_to(tmp / "outside")

    def restore() -> None:
        token.unlink()
        token.write_text("inside", encoding="utf-8")

    _race(monkeypatch, swap, restore, on="token")


def _intermediate_triple_race(secrets: Path, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Пробник ревью: промежуточный каталог подменён перед ``open``, возвращён, снова
    подменён перед ``stat`` и возвращён. Сверку «путь и inode после open» это обходило."""
    (secrets / "sub").mkdir()
    (secrets / "sub" / "token").write_text("inside", encoding="utf-8")
    (secrets / "token").symlink_to("sub/token")
    outside = tmp / "outside"
    outside.mkdir()
    (outside / "token").write_text("stolen", encoding="utf-8")
    parked = tmp / "parked"

    def swap() -> None:
        (secrets / "sub").rename(parked)
        (secrets / "sub").symlink_to(outside)

    def restore() -> None:
        (secrets / "sub").unlink()
        parked.rename(secrets / "sub")

    real_open, real_stat = os.open, os.stat
    armed = False

    def racing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal armed
        swap()
        try:
            return real_open(path, flags, *args, **kwargs)
        finally:
            restore()
            armed = True

    def racing_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal armed
        if not armed:
            return real_stat(path, *args, **kwargs)
        armed = False
        swap()
        try:
            return real_stat(path, *args, **kwargs)
        finally:
            restore()

    monkeypatch.setattr(os, "open", racing_open)
    monkeypatch.setattr(os, "stat", racing_stat)


REJECTED = "secret_file_rejected"
SWAPPED = (REJECTED, "symlink_swapped")
LIMIT = MAX_SECRET_FILE_BYTES
INVALID = ("secret_name_invalid", None)

EXAMPLES = [
    Example("trailing-lf", "token", _file("v\n"), "v"),
    Example("trailing-crlf", "token", _file("v\r\n"), "v"),
    Example("spaces-are-value", "token", _file(" v \r\n"), " v "),
    Example("inner-spaces", "token", _file("a b\n"), "a b"),
    Example("only-spaces", "token", _file("  \n"), ""),
    Example("single-space", "token", _file(" "), ""),
    Example("blank-with-tab-and-crlf", "token", _file("\t \r\n"), ""),
    Example("blank-lines", "token", _file("\n \n"), ""),
    Example("only-line-breaks", "token", _file("\r\n\n"), ""),
    Example("empty", "token", _file(""), ""),
    Example("absent", "token", _nothing, None),
    Example("dangling-link-inside", "token", _dangling, None),
    Example("link-inside", "token", _inside_link, "inside"),
    Example("link-outside", "token", _outside_link, (REJECTED, "outside_secrets_dir")),
    Example("directory", "token", _directory, (REJECTED, "not_regular_file")),
    Example("fifo", "token", _fifo, (REJECTED, "not_regular_file")),
    Example("at-limit", "token", _file(b"y" * LIMIT), "y" * LIMIT),
    Example(
        "too-large", "token", _file(b"x" * (MAX_SECRET_FILE_BYTES + 1)), (REJECTED, "too_large")
    ),
    Example("not-utf8", "token", _file(b"\xff\xfe"), (REJECTED, "not_utf8")),
    Example("agent-pat-reserved", "agent-pat", _file("pat", "agent-pat"), INVALID),
    Example("env-style-name", "EXT_TOKEN", _file("v", "EXT_TOKEN"), INVALID),
    Example("traversal", "../token", _nothing, INVALID),
    Example("empty-name", "", _nothing, INVALID),
    Example("kubernetes-layout", "token", _kubernetes, "old"),
    Example("kubernetes-rotation-before-open", "token", _rotated_before_open, "new"),
    Example("kubernetes-rotation-after-open", "token", _rotated_after_open, "new"),
    Example(
        "secrets-dir-swapped-and-put-back", "token", _secrets_dir_swapped_and_put_back, SWAPPED
    ),
    Example("file-swapped-for-link", "token", _file_swapped_for_link, SWAPPED),
    Example("intermediate-triple-race", "token", _intermediate_triple_race, SWAPPED),
    Example(
        "link-dot-dot-outside", "token", _file_link("../outside"), (REJECTED, "outside_secrets_dir")
    ),
    Example("link-to-the-dir-itself", "token", _file_link("."), (REJECTED, "not_regular_file")),
]
if os.geteuid() != 0:  # root читает файл без прав
    EXAMPLES.append(Example("no-permission", "token", _locked, ("secret_unreadable", None)))


def _connector(directory: Path, name: str) -> Outcome:
    try:
        return read_secret_file(directory, name)
    except SecretRejected as error:
        assert "stolen" not in str(error)
        return (error.code, error.reason)


def _skill_sdk(directory: Path, name: str) -> Outcome:
    canon = pytest.importorskip("skill_sdk.secrets", reason="канон — extra skills (skill-sdk)")
    errors = pytest.importorskip("skill_sdk.errors")
    try:
        value: str | None = canon.read_secret_file(directory, name)
        return value
    except errors.SkillError as error:
        return (error.code, (error.details or {}).get("reason"))


READERS: dict[str, Callable[[Path, str], Outcome]] = {
    "connector": _connector,
    "skill-sdk": _skill_sdk,
}


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("example", EXAMPLES, ids=[e.id for e in EXAMPLES])
def test_secret_file_rule_is_the_canon(
    reader: str, example: Example, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    example.arrange(secrets, tmp_path, monkeypatch)
    try:
        assert READERS[reader](secrets, example.name) == example.expected
    finally:
        monkeypatch.undo()
        if (secrets / "token").exists() and not (secrets / "token").is_symlink():
            (secrets / "token").chmod(0o600)


def test_swap_on_every_attempt_gives_up_after_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    _secrets_dir_swapped_and_put_back(secrets, tmp_path, monkeypatch)
    racing = os.open
    roots = 0

    def counting(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal roots
        roots += "dir_fd" not in kwargs
        return racing(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", counting)
    with pytest.raises(SecretRejected) as error:
        read_secret_file(secrets, "token")
    assert (error.value.reason, error.value.retryable) == ("symlink_swapped", False)
    assert roots == SWAP_ATTEMPTS


# --- цикл наблюдателя: «нет секрета» и «секрет отвергнут» различимы ----------------------


@observer(kind="secret-reader", entrypoint="tests.secret_reader:observe")
def reads_token(ctx: ObserveContext) -> None:
    ctx.secret("feed-token")


def test_a_rejected_secret_is_a_cycle_failure_with_its_reason(tmp_path: Path) -> None:
    oversize = run_once(reads_token, secrets={"feed-token": "x" * (MAX_SECRET_FILE_BYTES + 1)})
    (report,) = oversize.observations
    assert report["kind"] == CYCLE_FAILED
    assert report["data"]["error"] == "SecretRejected"
    assert (report["data"]["code"], report["data"]["reason"]) == (
        "secret_file_rejected",
        "too_large",
    )
    assert "x" * 16 not in str(report)


@pytest.mark.parametrize("content", ["\n", "  \n", " "], ids=["empty", "blank", "space"])
def test_an_empty_or_blank_secret_is_still_missing(content: str) -> None:
    result = run_once(reads_token, secrets={"feed-token": content})
    (report,) = result.observations
    assert report["kind"] == SECRET_MISSING


def test_a_reserved_name_is_rejected_not_missing(tmp_path: Path) -> None:
    @observer(kind="pat-reader", entrypoint="tests.pat_reader:observe")
    def reads_pat(ctx: ObserveContext) -> None:
        ctx.secret("agent-pat")

    result = run_once(reads_pat, secrets={"agent-pat": "pat-value"})
    (report,) = result.observations
    assert report["kind"] == CYCLE_FAILED
    assert report["data"]["code"] == "secret_name_invalid"
    assert "pat-value" not in str(report)
