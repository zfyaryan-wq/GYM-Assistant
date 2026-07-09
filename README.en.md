# GYM-Assistant

<p align="center">
  <img src="assets/logo.png" alt="GYM-Assistant logo" width="240">
</p>

[中文](README.md)

A self-hosted Feishu bot backend for health check-ins, wellness scoring, calorie estimation, leaderboards, and weekly reports.

## Features

- `POST /api/grading`: scores text/image health check-ins and writes score logs.
- `GET /api/report`: generates a Markdown season leaderboard from score logs.
- `GET /api/weekly-calories`: generates a weekly calorie report.
- `POST /api/feishu/events`: Feishu event callback endpoint for text, image, and rich-text messages.
- Group commands such as `排行榜`, `积分榜`, `排名`, and `日报` return the current season leaderboard.
- Group commands such as `周报`, `本周总结`, `本周消耗`, and `卡路里周报` return the weekly calorie report.
- Each workout check-in estimates calories burned and uses recent user activity memory to produce a more natural reply.
- `scripts/import_csv.py` imports historical check-in log CSV files.

## Technical Architecture

```mermaid
flowchart LR
    user[Feishu group user] --> feishu[Feishu Open Platform]
    feishu --> webhook[FastAPI callback<br/>/api/feishu/events]

    webhook --> parser[Message parser<br/>text / image / rich text]
    parser --> intent[Intent detection<br/>check-in / my score / leaderboard / weekly report]

    intent --> grading[Scoring and calorie estimation]
    grading --> memory[Recent user activity memory]
    memory --> llm[OpenAI-compatible multimodal model]
    llm --> log[(score_logs)]

    intent --> score[Personal score query]
    intent --> leaderboard[Season leaderboard]
    intent --> weekly[Weekly calorie report]

    score --> log
    leaderboard --> log
    weekly --> log

    log --> reply[Feishu message reply]
    llm --> reply
    reply --> feishu
```

Core modules:

- `app/main.py`: HTTP API and Feishu event entry point.
- `app/services.py`: scoring, score queries, leaderboards, weekly reports, and user activity memory.
- `app/llm.py`: calls an OpenAI-compatible multimodal model and parses structured grading results.
- `app/db.py`: SQLAlchemy models and SQLite/PostgreSQL connection setup.
- `app/workflow_config.json`: editable keywords, responses, scoring rules, and report templates.
- `scripts/import_csv.py`: imports historical check-in logs.

## Local Development

```powershell
cd GYM-Assistant
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
```

Edit `.env` and fill in real values:

```env
LLM_API_KEY=your model API key
LLM_MODEL=glm-4.6v
FEISHU_APP_ID=your Feishu app ID
FEISHU_APP_SECRET=your Feishu app secret
FEISHU_VERIFICATION_TOKEN=your Feishu verification token
```

Optionally import historical CSV data:

```powershell
python scripts\import_csv.py ".\path\to\checkin_logs.csv"
```

Start the API:

```powershell
uvicorn app.main:app --reload --port 8000
```

Smoke test:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod "http://127.0.0.1:8000/api/report"
Invoke-RestMethod "http://127.0.0.1:8000/api/weekly-calories"
```

Manual grading test:

```powershell
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/api/grading" `
  -ContentType "application/json" `
  -Body '{"input":"Ran 5 km today","sender_id":"test_user","sender_name":"Test User"}'
```

## Feishu Setup

Create and configure a Feishu app:

- Event callback URL: `https://your-domain.example/api/feishu/events`
- Subscribe to the message receive event: `im.message.receive_v1`
- Enable the permissions needed to send messages, read message resources, and read basic user information.
- Do not enable event encryption unless the service is updated to decrypt encrypted events.

Bot behavior:

- Normal text or image check-ins are scored by the model and written to `score_logs`.
- `我的积分` and `查积分` return the user's current season score.
- `排行榜`, `积分榜`, `排名`, and `日报` return the current season leaderboard.
- `周报`, `本周总结`, `本周消耗`, and `卡路里周报` return the weekly calorie report.
- Attempts to reveal rules, alter scores, or query other users' scores are rejected.

## Customizing The Workflow

Most workflow changes should be made in `app/workflow_config.json` instead of Python code:

- `intent_keywords`: command words and rejection keywords.
- `responses`: refusal text, empty-input text, score reply text, and suffixes.
- `report`: leaderboard and weekly report titles and empty-state messages.
- `grading_prompt`: scoring rules, calorie estimation rules, violation rules, and reply requirements.

`DEFAULT_SEASON_START` is configured in `.env`. It controls the current season start date. Both the leaderboard and "my score" query use this date.

Calorie estimates are stored in `score_logs.calories_burned`. They are intended for activity encouragement and weekly summaries, not medical or precise exercise measurements.

## Docker

Build directly from the included `Dockerfile`. PostgreSQL is recommended for production:

```env
DATABASE_URL=postgresql+psycopg://user:password@db-host:5432/db-name
DEFAULT_SEASON_START=2025-05-11
LLM_BASE_URL=https://api.example.com/v1
LLM_API_KEY=your API key
LLM_MODEL=glm-4.6v
LLM_PROVIDER_ID=
FEISHU_APP_ID=your Feishu app ID
FEISHU_APP_SECRET=your Feishu app secret
FEISHU_VERIFICATION_TOKEN=your Feishu verification token
```

Start command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Pre-Production Checklist

- Do not commit `.env`, databases, CSV exports, logs, or real secrets.
- Rotate any secret that has appeared in chat, logs, screenshots, or terminal output.
- Test text check-ins, image check-ins, "my score", leaderboard, and weekly report commands in a test group.
- Put the service behind Nginx or another gateway and enable HTTPS. Feishu event callbacks require a publicly reachable callback URL.
