from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

from .config import secret


class FacebookError(RuntimeError):
    pass


def _base_url() -> str:
    version = secret("FB_GRAPH_VERSION", "v25.0") or "v25.0"
    return f"https://graph.facebook.com/{version}"


def _page_id() -> str:
    page_id = secret("FB_PAGE_ID", "")
    if not page_id:
        raise FacebookError("Chưa cấu hình FB_PAGE_ID.")
    return str(page_id)


def _token() -> str:
    token = secret("FB_PAGE_ACCESS_TOKEN", "")
    if not token:
        raise FacebookError("Chưa cấu hình FB_PAGE_ACCESS_TOKEN.")
    return str(token)


def _handle(resp: requests.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    if not resp.ok or "error" in data:
        err = data.get("error", data)
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        raise FacebookError(msg)
    return data


def test_connection() -> dict[str, Any]:
    url = f"{_base_url()}/{_page_id()}"
    resp = requests.get(url, params={"fields": "id,name,link,fan_count", "access_token": _token()}, timeout=20)
    return _handle(resp)


def debug_token() -> dict[str, Any]:
    app_token = secret("FB_APP_ACCESS_TOKEN", "")
    if not app_token:
        raise FacebookError("Muốn kiểm tra token sâu cần thêm FB_APP_ACCESS_TOKEN = APP_ID|APP_SECRET.")
    url = f"{_base_url()}/debug_token"
    resp = requests.get(url, params={"input_token": _token(), "access_token": app_token}, timeout=20)
    return _handle(resp)


def create_feed_post(message: str, link: str | None = None, scheduled_at: datetime | None = None) -> dict[str, Any]:
    url = f"{_base_url()}/{_page_id()}/feed"
    data: dict[str, Any] = {"message": message, "access_token": _token()}
    if link:
        data["link"] = link
    if scheduled_at:
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
        data["published"] = "false"
        data["scheduled_publish_time"] = str(int(scheduled_at.timestamp()))
    resp = requests.post(url, data=data, timeout=40)
    return _handle(resp)


def create_photo_post(message: str, file_bytes: bytes, filename: str, mime_type: str | None = None, scheduled_at: datetime | None = None) -> dict[str, Any]:
    url = f"{_base_url()}/{_page_id()}/photos"
    files = {"source": (filename, file_bytes, mime_type or "application/octet-stream")}
    data: dict[str, Any] = {"message": message, "access_token": _token()}
    if scheduled_at:
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
        data["published"] = "false"
        data["unpublished_content_type"] = "SCHEDULED"
        data["scheduled_publish_time"] = str(int(scheduled_at.timestamp()))
    resp = requests.post(url, data=data, files=files, timeout=90)
    return _handle(resp)


def create_first_comment(post_id: str, message: str) -> dict[str, Any]:
    if not message.strip():
        raise FacebookError("Bình luận đang trống.")
    url = f"{_base_url()}/{post_id}/comments"
    resp = requests.post(url, data={"message": message, "access_token": _token()}, timeout=30)
    return _handle(resp)


def delete_post(post_id: str) -> dict[str, Any]:
    url = f"{_base_url()}/{post_id}"
    resp = requests.delete(url, params={"access_token": _token()}, timeout=30)
    return _handle(resp)


def get_scheduled_posts(limit: int = 25) -> list[dict[str, Any]]:
    url = f"{_base_url()}/{_page_id()}/scheduled_posts"
    resp = requests.get(
        url,
        params={"fields": "id,message,scheduled_publish_time,created_time", "limit": limit, "access_token": _token()},
        timeout=30,
    )
    data = _handle(resp)
    return data.get("data", []) if isinstance(data, dict) else []
