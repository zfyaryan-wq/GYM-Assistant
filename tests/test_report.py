from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, ScoreLog
from app.main import _extract_message, _is_report_command, _is_weekly_report_command, _verify_feishu_token
from app.services import generate_report, generate_weekly_calorie_report, query_user_activity_memory, query_user_score
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


def test_extract_text_and_image_message() -> None:
    event = {
        "sender": {"sender_id": {"open_id": "ou_xxx"}},
        "message": {
            "message_id": "om_xxx",
            "content": '{"text":"今天跑步 5 公里","image_key":"img_xxx"}',
        },
    }

    assert _extract_message(event) == ("om_xxx", "ou_xxx", "今天跑步 5 公里", "img_xxx")


def test_extract_rich_text_message() -> None:
    event = {
        "message": {
            "content": (
                '{"post":{"zh_cn":{"content":'
                '[[{"tag":"text","text":"今天骑行 "},{"tag":"text","text":"10 公里"}]]}}}'
            )
        }
    }

    assert _extract_message(event) == ("", "", "今天骑行 10 公里", "")


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


def test_workflow_config_loads_editable_fields() -> None:
    workflow = get_workflow_config()

    assert "排行榜" in workflow.intent_keywords.report_command
    assert "周报" in workflow.intent_keywords.weekly_report_command
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
