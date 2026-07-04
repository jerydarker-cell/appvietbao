from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .config import sensitive_keywords


@dataclass
class SafetyResult:
    score: int
    level: str
    notes: list[str]
    suggestions: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def check_post_safety(post_text: str, source_url: str = "", title: str = "", summary: str = "") -> SafetyResult:
    text = (post_text or "").strip()
    haystack = f"{post_text} {title} {summary}".lower()
    score = 0
    notes: list[str] = []
    suggestions: list[str] = []

    if not text:
        score += 35
        notes.append("Nội dung bài đăng đang trống.")
        suggestions.append("Tạo hoặc nhập nội dung trước khi đăng.")
    if not source_url:
        score += 25
        notes.append("Thiếu link nguồn báo/chính thức.")
        suggestions.append("Nên có link nguồn ở bình luận ghim hoặc trong bài.")
    if len(text) < 180:
        score += 8
        notes.append("Bài hơi ngắn, dễ thiếu ngữ cảnh.")
        suggestions.append("Bổ sung thời gian/địa điểm/điểm cần lưu ý nhưng không bịa thêm chi tiết.")
    if len(text) > 1800:
        score += 10
        notes.append("Bài khá dài, dễ giống copy nguyên văn.")
        suggestions.append("Rút gọn, viết lại bằng lời của page, giữ link nguồn.")
    if re.search(r"(!{2,}|\?{2,}|HOT|SỐC|KINH HOÀNG|CHẤN ĐỘNG)", text, re.I):
        score += 15
        notes.append("Có dấu hiệu giật tít/cường điệu.")
        suggestions.append("Giảm từ quá mạnh, dùng giọng thông tin bình tĩnh.")
    for phrase in ["theo nguồn tin riêng", "chúng tôi có mặt", "phóng viên", "độc quyền"]:
        if phrase in haystack:
            score += 20
            notes.append(f"Có cụm dễ khiến page bị hiểu là cơ quan báo chí/đưa tin hiện trường: {phrase}")
            suggestions.append("Chuyển sang cách nói: theo thông tin từ nguồn báo/chính quyền, Beat Nghệ An tóm tắt lại.")
            break
    sensitive_hits = [kw for kw in sensitive_keywords() if kw in haystack]
    if sensitive_hits:
        score += min(30, 5 * len(sensitive_hits))
        notes.append("Tin nhạy cảm: " + ", ".join(sensitive_hits[:8]))
        suggestions.append("Tin nhạy cảm nên tránh suy đoán, tránh ảnh nạn nhân, chỉ nêu theo nguồn đã công bố.")
    if source_url and source_url not in text and "nguồn" not in haystack:
        score += 8
        notes.append("Chưa thấy nhắc nguồn trong nội dung/bình luận.")
        suggestions.append("Đặt link nguồn ở bình luận ghim ngắn gọn.")

    score = min(score, 100)
    if score >= 65:
        level = "Cao"
    elif score >= 35:
        level = "Trung bình"
    else:
        level = "Thấp"
    if not notes:
        notes.append("Không thấy rủi ro lớn theo bộ kiểm tra nội bộ.")
    if not suggestions:
        suggestions.append("Vẫn nên đọc lại thủ công trước khi đăng Page.")
    return SafetyResult(score=score, level=level, notes=notes, suggestions=suggestions)
