"""Токен перед каждым запросом: установка дольше жизни access token (TASK-001256).

На staging access token ядра и сервиса уведомлений живёт 300 с, а apply полной установки —
около 570 с: токен, полученный один раз, истекал посреди секции каталога (401
invalid_credentials). Здесь стенд — поддельное ядро за «привратником», который двигает часы
на каждом запросе и пускает только действующий токен; часы общие с поставщиком токена."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from package_sdk import commands, install
from package_sdk.apply import HttpError
from package_sdk.auth import Authorized, Bearer, error_code
from package_sdk.core import ProcessApi
from package_sdk.install.plan import Target
from package_sdk.model import PackageError
from tests.test_install import (  # noqa: F401  — фикстуры тестов установки
    ENV,
    SERVER,
    FakeCore,
    FakeNotificationService,
    core,
    notify,
    project,
)

TTL = 300.0  # как у access token staging
STEP = 20.0  # «длительность» одного запроса по подменённым часам


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Issuer:
    """IAM: обмен PAT на access token со сроком TTL; отзыв — до срока."""

    def __init__(self, clock: Clock, ttl: float = TTL) -> None:
        self.clock = clock
        self.ttl = ttl
        self.until: dict[str, float] = {}
        self.revoked: set[str] = set()

    def exchange(self) -> dict[str, Any]:
        token = f"tok-{len(self.until) + 1}"
        self.until[token] = self.clock() + self.ttl
        return {"accessToken": token, "expiresIn": int(self.ttl), "tokenType": "Bearer"}

    def accepts(self, token: str) -> bool:
        return token not in self.revoked and self.until.get(token, -1.0) > self.clock()


class Stand:
    """Привратник стенда: запрос «идёт» STEP секунд, затем токен сверяется с IAM.
    Недействительный — 401 с кодом сервиса; действующий — запрос уходит поддельному
    сервису с тем заголовком, которого тот ждёт."""

    def __init__(
        self,
        inner: Any,
        issuer: Issuer,
        *,
        code: str = "invalid_credentials",
        inner_auth: str = "Bearer t",
    ) -> None:
        self.inner = inner
        self.issuer = issuer
        self.code = code
        self.inner_auth = inner_auth
        self.seen: list[tuple[float, str, dict[str, str]]] = []
        self.rejected = 0

    def call(self, method: str, path: str, body: Any = None, headers: dict | None = None) -> dict:
        self.issuer.clock.now += STEP
        sent = dict(headers or {})
        token = sent.get("Authorization", "").removeprefix("Bearer ")
        self.seen.append((self.issuer.clock(), token, sent))
        if not self.issuer.accepts(token):
            self.rejected += 1
            raise HttpError(
                f"{method} {path}: HTTP 401", 401, {"error": {"code": self.code, "message": "x"}}
            )
        if self.inner is None:
            return {}
        result: dict = self.inner.call(
            method, path, body, {**sent, "Authorization": self.inner_auth}
        )
        return result


def _tokens(stand: Stand) -> list[str]:
    return list(dict.fromkeys(token for _at, token, _h in stand.seen))


def _credential(iam: Issuer, *, audience: str = "control-plane") -> Any:
    """IamCredential клиента ядра поверх поддельного IAM и его часов: сам кэширует токен и
    обменивает PAT заново, когда тот истекает."""
    httpx = pytest.importorskip("httpx")
    iam_module = pytest.importorskip("control_plane_client.iam")

    def exchange(request: Any) -> Any:
        sent = json.loads(request.content)
        assert sent["token"] == "pat" and sent["audience"] == audience, sent
        return httpx.Response(200, json=iam.exchange())

    return iam_module.IamCredential(
        "https://iam.example",
        "tenant",
        audience=audience,
        platform_access_token="pat",
        transport=httpx.MockTransport(exchange),
        clock=iam.clock,
    )


def _notify_credential(monkeypatch: pytest.MonkeyPatch, iam: Issuer) -> None:
    """Токен audience notification-service — обменом того же PAT (commands._bearer_for), а не
    переменной NOTIFY_TOKEN."""
    iam_module = pytest.importorskip("control_plane_client.iam")
    monkeypatch.delenv("NOTIFY_TOKEN", raising=False)

    def from_environment(environ: Any = None, **_kw: Any) -> Any:
        assert environ[iam_module.ENV_IAM_AUDIENCE] == "notification-service"
        return _credential(iam, audience="notification-service")

    monkeypatch.setattr(iam_module, "iam_credential_from_environment", from_environment)


def _cp_credential(monkeypatch: pytest.MonkeyPatch, iam: Issuer) -> None:
    credentials = pytest.importorskip("control_plane_client.credentials")
    credential = _credential(iam)
    monkeypatch.delenv("CP_TOKEN", raising=False)
    monkeypatch.setattr(credentials, "resolve_credential", lambda _server: credential)


# --- поставщик дольше жизни токена ---------------------------------------------------------


def test_apply_longer_than_the_token_ttl_passes_with_a_fresh_token_per_request(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    clock = Clock()
    iam = Issuer(clock)
    cp_stand = Stand(core, iam)
    notify_stand = Stand(notify, iam, code="invalid_token", inner_auth="Bearer n")

    def target() -> Target:
        return Target(
            server=SERVER,
            http=cp_stand,
            token=Bearer.expiring(iam.exchange, clock=clock),
            notify=(notify_stand, Bearer.expiring(iam.exchange, clock=clock)),
        )

    plan_file = project / "plan.json"
    install.plan(
        project / "packages.yaml", target=target(), env=ENV, out=plan_file, log=lambda _m: None
    )
    started = clock()
    applied = install.apply(
        plan_file, target=target(), env=ENV, assume_yes=True, log=lambda _m: None
    )
    assert applied and core.core_writes and notify.writes
    assert clock() - started > TTL, "применение должно длиться дольше жизни токена"
    # ни один запрос не ушёл с недействительным токеном, и токен по ходу сменился
    assert cp_stand.rejected == 0 and notify_stand.rejected == 0
    assert len(_tokens(cp_stand)) > 2 and len(_tokens(notify_stand)) >= 1
    for at, token, _headers in cp_stand.seen + notify_stand.seen:
        assert iam.until[token] > at, (token, at)


def test_the_old_static_header_breaks_on_the_same_stand(
    project: Path, core: FakeCore, notify: FakeNotificationService
) -> None:
    """Обратная проверка привратника: заголовок на весь прогон — тот самый дефект."""
    clock = Clock()
    iam = Issuer(clock)
    token = iam.exchange()["accessToken"]
    target = Target(
        server=SERVER,
        http=Stand(core, iam),
        headers={"Authorization": f"Bearer {token}"},
        notify=(
            Stand(notify, iam, inner_auth="Bearer n"),
            {"Authorization": f"Bearer {token}"},
        ),
    )
    with pytest.raises((HttpError, PackageError), match="401"):
        install.plan(
            project / "packages.yaml",
            target=target,
            env=ENV,
            out=project / "plan.json",
            log=lambda _m: None,
        )
        install.apply(
            project / "plan.json", target=target, env=ENV, assume_yes=True, log=lambda _m: None
        )


def test_cli_target_takes_the_credential_token_before_every_request(
    project: Path,
    core: FakeCore,
    notify: FakeNotificationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI и MCP-сервер автора (commands._target): credential клиента ядра с часами стенда —
    IamCredential сам кэширует токен и обменивает PAT заново, когда тот истекает."""
    httpx = pytest.importorskip("httpx")
    credentials = pytest.importorskip("control_plane_client.credentials")
    iam_module = pytest.importorskip("control_plane_client.iam")
    clock = Clock()
    iam = Issuer(clock)
    cp_stand = Stand(core, iam)

    def exchange(request: Any) -> Any:
        assert json.loads(request.content)["token"] == "pat"
        return httpx.Response(200, json=iam.exchange())

    credential = iam_module.IamCredential(
        "https://iam.example",
        "tenant",
        platform_access_token="pat",
        transport=httpx.MockTransport(exchange),
        clock=clock,
    )
    monkeypatch.delenv("CP_TOKEN", raising=False)
    monkeypatch.setattr(credentials, "resolve_credential", lambda _server: credential)
    monkeypatch.setattr(commands, "Http", lambda base: notify if "notify" in base else cp_stand)
    monkeypatch.setenv("NOTIFY_TOKEN", "n")  # статический: его срок на человеке
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "https://platform.example.com/notify")
    env = {**ENV, "NOTIFY_TOKEN": "n", "NOTIFICATION_SERVICE_URL": "https://x/notify"}
    install_file = project / "packages.yaml"
    plan_file = project / "plan.json"
    install.plan(
        install_file,
        target=commands._target(SERVER, env, install_file),
        env=ENV,
        out=plan_file,
        log=lambda _m: None,
    )
    started = clock()
    install.apply(
        plan_file,
        target=commands._target(SERVER, env, install_file),
        env=ENV,
        assume_yes=True,
        log=lambda _m: None,
    )
    assert clock() - started > TTL and cp_stand.rejected == 0
    assert len(_tokens(cp_stand)) > 2


# --- 401 просроченной учётки: ровно один повтор ---------------------------------------------


def test_expired_credential_is_retried_once_with_a_renewed_token() -> None:
    clock = Clock()
    iam = Issuer(clock, ttl=10_000)
    stand = Stand(None, iam)
    calls = {"exchange": 0}

    def exchange() -> dict[str, Any]:
        calls["exchange"] += 1
        return iam.exchange()

    http = Authorized(stand, Bearer.expiring(exchange, clock=clock))
    http.call("GET", "/api/v1/roles")  # tok-1 действует
    iam.revoked.add("tok-1")  # стенд разлюбил токен раньше срока (отзыв, сдвиг часов)
    http.call("POST", "/api/v1/roles", {"key": "r"}, {"Idempotency-Key": "k-1"})
    assert [token for _at, token, _h in stand.seen] == ["tok-1", "tok-1", "tok-2"]
    assert calls["exchange"] == 2
    # повтор — та же команда: тот же Idempotency-Key
    assert [h.get("Idempotency-Key") for _at, _t, h in stand.seen[1:]] == ["k-1", "k-1"]


def test_second_401_is_an_error_without_more_retries() -> None:
    clock = Clock()
    iam = Issuer(clock, ttl=10_000)
    iam.accepts = lambda _token: False  # type: ignore[method-assign]  # учётка отозвана совсем
    stand = Stand(None, iam)
    calls = {"exchange": 0}

    def exchange() -> dict[str, Any]:
        calls["exchange"] += 1
        return iam.exchange()

    api = ProcessApi(Authorized(stand, Bearer.expiring(exchange, clock=clock)), {})
    with pytest.raises(HttpError) as raised:
        api.test({"package": {"files": []}})
    assert raised.value.status == 401
    assert [token for _at, token, _h in stand.seen] == ["tok-1", "tok-2"]
    assert calls["exchange"] == 2


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, {"error": {"code": "unauthorized"}}),  # другой отказ — не просрочка
        (401, None),
        (403, {"error": {"code": "invalid_credentials"}}),
        (403, {"error": "insufficient_scope"}),
    ],
)
def test_other_refusals_are_not_retried(status: int, body: Any) -> None:
    calls: list[str] = []

    class Refusing:
        def call(self, method: str, path: str, body_: Any = None, headers: Any = None) -> dict:
            calls.append(headers["Authorization"])
            raise HttpError(f"{method} {path}: HTTP {status}", status, body)

    renewed: list[int] = []
    bearer = Bearer(lambda: "tok", refresh=lambda: renewed.append(1))
    with pytest.raises(HttpError):
        Authorized(Refusing(), bearer).call("GET", "/api/v1/roles")
    assert calls == ["Bearer tok"] and renewed == []


def test_an_error_with_a_parsed_code_is_retried_too() -> None:
    """HttpError bootstrap суперпроекта несёт код готовым атрибутом, без тела."""

    class ParsedError(RuntimeError):
        def __init__(self) -> None:
            super().__init__("GET /api/v1/roles: HTTP 401")
            self.status = 401
            self.code = "invalid_credentials"

    sent: list[str] = []

    class Expiring:
        def call(self, method: str, path: str, body: Any = None, headers: Any = None) -> dict:
            sent.append(headers["Authorization"])
            if len(sent) == 1:
                raise ParsedError()
            return {"ok": True}

    tokens = iter(["old", "new"])
    current = {"token": next(tokens)}
    bearer = Bearer(lambda: current["token"], refresh=lambda: current.update(token=next(tokens)))
    assert Authorized(Expiring(), bearer).call("GET", "/api/v1/roles") == {"ok": True}
    assert sent == ["Bearer old", "Bearer new"]


def test_error_codes_of_core_and_auth_sdk_services() -> None:
    assert error_code({"error": {"code": "invalid_credentials", "message": "m"}}) == (
        "invalid_credentials"
    )
    assert error_code({"error": "invalid_token"}) == "invalid_token"
    assert error_code({"detail": {"error": "invalid_token"}}) == "invalid_token"
    assert error_code("text") is None


# --- статический токен человека не обновляется ----------------------------------------------


def test_static_cp_token_is_not_refreshed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CP_TOKEN", "human")
    try:
        from control_plane_client import credentials
    except ImportError:
        pass
    else:

        def refuse(_server: str) -> Any:
            raise AssertionError("при CP_TOKEN credential не ищется")

        monkeypatch.setattr(credentials, "resolve_credential", refuse)
    bearer = commands._bearer(SERVER)
    assert bearer.static and bearer.token() == "human" and bearer.renew() is False
    clock = Clock()
    stand = Stand(None, Issuer(clock))
    with pytest.raises(HttpError) as raised:
        Authorized(stand, bearer).call("GET", "/api/v1/roles")
    assert raised.value.status == 401
    assert [token for _at, token, _h in stand.seen] == ["human"], "повтора нет"


def test_static_notify_token_and_literal_tokens_are_not_refreshed() -> None:
    notify_bearer = commands._bearer_for(
        "notification-service", ("notifications:admin",), fallback="NOTIFY_TOKEN",
        environ={"NOTIFY_TOKEN": "n"},
    )  # fmt: skip
    assert notify_bearer.static and notify_bearer.token() == "n"
    target = Target(server=SERVER, http=Stand(None, Issuer(Clock())), token="literal")
    with pytest.raises(HttpError):
        target.get("/roles")
    assert [token for _at, token, _h in target.http.http.seen] == ["literal"]  # type: ignore[attr-defined]


def test_static_credential_of_the_client_is_not_refreshed() -> None:
    credentials = pytest.importorskip("control_plane_client.credentials")
    bearer = Bearer.of_credential(credentials.StaticCredential("cp_key"))
    assert bearer.static and bearer.token() == "cp_key" and bearer.renew() is False


def test_token_and_an_authorization_header_together_are_refused(core: FakeCore) -> None:
    with pytest.raises(ValueError, match="либо token"):
        Target(server=SERVER, http=core, headers={"Authorization": "Bearer t"}, token="t")


# --- проводка CLI: сервис уведомлений и export тоже берут токен перед каждым запросом -------
#
# Учётка здесь — credential клиента ядра (обмен PAT), а не переменная: со статическим
# заголовком, взятым один раз, эти прогоны падают на 401 — так тесты ловят возврат к нему.


def test_cli_notify_target_takes_the_credential_token_before_every_request(
    project: Path,
    core: FakeCore,
    notify: FakeNotificationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    cp_iam, notify_iam = Issuer(clock), Issuer(clock)
    cp_stand = Stand(core, cp_iam)
    notify_stand = Stand(notify, notify_iam, code="invalid_token", inner_auth="Bearer n")
    _cp_credential(monkeypatch, cp_iam)
    _notify_credential(monkeypatch, notify_iam)
    monkeypatch.setattr(
        commands, "Http", lambda base: notify_stand if "notify" in base else cp_stand
    )
    env = {**ENV, "NOTIFICATION_SERVICE_URL": "https://platform.example.com/notify"}
    install_file, plan_file = project / "packages.yaml", project / "plan.json"
    install.plan(
        install_file,
        target=commands._target(SERVER, env, install_file),
        env=ENV,
        out=plan_file,
        log=lambda _m: None,
    )
    started, before = clock(), len(notify_stand.seen)
    install.apply(
        plan_file,
        target=commands._target(SERVER, env, install_file),
        env=ENV,
        assume_yes=True,
        log=lambda _m: None,
    )
    applied = notify_stand.seen[before:]
    assert notify.writes and clock() - started > TTL
    assert notify_stand.rejected == 0 and cp_stand.rejected == 0
    # за применение токен сервиса уведомлений сменился: прогон длиннее его жизни
    assert len({token for _at, token, _h in applied}) >= 2
    assert applied[-1][0] - started > TTL


def _task_type(key: str) -> dict[str, Any]:
    return {
        "id": f"tt-{key}",
        "key": key,
        "version": 1,
        "status": "active",
        "name": key.title(),
        "workflow": {"statuses": [{"key": "todo", "category": "todo"}]},
    }


def test_cli_export_takes_the_credential_token_before_every_request(
    tmp_path: Path, core: FakeCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    iam = Issuer(clock)
    # по два запроса на ключ (список и объект): выгрузка дольше жизни токена
    keys = [f"type-{index:02d}" for index in range(int(TTL / STEP / 2) + 2)]
    core.rows["task-types"] += [_task_type(key) for key in keys]
    stand = Stand(core, iam)
    _cp_credential(monkeypatch, iam)
    monkeypatch.setattr(commands, "Http", lambda _base: stand)
    package_dir = tmp_path / "packages" / "acme"
    argv = ["export", "--server", SERVER, "--env", str(tmp_path / "none.env")]
    argv += ["--kind", "TaskType", "--package", str(package_dir)]
    for key in keys:
        argv += ["--key", key]
    assert commands.main(argv) == 0
    assert sorted(p.stem for p in (package_dir / "task-types").glob("*.yaml")) == keys
    assert clock() > TTL and stand.rejected == 0
    assert len(_tokens(stand)) >= 2


def test_cli_notification_rule_export_takes_the_credential_token_before_every_request(
    tmp_path: Path, notify: FakeNotificationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    iam = Issuer(clock)
    keys = [f"rule-{index:02d}" for index in range(int(TTL / STEP) + 2)]  # запрос на ключ
    notify.rows += [
        {
            "key": key,
            "version": 1,
            "spec": {"on": {"type": "x"}},
            "specHash": "h",
            "state": "active",
        }
        for key in keys
    ]
    stand = Stand(notify, iam, code="invalid_token", inner_auth="Bearer n")
    _notify_credential(monkeypatch, iam)
    monkeypatch.setattr(commands, "Http", lambda _base: stand)
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "https://platform.example.com/notify")
    package_dir = tmp_path / "packages" / "acme"
    argv = ["export", "--env", str(tmp_path / "none.env"), "--kind", "NotificationRule"]
    argv += ["--package", str(package_dir)]
    for key in keys:
        argv += ["--key", key]
    assert commands.main(argv) == 0
    assert len(list((package_dir / "notification-rules").glob("*.yaml"))) == len(keys)
    assert clock() > TTL and stand.rejected == 0
    assert len(_tokens(stand)) >= 2


# --- Target: учётка не в repr, обёртка транспорта одна --------------------------------------


def test_target_repr_does_not_show_the_credential() -> None:
    secret = "secret-token-value"
    for target in (
        Target(server=SERVER, http=Stand(None, Issuer(Clock())), token=secret),
        Target(
            server=SERVER,
            http=Stand(None, Issuer(Clock())),
            headers={"Authorization": f"Bearer {secret}"},
            notify=(Stand(None, Issuer(Clock())), {"Authorization": f"Bearer {secret}"}),
        ),
        Target(
            server=SERVER,
            http=Stand(None, Issuer(Clock())),
            token=secret,
            notify=(Stand(None, Issuer(Clock())), secret),
        ),
    ):
        assert secret not in repr(target) and SERVER in repr(target)
        applier = target.applier(ENV, dry_run=True, log=lambda _m: None)
        assert secret not in repr(applier)
    assert secret not in repr(ProcessApi(Stand(None, Issuer(Clock())), {"Authorization": secret}))


@pytest.mark.parametrize("token", ["literal", Bearer(lambda: "tok-1", refresh=lambda: None)])
def test_replaced_target_wraps_the_transport_once(token: Any) -> None:
    clock = Clock()
    iam = Issuer(clock, ttl=10_000)
    iam.accepts = lambda _token: False  # type: ignore[method-assign]  # каждый ответ — 401
    stand = Stand(None, iam)
    original = Target(server=SERVER, http=stand, token=token)
    replaced = dataclasses.replace(original)
    assert isinstance(replaced.http, Authorized) and replaced.http.http is stand
    with pytest.raises(HttpError) as raised:
        replaced.get("/roles")
    assert raised.value.status == 401
    # статический токен — без повтора, поставщик — ровно один повтор, а не по одному на обёртку
    assert len(stand.seen) == (1 if isinstance(token, str) else 2)


def test_replaced_target_takes_the_new_token() -> None:
    stand = Stand(None, Issuer(Clock()))
    replaced = dataclasses.replace(Target(server=SERVER, http=stand, token="old"), token="new")
    with pytest.raises(HttpError):
        replaced.get("/roles")
    assert [token for _at, token, _h in stand.seen] == ["new"]
