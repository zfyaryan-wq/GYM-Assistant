from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "GYM-Assistant"
    env: str = "local"
    database_url: str = "sqlite:///./data/app.db"
    default_season_start: str = "2025-05-11"
    public_base_url: str = ""

    llm_base_url: str = "https://api.example.com/v1"
    llm_api_key: str = ""
    llm_model: str = "glm-4.6v"
    llm_provider_id: str = ""
    llm_timeout_seconds: float = 120

    feishu_app_id: str = ""
    feishu_app_secret: str = Field(default="", repr=False)
    feishu_verification_token: str = Field(default="", repr=False)
    feishu_encrypt_key: str = Field(default="", repr=False)
    feishu_report_chat_id: str = ""

    @property
    def chat_completions_url(self) -> str:
        return f"{self.llm_base_url.rstrip('/')}/chat/completions"


@lru_cache
def get_settings() -> Settings:
    return Settings()
