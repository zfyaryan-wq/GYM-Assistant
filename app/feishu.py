import base64
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import HTTPException

from app.config import Settings


logger = logging.getLogger(__name__)


async def get_tenant_access_token(settings: Settings) -> str:
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        raise HTTPException(status_code=503, detail="Feishu app credentials are not configured")

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": settings.feishu_app_id, "app_secret": settings.feishu_app_secret},
        )
    data = response.json()
    if data.get("code") != 0:
        raise HTTPException(status_code=502, detail=f"Feishu token failed: {data}")
    return data["tenant_access_token"]


async def get_user_name(settings: Settings, open_id: str) -> str:
    if not open_id:
        return ""
    token = await get_tenant_access_token(settings)
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"https://open.feishu.cn/open-apis/contact/v3/users/{open_id}",
            params={"user_id_type": "open_id", "department_id_type": "open_department_id"},
            headers={"Authorization": f"Bearer {token}"},
        )
    data = response.json()
    if data.get("code") != 0:
        return ""
    return data.get("data", {}).get("user", {}).get("name", "")


async def get_message_resource(settings: Settings, message_id: str, file_key: str, resource_type: str = "image") -> bytes:
    token = await get_tenant_access_token(settings)
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
            params={"type": resource_type},
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Feishu resource download failed: {response.text[:500]}")
    return response.content


def image_bytes_to_data_uri(content: bytes, filename: str = "image.jpg") -> str:
    mime_type = mimetypes.guess_type(filename)[0] or "image/jpeg"
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _image_extension(content: bytes, filename: str) -> str:
    guessed = Path(filename).suffix.lower()
    if guessed in {".jpg", ".jpeg", ".png", ".webp"}:
        return ".jpg" if guessed == ".jpeg" else guessed
    if content.startswith(b"\xff\xd8"):
        return ".jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def image_bytes_to_public_url(settings: Settings, content: bytes, filename: str = "image.jpg") -> str:
    if not settings.public_base_url:
        return image_bytes_to_data_uri(content, filename)

    extension = _image_extension(content, filename)
    static_dir = Path("data/static/images")
    static_dir.mkdir(parents=True, exist_ok=True)
    image_name = f"{uuid4().hex}{extension}"
    (static_dir / image_name).write_bytes(content)
    return f"{settings.public_base_url.rstrip('/')}/static/images/{image_name}"


async def reply_message(settings: Settings, message_id: str, text: str) -> dict[str, Any]:
    token = await get_tenant_access_token(settings)
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
            headers={"Authorization": f"Bearer {token}"},
            json={"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
        )
    if response.status_code >= 400:
        logger.error("Feishu reply HTTP error: status=%s body=%s", response.status_code, response.text[:1000])
        raise HTTPException(status_code=502, detail=f"Feishu reply failed: {response.text[:500]}")

    data = response.json()
    if data.get("code") != 0:
        logger.error("Feishu reply API error: %s", data)
        raise HTTPException(status_code=502, detail=f"Feishu reply failed: {data}")
    return data


async def delete_message(settings: Settings, message_id: str) -> dict[str, Any]:
    token = await get_tenant_access_token(settings)
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.delete(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code >= 400:
        logger.error("Feishu delete HTTP error: status=%s body=%s", response.status_code, response.text[:1000])
        raise HTTPException(status_code=502, detail=f"Feishu delete failed: {response.text[:500]}")

    data = response.json()
    if data.get("code") != 0:
        logger.error("Feishu delete API error: %s", data)
        raise HTTPException(status_code=502, detail=f"Feishu delete failed: {data}")
    return data


async def send_message_to_chat(settings: Settings, chat_id: str, text: str) -> dict[str, Any]:
    token = await get_tenant_access_token(settings)
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
        )
    if response.status_code >= 400:
        logger.error("Feishu send HTTP error: status=%s body=%s", response.status_code, response.text[:1000])
        raise HTTPException(status_code=502, detail=f"Feishu send failed: {response.text[:500]}")

    data = response.json()
    if data.get("code") != 0:
        logger.error("Feishu send API error: %s", data)
        raise HTTPException(status_code=502, detail=f"Feishu send failed: {data}")
    return data
