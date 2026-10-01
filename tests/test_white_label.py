"""White-label guard: the product name stays out of code, schema and templates.

Sanctioned exceptions are wire contracts, not names in code: the package format
identifier `taimen.ai/v1` (`apiVersion`, TAI-ADR-0044), the `$id` base of the format
schemas `https://taimen.ai/schema/` and the CEL profile name `taimen/1` (CP-ADR-0075).
Project documents about the project itself (README, NOTICE, TRADEMARK, CONTRIBUTING,
SECURITY) are not scanned.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CODENAME = re.compile(r"taimen", re.IGNORECASE)
FORMAT_ID = re.compile(r"taimen\.ai/v1|https://taimen\.ai/schema/|\btaimen/1\b")

# Every text file of code, schema and scaffolding templates, plus the project file.
SCAN_ROOTS = ["src", "schema"]
EXTRA_FILES = ["pyproject.toml"]
BINARY_SUFFIXES = {".pyc", ".png", ".jpg", ".gif", ".ico", ".whl", ".zip"}
# This guard names the codename by necessity.
ALLOWED = {"tests/test_white_label.py"}


def candidates() -> list[Path]:
    paths = [REPO / name for name in EXTRA_FILES]
    for top in SCAN_ROOTS:
        paths += sorted((REPO / top).rglob("*"))
    return [
        path
        for path in paths
        if path.is_file() and path.suffix not in BINARY_SUFFIXES and "__pycache__" not in path.parts
    ]


def offenders() -> list[str]:
    found: list[str] = []
    for path in candidates():
        rel = path.relative_to(REPO).as_posix()
        if rel in ALLOWED:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(lines, 1):
            if CODENAME.search(FORMAT_ID.sub("", line)):
                found.append(f"{rel}:{number}: {line.strip()}")
    return found


def test_codename_absent_outside_format_id() -> None:
    assert offenders() == []
