"""Авторизация запросов к стенду: токен берётся перед каждым запросом (TASK-001256).

Access token ядра и сервиса уведомлений живёт минуты (на staging — 300 с), а установка идёт
дольше: последовательные GET/PUT каталога, план ядра, применение. Токен, полученный один раз
и положенный в заголовки, истекает посреди применения — стенд отвечает
``401 invalid_credentials``. Поэтому SDK держит не заголовок, а поставщика токена
(:class:`Bearer`), и заголовок ``Authorization`` ставит транспорт (:class:`Authorized`) перед
каждым запросом.

Граница: авторизация — дело транспорта. ``Applier``, ``ProcessApi``, ``Target`` и прочие
вызывают ``http.call(…, headers)`` как прежде; ``headers`` у них — только прочие заголовки
(``Idempotency-Key``), а ``Authorized`` поверх любого ``HttpLike`` добавляет свежий токен.

Отказ ``401`` с кодом просроченной учётки (``invalid_credentials`` ядра, ``invalid_token``
сервисов на platform-auth-sdk) — ровно один повтор с обновлённым токеном и тем же
``Idempotency-Key`` (это та же бизнес-команда), второй ``401`` — ошибка как есть. Так же
ведёт себя клиент ядра (``control_plane_client``). Статический токен (``CP_TOKEN``,
``NOTIFY_TOKEN`` — явный выбор человека) не обновляется и не повторяется.

Поставщики:

- :meth:`Bearer.static_token` — строка, не меняется;
- :meth:`Bearer.of_credential` — credential клиента ядра (``resolve_credential`` или
  ``IamCredential``): он сам кэширует токен и обменивает PAT заново, когда тот истекает;
- :meth:`Bearer.expiring` — функция обмена, которая возвращает ответ IAM
  ``{accessToken, expiresIn}`` (так bootstrap суперпроекта меняет PAT оператора): токен
  кэшируется до ``expiresIn`` минус запас;
- любой ``Callable[[], str]`` — спрашивается перед каждым запросом и ещё раз после ``401``;
  кэшировать — его дело.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from typing import Any

from package_sdk.apply import HttpLike

# Коды 401 «учётка просрочена или отозвана»: ядро (AuthenticationError control-plane) и
# сервисы на platform-auth-sdk (InvalidToken). Прочие 401 и любые 403 — отказ по существу.
EXPIRED_CODES = frozenset({"invalid_credentials", "invalid_token"})
# Обменять заново за столько секунд до истечения: токен, истекший в полёте, дал бы 401.
REFRESH_MARGIN_SECONDS = 30.0


class Bearer:
    """Поставщик access token: ``token()`` — перед каждым запросом, ``renew()`` — после
    ``401`` просроченной учётки (``False`` — обновлять нечего, повтора не будет)."""

    def __init__(
        self,
        token: Callable[[], str],
        *,
        refresh: Callable[[], object] | None = None,
        static: bool = False,
    ) -> None:
        self._token = token
        self._refresh = refresh
        self.static = static

    def token(self) -> str:
        return self._token()

    def renew(self) -> bool:
        if self.static:
            return False
        if self._refresh is not None:
            self._refresh()
        return True

    def authorization(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    @classmethod
    def static_token(cls, value: str) -> Bearer:
        """Токен, который назвал человек (``CP_TOKEN``, ``NOTIFY_TOKEN``): не обновляется."""
        return cls(lambda: value, static=True)

    @classmethod
    def of_credential(cls, credential: Any) -> Bearer:
        """Credential клиента ядра (протокол ``CredentialProvider``: async ``token()``,
        async ``refresh()``, ``refreshable``). Кэш и срок жизни — его; здесь только мост из
        синхронного кода SDK (MCP-сервер зовёт его из потока, не из цикла событий)."""
        return cls(
            lambda: str(asyncio.run(credential.token())),
            refresh=lambda: asyncio.run(credential.refresh()),
            static=not getattr(credential, "refreshable", True),
        )

    @classmethod
    def expiring(
        cls,
        exchange: Callable[[], Mapping[str, Any]],
        *,
        clock: Callable[[], float] = time.monotonic,
        margin: float = REFRESH_MARGIN_SECONDS,
    ) -> Bearer:
        """Токен обмена IAM: ``exchange()`` возвращает ``{accessToken, expiresIn}`` и
        зовётся, когда кэш пуст, истекает через ``margin`` секунд или стенд отверг токен."""
        state: dict[str, Any] = {"token": "", "until": 0.0}

        def refresh() -> str:
            answer = exchange()
            token = str(answer.get("accessToken") or "")
            if not token:
                raise RuntimeError("обмен учётки не вернул accessToken")
            expires_in = float(answer.get("expiresIn") or 0)
            state["token"] = token
            state["until"] = clock() + expires_in
            return token

        def current() -> str:
            if state["token"] and clock() + margin < state["until"]:
                return str(state["token"])
            return refresh()

        return cls(current, refresh=refresh)


# Источник токена там, где SDK его принимает: строка — статический токен, функция — поставщик.
TokenSource = Bearer | Callable[[], str] | str


def bearer(source: TokenSource) -> Bearer:
    if isinstance(source, Bearer):
        return source
    if isinstance(source, str):
        return Bearer.static_token(source)
    if callable(source):
        return Bearer(source)
    raise TypeError(f"источник токена — строка, функция или Bearer, а не {type(source)!r}")


def error_code(body: Any) -> str | None:
    """Код ошибки тела ответа: ``{"error": {"code"}}`` ядра, ``{"error": "<код>"}``
    platform-auth-sdk, ``{"code"}`` или те же формы под ``detail`` (FastAPI)."""
    if not isinstance(body, dict):
        return None
    for envelope in (body, body.get("detail")):
        if not isinstance(envelope, dict):
            continue
        error = envelope.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return str(error["code"])
        if isinstance(error, str):
            return error
        if isinstance(envelope.get("code"), str):
            return str(envelope["code"])
    return None


def expired(error: BaseException) -> bool:
    """401 просроченной или отозванной учётки — его лечит новый токен. Код — из тела ответа
    (``HttpError`` SDK) или готовым атрибутом ``code`` (``HttpError`` bootstrap суперпроекта)."""
    if getattr(error, "status", None) != 401:
        return False
    code = error_code(getattr(error, "body", None)) or getattr(error, "code", None)
    return code in EXPIRED_CODES


class Authorized:
    """``HttpLike`` поверх транспорта: ``Authorization`` — свежий токен поставщика перед
    каждым запросом; на 401 просроченной учётки — один повтор с обновлённым токеном."""

    def __init__(self, http: HttpLike, token: TokenSource) -> None:
        self.http = http
        self.bearer = bearer(token)

    def call(
        self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        try:
            return self.http.call(method, path, body, self._headers(headers))
        except RuntimeError as error:  # HttpError SDK и bootstrap — RuntimeError со status
            if not expired(error) or not self.bearer.renew():
                raise
        # тот же Idempotency-Key: повтор — та же команда; второй 401 — отказ как есть
        return self.http.call(method, path, body, self._headers(headers))

    def _headers(self, headers: dict[str, str] | None) -> dict[str, str]:
        return {**(headers or {}), **self.bearer.authorization()}
