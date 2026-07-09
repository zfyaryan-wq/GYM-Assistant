from datetime import date, datetime, timedelta
from uuid import uuid4

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import ScoreLog
from app.intent import Intent, detect_intent
from app.llm import GradeResult, answer_health_question, grade_with_llm
from app.schemas import GradingRequest, GradingResponse, ReportResponse
from app.workflow_config import get_workflow_config


def _new_log_id() -> str:
    return str(int(datetime.now().timestamp() * 1000)) + uuid4().hex[:8]


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
            parts.append(f"{int(row.activity_duration_minutes)}分钟")
        if row.calories_burned:
            parts.append(f"约{int(row.calories_burned)}千卡")
        memory.append("，".join(parts))
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
    if any(keyword in text for keyword in ("胸", "背", "肩", "臂", "二头", "三头", "卧推", "划船", "引体", "上肢")):
        return "upper_strength"
    if any(keyword in text for keyword in ("腿", "臀", "深蹲", "硬拉", "下肢")):
        return "lower_strength"
    if any(keyword in text for keyword in ("跑", "骑", "游泳", "椭圆", "有氧", "跳绳", "爬坡")):
        return "cardio"
    if any(keyword in text for keyword in ("瑜伽", "普拉提", "拉伸", "恢复")):
        return "mobility"
    if any(keyword in text for keyword in ("力量", "训练", "健身")):
        return "strength"
    return "other"


def build_training_tip(result: GradeResult, recent_activity_types: list[str]) -> str:
    if result.score <= 0:
        return ""

    tips: list[str] = []
    categories = [_activity_training_category(item) for item in [result.activity_type or result.activity_summary or result.note, *recent_activity_types]]
    if categories[:3] == ["upper_strength", "upper_strength", "upper_strength"]:
        tips.append("我多嘴一句：你最近有点上肢连轴转了，下次可以换个下肢、核心或轻有氧，给肩肘腕放个小假。")

    duration = result.activity_duration_minutes or 0
    calories = result.calories_burned or 0
    calories_per_minute = calories / duration if duration else 0
    if duration >= 90 or calories >= 800 or calories_per_minute >= 12:
        tips.append("这次量不小，收操别省，拉伸和补水安排一下，别让明天的身体来群里投诉。")
    elif duration >= 60 or calories >= 500:
        tips.append("这次训练量挺实在，后面记得拉伸补水；下一练看疲劳感，别硬刚。")

    return "\n".join(tips[:2])


async def grade_request(settings: Settings, db: Session, request: GradingRequest) -> GradingResponse:
    workflow = get_workflow_config()
    responses = workflow.responses
    text = request.input.strip()
    if not text and not request.picture:
        return GradingResponse(output=responses.no_input, score=0, note="无有效输入", inserted=False)

    intent = detect_intent(text)
    if intent == Intent.UNSUPPORTED:
        return GradingResponse(output=responses.unsupported, score=0, note="不支持要求", inserted=False)
    if intent == Intent.CLAIM_SCORE:
        return GradingResponse(output=responses.claim_score, score=0, note="主张分数", inserted=False)
    if intent == Intent.QUERY_OWN_SCORE:
        score = query_user_score(db, request.sender_id, settings.default_season_start)
        output = responses.query_own_score.format(score=score)
        return GradingResponse(output=output, score=0, note="查询本人积分", inserted=False)
    if intent == Intent.HEALTH_ADVICE:
        user_activity_memory = query_user_activity_memory(db, request.sender_id)
        output = await answer_health_question(settings, text, user_activity_memory=user_activity_memory, user_name=request.sender_name)
        return GradingResponse(output=output, score=0, note="健康问答", inserted=False)
    if has_processed_message(db, request.source_message_id):
        return GradingResponse(output="这条打卡已经记录过啦，避免重复加分。", score=0, note="重复消息", inserted=False)

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

    lines = [title, "", "| 排名 | 姓名 | 分数 |", "| --- | --- | ---: |"]
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

    lines = [title, "", "| 排名 | 姓名 | 估算消耗 | 打卡次数 |", "| --- | --- | ---: | ---: |"]
    previous_calories: int | None = None
    previous_rank = 0
    for index, row in enumerate(rows, start=1):
        calories = int(row.calories or 0)
        rank = previous_rank if calories == previous_calories else index
        name = row.sender_name or row.sender_id
        lines.append(f"| {rank} | {name} | {calories} 千卡 | {int(row.checkin_count)} |")
        previous_calories = calories
        previous_rank = rank

    return ReportResponse(title=title, markdown="\n".join(lines))
