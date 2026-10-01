"""The skills on fakes: the model of the classification and the helpdesk of the reply."""

from __future__ import annotations

from typing import Any

import pytest
from claims_helpdesk import skills
from claims_helpdesk.helpdesk import HelpdeskError
from skill_sdk import SkillError
from skill_sdk.testing import FakeLlm, check_contract, invoke


def test_contracts_are_accepted_by_the_core() -> None:
    check_contract(skills.classify)
    check_contract(skills.reply)


def test_the_claim_is_classified_by_the_model() -> None:
    llm = FakeLlm([{"category": "defect", "severity": "high", "confidence": 0.93}])

    result = invoke(
        skills.classify,
        {"subject": "Sparks from the heater", "text": "It sparked and smells of smoke."},
        llm=llm,
    )

    assert result.outputs == {"category": "defect", "severity": "high", "confidence": 0.93}
    assert llm.calls[0].prompt.startswith("Subject: Sparks from the heater")


def test_an_answer_outside_the_categories_is_not_accepted() -> None:
    llm = FakeLlm([{"category": "weather", "severity": "low", "confidence": 0.5}])

    with pytest.raises(Exception):  # noqa: B017 — the response model refuses the answer
        invoke(skills.classify, {"subject": "?", "text": "?"}, llm=llm)


# The host's configuration and the node's secret, as the skills host passes them.
HOST = {"HELPDESK_URL": "http://helpdesk.test", "helpdesk-token": "test-token"}


@pytest.fixture
def helpdesk(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    def post_reply(base_url: str, token: str, **call: Any) -> dict[str, Any]:
        sent.append({"baseUrl": base_url, "token": token, **call})
        return {"replyId": "R-1", "status": "closed"}

    monkeypatch.setattr(skills, "post_reply", post_reply)
    return sent


def test_the_reply_goes_to_the_ticket_with_the_idempotency_key(
    helpdesk: list[dict[str, Any]],
) -> None:
    result = invoke(
        skills.reply,
        {"ticketId": "T-1001", "message": "We refund the grinder."},
        env=HOST,
        idempotency_key="approval-1",
    )

    assert result.outputs == {"replyId": "R-1", "status": "closed"}
    (call,) = helpdesk
    assert call == {
        "baseUrl": "http://helpdesk.test",
        "token": "test-token",
        "ticket_id": "T-1001",
        "message": "We refund the grinder.",
        "close": True,
        "key": "approval-1",
    }


def test_an_unavailable_helpdesk_is_a_retryable_failure(
    helpdesk: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise HelpdeskError(503, "unavailable")

    monkeypatch.setattr(skills, "post_reply", down)

    with pytest.raises(SkillError) as failure:
        invoke(skills.reply, {"ticketId": "T-1001", "message": "Hi"}, env=HOST, idempotency_key="k")

    assert failure.value.code == "helpdesk_unavailable" and failure.value.retryable
