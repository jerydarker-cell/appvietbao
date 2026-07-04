from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .config import app_timezone, default_post_hours
from .scheduler import iso_to_local_text


@dataclass
class QualityItem:
    post_id: str
    title: str
    status: str
    score: int
    level: str
    issues: list[str]
    suggestions: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _norm(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^0-9a-zA-ZÀ-ỹ\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _tokens(text: str) -> set[str]:
    stop = {"và", "của", "các", "cho", "một", "những", "trên", "dưới", "với", "tại", "về", "khi", "đã", "là", "có", "theo"}
    return {t for t in _norm(text).split() if len(t) >= 3 and t not in stop}


def bucket_from_text(title: str, summary: str = "") -> str:
    text = _norm(f"{title} {summary}")
    buckets = [
        ("Cảnh báo / an toàn", ["cảnh báo", "tai nạn", "đuối nước", "cháy", "lừa đảo", "mưa", "bão", "nắng nóng", "sạt lở"]),
        ("Giao thông / hạ tầng", ["đường", "cầu", "quốc lộ", "cao tốc", "ùn tắc", "giao thông", "sân bay", "cảng"]),
        ("Giá cả / dân sinh", ["giá", "vàng", "xăng", "điện", "nước", "tiền", "hỗ trợ", "chính sách"]),
        ("Giáo dục", ["học sinh", "trường", "thi", "điểm", "tuyển sinh", "giáo dục"]),
        ("Y tế / sức khỏe", ["bệnh", "y tế", "bác sĩ", "sốt", "dịch", "cấp cứu", "bệnh viện"]),
        ("Văn hóa / du lịch", ["du lịch", "lễ hội", "di sản", "ẩm thực", "văn hóa", "cửa lò", "biển"]),
        ("Thể thao / giải trí", ["bóng đá", "sông lam", "slna", "thể thao", "giải đấu"]),
        ("Hyperlocal xã/phường", ["xã", "phường", "thôn", "bản", "khối", "làng", "huyện", "thị xã"]),
    ]
    for name, keywords in buckets:
        if any(k in text for k in keywords):
            return name
    return "Tin chung Nghệ An"


def post_quality(row: dict[str, Any]) -> QualityItem:
    title = row.get("title") or ""
    post_text = row.get("post_text") or ""
    source_url = row.get("source_url") or ""
    first_comment = row.get("first_comment") or ""
    risk_score = int(row.get("risk_score") or 0)
    issues: list[str] = []
    suggestions: list[str] = []

    if not title.strip():
        issues.append("Thiếu tiêu đề nội bộ")
    if not source_url.strip():
        issues.append("Thiếu link nguồn")
        suggestions.append("Thêm link nguồn báo chính thức trước khi đăng.")
    if len(post_text.strip()) < 120:
        issues.append("Bài hơi ngắn")
        suggestions.append("Bổ sung 1–2 câu bối cảnh địa phương hoặc nhắc người đọc kiểm chứng nguồn.")
    if len(post_text) > 2200:
        issues.append("Bài khá dài")
        suggestions.append("Rút gọn để phù hợp Facebook, giữ ý chính và link nguồn.")
    if first_comment and source_url and source_url not in first_comment:
        issues.append("Bình luận nguồn chưa chứa link nguồn")
        suggestions.append("Đưa link nguồn vào bình luận đầu tiên để tiện kiểm chứng.")
    if not first_comment.strip():
        issues.append("Thiếu bình luận nguồn")
        suggestions.append("Tạo bình luận ghim ngắn: Nguồn: <link> + 1 câu hỏi tương tác.")
    risky_words = ["sốc", "kinh hoàng", "rúng động", "khủng khiếp", "chưa từng có"]
    if any(w in _norm(post_text) for w in risky_words):
        issues.append("Có từ dễ bị xem là giật tít")
        suggestions.append("Đổi sang giọng trung tính, ưu tiên thông tin kiểm chứng.")
    if risk_score >= 65:
        issues.append("Điểm rủi ro cao")
        suggestions.append("Không tự động đăng. Cần sửa kỹ, xác minh lại và cân nhắc chỉ dùng link preview.")
    elif risk_score >= 35:
        issues.append("Điểm rủi ro trung bình")
        suggestions.append("Đọc lại nguồn, tránh khẳng định thêm ngoài bài gốc.")

    score = 100 - min(80, risk_score)
    score -= min(45, 8 * len(issues))
    score = max(0, min(100, score))
    if score >= 80:
        level = "Tốt"
    elif score >= 60:
        level = "Cần rà soát nhẹ"
    else:
        level = "Cần sửa trước khi đăng"
    return QualityItem(str(row.get("id") or ""), title, row.get("status") or "", score, level, issues, suggestions)


def quality_table(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        q = post_quality(row)
        out.append({
            "ID": q.post_id,
            "Trạng thái": q.status,
            "Điểm chất lượng": q.score,
            "Mức": q.level,
            "Tiêu đề": q.title,
            "Vấn đề": "; ".join(q.issues) if q.issues else "OK",
            "Gợi ý": "; ".join(q.suggestions[:2]) if q.suggestions else "Có thể đăng/hẹn giờ sau khi đọc lại.",
        })
    return sorted(out, key=lambda x: x["Điểm chất lượng"])


def duplicate_groups(rows: Iterable[dict[str, Any]], threshold: float = 0.72) -> list[dict[str, Any]]:
    rows = list(rows)
    groups: list[dict[str, Any]] = []
    used: set[str] = set()
    by_url: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    by_hash: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r.get("source_url"):
            by_url[str(r.get("source_url"))].append(r)
        if r.get("content_hash"):
            by_hash[str(r.get("content_hash"))].append(r)

    def add_group(kind: str, items: list[dict[str, Any]]) -> None:
        ids = [str(x.get("id")) for x in items if x.get("id")]
        if len(ids) <= 1 or all(i in used for i in ids):
            return
        used.update(ids)
        groups.append({"Kiểu trùng": kind, "Số bài": len(items), "ID": ", ".join(ids), "Tiêu đề đại diện": items[0].get("title") or ""})

    for items in by_url.values():
        add_group("Trùng link nguồn", items)
    for items in by_hash.values():
        add_group("Trùng content_hash", items)

    token_cache = {str(r.get("id")): _tokens((r.get("title") or "") + " " + (r.get("summary") or "")) for r in rows if r.get("id")}
    for i, a in enumerate(rows):
        aid = str(a.get("id") or "")
        if not aid or aid in used:
            continue
        at = token_cache.get(aid, set())
        if len(at) < 4:
            continue
        items = [a]
        for b in rows[i + 1:]:
            bid = str(b.get("id") or "")
            if not bid or bid in used:
                continue
            bt = token_cache.get(bid, set())
            if len(bt) < 4:
                continue
            sim = len(at & bt) / max(1, len(at | bt))
            if sim >= threshold:
                items.append(b)
        add_group("Tiêu đề/nội dung giống nhau", items)
    return groups


def calendar_rows(posts: Iterable[dict[str, Any]], days: int = 14) -> list[dict[str, Any]]:
    tz = ZoneInfo(app_timezone())
    now = datetime.now(tz)
    end = now + timedelta(days=days)
    out: list[dict[str, Any]] = []
    for p in posts:
        value = p.get("scheduled_at") or p.get("updated_at") or p.get("created_at")
        if not value:
            continue
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            local = dt.astimezone(tz)
        except Exception:
            continue
        if local < now - timedelta(days=1) or local > end:
            continue
        out.append({
            "Ngày": local.strftime("%d/%m/%Y"),
            "Giờ": local.strftime("%H:%M"),
            "Trạng thái": p.get("status") or "",
            "Chiến dịch": p.get("campaign") or bucket_from_text(p.get("title") or "", p.get("summary") or ""),
            "Tiêu đề": p.get("title") or "",
            "Còn lại": iso_to_local_text(p.get("scheduled_at")),
            "ID": p.get("id") or "",
        })
    return sorted(out, key=lambda x: (x["Ngày"], x["Giờ"]))


def next_smart_slots(existing_posts: Iterable[dict[str, Any]], count: int = 10, start: datetime | None = None, min_gap_minutes: int = 90) -> list[datetime]:
    tz = ZoneInfo(app_timezone())
    base = start or datetime.now(tz)
    if base.tzinfo is None:
        base = base.replace(tzinfo=tz)
    occupied: list[datetime] = []
    for p in existing_posts:
        value = p.get("scheduled_at")
        if not value:
            continue
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            occupied.append(dt.astimezone(tz))
        except Exception:
            pass
    parsed: list[tuple[int, int]] = []
    for item in default_post_hours():
        try:
            h, m = item.split(":", 1)
            parsed.append((int(h), int(m)))
        except Exception:
            continue
    parsed = parsed or [(6, 30), (11, 30), (17, 30), (20, 30)]

    slots: list[datetime] = []
    day = base.date()
    while len(slots) < count and day <= (base + timedelta(days=90)).date():
        for h, m in parsed:
            candidate = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
            if candidate <= base + timedelta(minutes=15):
                continue
            too_close = any(abs((candidate - old).total_seconds()) < min_gap_minutes * 60 for old in occupied + slots)
            if not too_close:
                slots.append(candidate)
                if len(slots) >= count:
                    break
        day = day + timedelta(days=1)
    return slots


def analytics_summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    by_status = Counter(r.get("status") or "" for r in rows)
    by_campaign = Counter((r.get("campaign") or bucket_from_text(r.get("title") or "", r.get("summary") or "")) for r in rows)
    risk_scores = [int(r.get("risk_score") or 0) for r in rows]
    quality = quality_table(rows)
    avg_quality = round(sum(x["Điểm chất lượng"] for x in quality) / len(quality), 1) if quality else 0
    return {
        "total_posts": len(rows),
        "by_status": dict(by_status),
        "by_campaign": dict(by_campaign.most_common(12)),
        "avg_risk": round(sum(risk_scores) / len(risk_scores), 1) if risk_scores else 0,
        "avg_quality": avg_quality,
        "needs_fix": sum(1 for x in quality if x["Điểm chất lượng"] < 60),
    }


def make_backup_payload(posts: list[dict[str, Any]], sources: list[dict[str, Any]], articles: list[dict[str, Any]], logs: list[dict[str, Any]]) -> bytes:
    payload = {
        "app": "Beat Nghệ An AutoPost Pro v5 Ultra Stable",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "posts": posts,
        "sources": sources,
        "articles": articles,
        "logs": logs,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
