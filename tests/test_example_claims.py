"""Сквозной пример «претензии клиентов» (examples/claims, S027: FR-035, SC-004).

Что проверяется без стенда:

- пакет примера проходит ``check`` кодом ядра и всю пирамиду ``package-sdk test`` в
  песочнице — сценарии правил и типов задач при ``PACKAGE_SDK_SANDBOX_DATABASE_URL``
  (job CI ``example-sandbox``), без базы — всё, кроме них;
- демонстрационный источник обращений и клиент интеграции говорят на одном API:
  обращение, изменения после курсора, идемпотентный ответ, повторное открытие;
- подделка модели стенда отвечает по схеме ``claims.classify@1``;
- страж нейтральности ядра (``tests/unit/test_process_neutrality.py`` control-plane):
  слов примера нет в файлах языка процессов, которые он охраняет (SC-004).

Стенд из compose открытой поставки — job ``stand`` (examples/claims/stand/e2e.py).
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from package_sdk import cli, model, sandbox, testing

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "examples" / "claims"
PACKAGE = EXAMPLE / "claims"
INTEGRATION = PACKAGE / "integration" / "src"
CORE = REPO.parent / "control-plane"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def integration() -> Iterator[None]:
    sys.path.insert(0, str(INTEGRATION))
    try:
        yield
    finally:
        sys.path.remove(str(INTEGRATION))
        for name in [n for n in sys.modules if n.split(".")[0] == "claims_helpdesk"]:
            del sys.modules[name]


def _documents() -> list[dict[str, Any]]:
    return [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(PACKAGE.rglob("*.yaml"))
        if "tests" not in path.relative_to(PACKAGE).parts
    ]


# --- пакет ---------------------------------------------------------------------------------


def test_the_example_is_a_package_of_every_kind_the_plan_names() -> None:
    kinds = {(d["kind"], d["key"]) for d in _documents()}
    assert {
        ("Package", "claims"),
        ("KnowledgePack", "claims"),
        ("Process", "claim"),
        ("WorkRule", "claim-reopened"),
        ("NotificationRule", "claim-reply-approval"),
        ("Skill", "claims.classify"),
        ("Skill", "helpdesk.reply"),
        ("Agent", "helpdesk-observer"),
        ("Agent", "claims-skills"),
        ("Agent", "claims-process"),
    } <= kinds
    process = next(d for d in _documents() if d["kind"] == "Process")
    assert process["spec"]["start"]["on"] == {"observation": "helpdesk.ticket_created"}
    images = {
        d["key"]: d["spec"]["executor"].get("image")
        for d in _documents()
        if d["kind"] == "Agent" and d["spec"].get("placement") != "none"
    }
    assert images == {
        "helpdesk-observer": "registry.example.com/claims/helpdesk-observer:0.1.0",
        "claims-skills": "registry.example.com/claims/claims-skills:0.1.0",
    }


def test_the_example_is_written_in_english() -> None:
    cyrillic = re.compile("[а-яА-ЯёЁ]")
    found = [
        f"{path.relative_to(EXAMPLE)}:{number}"
        for path in sorted(EXAMPLE.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if cyrillic.search(line)
    ]
    assert found == []


def test_the_readme_of_the_example_is_what_docs_generates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Раздел README примера (examples/claims/claims) сгенерирован package-sdk docs: если
    генератор поменяется, а пример нет, docs --check краснеет здесь, а не у автора, который
    скопировал пример. Чинится package-sdk docs --write examples/claims/claims."""
    assert cli.main(["docs", str(PACKAGE), "--check"]) == 0, capsys.readouterr().out


def test_the_pyramid_of_the_example_is_green_in_the_sandbox() -> None:
    """Без базы песочницы сценарии правил и типов задач не исполняются, и прогон честно
    красный; с базой (job example-sandbox) — зелёный целиком."""
    pytest.importorskip("control_plane", reason="сценарии — кодом ядра (extra sandbox)")
    pytest.importorskip("skill_sdk", reason="контракты скиллов — skill-sdk (extra skills)")
    installation = model.resolve_targets([str(PACKAGE)])

    database = sandbox.database_url(None)  # PACKAGE_SDK_SANDBOX_DATABASE_URL, как у CLI
    report = testing.Pyramid(installation, ["claims"], {}, database=database).execute()

    stages = {stage["stage"]: stage for stage in report["stages"]}
    assert [stages[s]["status"] for s in ("check", "skills", "integration")] == ["passed"] * 3
    coverage = report["coverage"]
    assert coverage["untested"] == [], coverage["untested"]
    assert (
        coverage["totals"]["processes"]["elements"]["covered"]
        == (coverage["totals"]["processes"]["elements"]["total"])
    )
    if database is None:
        assert stages["scenarios"]["status"] == "failed"  # rules and task types: skipped
        return
    assert report["status"] == "passed", json.dumps(report, ensure_ascii=False, indent=1)
    (scenarios,) = stages["scenarios"]["packages"]
    tests = scenarios["report"]["tests"]
    assert len(tests) >= 12 and {t["status"] for t in tests} == {"passed"}
    assert {t["subject"] for t in tests} == {"process", "rule", "taskType"}


# --- демонстрационный источник и клиент интеграции -----------------------------------------


@pytest.fixture
def demo_helpdesk(tmp_path: Path) -> Iterator[str]:
    demo = _load("demo_helpdesk", EXAMPLE / "helpdesk" / "demo_helpdesk.py")
    httpd = demo.server("127.0.0.1", 0, tmp_path, "test-token")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _file(base_url: str, **body: Any) -> dict[str, Any]:
    request = urllib.request.Request(
        base_url + "/tickets",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        answer: dict[str, Any] = json.loads(response.read())
        return answer


def test_the_integration_client_speaks_the_api_of_the_demo_helpdesk(
    demo_helpdesk: str, integration: None
) -> None:
    from claims_helpdesk import helpdesk, observer

    ticket = _file(demo_helpdesk, subject="Broken", text="It broke.", customer={"id": "C-1"})
    changed = helpdesk.changed_tickets(demo_helpdesk, "test-token", since=0, limit=10)
    assert [(t["id"], t["version"], observer.kind_of(t)) for t in changed] == [
        (ticket["id"], 1, observer.CREATED)
    ]

    first = helpdesk.post_reply(
        demo_helpdesk, "test-token", ticket_id=ticket["id"], message="Sorry", close=True, key="k-1"
    )
    again = helpdesk.post_reply(
        demo_helpdesk, "test-token", ticket_id=ticket["id"], message="Sorry", close=True, key="k-1"
    )
    assert first == again and first["status"] == "closed"  # the same key — one reply

    reopen = urllib.request.Request(
        f"{demo_helpdesk}/tickets/{ticket['id']}:reopen",
        data=b'{"text": "Still broken"}',
        method="POST",
        headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
    )
    urllib.request.urlopen(reopen, timeout=10).close()
    changed = helpdesk.changed_tickets(
        demo_helpdesk, "test-token", since=changed[-1]["seq"], limit=10
    )
    assert [(t["version"], t["status"], observer.kind_of(t)) for t in changed] == [
        (3, "reopened", observer.REOPENED)
    ]
    assert [r["message"] for r in changed[0]["replies"]] == ["Sorry"]

    with pytest.raises(helpdesk.HelpdeskError) as refused:
        helpdesk.changed_tickets(demo_helpdesk, "wrong", since=0, limit=10)
    assert refused.value.status == 401 and not refused.value.retryable


def test_the_demo_helpdesk_refuses_bad_input_and_an_open_address_without_a_token(
    demo_helpdesk: str, tmp_path: Path
) -> None:
    import http.client
    import urllib.error

    headers = {"Authorization": "Bearer test-token"}
    for query in ("since=x", "since=-1", "limit=0"):
        request = urllib.request.Request(f"{demo_helpdesk}/tickets?{query}", headers=headers)
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request, timeout=10)
        assert refused.value.code == 400, query

    host, port = demo_helpdesk.removeprefix("http://").split(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=10)
    connection.putrequest("POST", "/tickets")
    connection.putheader("Authorization", "Bearer test-token")
    connection.putheader("Content-Length", "-5")
    connection.endheaders()
    assert connection.getresponse().status == 400
    connection.close()

    demo = sys.modules["demo_helpdesk"]
    with pytest.raises(ValueError, match="HELPDESK_TOKEN"):
        demo.server("0.0.0.0", 0, tmp_path, "")


def test_the_stub_model_answers_by_the_contract_of_the_classification() -> None:
    stub = _load("stub_model", EXAMPLE / "stand" / "stub_model.py")
    contract = yaml.safe_load((PACKAGE / "skills" / "claims.classify.yaml").read_text())
    schema = contract["spec"]["contract"]["outputs"]
    import jsonschema

    for text, expected in (
        ("The heater sparked", ("defect", "high")),
        ("The parcel came late", ("delivery", "medium")),
        ("I was charged twice", ("billing", "medium")),
        ("Hello", ("other", "medium")),
    ):
        answer = stub.classify(text)
        jsonschema.validate(answer, schema)
        assert (answer["category"], answer["severity"]) == expected


# --- ядро не знает примера (SC-004) -------------------------------------------------------


# Слова предметной области примера помимо имён его объектов. `claim` — слово и ядра
# (claim задачи исполнителем), поэтому предметными считаются составные имена примера.
DOMAIN_WORDS = {"refund", "refunds", "refunded", "helpdesk", "ticket", "tickets", "complaint"}


def _example_words() -> set[str]:
    """Ключи объектов примера, виды его наблюдений и имена скиллов — и слова домена."""
    words = set(DOMAIN_WORDS)
    for document in _documents():
        words.add(document["key"])
        words.update(re.findall(r"helpdesk\.[a-z_]+", json.dumps(document)))
    return words - {"claim", "claims"}


def test_the_guard_of_the_core_does_not_find_the_words_of_the_example() -> None:
    guard_path = CORE / "tests" / "unit" / "test_process_neutrality.py"
    if not guard_path.exists():
        pytest.skip("нет соседа ../control-plane: страж ядра сверяется в CI")
    guard = _load("core_process_neutrality", guard_path)
    words = sorted(_example_words(), key=len, reverse=True)
    pattern = re.compile(r"(?<![\w-])(" + "|".join(map(re.escape, words)) + r")(?![\w-])", re.I)
    found = [
        f"{path.name}:{number}: {match.group(0)!r}"
        for path in guard.GUARDED
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for match in pattern.finditer(line)
    ]
    assert found == [], "the core knows the example:\n" + "\n".join(found)
    # и сам страж ядра на своих словах зелёный
    for path in guard.GUARDED:
        guard.test_no_domain_word_in_the_process_language(path)
