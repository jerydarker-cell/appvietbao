from __future__ import annotations

import os
from typing import Any

try:
    import streamlit as st
except Exception:  # pragma: no cover
    st = None


def secret(name: str, default: Any = None) -> Any:
    """Read config from Streamlit secrets first, then environment variables."""
    if st is not None:
        try:
            if name in st.secrets:
                return st.secrets[name]
        except Exception:
            pass
    return os.getenv(name, default)


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "bật", "co", "có"}


def as_int(name: str, default: int) -> int:
    try:
        return int(secret(name, default) or default)
    except Exception:
        return default


def app_timezone() -> str:
    return str(secret("APP_TIMEZONE", "Asia/Bangkok") or "Asia/Bangkok")


def rss_sources() -> list[str]:
    raw = secret("RSS_SOURCES", "") or ""
    return [line.strip() for line in str(raw).splitlines() if line.strip() and not line.strip().startswith("#")]


def default_post_hours() -> list[str]:
    raw = str(secret("DEFAULT_POST_HOURS", "06:30,11:30,17:30,20:30") or "06:30,11:30,17:30,20:30")
    return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]


def hot_keywords() -> list[str]:
    raw = secret("HOT_KEYWORDS", "") or ""
    if str(raw).strip():
        return [x.strip().lower() for x in str(raw).replace("\n", ",").split(",") if x.strip()]
    return [
        "nghệ an", "vinh", "cửa lò", "diễn châu", "quỳnh lưu", "nam đàn", "hưng nguyên",
        "nghi lộc", "yên thành", "đô lương", "thanh chương", "anh sơn", "con cuông", "tân kỳ",
        "quỳ hợp", "quỳ châu", "quế phong", "kỳ sơn", "tương dương", "cửa khẩu", "nậm cắn",
        "khẩn", "nóng", "cảnh báo", "mới nhất", "vừa", "hôm nay", "đêm nay", "sáng nay",
        "tai nạn", "cháy", "nổ", "đuối nước", "mưa lớn", "lũ", "ngập", "sạt lở", "bão", "nắng nóng",
        "mất điện", "kẹt xe", "tắc đường", "quốc lộ", "cao tốc", "giá", "xăng", "vàng", "đất",
        "học sinh", "điểm thi", "tuyển sinh", "bệnh viện", "công an", "bắt giữ", "xử phạt",
        "xã", "phường", "thôn", "bản", "khối", "làng", "chợ", "trường", "cầu", "đường",
    ]


def sensitive_keywords() -> list[str]:
    raw = secret("SENSITIVE_KEYWORDS", "") or ""
    if str(raw).strip():
        return [x.strip().lower() for x in str(raw).replace("\n", ",").split(",") if x.strip()]
    return [
        "án mạng", "tử vong", "chết", "tai nạn", "đuối nước", "cháy", "nổ", "hiếp", "tự tử",
        "trẻ em", "bệnh", "dịch", "bắt giữ", "ma túy", "điều tra", "khởi tố", "lừa đảo",
        "thiên tai", "sạt lở", "bão", "lũ", "chính sách", "phạt", "tranh chấp",
    ]
