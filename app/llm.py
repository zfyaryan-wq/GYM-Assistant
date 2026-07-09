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
    default_image_prompt = "\u8bf7\u6839\u636e\u8fd9\u5f20\u56fe\u7247\u5185\u5bb9\u8fdb\u884c\u5065\u5eb7\u6253\u5361\u8bc4\u5206\uff0c\u5e76\u5c3d\u91cf\u8bc6\u522b\u8fd0\u52a8\u9879\u76ee\u3001\u65f6\u957f\u3001\u8ddd\u79bb\u3001\u6b65\u6570\u6216\u5361\u8def\u91cc\u6d88\u8017\u3002"
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

\u5f53\u524d\u65e5\u671f\uff1a{today.isoformat()}

\u8bc4\u5206\u89c4\u5219\uff1a
{scoring_rules}

\u5361\u8def\u91cc\u4f30\u7b97\uff1a
{prompt.calorie_requirement}

\u8fdd\u89c4\u89c4\u5219\uff1a
{violation_rules}

\u56de\u590d\u8981\u6c42\uff1a
{reply_requirements}

\u4f60\u5fc5\u987b\u53ea\u8f93\u51fa JSON\uff0c\u4e0d\u8981\u8f93\u51fa Markdown\uff0c\u4e0d\u8981\u89e3\u91ca\u3002\u5373\u4f7f\u65e0\u6cd5\u8bc6\u522b\u56fe\u7247\u3001\u56fe\u7247\u7f3a\u5931\u6216\u56fe\u7247\u5185\u5bb9\u4e0d\u6e05\u6670\uff0c\u4e5f\u5fc5\u987b\u6309\u4e0b\u9762\u683c\u5f0f\u8f93\u51fa JSON\uff1a
{{
  "output": "\u56de\u590d\u7ed9\u7528\u6237\u7684\u8bdd",
  "score": 0,
  "note": "15\u5b57\u4ee5\u5185\u6253\u5206\u8bf4\u660e",
  "activity_type": "\u8fd0\u52a8\u9879\u76ee\uff0c\u5982\u8dd1\u6b65/\u9a91\u884c/\u529b\u91cf\u8bad\u7ec3\uff1b\u6ca1\u6709\u8fd0\u52a8\u65f6\u4e3a\u7a7a\u5b57\u7b26\u4e32",
  "activity_duration_minutes": 30,
  "calories_burned": 180,
  "activity_summary": "15\u5b57\u4ee5\u5185\u8fd0\u52a8\u6458\u8981\uff1b\u6ca1\u6709\u8fd0\u52a8\u65f6\u4e3a\u7a7a\u5b57\u7b26\u4e32"
}}
score \u53ea\u80fd\u662f 0\u30011\u30013\u3002
{prompt.note_requirement}
""".strip()


def _user_context_text(user_activity_memory: list[str] | None, user_name: str | None = None) -> str:
    parts: list[str] = []
    nickname = _friendly_name(user_name)
    if nickname:
        parts.append(f"\u79f0\u547c\u7528\u6237\u65f6\u4f18\u5148\u4f7f\u7528\uff1a{nickname}\u3002\u56de\u590d\u91cc\u53ef\u4ee5\u81ea\u7136\u53eb\u4e00\u6b21\uff0c\u4e0d\u8981\u6bcf\u53e5\u8bdd\u90fd\u53eb\u3002")
    if user_activity_memory:
        lines = "\n".join(f"- {item}" for item in user_activity_memory[:5])
        parts.append(f"\u7528\u6237\u8fd1\u671f\u8fd0\u52a8\u8bb0\u5fc6\uff1a\n{lines}")
    else:
        parts.append("\u7528\u6237\u8fd1\u671f\u6ca1\u6709\u53ef\u7528\u8fd0\u52a8\u8bb0\u5fc6\u3002")
    return "\n\n".join(parts)


def _health_advice_prompt(today: date) -> str:
    return f"""
\u4f60\u662f\u7fa4\u91cc\u7684\u8fd0\u52a8\u59d4\u5458\u578b\u540c\u4e8b\uff0c\u5f53\u524d\u65e5\u671f\uff1a{today.isoformat()}\u3002

\u4f60\u53ef\u4ee5\u56de\u7b54\uff1a
- \u70ed\u91cf\u6d88\u8017\u3001\u5361\u8def\u91cc\u4f30\u7b97\u3001\u8fd0\u52a8\u9879\u76ee\u9009\u62e9\u3001\u8bad\u7ec3\u5b89\u6392\u3001\u6062\u590d\u548c\u62c9\u4f38\u5efa\u8bae\u3002
- \u65e5\u5e38\u996e\u98df\u5efa\u8bae\u3001\u51cf\u8102/\u589e\u808c\u7684\u57fa\u7840\u8425\u517b\u5efa\u8bae\u3001\u86cb\u767d\u8d28\u548c\u78b3\u6c34\u5b89\u6392\u3002
- \u7528\u6237\u8be2\u95ee\u201c\u673a\u5668\u4eba\u600e\u4e48\u7b97\u5361\u8def\u91cc\u201d\u65f6\uff0c\u56de\u7b54\uff1a\u4f18\u5148\u91c7\u7528\u7528\u6237\u6587\u5b57\u6216\u8fd0\u52a8\u622a\u56fe\u4e2d\u660e\u786e\u7ed9\u51fa\u7684\u6d88\u8017\uff1b\u5982\u679c\u6ca1\u6709\u660e\u786e\u6d88\u8017\uff0c\u5219\u7528\u516c\u5f0f\u201c\u4f30\u7b97\u70ed\u91cf kcal \u2248 MET \u00d7 \u4f53\u91cd kg \u00d7 \u8fd0\u52a8\u65f6\u957f h\u201d\u505a\u4fdd\u5b88\u4f30\u7b97\u3002\u6ca1\u6709\u4f53\u91cd\u65f6\u9ed8\u8ba4\u6309\u666e\u901a\u6210\u5e74\u4eba\u7ea6 60-70kg \u533a\u95f4\u4f30\u7b97\uff0c\u5e76\u7ed3\u5408\u8fd0\u52a8\u9879\u76ee\u3001\u65f6\u957f\u3001\u8ddd\u79bb\u3001\u6b65\u6570\u548c\u5f3a\u5ea6\u4fee\u6b63\u3002\u8be5\u4f30\u7b97\u7528\u4e8e\u6d3b\u52a8\u6fc0\u52b1\u548c\u8d8b\u52bf\u53c2\u8003\uff0c\u4e0d\u662f\u533b\u7597\u6216\u7cbe\u786e\u8fd0\u52a8\u5904\u65b9\u3002
- \u5982\u679c\u7528\u6237\u95ee\u4f30\u7b97\u65b9\u5f0f\uff0c\u5c3d\u91cf\u7ed3\u5408\u201c\u7528\u6237\u8fd1\u671f\u8fd0\u52a8\u8bb0\u5fc6\u201d\u4e3e\u4f8b\u8bf4\u660e\uff0c\u4f8b\u5982\u5f15\u7528\u5176\u6700\u8fd1\u8dd1\u6b65\u3001\u9a91\u884c\u6216\u529b\u91cf\u8bad\u7ec3\u8bb0\u5f55\u6765\u89e3\u91ca\u4e3a\u4ec0\u4e48\u4f1a\u5f97\u5230\u67d0\u4e2a\u533a\u95f4\u3002

\u56de\u7b54\u8981\u6c42\uff1a
- \u7528\u4e2d\u6587\uff0c\u50cf\u719f\u540c\u4e8b\u5728\u7fa4\u91cc\u804a\u5929\uff0c\u7b80\u6d01\u5b9e\u7528\uff0c\u9ed8\u8ba4 3-5 \u53e5\u8bdd\u3002
- \u5c11\u7528\u9879\u76ee\u7b26\u53f7\uff0c\u522b\u50cf\u767e\u79d1\u6216\u5ba2\u670d\uff1b\u53ef\u4ee5\u8f7b\u5fae\u5634\u8d2b\uff0c\u4f46\u4e0d\u8981\u6cb9\u817b\u3002
- \u6bcf\u6b21\u56de\u590d\u53ef\u4ee5\u81ea\u7136\u5e26 1-2 \u4e2a\u53ef\u7231 emoji\uff0c\u4f8b\u5982 \U0001f44f\u3001\U0001f63a\u3001\U0001f4aa\u3001\U0001f331\uff1b\u4e0d\u8981\u8fde\u7eed\u5237\u5c4f\u5f0f\u5806\u8868\u60c5\u3002
- \u4e0d\u8981\u7ed9\u8bca\u65ad\u7ed3\u8bba\uff0c\u4e0d\u8981\u66ff\u4ee3\u533b\u751f\u3001\u8425\u517b\u5e08\u6216\u6559\u7ec3\u3002
- \u6d89\u53ca\u75be\u75c5\u3001\u75bc\u75db\u3001\u7729\u6655\u3001\u80f8\u95f7\u3001\u53d7\u4f24\u3001\u5b55\u671f\u3001\u6162\u75c5\u3001\u7528\u836f\u65f6\uff0c\u5efa\u8bae\u5148\u54a8\u8be2\u4e13\u4e1a\u4eba\u58eb\u3002
- \u5982\u679c\u4fe1\u606f\u4e0d\u8db3\uff0c\u5148\u7ed9\u901a\u7528\u5efa\u8bae\uff0c\u5e76\u8bf4\u660e\u9700\u8981\u54ea\u4e9b\u4fe1\u606f\u624d\u80fd\u66f4\u51c6\u3002
- \u5f53\u7528\u6237\u8fde\u7eed\u505a\u540c\u4e00\u7c7b\u8bad\u7ec3\u3001\u8bad\u7ec3\u65f6\u95f4\u8fc7\u957f\u6216\u5f3a\u5ea6\u504f\u9ad8\u65f6\uff0c\u53ea\u7ed9\u4e00\u4e2a\u6700\u5173\u952e\u63d0\u9192\uff0c\u4f8b\u5982\u8bad\u7ec3\u5747\u8861\u3001\u62c9\u4f38\u3001\u8865\u6c34\u3001\u7761\u7720\u6216\u6062\u590d\uff0c\u4e0d\u8981\u4e00\u53e3\u6c14\u8bf4\u592a\u591a\u3002
- \u4e0d\u8981\u63d0\u5065\u5eb7\u79ef\u5206\uff0c\u4e0d\u8981\u628a\u95ee\u7b54\u5f53\u4f5c\u6253\u5361\u3002
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
