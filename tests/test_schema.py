"""The package format schema: valid documents, one $id base, the S003 extensions."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from package_sdk import schema

FIXTURES = Path(__file__).parent / "fixtures" / "schema"
NAMES = [p.name.removesuffix(".schema.json") for p in sorted(schema.schema_dir().glob("*.json"))]


def load(name: str) -> Any:
    return yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", NAMES)
def test_every_schema_is_valid_draft_2020_12(name: str) -> None:
    jsonschema.Draft202012Validator.check_schema(schema.load(name))


def test_one_id_base_for_relative_refs() -> None:
    bases = {schema.load(n)["$id"].rsplit("/", 1)[0] for n in NAMES}
    assert len(bases) == 1


@pytest.mark.parametrize(
    "fixture,name",
    [
        ("package-manifest.yaml", schema.OBJECT),
        ("installation-sources.yaml", schema.OBJECT),
        ("knowledge-pack.yaml", schema.OBJECT),
        ("agent-image.yaml", schema.OBJECT),
        ("process.test.yaml", schema.TEST),
        ("rule.test.yaml", schema.TEST),
        ("task-type.test.yaml", schema.TEST),
        ("plan.json", schema.PLAN),
        ("lock.json", schema.LOCK),
    ],
)
def test_valid_documents(fixture: str, name: str) -> None:
    assert schema.errors(name, load(fixture)) == []


@pytest.mark.parametrize(
    "fixture,name,fragment",
    [
        ("bad-variable-secret.yaml", schema.OBJECT, "secret"),
        ("bad-git-source-without-ref.yaml", schema.OBJECT, "packages/0"),
        ("bad-agent-image.yaml", schema.OBJECT, "image"),
        ("bad-rule-test-without-rule.yaml", schema.TEST, "'rule' is a required property"),
        ("bad-process-test-without-process.yaml", schema.TEST, "'process' is a required"),
    ],
)
def test_invalid_documents(fixture: str, name: str, fragment: str) -> None:
    found = schema.errors(name, load(fixture))
    assert found, fixture
    assert any(fragment in error for error in found), found


def test_plan_hash_is_required() -> None:
    plan = json.loads((FIXTURES / "plan.json").read_text(encoding="utf-8"))
    del plan["planHash"]
    assert schema.errors(schema.PLAN, plan)


GIT = {"git": "https://git.example.com/acme/pkg.git", "ref": "v1.0.0"}


@pytest.mark.parametrize(
    "source",
    [
        {},  # ни одной формы
        {"path": "packages/demo", "ref": "v1.0.0"},  # ref без git
        {**GIT, "subdir": "pkg"},  # прежнее имя подкаталога — теперь path, как в установке
        {"git": GIT["git"]},  # git без ref
    ],
)
def test_lock_source_is_exactly_one_form(source: dict[str, str]) -> None:
    lock = json.loads((FIXTURES / "lock.json").read_text(encoding="utf-8"))
    lock["packages"][0]["source"] = source
    assert schema.errors(schema.LOCK, lock)


def test_lock_git_source_names_its_subdirectory_path() -> None:
    lock = json.loads((FIXTURES / "lock.json").read_text(encoding="utf-8"))
    lock["packages"][1]["source"] = {**GIT, "path": "pkg"}
    assert schema.errors(schema.LOCK, lock) == []


# --- given теста правил и типов задач (CP-ADR-0074, амендмент 2026-09-30; TASK-001198) ---


def _rule_test(given: dict[str, Any]) -> dict[str, Any]:
    return {
        "subject": "rule",
        "rule": "r",
        "name": "n",
        "given": given,
        "steps": [{"expect": {"result": "matched"}}],
    }


@pytest.mark.parametrize(
    "given",
    [
        {"schedule": {}},
        {"schedule": {"at": "2026-10-01T09:00:00Z"}},
        {"event": {"type": "task.updated"}, "task": {"type": "coding-task", "title": "t"}},
        {"schedule": {}, "task": {"type": "x", "status": "todo", "customFields": {"a": 1}}},
    ],
)
def test_a_rule_test_may_start_from_a_schedule_and_a_task(given: dict[str, Any]) -> None:
    assert schema.errors(schema.TEST, _rule_test(given)) == []


@pytest.mark.parametrize(
    "given",
    [
        {"schedule": {}, "observation": {"kind": "x"}},  # один вход, не два
        {"schedule": {"every": "1d"}},
        {"schedule": {"at": 1}},
        {"event": {"type": "t"}, "task": {"title": "без типа"}},
        {"event": {"type": "t"}, "task": {"type": "Coding Task"}},
        {"event": {"type": "t"}, "task": {"type": "x", "owner": "y"}},
    ],
)
def test_a_rule_given_outside_the_schema_is_refused(given: dict[str, Any]) -> None:
    assert schema.errors(schema.TEST, _rule_test(given)) != []


def _task_type_test(artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "subject": "taskType",
        "taskType": "t",
        "name": "n",
        "given": {"artifacts": artifacts},
        "steps": [{"expect": {"status": {"category": "active"}}}],
    }


def test_a_given_artifact_may_carry_its_content() -> None:
    artifact = {"key": "k", "type": "doc", "content": "# Ok", "mediaType": "text/markdown"}
    assert schema.errors(schema.TEST, _task_type_test([artifact])) == []


@pytest.mark.parametrize(
    "artifacts",
    [
        [{"key": "k", "type": "doc", "content": "x"}],  # содержимое без mediaType
        [{"key": "k", "type": "doc", "mediaType": "text/plain"}],  # mediaType без содержимого
        [{"key": "k", "type": "doc", "content": "x", "mediaType": "not a media type"}],
        [{"type": "doc", "content": "x" * (1024 * 1024 + 1), "mediaType": "text/plain"}],
        [{"key": f"k{i}", "type": "doc"} for i in range(21)],
    ],
)
def test_given_artifacts_outside_the_schema_are_refused(artifacts: list[dict[str, Any]]) -> None:
    assert schema.errors(schema.TEST, _task_type_test(artifacts)) != []


def test_the_check_counts_the_content_in_utf8_bytes() -> None:
    """Схема считает символы; проверка пакета — байты UTF-8, как ядро (1 МиБ)."""
    from package_sdk.check import GIVEN_CONTENT_MAX_BYTES, _given_content_errors

    cyrillic = "я" * (GIVEN_CONTENT_MAX_BYTES // 2 + 1)  # символов — вдвое меньше предела
    fits = "я" * (GIVEN_CONTENT_MAX_BYTES // 2)
    test = _task_type_test(
        [
            {"type": "doc", "content": fits, "mediaType": "text/plain"},
            {"type": "doc", "content": cyrillic, "mediaType": "text/plain"},
        ]
    )
    assert schema.errors(schema.TEST, test) == []
    (error,) = _given_content_errors(test)
    assert error.startswith("given.artifacts[1].content: 1048578 байт")


# Описания и заголовки схем редактор показывает автору пакета как есть (по $schema): ссылки
# разработки — на ADR, задачи и этапы — живут в $comment соседнего узла, не в тексте.
_LEAKED = re.compile(r"\b(?:[A-Z]+-)?ADR-\d+|\b(?:FR|SC|TASK)-\d+|\b[SU]\d{3}\b")


def _texts(node: Any, path: str) -> list[tuple[str, str]]:
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("description", "title") and isinstance(value, str):
                found.append((f"{path}/{key}", value))
            elif key != "$comment":
                found += _texts(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += _texts(value, f"{path}/{index}")
    return found


@pytest.mark.parametrize("name", NAMES)
def test_descriptions_carry_no_development_references(name: str) -> None:
    leaked = [
        f"{where}: {match.group(0)}"
        for where, text in _texts(schema.load(name), name)
        if (match := _LEAKED.search(text))
    ]
    assert leaked == []


@pytest.mark.parametrize(
    "text",
    ["FR-005", "per SC-001", "TASK-000927", "CP-ADR-0073", "ADR-0012", "stage S003", "U012"],
)
def test_the_leak_pattern_catches_every_kind_of_reference(text: str) -> None:
    assert _LEAKED.search(text)


@pytest.mark.parametrize("text", ["UTF-8", "an S3 bucket", "SHA-256", "FRAME-1", "US001"])
def test_the_leak_pattern_spares_ordinary_words(text: str) -> None:
    assert not _LEAKED.search(text)
