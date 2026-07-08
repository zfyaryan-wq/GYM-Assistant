import json
import re
from datetime import date
from typing import Any

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError, field_validator

from app.config import Settings
from app.workflow_config import get_workflow_config


class GradeResult(BaseModel):
    output: str
    score: int
    note: str
    activity_type: str | None = ""
    activity_duration_minutes: int | None = None
    calories_burned: int | None = None
    activity_summary: str | None = ""

    @field_validator("score")
    @classmethod
    def validate_score(cls, value: int) -> int:
        if value not in {0, 1, 3}:
            raise ValueError("score must be one of 0, 1, 3")
        return value

    @field_validator("activity_duration_minutes", "calories_burned")
    @classmethod
    def validate_non_negative(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("activity values must be non-negative")
        return value


def _extract_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        raise ValueError("model response does not contain JSON")
    return json.loads(match.group(0))


def _message_content(user_input: str, picture: str | None) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": user_input or ""}]
    if picture:
        content.append({"type": "image_url", "image_url": {"url": picture}})
    return content


def _grade_prompt(today: date) -> str:
    prompt = get_workflow_config().grading_prompt
    scoring_rules = "\n".join(f"- {rule}" for rule in prompt.scoring_rules)
    violation_rules = "\n".join(f"- {rule}" for rule in prompt.violation_rules)
    reply_requirements = "\n".join(f"- {rule}" for rule in prompt.reply_requirements)
    return f"""
{prompt.role}

当前日期：{today.isoformat()}

评分规则：
{scoring_rules}

卡路里估算：
{prompt.calorie_requirement}

违规规则：
{violation_rules}

回复要求：
{reply_requirements}

你必须只输出 JSON，不要输出 Markdown，不要解释。JSON 格式如下：
{{
  "output": "回复给用户的话",
  "score": 0,
  "note": "15字以内打分说明",
  "activity_type": "运动项目，如跑步/骑行/力量训练；没有运动时为空字符串",
  "activity_duration_minutes": 30,
  "calories_burned": 180,
  "activity_summary": "15字以内运动摘要；没有运动时为空字符串"
}}
score 只能是 0、1、3。
{prompt.note_requirement}
""".strip()


def _user_context_text(user_activity_memory: list[str] | None) -> str:
    if not user_activity_memory:
        return "用户近期没有可用运动记忆。"
    lines = "\n".join(f"- {item}" for item in user_activity_memory[:5])
    return f"用户近期运动记忆：\n{lines}"


async def grade_with_llm(
    settings: Settings,
    user_input: str,
    picture: str | None,
    user_activity_memory: list[str] | None = None,
    today: date | None = None,
) -> GradeResult:
    if not settings.llm_api_key:
        raise HTTPException(status_code=503, detail="LLM_API_KEY is not configured")

    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": _grade_prompt(today or date.today())},
            {"role": "system", "content": _user_context_text(user_activity_memory)},
            {"role": "user", "content": _message_content(user_input, picture)},
        ],
        "stream": False,
        "temperature": 0.5,
    }
    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }
    if settings.llm_provider_id:
        headers["providerId"] = settings.llm_provider_id

    async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
        response = await client.post(settings.chat_completions_url, headers=headers, json=payload)

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"LLM request failed: {response.text[:500]}")

    data = response.json()
    content = data["choices"][0]["message"]["content"]
    try:
        result = GradeResult.model_validate(_extract_json(content))
    except (KeyError, ValueError, json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=502, detail=f"Invalid LLM response: {exc}") from exc

    return result
