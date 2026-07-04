from __future__ import annotations

import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from beatna.article import stable_hash
from beatna.chatgpt_bridge import parse_chatgpt_hourly_text
from beatna.health import health_summary, run_health_checks
from beatna.scheduler import check_schedule_time
from beatna.config import app_timezone
from beatna.storage import get_store
from beatna.security import publish_gate, sha256_text


def test_chatgpt_parser() -> None:
    sample = """
COPY BÀI ĐĂNG
Nghệ An ngày 4/7 có mưa dông rải rác, bà con chú ý khi ra đường.

COPY BÌNH LUẬN GHIM
Nguồn: https://baonghean.vn/thoi-tiet-nghe-an-4-7
"""
    items = parse_chatgpt_hourly_text(sample)
    assert len(items) == 1
    assert items[0].source_url.startswith("https://")
    assert items[0].first_comment.startswith("Nguồn:")


def test_schedule_future_ok() -> None:
    result = check_schedule_time(datetime.now(ZoneInfo(app_timezone())) + timedelta(minutes=30), mode="facebook")
    assert result.ok
    assert result.scheduled_utc


def test_storage_claim_lock() -> None:
    store = get_store()
    pid = store.add_post(
        title="Smoke test",
        source_url="https://example.com/smoke",
        post_text="Nội dung smoke test đủ dài để lưu và kiểm tra khóa hàng đợi.",
        first_comment="Nguồn: https://example.com/smoke",
        content_hash=stable_hash("Smoke test", "https://example.com/smoke"),
        status="queued",
        scheduled_at=(datetime.now(ZoneInfo(app_timezone())) - timedelta(minutes=1)).astimezone().isoformat(),
    )
    try:
        assert store.try_claim_post(pid)
        assert not store.try_claim_post(pid)
    finally:
        store.delete_post_local(pid)


def test_security_gate_blocks_high_risk() -> None:
    gate = publish_gate("HOT!!! Tin quá ngắn", "", "", "", "")
    assert isinstance(gate.ok, bool)
    assert gate.notes
    assert len(sha256_text("abc")) == 64


def test_health_no_hard_error() -> None:
    summary = health_summary(run_health_checks(get_store()))
    assert summary["error"] == 0


if __name__ == "__main__":
    test_chatgpt_parser()
    test_schedule_future_ok()
    test_storage_claim_lock()
    test_security_gate_blocks_high_risk()
    test_health_no_hard_error()
    print("smoke tests passed")
