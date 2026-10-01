"""Редиректы транспорта CLI (TASK-001258): учётка не уходит на другой origin.

Стенд — два настоящих HTTP-сервера на localhost на разных портах, то есть два origin.
Старый адрес стенда отвечает 308 на новый (так ``cp.old.example`` отвечает на ``cp.new.example``):
обычный ``urllib`` повторил бы GET у второго сервера с тем же ``Authorization``, а POST с
кодом 301–303 — превратил бы в GET без тела."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from package_sdk.apply import Http, HttpError, RedirectRefused
from package_sdk.auth import Authorized, Bearer

SECRET = {"Authorization": "Bearer secret-token", "Idempotency-Key": "k-1"}


class Server:
    """Сервер в потоке: ``routes`` — путь → (код, Location); прочее — 200 с эхом запроса.
    ``seen`` — все пришедшие запросы с заголовками."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, str]] = {}
        self.seen: list[dict[str, Any]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode() if length else ""
                request = {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                    "body": body,
                }
                server.seen.append(request)
                if self.path in server.routes:
                    code, location = server.routes[self.path]
                    self.send_response(code)
                    self.send_header("Location", location)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = json.dumps(request).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_PATCH = _serve

            def log_message(self, *_args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def servers() -> Iterator[tuple[Server, Server]]:
    old, new = Server(), Server()
    try:
        yield old, new
    finally:
        old.close()
        new.close()


def _header(request: dict[str, Any], name: str) -> str | None:
    return next((v for k, v in request["headers"].items() if k.lower() == name.lower()), None)


@pytest.mark.parametrize(
    ("method", "code", "body"),
    [
        ("GET", 308, None),
        ("GET", 301, None),
        ("GET", 302, None),
        ("POST", 302, {"key": "r"}),  # urllib сделал бы из него GET у чужого хоста
        ("POST", 308, {"key": "r"}),
        ("PUT", 307, {"key": "r"}),
    ],
)
def test_redirect_to_another_origin_is_refused_and_nothing_reaches_it(
    servers: tuple[Server, Server], method: str, code: int, body: Any
) -> None:
    old, new = servers
    old.routes["/api/v1/roles"] = (code, f"{new.url}/api/v1/roles")
    with pytest.raises(RedirectRefused) as raised:
        Http(old.url).call(method, "/api/v1/roles", body, dict(SECRET))
    message = str(raised.value)
    assert f"сервер перенаправляет на {new.url} — укажите его в --server" in message
    assert raised.value.status == code and isinstance(raised.value, HttpError)
    assert new.seen == [], "ни запроса, ни учётки у другого origin"
    assert "secret-token" not in message


def test_another_port_of_the_same_host_is_another_origin(servers: tuple[Server, Server]) -> None:
    old, new = servers
    assert old.url.rpartition(":")[0] == new.url.rpartition(":")[0]  # хост тот же
    old.routes["/x"] = (308, new.url + "/x")
    with pytest.raises(RedirectRefused, match="перенаправляет на"):
        Http(old.url).call("GET", "/x", None, dict(SECRET))
    assert new.seen == []


def test_redirect_within_the_origin_keeps_the_headers(servers: tuple[Server, Server]) -> None:
    old, _new = servers
    old.routes["/api/v1/roles"] = (308, "/api/v1/roles/")  # добавление «/»
    answer = Http(old.url).call("GET", "/api/v1/roles", None, dict(SECRET))
    assert answer["path"] == "/api/v1/roles/"
    first, second = old.seen
    for request in (first, second):
        assert _header(request, "Authorization") == SECRET["Authorization"]
        assert _header(request, "Idempotency-Key") == SECRET["Idempotency-Key"]


def test_redirect_of_a_request_with_a_body_is_refused_within_the_origin_too(
    servers: tuple[Server, Server],
) -> None:
    """Тело POST/PUT при редиректе теряется — повторять запрос без него нельзя."""
    old, _new = servers
    old.routes["/api/v1/roles"] = (302, "/api/v1/roles/")
    with pytest.raises(RedirectRefused, match="тело запроса при редиректе теряется"):
        Http(old.url).call("POST", "/api/v1/roles", {"key": "r"}, dict(SECRET))
    assert [r["path"] for r in old.seen] == ["/api/v1/roles"]


def test_refused_redirect_is_not_retried_as_an_expired_credential(
    servers: tuple[Server, Server],
) -> None:
    old, new = servers
    old.routes["/api/v1/roles"] = (308, f"{new.url}/api/v1/roles")
    renewed: list[int] = []
    http = Authorized(Http(old.url), Bearer(lambda: "tok", refresh=lambda: renewed.append(1)))
    with pytest.raises(RedirectRefused):
        http.call("GET", "/api/v1/roles")
    assert len(old.seen) == 1 and renewed == [] and new.seen == []


def test_plain_requests_still_carry_the_headers(servers: tuple[Server, Server]) -> None:
    old, _new = servers
    answer = Http(old.url).call("PUT", "/api/v1/roles/r", {"key": "r"}, dict(SECRET))
    assert answer["method"] == "PUT" and json.loads(answer["body"]) == {"key": "r"}
    assert _header(answer, "Authorization") == SECRET["Authorization"]
    assert _header(answer, "Idempotency-Key") == SECRET["Idempotency-Key"]
    assert _header(answer, "Content-Type") == "application/json"
