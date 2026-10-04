"""Протокол наблюдателя на поддельном ядре (S018, plan Р8): публикация, состояние, сверка
ревизии, коды выхода, чужая точка входа, повтор без дублей, отсутствующий секрет."""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
from pathlib import Path
from typing import Any

import pytest

from package_sdk.connector import (
    CYCLE_FAILED,
    EXIT_MISCONFIGURED,
    EXIT_REVISION_CHANGED,
    EXIT_STOPPED,
    SECRET_MISSING,
    Document,
    Observation,
    ObserveContext,
    Snapshot,
    load_entrypoint,
    observer,
)
from package_sdk.connector import __main__ as entry
from package_sdk.connector.runtime import Runner, _agent_credentials
from package_sdk.connector.testing import FakeCore, agent_body, run_once

FEED = Path(__file__).parent / "fixtures" / "connector" / "sample-feed" / "integration" / "src"
ITEMS = [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}, {"id": 3, "title": "c"}]
SECRETS = {"sample-feed-token": "t0ken-value"}


@pytest.fixture
def feed(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.syspath_prepend(str(FEED))
    sys.modules.pop("sample_feed.observer", None)
    return load_entrypoint("sample_feed.observer:observe")


def _runner(function: Any, core: FakeCore, tmp_path: Path, secrets: dict[str, str]) -> Runner:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(exist_ok=True)
    for name, value in secrets.items():
        (secrets_dir / name).write_text(value, encoding="utf-8")
    return Runner(function, core, tmp_path / "data", secrets_dir, asyncio.run)


def test_cycle_publishes_and_moves_the_cursor(feed: Any) -> None:
    result = run_once(feed, config={"items": ITEMS, "pageSize": 2}, secrets=SECRETS)
    assert [o["dedup_key"] for o in result.observations] == ["sample-feed:1", "sample-feed:2"]
    first = result.observations[0]
    assert first["source"] == "sample-feed-observer"  # имя наблюдателя по умолчанию
    assert first["kind"] == "sample_feed.item_seen" and first["data"] == ITEMS[0]
    assert first["external_ref"] == {"system": "sample-feed", "id": "1"}
    assert first["workspace_id"] == "00000000-0000-4000-8000-000000000001"
    assert result.state == {"cursor": 2}


def test_publish_failure_keeps_the_state_and_the_retry_has_no_duplicates(
    feed: Any, tmp_path: Path
) -> None:
    core = FakeCore(agent_body(feed, config={"items": ITEMS, "pageSize": 2}))
    runner = _runner(feed, core, tmp_path, SECRETS)
    revision = runner.revision()
    assert revision is not None
    core.fail_after = 1  # первая запись проходит, вторая — нет
    assert runner.cycle(revision) == []
    assert [o["dedup_key"] for o in core.observations] == ["sample-feed:1"]
    assert runner._load().get("state") is None  # курсор не сдвинулся

    core.fail_after = None
    assert runner.cycle(revision) == [
        "sample_feed.item_seen:sample-feed:1",
        "sample_feed.item_seen:sample-feed:2",
    ]
    # ядро узнало повтор первой записи по (source, dedupKey): в журнале по одной
    assert [o["dedup_key"] for o in core.observations] == ["sample-feed:1", "sample-feed:2"]
    assert runner._load()["state"] == {"cursor": 2}
    runner.cycle(revision)
    assert [o["dedup_key"] for o in core.observations][-1] == "sample-feed:3"


def test_missing_secret_skips_the_cycle_and_reports_once_a_day(feed: Any, tmp_path: Path) -> None:
    core = FakeCore(agent_body(feed, config={"items": ITEMS}))
    runner = _runner(feed, core, tmp_path, {})
    runner.now = lambda: dt.datetime(2026, 9, 30, 10, tzinfo=dt.UTC)
    revision = runner.revision()
    assert revision is not None
    assert runner.cycle(revision) == []
    runner.cycle(revision)  # тот же день — повторного сообщения нет
    (report,) = core.observations
    assert report["kind"] == SECRET_MISSING
    assert report["data"] == {"secret": "sample-feed-token", "agent": "observer-under-test"}
    assert "t0ken" not in str(report)
    runner.now = lambda: dt.datetime(2026, 10, 1, 10, tzinfo=dt.UTC)
    runner.cycle(revision)
    assert [o["kind"] for o in core.observations] == [SECRET_MISSING, SECRET_MISSING]

    # секрет появился — наблюдения пошли, отметка о пропаже снята
    (tmp_path / "secrets" / "sample-feed-token").write_text("t", encoding="utf-8")
    runner.cycle(revision)
    assert core.observations[-1]["kind"] == "sample_feed.item_seen"
    assert not any(k.startswith("secret-missing") for k in runner._load().get("__connector__", {}))


def test_cycle_failure_is_reported_once_an_hour_and_does_not_stop(tmp_path: Path) -> None:
    calls: list[int] = []

    @observer(kind="broken", entrypoint="tests.test_connector:broken")
    def broken(ctx: ObserveContext) -> None:
        calls.append(1)
        ctx.state["cursor"] = 99
        raise RuntimeError("upstream said: token=secret-value")

    core = FakeCore(agent_body(broken))
    runner = _runner(broken, core, tmp_path, {})
    runner.now = lambda: dt.datetime(2026, 9, 30, 10, 5, tzinfo=dt.UTC)
    revision = runner.revision()
    assert revision is not None
    runner.cycle(revision)
    runner.cycle(revision)
    (report,) = core.observations
    assert (
        report["kind"] == CYCLE_FAILED
        and report["dedup_key"] == "broken:cycle-failed:2026-09-30T10"
    )
    assert report["data"]["error"] == "RuntimeError"
    assert "secret-value" not in str(report)  # текст исключения в ядро не уходит
    assert runner._load().get("state") is None
    runner.now = lambda: dt.datetime(2026, 9, 30, 11, 5, tzinfo=dt.UTC)
    runner.cycle(revision)
    assert len(core.observations) == 2 and len(calls) == 3


def test_snapshot_and_document(tmp_path: Path) -> None:
    @observer(kind="docs", entrypoint="tests.test_connector:docs")
    def docs(ctx: ObserveContext) -> None:
        ctx.snapshot(
            Snapshot(
                source="docs",
                snapshot_id="s1",
                pack="company@1",
                entities=[{"kind": "org_unit", "key": "u1"}],
                observed_at=dt.datetime(2026, 9, 30, tzinfo=dt.UTC),
            )
        )
        ctx.document(Document(type="file", name="a.txt", content=b"hello", media_type="text/plain"))

    result = run_once(docs)
    (snapshot,) = result.snapshots
    assert snapshot["snapshotId"] == "s1" and snapshot["pack"] == "company@1"
    assert snapshot["observedAt"] == "2026-09-30T00:00:00+00:00"
    assert snapshot["workspaceId"] == "00000000-0000-4000-8000-000000000001"
    (artifact,) = result.artifacts
    assert artifact["type"] == "file" and artifact["content"] == b"hello"


def test_snapshot_without_a_workspace_is_a_publish_failure(tmp_path: Path) -> None:
    @observer(kind="docs", entrypoint="tests.test_connector:nows")
    def nows(ctx: ObserveContext) -> None:
        ctx.snapshot(Snapshot(source="docs", snapshot_id="s1"))

    core = FakeCore(agent_body(nows, workspace_id=None))
    result = run_once(nows, core=core)
    assert result.snapshots == [] and result.published == []
    assert [o["kind"] for o in result.observations] == [CYCLE_FAILED]


def test_answers_of_the_core_are_usable_within_the_cycle() -> None:
    """id артефакта — в данные наблюдения, id наблюдения — в supersedes следующего."""

    @observer(kind="chain", entrypoint="tests.test_connector:chain")
    def chain(ctx: ObserveContext) -> None:
        link = ctx.document(
            Document(type="file", name="n.pdf", uri="https://x.example/n.pdf", idempotency_key="n")
        )
        first = ctx.emit(Observation(kind="x.notice", dedup_key="n:1", data={"file": link["id"]}))
        ctx.emit(Observation(kind="x.notice", dedup_key="n:2", supersedes=first["id"]))

    result = run_once(chain)
    (artifact,) = result.artifacts
    assert artifact["uri"] == "https://x.example/n.pdf" and artifact["idempotency_key"] == "n"
    first, second = result.observations
    assert first["data"] == {"file": artifact["id"]}
    assert second["supersedes"] == first["id"]
    assert result.published == ["document:file:n.pdf", "x.notice:n:1", "x.notice:n:2"]


def test_document_needs_exactly_one_of_content_and_uri() -> None:
    @observer(kind="bad", entrypoint="tests.test_connector:bad_doc")
    def bad_doc(ctx: ObserveContext) -> None:
        ctx.document(Document(type="file", name="x"))

    result = run_once(bad_doc)
    assert result.artifacts == [] and result.observations[0]["kind"] == CYCLE_FAILED


# --- ревизия и коды выхода ----------------------------------------------------------


def test_serve_restarts_on_a_new_revision(feed: Any, tmp_path: Path) -> None:
    core = FakeCore(agent_body(feed, config={"items": ITEMS}))
    runner = _runner(feed, core, tmp_path, SECRETS)

    def sleep(_seconds: float) -> None:
        core.agent = agent_body(feed, config={"items": ITEMS}, revision=2)

    assert runner.serve(sleep=sleep) == EXIT_REVISION_CHANGED
    assert core.observations  # цикл успел пройти


@pytest.mark.parametrize(("state", "status"), [("stopped", "active"), ("running", "retired")])
def test_serve_stops_when_the_agent_does(
    feed: Any, tmp_path: Path, state: str, status: str
) -> None:
    core = FakeCore(agent_body(feed, config={"items": ITEMS}))
    runner = _runner(feed, core, tmp_path, SECRETS)

    def sleep(_seconds: float) -> None:
        raise AssertionError("не должен спать")

    core.agent = agent_body(feed, state=state, status=status)
    assert runner.serve(sleep=sleep) == EXIT_STOPPED
    assert core.observations == []  # до первого цикла


def test_foreign_entrypoint_or_kind_is_misconfigured(feed: Any, tmp_path: Path) -> None:
    body = agent_body(feed)
    body["revision"]["spec"]["executor"]["params"]["entrypoint"] = "other.module:observe"
    runner = _runner(feed, FakeCore(body), tmp_path, SECRETS)
    assert runner.serve() == EXIT_MISCONFIGURED
    body = agent_body(feed)
    body["revision"]["spec"]["executor"]["kind"] = "skills"
    assert _runner(feed, FakeCore(body), tmp_path, SECRETS).serve() == EXIT_MISCONFIGURED
    assert _runner(feed, FakeCore(None), tmp_path, SECRETS).serve() == EXIT_MISCONFIGURED


def test_principal_lost_between_cycles_restarts(feed: Any, tmp_path: Path) -> None:
    core = FakeCore(agent_body(feed, config={"items": ITEMS}))
    runner = _runner(feed, core, tmp_path, SECRETS)

    def sleep(_seconds: float) -> None:
        core.agent = None

    assert runner.serve(sleep=sleep) == EXIT_REVISION_CHANGED


def test_load_entrypoint(feed: Any) -> None:
    assert feed.entrypoint == "sample_feed.observer:observe"
    with pytest.raises(ValueError, match="module not in the image"):
        load_entrypoint("no_such_module:observe")
    with pytest.raises(ValueError, match="not an observer"):
        load_entrypoint("sample_feed.observer:run")
    with pytest.raises(ValueError, match="module:function"):
        load_entrypoint("sample_feed.observer")


def test_single_entrypoint_finds_the_observer_from_the_revision(
    feed: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = FakeCore(agent_body(feed, config={"items": ITEMS}))
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "sample-feed-token").write_text("t", encoding="utf-8")
    monkeypatch.setenv("CONTROL_PLANE_SERVER", "https://core.example")
    monkeypatch.setenv("CONNECTOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONNECTOR_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setattr("package_sdk.connector.core.ClientCore", lambda _url: core)
    original = Runner.serve
    monkeypatch.setattr(Runner, "serve", lambda self: original(self, cycles=1))
    assert entry.main() == EXIT_STOPPED
    assert [o["dedup_key"] for o in core.observations] == ["sample-feed:1", "sample-feed:2"]

    body = agent_body(feed)
    body["revision"]["spec"]["executor"]["params"]["entrypoint"] = "sample_feed.observer:run"
    core.agent = body  # точка входа не помечена @observer — узел не перезапускает впустую
    assert entry.main() == EXIT_MISCONFIGURED


def test_agent_pat_goes_to_the_client_environment(tmp_path: Path) -> None:
    (tmp_path / "agent-pat").write_text("pat-value\n", encoding="utf-8")
    env = _agent_credentials({}, tmp_path)
    assert env["IAM_CREDENTIAL_MODE"] == "environment"
    assert env["IAM_PLATFORM_ACCESS_TOKEN"] == "pat-value"
    assert "control-plane:write" in env["CONTROL_PLANE_IAM_SCOPES"]
    assert _agent_credentials({"IAM_PLATFORM_ACCESS_TOKEN": "x"}, tmp_path) == {}
    assert _agent_credentials({}, tmp_path / "none") == {}


def test_observation_content_defaults_to_kind_and_key() -> None:
    @observer(kind="k", entrypoint="tests.test_connector:plain")
    def plain(ctx: ObserveContext) -> None:
        ctx.emit(Observation(kind="x.seen", dedup_key="x:1", observed_at="2026-09-30T00:00:00Z"))

    (item,) = run_once(plain).observations
    assert item["content"] == "x.seen: x:1" and item["observed_at"] == "2026-09-30T00:00:00Z"


def test_retry_after_a_failed_emit_does_not_duplicate_the_document(tmp_path: Path) -> None:
    """document прошёл, emit упал — повтор цикла не заводит второй артефакт: ключ
    идемпотентности по умолчанию выводится из наблюдателя, типа, имени и содержимого."""

    @observer(kind="docs", entrypoint="tests.test_connector:doc_then_note")
    def doc_then_note(ctx: ObserveContext) -> None:
        link = ctx.document(
            Document(type="file", name="a.txt", content=b"hello", media_type="text/plain")
        )
        ctx.emit(Observation(kind="x.seen", dedup_key="x:1", data={"file": link["id"]}))

    core = FakeCore(agent_body(doc_then_note))
    runner = _runner(doc_then_note, core, tmp_path, {})
    revision = runner.revision()
    assert revision is not None
    core.fail_after = 2  # upload и create_artifact проходят, remember — нет
    assert runner.cycle(revision) == []
    core.fail_after = None
    runner.cycle(revision)
    (artifact,) = core.artifacts
    assert artifact["idempotency_key"].startswith("doc:")
    # повтор прислал то же тело: contentRef первой загрузки, второй загрузки не было
    assert len(core.contents) == 1
    (note,) = core.observations
    assert note["data"] == {"file": artifact["id"]}
    # цикл прошёл — отложенных загрузок не осталось
    assert runner._load()["__connector__"]["uploads"] == {}
    # тот же документ в следующих циклах — без новой загрузки и без 409 от ядра
    runner.cycle(revision)
    runner.cycle(revision)
    assert len(core.contents) == 1 and len(core.artifacts) == 1
    assert not any(o["kind"] == CYCLE_FAILED for o in core.observations)


def test_long_document_name_keeps_the_key_within_the_core_limit() -> None:
    @observer(kind="docs", entrypoint="tests.test_connector:long_name")
    def long_name(ctx: ObserveContext) -> None:
        ctx.document(Document(type="file", name="n" * 300, uri="https://x.example/f"))

    result = run_once(long_name)
    (artifact,) = result.artifacts
    assert len(artifact["idempotency_key"]) == len("doc:") + 64


def test_explicit_key_over_the_limit_is_a_cycle_failure() -> None:
    @observer(kind="docs", entrypoint="tests.test_connector:long_key")
    def long_key(ctx: ObserveContext) -> None:
        ctx.document(Document(type="file", name="n", uri="u", idempotency_key="k" * 201))

    result = run_once(long_key)
    assert result.artifacts == [] and result.observations[0]["kind"] == CYCLE_FAILED


def test_fake_core_refuses_another_body_under_the_same_key() -> None:
    """FakeCore держит контракт ядра: другое тело под тем же ключом — 409."""
    from package_sdk.connector.testing import FakeCoreError

    core = FakeCore()
    asyncio.run(core.create_artifact(type="f", name="a", uri="u1", idempotency_key="k"))
    with pytest.raises(FakeCoreError) as error:
        asyncio.run(core.create_artifact(type="f", name="a", uri="u2", idempotency_key="k"))
    assert error.value.status == 409


def test_default_document_key_depends_on_content_and_uri() -> None:
    from package_sdk.connector.runtime import document_key

    a = document_key("s", Document(type="f", name="n", content=b"1", media_type="text/plain"))
    b = document_key("s", Document(type="f", name="n", content=b"2", media_type="text/plain"))
    c = document_key("s", Document(type="f", name="n", uri="https://x.example/n"))
    assert len({a, b, c}) == 3
    assert document_key("s", Document(type="f", name="n", uri="u", idempotency_key="k")) == "k"


class _Rejected(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


@pytest.mark.parametrize(
    ("status", "reported"), [(422, True), (403, True), (503, False), (429, False)]
)
def test_rejection_by_the_core_is_a_cycle_failure(
    tmp_path: Path, status: int, reported: bool
) -> None:
    """Отказ ядра по сути (4xx) — сбой цикла с cycle_failed; недоступность — тихий повтор."""

    @observer(kind="rej", entrypoint="tests.test_connector:rejected")
    def rejected(ctx: ObserveContext) -> None:
        ctx.emit(Observation(kind="x.seen", dedup_key="x:1"))

    class Refusing(FakeCore):
        async def remember(self, **kwargs: Any) -> Any:
            if kwargs["kind"] == "x.seen":
                raise _Rejected(status)
            return await super().remember(**kwargs)

    core = Refusing(agent_body(rejected))
    runner = _runner(rejected, core, tmp_path, {})
    revision = runner.revision()
    assert revision is not None
    assert runner.cycle(revision) == []
    assert [o["kind"] for o in core.observations] == ([CYCLE_FAILED] if reported else [])
    assert runner._load().get("state") is None


@observer(kind="docs", entrypoint="tests.test_connector:doc_and_note")
def doc_and_note(ctx: ObserveContext) -> None:
    link = ctx.document(
        Document(type="file", name="a.txt", content=b"hello", media_type="text/plain")
    )
    ctx.emit(Observation(kind="x.seen", dedup_key="x:1", data={"file": link["id"]}))


def test_expired_upload_is_uploaded_again(tmp_path: Path) -> None:
    """Б3: create не проходил дольше срока жизни загрузки — contentRef не переиспользуется,
    загрузка идёт заново, итог — один артефакт и доставленное наблюдение."""
    clock = {"now": dt.datetime(2026, 9, 30, 10, tzinfo=dt.UTC)}
    core = FakeCore(agent_body(doc_and_note))
    core.now = lambda: clock["now"]
    runner = _runner(doc_and_note, core, tmp_path, {})
    runner.now = lambda: clock["now"]
    revision = runner.revision()
    assert revision is not None
    core.fail_after = 1  # загрузка проходит, create — нет (сеть)
    assert runner.cycle(revision) == []
    clock["now"] += dt.timedelta(hours=30)  # загрузку за это время убрали
    core.fail_after = None
    runner.cycle(revision)
    (artifact,) = core.artifacts
    assert len(core.contents) == 2 and artifact["content"] == b"hello"
    assert [o["kind"] for o in core.observations] == ["x.seen"]


def test_swept_upload_is_retried_once_on_content_ref_not_found(tmp_path: Path) -> None:
    """Загрузку убрали раньше срока из файла состояния — 422 content_ref_not_found,
    одна повторная загрузка в том же цикле."""
    core = FakeCore(agent_body(doc_and_note))
    runner = _runner(doc_and_note, core, tmp_path, {})
    revision = runner.revision()
    assert revision is not None
    core.fail_after = 1
    runner.cycle(revision)
    core.fail_after = None
    core._expires.clear()  # ядро уже забыло загрузку, хотя её срок ещё не вышел
    runner.cycle(revision)
    assert len(core.artifacts) == 1 and len(core.contents) == 2
    assert [o["kind"] for o in core.observations] == ["x.seen"]


def test_two_agents_with_the_same_document_do_not_collide(tmp_path: Path) -> None:
    """Б4: ключ ядро ищет по (tenant, key) для всех principal — ключ привязан к агенту."""
    core = FakeCore()
    for index, principal in enumerate(("principal-a", "principal-b")):
        core.principal = principal
        core.agent = agent_body(doc_and_note, key=f"feed-{index}", principal_id=principal)
        (tmp_path / principal).mkdir()
        runner = _runner(doc_and_note, core, tmp_path / principal, {})
        revision = runner.revision()
        assert revision is not None
        runner.cycle(revision)
    assert len(core.artifacts) == 2
    assert len({a["idempotency_key"] for a in core.artifacts}) == 2
    assert not any(o["kind"] == CYCLE_FAILED for o in core.observations)


def test_lost_response_after_commit_gives_one_artifact(tmp_path: Path) -> None:
    core = FakeCore(agent_body(doc_and_note))
    runner = _runner(doc_and_note, core, tmp_path, {})
    revision = runner.revision()
    assert revision is not None
    core.lose_next_response = True
    assert runner.cycle(revision) == []  # ответ потерян — цикл повторится
    runner.cycle(revision)
    (artifact,) = core.artifacts
    (note,) = core.observations
    assert note["data"] == {"file": artifact["id"]}


def test_in_flight_idempotency_is_a_quiet_retry(tmp_path: Path) -> None:
    @observer(kind="fl", entrypoint="tests.test_connector:in_flight")
    def in_flight(ctx: ObserveContext) -> None:
        ctx.emit(Observation(kind="x.seen", dedup_key="x:1"))

    class Busy(FakeCore):
        async def remember(self, **kwargs: Any) -> Any:
            from package_sdk.connector.testing import FakeCoreError

            raise FakeCoreError(409, "idempotency_in_flight")

    core = Busy(agent_body(in_flight))
    runner = _runner(in_flight, core, tmp_path, {})
    revision = runner.revision()
    assert revision is not None
    assert runner.cycle(revision) == []
    assert core.observations == []  # ни cycle_failed, ни записи — тихий повтор


def test_document_key_covers_agent_workspace_and_metadata() -> None:
    from package_sdk.connector.runtime import document_key

    base = Document(type="f", name="n", uri="u", metadata={"a": 1})
    keys = {
        document_key("s", base, agent="a1", workspace="w1"),
        document_key("s", base, agent="a2", workspace="w1"),
        document_key("s", base, agent="a1", workspace="w2"),
        document_key(
            "s",
            Document(type="f", name="n", uri="u", metadata={"a": 2}),
            agent="a1",
            workspace="w1",
        ),
    }
    assert len(keys) == 4 and all(len(k) == 68 for k in keys)
    assert document_key("s", base, agent="a1", workspace="w1") == document_key(
        "s", Document(type="f", name="n", uri="u", metadata={"a": 1}), agent="a1", workspace="w1"
    )


def test_same_agent_key_under_two_principals_does_not_collide(tmp_path: Path) -> None:
    """Даже если ключ агента совпал, principal из /agents/me разводит ключи документа."""
    core = FakeCore()
    for principal in ("principal-a", "principal-b"):
        core.principal = principal
        core.agent = agent_body(doc_and_note, principal_id=principal)
        (tmp_path / principal).mkdir()
        runner = _runner(doc_and_note, core, tmp_path / principal, {})
        revision = runner.revision()
        assert revision is not None
        runner.cycle(revision)
    assert len(core.artifacts) == 2
    assert not any(o["kind"] == CYCLE_FAILED for o in core.observations)


GIT_PARAMS = {"repositories": [{"name": "core", "url": "https://git.example/core.git"}]}


@observer(kind="git-watch", entrypoint="tests.git_watch:observe", executor="git-connector")
def git_watch(ctx: ObserveContext) -> None:
    """Собственный вид исполнителя: описание — в params, рабочие файлы — в data_dir."""
    (ctx.data_dir / "clones").mkdir(parents=True, exist_ok=True)
    for repo in ctx.params["repositories"]:
        ctx.emit(Observation(kind="repo.seen", dedup_key=repo["name"], data=dict(repo)))


def test_own_executor_kind_runs_on_its_params(tmp_path: Path) -> None:
    result = run_once(git_watch, params=GIT_PARAMS, data_dir=tmp_path / "data")
    assert [o["data"]["url"] for o in result.observations] == ["https://git.example/core.git"]
    assert (tmp_path / "data" / "clones").is_dir()
    body = agent_body(git_watch, params=GIT_PARAMS)
    assert body["revision"]["spec"]["executor"] == {
        "kind": "git-connector",
        "params": {"intervalSeconds": 60, **GIT_PARAMS},
    }


def test_own_executor_kind_is_checked(tmp_path: Path) -> None:
    body = agent_body(git_watch, params=GIT_PARAMS)
    runner = _runner(git_watch, FakeCore(body), tmp_path, {})
    assert runner.verdict_at_start(runner.revision()) is None
    body["revision"]["spec"]["executor"]["kind"] = "observer"  # чужой вид
    assert _runner(git_watch, FakeCore(body), tmp_path, {}).serve() == EXIT_MISCONFIGURED
    body = agent_body(git_watch, params={**GIT_PARAMS, "entrypoint": "other:observe"})
    assert _runner(git_watch, FakeCore(body), tmp_path, {}).serve() == EXIT_MISCONFIGURED


def test_image_entrypoint_serves_a_kind_without_params_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = FakeCore(agent_body(git_watch, params=GIT_PARAMS))
    monkeypatch.setenv("CONTROL_PLANE_SERVER", "https://core.example")
    monkeypatch.setenv("CONNECTOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONNECTOR_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setattr("package_sdk.connector.core.ClientCore", lambda _url: core)
    monkeypatch.setattr(
        "package_sdk.connector.__main__.load_entrypoint",
        lambda name: git_watch if name == git_watch.entrypoint else load_entrypoint(name),
    )
    original = Runner.serve
    monkeypatch.setattr(Runner, "serve", lambda self: original(self, cycles=1))
    assert entry.main() == EXIT_MISCONFIGURED  # точки входа нет ни в params, ни в образе
    monkeypatch.setenv("CONNECTOR_ENTRYPOINT", git_watch.entrypoint)
    assert entry.main() == EXIT_STOPPED
    assert [o["dedup_key"] for o in core.observations] == ["core"]


def test_secret_file_is_a_path_and_a_missing_one_is_reported(tmp_path: Path) -> None:
    @observer(kind="git-watch", entrypoint="tests.git_files:observe", executor="git-connector")
    def uses_file(ctx: ObserveContext) -> None:
        path = ctx.secret_file("github-token")
        ctx.emit(Observation(kind="file.seen", dedup_key=path.name))

    result = run_once(uses_file, params=GIT_PARAMS, secrets={"github-token": "t"})
    assert [o["dedup_key"] for o in result.observations] == ["github-token"]
    missing = run_once(uses_file, params=GIT_PARAMS)
    assert [o["kind"] for o in missing.observations] == [SECRET_MISSING]


def test_interval_default_of_the_observer_applies_without_params(tmp_path: Path) -> None:
    @observer(
        kind="git-watch", entrypoint="tests.slow:observe", executor="git-connector", interval=300
    )
    def slow(ctx: ObserveContext) -> None:
        pass

    body = agent_body(slow, params=GIT_PARAMS)
    del body["revision"]["spec"]["executor"]["params"]["intervalSeconds"]
    slept: list[float] = []
    runner = _runner(slow, FakeCore(body), tmp_path, {})
    runner.serve(sleep=slept.append, cycles=2)
    assert slept == [300]
