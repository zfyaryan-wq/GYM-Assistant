from collections.abc import Generator
from pathlib import Path

from sqlalchemy import DateTime, Integer, String, create_engine, func, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


class ScoreLog(Base):
    __tablename__ = "score_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sys_platform: Mapped[str | None] = mapped_column(String(32), nullable=True)
    uuid: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_message_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    bstudio_create_time: Mapped[DateTime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    score_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    sender_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    sender_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    activity_type: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    activity_duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    calories_burned: Mapped[int | None] = mapped_column(Integer, nullable=True)
    activity_summary: Mapped[str] = mapped_column(String(255), nullable=False, default="")


def _engine_args(database_url: str) -> dict:
    if database_url.startswith("sqlite"):
        if database_url.startswith("sqlite:///./"):
            db_path = Path(database_url.removeprefix("sqlite:///"))
            db_path.parent.mkdir(parents=True, exist_ok=True)
        return {"connect_args": {"check_same_thread": False}}
    return {}


settings = get_settings()
engine = create_engine(settings.database_url, pool_pre_ping=True, **_engine_args(settings.database_url))
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_score_log_columns()


def _ensure_score_log_columns() -> None:
    existing = {column["name"] for column in inspect(engine).get_columns(ScoreLog.__tablename__)}
    missing_columns = {
        "source_message_id": "VARCHAR(255) NOT NULL DEFAULT ''",
        "activity_type": "VARCHAR(255) NOT NULL DEFAULT ''",
        "activity_duration_minutes": "INTEGER",
        "calories_burned": "INTEGER",
        "activity_summary": "VARCHAR(255) NOT NULL DEFAULT ''",
    }
    with engine.begin() as connection:
        for name, definition in missing_columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE {ScoreLog.__tablename__} ADD COLUMN {name} {definition}"))


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
