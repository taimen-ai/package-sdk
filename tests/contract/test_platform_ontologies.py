"""Снимок онтологий памяти платформы в SDK совпадает с memory-service (S014).

check проверяет виды и связи процессов и против default@1; снимок лежит в пакете SDK,
источник — memory-service. В суперпроекте сабмодуль рядом — сверка; отдельно от него
тест пропускается."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from package_sdk import layout, manifest

SNAPSHOT = Path(manifest.__file__).parent / "platform_ontologies"
MEMORY = layout.neighbour("memory-service") / "src" / "platform_memory"


@pytest.mark.parametrize("name", ["default"])
def test_snapshot_matches_memory_service(name: str) -> None:
    source = MEMORY / "core" / "packs" / f"{name}.json"
    if not source.is_file():
        pytest.skip(f"no memory-service next to the SDK ({source}): not a superproject checkout")
    ours = json.loads((SNAPSHOT / f"{name}.json").read_text(encoding="utf-8"))
    assert ours == json.loads(source.read_text(encoding="utf-8"))


def test_platform_ontology_terms_are_known() -> None:
    terms = manifest._ontology_terms(("default", 1), manifest._platform_packs(), set())
    assert terms is not None
    kinds, relations = terms
    assert "legal_entity" in kinds and "mentions" in relations
