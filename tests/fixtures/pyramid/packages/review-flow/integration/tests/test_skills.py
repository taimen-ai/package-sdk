from review_flow.skills import summarize
from skill_sdk.testing import invoke


def test_summarize_names_the_request() -> None:
    assert invoke(summarize, {"request": "R-1"}).outputs == {"summary": "request R-1"}
