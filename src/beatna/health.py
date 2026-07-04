from __future__ import annotations

import importlib
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import app_timezone, as_int, default_post_hours, secret
from .scheduler import check_schedule_time, default_slots


@dataclass
class HealthCheck:
    name: str
    ok: bool
    level: str
    message: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _check_imports() -> list[HealthCheck]:
    checks: list[HealthCheck] = []
    packages = [
        ("pandas", "Bảng dữ liệu"),
        ("requests", "Kết nối web/Facebook"),
        ("bs4", "Đọc metadata bài báo"),
        ("feedparser", "Đọc RSS"),
        ("dateutil", "Xử lý ngày giờ"),
    ]
    optional = [("openai", "AI viết lại bài"), ("supabase", "Lưu vĩnh viễn Supabase")]
    for pkg, label in packages:
        try:
            importlib.import_module(pkg)
            checks.append(HealthCheck(label, True, "ok", f"Đã cài {pkg}"))
        except Exception as e:
            checks.append(HealthCheck(label, False, "error", f"Thiếu package bắt buộc: {pkg}", str(e)))
    for pkg, label in optional:
        try:
            importlib.import_module(pkg)
            checks.append(HealthCheck(label, True, "ok", f"Đã cài {pkg}"))
        except Exception as e:
            checks.append(HealthCheck(label, False, "warning", f"Chưa dùng được {label}; app vẫn chạy nếu không bật tính năng này.", str(e)))
    return checks


def _check_timezone_and_schedule() -> list[HealthCheck]:
    checks: list[HealthCheck] = []
    try:
        tz = ZoneInfo(app_timezone())
        checks.append(HealthCheck("Timezone", True, "ok", f"Timezone hợp lệ: {tz.key}"))
    except Exception as e:
        checks.append(HealthCheck("Timezone", False, "error", "APP_TIMEZONE không hợp lệ", str(e)))
        return checks

    slots = default_post_hours()
    bad = [s for s in slots if not isinstance(s, str) or ":" not in s]
    if bad:
        checks.append(HealthCheck("Khung giờ đăng", False, "warning", "Một số khung giờ sai định dạng HH:MM", ", ".join(map(str, bad))))
    else:
        checks.append(HealthCheck("Khung giờ đăng", True, "ok", f"{len(slots)} khung giờ mặc định: {', '.join(slots)}"))

    try:
        future_slot = default_slots(count=1)[0]
        checks.append(HealthCheck("Sinh lịch tự động", True, "ok", f"Slot kế tiếp: {future_slot.strftime('%d/%m/%Y %H:%M')}"))
    except Exception as e:
        checks.append(HealthCheck("Sinh lịch tự động", False, "error", "Không tạo được lịch đăng tự động", str(e)))

    try:
        min_minutes = max(15, as_int("MIN_SCHEDULE_MINUTES", 12))
        test_dt = datetime.now(ZoneInfo(app_timezone())) + timedelta(minutes=min_minutes + 5)
        result = check_schedule_time(test_dt, mode="facebook")
        checks.append(HealthCheck("Luật hẹn Facebook", result.ok, "ok" if result.ok else "warning", result.message, result.scheduled_utc or ""))
    except Exception as e:
        checks.append(HealthCheck("Luật hẹn Facebook", False, "warning", "Không kiểm tra được luật hẹn Facebook", str(e)))
    return checks


def _check_storage(store: Any | None) -> list[HealthCheck]:
    checks: list[HealthCheck] = []
    if store is None:
        checks.append(HealthCheck("Kho dữ liệu", False, "warning", "Chưa truyền store vào health check."))
        return checks
    try:
        stats = store.stats()
        checks.append(HealthCheck("Kho dữ liệu", True, "ok", f"Kết nối được {getattr(store, 'backend_name', 'store')}", str(stats)))
    except Exception as e:
        checks.append(HealthCheck("Kho dữ liệu", False, "error", "Không đọc được kho dữ liệu", str(e)))
    try:
        logs = store.list_logs(limit=1)
        checks.append(HealthCheck("Log hệ thống", True, "ok", f"Đọc log được, số log mẫu: {len(logs)}"))
    except Exception as e:
        checks.append(HealthCheck("Log hệ thống", False, "warning", "Không đọc được automation_logs", str(e)))
    return checks


def _check_secrets() -> list[HealthCheck]:
    checks: list[HealthCheck] = []
    required_for_private_app = ["APP_PASSWORD", "PAGE_CONNECT_PASSWORD"]
    required_for_fb = ["FB_PAGE_ID", "FB_PAGE_ACCESS_TOKEN"]
    required_for_supabase = ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"]

    for key in required_for_private_app:
        ok = bool(secret(key, ""))
        checks.append(HealthCheck(key, ok, "ok" if ok else "warning", "Đã cấu hình" if ok else "Nên đặt để app/khu kết nối riêng tư hơn."))
    fb_ok = all(bool(secret(k, "")) for k in required_for_fb)
    checks.append(HealthCheck("Facebook secrets", fb_ok, "ok" if fb_ok else "warning", "Đủ Page ID/token" if fb_ok else "Thiếu FB_PAGE_ID hoặc FB_PAGE_ACCESS_TOKEN; chưa đăng thật được."))
    storage_backend = str(secret("STORAGE_BACKEND", "supabase") or "supabase").lower()
    if storage_backend == "supabase":
        sb_ok = all(bool(secret(k, "")) for k in required_for_supabase)
        checks.append(HealthCheck("Supabase secrets", sb_ok, "ok" if sb_ok else "warning", "Đủ Supabase URL/key" if sb_ok else "Thiếu Supabase secrets; app sẽ fallback SQLite nếu code cho phép."))
    else:
        checks.append(HealthCheck("Supabase secrets", True, "ok", "Đang chọn backend khác Supabase."))
    return checks


def run_health_checks(store: Any | None = None) -> list[HealthCheck]:
    """Fast, offline-friendly health checks for Streamlit deploy readiness."""
    checks: list[HealthCheck] = []
    checks.extend(_check_imports())
    checks.extend(_check_timezone_and_schedule())
    checks.extend(_check_secrets())
    checks.extend(_check_storage(store))
    return checks


def health_rows(checks: list[HealthCheck]) -> list[dict[str, Any]]:
    icon = {"ok": "✅", "warning": "⚠️", "error": "❌"}
    return [{"Trạng thái": icon.get(c.level, "ℹ️"), "Mục": c.name, "OK": c.ok, "Mức": c.level, "Thông báo": c.message, "Chi tiết": c.detail} for c in checks]


def health_summary(checks: list[HealthCheck]) -> dict[str, int]:
    return {
        "ok": sum(1 for c in checks if c.level == "ok"),
        "warning": sum(1 for c in checks if c.level == "warning"),
        "error": sum(1 for c in checks if c.level == "error"),
        "total": len(checks),
    }
