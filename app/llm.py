import json
import logging
import re
from datetime import date
from typing import Any

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError, field_validator

from app.config import Settings
from app.workflow_config import get_workflow_config


logger = logging.getLogger(__name__)


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
    default_image_prompt = "请根据这张图片内容进行健康打卡评分，并尽量识别运动项目、时长、距离、步数或卡路里消耗。"
    content: list[dict[str, Any]] = [{"type": "text", "text": user_input or (default_image_prompt if picture else "")}]
    if picture:
        content.append({"type": "image_url", "image_url": {"url": picture}})
    return content


def _friendly_name(user_name: str | None) -> str:
    name = (user_name or "").strip()
    if len(name) == 3 and all("\u4e00" <= char <= "\u9fff" for char in name):
        return name[1:]
    return name


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

你必须只输出 JSON，不要输出 Markdown，不要解释。即使无法识别图片、图片缺失或图片内容不清晰，也必须按下面格式输出 JSON：
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


def _user_context_text(user_activity_memory: list[str] | None, user_name: str | None = None) -> str:
    parts: list[str] = []
    nickname = _friendly_name(user_name)
    if nickname:
        parts.append(f"称呼用户时优先使用：{nickname}。回复里可以自然叫一次，不要每句话都叫。")
    if user_activity_memory:
        lines = "\n".join(f"- {item}" for item in user_activity_memory[:5])
        parts.append(f"用户近期运动记忆：\n{lines}")
    else:
        parts.append("用户近期没有可用运动记忆。")
    return "\n\n".join(parts)


def _health_advice_prompt(today: date) -> str:
    return f"""
你是群里的运动委员型同事，当前日期：{today.isoformat()}。

你可以回答：
- 热量消耗、卡路里估算、运动项目选择、训练安排、恢复和拉伸建议。
- 日常饮食建议、减脂/增肌的基础营养建议、蛋白质和碳水安排。
- 用户询问“机器人怎么算卡路里”时，回答：优先采用用户文字或运动截图中明确给出的消耗；如果没有明确消耗，则用公式“估算热量 kcal ≈ MET × 体重 kg × 运动时长 h”做保守估算。没有体重时默认按普通成年人约 60-70kg 区间估算，并结合运动项目、时长、距离、步数和强度修正。该估算用于活动激励和趋势参考，不是医疗或精确运动处方。
- 如果用户问估算方式，尽量结合“用户近期运动记忆”举例说明，例如引用其最近跑步、骑行或力量训练记录来解释为什么会得到某个区间。

回答要求：
- 用中文，像熟同事在群里聊天，简洁实用，默认 3-5 句话。
- 少用项目符号，别像百科或客服；可以轻微嘴贫，但不要油腻。
- 不要给诊断结论，不要替代医生、营养师或教练。
- 涉及疾病、疼痛、眩晕、胸闷、受伤、孕期、慢病、用药时，建议先咨询专业人士。
- 如果信息不足，先给通用建议，并说明需要哪些信息才能更准。
- 当用户连续做同一类训练、训练时间过长或强度偏高时，只给一个最关键提醒，例如训练均衡、拉伸、补水、睡眠或恢复，不要一口气说太多。
- 不要提健康积分，不要把问答当作打卡。
""".strip()


async def answer_health_question(
    settings: Settings,
    user_input: str,
    user_activity_memory: list[str] | None = None,
    user_name: str | None = None,
    today: date | None = None,
) -> str:
    if not settings.llm_api_key:
        raise HTTPException(status_code=503, detail="LLM_API_KEY is not configured")

    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": _health_advice_prompt(today or date.today())},
            {"role": "system", "content": _user_context_text(user_activity_memory, user_name)},
            {"role": "user", "content": user_input},
        ],
        "stream": False,
        "temperature": 0.6,
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
    try:
        return str(data["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError) as exc:
        raise HTTPException(status_code=502, detail=f"Invalid LLM response: {exc}") from exc


async def grade_with_llm(
    settings: Settings,
    user_input: str,
    picture: str | None,
    user_activity_memory: list[str] | None = None,
    user_name: str | None = None,
    today: date | None = None,
) -> GradeResult:
    if not settings.llm_api_key:
        raise HTTPException(status_code=503, detail="LLM_API_KEY is not configured")

    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": _grade_prompt(today or date.today())},
            {"role": "system", "content": _user_context_text(user_activity_memory, user_name)},
            {"role": "user", "content": _message_content(user_input, picture)},
        ],
        "stream": False,
        "temperature": 0.5,
        "response_format": {"type": "json_object"},
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
        logger.error("Invalid LLM response content: %s", str(content)[:1000])
        raise HTTPException(status_code=502, detail=f"Invalid LLM response: {exc}") from exc

    return result
