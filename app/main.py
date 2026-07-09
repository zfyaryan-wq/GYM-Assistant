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
from app.intent import detect_intent
from app.schemas import GradingRequest, GradingResponse, HealthResponse, ReportResponse
from app.services import add_message_log, generate_report, generate_weekly_calorie_report, grade_request, update_message_log_response
from app.workflow_config import get_workflow_config


logger = logging.getLogger(__name__)
ACK_REPLY_TEXT = "\u6536\u5230 \U0001f3c3"
BOT_INTRO_TEXT = (
    "\u5927\u5bb6\u597d\uff0c\u6211\u662f GYM-Assistant\uff0c\u7fa4\u91cc\u7684\u5065\u5eb7\u6253\u5361\u5c0f\u540c\u4e8b \U0001f44b\n"
    "\u6211\u53ef\u4ee5\u5e2e\u5927\u5bb6\u8bb0\u5f55\u8fd0\u52a8\u548c\u5065\u5eb7\u751f\u6d3b\u6253\u5361\uff0c\u6839\u636e\u6587\u5b57\u6216\u56fe\u7247\u4f30\u7b97\u79ef\u5206\u3001\u5361\u8def\u91cc\u548c\u8fd0\u52a8\u7c7b\u578b\u3002\n"
    "\u6211\u8fd8\u4f1a\u770b\u4f60\u6700\u8fd1\u7684\u8bad\u7ec3\u8282\u594f\uff0c\u63d0\u9192\u6709\u6c27/\u65e0\u6c27\u642d\u914d\u3001\u6062\u590d\u3001\u62c9\u4f38\u3001\u8425\u517b\u548c\u5173\u8282\u538b\u529b\uff0c\u522b\u8ba9\u5927\u5bb6\u4e3a\u4e86\u53d8\u5f3a\u628a\u81ea\u5df1\u7ec3\u574f \U0001f4aa\n"
    "\u4f60\u53ef\u4ee5\u76f4\u63a5\u53d1\u6253\u5361\u6587\u5b57\u6216\u8fd0\u52a8\u622a\u56fe\uff1b\u53d1\u201c\u6211\u7684\u79ef\u5206\u201d\u6216\u201c\u67e5\u79ef\u5206\u201d\u53ef\u4ee5\u67e5\u81ea\u5df1\u5206\u6570\uff1b\u53d1\u201c\u6392\u884c\u699c\u201d\u6216\u201c\u5468\u62a5\u201d\u53ef\u4ee5\u770b\u7fa4\u91cc\u6218\u51b5\u3002\n"
    "\u6211\u4e5f\u4f1a\u987a\u624b\u770b\u5317\u4eac\u5929\u6c14\uff0c\u9ad8\u6e29\u63d0\u9192\u8865\u6c34\uff0c\u4e0b\u96e8\u63d0\u9192\u8def\u4e0a\u5b89\u5168\u3002\u653e\u5fc3\uff0c\u6211\u5c3d\u91cf\u50cf\u540c\u4e8b\u804a\u5929\uff0c\u4e0d\u5f53\u51b7\u51b0\u51b0\u7684\u7cfb\u7edf\u901a\u77e5 \U0001f63a"
)
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


def _extract_message(event: dict[str, Any]) -> tuple[str, str, str, str, str, str, str]:
    sender_id = event.get("sender", {}).get("sender_id", {}).get("open_id", "")
    message = event.get("message", {})
    message_id = message.get("message_id", "")
    chat_id = message.get("chat_id", "")
    chat_type = message.get("chat_type", "")
    create_time = str(message.get("create_time") or "")
    raw_content = message.get("content") or "{}"
    try:
        content = json.loads(raw_content)
    except json.JSONDecodeError:
        content = {}
    return message_id, sender_id, _extract_text_from_content(content), content.get("image_key", ""), chat_id, chat_type, create_time


def _extract_bot_added_chat_id(event: dict[str, Any]) -> str:
    return str(
        event.get("chat_id")
        or event.get("chat", {}).get("chat_id")
        or event.get("chat", {}).get("open_chat_id")
        or event.get("open_chat_id")
        or ""
    )


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
        event_type = header.get("event_type")
        event = payload.get("event", {})

        if event_type == "im.chat.member.bot.added_v1":
            chat_id = _extract_bot_added_chat_id(event)
            _remember_report_chat_id(chat_id, "group")
            replied = False
            if chat_id and settings.feishu_app_id and settings.feishu_app_secret:
                await send_message_to_chat(settings, chat_id, BOT_INTRO_TEXT)
                replied = True
            return {"ok": True, "replied": replied, "chat_id": chat_id}

        if event_type != "im.message.receive_v1":
            return {"ok": True, "ignored": True}

        message_id, sender_id, text, image_key, chat_id, chat_type, message_created_at = _extract_message(event)
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

        message_intent = detect_intent(text).value
        if _is_report_command(text):
            message_intent = "report_command"
        elif _is_weekly_report_command(text):
            message_intent = "weekly_report_command"
        add_message_log(
            db,
            message_id=message_id,
            sender_id=sender_id,
            sender_name=sender_name,
            chat_id=chat_id,
            chat_type=chat_type,
            text=text,
            image_key=image_key,
            intent=message_intent,
            message_created_at=message_created_at,
        )

        if message_intent == "report_command":
            report_result = generate_report(db, settings.default_season_start)
            replied = False
            if message_id and settings.feishu_app_id and settings.feishu_app_secret:
                await _send_result_message(settings, message_id, chat_id, report_result.markdown)
                await _delete_ack_message(settings, ack_message_id)
                replied = True
            update_message_log_response(db, message_id, report_result.markdown, message_intent)
            return {"ok": True, "replied": replied, "report": report_result.model_dump()}

        if message_intent == "weekly_report_command":
            weekly_result = generate_weekly_calorie_report(db)
            replied = False
            if message_id and settings.feishu_app_id and settings.feishu_app_secret:
                await _send_result_message(settings, message_id, chat_id, weekly_result.markdown)
                await _delete_ack_message(settings, ack_message_id)
                replied = True
            update_message_log_response(db, message_id, weekly_result.markdown, message_intent)
            return {"ok": True, "replied": replied, "weekly_report": weekly_result.model_dump()}

        picture = None
        if image_key and message_id:
            image_bytes = await get_message_resource(settings, message_id, image_key)
            picture = image_bytes_to_public_url(settings, image_bytes, image_key)

        result = await grade_request(
            settings,
            db,
            GradingRequest(
                input=text,
                picture=picture,
                sender_id=sender_id,
                sender_name=sender_name,
                source_message_id=message_id,
                message_created_at=message_created_at,
            ),
        )

        replied = False
        if message_id and settings.feishu_app_id and settings.feishu_app_secret:
            await _send_result_message(settings, message_id, chat_id, result.output)
            await _delete_ack_message(settings, ack_message_id)
            replied = True
        update_message_log_response(db, message_id, result.output, message_intent)
        return {"ok": True, "replied": replied, "result": result.model_dump()}
    except HTTPException as exc:
        logger.exception("Feishu event handling failed: status=%s detail=%s", exc.status_code, exc.detail)
        raise
    except Exception:
        logger.exception("Unexpected Feishu event handling failure")
        raise
