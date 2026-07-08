from enum import StrEnum

from app.workflow_config import get_workflow_config


class Intent(StrEnum):
    UNSUPPORTED = "unsupported"
    CLAIM_SCORE = "claim_score"
    QUERY_OWN_SCORE = "query_own_score"
    NORMAL = "normal"


def detect_intent(text: str) -> Intent:
    normalized = (text or "").strip().lower()
    if not normalized:
        return Intent.NORMAL
    keywords = get_workflow_config().intent_keywords
    if any(keyword.lower() in normalized for keyword in keywords.unsupported):
        return Intent.UNSUPPORTED
    if any(keyword in normalized for keyword in keywords.claim_score):
        return Intent.CLAIM_SCORE
    if any(keyword in normalized for keyword in keywords.query_own_score):
        return Intent.QUERY_OWN_SCORE
    return Intent.NORMAL
