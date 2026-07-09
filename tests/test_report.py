from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, ScoreLog
from app.intent import Intent, detect_intent
from app.llm import GradeResult, _friendly_name, _message_content, _user_context_text
from app.main import _extract_message, _is_report_command, _is_weekly_report_command, _verify_feishu_token
from app.services import build_training_tip, generate_report, generate_weekly_calorie_report, has_processed_message, query_user_activity_memory, query_user_score
from app.config import Settings
from app.workflow_config import get_workflow_config


def test_report_uses_competition_ranking() -> None:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as db:
        db.add_all(
            [
                ScoreLog(
                    id="1",
                    bstudio_create_time=datetime(2025, 5, 12, 8, 0, 0),
                    score_delta=3,
                    sender_id="u1",
                    sender_name="甲",
                ),
                ScoreLog(
                    id="2",
                    bstudio_create_time=datetime(2025, 5, 12, 8, 0, 0),
                    score_delta=3,
                    sender_id="u2",
                    sender_name="乙",
                ),
                ScoreLog(
                    id="3",
                    bstudio_create_time=datetime(2025, 5, 12, 8, 0, 0),
                    score_delta=1,
                    sender_id="u3",
                    sender_name="丙",
                ),
            ]
        )
        db.commit()

        report = generate_report(db, "2025-05-11")

    assert "| 1 | 甲 | 3 |" in report.markdown
    assert "| 1 | 乙 | 3 |" in report.markdown
    assert "| 3 | 丙 | 1 |" in report.markdown


def test_report_command_detection() -> None:
    assert _is_report_command("发一下积分榜")
    assert _is_report_command("今日排行榜")
    assert not _is_report_command("我的积分是多少")
    assert _is_weekly_report_command("发一下本周总结")
    assert _is_weekly_report_command("卡路里周报")
    assert detect_intent("跑步 30 分钟大概消耗多少卡？") == Intent.HEALTH_ADVICE
    assert detect_intent("卡路里怎么算的？") == Intent.HEALTH_ADVICE
    assert detect_intent("减脂期晚饭怎么吃比较好？") == Intent.HEALTH_ADVICE
    assert detect_intent("今天跑步 30 分钟") == Intent.NORMAL


def test_extract_text_and_image_message() -> None:
    event = {
        "sender": {"sender_id": {"open_id": "ou_xxx"}},
        "message": {
            "message_id": "om_xxx",
            "chat_id": "oc_xxx",
            "chat_type": "group",
            "content": '{"text":"今天跑步 5 公里","image_key":"img_xxx"}',
        },
    }

    assert _extract_message(event) == ("om_xxx", "ou_xxx", "今天跑步 5 公里", "img_xxx", "oc_xxx", "group")


def test_extract_rich_text_message() -> None:
    event = {
        "message": {
            "content": (
                '{"post":{"zh_cn":{"content":'
                '[[{"tag":"text","text":"今天骑行 "},{"tag":"text","text":"10 公里"}]]}}}'
            )
        }
    }

    assert _extract_message(event) == ("", "", "今天骑行 10 公里", "", "", "")


def test_friendly_name_and_image_only_prompt() -> None:
    assert _friendly_name("张旭阳") == "旭阳"
    assert _friendly_name("Alice") == "Alice"
    assert "旭阳" in _user_context_text([], "张旭阳")
    content = _message_content("", "http://example.com/run.jpg")
    assert content[0]["type"] == "text"
    assert "图片内容" in content[0]["text"]
    assert content[1]["type"] == "image_url"


def test_verify_feishu_token_accepts_header_token() -> None:
    settings = Settings(feishu_verification_token="expected")

    _verify_feishu_token(settings, {"header": {"token": "expected"}})


def test_user_score_uses_season_start_when_provided() -> None:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as db:
        db.add_all(
            [
                ScoreLog(
                    id="old",
                    bstudio_create_time=datetime(2025, 5, 1, 8, 0, 0),
                    score_delta=100,
                    sender_id="u1",
                    sender_name="甲",
                ),
                ScoreLog(
                    id="new",
                    bstudio_create_time=datetime(2025, 5, 12, 8, 0, 0),
                    score_delta=3,
                    sender_id="u1",
                    sender_name="甲",
                ),
            ]
        )
        db.commit()

        assert query_user_score(db, "u1", "2025-05-11") == 3


def test_report_groups_by_sender_id_when_name_changes() -> None:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as db:
        db.add_all(
            [
                ScoreLog(
                    id="1",
                    bstudio_create_time=datetime(2025, 5, 12, 8, 0, 0),
                    score_delta=3,
                    sender_id="u1",
                    sender_name="Alice A",
                ),
                ScoreLog(
                    id="2",
                    bstudio_create_time=datetime(2025, 5, 13, 8, 0, 0),
                    score_delta=1,
                    sender_id="u1",
                    sender_name="Alice B",
                ),
            ]
        )
        db.commit()

        report = generate_report(db, "2025-05-11")

    assert "| 1 | Alice B | 4 |" in report.markdown
    assert "Alice A | 3" not in report.markdown


def test_processed_message_detection() -> None:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as db:
        db.add(
            ScoreLog(
                id="1",
                bstudio_create_time=datetime(2025, 5, 12, 8, 0, 0),
                score_delta=3,
                sender_id="u1",
                sender_name="甲",
                source_message_id="om_1",
            )
        )
        db.commit()

        assert has_processed_message(db, "om_1")
        assert not has_processed_message(db, "om_2")


def test_training_tip_flags_balance_and_high_volume() -> None:
    result = GradeResult(
        output="不错",
        score=3,
        note="胸训90分钟",
        activity_type="胸部力量",
        activity_duration_minutes=95,
        calories_burned=850,
        activity_summary="胸部力量",
    )

    tip = build_training_tip(result, ["肩部力量", "上肢力量"])

    assert "上肢" in tip
    assert "下肢" in tip
    assert "拉伸" in tip


def test_workflow_config_loads_editable_fields() -> None:
    workflow = get_workflow_config()

    assert "排行榜" in workflow.intent_keywords.report_command
    assert "周报" in workflow.intent_keywords.weekly_report_command
    assert "卡路里" in workflow.intent_keywords.health_advice
    assert "公式" in workflow.intent_keywords.health_advice
    assert "{score}" in workflow.responses.query_own_score
    assert "{since}" in workflow.report.title_template


def test_weekly_calorie_report_ranks_by_calories() -> None:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as db:
        db.add_all(
            [
                ScoreLog(
                    id="1",
                    bstudio_create_time=datetime(2026, 7, 6, 8, 0, 0),
                    score_delta=3,
                    sender_id="u1",
                    sender_name="甲",
                    activity_type="跑步",
                    calories_burned=300,
                ),
                ScoreLog(
                    id="2",
                    bstudio_create_time=datetime(2026, 7, 7, 8, 0, 0),
                    score_delta=3,
                    sender_id="u2",
                    sender_name="乙",
                    activity_type="骑行",
                    calories_burned=500,
                ),
                ScoreLog(
                    id="3",
                    bstudio_create_time=datetime(2026, 6, 30, 8, 0, 0),
                    score_delta=3,
                    sender_id="u1",
                    sender_name="甲",
                    activity_type="跑步",
                    calories_burned=999,
                ),
            ]
        )
        db.commit()

        report = generate_weekly_calorie_report(db, "2026-07-06")

    assert "| 1 | 乙 | 500 千卡 | 1 |" in report.markdown
    assert "| 2 | 甲 | 300 千卡 | 1 |" in report.markdown
    assert "999 千卡" not in report.markdown


def test_user_activity_memory_uses_recent_activity_logs() -> None:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as db:
        db.add(
            ScoreLog(
                id="1",
                bstudio_create_time=datetime(2026, 7, 8, 8, 0, 0),
                score_delta=3,
                sender_id="u1",
                sender_name="甲",
                activity_type="练腿",
                activity_duration_minutes=45,
                calories_burned=260,
                activity_summary="力量训练",
            )
        )
        db.commit()

        memory = query_user_activity_memory(db, "u1")

    assert memory == ["07-08，练腿，45分钟，约260千卡"]
