import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db, init_db
from app.feishu import get_message_resource, get_user_name, image_bytes_to_data_uri, reply_message
from app.schemas import GradingRequest, GradingResponse, HealthResponse, ReportResponse
from app.services import generate_report, generate_weekly_calorie_report, grade_request
from app.workflow_config import get_workflow_config


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Coze Migration API", version="0.1.0", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/api/grading", response_model=GradingResponse)
async def grading(
    payload: GradingRequest,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> GradingResponse:
    if payload.sender_id and not payload.sender_name and settings.feishu_app_id and settings.feishu_app_secret:
        payload.sender_name = await get_user_name(settings, payload.sender_id)
    return await grade_request(settings, db, payload)


@app.get("/api/report", response_model=ReportResponse)
def report(
    since: str | None = None,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> ReportResponse:
    return generate_report(db, since or settings.default_season_start)


@app.get("/api/weekly-calories", response_model=ReportResponse)
def weekly_calories(
    week_start: str | None = None,
    db: Session = Depends(get_db),
) -> ReportResponse:
    return generate_weekly_calorie_report(db, week_start)


def _verify_feishu_token(settings: Settings, payload: dict[str, Any]) -> None:
    token = settings.feishu_verification_token
    received_token = payload.get("token") or payload.get("header", {}).get("token")
    if token and received_token != token:
        raise HTTPException(status_code=403, detail="Invalid Feishu verification token")


def _extract_text_from_content(content: dict[str, Any]) -> str:
    if text := content.get("text"):
        return str(text).strip()

    # Rich text messages use a nested "post" structure. Preserve only readable text.
    post = content.get("post") or {}
    zh_cn = post.get("zh_cn") or post.get("en_us") or {}
    chunks: list[str] = []
    for line in zh_cn.get("content", []):
        for item in line:
            if item.get("tag") == "text" and item.get("text"):
                chunks.append(str(item["text"]))
            elif item.get("tag") == "a" and item.get("text"):
                chunks.append(str(item["text"]))
    return "".join(chunks).strip()


def _extract_message(event: dict[str, Any]) -> tuple[str, str, str, str]:
    sender_id = event.get("sender", {}).get("sender_id", {}).get("open_id", "")
    message = event.get("message", {})
    message_id = message.get("message_id", "")
    raw_content = message.get("content") or "{}"
    try:
        content = json.loads(raw_content)
    except json.JSONDecodeError:
        content = {}
    return message_id, sender_id, _extract_text_from_content(content), content.get("image_key", "")


def _is_report_command(text: str) -> bool:
    normalized = (text or "").strip()
    return any(keyword in normalized for keyword in get_workflow_config().intent_keywords.report_command)


def _is_weekly_report_command(text: str) -> bool:
    normalized = (text or "").strip()
    return any(keyword in normalized for keyword in get_workflow_config().intent_keywords.weekly_report_command)


@app.post("/api/feishu/events")
async def feishu_events(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    payload = await request.json()
    if "encrypt" in payload:
        raise HTTPException(status_code=400, detail="Encrypted Feishu events are not supported yet. Disable event encryption first.")

    if payload.get("type") == "url_verification":
        _verify_feishu_token(settings, payload)
        return {"challenge": payload.get("challenge")}

    _verify_feishu_token(settings, payload)
    header = payload.get("header", {})
    if header.get("event_type") != "im.message.receive_v1":
        return {"ok": True, "ignored": True}

    message_id, sender_id, text, image_key = _extract_message(payload.get("event", {}))
    sender_name = ""
    if sender_id and settings.feishu_app_id and settings.feishu_app_secret:
        sender_name = await get_user_name(settings, sender_id)

    if _is_report_command(text):
        report_result = generate_report(db, settings.default_season_start)
        replied = False
        if message_id and settings.feishu_app_id and settings.feishu_app_secret:
            await reply_message(settings, message_id, report_result.markdown)
            replied = True
        return {"ok": True, "replied": replied, "report": report_result.model_dump()}

    if _is_weekly_report_command(text):
        weekly_result = generate_weekly_calorie_report(db)
        replied = False
        if message_id and settings.feishu_app_id and settings.feishu_app_secret:
            await reply_message(settings, message_id, weekly_result.markdown)
            replied = True
        return {"ok": True, "replied": replied, "weekly_report": weekly_result.model_dump()}

    picture = None
    if image_key and message_id:
        image_bytes = await get_message_resource(settings, message_id, image_key)
        picture = image_bytes_to_data_uri(image_bytes, image_key)

    result = await grade_request(
        settings,
        db,
        GradingRequest(input=text, picture=picture, sender_id=sender_id, sender_name=sender_name),
    )

    replied = False
    if message_id and settings.feishu_app_id and settings.feishu_app_secret:
        await reply_message(settings, message_id, result.output)
        replied = True
    return {"ok": True, "replied": replied, "result": result.model_dump()}
