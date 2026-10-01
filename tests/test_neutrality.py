"""Neutrality guard: no business domain, product or vendor names in the SDK itself.

The SDK serves any domain (constitution, article II). Examples live in `examples/`
and are not scanned; code, schema, scaffolding templates and the author plugin (its
skills, hook and manifests) are. The plugin's README names the assistant it plugs into
and is a project document, like the repository README.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Words that would signal a domain, a vendor or a model provider leaking into the SDK.
FORBIDDEN = re.compile(
    r"\b("
    r"tender\w*|procurement|invoice\w*|44-?fz|zakupki|"
    r"amocrm|bitrix\w*|asana|salesforce|hubspot|1c|onec|"
    r"yandex|sber\w*|tinkoff|t-bank|kontur|moysklad|megaplan|retailcrm|iiko|"
    r"avito|ozon|wildberries|mts|beeline|"
    r"openai|anthropic|claude|codex|gpt-?\d\w*|gemini|deepseek|"
    r"github|gitlab|slack|telegram"
    r")\b",
    re.IGNORECASE,
)
# Every text file of code, schema and scaffolding templates — whatever its suffix.
SCAN_ROOTS = ["src", "schema", "plugin"]
# Project documents inside the scanned roots.
NOT_SCANNED = {"README.md"}
BINARY_SUFFIXES = {".pyc", ".png", ".jpg", ".gif", ".ico", ".whl", ".zip"}


# Executor kinds are values of the core's closed list (TAI-ADR-0052 п.7): the schema
# mirrors them as quoted enum/const/params keys; the SDK code itself never names them.
WIRE_VALUES = re.compile(
    r'"(claude-code|codex)"|agent(?:Executors|WorkingCopies)/(claude-code|codex)\b'
)
# Where a CI runner looks for workflow files is a path convention, not a vendor the SDK
# depends on: `init` writes the package's CI file there (FR-005). Only the quoted
# directory name is allowed, not the word.
PATH_CONVENTIONS = re.compile(r'"\.github"')


def scan(root: Path) -> list[str]:
    found: list[str] = []
    for top in SCAN_ROOTS:
        for path in sorted((root / top).rglob("*")):
            if not path.is_file() or path.suffix in BINARY_SUFFIXES or "__pycache__" in path.parts:
                continue
            if top == "plugin" and path.name in NOT_SCANNED:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                checked = WIRE_VALUES.sub('""', line) if top == "schema" else line
                if FORBIDDEN.search(PATH_CONVENTIONS.sub('""', checked)):
                    found.append(f"{path.relative_to(root).as_posix()}:{number}: {line.strip()}")
    return found


def test_sdk_is_domain_and_vendor_neutral() -> None:
    assert scan(REPO) == []


def test_guard_catches_a_domain_word(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "leak.py").write_text("KIND = 'tender.notice'\n", encoding="utf-8")
    assert scan(tmp_path) == ["src/leak.py:1: KIND = 'tender.notice'"]


def test_guard_scans_templates_of_any_suffix(tmp_path: Path) -> None:
    (tmp_path / "src" / "templates").mkdir(parents=True)
    template = tmp_path / "src" / "templates" / "ci.tmpl"
    template.write_text("uses: yandex/action\n", encoding="utf-8")
    assert scan(tmp_path) == ["src/templates/ci.tmpl:1: uses: yandex/action"]


def test_guard_allows_only_the_ci_directory_convention(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ci.py").write_text(
        'path = root / ".github" / "workflows"\nHOST = "github.com"\n', encoding="utf-8"
    )
    assert scan(tmp_path) == ['src/ci.py:2: HOST = "github.com"']


def test_guard_scans_plugin_skills(tmp_path: Path) -> None:
    skill = tmp_path / "plugin" / "author" / "skills" / "x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("process: invoice-payment\n", encoding="utf-8")
    (skill.parents[2] / "README.md").write_text("Install it in Claude Code\n", encoding="utf-8")
    assert scan(tmp_path) == ["plugin/author/skills/x/SKILL.md:1: process: invoice-payment"]
