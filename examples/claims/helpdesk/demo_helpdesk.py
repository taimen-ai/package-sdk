"""A demo helpdesk: the source of claims of the example, no external accounts needed.

One file of the standard library. Tickets live in a JSON file; every change of a
ticket (created, replied, reopened) gets the next change number ``seq``, and the
observer reads the changes after its cursor.

    python demo_helpdesk.py --port 8080 --data ./data     # 127.0.0.1 by default
    HELPDESK_TOKEN=… python demo_helpdesk.py --host 0.0.0.0  # other addresses need the token

API (JSON; with HELPDESK_TOKEN set, every call but /healthz needs
``Authorization: Bearer <token>``):

    GET  /healthz
    GET  /tickets?since=<seq>&limit=<n>   changes after seq: {"tickets": [...], "next": seq}
    POST /tickets                         file a ticket: {customer: {id, name}, product,
                                          subject, text, amount, currency, channel}
    GET  /tickets/<id>                    the ticket with its replies
    POST /tickets/<id>/replies            {message, close}; header Idempotency-Key — the same
                                          key again answers the same reply, not a second one
    POST /tickets/<id>:reopen             {text}: the customer is not satisfied
"""

from __future__ import annotations

import argparse
import datetime as dt
import hmac
import ipaddress
import json
import os
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MAX_BODY = 64 * 1024
MAX_PAGE = 200


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


class Store:
    """Tickets in one JSON file, written atomically; one lock for the whole store."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        if path.exists():
            self.state: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.state = {"seq": 0, "tickets": {}, "replies": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def _touch(self, ticket: dict[str, Any]) -> dict[str, Any]:
        self.state["seq"] += 1
        ticket["seq"] = self.state["seq"]
        ticket["version"] = int(ticket.get("version", 0)) + 1
        ticket["updatedAt"] = _now()
        return ticket

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            number = 1000 + len(self.state["tickets"]) + 1
            customer = body.get("customer") or {}
            ticket = {
                "id": f"T-{number}",
                "status": "open",
                "customer": {
                    "id": str(customer.get("id", "")),
                    "name": str(customer.get("name", "")),
                },
                "product": str(body.get("product", "")),
                "subject": str(body.get("subject", "")),
                "text": str(body.get("text", "")),
                "amount": float(body.get("amount") or 0),
                "currency": str(body.get("currency", "")),
                "channel": str(body.get("channel") or "web"),
                "createdAt": _now(),
                "replies": [],
            }
            self._touch(ticket)
            self.state["tickets"][ticket["id"]] = ticket
            self._save()
            return ticket

    def changes(self, since: int, limit: int) -> list[dict[str, Any]]:
        with self.lock:
            changed = [t for t in self.state["tickets"].values() if t["seq"] > since]
            return sorted(changed, key=lambda t: t["seq"])[:limit]

    def get(self, ticket_id: str) -> dict[str, Any] | None:
        with self.lock:
            ticket = self.state["tickets"].get(ticket_id)
            return dict(ticket) if ticket else None

    def reply(
        self, ticket_id: str, message: str, close: bool, key: str
    ) -> tuple[dict[str, Any], bool] | None:
        """The reply and whether it is new; None — no such ticket."""
        with self.lock:
            ticket = self.state["tickets"].get(ticket_id)
            if ticket is None:
                return None
            known = self.state["replies"].get(key)
            if known is not None:
                return known, False
            reply = {
                "id": f"R-{sum(len(t['replies']) for t in self.state['tickets'].values()) + 1}",
                "message": message,
                "at": _now(),
            }
            ticket["replies"].append(reply)
            ticket["status"] = "closed" if close else "answered"
            self._touch(ticket)
            answer = {"replyId": reply["id"], "ticketId": ticket_id, "status": ticket["status"]}
            self.state["replies"][key] = answer
            self._save()
            return answer, True

    def reopen(self, ticket_id: str, text: str) -> dict[str, Any] | str | None:
        with self.lock:
            ticket = self.state["tickets"].get(ticket_id)
            if ticket is None:
                return None
            if ticket["status"] not in ("closed", "answered"):
                return "only an answered or closed ticket can be reopened"
            ticket["status"] = "reopened"
            if text:
                ticket["text"] = text
            self._touch(ticket)
            self._save()
            return dict(ticket)


class Handler(BaseHTTPRequestHandler):
    store: Store
    token: str = ""

    def log_message(self, format: str, *args: Any) -> None:
        print(
            f"helpdesk: {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}",
            flush=True,
        )

    def _send(self, status: int, body: Any) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, message: str) -> None:
        self._send(status, {"error": {"message": message}})

    def _authorized(self) -> bool:
        if not self.token:
            return True
        given = self.headers.get("Authorization", "")
        return hmac.compare_digest(given, f"Bearer {self.token}")

    def _body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY:
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return None
        return body if isinstance(body, dict) else None

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/healthz":
            self._send(HTTPStatus.OK, {"status": "ok"})
            return
        if not self._authorized():
            self._error(HTTPStatus.UNAUTHORIZED, "a bearer token is required")
            return
        if url.path == "/tickets":
            query = urllib.parse.parse_qs(url.query)
            try:
                since = int((query.get("since") or ["0"])[0])
                limit = int((query.get("limit") or ["50"])[0])
            except ValueError:
                self._error(HTTPStatus.BAD_REQUEST, "since and limit are integers")
                return
            if since < 0 or limit < 1:
                self._error(HTTPStatus.BAD_REQUEST, "since ≥ 0 and limit ≥ 1 are expected")
                return
            limit = min(limit, MAX_PAGE)
            tickets = self.store.changes(since, limit)
            self._send(
                HTTPStatus.OK,
                {"tickets": tickets, "next": tickets[-1]["seq"] if tickets else since},
            )
            return
        if url.path.startswith("/tickets/"):
            ticket = self.store.get(urllib.parse.unquote(url.path.removeprefix("/tickets/")))
            if ticket is None:
                self._error(HTTPStatus.NOT_FOUND, "no such ticket")
            else:
                self._send(HTTPStatus.OK, ticket)
            return
        self._error(HTTPStatus.NOT_FOUND, "no such route")

    def do_POST(self) -> None:
        if not self._authorized():
            self._error(HTTPStatus.UNAUTHORIZED, "a bearer token is required")
            return
        body = self._body()
        if body is None:
            self._error(HTTPStatus.BAD_REQUEST, "a JSON object is expected")
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/tickets":
            if not body.get("subject") or not body.get("text"):
                self._error(HTTPStatus.UNPROCESSABLE_ENTITY, "subject and text are required")
                return
            self._send(HTTPStatus.CREATED, self.store.create(body))
            return
        if path.startswith("/tickets/") and path.endswith("/replies"):
            ticket_id = urllib.parse.unquote(
                path.removeprefix("/tickets/").removesuffix("/replies")
            )
            key = self.headers.get("Idempotency-Key", "")
            if not key or not body.get("message"):
                self._error(
                    HTTPStatus.UNPROCESSABLE_ENTITY, "Idempotency-Key and message are required"
                )
                return
            result = self.store.reply(
                ticket_id, str(body["message"]), bool(body.get("close", True)), key
            )
            if result is None:
                self._error(HTTPStatus.NOT_FOUND, "no such ticket")
                return
            answer, created = result
            self._send(HTTPStatus.CREATED if created else HTTPStatus.OK, answer)
            return
        if path.startswith("/tickets/") and path.endswith(":reopen"):
            ticket_id = urllib.parse.unquote(path.removeprefix("/tickets/").removesuffix(":reopen"))
            reopened = self.store.reopen(ticket_id, str(body.get("text") or ""))
            if reopened is None:
                self._error(HTTPStatus.NOT_FOUND, "no such ticket")
            elif isinstance(reopened, str):
                self._error(HTTPStatus.CONFLICT, reopened)
            else:
                self._send(HTTPStatus.OK, reopened)
            return
        self._error(HTTPStatus.NOT_FOUND, "no such route")


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def server(host: str, port: int, data: Path, token: str) -> ThreadingHTTPServer:
    """The helpdesk on ``host:port``; on an address other than loopback only with a token."""
    if not token and not _loopback(host):
        raise ValueError(f"{host}: set HELPDESK_TOKEN to listen on an address other than loopback")
    handler = type(
        "DemoHelpdesk", (Handler,), {"store": Store(data / "tickets.json"), "token": token}
    )
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data", type=Path, default=Path("data"))
    args = parser.parse_args()
    try:
        httpd = server(args.host, args.port, args.data, os.environ.get("HELPDESK_TOKEN", ""))
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(f"demo helpdesk on {args.host}:{args.port}, data in {args.data}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
