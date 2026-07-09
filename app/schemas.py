from pydantic import BaseModel, Field


class GradingRequest(BaseModel):
    input: str = ""
    picture: str | None = Field(default=None, description="Image URL or data URI/base64 payload.")
    sender_id: str = ""
    sender_name: str = ""
    sys_platform: str = "10000011"
    source_message_id: str = ""


class GradingResponse(BaseModel):
    output: str
    score: int
    note: str
    inserted: bool = False
    activity_type: str = ""
    activity_duration_minutes: int | None = None
    calories_burned: int | None = None
    activity_summary: str = ""


class ReportResponse(BaseModel):
    title: str
    markdown: str


class HealthResponse(BaseModel):
    status: str
