#!/usr/bin/env python3
"""The guide's quickstart as a CI check (S032, SC-001).

The "Package in 10 minutes" page (`guide/docs/packages/quickstart.md` of the
superproject) is executed as is: commands come from its code blocks, files from its
YAML blocks, and everything runs in a clean directory with an isolated `HOME`, uv
directories and git configuration. No server is needed; the page marks steps that
need one as skipped.

Page markup is an HTML comment on the last non-blank line before a code block
(it is invisible in the built guide):

    <!-- quickstart: skip <reason> -->         the block is not executed (plan/apply);
    <!-- quickstart: file <path> -->           the block's content is written to a file
                                               relative to the current directory;
    <!-- quickstart: requires docker -->      the block runs only with Docker;
    <!-- quickstart: without-docker exit=<code> [output=<string>] -->
                                               without Docker the block runs as a
                                               separate `bash -e` and must exit
                                               with this code (and print the
                                               string).

Markup is a single line; markup spread over several lines and a code block inside
a multi-line HTML comment are extraction errors.

A `bash`/`sh`/`shell` block without markup is executed. Illustrations are only the
blocks from the explicit ILLUSTRATION list (`text` output and the like). Any other
block without markup — data (`yaml`), empty, `console`, `zsh`, `{.bash}` — is an
extraction error: a page step must not silently drop out of the check and the
fingerprint.

Substitutions: `<тег>` and `<тег ядра>` (the page's release-tag placeholders) are
replaced by the branch of the local mirrors, and `git clone https://…/<name>.git`
goes to the mirror of the neighbour at its path in the layout of the installation
(src/package_sdk/layout.json; `url.<mirror>.insteadOf`):
the code of this SDK commit and of the neighbouring clones is checked, not the
published release. An unknown substitution is an error in commands and files alike.

Interrupting a run (the limit, SIGTERM, Ctrl-C) takes down the whole bash session
and stops the database container if this run started it.

Pinning. The superproject revision is pinned in `ci/quickstart-pin.json`: the commit,
the page path and the plan fingerprint — the sha256 of the extracted steps (commands,
files, markup; without line numbers and prose). `run` executes the page at the pinned
commit; `pin check` compares the plan at the head of the superproject branch with the
pinned one and fails if the page's commands or files changed while the pin did not.
Editing prose does not break the check. Moving the pin is `pin update` and a green
`run` in the same SDK commit.

The public umbrella is a projection of snapshots with its own history: the pinned
commit of the private superproject is not there and never will be. If the commit is
unreachable in the clone, the page is checked by content: the page at the clone's head
(`run`) or at `--against` (`pin check`) is taken, and its plan must equal the pinned
one — then it is executed, otherwise the error is "the page changed — `pin update`".
If the page is not there either, that is `PageMissing`: `pin status` prints
`page=missing`, and CI skips the job with a notice instead of failing. Where the commit
is reachable, the behaviour is as before — by commit.

The pin always names the superproject's source page — the Russian one
(`guide/docs/packages/quickstart.md`; its English translation lies next to it as
`quickstart.en.md` and is not executed). The umbrella's guide is bilingual with the
English default: there `quickstart.md` is the translation (`<tag>` placeholders,
English comments in the commands) and the Russian projection of the source is
`quickstart.ru.md`. So the check by content takes `<page>.ru.md` when the clone has
it and the pinned path otherwise (a monolingual or shallow clone of the superproject
itself): the plan is compared with the same page it was pinned from.

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
# Where each component lies in the tree of the installation, by its stable name (TAI-ADR-0064).
LAYOUT_FILE = REPO / "src" / "package_sdk" / "layout.json"
PAGE_NAME = "quickstart.md"
# The Russian projection of a source page in the bilingual umbrella (mkdocs-static-i18n,
# docs_structure: suffix — English is the default, Russian is `<page>.ru.md`).
RU_SUFFIX = ".ru.md"

MARKER = re.compile(r"^\s*<!--\s*quickstart:\s*(?P<body>.*?)\s*-->\s*$")
MARKER_LOOSE = re.compile(r"<!--\s*quickstart\b")
MARKER_CONTINUED = re.compile(r"^\s*quickstart\s*:")
FENCE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})\s*(?P<info>[^`\s]*)[^`]*$")
SHELL = {"bash", "sh", "shell"}
DATA = {"yaml", "yml", "json", "toml"}
# Blocks the page shows rather than executes: command output and diagrams.
ILLUSTRATION = {"text", "txt", "output", "mermaid"}

# The branch of the neighbours' local mirrors that replaces the page's release tags.
# The keys are the page's own placeholders and stay as the page writes them.
TAG = "quickstart"
PLACEHOLDERS = {"<тег>": TAG, "<тег ядра>": TAG}
PLACEHOLDER = re.compile(r"<[^\s<>][^<>\n]*>")
CLONE = re.compile(
    r"\bgit\s+clone\b[^\n]*?\s(?P<url>https://\S+/(?P<name>[\w.-]+?)(?:\.git)?)(?=\s|$)"
)
DOCKER_NAME = re.compile(r"\bdocker\s+run\b[^\n]*?--name[ =](?P<name>[\w.-]+)")

# Environment that passes into the clean directory; the rest (tokens, VIRTUAL_ENV,
# the sandbox database address, PYTHONPATH) is cut off.
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
    """The page does not extract or the run failed."""


class PageMissing(QuickstartError):
    """The pinned commit is unreachable in the umbrella clone and the head has no page."""


@dataclass(frozen=True)
class Step:
    """A page step: shell commands or a file."""

    line: int
    kind: str  # "run" | "file" | "skip"
    text: str
    path: str | None = None
    reason: str | None = None
    requires_docker: bool = False
    without_docker_exit: int | None = None
    without_docker_output: str | None = None

    def canonical(self) -> dict[str, object]:
        """The step's meaning without the line number: the plan fingerprint is computed from it."""
        data: dict[str, object] = {"kind": self.kind, "text": self.text}
        # The skip reason is prose and is not part of the fingerprint.
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
        # Skipped blocks are part of the fingerprint too: their commands are not executed,
        # but editing them on the page must reach the SDK, as editing executed ones does.
        body = json.dumps(
            [step.canonical() for step in self.steps], ensure_ascii=False, sort_keys=True
        )
        return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()

    def clones(self) -> dict[str, str]:
        """Clone URL → repository name over all executed blocks."""
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


# --- extraction --------------------------------------------------------------------


def _blocks(text: str) -> Iterator[tuple[int, str, str, str | None, int | None]]:
    """(fence line, language, content, markup body, markup line)."""
    lines = text.splitlines()
    marker: tuple[str, int] | None = None
    comment: int | None = None  # first line of a multi-line HTML comment
    index = 0
    while index < len(lines):
        line = lines[index]
        if comment is not None:
            if MARKER_CONTINUED.match(line) or MARKER_LOOSE.search(line):
                raise QuickstartError(
                    f"{PAGE_NAME}:{index + 1}: quickstart markup in a multi-line comment "
                    f"(from line {comment}) — write it as one line <!-- quickstart: … -->"
                )
            if FENCE.match(line):
                raise QuickstartError(
                    f"{PAGE_NAME}:{index + 1}: a code block inside an HTML comment (from line "
                    f"{comment}) — the extractor does not execute it"
                )
            if "-->" in line:
                comment = None
            index += 1
            continue
        found = MARKER.match(line)
        if found:
            if marker is not None:
                raise QuickstartError(
                    f"{PAGE_NAME}:{marker[1]}: markup without a code block — another markup "
                    f"follows (line {index + 1})"
                )
            marker = (found["body"], index + 1)
            index += 1
            continue
        if MARKER_LOOSE.search(line):
            raise QuickstartError(
                f"{PAGE_NAME}:{index + 1}: quickstart markup must take the whole line: "
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
                raise QuickstartError(f"{PAGE_NAME}:{start}: the code block is not closed")
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
                f'{PAGE_NAME}:{marker[1]}: markup "{marker[0]}" is not right before a code '
                f"block (line {index + 1} is text)"
            )
        opened = line.rfind("<!--")
        if opened != -1 and "-->" not in line[opened:]:
            comment = index + 1
        index += 1
    if marker is not None:
        raise QuickstartError(
            f"{PAGE_NAME}:{marker[1]}: markup without a code block at the end of the page"
        )
    if comment is not None:
        raise QuickstartError(f"{PAGE_NAME}:{comment}: the HTML comment is not closed")


def _substitute(text: str, line: int) -> str:
    for found in PLACEHOLDER.findall(text):
        if found not in PLACEHOLDERS:
            raise QuickstartError(
                f"{PAGE_NAME}:{line}: unknown placeholder {found} — add it to "
                f"PLACEHOLDERS in ci/quickstart.py or remove it from the page"
            )
    for key, value in PLACEHOLDERS.items():
        text = text.replace(key, value)
    return text


def _file_path(value: str, line: int) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise QuickstartError(
            f'{PAGE_NAME}:{line}: file path "{value}" — relative only, without ".."'
        )
    return path.as_posix()


def _directive(body: str, marker_line: int, line: int, lang: str, content: str) -> Step:
    word, _, rest = body.partition(" ")
    rest = rest.strip()
    if word == "skip":
        if not rest:
            raise QuickstartError(f"{PAGE_NAME}:{marker_line}: skip without a reason")
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
            f'{PAGE_NAME}:{marker_line}: "{word}" is for shell blocks only, here "{lang}"'
        )
    if word == "requires":
        if rest != "docker":
            raise QuickstartError(f"{PAGE_NAME}:{marker_line}: requires knows only docker")
        return Step(line=line, kind="run", text=_substitute(content, line), requires_docker=True)
    if word == "without-docker":
        options: dict[str, str] = {}
        for token in shlex.split(rest):
            key, sep, value = token.partition("=")
            if not sep or key not in {"exit", "output"} or key in options:
                raise QuickstartError(
                    f"{PAGE_NAME}:{marker_line}: without-docker exit=<code> [output=<string>]"
                )
            options[key] = value
        if not options.get("exit", "").isdigit():
            raise QuickstartError(f"{PAGE_NAME}:{marker_line}: without-docker without exit=<code>")
        return Step(
            line=line,
            kind="run",
            text=_substitute(content, line),
            without_docker_exit=int(options["exit"]),
            without_docker_output=options.get("output") or None,
        )
    raise QuickstartError(f'{PAGE_NAME}:{marker_line}: unknown markup "{body}"')


def extract(text: str) -> Page:
    """The page's steps in order. A markup error is a QuickstartError with the line number."""
    page = Page()
    for line, lang, content, body, marker_line in _blocks(text):
        if body is not None and marker_line is not None:
            page.steps.append(_directive(body, marker_line, line, lang, content))
        elif lang in SHELL:
            page.steps.append(Step(line=line, kind="run", text=_substitute(content, line)))
        elif lang not in ILLUSTRATION:
            kind = f'block "{lang}"' if lang else "block without a language"
            raise QuickstartError(
                f"{PAGE_NAME}:{line}: {kind} without markup is neither executed nor an "
                f"illustration — a language from {sorted(SHELL)} or {sorted(ILLUSTRATION)}, or "
                f"<!-- quickstart: file <path> --> / <!-- quickstart: skip <reason> -->"
            )
    if not page.executed:
        raise QuickstartError(f"{PAGE_NAME}: the page has no executable steps")
    return page


# --- shell script ------------------------------------------------------------------


def _heredoc_tag(content: str) -> str:
    tag = "QUICKSTART_EOF"
    while tag in content:
        tag += "_"
    return tag


def render(page: Page, *, docker: bool) -> str:
    """One bash script: the directory and variables carry from block to block, as for a reader."""
    out = [
        "set -euo pipefail",
        "qs_block=0",
        "trap 'rc=$?; echo \"quickstart: failed in block quickstart.md:$qs_block"
        " (exit code $rc)\" >&2' ERR",
    ]
    containers: list[str] = []
    for step in page.steps:
        where = f"quickstart.md:{step.line}"
        out.append(f"qs_block={step.line}")
        if step.kind == "skip":
            out.append(f"echo {shlex.quote(f'== {where}: skipped — {step.reason}')}")
            continue
        if step.kind == "file":
            assert step.path is not None
            tag = _heredoc_tag(step.text)
            out.append(f'echo "== {where} [+${{SECONDS}}s]: file "{shlex.quote(step.path)}')
            parent = str(Path(step.path).parent)
            if parent != ".":
                out.append(f"mkdir -p {shlex.quote(parent)}")
            out.append(f"cat > {shlex.quote(step.path)} <<'{tag}'\n{step.text}{tag}")
            continue
        shown = "".join(f"$ {line}\n" for line in step.text.splitlines())
        if step.requires_docker and not docker:
            out.append(f"echo {shlex.quote(f'== {where}: skipped — needs Docker (--db none)')}")
            continue
        out.append(f'echo "== {where} [+${{SECONDS}}s]"')
        out.append(f"printf '%s' {shlex.quote(shown)}")
        if step.without_docker_exit is not None and not docker:
            expected = step.without_docker_exit
            tag = _heredoc_tag(step.text)
            # A separate bash process: inside `… && … || …` the subshell's set -e does not
            # apply and an intermediate failure in the block would go unnoticed.
            out += [
                'qs_step="$(mktemp)"',
                'qs_log="$(mktemp)"',
                f"cat > \"$qs_step\" <<'{tag}'\n{step.text}{tag}",
                'bash -eo pipefail "$qs_step" >"$qs_log" 2>&1 && qs_rc=0 || qs_rc=$?',
                'cat "$qs_log"',
                f'if [ "$qs_rc" -ne {expected} ]; then',
                f'  echo "quickstart: {where} without Docker expects exit code {expected},'
                ' got $qs_rc" >&2',
                "  exit 1",
                "fi",
            ]
            if step.without_docker_output:
                needle = shlex.quote(step.without_docker_output)
                missing = (
                    f"quickstart: {where} without Docker did not print "
                    f'"{step.without_docker_output}"'
                )
                out += [
                    f'if ! grep -qF -- {needle} "$qs_log"; then',
                    f"  echo {shlex.quote(missing)} >&2",
                    "  exit 1",
                    "fi",
                ]
            done = f"   as expected without Docker: exit code {expected}"
            out.append(f"echo {shlex.quote(done)}")
            continue
        if step.requires_docker:
            # This run starts the container, so it stops it too, even on a failure in the
            # middle of the block. No foreign container has the same name: run_page checked.
            containers += [match["name"] for match in DOCKER_NAME.finditer(step.text)]
            if containers:
                names = " ".join(shlex.quote(name) for name in containers)
                out.append(f"trap 'docker stop {names} >/dev/null 2>&1 || true' EXIT")
        out.append(step.text.rstrip("\n"))
    out.append('echo "== end of page [+${SECONDS}s]"')
    return "\n".join(out) + "\n"


# --- pinning -----------------------------------------------------------------------


def load_pin(path: Path = PIN_FILE) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    missing = {"ref", "path", "commit", "plan"} - set(data)
    if missing:
        raise QuickstartError(f"{path.name}: missing fields {sorted(missing)}")
    return {key: str(value) for key, value in data.items()}


def _git(*args: str, cwd: Path) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if done.returncode != 0:
        raise QuickstartError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def page_at(superproject: Path, rev: str, path: str) -> tuple[str, str]:
    """(full commit sha, the page text at it)."""
    commit = _git("rev-parse", "--verify", f"{rev}^{{commit}}", cwd=superproject).strip()
    return commit, _git("show", f"{commit}:{path}", cwd=superproject)


def _git_ok(*args: str, cwd: Path) -> bool:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)
    return done.returncode == 0


def commit_reachable(superproject: Path, commit: str) -> bool:
    """Whether the pinned commit is in the clone (the public umbrella projection lacks it)."""
    return bool(commit) and _git_ok("cat-file", "-e", f"{commit}^{{commit}}", cwd=superproject)


def page_exists(superproject: Path, rev: str, path: str) -> bool:
    return _git_ok("cat-file", "-e", f"{rev}:{path}", cwd=superproject)


def ru_projection(path: str) -> str:
    """`<page>.md` → `<page>.ru.md`: where the umbrella keeps the source (Russian) page."""
    if path.endswith(RU_SUFFIX) or not path.endswith(".md"):
        return path
    return path[: -len(".md")] + RU_SUFFIX


def content_path(superproject: Path, rev: str, path: str) -> str | None:
    """The page compared by content at `rev`: the Russian projection, else the pinned path.

    In the bilingual umbrella `path` is the English translation, and its plan never equals
    the one pinned from the Russian source; a monolingual clone has only `path`.
    """
    for candidate in (ru_projection(path), path):
        if page_exists(superproject, rev, candidate):
            return candidate
    return None


def pinned_page(superproject: Path, pin: dict[str, str], head: str = "HEAD") -> tuple[str, str]:
    """(commit, text) of the page `run` executes, checked against the pin.

    The pinned commit is reachable — the page at it. Unreachable (public umbrella) —
    the page at `head` if its plan equals the pinned one: a check by content (the
    umbrella's `<page>.ru.md` when it is there, see content_path).
    """
    by_commit = commit_reachable(superproject, pin["commit"])
    path = pin["path"] if by_commit else content_path(superproject, head, pin["path"])
    if path is None:
        raise PageMissing(
            f"page {pin['path']} is not published in the umbrella yet: it is not at {head} "
            f"(nor {ru_projection(pin['path'])}), and the pinned commit {pin['commit'][:12]} "
            f"is unreachable in the clone"
        )
    commit, text = page_at(superproject, pin["commit"] if by_commit else head, path)
    plan = extract(text).plan_hash()
    if plan != pin["plan"]:
        if by_commit:
            raise QuickstartError(
                f"the page plan at {commit[:12]} ({plan}) does not equal the pinned one "
                f"({pin['plan']}) — `pin update`"
            )
        raise QuickstartError(
            f"the page changed — `pin update`: the plan of {path} at {head} "
            f"({commit[:12]}, {plan}) does not equal the pinned one ({pin['plan']}); the pinned "
            f"commit {pin['commit'][:12]} is unreachable in the clone, checked by content"
        )
    return commit, text


def pin_status(superproject: Path, against: str = "HEAD", pin_path: Path = PIN_FILE) -> str:
    """key=value lines for CI: page=present|missing, mode=commit|content|missing."""
    pin = load_pin(pin_path)
    path: str | None = pin["path"]
    if commit_reachable(superproject, pin["commit"]):
        mode = "commit"
    else:
        path = content_path(superproject, against, pin["path"])
        mode = "content" if path is not None else "missing"
    page = "missing" if mode == "missing" else "present"
    return f"page={page}\nmode={mode}\npath={path or pin['path']}"


def pin_check(superproject: Path, against: str, pin_path: Path = PIN_FILE) -> str:
    pin = load_pin(pin_path)
    if not commit_reachable(superproject, pin["commit"]):
        head_commit, _ = pinned_page(superproject, pin, against)
        return (
            f"ok: the pinned commit {pin['commit'][:12]} is unreachable in the clone, the page "
            f"plan at {against} ({head_commit[:12]}) equals the pinned one — checked by content"
        )
    _, pinned_text = page_at(superproject, pin["commit"], pin["path"])
    pinned = extract(pinned_text)
    if pinned.plan_hash() != pin["plan"]:
        raise QuickstartError(
            f"pin {pin['commit'][:12]}: the page plan {pinned.plan_hash()} does not equal "
            f"the pinned {pin['plan']} — the extractor or the pin file was edited "
            f"without `pin update`"
        )
    head_commit, head_text = page_at(superproject, against, pin["path"])
    head = extract(head_text)
    if head.plan_hash() != pin["plan"]:
        diff = _plan_diff(pinned, head)
        raise QuickstartError(
            f"the commands or files of {pin['path']} changed after pinning "
            f"({pin['commit'][:12]} → {head_commit[:12]}) and the SDK was not updated:\n{diff}\n"
            f"Check the page with a run and move the pin: "
            f"python3 ci/quickstart.py pin update --superproject … --rev {against}"
        )
    note = "" if head_text == pinned_text else " (the page prose changed, the commands did not)"
    return f"ok: pin {pin['commit'][:12]} is up to date for {against} ({head_commit[:12]}){note}"


def _plan_diff(old: Page, new: Page) -> str:
    import difflib

    def lines(page: Page) -> list[str]:
        return render(page, docker=True).splitlines()

    return "\n".join(
        difflib.unified_diff(lines(old), lines(new), "pinned", "head", lineterm="", n=1)
    )


def pin_update(superproject: Path, rev: str, pin_path: Path = PIN_FILE) -> str:
    pin = load_pin(pin_path)
    commit, text = page_at(superproject, rev, pin["path"])
    pin["commit"] = commit
    pin["plan"] = extract(text).plan_hash()
    pin_path.write_text(json.dumps(pin, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return f"pinned: {commit} {pin['plan']}"


# --- run ---------------------------------------------------------------------------


def _mirror(source: Path, mirror: Path) -> None:
    """A bare mirror with branch TAG at the neighbour's HEAD (from a shallow CI clone too)."""
    if not (source / ".git").exists():
        raise QuickstartError(f"neighbour {source} is not a git clone")
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
        # The Docker Desktop context lives in ~/.docker — without it the CLI misses the daemon.
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
    """Take down the run's process group and its containers."""
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
    # The group leader may have exited before its children: kill the rest.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    for name in containers:
        if _container_exists(name):
            subprocess.run(["docker", "stop", name], capture_output=True, check=False)


def _terminate(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def layout() -> dict[str, str]:
    """Component name → its path in the tree of the installation (the SDK's layout manifest)."""
    components = json.loads(LAYOUT_FILE.read_text(encoding="utf-8"))["components"]
    return {str(name): str(path) for name, path in components.items()}


def installation_root() -> Path:
    """The root of the installation this clone of package-sdk lies in, by the layout."""
    depth = len(Path(layout().get("package-sdk", REPO.name)).parts)
    return REPO.parents[depth - 1]


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
    """Execute the page in a clean directory. Returns the duration in seconds."""
    docker = db == "docker"
    names = page.docker_names() if docker else []
    if docker:
        if not _docker_available():
            raise QuickstartError("--db docker: Docker is not available; without it use --db none")
        busy = [name for name in names if _container_exists(name)]
        if busy:
            raise QuickstartError(
                f"container {', '.join(busy)} already exists — the page creates it itself; "
                f"stop the other run or wait for it"
            )
    work = Path(tempfile.mkdtemp(prefix="quickstart-", dir=workdir)).resolve()
    started = time.monotonic()
    try:
        mirrors = work / "mirrors"
        mirrors.mkdir()
        config: list[str] = []
        for url, name in sorted(page.clones().items()):
            if name == REPO.name or name == "package-sdk":
                source = REPO
            else:
                source = neighbours.joinpath(*Path(layout().get(name, name)).parts)
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
        print(f"quickstart: directory {work}, database: {db}, limit {timeout} s", flush=True)
        process = subprocess.Popen(["bash", str(script)], cwd=root, env=env, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except BaseException as error:
            # The limit, SIGTERM (main turns it into SystemExit) or Ctrl-C: take down the
            # whole bash session and the database container this run started.
            _stop(process, names)
            if isinstance(error, subprocess.TimeoutExpired):
                raise QuickstartError(
                    f"the quickstart did not fit in {timeout} s — interrupted"
                ) from None
            raise
        elapsed = time.monotonic() - started
        if code != 0:
            keep = True
            raise QuickstartError(f"the quickstart failed (exit code {code}) after {elapsed:.0f} s")
        return elapsed
    finally:
        if keep:
            print(f"quickstart: the run directory is kept — {work}", file=sys.stderr)
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
        group.add_argument("--page", help="page file (no pin)")
        group.add_argument("--superproject", help="superproject clone: the page at the pin")
        p.add_argument("--rev", help="superproject revision instead of the pinned one")

    extract_cmd = sub.add_parser("extract", help="print the script without executing anything")
    source(extract_cmd)
    extract_cmd.add_argument("--db", choices=["docker", "none"], default="docker")

    run_cmd = sub.add_parser("run", help="execute the page in a clean directory")
    source(run_cmd)
    run_cmd.add_argument(
        "--db",
        choices=["docker", "none"],
        default="docker",
        help="docker: the sandbox database as the page's container; none: without Docker "
        "(requires docker blocks are skipped, without-docker blocks expect their exit code)",
    )
    run_cmd.add_argument(
        "--neighbours",
        default=str(installation_root()),
        help="root of the installation with the neighbouring clones, laid out as in "
        "src/package_sdk/layout.json",
    )
    run_cmd.add_argument("--timeout", type=int, default=600, help="limit, seconds (SC-001)")
    run_cmd.add_argument("--workdir", help="where to create the run directory")
    run_cmd.add_argument("--uv-cache", help="uv cache (empty by default, as in a clean setup)")
    run_cmd.add_argument("--keep", action="store_true", help="do not remove the run directory")

    pin_cmd = sub.add_parser("pin", help="pinning of the superproject revision")
    pin_sub = pin_cmd.add_subparsers(dest="pin_command", required=True)
    check_cmd = pin_sub.add_parser("check", help="the page commands at the head = the pinned ones")
    check_cmd.add_argument("--superproject", required=True)
    check_cmd.add_argument("--against", default="HEAD", help="head revision (HEAD by default)")
    update_cmd = pin_sub.add_parser("update", help="pin a revision")
    update_cmd.add_argument("--superproject", required=True)
    update_cmd.add_argument("--rev", default="HEAD")
    status_cmd = pin_sub.add_parser(
        "status", help="whether the umbrella clone has the page: page=present|missing (for CI)"
    )
    status_cmd.add_argument("--superproject", required=True)
    status_cmd.add_argument("--against", default="HEAD", help="head revision (HEAD by default)")
    pin_sub.add_parser("show", help="pin fields as key=value lines")

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
            print(f"ok: quickstart in {elapsed:.0f} s (limit {args.timeout} s), db: {args.db}")
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
        print("quickstart: interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
