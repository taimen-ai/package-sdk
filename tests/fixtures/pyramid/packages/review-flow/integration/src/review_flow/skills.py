"""Скилл кода интеграции: контракт в skills/request.summarize.yaml пакета — его выгрузка."""

from pydantic import BaseModel
from skill_sdk import SkillContext, skill


class SummarizeIn(BaseModel):
    request: str


class SummarizeOut(BaseModel):
    summary: str


@skill("request.summarize", version="1", side_effects="none", risk="low")
def summarize(inputs: SummarizeIn, ctx: SkillContext) -> SummarizeOut:
    """A short summary of a request for its reviewer."""
    return SummarizeOut(summary=f"request {inputs.request}")
