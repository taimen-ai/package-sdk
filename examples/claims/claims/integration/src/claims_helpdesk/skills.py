"""Skills of the package claims: the code is the source, skills/*.yaml is its export.

- ``claims.classify@1`` — what the claim is about and how severe it is; a model of the
  installation answers (``ctx.llm``), nothing is written anywhere;
- ``helpdesk.reply@1`` — the reply to the customer in the helpdesk: a write to an
  external system, so the core runs it only on an approved gate of the task
  ``claim-reply`` (skills.md, external write).

Regenerate the package files after a change:
``skill-sdk export --package .. claims_helpdesk.skills`` (from integration/).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from skill_sdk import SkillContext, SkillError, skill

from claims_helpdesk.helpdesk import HelpdeskError, post_reply

Category = Literal["defect", "delivery", "billing", "other"]
Severity = Literal["low", "medium", "high"]

CLASSIFY_PROMPT = (
    "You triage customer claims about products. Answer with the category of the claim — "
    "defect (the product is broken or faulty), delivery (late, lost or damaged in "
    "transit), billing (charged wrongly) or other — and its severity: high when the "
    "customer reports a risk to safety or a legal threat, medium when the product cannot "
    "be used, low otherwise. confidence is between 0 and 1."
)


class ClaimText(BaseModel):
    subject: str = Field(max_length=300)
    text: str = Field(max_length=8000)


class Classification(BaseModel):
    category: Category
    severity: Severity
    confidence: float = Field(ge=0, le=1)


@skill("claims.classify", version="1", side_effects="none", risk="low", timeout=120)
async def classify(inputs: ClaimText, ctx: SkillContext) -> Classification:
    """The category and the severity of a customer claim, by its subject and text."""
    answer = await ctx.llm.chat_json(
        system_prompt=CLASSIFY_PROMPT,
        messages=[{"role": "user", "content": f"Subject: {inputs.subject}\n\n{inputs.text}"}],
        response_model=Classification,
        schema_name="classification",
    )
    result: Classification = answer.data
    return result


class Reply(BaseModel):
    ticketId: str = Field(min_length=1, max_length=64, title="Ticket id")
    message: str = Field(min_length=1, max_length=4000)
    close: bool = True


class Replied(BaseModel):
    replyId: str = Field(title="Reply id")
    status: str


@skill(
    "helpdesk.reply",
    version="1",
    side_effects="external_write",
    risk="medium",
    idempotency="required",
    timeout=60,
    retry=(3, 10),
)
def reply(inputs: Reply, ctx: SkillContext) -> Replied:
    """Reply to a helpdesk ticket on behalf of the organization and close it."""
    base_url = ctx.config("HELPDESK_URL")
    if not base_url:
        raise SkillError(
            "config_missing", "HELPDESK_URL is not set on the skills host", retryable=True
        )
    key = ctx.idempotency_key or ctx.invocation_id or ""
    if not key:
        raise SkillError("idempotency_key_required", "an external write needs a key")
    try:
        answer = post_reply(
            base_url,
            ctx.secret("helpdesk-token"),
            ticket_id=inputs.ticketId,
            message=inputs.message,
            close=inputs.close,
            key=key,
        )
    except HelpdeskError as error:
        raise SkillError(
            "helpdesk_unavailable" if error.retryable else "helpdesk_refused",
            str(error),
            retryable=error.retryable,
        ) from error
    return Replied(replyId=str(answer["replyId"]), status=str(answer["status"]))
