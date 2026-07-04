from __future__ import annotations

from .article import clean_spaces
from .config import secret


def _source_comment(source_url: str) -> str:
    return f"Nguồn: {source_url}" if source_url else "Nguồn sẽ được bổ sung dưới bình luận."


def make_rule_based_post(title: str, source_url: str, summary: str = "", source_name: str = "", tone: str = "Tin nhanh", local_angle: str = "") -> dict[str, str]:
    title = clean_spaces(title)
    summary = clean_spaces(summary)
    source_name = clean_spaces(source_name) or "nguồn báo"
    local_angle = clean_spaces(local_angle)

    if tone == "Cảnh báo / dân sinh":
        opener = "Bà con chú ý thông tin mới này."
    elif tone == "Hyperlocal xã/phường":
        opener = "Một tin đáng chú ý ở cấp địa phương, bà con nên nắm để tiện theo dõi."
    elif tone == "Nhẹ nhàng cộng đồng":
        opener = "Có thông tin mới liên quan đời sống bà con Nghệ An."
    elif tone == "Thể thao / giải trí":
        opener = "Một tin đáng chú ý trong ngày, anh em cùng theo dõi."
    else:
        opener = "Tin mới đáng chú ý trong ngày."

    body_lines = [opener, "", f"{title}"]
    if summary:
        body_lines += ["", f"Theo {source_name}, {summary}"]
    if local_angle:
        body_lines += ["", local_angle]
    body_lines += [
        "",
        "Beat Nghệ An tóm tắt lại để bà con tiện theo dõi. Nội dung chi tiết mọi người xem ở link nguồn phía dưới.",
    ]
    post_text = "\n".join(body_lines).strip()
    image_note = "Ưu tiên dùng link preview của bài gốc. Nếu tự thiết kế ảnh, không dùng ảnh tai nạn/nạn nhân/logo báo khi chưa có quyền."
    tags = "#BeatNgheAn #NgheAn #TinNgheAn"
    return {
        "post_text": post_text,
        "first_comment": _source_comment(source_url),
        "image_note": image_note,
        "tags": tags,
        "risk_note": "Bản mẫu an toàn: đã viết lại, có nhắc nguồn, không nhận là tin độc quyền.",
    }


def make_ai_post(title: str, source_url: str, summary: str = "", source_name: str = "", tone: str = "Tin nhanh", local_angle: str = "") -> dict[str, str]:
    api_key = secret("OPENAI_API_KEY", "")
    if not api_key:
        return make_rule_based_post(title, source_url, summary, source_name, tone, local_angle)
    try:
        from openai import OpenAI
        client = OpenAI(api_key=str(api_key))
        model = str(secret("OPENAI_MODEL", "gpt-4.1-mini") or "gpt-4.1-mini")
        prompt = f"""
Bạn là biên tập viên Facebook Page Beat Nghệ An. Viết lại tin báo thành bài đăng Facebook an toàn, tự nhiên, không copy nguyên văn, không giật tít, không bịa thêm chi tiết.

Yêu cầu:
- Tiếng Việt tự nhiên, có hơi thở địa phương Nghệ An.
- Không tự xưng phóng viên/cơ quan báo chí/độc quyền/đang có mặt hiện trường.
- Tin nhạy cảm thì bình tĩnh, tránh suy đoán, tránh mô tả gây sốc.
- Có lời nhắc xem nguồn phía dưới.
- Trả về JSON với các khóa: post_text, first_comment, image_note, tags, risk_note.
- first_comment ngắn gọn, ưu tiên dạng: Nguồn: <link>

Tiêu đề: {title}
Tóm tắt nguồn: {summary}
Nguồn: {source_name}
Link nguồn: {source_url}
Giọng bài: {tone}
Góc địa phương muốn nhấn: {local_angle}
""".strip()
        resp = client.responses.create(model=model, input=prompt)
        raw = getattr(resp, "output_text", "") or ""
        import json, re
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0) if m else raw)
        fallback = make_rule_based_post(title, source_url, summary, source_name, tone, local_angle)
        return {k: str(data.get(k) or fallback[k]) for k in ["post_text", "first_comment", "image_note", "tags", "risk_note"]}
    except Exception:
        return make_rule_based_post(title, source_url, summary, source_name, tone, local_angle)
