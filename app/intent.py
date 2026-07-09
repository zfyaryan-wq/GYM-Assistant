from enum import StrEnum

from app.workflow_config import get_workflow_config


class Intent(StrEnum):
    UNSUPPORTED = "unsupported"
    CLAIM_SCORE = "claim_score"
    QUERY_OWN_SCORE = "query_own_score"
    HEALTH_ADVICE = "health_advice"
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
    question_markers = ("?", "？", "吗", "么", "怎么", "如何", "多少", "建议", "能不能", "应该")
    if any(keyword.lower() in normalized for keyword in keywords.health_advice) and any(marker in normalized for marker in question_markers):
        return Intent.HEALTH_ADVICE
    return Intent.NORMAL
