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
                    sender_name="\u7532",
                ),
                ScoreLog(
                    id="2",
                    bstudio_create_time=datetime(2025, 5, 12, 8, 0, 0),
                    score_delta=3,
                    sender_id="u2",
                    sender_name="\u4e59",
                ),
                ScoreLog(
                    id="3",
                    bstudio_create_time=datetime(2025, 5, 12, 8, 0, 0),
                    score_delta=1,
                    sender_id="u3",
                    sender_name="\u4e19",
                ),
            ]
        )
        db.commit()

        report = generate_report(db, "2025-05-11")

    assert "| 1 | \u7532 | 3 |" in report.markdown
    assert "| 1 | \u4e59 | 3 |" in report.markdown
    assert "| 3 | \u4e19 | 1 |" in report.markdown


def test_report_command_detection() -> None:
    assert detect_intent("\u6211\u7684\u79ef\u5206") == Intent.QUERY_OWN_SCORE
    assert detect_intent("\u67e5\u79ef\u5206") == Intent.QUERY_OWN_SCORE
    for command in ["\u6392\u884c\u699c", "\u79ef\u5206\u699c", "\u6392\u540d", "\u65e5\u62a5"]:
        assert _is_report_command(command)
    for command in ["\u5468\u62a5", "\u672c\u5468\u603b\u7ed3", "\u672c\u5468\u6d88\u8017", "\u5361\u8def\u91cc\u5468\u62a5"]:
        assert _is_weekly_report_command(command)
    assert _is_report_command("\u53d1\u4e00\u4e0b\u79ef\u5206\u699c")
    assert _is_report_command("\u4eca\u65e5\u6392\u884c\u699c")
    assert not _is_report_command("\u6211\u7684\u79ef\u5206\u662f\u591a\u5c11")
    assert _is_weekly_report_command("\u53d1\u4e00\u4e0b\u672c\u5468\u603b\u7ed3")
    assert _is_weekly_report_command("\u5361\u8def\u91cc\u5468\u62a5")
    assert detect_intent("\u8dd1\u6b65 30 \u5206\u949f\u5927\u6982\u6d88\u8017\u591a\u5c11\u5361\uff1f") == Intent.HEALTH_ADVICE
    assert detect_intent("\u5361\u8def\u91cc\u600e\u4e48\u7b97\u7684\uff1f") == Intent.HEALTH_ADVICE
    assert detect_intent("\u51cf\u8102\u671f\u665a\u996d\u600e\u4e48\u5403\u6bd4\u8f83\u597d\uff1f") == Intent.HEALTH_ADVICE
    assert detect_intent("\u4eca\u5929\u8dd1\u6b65 30 \u5206\u949f") == Intent.NORMAL


def test_extract_text_and_image_message() -> None:
    event = {
        "sender": {"sender_id": {"open_id": "ou_xxx"}},
        "message": {
            "message_id": "om_xxx",
            "chat_id": "oc_xxx",
            "chat_type": "group",
            "content": '{"text":"\u4eca\u5929\u8dd1\u6b65 5 \u516c\u91cc","image_key":"img_xxx"}',
        },
    }

    assert _extract_message(event) == ("om_xxx", "ou_xxx", "\u4eca\u5929\u8dd1\u6b65 5 \u516c\u91cc", "img_xxx", "oc_xxx", "group")


def test_extract_rich_text_message() -> None:
    event = {
        "message": {
            "content": (
                '{"post":{"zh_cn":{"content":'
                '[[{"tag":"text","text":"\u4eca\u5929\u9a91\u884c "},{"tag":"text","text":"10 \u516c\u91cc"}]]}}}'
            )
        }
    }

    assert _extract_message(event) == ("", "", "\u4eca\u5929\u9a91\u884c 10 \u516c\u91cc", "", "", "")


def test_friendly_name_and_image_only_prompt() -> None:
    assert _friendly_name("\u5f20\u65ed\u9633") == "\u65ed\u9633"
    assert _friendly_name("Alice") == "Alice"
    assert "\u65ed\u9633" in _user_context_text([], "\u5f20\u65ed\u9633")
    content = _message_content("", "http://example.com/run.jpg")
    assert content[0]["type"] == "text"
    assert "\u56fe\u7247\u5185\u5bb9" in content[0]["text"]
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
                    sender_name="\u7532",
                ),
                ScoreLog(
                    id="new",
                    bstudio_create_time=datetime(2025, 5, 12, 8, 0, 0),
                    score_delta=3,
                    sender_id="u1",
                    sender_name="\u7532",
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
                sender_name="\u7532",
                source_message_id="om_1",
            )
        )
        db.commit()

        assert has_processed_message(db, "om_1")
        assert not has_processed_message(db, "om_2")


def test_training_tip_flags_balance_and_high_volume() -> None:
    result = GradeResult(
        output="\u4e0d\u9519",
        score=3,
        note="\u80f8\u8bad90\u5206\u949f",
        activity_type="\u80f8\u90e8\u529b\u91cf",
        activity_duration_minutes=95,
        calories_burned=850,
        activity_summary="\u80f8\u90e8\u529b\u91cf",
    )

    tip = build_training_tip(result, ["\u80a9\u90e8\u529b\u91cf", "\u4e0a\u80a2\u529b\u91cf"])

    assert "\u4e0a\u80a2" in tip
    assert "\u4e0b\u80a2" in tip
    assert "\u62c9\u4f38" in tip


def test_workflow_config_loads_editable_fields() -> None:
    workflow = get_workflow_config()

    assert "\u6392\u884c\u699c" in workflow.intent_keywords.report_command
    assert "\u5468\u62a5" in workflow.intent_keywords.weekly_report_command
    assert "\u5361\u8def\u91cc" in workflow.intent_keywords.health_advice
    assert "\u516c\u5f0f" in workflow.intent_keywords.health_advice
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
                    sender_name="\u7532",
                    activity_type="\u8dd1\u6b65",
                    calories_burned=300,
                ),
                ScoreLog(
                    id="2",
                    bstudio_create_time=datetime(2026, 7, 7, 8, 0, 0),
                    score_delta=3,
                    sender_id="u2",
                    sender_name="\u4e59",
                    activity_type="\u9a91\u884c",
                    calories_burned=500,
                ),
                ScoreLog(
                    id="3",
                    bstudio_create_time=datetime(2026, 6, 30, 8, 0, 0),
                    score_delta=3,
                    sender_id="u1",
                    sender_name="\u7532",
                    activity_type="\u8dd1\u6b65",
                    calories_burned=999,
                ),
            ]
        )
        db.commit()

        report = generate_weekly_calorie_report(db, "2026-07-06")

    assert "| 1 | \u4e59 | 500 \u5343\u5361 | 1 |" in report.markdown
    assert "| 2 | \u7532 | 300 \u5343\u5361 | 1 |" in report.markdown
    assert "999 \u5343\u5361" not in report.markdown


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
                sender_name="\u7532",
                activity_type="\u7ec3\u817f",
                activity_duration_minutes=45,
                calories_burned=260,
                activity_summary="\u529b\u91cf\u8bad\u7ec3",
            )
        )
        db.commit()

        memory = query_user_activity_memory(db, "u1")

    assert memory == ["07-08\uff0c\u7ec3\u817f\uff0c45\u5206\u949f\uff0c\u7ea6260\u5343\u5361"]
