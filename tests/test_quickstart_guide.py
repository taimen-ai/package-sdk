"""Извлекатель быстрого старта руководства (ci/quickstart.py, S032, SC-001).

Сама страница живёт в суперпроекте; здесь — синтетические страницы с той же
разметкой, прогон сценария в чистом каталоге без сети и Docker и закрепление на
временном git-репозитории.
"""

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("quickstart", REPO / "ci" / "quickstart.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["quickstart"] = module
    spec.loader.exec_module(module)
    return module


qs = _load()
F = "```"

PAGE = f"""# Пакет

Установите:

{F}bash
mkdir src && cd src
git clone --branch <тег> https://example.org/org/package-sdk.git
git clone --branch <тег ядра> https://example.org/org/control-plane
{F}

{F}text
вывод — иллюстрация
{F}

Замените файл:

<!-- quickstart: file task-types/review.yaml -->
{F}yaml
key: review
{F}

!!! tip "Внутри заметки"
    {F}bash
    echo indented
    {F}

<!-- quickstart: requires docker -->
{F}bash
docker run -d --rm --name sandbox-db -p 127.0.0.1:1:5432 postgres:16-alpine
{F}

<!-- quickstart: without-docker exit=1 output=sandbox_database_required -->
{F}bash
package-sdk test .
{F}

<!-- quickstart: skip нужен стенд -->
{F}bash
export CP_TOKEN=<access token>
package-sdk plan
{F}
"""


def test_extract_steps_in_order() -> None:
    page = qs.extract(PAGE)
    kinds = [(step.kind, step.path) for step in page.steps]
    assert kinds == [
        ("run", None),
        ("file", "task-types/review.yaml"),
        ("run", None),
        ("run", None),
        ("run", None),
        ("skip", None),
    ]
    setup, file, indented, docker, test, skip = page.steps
    assert "--branch quickstart https://example.org/org/package-sdk.git" in setup.text
    assert "<тег" not in setup.text
    assert file.text == "key: review\n"
    assert indented.text == "echo indented\n"
    assert docker.requires_docker
    assert (test.without_docker_exit, test.without_docker_output) == (
        1,
        "sandbox_database_required",
    )
    assert skip.reason == "нужен стенд" and "<access token>" in skip.text
    assert page.clones() == {
        "https://example.org/org/package-sdk.git": "package-sdk",
        "https://example.org/org/control-plane": "control-plane",
    }
    assert page.docker_names() == ["sandbox-db"]


@pytest.mark.parametrize(
    ("page", "message"),
    [
        (f"{F}yaml\nkey: x\n{F}\n", "блок «yaml» без разметки"),
        # Шаг не выпадает молча: пустой и неизвестный язык — ошибка, а не иллюстрация.
        (f"{F}bash\ntrue\n{F}\n\n{F}\npackage-sdk publish .\n{F}\n", "блок без языка"),
        (f"{F}bash\ntrue\n{F}\n{F}console\n$ package-sdk publish .\n{F}\n", "«console»"),
        (f"{F}bash\ntrue\n{F}\n{F}zsh\npackage-sdk publish .\n{F}\n", "«zsh»"),
        (f"{F}bash\ntrue\n{F}\n{F}{{.bash}}\npackage-sdk publish .\n{F}\n", "«{.bash}»"),
        (
            f"<!--\nquickstart: skip стенд\n-->\n{F}bash\ntrue\n{F}\n",
            "многострочном комментарии",
        ),
        (
            f"<!-- пояснение\n  quickstart: file a.yaml -->\n{F}yaml\nk: v\n{F}\n",
            "многострочном комментарии",
        ),
        (f"<!--\n{F}bash\ntrue\n{F}\n-->\n{F}bash\ntrue\n{F}\n", "внутри HTML-комментария"),
        (f"{F}bash\ntrue\n{F}\n<!-- не закрыт\n", "не закрыт"),
        (
            f"<!-- quickstart: file a.yaml -->\n{F}yaml\nref: <версия>\n{F}\n{F}bash\ntrue\n{F}\n",
            "подстановка <версия>",
        ),
        (f"<!-- quickstart: frobnicate -->\n{F}bash\ntrue\n{F}\n", "неизвестная разметка"),
        (f"<!-- quickstart: skip -->\n{F}bash\ntrue\n{F}\n", "skip без причины"),
        (f"<!-- quickstart: file /etc/x -->\n{F}yaml\nk: v\n{F}\n", "только относительный"),
        (f"<!-- quickstart: file ../x.yaml -->\n{F}yaml\nk: v\n{F}\n", "без «..»"),
        (f"<!-- quickstart: skip стенд -->\nтекст\n{F}bash\ntrue\n{F}\n", "не стоит прямо перед"),
        (f"{F}bash\ntrue\n{F}\n<!-- quickstart: skip стенд -->\n", "в конце страницы"),
        (
            f"<!-- quickstart: skip a -->\n<!-- quickstart: skip b -->\n{F}bash\ntrue\n{F}\n",
            "другая разметка",
        ),
        (f"текст <!-- quickstart: skip a -->\n{F}bash\ntrue\n{F}\n", "строку целиком"),
        (f"<!-- quickstart: requires db -->\n{F}bash\ntrue\n{F}\n", "только docker"),
        (f"<!-- quickstart: requires docker -->\n{F}yaml\nk: v\n{F}\n", "только для блоков"),
        (f"<!-- quickstart: without-docker output=x -->\n{F}bash\ntrue\n{F}\n", "без exit"),
        (f"{F}bash\ngit clone --branch <версия> https://x/y.git\n{F}\n", "подстановка <версия>"),
        (f"{F}bash\ntrue\n", "не закрыт"),
        (f"{F}text\nтолько вывод\n{F}\n", "нет исполняемых шагов"),
    ],
)
def test_extract_rejects(page: str, message: str) -> None:
    with pytest.raises(qs.QuickstartError, match=message):
        qs.extract(page)


def test_extract_accepts_comments_and_illustrations() -> None:
    page = qs.extract(
        f"<!--\nПояснение редактору: job «quickstart» исполняет страницу.\n-->\n"
        f"{F}mermaid\ngraph LR\n{F}\n"
        f"<!-- quickstart: file a.yaml -->\n{F}yaml\nref: <тег>\n{F}\n"
        f"{F}bash\ntrue\n{F}\n"
    )
    assert [step.kind for step in page.steps] == ["file", "run"]
    assert page.steps[0].text == f"ref: {qs.TAG}\n"


def test_plan_hash_follows_commands_not_prose() -> None:
    base = qs.extract(PAGE).plan_hash()
    prose = PAGE.replace("Установите:", "Поставьте инструмент\nи соседей:\n\nВот так:")
    assert qs.extract(prose).plan_hash() == base
    reason = PAGE.replace("skip нужен стенд", "skip нужен стенд разработчика")
    assert qs.extract(reason).plan_hash() == base
    command = PAGE.replace("package-sdk test .", "package-sdk test --all .")
    assert qs.extract(command).plan_hash() != base
    skipped = PAGE.replace("package-sdk plan", "package-sdk plan --out plan.json")
    assert qs.extract(skipped).plan_hash() != base
    content = PAGE.replace("key: review", "key: reviews")
    assert qs.extract(content).plan_hash() != base


def test_render_modes() -> None:
    page = qs.extract(PAGE)
    docker = qs.render(page, docker=True)
    assert "docker run -d --rm --name sandbox-db" in docker
    trap = "trap 'docker stop sandbox-db >/dev/null 2>&1 || true' EXIT"
    assert trap in docker
    # Ловушка стоит до блока: провал посреди блока тоже останавливает контейнер.
    assert docker.index(trap) < docker.index("\ndocker run -d")
    assert "qs_rc" not in docker
    plain = qs.render(page, docker=False)
    assert "\ndocker run" not in plain and "нужен Docker" in plain
    assert '"$qs_rc" -ne 1' in plain and "sandbox_database_required" in plain
    assert 'bash -eo pipefail "$qs_step"' in plain
    assert "package-sdk plan" not in plain


def test_heredoc_tag_never_collides() -> None:
    page = qs.extract(
        f"<!-- quickstart: file a.txt -->\n{F}text\nQUICKSTART_EOF\n{F}\n{F}bash\ntrue\n{F}\n"
    )
    assert "<<'QUICKSTART_EOF_'" in qs.render(page, docker=False)


def _run(page: str, tmp_path: Path, **kwargs: object) -> float:
    options: dict[str, object] = {
        "db": "none",
        "neighbours": tmp_path,
        "timeout": 60,
        "workdir": tmp_path,
    }
    options.update(kwargs)
    return float(qs.run_page(qs.extract(page), **options))


def test_run_executes_page_in_clean_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CP_TOKEN", "leaked")
    monkeypatch.setenv("PACKAGE_SDK_SANDBOX_DATABASE_URL", "postgresql://leaked")
    page = f"""{F}bash
mkdir pkg && cd pkg
test -z "${{CP_TOKEN:-}}" && test -z "${{PACKAGE_SDK_SANDBOX_DATABASE_URL:-}}"
case "$HOME" in "$PWD"/*|*/quickstart-*/home) ;; *) exit 9 ;; esac
{F}

<!-- quickstart: file tests/a.yaml -->
{F}yaml
key: a
{F}

<!-- quickstart: requires docker -->
{F}bash
exit 7
{F}

<!-- quickstart: without-docker exit=3 output=no-db -->
{F}bash
echo no-db
exit 3
{F}

{F}bash
grep -q "key: a" tests/a.yaml
{F}
"""
    assert _run(page, tmp_path) >= 0
    assert not list(tmp_path.glob("quickstart-*")), "каталог прогона убран после успеха"


@pytest.mark.parametrize(
    ("page", "message"),
    [
        (f"{F}bash\ntrue\nfalse\n{F}\n", "упал"),
        (f"<!-- quickstart: without-docker exit=1 -->\n{F}bash\ntrue\n{F}\n", "упал"),
        (
            f"<!-- quickstart: without-docker exit=1 output=needle -->\n{F}bash\nexit 1\n{F}\n",
            "упал",
        ),
        # Промежуточный сбой блока without-docker не маскируется ожидаемым кодом в конце.
        (f"<!-- quickstart: without-docker exit=3 -->\n{F}bash\nfalse\nexit 3\n{F}\n", "упал"),
    ],
)
def test_run_fails_on_broken_step(
    page: str, message: str, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(qs.QuickstartError, match=message):
        _run(page, tmp_path)
    err = capfd.readouterr().err
    assert "quickstart" in err
    assert list(tmp_path.glob("quickstart-*")), "каталог провала сохранён для разбора"


def test_run_enforces_time_limit(tmp_path: Path) -> None:
    with pytest.raises(qs.QuickstartError, match="не уложился в 1 с"):
        _run(f"{F}bash\nsleep 30\n{F}\n", tmp_path, timeout=1)


def test_sigterm_stops_the_whole_session(tmp_path: Path) -> None:
    pid_file = tmp_path / "bash.pid"
    page = tmp_path / "page.md"
    page.write_text(f"{F}bash\necho $$ > {pid_file}\nsleep 60 &\nsleep 60\n{F}\n", encoding="utf-8")
    runner = subprocess.Popen(
        [
            sys.executable,
            str(REPO / "ci" / "quickstart.py"),
            "run",
            "--page",
            str(page),
            "--db",
            "none",
            "--workdir",
            str(tmp_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while not (pid_file.exists() and pid_file.read_text().strip()):
            assert time.monotonic() < deadline, "сценарий не стартовал"
            time.sleep(0.05)
        session = int(pid_file.read_text())
        runner.send_signal(signal.SIGTERM)
        assert runner.wait(timeout=30) == 128 + signal.SIGTERM
    finally:
        if runner.poll() is None:
            runner.kill()
    deadline = time.monotonic() + 10
    while True:
        try:
            os.killpg(session, 0)
        except ProcessLookupError:
            break
        assert time.monotonic() < deadline, "процессы сессии bash пережили SIGTERM"
        time.sleep(0.05)
    assert not list(tmp_path.glob("quickstart-*")), "каталог прерванного прогона убран"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_pin_update_and_check(tmp_path: Path) -> None:
    repo = tmp_path / "super"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "ci@example.org")
    _git(repo, "config", "user.name", "ci")
    page = repo / "guide" / "quickstart.md"
    page.parent.mkdir()

    def commit(text: str) -> str:
        page.write_text(text, encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "--quiet", "-m", "page")
        return _git(repo, "rev-parse", "HEAD")

    first = commit(PAGE)
    pin = tmp_path / "pin.json"
    pin.write_text(
        json.dumps({"ref": "main", "path": "guide/quickstart.md", "commit": "", "plan": ""}),
        encoding="utf-8",
    )
    qs.pin_update(repo, "HEAD", pin)
    assert json.loads(pin.read_text())["commit"] == first
    assert "актуально" in qs.pin_check(repo, "HEAD", pin)

    commit(PAGE.replace("Установите:", "Поставьте:"))
    assert "проза страницы менялась" in qs.pin_check(repo, "HEAD", pin)

    commit(PAGE.replace("package-sdk test .", "package-sdk test --strict ."))
    with pytest.raises(qs.QuickstartError, match="изменились после закрепления"):
        qs.pin_check(repo, "HEAD", pin)

    stale = json.loads(pin.read_text())
    stale["plan"] = "sha256:0"
    pin.write_text(json.dumps(stale), encoding="utf-8")
    with pytest.raises(qs.QuickstartError, match="без `pin update`"):
        qs.pin_check(repo, first, pin)


def _umbrella(path: Path, page: str | None) -> Path:
    """Клон зонтика со своей историей: страница (или её нет) в одном коммите."""
    path.mkdir()
    _git(path, "init", "--quiet")
    _git(path, "config", "user.email", "ci@example.org")
    _git(path, "config", "user.name", "ci")
    (path / "README.md").write_text("umbrella\n", encoding="utf-8")
    if page is not None:
        (path / "guide").mkdir()
        (path / "guide" / "quickstart.md").write_text(page, encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "--quiet", "-m", "snapshot")
    return path


def _pinned(tmp_path: Path) -> tuple[Path, Path, str]:
    """Закрепление на приватном суперпроекте: (суперпроект, файл закрепления, коммит)."""
    private = _umbrella(tmp_path / "private", PAGE)
    pin = tmp_path / "pin.json"
    pin.write_text(
        json.dumps({"ref": "main", "path": "guide/quickstart.md", "commit": "", "plan": ""}),
        encoding="utf-8",
    )
    qs.pin_update(private, "HEAD", pin)
    return private, pin, _git(private, "rev-parse", "HEAD")


def test_pin_commit_reachable(tmp_path: Path) -> None:
    """Приватная раскладка: коммит достижим — сверка и прогон по коммиту, как прежде."""
    private, pin, commit = _pinned(tmp_path)
    assert qs.pin_status(private, "HEAD", pin).splitlines()[:2] == ["page=present", "mode=commit"]
    assert "актуально" in qs.pin_check(private, "HEAD", pin)
    # голова ушла вперёд прозой — run всё равно берёт закреплённый коммит
    (private / "guide" / "quickstart.md").write_text(
        PAGE.replace("Установите:", "Поставьте:"), encoding="utf-8"
    )
    _git(private, "commit", "--quiet", "-am", "prose")
    assert qs.pinned_page(private, json.loads(pin.read_text())) == (commit, PAGE)


def test_pin_commit_unreachable_same_content(tmp_path: Path) -> None:
    """Публичный зонтик: коммита нет, страница та же по плану — сверка по содержимому."""
    _, pin, commit = _pinned(tmp_path)
    public = _umbrella(tmp_path / "public", PAGE.replace("Установите:", "Поставьте:"))
    head = _git(public, "rev-parse", "HEAD")
    assert head != commit and not qs.commit_reachable(public, commit)
    assert qs.pin_status(public, "HEAD", pin).splitlines()[:2] == ["page=present", "mode=content"]
    assert "сверка по содержимому" in qs.pin_check(public, "HEAD", pin)
    found, text = qs.pinned_page(public, json.loads(pin.read_text()))
    assert found == head and "Поставьте:" in text

    # команды страницы изменились — понятная ошибка, а не «not our ref»
    (public / "guide" / "quickstart.md").write_text(
        PAGE.replace("package-sdk test .", "package-sdk test --strict ."), encoding="utf-8"
    )
    _git(public, "commit", "--quiet", "-am", "commands")
    with pytest.raises(qs.QuickstartError, match="страница изменилась — `pin update`"):
        qs.pin_check(public, "HEAD", pin)
    with pytest.raises(qs.QuickstartError, match="страница изменилась — `pin update`"):
        qs.pinned_page(public, json.loads(pin.read_text()))


def test_pin_page_not_published(tmp_path: Path) -> None:
    """Страницы в зонтике нет: status — page=missing (CI пропускает job), иначе PageMissing."""
    _, pin, _ = _pinned(tmp_path)
    public = _umbrella(tmp_path / "public", None)
    assert qs.pin_status(public, "HEAD", pin).splitlines()[:2] == ["page=missing", "mode=missing"]
    with pytest.raises(qs.PageMissing, match="ещё не опубликована"):
        qs.pin_check(public, "HEAD", pin)
    with pytest.raises(qs.PageMissing, match="ещё не опубликована"):
        qs.pinned_page(public, json.loads(pin.read_text()))


def test_repository_pin_is_well_formed() -> None:
    pin = qs.load_pin()
    assert len(pin["commit"]) == 40 and pin["plan"].startswith("sha256:")
    assert pin["path"].endswith("quickstart.md")
