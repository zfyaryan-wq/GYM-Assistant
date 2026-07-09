from datetime import date, datetime, timedelta
from uuid import uuid4

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import MessageLog, ScoreLog
from app.intent import Intent, detect_intent
from app.llm import GradeResult, answer_health_question, grade_with_llm
from app.schemas import GradingRequest, GradingResponse, ReportResponse
from app.weather import get_weather_training_tip
from app.workflow_config import get_workflow_config


def _new_log_id() -> str:
    return str(int(datetime.now().timestamp() * 1000)) + uuid4().hex[:8]


def add_message_log(
    db: Session,
    *,
    message_id: str,
    sender_id: str,
    sender_name: str,
    chat_id: str,
    chat_type: str,
    text: str,
    image_key: str,
    intent: str,
    message_created_at: str,
) -> MessageLog:
    row = None
    if message_id:
        row = db.scalar(select(MessageLog).where(MessageLog.message_id == message_id).limit(1))
    if row is None:
        row = MessageLog(id=_new_log_id(), message_id=message_id or "")
        db.add(row)

    row.sender_id = sender_id or ""
    row.sender_name = sender_name or ""
    row.chat_id = chat_id or ""
    row.chat_type = chat_type or ""
    row.text = text or ""
    row.image_key = image_key or ""
    row.intent = intent or ""
    row.message_created_at = message_created_at or ""
    db.commit()
    db.refresh(row)
    return row


def update_message_log_response(db: Session, message_id: str, bot_reply: str, intent: str | None = None) -> None:
    if not message_id:
        return
    row = db.scalar(select(MessageLog).where(MessageLog.message_id == message_id).limit(1))
    if row is None:
        return
    if intent is not None:
        row.intent = intent
    row.bot_reply = bot_reply or ""
    row.processed_at = datetime.now()
    db.commit()


def add_score_log(db: Session, request: GradingRequest, result: GradeResult) -> ScoreLog:
    row = ScoreLog(
        id=_new_log_id(),
        sys_platform=request.sys_platform,
        uuid=None,
        source_message_id=request.source_message_id or "",
        bstudio_create_time=datetime.now(),
        score_delta=result.score,
        note=result.note,
        sender_name=request.sender_name or "",
        sender_id=request.sender_id or "",
        activity_type=result.activity_type or "",
        activity_duration_minutes=result.activity_duration_minutes,
        calories_burned=result.calories_burned,
        activity_summary=result.activity_summary or "",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def has_processed_message(db: Session, source_message_id: str) -> bool:
    if not source_message_id:
        return False
    existing = db.scalar(select(ScoreLog.id).where(ScoreLog.source_message_id == source_message_id).limit(1))
    return existing is not None


def query_user_score(db: Session, sender_id: str, since: str | None = None) -> int:
    if not sender_id:
        return 0
    query = select(func.coalesce(func.sum(ScoreLog.score_delta), 0)).where(ScoreLog.sender_id == sender_id)
    if since:
        query = query.where(ScoreLog.bstudio_create_time >= since)
    score = db.scalar(query)
    return int(score or 0)


def query_user_activity_memory(db: Session, sender_id: str, limit: int = 5) -> list[str]:
    if not sender_id:
        return []
    rows = db.execute(
        select(
            ScoreLog.bstudio_create_time,
            ScoreLog.activity_type,
            ScoreLog.activity_duration_minutes,
            ScoreLog.calories_burned,
            ScoreLog.activity_summary,
            ScoreLog.note,
        )
        .where(ScoreLog.sender_id == sender_id)
        .where(ScoreLog.activity_type != "")
        .order_by(ScoreLog.bstudio_create_time.desc())
        .limit(limit)
    ).all()
    memory: list[str] = []
    for row in rows:
        parts = [row.bstudio_create_time.strftime("%m-%d"), row.activity_type or row.activity_summary or row.note]
        if row.activity_duration_minutes:
            parts.append(f"{int(row.activity_duration_minutes)}\u5206\u949f")
        if row.calories_burned:
            parts.append(f"\u7ea6{int(row.calories_burned)}\u5343\u5361")
        memory.append("\uff0c".join(parts))
    return memory


def query_recent_activity_types(db: Session, sender_id: str, limit: int = 2) -> list[str]:
    if not sender_id:
        return []
    rows = db.execute(
        select(ScoreLog.activity_type, ScoreLog.activity_summary, ScoreLog.note)
        .where(ScoreLog.sender_id == sender_id)
        .where(ScoreLog.score_delta > 0)
        .order_by(ScoreLog.bstudio_create_time.desc())
        .limit(limit)
    ).all()
    return [str(row.activity_type or row.activity_summary or row.note or "") for row in rows]


def _activity_training_category(activity_text: str) -> str:
    text = activity_text or ""
    has_cardio = any(keyword in text for keyword in ("\u8dd1", "\u9a91", "\u6e38\u6cf3", "\u692d\u5706", "\u6709\u6c27", "\u8df3\u7ef3", "\u722c\u5761", "\u5feb\u8d70", "\u6162\u8dd1"))
    has_strength = any(
        keyword in text
        for keyword in (
            "\u529b\u91cf",
            "\u8bad\u7ec3",
            "\u5065\u8eab",
            "\u80f8",
            "\u80cc",
            "\u80a9",
            "\u81c2",
            "\u817f",
            "\u81c0",
            "\u5367\u63a8",
            "\u5212\u8239",
            "\u6df1\u8e72",
            "\u786c\u62c9",
            "\u65e0\u6c27",
        )
    )
    if has_cardio and has_strength:
        return "mixed"
    if any(keyword in text for keyword in ("\u80f8", "\u80cc", "\u80a9", "\u81c2", "\u4e8c\u5934", "\u4e09\u5934", "\u5367\u63a8", "\u5212\u8239", "\u5f15\u4f53", "\u4e0a\u80a2")):
        return "upper_strength"
    if any(keyword in text for keyword in ("\u817f", "\u81c0", "\u6df1\u8e72", "\u786c\u62c9", "\u4e0b\u80a2")):
        return "lower_strength"
    if has_cardio:
        return "cardio"
    if any(keyword in text for keyword in ("\u745c\u4f3d", "\u666e\u62c9\u63d0", "\u62c9\u4f38", "\u6062\u590d")):
        return "mobility"
    if any(keyword in text for keyword in ("\u529b\u91cf", "\u8bad\u7ec3", "\u5065\u8eab")):
        return "strength"
    return "other"


def build_training_tip(result: GradeResult, recent_activity_types: list[str]) -> str:
    if result.score <= 0:
        return ""

    tips: list[str] = []
    categories = [_activity_training_category(item) for item in [result.activity_type or result.activity_summary or result.note, *recent_activity_types]]
    current_category = categories[0] if categories else "other"
    recent_categories = categories[:4]
    if current_category == "mixed":
        tips.append("\u8fd9\u6b21\u6709\u6c27+\u65e0\u6c27\u90fd\u6709\uff0c\u642d\u914d\u633a\u50cf\u6837\uff1b\u8bb0\u5f97\u628a\u70ed\u8eab\u3001\u62c9\u4f38\u548c\u86cb\u767d\u8d28\u4e5f\u7b97\u8fdb\u8bad\u7ec3\u91cc\uff0c\u522b\u53ea\u8bb0\u5f97\u51b2\u91cf \U0001f4aa")
    if sum(category == "cardio" for category in recent_categories) >= 3:
        tips.append("\u4f60\u6700\u8fd1\u6709\u6c27\u51fa\u73b0\u5f97\u6709\u70b9\u9891\u7e41\uff0c\u662f\u5728\u51cf\u8102\u671f\u5417\uff1f\u8bb0\u5f97\u8425\u517b\u5747\u8861\uff0c\u78b3\u6c34\u548c\u86cb\u767d\u8d28\u522b\u538b\u592a\u72e0\uff0c\u522b\u628a\u8eab\u4f53\u7ec3\u6210\u7701\u7535\u6a21\u5f0f \U0001f331")
    strength_like_count = sum(category in {"upper_strength", "lower_strength", "strength"} for category in recent_categories)
    if strength_like_count >= 3:
        tips.append("\u6700\u8fd1\u529b\u91cf/\u65e0\u6c27\u504f\u591a\uff0c\u808c\u8089\u662f\u60f3\u53d8\u5f3a\uff0c\u4f46\u5173\u8282\u3001\u62c9\u4f38\u548c\u7761\u7720\u4e5f\u8981\u6709\u6392\u9762\uff1b\u75bc\u75db\u5c31\u522b\u786c\u626b\u91cf \U0001f63a")
    if categories[:3] == ["upper_strength", "upper_strength", "upper_strength"]:
        tips.append("\u6211\u591a\u5634\u4e00\u53e5\uff1a\u4f60\u6700\u8fd1\u6709\u70b9\u4e0a\u80a2\u8fde\u8f74\u8f6c\u4e86\uff0c\u4e0b\u6b21\u53ef\u4ee5\u6362\u4e2a\u4e0b\u80a2\u3001\u6838\u5fc3\u6216\u8f7b\u6709\u6c27\uff0c\u7ed9\u80a9\u8098\u8155\u653e\u4e2a\u5c0f\u5047 \U0001f331")

    duration = result.activity_duration_minutes or 0
    calories = result.calories_burned or 0
    calories_per_minute = calories / duration if duration else 0
    if duration >= 90 or calories >= 800 or calories_per_minute >= 12:
        tips.append("\u8fd9\u6b21\u91cf\u4e0d\u5c0f\uff0c\u6536\u64cd\u522b\u7701\uff0c\u62c9\u4f38\u548c\u8865\u6c34\u5b89\u6392\u4e00\u4e0b\uff0c\u522b\u8ba9\u660e\u5929\u7684\u8eab\u4f53\u6765\u7fa4\u91cc\u6295\u8bc9 \U0001f4aa")
    elif duration >= 60 or calories >= 500:
        tips.append("\u8fd9\u6b21\u8bad\u7ec3\u91cf\u633a\u5b9e\u5728\uff0c\u540e\u9762\u8bb0\u5f97\u62c9\u4f38\u8865\u6c34\uff1b\u4e0b\u4e00\u7ec3\u770b\u75b2\u52b3\u611f\uff0c\u522b\u786c\u521a \U0001f63a")

    return "\n".join(tips[:2])


async def grade_request(settings: Settings, db: Session, request: GradingRequest) -> GradingResponse:
    workflow = get_workflow_config()
    responses = workflow.responses
    text = request.input.strip()
    if not text and not request.picture:
        return GradingResponse(output=responses.no_input, score=0, note="\u65e0\u6709\u6548\u8f93\u5165", inserted=False)

    intent = detect_intent(text)
    if intent == Intent.UNSUPPORTED:
        return GradingResponse(output=responses.unsupported, score=0, note="\u4e0d\u652f\u6301\u8981\u6c42", inserted=False)
    if intent == Intent.CLAIM_SCORE:
        return GradingResponse(output=responses.claim_score, score=0, note="\u4e3b\u5f20\u5206\u6570", inserted=False)
    if intent == Intent.QUERY_OWN_SCORE:
        score = query_user_score(db, request.sender_id, settings.default_season_start)
        output = responses.query_own_score.format(score=score)
        return GradingResponse(output=output, score=0, note="\u67e5\u8be2\u672c\u4eba\u79ef\u5206", inserted=False)
    if intent == Intent.HEALTH_ADVICE:
        user_activity_memory = query_user_activity_memory(db, request.sender_id)
        output = await answer_health_question(settings, text, user_activity_memory=user_activity_memory, user_name=request.sender_name)
        weather_tip = await get_weather_training_tip(settings, request.message_created_at, text)
        if weather_tip:
            output = f"{output}\n{weather_tip}"
        return GradingResponse(output=output, score=0, note="\u5065\u5eb7\u95ee\u7b54", inserted=False)
    if has_processed_message(db, request.source_message_id):
        return GradingResponse(output="\u8fd9\u6761\u6253\u5361\u5df2\u7ecf\u8bb0\u5f55\u8fc7\u5566\uff0c\u907f\u514d\u91cd\u590d\u52a0\u5206\u3002", score=0, note="\u91cd\u590d\u6d88\u606f", inserted=False)

    user_activity_memory = query_user_activity_memory(db, request.sender_id)
    recent_activity_types = query_recent_activity_types(db, request.sender_id)
    result = await grade_with_llm(settings, text, request.picture, user_activity_memory=user_activity_memory, user_name=request.sender_name)
    add_score_log(db, request, result)

    suffixes: list[str] = []
    if result.score > 0:
        suffixes.append(responses.positive_suffix.format(score=result.score))
    else:
        suffixes.append(responses.zero_suffix)
    if result.calories_burned:
        suffixes.append(responses.calorie_suffix.format(calories=result.calories_burned))
    training_tip = build_training_tip(result, recent_activity_types)
    if training_tip:
        suffixes.append(training_tip)
    weather_tip = await get_weather_training_tip(settings, request.message_created_at, text)
    if weather_tip:
        suffixes.append(weather_tip)

    output = f"{result.output}\n" + "\n".join(suffixes)

    return GradingResponse(
        output=output,
        score=result.score,
        note=result.note,
        inserted=True,
        activity_type=result.activity_type or "",
        activity_duration_minutes=result.activity_duration_minutes,
        calories_burned=result.calories_burned,
        activity_summary=result.activity_summary or "",
    )


def _leaderboard_query(since: str) -> Select:
    return (
        select(
            ScoreLog.sender_id,
            func.max(ScoreLog.sender_name).label("sender_name"),
            func.sum(ScoreLog.score_delta).label("score"),
        )
        .where(ScoreLog.sender_id != "")
        .where(ScoreLog.bstudio_create_time >= since)
        .group_by(ScoreLog.sender_id)
        .having(func.sum(ScoreLog.score_delta) > 0)
        .order_by(func.sum(ScoreLog.score_delta).desc())
    )


def generate_report(db: Session, since: str) -> ReportResponse:
    report_config = get_workflow_config().report
    rows = db.execute(_leaderboard_query(since)).all()
    title = report_config.title_template.format(since=since)
    if not rows:
        return ReportResponse(title=title, markdown=f"{title}\n\n{report_config.empty_message}")

    lines = [title, "", "| \u6392\u540d | \u59d3\u540d | \u5206\u6570 |", "| --- | --- | ---: |"]
    previous_score: int | None = None
    previous_rank = 0
    for index, row in enumerate(rows, start=1):
        score = int(row.score)
        rank = previous_rank if score == previous_score else index
        name = row.sender_name or row.sender_id
        lines.append(f"| {rank} | {name} | {score} |")
        previous_score = score
        previous_rank = rank

    return ReportResponse(title=title, markdown="\n".join(lines))


def current_week_start(today: date | None = None) -> date:
    current = today or date.today()
    return current - timedelta(days=current.weekday())


def generate_weekly_calorie_report(db: Session, week_start: str | None = None) -> ReportResponse:
    report_config = get_workflow_config().report
    start_date = datetime.strptime(week_start, "%Y-%m-%d").date() if week_start else current_week_start()
    end_date = start_date + timedelta(days=6)
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_exclusive = datetime.combine(end_date + timedelta(days=1), datetime.min.time())

    rows = db.execute(
        select(
            ScoreLog.sender_id,
            func.max(ScoreLog.sender_name).label("sender_name"),
            func.coalesce(func.sum(ScoreLog.calories_burned), 0).label("calories"),
            func.count(ScoreLog.id).label("checkin_count"),
        )
        .where(ScoreLog.sender_id != "")
        .where(ScoreLog.bstudio_create_time >= start_dt)
        .where(ScoreLog.bstudio_create_time < end_exclusive)
        .where(ScoreLog.calories_burned.is_not(None))
        .where(ScoreLog.calories_burned > 0)
        .group_by(ScoreLog.sender_id)
        .order_by(func.sum(ScoreLog.calories_burned).desc())
    ).all()

    title = report_config.weekly_calorie_title_template.format(
        week_start=start_date.isoformat(),
        week_end=end_date.isoformat(),
    )
    if not rows:
        return ReportResponse(title=title, markdown=f"{title}\n\n{report_config.weekly_calorie_empty_message}")

    lines = [title, "", "| \u6392\u540d | \u59d3\u540d | \u4f30\u7b97\u6d88\u8017 | \u6253\u5361\u6b21\u6570 |", "| --- | --- | ---: | ---: |"]
    previous_calories: int | None = None
    previous_rank = 0
    for index, row in enumerate(rows, start=1):
        calories = int(row.calories or 0)
        rank = previous_rank if calories == previous_calories else index
        name = row.sender_name or row.sender_id
        lines.append(f"| {rank} | {name} | {calories} \u5343\u5361 | {int(row.checkin_count)} |")
        previous_calories = calories
        previous_rank = rank

    return ReportResponse(title=title, markdown="\n".join(lines))
