import base64
import json
import mimetypes
from typing import Any

import httpx
from fastapi import HTTPException

from app.config import Settings


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


async def reply_message(settings: Settings, message_id: str, text: str) -> dict[str, Any]:
    token = await get_tenant_access_token(settings)
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
            headers={"Authorization": f"Bearer {token}"},
            json={"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
        )
    return response.json()
