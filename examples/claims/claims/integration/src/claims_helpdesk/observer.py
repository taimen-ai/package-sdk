"""The helpdesk observer: new and reopened tickets become observations of the core.

A new ticket is ``helpdesk.ticket_created`` — it starts the process ``claim``; a ticket
the customer reopened after the reply is ``helpdesk.ticket_reopened`` — the rule
``claim-reopened`` files a follow-up. The other changes (the replies the skill
``helpdesk.reply@1`` wrote) are not facts for the package and are skipped.
"""

from __future__ import annotations

from typing import Any

from claims_helpdesk.helpdesk import changed_tickets
from package_sdk.connector import Observation, ObserveContext, observer, run

CREATED = "helpdesk.ticket_created"
REOPENED = "helpdesk.ticket_reopened"
DEFAULT_PAGE = 50


def fetch(base_url: str, token: str, *, since: int, limit: int) -> list[dict[str, Any]]:
    """Changed tickets of the helpdesk; a seam for the tests."""
    return changed_tickets(base_url, token, since=since, limit=limit)


def kind_of(ticket: dict[str, Any]) -> str | None:
    if ticket.get("status") == "reopened":
        return REOPENED
    if int(ticket.get("version") or 0) == 1:
        return CREATED
    return None


def data_of(ticket: dict[str, Any]) -> dict[str, Any]:
    """What rules and processes read: flat, without the replies and the change number."""
    customer = ticket.get("customer") or {}
    return {
        "ticketId": str(ticket["id"]),
        "version": int(ticket["version"]),
        "status": ticket.get("status"),
        "customerId": str(customer.get("id") or ""),
        "customerName": str(customer.get("name") or ""),
        "product": str(ticket.get("product") or ""),
        "subject": str(ticket.get("subject") or ""),
        "text": str(ticket.get("text") or ""),
        "amount": float(ticket.get("amount") or 0),
        "currency": str(ticket.get("currency") or ""),
        "channel": str(ticket.get("channel") or "web"),
    }


@observer(kind="helpdesk-observer", entrypoint="claims_helpdesk.observer:observe")
def observe(ctx: ObserveContext) -> None:
    base_url = str(ctx.config["baseUrl"])
    page = int(ctx.config.get("pageSize") or DEFAULT_PAGE)
    token = ctx.secret("helpdesk-token")  # the node's secret file, read again every cycle
    cursor = int(ctx.state.get("cursor", 0))
    for ticket in fetch(base_url, token, since=cursor, limit=page):
        kind = kind_of(ticket)
        if kind is not None:
            ctx.emit(
                Observation(
                    kind=kind,
                    # the ticket and its version: a repeated cycle is not a second fact
                    dedup_key=f"helpdesk:{ticket['id']}:{ticket['version']}",
                    data=data_of(ticket),
                    content=f"{ticket['id']}: {ticket.get('subject') or ''}",
                    external_ref={
                        "system": "helpdesk",
                        "id": str(ticket["id"]),
                        "url": f"{base_url.rstrip('/')}/tickets/{ticket['id']}",
                    },
                    observed_at=ticket.get("updatedAt"),
                )
            )
        cursor = max(cursor, int(ticket["seq"]))
    ctx.state["cursor"] = cursor  # stored only after a cycle without errors


if __name__ == "__main__":
    run(observe)
