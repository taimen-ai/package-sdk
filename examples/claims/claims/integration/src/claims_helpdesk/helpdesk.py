"""A small client of the helpdesk API the example integrates with.

The demo helpdesk of the example (examples/claims/helpdesk) implements it; a real
helpdesk gets its own client with the same two calls. Standard library only, so the
integration brings no dependencies into the images.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TIMEOUT_SECONDS = 30


class HelpdeskError(Exception):
    """The helpdesk answered with an error; ``retryable`` — worth another attempt."""

    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = status is None or status in (408, 425, 429) or status >= 500


def _call(
    method: str,
    url: str,
    token: str,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        raise HelpdeskError(error.code, f"{method} {url}: HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise HelpdeskError(None, f"{method} {url}: {error}") from error


def changed_tickets(base_url: str, token: str, *, since: int, limit: int) -> list[dict[str, Any]]:
    """Tickets changed after the change number ``since``, oldest change first."""
    query = urllib.parse.urlencode({"since": since, "limit": limit})
    answer = _call("GET", f"{base_url.rstrip('/')}/tickets?{query}", token)
    return list(answer.get("tickets") or [])


def post_reply(
    base_url: str, token: str, *, ticket_id: str, message: str, close: bool, key: str
) -> dict[str, Any]:
    """Reply to a ticket. The same ``key`` again returns the same reply, not a second one."""
    ticket = urllib.parse.quote(ticket_id, safe="")
    answer: dict[str, Any] = _call(
        "POST",
        f"{base_url.rstrip('/')}/tickets/{ticket}/replies",
        token,
        {"message": message, "close": close},
        {"Idempotency-Key": key},
    )
    return answer
