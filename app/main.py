import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.staticfiles import StaticFiles

from app.config import Settings, get_settings
from app.db import get_db, init_db
from app.feishu import delete_message, get_message_resource, get_user_name, image_bytes_to_public_url, reply_message, send_message_to_chat
from app.schemas import GradingRequest, GradingResponse, HealthResponse, ReportResponse
from app.services import generate_report, generate_weekly_calorie_report, grade_request
from app.workflow_config import get_workflow_config


logger = logging.getLogger(__name__)
ACK_REPLY_TEXT = "收到 🏃"
REPORT_CHAT_ID_FILE = Path("data/report_chat_id.txt")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="GYM-Assistant", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="data/static", check_dir=False), name="static")


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


def _extract_message(event: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    sender_id = event.get("sender", {}).get("sender_id", {}).get("open_id", "")
    message = event.get("message", {})
    message_id = message.get("message_id", "")
    chat_id = message.get("chat_id", "")
    chat_type = message.get("chat_type", "")
    raw_content = message.get("content") or "{}"
    try:
        content = json.loads(raw_content)
    except json.JSONDecodeError:
        content = {}
    return message_id, sender_id, _extract_text_from_content(content), content.get("image_key", ""), chat_id, chat_type


def _is_report_command(text: str) -> bool:
    normalized = (text or "").strip()
    return any(keyword in normalized for keyword in get_workflow_config().intent_keywords.report_command)


def _is_weekly_report_command(text: str) -> bool:
    normalized = (text or "").strip()
    return any(keyword in normalized for keyword in get_workflow_config().intent_keywords.weekly_report_command)


def _is_local_request(request: Request) -> bool:
    return bool(request.client and request.client.host in {"127.0.0.1", "::1", "localhost"})


def _remember_report_chat_id(chat_id: str, chat_type: str) -> None:
    if not chat_id or chat_type != "group" or REPORT_CHAT_ID_FILE.exists():
        return
    REPORT_CHAT_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_CHAT_ID_FILE.write_text(chat_id, encoding="utf-8")


def _resolve_report_chat_id(settings: Settings) -> str:
    if settings.feishu_report_chat_id:
        return settings.feishu_report_chat_id
    if REPORT_CHAT_ID_FILE.exists():
        return REPORT_CHAT_ID_FILE.read_text(encoding="utf-8").strip()
    return ""


async def _send_result_message(settings: Settings, message_id: str, chat_id: str, text: str) -> None:
    if chat_id:
        await send_message_to_chat(settings, chat_id, text)
    elif message_id:
        await reply_message(settings, message_id, text)


def _extract_sent_message_id(response: dict[str, Any]) -> str:
    data = response.get("data") or {}
    message = data.get("message") or {}
    return str(message.get("message_id") or data.get("message_id") or "")


async def _delete_ack_message(settings: Settings, ack_message_id: str) -> None:
    if not ack_message_id:
        return
    try:
        await delete_message(settings, ack_message_id)
    except HTTPException:
        logger.exception("Failed to delete ack message: message_id=%s", ack_message_id)


@app.post("/api/tasks/daily-report")
async def send_daily_report(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Daily report task is only available locally")
    report_chat_id = _resolve_report_chat_id(settings)
    if not report_chat_id:
        raise HTTPException(status_code=503, detail="FEISHU_REPORT_CHAT_ID is not configured")

    report_result = generate_report(db, settings.default_season_start)
    await send_message_to_chat(settings, report_chat_id, report_result.markdown)
    return {"ok": True, "report": report_result.model_dump()}


@app.post("/api/feishu/events")
async def feishu_events(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        logger.warning("Invalid Feishu event JSON body: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    try:
        if "encrypt" in payload:
            raise HTTPException(status_code=400, detail="Encrypted Feishu events are not supported yet. Disable event encryption first.")

        if payload.get("type") == "url_verification":
            _verify_feishu_token(settings, payload)
            return {"challenge": payload.get("challenge")}

        _verify_feishu_token(settings, payload)
        header = payload.get("header", {})
        if header.get("event_type") != "im.message.receive_v1":
            return {"ok": True, "ignored": True}

        message_id, sender_id, text, image_key, chat_id, chat_type = _extract_message(payload.get("event", {}))
        _remember_report_chat_id(chat_id, chat_type)
        logger.info(
            "Feishu message received: message_id=%s sender_id=%s chat_id=%s chat_type=%s has_text=%s has_image=%s",
            message_id,
            sender_id,
            chat_id,
            chat_type,
            bool(text),
            bool(image_key),
        )
        ack_message_id = ""
        if message_id and settings.feishu_app_id and settings.feishu_app_secret:
            ack_response = await reply_message(settings, message_id, ACK_REPLY_TEXT)
            ack_message_id = _extract_sent_message_id(ack_response)

        sender_name = ""
        if sender_id and settings.feishu_app_id and settings.feishu_app_secret:
            sender_name = await get_user_name(settings, sender_id)

        if _is_report_command(text):
            report_result = generate_report(db, settings.default_season_start)
            replied = False
            if message_id and settings.feishu_app_id and settings.feishu_app_secret:
                await _send_result_message(settings, message_id, chat_id, report_result.markdown)
                await _delete_ack_message(settings, ack_message_id)
                replied = True
            return {"ok": True, "replied": replied, "report": report_result.model_dump()}

        if _is_weekly_report_command(text):
            weekly_result = generate_weekly_calorie_report(db)
            replied = False
            if message_id and settings.feishu_app_id and settings.feishu_app_secret:
                await _send_result_message(settings, message_id, chat_id, weekly_result.markdown)
                await _delete_ack_message(settings, ack_message_id)
                replied = True
            return {"ok": True, "replied": replied, "weekly_report": weekly_result.model_dump()}

        picture = None
        if image_key and message_id:
            image_bytes = await get_message_resource(settings, message_id, image_key)
            picture = image_bytes_to_public_url(settings, image_bytes, image_key)

        result = await grade_request(
            settings,
            db,
            GradingRequest(input=text, picture=picture, sender_id=sender_id, sender_name=sender_name, source_message_id=message_id),
        )

        replied = False
        if message_id and settings.feishu_app_id and settings.feishu_app_secret:
            await _send_result_message(settings, message_id, chat_id, result.output)
            await _delete_ack_message(settings, ack_message_id)
            replied = True
        return {"ok": True, "replied": replied, "result": result.model_dump()}
    except HTTPException as exc:
        logger.exception("Feishu event handling failed: status=%s detail=%s", exc.status_code, exc.detail)
        raise
    except Exception:
        logger.exception("Unexpected Feishu event handling failure")
        raise
