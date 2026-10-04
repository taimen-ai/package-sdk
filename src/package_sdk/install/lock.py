"""Источники установки и их фиксация (TAI-ADR-0062 п.6, plan Р4).

Пакет установки берётся из каталога установки (ключ), по пути (``{key, path}``) или из
git по тегу (``{key, git, ref, path?}``). ``package-sdk lock`` записывает рядом с файлом
установки ``packages.lock`` (``package-sdk.lock/v1``): у каждого пакета — источник, версия,
коммит (для git) и ``contentHash`` — хэш канонического набора его файлов
(:func:`package_sdk.model.install_hash`).

План строится только по зафиксированному содержимому:

- источник git без записи в lock — ``lock_required``;
- тег, переставленный в источнике после фиксации, — ``source_ref_moved``: та же установка
  не может молча дать другой результат;
- содержимое, разошедшееся с ``contentHash`` (правленый кэш, правленый пакет по пути), —
  ``content_mismatch``;
- lock, который не соответствует установке (нет пакета, другой источник), — ``lock_stale``.

Для пакетов каталога установки и путей lock необязателен — они и так в git установки; но
если lock есть, он покрывает все пакеты установки и сверяется.

Кэш git — ``$PACKAGE_SDK_CACHE`` или ``$XDG_CACHE_HOME/package-sdk`` (по умолчанию
``~/.cache/package-sdk``), каталог ``git/<sha256(url)>``: bare-зеркало источника и
выгрузки коммитов. Недоступный источник при построении плана — отказ, если коммита из lock
нет в кэше; кэш используется, только если хэш содержимого совпадает с lock.

Кэш сам не растёт бесконечно из-за брошенного: временные выгрузки (``.staging-*``) старше
часа убираются при каждом входе в каталог источника. Выгрузки, на которые больше не
ссылается ни один lock, убирает ``package-sdk cache prune`` (:func:`prune`).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from package_sdk import schema as schema_module
from package_sdk.model import (
    Installation,
    Package,
    PackageError,
    _read_yaml,
    _rel,
    canonical,
    install_hash,
    load_installation,
)

LOCK_FORMAT = "package-sdk.lock/v1"
LOCK_NAME = "packages.lock"
CACHE_ENV = "PACKAGE_SDK_CACHE"
# Временная выгрузка старше этого — брошена упавшим процессом: живая пишется секунды.
STAGING_MAX_AGE = 60 * 60
# Временные каталоги кэша: выгрузка в работе, испорченная и убираемая выгрузки.
_LEFTOVERS = (".staging-*", ".spoiled-*", ".pruned-*")


def warn(message: str) -> None:
    """Предупреждение — в stderr: stdout команд занят их результатом (--json)."""
    print(message, file=sys.stderr)


def touch(path: Path) -> Path:
    """Отметить готовую выгрузку как используемую (mtime — сейчас): `cache prune` не удаляет
    выгрузки моложе STAGING_MAX_AGE, так что читаемую сейчас он не тронет."""
    with contextlib.suppress(OSError):
        os.utime(path)
    return path


def _owned(path: Path, directory: Path) -> bool:
    """Путь кэша, который уборке можно удалять: не символическая ссылка и не ведёт через
    ссылку за пределы каталога источника."""
    if path.is_symlink():
        return False
    try:
        path.resolve().relative_to(directory.resolve())
    except (OSError, ValueError):
        return False
    return True


def lock_path(install: Path) -> Path:
    """packages.lock рядом с файлом установки."""
    return install.parent / LOCK_NAME


def default_cache_dir() -> Path:
    explicit = os.environ.get(CACHE_ENV)
    if explicit:
        return Path(explicit)
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "package-sdk"


# Адрес источника: https без учётных данных в адресе или ssh вида git@host:path — то же, что
# в схеме установки и lock. Токен в адресе попал бы в packages.lock, журнал и ошибки.
_URL = re.compile(
    r"^(https://[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]+)?/[^\s@]*[^\s@/]"
    r"|git@[A-Za-z0-9][A-Za-z0-9.-]*:[^\s@-][^\s@]*)$"
)
# Тег: имя из refs/tags/<ref> без «..», «@{», управляющих символов и ведущего «-».
_REF = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._+/-]*$")
# Подкаталог пакета в репозитории: относительный путь без «.» и «..».
_SUBDIR = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9_][A-Za-z0-9._-]*)*$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
# Сетевые протоколы git: только https и ssh; прочие (file, ext, git) запрещены.
_PROTOCOLS = (
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.https.allow=always",
    "-c",
    "protocol.ssh.allow=always",
)
_FILE_MODES = frozenset({"100644", "100755"})


def check_url(url: str) -> str:
    if not _URL.match(url):
        raise PackageError(
            f"git source {url!r}: expected https://host/path without credentials in the URL or "
            "git@host:path (credentials belong in the git credential helper, not in the "
            "installation)"
        )
    return url


def check_ref(ref: str) -> str:
    if not _REF.match(ref) or ".." in ref or ref.endswith((".", "/", ".lock")) or "//" in ref:
        raise PackageError(f"ref {ref!r}: expected a tag name (refs/tags/<ref>)")
    return ref


def check_subdir(subdir: str) -> str:
    if not _SUBDIR.match(subdir) or any(part in (".", "..") for part in subdir.split("/")):
        raise PackageError(
            f"package path in the repository {subdir!r}: expected a relative path "
            "without `.` and `..`"
        )
    return subdir


class GitCache:
    """Кэш источников git: bare-зеркало на адрес и выгрузки коммитов (только чтение сети).

    Выгрузка — блобы дерева коммита как они лежат в git (``cat-file``): без autocrlf,
    export-subst и фильтров рабочей копии, поэтому хэш содержимого одинаков на любой
    машине. Символические ссылки и сабмодули в пакете не принимаются."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_cache_dir()) / "git"

    def _dir(self, url: str) -> Path:
        return self.root / hashlib.sha256(url.encode("utf-8")).hexdigest()

    def _repo(self, url: str) -> Path:
        return self._dir(url) / "repo.git"

    @contextlib.contextmanager
    def locked(self, url: str) -> Iterator[None]:
        """Один процесс на каталог адреса: зеркало и выгрузки не пишутся наперегонки."""
        with self._locked_dir(self._dir(check_url(url))):
            yield

    @contextlib.contextmanager
    def _locked_dir(self, directory: Path) -> Iterator[None]:
        """Блокировка каталога источника; войдя, убираем брошенные временные выгрузки."""
        directory.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows: без блокировки, выгрузки неизменяемы
            self.sweep(directory)
            yield
            return
        with (directory / ".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                self.sweep(directory)
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @staticmethod
    def sweep(directory: Path, *, max_age: float = STAGING_MAX_AGE) -> list[Path]:
        """Убрать временные выгрузки каталога источника старше max_age секунд — их бросил
        упавший процесс. Живая выгрузка моложе: она пишется под той же блокировкой."""
        limit = time.time() - max_age
        removed: list[Path] = []
        for pattern in _LEFTOVERS:
            # выгрузки — рядом с готовыми, убранные целиком checkouts и repo.git — в корне
            candidates = [*directory.glob(f"checkouts/*/*/{pattern}"), *directory.glob(pattern)]
            for leftover in candidates:
                if not _owned(leftover, directory):
                    continue
                try:
                    if leftover.lstat().st_mtime >= limit:
                        continue
                except FileNotFoundError:
                    continue
                shutil.rmtree(leftover, ignore_errors=True)
                removed.append(leftover)
        return removed

    @staticmethod
    def _run(
        args: list[str], *, text: bool = True, stdin: bytes | None = None, literal: bool = False
    ) -> Any:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if literal:
            env["GIT_LITERAL_PATHSPECS"] = "1"
        try:
            return subprocess.run(
                ["git", *_PROTOCOLS, *args],
                env=env,
                check=True,
                capture_output=True,
                text=text,
                input=stdin,
            ).stdout
        except FileNotFoundError as error:
            raise PackageError("git sources need git in PATH") from error
        except subprocess.CalledProcessError as error:
            output = error.stderr or error.stdout or ""
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            raise PackageError(f"git {' '.join(args[2:4])}: {output.strip()[:400]}") from error

    def _git(self, url: str, *args: str) -> str:
        result: str = self._run(["-C", str(self._repo(url)), *args])
        return result

    def fetch(self, url: str) -> None:
        """Зеркало источника: ветки и теги как в источнике (переставленный тег — тоже)."""
        repo = self._repo(check_url(url))
        if not (repo / "HEAD").exists():
            repo.parent.mkdir(parents=True, exist_ok=True)
            self._run(["init", "--bare", "--quiet", "--end-of-options", str(repo)])
            self._git(url, "remote", "add", "--", "origin", url)
        self._git(
            url,
            "fetch",
            "--quiet",
            "--prune",
            "--force",
            "--no-recurse-submodules",
            "origin",
            "+refs/tags/*:refs/tags/*",
        )

    def resolve(self, url: str, ref: str) -> str:
        """Коммит тега в зеркале (после fetch): только refs/tags/<ref>, ответ — SHA."""
        try:
            commit = self._git(
                url,
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"refs/tags/{check_ref(ref)}^{{commit}}",
            ).strip()
        except PackageError as error:
            raise PackageError(
                f"{url}: no tag {ref!r} — the ref of a git source is a tag only (refs/tags/<ref>), "
                "branches and commits are not accepted"
            ) from error
        if not _SHA.match(commit):
            raise PackageError(f"{url}: tag {ref!r} does not point to a commit")
        return commit

    def has(self, url: str, commit: str) -> bool:
        if not _SHA.match(commit) or not (self._repo(url) / "HEAD").exists():
            return False
        try:
            self._git(url, "cat-file", "-e", "--end-of-options", f"{commit}^{{commit}}")
        except PackageError:
            return False
        return True

    def _checkouts(self, url: str, commit: str, subdir: str | None) -> Path:
        tree = hashlib.sha256((subdir or "").encode("utf-8")).hexdigest()[:16]
        return self._dir(url) / "checkouts" / commit / tree

    def materialize(
        self, url: str, commit: str, subdir: str | None, expected: str | None = None
    ) -> Path:
        """Каталог содержимого пакета на коммите: ``checkouts/<коммит>/<путь>/<хэш>``.

        Выгрузки неизменяемы и называются хэшем своего содержимого: запись — только новым
        каталогом (выгрузка во временный и rename), поэтому параллельное чтение не видит
        полупустой выгрузки, а та же выгрузка другим процессом не подменяется. expected —
        хэш из lock: готовая выгрузка с ним берётся без git, если её содержимое сходится
        с именем; испорченная выгружается заново. Без expected (package-sdk lock) коммит
        выгружается всегда: кэшу lock не доверяет.

        Готовую выгрузку читают без блокировки, и ``cache prune --all`` удаляет её и
        свежей: файл, пропавший посреди сверки хэша, — ясный отказ «повторите», а не
        трассировка ``FileNotFoundError``."""
        parent = self._checkouts(url, commit, subdir)
        if expected is not None:
            ready = parent / expected.removeprefix("sha256:")
            try:
                intact = ready.is_dir() and install_hash(ready) == expected
            except (FileNotFoundError, NotADirectoryError) as error:
                raise PackageError(
                    f"{url} {commit[:12]}: checkout {ready} was removed while being read "
                    "(a concurrent package-sdk cache prune?) — run the command again"
                ) from error
            if intact:
                return touch(ready)
        with self.locked(url):
            staging = self.extract(url, commit, subdir, parent)
            try:
                actual = install_hash(staging)
                target = parent / actual.removeprefix("sha256:")
                if target.is_dir() and install_hash(target) == actual:
                    return touch(target)
                if target.exists():  # испорченная выгрузка: убрать с дороги и заменить
                    spoiled = parent / f".spoiled-{uuid.uuid4().hex}"
                    with contextlib.suppress(FileNotFoundError):
                        target.rename(spoiled)
                    shutil.rmtree(spoiled, ignore_errors=True)
                return touch(self._publish(staging, target, actual))
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _publish(staging: Path, target: Path, actual: str) -> Path:
        """Готовая выгрузка под своё имя. Без fcntl (Windows) блокировки нет, и между
        проверкой и rename ту же выгрузку может положить другой процесс: rename тогда
        падает, и это успех, если на месте то же содержимое, иначе — ясный отказ."""
        try:
            staging.rename(target)
        except OSError as error:
            if not target.is_dir():
                raise PackageError(f"checkout {target} not written: {error}") from error
            if install_hash(target) != actual:
                raise PackageError(
                    f"checkout {target} was written concurrently by another process and its "
                    "content does not match its name — run the command again"
                ) from error
        return target

    def _listing(
        self, url: str, commit: str, subdir: str | None
    ) -> list[tuple[str, str, str, str]]:
        """(режим, вид, объект, путь внутри пакета) дерева пакета — только его поддерево."""
        args = ["ls-tree", "-r", "-z", "--full-tree", "--end-of-options", commit]
        if subdir:
            args += ["--", subdir]
        raw: bytes = self._run(["-C", str(self._repo(url)), *args], text=False, literal=True)
        prefix = f"{subdir}/" if subdir else ""
        entries: list[tuple[str, str, str, str]] = []
        for record in raw.split(b"\0"):
            if not record:
                continue
            meta, _, name = record.partition(b"\t")
            mode, kind, blob = meta.decode("ascii").split()
            try:
                path = name.decode("utf-8")
            except UnicodeDecodeError as error:
                raise PackageError(
                    f"{url} {commit[:12]}: file name is not UTF-8 ({name!r}) — rename the file "
                    "in the package"
                ) from error
            if not path.startswith(prefix):
                continue
            inner = path[len(prefix) :]
            parts = inner.split("/")
            if any(p in ("", ".", "..", ".git") for p in parts):
                raise PackageError(f"{url} {commit[:12]}: invalid path in the tree {path!r}")
            if kind != "blob" or mode not in _FILE_MODES:
                what = {
                    "120000": "symbolic link",
                    "160000": "submodule",
                }.get(mode, f"mode {mode} ({kind})")
                raise PackageError(
                    f"{url} {commit[:12]}: {path} — {what}; a package holds regular files only "
                    "(modes 100644 and 100755)"
                )
            entries.append((mode, kind, blob, inner))
        # Файлы и каталоги (Roles/ и roles/): на файловой системе без учёта регистра это один
        # путь, и выгрузка молча слила бы их.
        folded: dict[str, str] = {}
        for _mode, _kind, _blob, inner in entries:
            parts = inner.split("/")
            for depth in range(1, len(parts) + 1):
                step = "/".join(parts[:depth])
                other = folded.setdefault(step.casefold(), step)
                if other != step:
                    what = "file" if depth == len(parts) else "directory"
                    raise PackageError(
                        f"{url} {commit[:12]}: {other} and {step} differ only in case — on a "
                        f"case-insensitive file system this is the same {what}"
                    )
        return entries

    def _blobs(self, url: str, blobs: list[str]) -> dict[str, bytes]:
        """Содержимое блобов одним вызовом cat-file --batch."""
        if not blobs:
            return {}
        unique = list(dict.fromkeys(blobs))
        output: bytes = self._run(
            ["-C", str(self._repo(url)), "cat-file", "--batch"],
            text=False,
            stdin="".join(f"{blob}\n" for blob in unique).encode("ascii"),
        )
        found: dict[str, bytes] = {}
        position = 0
        for blob in unique:
            end = output.index(b"\n", position)
            header = output[position:end].decode("ascii").split()
            if len(header) != 3 or header[0] != blob or header[1] != "blob":
                raise PackageError(f"{url}: cat-file did not return blob {blob[:12]}: {header}")
            size = int(header[2])
            found[blob] = output[end + 1 : end + 1 + size]
            position = end + 1 + size + 1
        return found

    def extract(self, url: str, commit: str, subdir: str | None, parent: Path) -> Path:
        """Содержимое пакета на коммите во временный каталог в parent."""
        if not _SHA.match(commit):
            raise PackageError(f"commit {commit!r}: expected a SHA-1 of 40 hex digits")
        if subdir:
            check_subdir(subdir)
        entries = self._listing(url, commit, subdir)
        data = self._blobs(url, [blob for _m, _k, blob, _p in entries])
        parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=parent))
        try:
            for _mode, _kind, blob, inner in entries:
                destination = staging.joinpath(*inner.split("/"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data[blob])
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return staging


# --- фиксация ----------------------------------------------------------------------------


@dataclass
class Locked:
    """Запись lock: пакет, его источник и зафиксированное содержимое."""

    key: str
    version: str
    source: dict[str, str]
    content_hash: str
    commit: str | None = None

    def entry(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"key": self.key, "version": self.version, "source": self.source}
        if self.commit:
            entry["commit"] = self.commit
        entry["contentHash"] = self.content_hash
        return entry


@dataclass
class Sources:
    """Установка, собранная по зафиксированным источникам, и её фиксация."""

    installation: Installation
    locked: list[Locked] = field(default_factory=list)
    lock_file: Path | None = None  # None — lock нет (нет источников git)

    @property
    def lock_hash(self) -> str:
        """Хэш фиксации: источники, коммиты и хэши содержимого всех пакетов установки."""
        body = [item.entry() for item in sorted(self.locked, key=lambda i: i.key)]
        return "sha256:" + hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


def read_lock(install: Path) -> dict[str, Any] | None:
    """packages.lock рядом с установкой или None; не той формы — ясный отказ."""
    path = lock_path(install)
    if not path.exists():
        return None
    return read_lock_file(path)


def read_lock_file(path: Path) -> dict[str, Any]:
    """Файл lock (``package-sdk.lock/v1``); не той формы — ясный отказ."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise PackageError(f"{_rel(path)}: not JSON: {error}") from error
    problems = sorted(
        schema_module.validator(schema_module.LOCK).iter_errors(document),
        key=lambda e: list(e.absolute_path),
    )
    if problems:
        first = problems[0]
        where = "/".join(str(p) for p in first.absolute_path) or "(root)"
        raise PackageError(f"{_rel(path)}: not {LOCK_FORMAT}: {where}: {first.message}")
    result: dict[str, Any] = document
    return result


def _source_of(package: Package, base: Path) -> dict[str, str]:
    if package.origin is not None:
        source = {"git": str(package.origin["git"]), "ref": str(package.origin["ref"])}
        if package.origin.get("path"):
            source["path"] = str(package.origin["path"])
        return source
    return {"path": os.path.relpath(package.path.resolve(), base.resolve()).replace(os.sep, "/")}


def _version(package: Package) -> str:
    version = package.spec.get("version")
    if version in (None, ""):
        raise PackageError(f"{_rel(package.path / 'package.yaml')}: no spec.version")
    return str(version)


class _GitSources:
    """Содержимое источников git установки по lock (для model.resolve)."""

    def __init__(
        self,
        entries: dict[str, dict[str, Any]],
        cache: GitCache,
        *,
        fetch: bool,
        log: Callable[[str], None],
    ) -> None:
        self.entries = entries
        self.cache = cache
        self.fetch = fetch
        self.log = log
        self.commits: dict[str, str] = {}

    def __call__(self, entry: dict[str, Any]) -> Path:
        key, url, ref, subdir = _git_entry(entry)
        return self._resolve(key, url, ref, subdir)

    def _resolve(self, key: str, url: str, ref: str, subdir: str | None) -> Path:
        locked = self.entries.get(key)
        if locked is None or locked.get("commit") is None:
            raise PackageError(
                f"lock_required: package {key!r} from git ({url} {ref}) is not locked — "
                "package-sdk lock --install <installation file>"
            )
        wanted = {"git": url, "ref": ref, **({"path": str(subdir)} if subdir else {})}
        if locked.get("source") != wanted:
            raise PackageError(
                f"lock_stale: package {key!r}: source in lock {locked.get('source')}, "
                f"in the installation {wanted} — lock again: package-sdk lock"
            )
        commit = str(locked["commit"])
        if not _SHA.match(commit):
            raise PackageError(f"lock: commit of package {key!r} is not a SHA-1: {commit!r}")
        if self.fetch or not self.cache.has(url, commit):
            try:
                with self.cache.locked(url):
                    self.cache.fetch(url)
            except PackageError as error:
                if not self.cache.has(url, commit):
                    raise PackageError(
                        f"package {key!r}: source {url} is unavailable and commit {commit[:12]} "
                        f"from lock is not in the cache — {error}"
                    ) from error
                self.log(
                    f"   ! {key}: source {url} is unavailable — content {commit[:12]} "
                    "from the cache"
                )
            else:
                if self.fetch:
                    with self.cache.locked(url):
                        current = self.cache.resolve(url, ref)
                    if current != commit:
                        raise PackageError(
                            f"source_ref_moved: package {key!r}: {ref} in {url} points to "
                            f"{current[:12]}, but lock has {commit[:12]}: content under the same "
                            "tag was replaced. Check the source and lock again "
                            "(package-sdk lock) if the new content is accepted"
                        )
        if not self.cache.has(url, commit):
            raise PackageError(f"package {key!r}: commit {commit[:12]} from lock is not in {url}")
        expected = str(locked.get("contentHash"))
        # mtime выгрузки — «в работе»: prune не трогает её, пока установка её читает
        directory = touch(self.cache.materialize(url, commit, subdir, expected))
        if not (directory / "package.yaml").exists():
            raise PackageError(
                f"package {key!r}: no package.yaml in {url} at {commit[:12]}"
                + (f" in {subdir}" if subdir else "")
            )
        actual = install_hash(directory)
        if actual != expected:
            raise PackageError(
                f"content_mismatch: package {key!r}: content of {url} at {commit[:12]} "
                f"({actual[:19]}…) does not match lock ({str(expected)[:19]}…)"
            )
        self.commits[key] = commit
        return directory


def load(
    install: Path,
    *,
    strict: bool,
    cache: GitCache | None = None,
    log: Callable[[str], None] = warn,
) -> Sources:
    """Установка по зафиксированным источникам.

    strict — для плана и применения: источники git сверяются с сетью (переставленный тег —
    отказ), пакеты по пути и из каталога установки — с lock, если он есть. Без strict (check,
    test) содержимое git берётся из кэша по коммиту lock, локальные пакеты не сверяются."""
    if _has_git(install):
        validate_installation(install)
    document = read_lock(install)
    entries = {str(e["key"]): e for e in (document or {}).get("packages") or []}
    git = _GitSources(entries, cache or GitCache(), fetch=strict, log=log)
    installation = load_installation(install, git=git)
    base = install.parent
    locked: list[Locked] = []
    problems: list[str] = []
    for package in installation.packages:
        source = _source_of(package, base)
        current = Locked(
            key=package.key,
            version=_version(package),
            source=source,
            content_hash=install_hash(package.path),
            commit=git.commits.get(package.key),
        )
        locked.append(current)
        entry = entries.get(package.key)
        if document is None or not strict:
            continue
        if entry is None:
            problems.append(f"lock_stale: package {package.key!r} is not in lock")
        elif entry.get("source") != source:
            problems.append(
                f"lock_stale: package {package.key!r}: source in lock {entry.get('source')}, "
                f"in the installation {source}"
            )
        elif entry.get("contentHash") != current.content_hash:
            problems.append(
                f"content_mismatch: package {package.key!r} ({_rel(package.path)}) changed after "
                "locking"
            )
        elif str(entry.get("version")) != current.version:
            problems.append(
                f"lock_stale: package {package.key!r}: version in lock {entry.get('version')}, in "
                f"the manifest {current.version}"
            )
    if document is not None and strict:
        extra = sorted(set(entries) - {p.key for p in installation.packages})
        if extra:
            problems.append(
                f"lock_stale: lock has packages not in the installation: {', '.join(extra)}"
            )
    if problems:
        raise PackageError(
            f"{_rel(lock_path(install))} does not match the installation — lock again "
            "(package-sdk lock) if the changes are accepted:\n  " + "\n  ".join(problems)
        )
    return Sources(
        installation=installation,
        locked=locked,
        lock_file=lock_path(install) if document is not None else None,
    )


def lock(
    install: Path, *, cache: GitCache | None = None, log: Callable[[str], None] = print
) -> dict[str, Any]:
    """package-sdk lock: зафиксировать источники установки — коммит тега и хэш содержимого
    каждого пакета — в packages.lock рядом с файлом установки.

    Содержимое коммита каждый раз выгружается заново из git: выгрузке в кэше lock не
    доверяет, иначе правленый кэш зафиксировался бы как содержимое тега."""
    cache = cache or GitCache()
    validate_installation(install)
    previous = read_lock(install) if lock_path(install).exists() else None
    before = {str(e["key"]): e for e in (previous or {}).get("packages") or []}
    resolved: dict[str, str] = {}
    extracted: dict[str, str] = {}

    def git(entry: dict[str, Any]) -> Path:
        key, url, ref, subdir = _git_entry(entry)
        with cache.locked(url):
            cache.fetch(url)
            commit = cache.resolve(url, ref)
        directory = cache.materialize(url, commit, subdir)
        resolved[key] = commit
        # выгрузка названа хэшем своего содержимого — им и сверяется итог перед записью
        extracted[key] = "sha256:" + directory.name
        if not (directory / "package.yaml").exists():
            raise PackageError(
                f"package {key!r}: no package.yaml in {url} at {commit[:12]}"
                + (f" in {subdir}" if subdir else "")
            )
        old = before.get(key)
        if (
            old
            and old.get("commit") not in (None, commit)
            and old.get("source", {}).get("ref") == str(ref)
        ):
            log(
                f"   ! {key}: {ref} now points to {commit[:12]} (previous lock had "
                f"{str(old['commit'])[:12]}) — the tag was moved, check the source"
            )
        return directory

    installation = load_installation(install, git=git)
    base = install.parent
    entries = []
    for package in installation.packages:
        content_hash = install_hash(package.path)
        wanted = extracted.get(package.key)
        if wanted is not None and (
            content_hash != wanted or not (package.path / "package.yaml").is_file()
        ):
            # выгрузку удалили или подменили, пока lock её читал (например, cache prune):
            # такой хэш зафиксировал бы не содержимое тега
            raise PackageError(
                f"lock not written: checkout of package {package.key!r} changed while "
                f"locking ({content_hash[:19]}… instead of {wanted[:19]}…) — run "
                "package-sdk lock again"
            )
        entries.append(
            Locked(
                key=package.key,
                version=_version(package),
                source=_source_of(package, base),
                content_hash=content_hash,
                commit=resolved.get(package.key),
            ).entry()
        )
    document: dict[str, Any] = {"format": LOCK_FORMAT}
    if installation.key:
        document["installation"] = installation.key
    document["packages"] = entries
    problems = schema_module.errors(schema_module.LOCK, document)
    if problems:
        raise PackageError(f"lock not written: not {LOCK_FORMAT}: {problems[0]}")
    path = lock_path(install)
    _write_atomically(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    for entry in entries:
        where = entry["source"].get("git") or entry["source"].get("path")
        commit = f" {entry['commit'][:12]}" if entry.get("commit") else ""
        log(f"   {entry['key']} {entry['version']}: {where}{commit} {entry['contentHash'][:19]}…")
    log(f"written {_rel(path)}")
    return document


def _write_atomically(path: Path, text: str) -> None:
    handle, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _git_entry(entry: dict[str, Any]) -> tuple[str, str, str, str | None]:
    """Ключ, адрес, тег и подкаталог источника git — проверенные до любого вызова git."""
    subdir = entry.get("path")
    return (
        str(entry["key"]),
        check_url(str(entry["git"])),
        check_ref(str(entry["ref"])),
        check_subdir(str(subdir)) if subdir else None,
    )


def _has_git(install: Path) -> bool:
    document = _read_yaml(install)
    spec = document.get("spec") if isinstance(document, dict) else None
    packages = spec.get("packages") if isinstance(spec, dict) else None
    return isinstance(packages, list) and any(
        isinstance(entry, dict) and "git" in entry for entry in packages
    )


def validate_installation(install: Path) -> None:
    """Файл установки по схеме формата — до любого обращения к git: источник, который схема
    не принимает (локальный путь, file://, ref или путь-опция), до git не доходит."""
    document = _read_yaml(install)
    problems = schema_module.errors(schema_module.OBJECT, document)
    if problems:
        raise PackageError(
            f"{_rel(install)}: the installation does not match the format schema:\n  "
            + "\n  ".join(problems[:5])
        )


# --- уборка кэша (package-sdk cache prune) --------------------------------------------------

# Каталоги, в которых lock-файлов установки не ищем: служебные и чужие деревья.
_SKIP_DIRS = frozenset({"node_modules", "venv", "__pycache__", "site-packages"})


# Глубина поиска lock-файлов под каталогом: установки лежат у корня проекта, а не в недрах.
FIND_LOCKS_DEPTH = 6


def find_locks(root: Path, *, depth: int = FIND_LOCKS_DEPTH) -> list[Path]:
    """packages.lock под root не глубже depth уровней, без скрытых каталогов и чужих
    деревьев (node_modules…). Из домашнего каталога и корня файловой системы не ищет:
    это обход всего диска, а не проекта."""
    start = root.resolve()
    if start in (Path.home().resolve(), Path(start.anchor)):
        raise PackageError(
            f"lock files are not searched in {start}: it is not a project directory — run prune in "
            "the installations directory or name their lock files (--lock)"
        )
    found: list[Path] = []
    for current, dirs, files in os.walk(start):
        level = len(Path(current).relative_to(start).parts)
        dirs[:] = (
            []
            if level >= depth
            else sorted(d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS)
        )
        if LOCK_NAME in files:
            found.append(Path(current) / LOCK_NAME)
    return sorted(found)


@dataclass
class Pruned:
    """Итог уборки: какие lock-файлы учтены и что удалено."""

    locks: list[Path]
    removed: list[Path] = field(default_factory=list)
    kept: int = 0
    # без ссылок, но моложе STAGING_MAX_AGE: их сейчас может читать lock или план
    recent: int = 0


def prune(
    locks: Iterable[Path] | None,
    *,
    cache: GitCache | None = None,
    log: Callable[[str], None] = print,
) -> Pruned:
    """package-sdk cache prune: убрать из кэша git выгрузки, на которые не ссылается ни один
    из lock-файлов; ``locks=None`` (``--all``) — убрать всё: и выгрузки, и зеркала.

    Выгрузка, на которую ссылается lock, — ``checkouts/<коммит>/<путь>/<contentHash>`` его
    записи git; прочие выгрузки удаляются, зеркала (``repo.git``) без ``--all`` остаются —
    выгрузка из зеркала сети не требует. Каждый каталог источника чистится под его
    блокировкой, так что выгрузку, которую сейчас пишет другой процесс, уборка не трогает;
    читают же выгрузки без блокировки, поэтому готовая выгрузка моложе STAGING_MAX_AGE
    (её mtime обновляет каждое обращение) не удаляется и без ссылки — её может читать
    параллельный lock или план. Символические ссылки в кэше не удаляются и не обходятся.
    Lock-файл, которого нет или который не той формы, — отказ до любого удаления."""
    cache = cache or GitCache()
    keep: set[Path] | None = None
    used = list(locks) if locks is not None else []
    if locks is not None:
        keep = set()
        for path in used:
            if not path.is_file():
                raise PackageError(f"{path}: no lock file — nothing removed")
            document = read_lock_file(path)
            for entry in document.get("packages") or []:
                source = entry.get("source") or {}
                if "git" not in source or not entry.get("commit"):
                    continue
                url, commit = str(source["git"]), str(entry["commit"])
                subdir = str(source["path"]) if source.get("path") else None
                if not _URL.match(url) or not _SHA.match(commit):
                    continue  # такую запись план не примет, выгрузки у неё нет
                name = str(entry.get("contentHash", "")).removeprefix("sha256:")
                keep.add(cache._checkouts(url, commit, subdir) / name)
    result = Pruned(locks=used)
    if not cache.root.is_dir():
        return result
    for directory in sorted(cache.root.iterdir()):
        if directory.is_symlink() or not directory.is_dir():
            continue
        with cache._locked_dir(directory):
            result.removed += _prune_source(directory, keep, result)
    for path in result.removed:
        log(f"   removed {path}")
    return result


def _prune_source(directory: Path, keep: set[Path] | None, result: Pruned) -> list[Path]:
    """Уборка одного каталога источника (под его блокировкой)."""
    removed: list[Path] = []
    checkouts = directory / "checkouts"
    if keep is None:
        for name in ("checkouts", "repo.git"):
            if (directory / name).exists() and _owned(directory / name, directory):
                _remove(directory / name)
                removed.append(directory / name)
        return removed
    removed += GitCache.sweep(directory)
    if not _owned(checkouts, directory):
        return removed  # checkouts — ссылка наружу: не наше
    recent = time.time() - STAGING_MAX_AGE
    for ready in sorted(checkouts.glob("*/*/*")):
        if ready.name.startswith("."):
            continue  # временные — дело sweep: молодая может быть в работе без fcntl
        if not _owned(ready, directory):
            continue
        if ready in keep:
            result.kept += 1
            continue
        try:
            if ready.stat().st_mtime >= recent:
                result.recent += 1
                continue
        except FileNotFoundError:
            continue
        _remove(ready)
        removed.append(ready)
    for tree in sorted(checkouts.glob("*/*")):
        with contextlib.suppress(OSError):
            tree.rmdir()  # только пустой
    for commit in sorted(checkouts.glob("*")):
        with contextlib.suppress(OSError):
            commit.rmdir()
    return removed


def _remove(path: Path) -> None:
    """Сначала убрать с дороги (rename атомарен), потом удалить: читатель не видит
    полуудалённой выгрузки под её именем."""
    trash = path.parent / f".pruned-{uuid.uuid4().hex}"
    try:
        path.rename(trash)
    except FileNotFoundError:
        return
    shutil.rmtree(trash, ignore_errors=True)
