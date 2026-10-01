#!/usr/bin/env python3
"""Быстрый старт руководства как проверка CI (S032, SC-001).

Страница «Пакет за 10 минут» (`guide/docs/packages/quickstart.md` суперпроекта)
исполняется как есть: команды берутся из её блоков кода, файлы — из её блоков
YAML, и всё это идёт в чистом каталоге с изолированными `HOME`, каталогами uv и
конфигурацией git. Стенд не нужен; шаги со стендом страница помечает пропуском.

Разметка страницы — HTML-комментарий последней непустой строкой перед блоком кода
(в собранном руководстве его не видно):

    <!-- quickstart: skip <причина> -->        блок не исполняется (plan/apply);
    <!-- quickstart: file <путь> -->           содержимое блока пишется в файл
                                               относительно текущего каталога;
    <!-- quickstart: requires docker -->      блок исполняется только с Docker;
    <!-- quickstart: without-docker exit=<код> [output=<строка>] -->
                                               без Docker блок исполняется
                                               отдельным `bash -e` и обязан
                                               завершиться этим кодом (и
                                               напечатать строку).

Разметка — одна строка; разметка, разнесённая на несколько строк, и блок кода
внутри многострочного HTML-комментария — ошибки извлечения.

Блок `bash`/`sh`/`shell` без разметки исполняется. Иллюстрации — только блоки из
явного списка ILLUSTRATION (вывод `text` и т.п.). Любой другой блок без разметки —
данные (`yaml`), пустой, `console`, `zsh`, `{.bash}` — ошибка извлечения: шаг
страницы не должен молча выпасть из проверки и из отпечатка.

Подстановки: `<тег>` и `<тег ядра>` заменяются веткой локальных зеркал, а
`git clone https://…/<имя>.git` идёт в зеркало соседа по плоской раскладке
(`url.<зеркало>.insteadOf`): проверяется код этого коммита SDK и соседних клонов,
а не опубликованный выпуск. Неизвестная подстановка — ошибка и в командах, и в
файлах.

Прерывание прогона (лимит, SIGTERM, Ctrl-C) снимает сессию bash целиком и
останавливает контейнер базы, если его запустил этот прогон.

Закрепление. Ревизия суперпроекта закреплена в `ci/quickstart-pin.json`: коммит,
путь страницы и отпечаток плана — sha256 извлечённых шагов (команды, файлы,
разметка; без номеров строк и прозы). `run` исполняет страницу на закреплённом
коммите; `pin check` сравнивает план головы ветки суперпроекта с закреплённым и
падает, если команды или файлы страницы изменились, а закрепление — нет. Правка
прозы проверку не ломает. Сдвиг закрепления — `pin update` и зелёный `run` в том
же коммите SDK.

Публичный зонтик — проекция снимков с собственной историей: закреплённого коммита
приватного суперпроекта в нём нет и не будет. Если коммит в клоне недостижим,
страница сверяется по содержимому: берётся страница на голове клона (`run`) или на
`--against` (`pin check`), и её план обязан совпасть с закреплённым — тогда
исполняется она, иначе ошибка «страница изменилась — `pin update`». Если страницы
нет и там, это `PageMissing`: `pin status` печатает `page=missing`, и CI пропускает
job с пояснением, а не падает. Где коммит достижим, поведение прежнее — по коммиту.

    python3 ci/quickstart.py pin status --superproject ../taimen

    python3 ci/quickstart.py run --superproject ../taimen --db docker
    python3 ci/quickstart.py run --page quickstart.md --db none
    python3 ci/quickstart.py pin check --superproject ../taimen
    python3 ci/quickstart.py pin update --superproject ../taimen --rev origin/main
    python3 ci/quickstart.py extract --page quickstart.md
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PIN_FILE = REPO / "ci" / "quickstart-pin.json"
PAGE_NAME = "quickstart.md"

MARKER = re.compile(r"^\s*<!--\s*quickstart:\s*(?P<body>.*?)\s*-->\s*$")
MARKER_LOOSE = re.compile(r"<!--\s*quickstart\b")
MARKER_CONTINUED = re.compile(r"^\s*quickstart\s*:")
FENCE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})\s*(?P<info>[^`\s]*)[^`]*$")
SHELL = {"bash", "sh", "shell"}
DATA = {"yaml", "yml", "json", "toml"}
# Блоки, которые страница показывает, а не исполняет: вывод команд и схемы.
ILLUSTRATION = {"text", "txt", "output", "mermaid"}

# Ветка локальных зеркал соседей, которой подменяются теги выпуска на странице.
TAG = "quickstart"
PLACEHOLDERS = {"<тег>": TAG, "<тег ядра>": TAG}
PLACEHOLDER = re.compile(r"<[^\s<>][^<>\n]*>")
CLONE = re.compile(
    r"\bgit\s+clone\b[^\n]*?\s(?P<url>https://\S+/(?P<name>[\w.-]+?)(?:\.git)?)(?=\s|$)"
)
DOCKER_NAME = re.compile(r"\bdocker\s+run\b[^\n]*?--name[ =](?P<name>[\w.-]+)")

# Окружение, которое проходит в чистый каталог; остальное (токены, VIRTUAL_ENV,
# адрес базы песочницы, PYTHONPATH) отсекается.
PASS_ENV = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "UV_INDEX_URL",
    "UV_DEFAULT_INDEX",
    "UV_EXTRA_INDEX_URL",
)


class QuickstartError(Exception):
    """Страница не извлекается или прогон не прошёл."""


class PageMissing(QuickstartError):
    """Закреплённый коммит в клоне зонтика недостижим, а страницы на голове нет."""


@dataclass(frozen=True)
class Step:
    """Шаг страницы: команды оболочки или файл."""

    line: int
    kind: str  # "run" | "file" | "skip"
    text: str
    path: str | None = None
    reason: str | None = None
    requires_docker: bool = False
    without_docker_exit: int | None = None
    without_docker_output: str | None = None

    def canonical(self) -> dict[str, object]:
        """Смысл шага без номера строки: из него считается отпечаток плана."""
        data: dict[str, object] = {"kind": self.kind, "text": self.text}
        # Причина пропуска — проза, в отпечаток не входит.
        for key in ("path", "requires_docker", "without_docker_exit", "without_docker_output"):
            value = getattr(self, key)
            if value not in (None, False):
                data[key] = value
        return data


@dataclass
class Page:
    steps: list[Step] = field(default_factory=list)

    @property
    def executed(self) -> list[Step]:
        return [step for step in self.steps if step.kind != "skip"]

    def plan_hash(self) -> str:
        # Пропущенные блоки входят в отпечаток тоже: их команды не исполняются, но их
        # правка на странице должна дойти до SDK, как и правка исполняемых.
        body = json.dumps(
            [step.canonical() for step in self.steps], ensure_ascii=False, sort_keys=True
        )
        return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()

    def clones(self) -> dict[str, str]:
        """URL клона → имя репозитория по всем исполняемым блокам."""
        found: dict[str, str] = {}
        for step in self.executed:
            if step.kind == "run":
                for match in CLONE.finditer(step.text):
                    found[match["url"]] = match["name"]
        return found

    def docker_names(self) -> list[str]:
        names: list[str] = []
        for step in self.executed:
            if step.kind == "run":
                names += [m["name"] for m in DOCKER_NAME.finditer(step.text)]
        return names


# --- извлечение -------------------------------------------------------------------


def _blocks(text: str) -> Iterator[tuple[int, str, str, str | None, int | None]]:
    """(строка ограды, язык, содержимое, тело разметки, строка разметки)."""
    lines = text.splitlines()
    marker: tuple[str, int] | None = None
    comment: int | None = None  # строка начала многострочного HTML-комментария
    index = 0
    while index < len(lines):
        line = lines[index]
        if comment is not None:
            if MARKER_CONTINUED.match(line) or MARKER_LOOSE.search(line):
                raise QuickstartError(
                    f"{PAGE_NAME}:{index + 1}: разметка quickstart в многострочном комментарии "
                    f"(с строки {comment}) — пишите её одной строкой <!-- quickstart: … -->"
                )
            if FENCE.match(line):
                raise QuickstartError(
                    f"{PAGE_NAME}:{index + 1}: блок кода внутри HTML-комментария (с строки "
                    f"{comment}) — извлекатель его не исполняет"
                )
            if "-->" in line:
                comment = None
            index += 1
            continue
        found = MARKER.match(line)
        if found:
            if marker is not None:
                raise QuickstartError(
                    f"{PAGE_NAME}:{marker[1]}: разметка без блока кода — следом идёт "
                    f"другая разметка (строка {index + 1})"
                )
            marker = (found["body"], index + 1)
            index += 1
            continue
        if MARKER_LOOSE.search(line):
            raise QuickstartError(
                f"{PAGE_NAME}:{index + 1}: разметка quickstart должна занимать строку целиком: "
                f"{line.strip()}"
            )
        fence = FENCE.match(line)
        if fence:
            indent = len(fence["indent"].expandtabs())
            mark = fence["fence"]
            body: list[str] = []
            start = index + 1
            index += 1
            while index < len(lines):
                close = lines[index].strip()
                if close.startswith(mark[0] * len(mark)) and close.strip(mark[0]) == "":
                    break
                raw = lines[index].expandtabs()
                strip = min(indent, len(raw) - len(raw.lstrip(" ")))
                body.append(raw[strip:])
                index += 1
            else:
                raise QuickstartError(f"{PAGE_NAME}:{start}: блок кода не закрыт")
            content = "\n".join(body) + ("\n" if body else "")
            yield (
                start,
                fence["info"].lower(),
                content,
                marker[0] if marker else None,
                marker[1] if marker else None,
            )
            marker = None
            index += 1
            continue
        if marker is not None and line.strip():
            raise QuickstartError(
                f"{PAGE_NAME}:{marker[1]}: разметка «{marker[0]}» не стоит прямо перед блоком "
                f"кода (строка {index + 1} — текст)"
            )
        opened = line.rfind("<!--")
        if opened != -1 and "-->" not in line[opened:]:
            comment = index + 1
        index += 1
    if marker is not None:
        raise QuickstartError(f"{PAGE_NAME}:{marker[1]}: разметка без блока кода в конце страницы")
    if comment is not None:
        raise QuickstartError(f"{PAGE_NAME}:{comment}: HTML-комментарий не закрыт")


def _substitute(text: str, line: int) -> str:
    for found in PLACEHOLDER.findall(text):
        if found not in PLACEHOLDERS:
            raise QuickstartError(
                f"{PAGE_NAME}:{line}: неизвестная подстановка {found} — добавьте её в "
                f"PLACEHOLDERS ci/quickstart.py или уберите со страницы"
            )
    for key, value in PLACEHOLDERS.items():
        text = text.replace(key, value)
    return text


def _file_path(value: str, line: int) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise QuickstartError(
            f"{PAGE_NAME}:{line}: путь файла «{value}» — только относительный, без «..»"
        )
    return path.as_posix()


def _directive(body: str, marker_line: int, line: int, lang: str, content: str) -> Step:
    word, _, rest = body.partition(" ")
    rest = rest.strip()
    if word == "skip":
        if not rest:
            raise QuickstartError(f"{PAGE_NAME}:{marker_line}: skip без причины")
        return Step(line=line, kind="skip", text=content, reason=rest)
    if word == "file":
        return Step(
            line=line,
            kind="file",
            text=_substitute(content, line),
            path=_file_path(rest, marker_line),
        )
    if lang not in SHELL:
        raise QuickstartError(
            f"{PAGE_NAME}:{marker_line}: «{word}» — только для блоков оболочки, здесь «{lang}»"
        )
    if word == "requires":
        if rest != "docker":
            raise QuickstartError(f"{PAGE_NAME}:{marker_line}: requires знает только docker")
        return Step(line=line, kind="run", text=_substitute(content, line), requires_docker=True)
    if word == "without-docker":
        options: dict[str, str] = {}
        for token in shlex.split(rest):
            key, sep, value = token.partition("=")
            if not sep or key not in {"exit", "output"} or key in options:
                raise QuickstartError(
                    f"{PAGE_NAME}:{marker_line}: without-docker exit=<код> [output=<строка>]"
                )
            options[key] = value
        if not options.get("exit", "").isdigit():
            raise QuickstartError(f"{PAGE_NAME}:{marker_line}: without-docker без exit=<код>")
        return Step(
            line=line,
            kind="run",
            text=_substitute(content, line),
            without_docker_exit=int(options["exit"]),
            without_docker_output=options.get("output") or None,
        )
    raise QuickstartError(f"{PAGE_NAME}:{marker_line}: неизвестная разметка «{body}»")


def extract(text: str) -> Page:
    """Шаги страницы по порядку. Ошибка разметки — QuickstartError с номером строки."""
    page = Page()
    for line, lang, content, body, marker_line in _blocks(text):
        if body is not None and marker_line is not None:
            page.steps.append(_directive(body, marker_line, line, lang, content))
        elif lang in SHELL:
            page.steps.append(Step(line=line, kind="run", text=_substitute(content, line)))
        elif lang not in ILLUSTRATION:
            kind = f"блок «{lang}»" if lang else "блок без языка"
            raise QuickstartError(
                f"{PAGE_NAME}:{line}: {kind} без разметки не исполняется и не иллюстрация — "
                f"язык из {sorted(SHELL)} или {sorted(ILLUSTRATION)}, либо "
                f"<!-- quickstart: file <путь> --> / <!-- quickstart: skip <причина> -->"
            )
    if not page.executed:
        raise QuickstartError(f"{PAGE_NAME}: на странице нет исполняемых шагов")
    return page


# --- сценарий оболочки -----------------------------------------------------------


def _heredoc_tag(content: str) -> str:
    tag = "QUICKSTART_EOF"
    while tag in content:
        tag += "_"
    return tag


def render(page: Page, *, docker: bool) -> str:
    """Один сценарий bash: каталог и переменные переходят из блока в блок, как у читателя."""
    out = [
        "set -euo pipefail",
        "qs_block=0",
        "trap 'rc=$?; echo \"quickstart: провал в блоке quickstart.md:$qs_block (код $rc)\" >&2'"
        " ERR",
    ]
    containers: list[str] = []
    for step in page.steps:
        where = f"quickstart.md:{step.line}"
        out.append(f"qs_block={step.line}")
        if step.kind == "skip":
            out.append(f"echo {shlex.quote(f'== {where}: пропуск — {step.reason}')}")
            continue
        if step.kind == "file":
            assert step.path is not None
            tag = _heredoc_tag(step.text)
            out.append(f'echo "== {where} [+${{SECONDS}}s]: файл "{shlex.quote(step.path)}')
            parent = str(Path(step.path).parent)
            if parent != ".":
                out.append(f"mkdir -p {shlex.quote(parent)}")
            out.append(f"cat > {shlex.quote(step.path)} <<'{tag}'\n{step.text}{tag}")
            continue
        shown = "".join(f"$ {line}\n" for line in step.text.splitlines())
        if step.requires_docker and not docker:
            out.append(f"echo {shlex.quote(f'== {where}: пропуск — нужен Docker (--db none)')}")
            continue
        out.append(f'echo "== {where} [+${{SECONDS}}s]"')
        out.append(f"printf '%s' {shlex.quote(shown)}")
        if step.without_docker_exit is not None and not docker:
            expected = step.without_docker_exit
            tag = _heredoc_tag(step.text)
            # Отдельный процесс bash: внутри `… && … || …` set -e подоболочки не действует
            # и промежуточный сбой блока прошёл бы незамеченным.
            out += [
                'qs_step="$(mktemp)"',
                'qs_log="$(mktemp)"',
                f"cat > \"$qs_step\" <<'{tag}'\n{step.text}{tag}",
                'bash -eo pipefail "$qs_step" >"$qs_log" 2>&1 && qs_rc=0 || qs_rc=$?',
                'cat "$qs_log"',
                f'if [ "$qs_rc" -ne {expected} ]; then',
                f'  echo "quickstart: {where} без Docker ждёт код {expected}, получен $qs_rc" >&2',
                "  exit 1",
                "fi",
            ]
            if step.without_docker_output:
                needle = shlex.quote(step.without_docker_output)
                missing = (
                    f"quickstart: {where} без Docker не напечатал «{step.without_docker_output}»"
                )
                out += [
                    f'if ! grep -qF -- {needle} "$qs_log"; then',
                    f"  echo {shlex.quote(missing)} >&2",
                    "  exit 1",
                    "fi",
                ]
            out.append(f"echo {shlex.quote(f'   ожидаемо без Docker: код {expected}')}")
            continue
        if step.requires_docker:
            # Контейнер запускает этот прогон — он его и останавливает, даже при провале
            # посреди блока. Чужого контейнера с тем же именем нет: run_page проверил.
            containers += [match["name"] for match in DOCKER_NAME.finditer(step.text)]
            if containers:
                names = " ".join(shlex.quote(name) for name in containers)
                out.append(f"trap 'docker stop {names} >/dev/null 2>&1 || true' EXIT")
        out.append(step.text.rstrip("\n"))
    out.append('echo "== конец страницы [+${SECONDS}s]"')
    return "\n".join(out) + "\n"


# --- закрепление -------------------------------------------------------------------


def load_pin(path: Path = PIN_FILE) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    missing = {"ref", "path", "commit", "plan"} - set(data)
    if missing:
        raise QuickstartError(f"{path.name}: нет полей {sorted(missing)}")
    return {key: str(value) for key, value in data.items()}


def _git(*args: str, cwd: Path) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if done.returncode != 0:
        raise QuickstartError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def page_at(superproject: Path, rev: str, path: str) -> tuple[str, str]:
    """(полный sha коммита, текст страницы на нём)."""
    commit = _git("rev-parse", "--verify", f"{rev}^{{commit}}", cwd=superproject).strip()
    return commit, _git("show", f"{commit}:{path}", cwd=superproject)


def _git_ok(*args: str, cwd: Path) -> bool:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)
    return done.returncode == 0


def commit_reachable(superproject: Path, commit: str) -> bool:
    """Есть ли закреплённый коммит в клоне (в публичной проекции зонтика его нет)."""
    return bool(commit) and _git_ok("cat-file", "-e", f"{commit}^{{commit}}", cwd=superproject)


def page_exists(superproject: Path, rev: str, path: str) -> bool:
    return _git_ok("cat-file", "-e", f"{rev}:{path}", cwd=superproject)


def pinned_page(superproject: Path, pin: dict[str, str], head: str = "HEAD") -> tuple[str, str]:
    """(коммит, текст) страницы, которую исполняет `run`, сверенной с закреплением.

    Закреплённый коммит достижим — страница на нём. Недостижим (публичный зонтик) —
    страница на `head`, если её план равен закреплённому: сверка по содержимому.
    """
    by_commit = commit_reachable(superproject, pin["commit"])
    if not by_commit and not page_exists(superproject, head, pin["path"]):
        raise PageMissing(
            f"страница {pin['path']} ещё не опубликована в зонтике: её нет на {head}, "
            f"а закреплённый коммит {pin['commit'][:12]} в клоне недостижим"
        )
    commit, text = page_at(superproject, pin["commit"] if by_commit else head, pin["path"])
    plan = extract(text).plan_hash()
    if plan != pin["plan"]:
        if by_commit:
            raise QuickstartError(
                f"план страницы на {commit[:12]} ({plan}) не равен закреплённому "
                f"({pin['plan']}) — `pin update`"
            )
        raise QuickstartError(
            f"страница изменилась — `pin update`: план {pin['path']} на {head} "
            f"({commit[:12]}, {plan}) не равен закреплённому ({pin['plan']}); закреплённый "
            f"коммит {pin['commit'][:12]} в клоне недостижим, сверка по содержимому"
        )
    return commit, text


def pin_status(superproject: Path, against: str = "HEAD", pin_path: Path = PIN_FILE) -> str:
    """Строки key=value для CI: page=present|missing, mode=commit|content|missing."""
    pin = load_pin(pin_path)
    if commit_reachable(superproject, pin["commit"]):
        mode = "commit"
    elif page_exists(superproject, against, pin["path"]):
        mode = "content"
    else:
        mode = "missing"
    page = "missing" if mode == "missing" else "present"
    return f"page={page}\nmode={mode}\npath={pin['path']}"


def pin_check(superproject: Path, against: str, pin_path: Path = PIN_FILE) -> str:
    pin = load_pin(pin_path)
    if not commit_reachable(superproject, pin["commit"]):
        head_commit, _ = pinned_page(superproject, pin, against)
        return (
            f"ok: закреплённый коммит {pin['commit'][:12]} в клоне недостижим, план страницы "
            f"на {against} ({head_commit[:12]}) равен закреплённому — сверка по содержимому"
        )
    _, pinned_text = page_at(superproject, pin["commit"], pin["path"])
    pinned = extract(pinned_text)
    if pinned.plan_hash() != pin["plan"]:
        raise QuickstartError(
            f"закрепление {pin['commit'][:12]}: план страницы {pinned.plan_hash()} не равен "
            f"закреплённому {pin['plan']} — извлекатель или файл закрепления правлены "
            f"без `pin update`"
        )
    head_commit, head_text = page_at(superproject, against, pin["path"])
    head = extract(head_text)
    if head.plan_hash() != pin["plan"]:
        diff = _plan_diff(pinned, head)
        raise QuickstartError(
            f"команды или файлы {pin['path']} изменились после закрепления "
            f"({pin['commit'][:12]} → {head_commit[:12]}), а SDK не обновлён:\n{diff}\n"
            f"Проверьте страницу прогоном и сдвиньте закрепление: "
            f"python3 ci/quickstart.py pin update --superproject … --rev {against}"
        )
    note = "" if head_text == pinned_text else " (проза страницы менялась, команды — нет)"
    return (
        f"ok: закрепление {pin['commit'][:12]} актуально для {against} ({head_commit[:12]}){note}"
    )


def _plan_diff(old: Page, new: Page) -> str:
    import difflib

    def lines(page: Page) -> list[str]:
        return render(page, docker=True).splitlines()

    return "\n".join(
        difflib.unified_diff(lines(old), lines(new), "закреплено", "голова", lineterm="", n=1)
    )


def pin_update(superproject: Path, rev: str, pin_path: Path = PIN_FILE) -> str:
    pin = load_pin(pin_path)
    commit, text = page_at(superproject, rev, pin["path"])
    pin["commit"] = commit
    pin["plan"] = extract(text).plan_hash()
    pin_path.write_text(json.dumps(pin, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return f"закреплено: {commit} {pin['plan']}"


# --- прогон ------------------------------------------------------------------------


def _mirror(source: Path, mirror: Path) -> None:
    """Голое зеркало с веткой TAG на HEAD соседа (и из поверхностного клона CI)."""
    if not (source / ".git").exists():
        raise QuickstartError(f"сосед {source} — не git-клон")
    subprocess.run(["git", "init", "--quiet", "--bare", str(mirror)], check=True)
    subprocess.run(
        ["git", "-C", str(mirror), "config", "receive.shallowUpdate", "true"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "push",
            "--quiet",
            "--no-verify",
            str(mirror),
            f"HEAD:refs/heads/{TAG}",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(mirror), "symbolic-ref", "HEAD", f"refs/heads/{TAG}"], check=True
    )


def _environment(work: Path, uv_cache: Path | None) -> dict[str, str]:
    env = {key: os.environ[key] for key in PASS_ENV if key in os.environ}
    home = work / "home"
    home.mkdir()
    tools = work / "uv"
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_CACHE_HOME=str(home / ".cache"),
        XDG_DATA_HOME=str(home / ".local" / "share"),
        UV_TOOL_DIR=str(tools / "tools"),
        UV_TOOL_BIN_DIR=str(tools / "bin"),
        UV_CACHE_DIR=str(uv_cache or tools / "cache"),
        UV_PYTHON_INSTALL_DIR=str(tools / "python"),
        GIT_CONFIG_GLOBAL=str(work / "gitconfig"),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        PYTHONUTF8="1",
    )
    env["PATH"] = f"{tools / 'bin'}{os.pathsep}{env.get('PATH', os.defpath)}"
    env.setdefault("LANG", "C.UTF-8")
    real_docker = Path.home() / ".docker"
    if "DOCKER_CONFIG" not in env and real_docker.is_dir():
        # Контекст Docker Desktop живёт в ~/.docker — без него CLI не найдёт демон.
        env["DOCKER_CONFIG"] = str(real_docker)
    return env


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    done = subprocess.run(["docker", "info"], capture_output=True, check=False)
    return done.returncode == 0


def _container_exists(name: str) -> bool:
    done = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"name=^/{name}$"],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(done.stdout.strip())


def _rmtree(path: Path) -> None:
    def retry(func, target, _exc):  # type: ignore[no-untyped-def]
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        func(target)

    shutil.rmtree(path, onexc=retry)


def _stop(process: subprocess.Popen[bytes], containers: list[str]) -> None:
    """Снять группу процессов прогона и его контейнеры."""
    for sig, grace in ((signal.SIGTERM, 15.0), (signal.SIGKILL, None)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=grace)
            break
        except subprocess.TimeoutExpired:
            continue
    # Лидер группы мог выйти раньше детей: добить оставшихся.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    for name in containers:
        if _container_exists(name):
            subprocess.run(["docker", "stop", name], capture_output=True, check=False)


def _terminate(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def run_page(
    page: Page,
    *,
    db: str,
    neighbours: Path,
    timeout: int,
    workdir: Path | None = None,
    keep: bool = False,
    uv_cache: Path | None = None,
) -> float:
    """Исполнить страницу в чистом каталоге. Возвращает длительность в секундах."""
    docker = db == "docker"
    names = page.docker_names() if docker else []
    if docker:
        if not _docker_available():
            raise QuickstartError("--db docker: Docker недоступен; без него — --db none")
        busy = [name for name in names if _container_exists(name)]
        if busy:
            raise QuickstartError(
                f"контейнер {', '.join(busy)} уже есть — страница создаёт его сама; "
                f"остановите чужой прогон или дождитесь его"
            )
    work = Path(tempfile.mkdtemp(prefix="quickstart-", dir=workdir)).resolve()
    started = time.monotonic()
    try:
        mirrors = work / "mirrors"
        mirrors.mkdir()
        config: list[str] = []
        for url, name in sorted(page.clones().items()):
            source = REPO if name == REPO.name or name == "package-sdk" else neighbours / name
            target = mirrors / f"{name}.git"
            if not target.exists():
                _mirror(source, target)
            config += [f'[url "{target.as_uri()}"]', f"\tinsteadOf = {url}"]
        (work / "gitconfig").write_text("\n".join(config) + "\n", encoding="utf-8")
        env = _environment(work, uv_cache)
        root = work / "reader"
        root.mkdir()
        script = work / "quickstart.sh"
        script.write_text(render(page, docker=docker), encoding="utf-8")
        print(f"quickstart: каталог {work}, база: {db}, лимит {timeout} с", flush=True)
        process = subprocess.Popen(["bash", str(script)], cwd=root, env=env, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except BaseException as error:
            # Лимит, SIGTERM (main превращает его в SystemExit) или Ctrl-C: снять всю
            # сессию bash и контейнер базы, который запустил этот прогон.
            _stop(process, names)
            if isinstance(error, subprocess.TimeoutExpired):
                raise QuickstartError(
                    f"быстрый старт не уложился в {timeout} с — прерван"
                ) from None
            raise
        elapsed = time.monotonic() - started
        if code != 0:
            keep = True
            raise QuickstartError(f"быстрый старт упал (код {code}) за {elapsed:.0f} с")
        return elapsed
    finally:
        if keep:
            print(f"quickstart: каталог прогона сохранён — {work}", file=sys.stderr)
        else:
            _rmtree(work)


# --- CLI ---------------------------------------------------------------------------


def _page_from_args(args: argparse.Namespace) -> Page:
    if args.page:
        return extract(Path(args.page).read_text(encoding="utf-8"))
    pin = load_pin()
    if args.rev is None:
        _, text = pinned_page(Path(args.superproject), pin)
    else:
        _, text = page_at(Path(args.superproject), args.rev, pin["path"])
    return extract(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def source(p: argparse.ArgumentParser) -> None:
        group = p.add_mutually_exclusive_group(required=True)
        group.add_argument("--page", help="файл страницы (без закрепления)")
        group.add_argument("--superproject", help="клон суперпроекта: страница на закреплении")
        p.add_argument("--rev", help="ревизия суперпроекта вместо закреплённой")

    extract_cmd = sub.add_parser("extract", help="напечатать сценарий, ничего не исполняя")
    source(extract_cmd)
    extract_cmd.add_argument("--db", choices=["docker", "none"], default="docker")

    run_cmd = sub.add_parser("run", help="исполнить страницу в чистом каталоге")
    source(run_cmd)
    run_cmd.add_argument(
        "--db",
        choices=["docker", "none"],
        default="docker",
        help="docker — база песочницы контейнером со страницы; none — без Docker "
        "(блоки requires docker пропускаются, without-docker ждут своего кода)",
    )
    run_cmd.add_argument("--neighbours", default=str(REPO.parent), help="каталог соседних клонов")
    run_cmd.add_argument("--timeout", type=int, default=600, help="лимит, секунды (SC-001)")
    run_cmd.add_argument("--workdir", help="где создать каталог прогона")
    run_cmd.add_argument("--uv-cache", help="кэш uv (по умолчанию пустой, как в чистой среде)")
    run_cmd.add_argument("--keep", action="store_true", help="не удалять каталог прогона")

    pin_cmd = sub.add_parser("pin", help="закрепление ревизии суперпроекта")
    pin_sub = pin_cmd.add_subparsers(dest="pin_command", required=True)
    check_cmd = pin_sub.add_parser("check", help="команды страницы на голове = закреплённые")
    check_cmd.add_argument("--superproject", required=True)
    check_cmd.add_argument("--against", default="HEAD", help="ревизия головы (по умолчанию HEAD)")
    update_cmd = pin_sub.add_parser("update", help="закрепить ревизию")
    update_cmd.add_argument("--superproject", required=True)
    update_cmd.add_argument("--rev", default="HEAD")
    status_cmd = pin_sub.add_parser(
        "status", help="есть ли страница в клоне зонтика: page=present|missing (для CI)"
    )
    status_cmd.add_argument("--superproject", required=True)
    status_cmd.add_argument("--against", default="HEAD", help="ревизия головы (по умолчанию HEAD)")
    pin_sub.add_parser("show", help="поля закрепления строками key=value")

    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _terminate)
    try:
        if args.command == "extract":
            print(render(_page_from_args(args), docker=args.db == "docker"), end="")
        elif args.command == "run":
            page = _page_from_args(args)
            elapsed = run_page(
                page,
                db=args.db,
                neighbours=Path(args.neighbours).resolve(),
                timeout=args.timeout,
                workdir=Path(args.workdir) if args.workdir else None,
                keep=args.keep,
                uv_cache=Path(args.uv_cache).resolve() if args.uv_cache else None,
            )
            print(f"ok: быстрый старт за {elapsed:.0f} с (лимит {args.timeout} с), база: {args.db}")
        elif args.pin_command == "check":
            print(pin_check(Path(args.superproject), args.against))
        elif args.pin_command == "update":
            print(pin_update(Path(args.superproject), args.rev))
        elif args.pin_command == "status":
            print(pin_status(Path(args.superproject), args.against))
        else:
            for key, value in load_pin().items():
                print(f"{key}={value}")
    except QuickstartError as error:
        print(f"quickstart: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("quickstart: прервано", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
