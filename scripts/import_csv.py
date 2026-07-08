import argparse
import csv
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.db import ScoreLog, SessionLocal, init_db


def parse_datetime(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def import_csv(path: Path) -> int:
    init_db()
    count = 0
    with path.open("r", encoding="utf-8-sig", newline="") as file, SessionLocal() as db:
        reader = csv.DictReader(file)
        for row in reader:
            upsert_row(db, row)
            count += 1
        db.commit()
    return count


def upsert_row(db: Session, row: dict[str, str]) -> None:
    existing = db.get(ScoreLog, row["id"])
    payload = {
        "id": row["id"],
        "sys_platform": row.get("sys_platform") or None,
        "uuid": row.get("uuid") or None,
        "bstudio_create_time": parse_datetime(row["bstudio_create_time"]),
        "score_delta": int(row.get("score_delta") or 0),
        "note": row.get("note") or "",
        "sender_name": row.get("sender_name") or "",
        "sender_id": row.get("sender_id") or "",
    }
    if existing:
        for key, value in payload.items():
            setattr(existing, key, value)
    else:
        db.add(ScoreLog(**payload))


def main() -> None:
    parser = argparse.ArgumentParser(description="Import historical GYM-Assistant check-in CSV logs into the local database.")
    parser.add_argument("csv_path", type=Path)
    args = parser.parse_args()
    count = import_csv(args.csv_path)
    print(f"Imported {count} rows from {args.csv_path}")


if __name__ == "__main__":
    main()
