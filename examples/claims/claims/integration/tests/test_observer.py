"""The observer on a fake core: which tickets become which observations, and the cursor."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from claims_helpdesk import observer

from package_sdk.connector.testing import FakeCore, run_once

CONFIG = {"baseUrl": "http://helpdesk.test", "pageSize": 10}
SECRETS = {"helpdesk-token": "test-token"}


def ticket(ticket_id: str, *, version: int, seq: int, status: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": ticket_id,
        "version": version,
        "seq": seq,
        "status": status,
        "customer": {"id": "C-7", "name": "Northwind Ltd"},
        "product": "Grinder X2",
        "subject": "The grinder stopped working",
        "text": "It stopped after a week.",
        "amount": 120,
        "currency": "EUR",
        "channel": "web",
        "updatedAt": "2026-01-15T10:00:00Z",
        **extra,
    }


@dataclass
class Helpdesk:
    """The changes the helpdesk returns and the calls the observer made."""

    changed: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, str, int, int]] = field(default_factory=list)

    def fetch(self, base_url: str, token: str, *, since: int, limit: int) -> list[dict[str, Any]]:
        self.calls.append((base_url, token, since, limit))
        return [t for t in self.changed if t["seq"] > since][:limit]


@pytest.fixture
def helpdesk(monkeypatch: pytest.MonkeyPatch) -> Helpdesk:
    fake = Helpdesk()
    monkeypatch.setattr(observer, "fetch", fake.fetch)
    return fake


def test_a_new_ticket_starts_a_case_and_moves_the_cursor(helpdesk: Helpdesk) -> None:
    helpdesk.changed.append(ticket("T-1001", version=1, seq=1, status="open"))

    result = run_once(observer.observe, config=CONFIG, secrets=SECRETS)

    (seen,) = result.observations
    assert seen["kind"] == "helpdesk.ticket_created"
    assert seen["dedup_key"] == "helpdesk:T-1001:1"
    assert seen["data"]["ticketId"] == "T-1001" and seen["data"]["customerId"] == "C-7"
    assert seen["data"]["amount"] == 120.0 and "replies" not in seen["data"]
    assert seen["external_ref"]["url"] == "http://helpdesk.test/tickets/T-1001"
    assert result.state == {"cursor": 1}
    assert helpdesk.calls == [("http://helpdesk.test", "test-token", 0, 10)]


def test_replies_are_skipped_and_a_reopened_ticket_is_its_own_fact(helpdesk: Helpdesk) -> None:
    helpdesk.changed.append(ticket("T-1001", version=2, seq=2, status="closed"))
    helpdesk.changed.append(
        ticket("T-1001", version=3, seq=3, status="reopened", text="It broke again.")
    )

    result = run_once(observer.observe, config=CONFIG, secrets=SECRETS, state={"cursor": 1})

    assert [(o["kind"], o["dedup_key"]) for o in result.observations] == [
        ("helpdesk.ticket_reopened", "helpdesk:T-1001:3")
    ]
    assert result.observations[0]["data"]["text"] == "It broke again."
    assert result.state == {"cursor": 3}


def test_a_repeated_cycle_is_not_a_second_fact(helpdesk: Helpdesk) -> None:
    helpdesk.changed.append(ticket("T-1001", version=1, seq=1, status="open"))
    core = FakeCore()

    run_once(observer.observe, config=CONFIG, secrets=SECRETS, core=core)
    # the cursor was lost (a new replica): the helpdesk returns the same change again
    run_once(observer.observe, config=CONFIG, secrets=SECRETS, core=core)

    assert len(core.observations) == 1


def test_a_failed_publication_keeps_the_cursor(helpdesk: Helpdesk) -> None:
    helpdesk.changed.append(ticket("T-1001", version=1, seq=1, status="open"))
    core = FakeCore()
    core.fail_after = 0

    result = run_once(
        observer.observe, config=CONFIG, secrets=SECRETS, core=core, state={"cursor": 0}
    )

    assert result.observations == [] and result.state == {"cursor": 0}


def test_without_the_token_the_cycle_is_skipped(helpdesk: Helpdesk) -> None:
    helpdesk.changed.append(ticket("T-1001", version=1, seq=1, status="open"))

    result = run_once(observer.observe, config=CONFIG, secrets={})

    assert [o["kind"] for o in result.observations] == ["connector.secret_missing"]
    assert helpdesk.calls == []
