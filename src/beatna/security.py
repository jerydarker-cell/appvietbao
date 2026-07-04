from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from .config import block_high_risk_posts, dry_run_mode, max_post_chars, max_risk_score_to_publish, secret
from .safety import check_post_safety


@dataclass
class GateResult:
    ok: bool
    level: str
    notes: list[str]
    suggestions: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "level": self.level, "notes": self.notes, "suggestions": self.suggestions}


def secure_equals(a: str | None, b: str | None) -> bool:
    return hmac.compare_digest(str(a or ""), str(b or ""))


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def secret_matches(plain_value: str, secret_name: str) -> bool:
    """Support either plain secret or SHA256 hash secret.

    Example:
    APP_PASSWORD="abc" OR APP_PASSWORD_SHA256="ba7816..."
    """
    plain = str(secret(secret_name, "") or "")
    hashed = str(secret(f"{secret_name}_SHA256", "") or "")
    if plain and secure_equals(plain_value, plain):
        return True
    if hashed and secure_equals(sha256_text(plain_value), hashed):
        return True
    return False


def login_role(password: str) -> str:
    """Return admin/editor/viewer/demo/none based on configured secrets."""
    has_any = any(str(secret(k, "") or "") or str(secret(f"{k}_SHA256", "") or "") for k in ["ADMIN_PASSWORD", "EDITOR_PASSWORD", "VIEWER_PASSWORD", "APP_PASSWORD"])
    if not has_any:
        return "demo"
    if secret_matches(password, "ADMIN_PASSWORD") or secret_matches(password, "APP_PASSWORD"):
        return "admin"
    if secret_matches(password, "EDITOR_PASSWORD"):
        return "editor"
    if secret_matches(password, "VIEWER_PASSWORD"):
        return "viewer"
    return "none"


def can_write(role: str | None) -> bool:
    return role in {"admin", "editor", "demo"}


def can_admin(role: str | None) -> bool:
    return role in {"admin", "demo"}


def publish_gate(post_text: str, source_url: str = "", title: str = "", summary: str = "", first_comment: str = "") -> GateResult:
    notes: list[str] = []
    suggestions: list[str] = []
    safety = check_post_safety(post_text, source_url, title, summary)

    if dry_run_mode():
        notes.append("DRY_RUN_MODE đang bật: các lệnh đăng Facebook sẽ chỉ tạo ID giả để test, không đăng thật.")

    if len((post_text or "").strip()) > max_post_chars():
        notes.append(f"Bài dài hơn MAX_POST_CHARS={max_post_chars()} ký tự.")
        suggestions.append("Rút gọn bài trước khi đăng để tránh giống copy nguyên văn và đọc mượt hơn.")

    if block_high_risk_posts() and safety.score > max_risk_score_to_publish():
        notes.append(f"Điểm rủi ro {safety.score}/100 vượt ngưỡng cho phép {max_risk_score_to_publish()}/100.")
        suggestions.append("Sửa bài, bổ sung nguồn, giảm từ giật tít hoặc đưa về nháp để duyệt lại.")

    if source_url and not (first_comment or "").strip():
        notes.append("Có link nguồn nhưng chưa có bình luận nguồn.")
        suggestions.append("Thêm bình luận nguồn ngắn gọn để người đọc kiểm chứng.")

    # Carry the original safety notes into the gate explanation.
    for n in safety.notes:
        if n not in notes:
            notes.append(n)
    for s in safety.suggestions:
        if s not in suggestions:
            suggestions.append(s)

    hard_block = (block_high_risk_posts() and safety.score > max_risk_score_to_publish()) or len((post_text or "").strip()) > max_post_chars()
    level = "blocked" if hard_block else ("dry_run" if dry_run_mode() else "ok")
    return GateResult(ok=not hard_block, level=level, notes=notes, suggestions=suggestions)


def security_check_rows() -> list[dict[str, Any]]:
    rows = []
    def row(name: str, ok: bool, note: str) -> None:
        rows.append({"Mục": name, "Trạng thái": "✅ OK" if ok else "⚠️ Cần chú ý", "Ghi chú": note})

    row("Mật khẩu app", bool(secret("APP_PASSWORD", "") or secret("APP_PASSWORD_SHA256", "") or secret("ADMIN_PASSWORD", "") or secret("ADMIN_PASSWORD_SHA256", "")), "Nên dùng ADMIN/EDITOR/VIEWER hoặc APP_PASSWORD.")
    row("Mật khẩu khu Page", bool(secret("PAGE_CONNECT_PASSWORD", "") or secret("PAGE_CONNECT_PASSWORD_SHA256", "")), "Bảo vệ khu nhập Page token.")
    row("Supabase", bool(secret("SUPABASE_URL", "") and (secret("SUPABASE_SERVICE_ROLE_KEY", "") or secret("SUPABASE_KEY", ""))), "Dùng Supabase để lưu vĩnh viễn.")
    row("Facebook Page", bool(secret("FB_PAGE_ID", "") and secret("FB_PAGE_ACCESS_TOKEN", "")), "Có thể dùng session tạm, nhưng Secrets ổn định hơn.")
    row("Dry-run", not dry_run_mode(), "Đang test không đăng thật" if dry_run_mode() else "Đăng thật nếu token/quyền hợp lệ.")
    row("Chặn bài rủi ro cao", block_high_risk_posts(), "Nên bật để tránh đăng nhầm bài nhạy cảm.")
    return rows


def make_backup_payload(store: Any) -> dict[str, Any]:
    return {
        "app": "Beat Nghệ An AutoPost Pro v9 Secure Ops",
        "posts": store.export_posts(),
        "sources": store.list_sources(include_disabled=True),
        "articles": store.list_articles(min_score=0, limit=5000, include_drafted=True),
        "logs": store.list_logs(limit=5000),
    }


def backup_json_bytes(store: Any) -> bytes:
    return json.dumps(make_backup_payload(store), ensure_ascii=False, indent=2).encode("utf-8")
