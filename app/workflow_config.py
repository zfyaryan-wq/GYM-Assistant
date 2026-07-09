import json
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel


class IntentKeywords(BaseModel):
    unsupported: list[str]
    claim_score: list[str]
    query_own_score: list[str]
    health_advice: list[str]
    report_command: list[str]
    weekly_report_command: list[str]


class Responses(BaseModel):
    unsupported: str
    claim_score: str
    no_input: str
    query_own_score: str
    positive_suffix: str
    zero_suffix: str
    calorie_suffix: str


class ReportConfig(BaseModel):
    title_template: str
    empty_message: str
    weekly_calorie_title_template: str
    weekly_calorie_empty_message: str


class GradingPromptConfig(BaseModel):
    role: str
    scoring_rules: list[str]
    violation_rules: list[str]
    reply_requirements: list[str]
    note_requirement: str
    calorie_requirement: str


class WorkflowConfig(BaseModel):
    intent_keywords: IntentKeywords
    responses: Responses
    report: ReportConfig
    grading_prompt: GradingPromptConfig


@lru_cache
def get_workflow_config() -> WorkflowConfig:
    path = Path(__file__).with_name("workflow_config.json")
    return WorkflowConfig.model_validate_json(path.read_text(encoding="utf-8"))
