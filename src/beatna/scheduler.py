from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import app_timezone, default_post_hours, secret


@dataclass
class ScheduleCheck:
    ok: bool
    level: str
    message: str
    scheduled_utc: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def local_to_utc_iso(dt: datetime, tz_name: str | None = None) -> str:
    tz = ZoneInfo(tz_name or app_timezone())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc).isoformat()


def iso_to_local_text(value: str | None, tz_name: str | None = None) -> str:
    if not value:
        return ""
    tz = ZoneInfo(tz_name or app_timezone())
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(value)


def check_schedule_time(dt: datetime, mode: str = "facebook") -> ScheduleCheck:
    tz = ZoneInfo(app_timezone())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    now = datetime.now(tz)
    min_minutes = int(secret("MIN_SCHEDULE_MINUTES", 12) or 12)
    max_days = int(secret("MAX_SCHEDULE_DAYS", 30) or 30)
    if dt <= now:
        return ScheduleCheck(False, "error", "Giờ hẹn phải nằm trong tương lai.")
    if mode == "facebook" and dt < now + timedelta(minutes=min_minutes):
        return ScheduleCheck(False, "error", f"Nên hẹn trước ít nhất {min_minutes} phút để Facebook nhận lịch ổn định.")
    if mode == "facebook" and dt > now + timedelta(days=max_days):
        return ScheduleCheck(False, "error", f"Lịch quá xa. App đang giới hạn {max_days} ngày để tránh lỗi API.")
    return ScheduleCheck(True, "ok", "Giờ hẹn hợp lệ.", dt.astimezone(timezone.utc).isoformat())


def _parse_slots() -> list[tuple[int, int]]:
    slots: list[tuple[int, int]] = []
    for item in default_post_hours():
        try:
            h, m = item.split(":", 1)
            slots.append((int(h), int(m)))
        except Exception:
            continue
    return slots or [(6, 30), (11, 30), (17, 30), (20, 30)]


def default_slots(start: datetime | None = None, count: int = 7, spacing_minutes: int = 0) -> list[datetime]:
    tz = ZoneInfo(app_timezone())
    base = start or datetime.now(tz)
    if base.tzinfo is None:
        base = base.replace(tzinfo=tz)
    slots: list[datetime] = []
    day = base.date()
    parsed = _parse_slots()
    while len(slots) < count:
        for h, m in parsed:
            candidate = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
            if candidate > base + timedelta(minutes=max(15, spacing_minutes)):
                slots.append(candidate)
                if len(slots) >= count:
                    break
        day = day + timedelta(days=1)
    return slots


def human_delta_from_now(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = dt - datetime.now(timezone.utc)
        seconds = int(diff.total_seconds())
        if seconds < 0:
            return "đã đến giờ"
        mins = seconds // 60
        if mins < 60:
            return f"còn {mins} phút"
        hours = mins // 60
        if hours < 24:
            return f"còn {hours} giờ {mins % 60} phút"
        days = hours // 24
        return f"còn {days} ngày {hours % 24} giờ"
    except Exception:
        return ""
