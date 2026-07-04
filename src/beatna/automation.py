from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .article import ArticleInfo, merge_dedupe_articles, read_feed
from .composer import make_ai_post, make_rule_based_post
from .facebook import create_feed_post, create_first_comment
from .safety import check_post_safety
from .storage import BaseStore, utc_now_iso


def _retry_time(attempt_count: int) -> str:
    delay_minutes = min(240, max(5, 5 * (2 ** max(0, attempt_count - 1))))
    return (datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)).isoformat()


def publish_due_posts(store: BaseStore, limit: int = 10) -> list[dict[str, Any]]:
    due = store.list_due_posts(utc_now_iso(), limit=limit)
    results: list[dict[str, Any]] = []
    for row in due:
        post_id = row["id"]
        attempt_count = int(row.get("attempt_count") or 0) + 1
        try:
            claim_time = utc_now_iso()
            if not store.try_claim_post(post_id, claim_time):
                store.add_log(post_id, "publish_due_skip", True, "Bỏ qua vì bài đã được worker khác khóa/xử lý hoặc chưa đủ điều kiện retry.")
                continue
            store.update_post(post_id, error="", attempt_count=attempt_count, last_attempt_at=claim_time)
            fb = create_feed_post(row.get("post_text") or "", link=row.get("source_url") or None)
            fb_id = str(fb.get("id") or "")
            comment_id = ""
            first_comment = row.get("first_comment") or ""
            if first_comment and fb_id:
                try:
                    cmt = create_first_comment(fb_id, first_comment)
                    comment_id = str(cmt.get("id") or "")
                except Exception as comment_error:
                    store.add_log(post_id, "first_comment", False, f"Đã đăng bài nhưng lỗi bình luận nguồn: {comment_error}")
                    store.update_post(post_id, error=f"Đã đăng bài nhưng lỗi bình luận nguồn: {comment_error}")
            store.update_post(post_id, status="published", fb_post_id=fb_id, fb_comment_id=comment_id, locked_at="", next_retry_at="", publish_channel="facebook_page")
            store.add_log(post_id, "publish_due", True, f"Đã đăng Facebook post_id={fb_id}")
            results.append({"id": post_id, "ok": True, "fb_post_id": fb_id, "fb_comment_id": comment_id})
        except Exception as e:
            next_retry = _retry_time(attempt_count)
            status = "retry" if attempt_count < 5 else "error"
            store.update_post(post_id, status=status, error=str(e), locked_at="", next_retry_at=next_retry if status == "retry" else "")
            store.add_log(post_id, "publish_due", False, str(e), extra_json={"attempt_count": attempt_count, "next_retry_at": next_retry})
            results.append({"id": post_id, "ok": False, "error": str(e), "next_retry_at": next_retry, "attempt_count": attempt_count})
    return results


def schedule_local(store: BaseStore, post_id: str, scheduled_at_utc: str) -> None:
    store.update_post(post_id, status="queued", scheduled_at=scheduled_at_utc, schedule_mode="local_worker", error="")
    store.add_log(post_id, "schedule_local", True, f"Đã đưa vào hàng đợi nội bộ: {scheduled_at_utc}")


def mark_ready(store: BaseStore, post_id: str) -> None:
    store.update_post(post_id, status="ready", error="")
    store.add_log(post_id, "mark_ready", True, "Đã đánh dấu bài sẵn sàng")


def scan_feeds_to_cache(store: BaseStore, urls: list[str], per_feed: int = 15) -> dict[str, Any]:
    groups: list[list[ArticleInfo]] = []
    errors: list[str] = []
    for url in urls:
        try:
            groups.append(read_feed(url, limit=per_feed))
        except Exception as e:
            errors.append(f"{url}: {e}")
            try:
                for src in store.list_sources(include_disabled=True):
                    if src.get("url") == url:
                        store.update_source(src["id"], last_error=str(e), last_scan_at=utc_now_iso())
            except Exception:
                pass
    items = merge_dedupe_articles(groups)
    for item in items:
        try:
            store.upsert_article(item)
        except Exception as e:
            errors.append(f"Lưu cache lỗi {item.title}: {e}")
    try:
        for src in store.list_sources(include_disabled=True):
            if src.get("url") in urls:
                store.update_source(src["id"], last_scan_at=utc_now_iso(), last_error="")
    except Exception:
        pass
    store.add_log(None, "scan_feeds", len(errors) == 0, f"Quét {len(urls)} nguồn, lưu {len(items)} tin", extra_json={"errors": errors[:20]})
    return {"items": items, "errors": errors}


def article_row_to_info(row: dict[str, Any]) -> ArticleInfo:
    return ArticleInfo(
        title=row.get("title") or "",
        description=row.get("summary") or "",
        source_name=row.get("source_name") or "",
        url=row.get("url") or "",
        image=row.get("source_image") or "",
        published_at=row.get("published_at") or "",
        score=int(row.get("score") or 0),
        reason=row.get("reason") or "",
        content_hash=row.get("content_hash") or "",
        sensitivity=row.get("sensitivity") or "normal",
    )


def create_post_from_article(store: BaseStore, article: ArticleInfo, status: str = "draft", use_ai: bool = False, tone: str = "Tin nhanh", local_angle: str = "") -> str:
    existing = store.find_post_by_hash(article.content_hash)
    if existing:
        return str(existing.get("id"))
    result = make_ai_post(article.title, article.url, article.description, article.source_name, tone, local_angle) if use_ai else make_rule_based_post(article.title, article.url, article.description, article.source_name, tone, local_angle)
    safety = check_post_safety(result.get("post_text", ""), article.url, article.title, article.description)
    post_id = store.add_post(
        title=article.title,
        source_url=article.url,
        source_name=article.source_name,
        summary=article.description,
        source_image=article.image,
        post_text=result.get("post_text", ""),
        first_comment=result.get("first_comment", ""),
        image_note=result.get("image_note", ""),
        status=status,
        risk_score=safety.score,
        risk_level=safety.level,
        risk_notes=safety.to_dict(),
        tags=result.get("tags", ""),
        content_hash=article.content_hash,
        priority=int(article.score or 0),
        post_type="link" if article.url else "text",
        extra_json={"feed_score": article.score, "feed_reason": article.reason, "sensitivity": article.sensitivity},
        publish_channel="facebook_page",
    )
    store.mark_article_drafted(article.content_hash, post_id)
    store.add_log(post_id, "auto_create_post", True, f"Tạo bài từ tin cache, status={status}")
    return post_id


def auto_draft_hot_articles(store: BaseStore, min_score: int = 25, limit: int = 5, status: str = "draft", use_ai: bool = False) -> list[str]:
    rows = store.list_articles(min_score=min_score, limit=limit, include_drafted=False)
    created: list[str] = []
    for row in rows[:limit]:
        article = article_row_to_info(row)
        pid = create_post_from_article(store, article, status=status, use_ai=use_ai, tone="Tin nhanh")
        created.append(pid)
    store.add_log(None, "auto_draft_hot", True, f"Tạo {len(created)} bài từ tin hot", extra_json={"min_score": min_score, "status": status})
    return created
