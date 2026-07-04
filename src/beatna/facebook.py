from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import dry_run_mode, secret

try:  # Streamlit is optional for worker scripts.
    import streamlit as st
except Exception:  # pragma: no cover
    st = None


class FacebookError(RuntimeError):
    pass


def _session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "DELETE"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "BeatNgheAnAutoPost/9.0"})
    return session


def _dry_id(prefix: str) -> str:
    return f"dryrun_{prefix}_{int(datetime.now(timezone.utc).timestamp())}"


def _session_value(name: str, default: str = "") -> str:
    if st is None:
        return default
    try:
        return str(st.session_state.get(name, default) or default)
    except Exception:
        return default


def _base_url() -> str:
    version = secret("FB_GRAPH_VERSION", "v25.0") or "v25.0"
    return f"https://graph.facebook.com/{version}"


def _page_id() -> str:
    # Session credentials are useful for private/manual connect flow. Secrets stay the stable production path.
    page_id = _session_value("fb_page_id_session") or secret("FB_PAGE_ID", "")
    if not page_id:
        raise FacebookError("Chưa cấu hình FB_PAGE_ID hoặc chưa kết nối Page trong phiên này.")
    return str(page_id).strip()


def _token() -> str:
    token = _session_value("fb_page_token_session") or secret("FB_PAGE_ACCESS_TOKEN", "")
    if not token:
        raise FacebookError("Chưa cấu hình FB_PAGE_ACCESS_TOKEN hoặc chưa kết nối token trong phiên này.")
    return str(token).strip()


def mask_token(token: str | None = None) -> str:
    tok = (token if token is not None else (_session_value("fb_page_token_session") or secret("FB_PAGE_ACCESS_TOKEN", ""))) or ""
    tok = str(tok)
    if not tok:
        return "Chưa có token"
    if len(tok) <= 12:
        return "••••" + tok[-3:]
    return tok[:4] + "••••••••••••" + tok[-6:]


def credential_source() -> dict[str, str]:
    session_page = _session_value("fb_page_id_session")
    session_token = _session_value("fb_page_token_session")
    secret_page = str(secret("FB_PAGE_ID", "") or "")
    secret_token = str(secret("FB_PAGE_ACCESS_TOKEN", "") or "")
    source = "session" if session_page and session_token else ("secrets" if secret_page and secret_token else "missing")
    return {
        "source": source,
        "page_id": session_page or secret_page,
        "has_token": "yes" if (session_token or secret_token) else "no",
        "masked_token": mask_token(session_token or secret_token),
    }


def set_session_credentials(page_id: str, token: str) -> None:
    if st is None:
        raise FacebookError("Chỉ có thể lưu kết nối phiên khi chạy trong Streamlit.")
    st.session_state["fb_page_id_session"] = str(page_id).strip()
    st.session_state["fb_page_token_session"] = str(token).strip()


def clear_session_credentials() -> None:
    if st is None:
        return
    st.session_state.pop("fb_page_id_session", None)
    st.session_state.pop("fb_page_token_session", None)


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
    resp = _session().get(url, params={"fields": "id,name,link,fan_count", "access_token": _token()}, timeout=20)
    return _handle(resp)


def test_connection_with_credentials(page_id: str, token: str) -> dict[str, Any]:
    url = f"{_base_url()}/{str(page_id).strip()}"
    resp = _session().get(url, params={"fields": "id,name,link,fan_count", "access_token": str(token).strip()}, timeout=20)
    return _handle(resp)


def debug_token(token: str | None = None) -> dict[str, Any]:
    app_token = secret("FB_APP_ACCESS_TOKEN", "")
    if not app_token:
        raise FacebookError("Muốn kiểm tra token sâu cần thêm FB_APP_ACCESS_TOKEN = APP_ID|APP_SECRET.")
    url = f"{_base_url()}/debug_token"
    resp = _session().get(url, params={"input_token": token or _token(), "access_token": app_token}, timeout=20)
    return _handle(resp)


def create_feed_post(message: str, link: str | None = None, scheduled_at: datetime | None = None) -> dict[str, Any]:
    if not (message or "").strip():
        raise FacebookError("Nội dung bài đăng đang trống.")
    url = f"{_base_url()}/{_page_id()}/feed"
    data: dict[str, Any] = {"message": message, "access_token": _token()}
    if link:
        data["link"] = link
    if scheduled_at:
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
        data["published"] = "false"
        data["scheduled_publish_time"] = str(int(scheduled_at.timestamp()))
    if dry_run_mode():
        return {"id": _dry_id("feed"), "dry_run": True, "scheduled": bool(scheduled_at)}
    resp = _session().post(url, data=data, timeout=40)
    return _handle(resp)


def create_photo_post(message: str, file_bytes: bytes, filename: str, mime_type: str | None = None, scheduled_at: datetime | None = None) -> dict[str, Any]:
    if not file_bytes:
        raise FacebookError("Chưa có file ảnh để đăng.")
    if not (message or "").strip():
        raise FacebookError("Nội dung bài đăng đang trống.")
    url = f"{_base_url()}/{_page_id()}/photos"
    files = {"source": (filename, file_bytes, mime_type or "application/octet-stream")}
    data: dict[str, Any] = {"message": message, "access_token": _token()}
    if scheduled_at:
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
        data["published"] = "false"
        data["unpublished_content_type"] = "SCHEDULED"
        data["scheduled_publish_time"] = str(int(scheduled_at.timestamp()))
    if dry_run_mode():
        return {"id": _dry_id("photo"), "post_id": _dry_id("photo_post"), "dry_run": True, "scheduled": bool(scheduled_at)}
    resp = _session().post(url, data=data, files=files, timeout=90)
    return _handle(resp)


def create_first_comment(post_id: str, message: str) -> dict[str, Any]:
    if not message.strip():
        raise FacebookError("Bình luận đang trống.")
    url = f"{_base_url()}/{post_id}/comments"
    if dry_run_mode():
        return {"id": _dry_id("comment"), "dry_run": True}
    resp = _session().post(url, data={"message": message, "access_token": _token()}, timeout=30)
    return _handle(resp)


def delete_post(post_id: str) -> dict[str, Any]:
    url = f"{_base_url()}/{post_id}"
    if dry_run_mode():
        return {"success": True, "dry_run": True}
    resp = _session().delete(url, params={"access_token": _token()}, timeout=30)
    return _handle(resp)


def get_scheduled_posts(limit: int = 25) -> list[dict[str, Any]]:
    url = f"{_base_url()}/{_page_id()}/scheduled_posts"
    resp = _session().get(
        url,
        params={"fields": "id,message,scheduled_publish_time,created_time", "limit": limit, "access_token": _token()},
        timeout=30,
    )
    data = _handle(resp)
    return data.get("data", []) if isinstance(data, dict) else []


def page_status_report() -> dict[str, Any]:
    """Safe status helper for UI; never returns the raw token."""
    status = credential_source()
    out: dict[str, Any] = {"credential_source": status, "dry_run": dry_run_mode()}
    try:
        info = test_connection()
        out["page"] = {"ok": True, "id": info.get("id"), "name": info.get("name"), "link": info.get("link"), "fan_count": info.get("fan_count")}
    except Exception as e:
        out["page"] = {"ok": False, "error": str(e)}
    return out

# -----------------------------
# Facebook OAuth Login helpers
# -----------------------------
from urllib.parse import urlencode


def oauth_configured() -> bool:
    return bool(secret("FB_APP_ID", "") and secret("FB_APP_SECRET", "") and secret("FB_OAUTH_REDIRECT_URI", ""))


def oauth_missing_items() -> list[str]:
    missing: list[str] = []
    for key in ["FB_APP_ID", "FB_APP_SECRET", "FB_OAUTH_REDIRECT_URI"]:
        if not secret(key, ""):
            missing.append(key)
    return missing


def default_oauth_scopes() -> str:
    # public_profile is automatically allowed. Page permissions may require Meta review if app is live for other users.
    raw = secret("FB_OAUTH_SCOPES", "pages_show_list,pages_read_engagement,pages_manage_posts,pages_manage_engagement")
    scopes = [x.strip() for x in str(raw).replace("\n", ",").split(",") if x.strip()]
    return ",".join(dict.fromkeys(scopes))


def build_oauth_login_url(state: str, redirect_uri: str | None = None) -> str:
    app_id = secret("FB_APP_ID", "")
    if not app_id:
        raise FacebookError("Chưa cấu hình FB_APP_ID trong Secrets.")
    redirect = redirect_uri or secret("FB_OAUTH_REDIRECT_URI", "")
    if not redirect:
        raise FacebookError("Chưa cấu hình FB_OAUTH_REDIRECT_URI trong Secrets.")
    version = secret("FB_GRAPH_VERSION", "v25.0") or "v25.0"
    params = {
        "client_id": str(app_id),
        "redirect_uri": str(redirect),
        "state": state,
        "scope": default_oauth_scopes(),
        "response_type": "code",
    }
    return f"https://www.facebook.com/{version}/dialog/oauth?{urlencode(params)}"


def exchange_code_for_user_token(code: str, redirect_uri: str | None = None) -> dict[str, Any]:
    app_id = secret("FB_APP_ID", "")
    app_secret = secret("FB_APP_SECRET", "")
    redirect = redirect_uri or secret("FB_OAUTH_REDIRECT_URI", "")
    if not (app_id and app_secret and redirect):
        raise FacebookError("Thiếu FB_APP_ID, FB_APP_SECRET hoặc FB_OAUTH_REDIRECT_URI trong Secrets.")
    url = f"{_base_url()}/oauth/access_token"
    resp = _session().get(url, params={
        "client_id": str(app_id),
        "redirect_uri": str(redirect),
        "client_secret": str(app_secret),
        "code": str(code),
    }, timeout=30)
    return _handle(resp)


def exchange_long_lived_user_token(short_token: str) -> dict[str, Any]:
    app_id = secret("FB_APP_ID", "")
    app_secret = secret("FB_APP_SECRET", "")
    if not (app_id and app_secret):
        raise FacebookError("Thiếu FB_APP_ID hoặc FB_APP_SECRET trong Secrets.")
    url = f"{_base_url()}/oauth/access_token"
    resp = _session().get(url, params={
        "grant_type": "fb_exchange_token",
        "client_id": str(app_id),
        "client_secret": str(app_secret),
        "fb_exchange_token": str(short_token),
    }, timeout=30)
    return _handle(resp)


def get_pages_from_user_token(user_token: str) -> list[dict[str, Any]]:
    url = f"{_base_url()}/me/accounts"
    resp = _session().get(url, params={
        "fields": "id,name,access_token,category,tasks,perms,picture{url}",
        "limit": 100,
        "access_token": str(user_token),
    }, timeout=30)
    data = _handle(resp)
    return data.get("data", []) if isinstance(data, dict) else []


def choose_page_from_oauth(page_id: str, pages: list[dict[str, Any]]) -> dict[str, Any]:
    target = str(page_id).strip()
    for page in pages:
        if str(page.get("id", "")).strip() == target:
            token = page.get("access_token")
            if not token:
                raise FacebookError("Page tìm thấy nhưng Facebook không trả Page access token. Hãy kiểm tra quyền pages_show_list/pages_manage_posts và vai trò quản trị Page.")
            return page
    raise FacebookError("Không thấy Page ID này trong danh sách Page của tài khoản vừa đăng nhập.")
