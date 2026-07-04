from __future__ import annotations

import csv
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

from beatna.article import ArticleInfo, fetch_article_meta, merge_dedupe_articles, read_feed
from beatna.automation import (
    article_row_to_info,
    auto_draft_hot_articles,
    create_post_from_article,
    mark_ready,
    publish_due_posts,
    scan_feeds_to_cache,
    schedule_local,
)
from beatna.composer import make_ai_post, make_rule_based_post
from beatna.config import app_timezone, as_bool, as_int, rss_sources, secret
from beatna.facebook import create_feed_post, create_first_comment, create_photo_post, debug_token, delete_post, get_scheduled_posts, test_connection
from beatna.safety import check_post_safety
from beatna.scheduler import check_schedule_time, default_slots, human_delta_from_now, iso_to_local_text
from beatna.planner import analytics_summary, bucket_from_text, calendar_rows, duplicate_groups, make_backup_payload, next_smart_slots, post_quality, quality_table
from beatna.storage import get_store, storage_warning, utc_now_iso

try:
    from streamlit_autorefresh import st_autorefresh
except Exception:  # pragma: no cover
    st_autorefresh = None

TZ = ZoneInfo(app_timezone())
st.set_page_config(page_title="Beat Nghệ An AutoPost Pro v5 Ultra Stable", page_icon="📰", layout="wide", initial_sidebar_state="expanded")
store = get_store()


def now_local() -> datetime:
    return datetime.now(TZ)


def clean_status(status: str | None) -> str:
    mapping = {
        "draft": "📝 Nháp",
        "ready": "✅ Sẵn sàng",
        "queued": "🕒 Chờ app đăng",
        "scheduled_local": "🕒 Chờ app đăng",
        "scheduled_fb": "⏰ Đã hẹn Facebook",
        "retry": "🔁 Chờ thử lại",
        "published": "🚀 Đã đăng",
        "publishing": "📤 Đang đăng",
        "error": "⚠️ Lỗi",
    }
    return mapping.get(status or "", status or "")


def require_login() -> bool:
    app_password = str(secret("APP_PASSWORD", "") or "")
    if not app_password:
        st.warning("Chưa đặt APP_PASSWORD. App đang chạy demo; ai có link đều có thể vào.")
        return True
    if st.session_state.get("authed"):
        return True
    st.title("📰 Beat Nghệ An AutoPost Pro v5 Ultra Stable")
    st.caption("App riêng để quét tin, soạn bài, lưu Supabase, chống trùng, hẹn giờ và đăng Facebook Page.")
    pw = st.text_input("Mật khẩu app", type="password")
    if st.button("Đăng nhập", type="primary"):
        if pw == app_password:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Sai mật khẩu.")
    return False


@st.cache_data(ttl=180, show_spinner=False)
def cached_read_feed(url: str, limit: int) -> list[dict]:
    return [x.to_dict() for x in read_feed(url, limit=limit)]


@st.cache_data(ttl=120, show_spinner=False)
def cached_fetch_meta(url: str) -> dict:
    return fetch_article_meta(url).to_dict()


def active_feed_urls() -> list[str]:
    urls = list(rss_sources())
    for src in store.list_sources():
        if src.get("url"):
            urls.append(str(src.get("url")))
    return list(dict.fromkeys([u.strip() for u in urls if u and u.strip()]))


def load_hot_news_live(urls: list[str], per_feed: int = 12) -> tuple[list[ArticleInfo], list[str]]:
    groups: list[list[ArticleInfo]] = []
    errors: list[str] = []
    for url in urls:
        try:
            raw = cached_read_feed(url, per_feed)
            groups.append([ArticleInfo(**x) for x in raw])
        except Exception as e:
            errors.append(f"{url}: {e}")
    return merge_dedupe_articles(groups), errors


def article_from_state() -> ArticleInfo:
    return ArticleInfo(
        title=st.session_state.get("manual_title", ""),
        description=st.session_state.get("manual_summary", ""),
        source_name=st.session_state.get("manual_source", ""),
        url=st.session_state.get("manual_url", ""),
        image=st.session_state.get("manual_image", ""),
        published_at=st.session_state.get("manual_published_at", ""),
        score=int(st.session_state.get("manual_score", 0) or 0),
        reason=st.session_state.get("manual_reason", ""),
        content_hash=st.session_state.get("manual_content_hash", ""),
        sensitivity=st.session_state.get("manual_sensitivity", "normal"),
    )


def put_article_to_state(item: ArticleInfo) -> None:
    st.session_state["manual_title"] = item.title
    st.session_state["manual_summary"] = item.description
    st.session_state["manual_source"] = item.source_name
    st.session_state["manual_url"] = item.url
    st.session_state["manual_image"] = item.image
    st.session_state["manual_published_at"] = item.published_at
    st.session_state["manual_score"] = item.score
    st.session_state["manual_reason"] = item.reason
    st.session_state["manual_content_hash"] = item.content_hash
    st.session_state["manual_sensitivity"] = item.sensitivity


def generate_post(article: ArticleInfo, tone: str, use_ai: bool, local_angle: str) -> dict[str, str]:
    if use_ai:
        return make_ai_post(article.title, article.url, article.description, article.source_name, tone, local_angle)
    return make_rule_based_post(article.title, article.url, article.description, article.source_name, tone, local_angle)


def put_generated_to_state(result: dict[str, str]) -> None:
    st.session_state["post_text"] = result.get("post_text", "")
    st.session_state["first_comment"] = result.get("first_comment", "")
    st.session_state["image_note"] = result.get("image_note", "")
    st.session_state["tags"] = result.get("tags", "")
    st.session_state["ai_risk_note"] = result.get("risk_note", "")


def save_current_post(status: str = "draft", scheduled_at: str | None = None, schedule_mode: str = "manual", fb_post_id: str = "") -> str:
    article = article_from_state()
    post_text = st.session_state.get("post_text", "")
    first_comment = st.session_state.get("first_comment", "")
    safety = check_post_safety(post_text, article.url, article.title, article.description)
    existing = store.find_post_by_hash(article.content_hash)
    if existing and status in {"draft", "ready"}:
        store.update_post(existing["id"], status=status, post_text=post_text, first_comment=first_comment, image_note=st.session_state.get("image_note", ""), risk_score=safety.score, risk_level=safety.level, risk_notes=json.dumps(safety.to_dict(), ensure_ascii=False), scheduled_at=scheduled_at, schedule_mode=schedule_mode, fb_post_id=fb_post_id)
        return existing["id"]
    post_id = store.add_post(
        title=article.title,
        source_url=article.url,
        source_name=article.source_name,
        summary=article.description,
        source_image=article.image,
        post_text=post_text,
        first_comment=first_comment,
        image_note=st.session_state.get("image_note", ""),
        status=status,
        scheduled_at=scheduled_at,
        schedule_mode=schedule_mode,
        fb_post_id=fb_post_id,
        risk_score=safety.score,
        risk_level=safety.level,
        risk_notes=json.dumps(safety.to_dict(), ensure_ascii=False),
        tags=st.session_state.get("tags", ""),
        content_hash=article.content_hash,
        priority=int(article.score or 0),
        post_type="link" if article.url else "text",
        extra_json=json.dumps({"feed_score": article.score, "feed_reason": article.reason, "sensitivity": article.sensitivity}, ensure_ascii=False),
        publish_channel="facebook_page",
    )
    if article.content_hash:
        store.mark_article_drafted(article.content_hash, post_id)
    return post_id


def render_safety_box(post_text: str, source_url: str, title: str, summary: str) -> None:
    safety = check_post_safety(post_text, source_url, title, summary)
    if safety.score >= 65:
        st.error(f"Mức rủi ro: {safety.level} — {safety.score}/100")
    elif safety.score >= 35:
        st.warning(f"Mức rủi ro: {safety.level} — {safety.score}/100")
    else:
        st.success(f"Mức rủi ro: {safety.level} — {safety.score}/100")
    with st.expander("Chi tiết rà soát"):
        for note in safety.notes:
            st.write("- " + note)
        st.divider()
        for suggestion in safety.suggestions:
            st.write("- " + suggestion)


def post_row_table(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{
        "ID": r.get("id"),
        "Trạng thái": clean_status(r.get("status")),
        "Tiêu đề": r.get("title"),
        "Hẹn lúc": iso_to_local_text(r.get("scheduled_at")),
        "Còn lại": human_delta_from_now(r.get("scheduled_at")),
        "Nguồn": r.get("source_name") or r.get("source_url"),
        "Ưu tiên": r.get("priority") or 0,
        "Rủi ro": f"{r.get('risk_score') or 0}/100",
        "Lỗi": r.get("error") or "",
    } for r in rows])


def article_table(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{
        "Điểm": r.get("score") or 0,
        "Nhạy cảm": r.get("sensitivity") or "normal",
        "Trạng thái": r.get("status") or "new",
        "Tiêu đề": r.get("title"),
        "Nguồn": r.get("source_name"),
        "Lúc đăng": iso_to_local_text(r.get("published_at")),
        "Lý do": r.get("reason"),
        "Link": r.get("url"),
    } for r in rows])


def export_csv_bytes(rows: list[dict]) -> bytes:
    output = io.StringIO()
    if rows:
        writer = csv.DictWriter(output, fieldnames=sorted({k for row in rows for k in row.keys()}))
        writer.writeheader(); writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


if not require_login():
    st.stop()

with st.sidebar:
    st.markdown("### ⚙️ Beat Nghệ An v5 Ultra Stable")
    st.write("Kho dữ liệu:", f"**{store.backend_name}**")
    warn = storage_warning()
    if warn:
        st.warning("Supabase chưa dùng được, đang fallback SQLite. " + warn)
    st.write("Facebook Page:", "✅" if secret("FB_PAGE_ID", "") and secret("FB_PAGE_ACCESS_TOKEN", "") else "❌")
    st.write("AI:", "✅" if secret("OPENAI_API_KEY", "") else "Tắt")
    st.write("Timezone:", app_timezone())
    st.divider()
    auto_worker = st.toggle("Tự xử lý lịch khi app đang mở", value=as_bool(secret("AUTO_WORKER_DEFAULT", True), True))
    refresh_seconds = max(as_int("AUTO_REFRESH_SECONDS", 90), 45)
    if auto_worker and st_autorefresh:
        st_autorefresh(interval=refresh_seconds * 1000, key="queue_autorefresh_v5")
        results = publish_due_posts(store, limit=as_int("WORKER_BATCH_LIMIT", 5))
        if results:
            ok = sum(1 for x in results if x.get("ok"))
            st.toast(f"Đã xử lý lịch: {ok}/{len(results)} bài thành công")
    elif auto_worker:
        st.caption("Chưa cài streamlit-autorefresh; dùng nút xử lý lịch hoặc GitHub Actions worker.")
    if st.button("Đăng xuất"):
        st.session_state.pop("authed", None); st.rerun()

st.title("📰 Beat Nghệ An AutoPost Pro v5 Ultra Stable")
st.caption("Quét RSS → chống trùng tin → tự soạn nháp → kiểm tra chất lượng → lên calendar → hẹn giờ Facebook/Page hoặc hàng đợi nội bộ.")

tab_home, tab_hot, tab_compose, tab_schedule, tab_plan, tab_posts, tab_sources, tab_settings = st.tabs([
    "Tổng quan",
    "Tin hot & Auto-draft",
    "Soạn & đăng",
    "Lịch hẹn đăng bài",
    "Kế hoạch & chất lượng",
    "Kho bài",
    "Nguồn RSS",
    "Cài đặt & kiểm tra",
])

with tab_home:
    stats = store.stats(); counts = stats.get("posts_by_status", {})
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Nguồn RSS", stats.get("active_sources", 0))
    c2.metric("Tin đã cache", stats.get("articles_cached", 0))
    c3.metric("Tin hot chưa soạn", stats.get("undrafted_hot", 0))
    c4.metric("Nháp/Sẵn sàng", counts.get("draft", 0) + counts.get("ready", 0))
    c5.metric("Đang hẹn", counts.get("queued", 0) + counts.get("scheduled_local", 0) + counts.get("retry", 0) + counts.get("scheduled_fb", 0))
    c6.metric("Đã đăng", counts.get("published", 0))

    left, right = st.columns([1.35, 1])
    with left:
        st.subheader("Luồng dùng an toàn")
        st.info("Đề xuất: quét tin → auto-draft → bạn duyệt nhanh → hẹn giờ Facebook native. Tự động hóa mạnh nhưng vẫn giữ bước duyệt để tránh spam, sai nguồn hoặc rủi ro bản quyền.")
        due = store.list_due_posts(utc_now_iso(), limit=10)
        if due:
            st.warning(f"Có {len(due)} bài nội bộ đã đến giờ/chờ retry.")
            st.dataframe(post_row_table(due), use_container_width=True, hide_index=True)
            if st.button("Xử lý ngay bài đến giờ", type="primary", key="home_process_due"):
                st.write(publish_due_posts(store, limit=10)); st.rerun()
        else:
            st.success("Chưa có bài nội bộ nào quá giờ.")
    with right:
        st.subheader("Log mới nhất")
        logs = store.list_logs(limit=10)
        for log in logs:
            icon = "✅" if log.get("ok") else "⚠️"
            st.caption(f"{icon} {log.get('created_at')} — {log.get('action')}: {log.get('message')}")
        if not logs:
            st.caption("Chưa có log.")

with tab_hot:
    st.subheader("🔥 Tin hot & Auto-draft")
    urls = active_feed_urls()
    h1, h2, h3, h4 = st.columns([1, 1, 1, 1])
    per_feed = h1.slider("Số tin mỗi nguồn", 5, 40, 15)
    min_score = h2.slider("Điểm hot tối thiểu", 0, 100, 20)
    include_drafted = h3.toggle("Hiện cả tin đã soạn", value=False)
    use_ai_auto = h4.toggle("Auto-draft dùng AI", value=bool(secret("OPENAI_API_KEY", "")))

    b1, b2, b3 = st.columns([1, 1, 1])
    if b1.button("Quét RSS và lưu Supabase", type="primary", disabled=not urls):
        with st.spinner("Đang quét nguồn, chấm điểm và lưu cache..."):
            result = scan_feeds_to_cache(store, urls, per_feed=per_feed)
        st.success(f"Đã lưu {len(result['items'])} tin vào cache.")
        if result["errors"]:
            st.warning("Một số nguồn lỗi. Xem log/cảnh báo bên dưới.")
    if b2.button("Quét live không lưu", disabled=not urls):
        items, errors = load_hot_news_live(urls, per_feed=per_feed)
        st.session_state["hot_live_items"] = [x.to_dict() for x in items]
        st.session_state["hot_live_errors"] = errors
    if b3.button("Auto-draft 5 tin hot", type="secondary"):
        ids = auto_draft_hot_articles(store, min_score=min_score, limit=5, status="draft", use_ai=use_ai_auto)
        st.success(f"Đã tạo/nhận diện {len(ids)} nháp. Có chống trùng theo content_hash.")
        st.rerun()

    cached_rows = store.list_articles(min_score=min_score, limit=150, include_drafted=include_drafted)
    if cached_rows:
        st.markdown("#### Tin đã cache trong Supabase/SQLite")
        st.dataframe(article_table(cached_rows), use_container_width=True, hide_index=True)
        labels = [f"{r.get('score')}/100 | {r.get('title') or '(không tiêu đề)'} | {r.get('content_hash')}" for r in cached_rows]
        selected_label = st.selectbox("Chọn tin để xử lý nhanh", labels)
        row = cached_rows[labels.index(selected_label)]
        article = article_row_to_info(row)
        st.write(article.description or "Không có mô tả RSS.")
        st.code(article.url, language=None)
        a, b, c = st.columns(3)
        if a.button("Đưa sang tab Soạn"):
            put_article_to_state(article); st.success("Đã đưa tin sang tab Soạn & đăng.")
        if b.button("Tạo nháp từ tin này"):
            pid = create_post_from_article(store, article, status="draft", use_ai=use_ai_auto)
            st.success(f"Đã tạo/nhận diện nháp: {pid}")
        if c.button("Tạo bài Sẵn sàng"):
            pid = create_post_from_article(store, article, status="ready", use_ai=use_ai_auto)
            st.success(f"Đã tạo/nhận diện bài sẵn sàng: {pid}")
    else:
        st.caption("Chưa có tin cache. Bấm 'Quét RSS và lưu Supabase'.")

    live_errors = st.session_state.get("hot_live_errors", [])
    if live_errors:
        with st.expander("Nguồn lỗi khi quét live"):
            for e in live_errors: st.write("- " + e)
    live_items = [ArticleInfo(**x) for x in st.session_state.get("hot_live_items", []) if int(x.get("score") or 0) >= min_score]
    if live_items:
        st.markdown("#### Kết quả quét live")
        st.dataframe(article_table([{"score": x.score, "sensitivity": x.sensitivity, "status": "live", "title": x.title, "source_name": x.source_name, "published_at": x.published_at, "reason": x.reason, "url": x.url} for x in live_items]), use_container_width=True, hide_index=True)

with tab_compose:
    st.subheader("✍️ Soạn & đăng bài")
    with st.form("fetch_link_form"):
        link_to_fetch = st.text_input("Dán link báo để lấy metadata", value=st.session_state.get("fetch_url", ""))
        f1, f2 = st.columns([1, 4])
        fetch_btn = f1.form_submit_button("Lấy tin", type="primary")
        clear_btn = f2.form_submit_button("Xóa form")
    if fetch_btn:
        try:
            item = ArticleInfo(**cached_fetch_meta(link_to_fetch.strip()))
            put_article_to_state(item); store.upsert_article(item)
            st.success("Đã lấy thông tin bài báo và lưu cache chống trùng.")
        except Exception as e:
            st.error(f"Không lấy được metadata: {e}")
    if clear_btn:
        for key in ["manual_title", "manual_summary", "manual_source", "manual_url", "manual_image", "manual_published_at", "manual_score", "manual_reason", "manual_content_hash", "manual_sensitivity", "post_text", "first_comment", "image_note", "tags", "ai_risk_note"]:
            st.session_state.pop(key, None)
        st.rerun()

    left, right = st.columns([1.05, 1])
    with left:
        st.text_input("Tiêu đề", key="manual_title")
        st.text_area("Tóm tắt chắc chắn / mô tả nguồn", key="manual_summary", height=110)
        st.text_input("Nguồn", key="manual_source")
        st.text_input("Link nguồn", key="manual_url")
        st.text_input("Ảnh nguồn / og:image nếu có", key="manual_image")
        st.caption(f"Điểm hot: {st.session_state.get('manual_score', 0)} — {st.session_state.get('manual_reason', '')} — Nhạy cảm: {st.session_state.get('manual_sensitivity', 'normal')}")
    with right:
        tone = st.selectbox("Giọng bài", ["Tin nhanh", "Cảnh báo / dân sinh", "Nhẹ nhàng cộng đồng", "Hyperlocal xã/phường", "Thể thao / giải trí"])
        local_angle = st.text_input("Góc địa phương muốn nhấn", placeholder="Ví dụ: bà con Vinh/Cửa Lò/Nam Đàn cần chú ý...")
        use_ai = st.toggle("Dùng AI nếu có API key", value=bool(secret("OPENAI_API_KEY", "")))
        if st.button("Tạo bài đăng", type="primary"):
            article = article_from_state()
            result = generate_post(article, tone, use_ai=use_ai, local_angle=local_angle)
            put_generated_to_state(result); st.success("Đã tạo bài. Hãy đọc lại rồi đăng/hẹn giờ.")

    st.text_area("Nội dung bài đăng", key="post_text", height=260)
    st.text_area("Bình luận nguồn đầu tiên / ghim", key="first_comment", height=95)
    st.text_area("Gợi ý ảnh/link preview", key="image_note", height=80)
    if st.session_state.get("ai_risk_note"):
        st.caption("Note: " + st.session_state.get("ai_risk_note", ""))
    article = article_from_state()
    render_safety_box(st.session_state.get("post_text", ""), article.url, article.title, article.description)

    st.divider(); st.markdown("#### Lưu, đăng ngay hoặc hẹn giờ")
    a, b, c, d = st.columns(4)
    if a.button("Lưu nháp"):
        pid = save_current_post("draft"); store.add_log(pid, "save_draft", True, "Lưu nháp từ form soạn"); st.success(f"Đã lưu nháp: {pid}")
    if b.button("Lưu sẵn sàng"):
        pid = save_current_post("ready"); store.add_log(pid, "save_ready", True, "Lưu bài sẵn sàng từ form soạn"); st.success(f"Đã lưu bài sẵn sàng: {pid}")
    if c.button("Đăng link/text ngay", type="primary"):
        try:
            fb = create_feed_post(st.session_state.get("post_text", ""), link=article.url or None)
            fb_id = str(fb.get("id") or ""); comment_id = ""
            if st.session_state.get("first_comment", ""):
                try:
                    cm = create_first_comment(fb_id, st.session_state.get("first_comment", "")); comment_id = str(cm.get("id") or "")
                except Exception as ce:
                    st.warning(f"Đã đăng bài nhưng lỗi bình luận nguồn: {ce}")
            pid = save_current_post("published", fb_post_id=fb_id, schedule_mode="manual_now"); store.update_post(pid, fb_comment_id=comment_id)
            store.add_log(pid, "publish_now", True, f"Đăng ngay Facebook post_id={fb_id}"); st.success(f"Đã đăng Facebook: {fb_id}")
        except Exception as e:
            st.error(f"Lỗi đăng Facebook: {e}")
    with d:
        image_file = st.file_uploader("Ảnh tự thiết kế để đăng ngay", type=["png", "jpg", "jpeg", "webp"], label_visibility="collapsed")
        if image_file and st.button("Đăng ảnh ngay"):
            try:
                fb = create_photo_post(st.session_state.get("post_text", ""), image_file.getvalue(), image_file.name, getattr(image_file, "type", None))
                fb_id = str(fb.get("post_id") or fb.get("id") or "")
                pid = save_current_post("published", fb_post_id=fb_id, schedule_mode="photo_now"); store.add_log(pid, "publish_photo", True, f"Đăng ảnh Facebook post_id={fb_id}")
                st.success(f"Đã đăng ảnh Facebook: {fb_id}")
            except Exception as e:
                st.error(f"Lỗi đăng ảnh: {e}")

    st.markdown("##### Hẹn giờ bài này")
    sc1, sc2, sc3, sc4 = st.columns([1, 1, 1.2, 1])
    schedule_date = sc1.date_input("Ngày đăng", value=now_local().date())
    schedule_time = sc2.time_input("Giờ đăng", value=(now_local() + timedelta(hours=1)).time().replace(second=0, microsecond=0))
    schedule_mode = sc3.selectbox("Kiểu hẹn", ["Facebook native - ổn định nhất", "Hàng đợi nội bộ - cần app/worker chạy"])
    schedule_dt = datetime.combine(schedule_date, schedule_time).replace(tzinfo=TZ)
    schedule_check = check_schedule_time(schedule_dt, mode="facebook" if schedule_mode.startswith("Facebook") else "local")
    sc4.success(schedule_check.message) if schedule_check.ok else sc4.error(schedule_check.message)
    if st.button("Hẹn giờ đăng bài", type="primary", disabled=not schedule_check.ok):
        try:
            if schedule_mode.startswith("Facebook"):
                fb = create_feed_post(st.session_state.get("post_text", ""), link=article.url or None, scheduled_at=schedule_dt)
                fb_id = str(fb.get("id") or "")
                pid = save_current_post("scheduled_fb", scheduled_at=schedule_dt.astimezone(timezone.utc).isoformat(), schedule_mode="facebook_native", fb_post_id=fb_id)
                store.add_log(pid, "schedule_facebook", True, f"Đã hẹn trên Facebook: {fb_id}"); st.success(f"Đã hẹn giờ trên Facebook: {fb_id}")
            else:
                pid = save_current_post("queued", scheduled_at=schedule_dt.astimezone(timezone.utc).isoformat(), schedule_mode="local_worker")
                store.add_log(pid, "schedule_local", True, f"Đã đưa vào hàng đợi nội bộ: {schedule_dt}"); st.success(f"Đã đưa vào hàng đợi nội bộ: {pid}")
        except Exception as e:
            st.error(f"Lỗi hẹn giờ: {e}")

with tab_schedule:
    st.subheader("🗓️ Lịch hẹn đăng bài")
    st.info("Bài link/text nên ưu tiên Facebook native. Hàng đợi nội bộ dùng khi bạn bật app/worker GitHub Actions.")
    s1, s2, s3, s4 = st.columns([1, 1, 1, 1])
    if s1.button("Xử lý bài nội bộ đến giờ", type="primary"):
        st.write(publish_due_posts(store, limit=20)); st.rerun()
    slots_count = s2.slider("Số khung giờ gợi ý", 3, 30, 10)
    slots = default_slots(count=slots_count)
    s3.metric("Bài sẵn sàng", len(store.list_posts(status="ready", limit=500)))
    s4.caption("Khung giờ: " + ", ".join([x.strftime("%d/%m %H:%M") for x in slots[:5]]))

    scheduled_rows: list[dict] = []
    for stt in ["scheduled_fb", "queued", "scheduled_local", "retry"]:
        scheduled_rows.extend(store.list_posts(status=stt, limit=200))
    scheduled_rows = sorted(scheduled_rows, key=lambda r: r.get("scheduled_at") or "")
    st.markdown("#### Bài đang hẹn/chờ đăng")
    st.dataframe(post_row_table(scheduled_rows), use_container_width=True, hide_index=True) if scheduled_rows else st.caption("Chưa có bài đang hẹn giờ.")

    st.divider(); st.markdown("#### Xếp lịch hàng loạt cho bài đã sẵn sàng")
    ready_rows = store.list_posts(status="ready", limit=100)
    if not ready_rows:
        st.caption("Chưa có bài trạng thái Sẵn sàng.")
    else:
        st.dataframe(post_row_table(ready_rows[:50]), use_container_width=True, hide_index=True)
        batch_n = st.slider("Số bài muốn xếp lịch", 1, min(len(ready_rows), 30), min(len(ready_rows), 5))
        batch_mode = st.selectbox("Kiểu xếp lịch hàng loạt", ["Facebook native", "Hàng đợi nội bộ"])
        if st.button("Xếp lịch tự động theo khung giờ", type="primary"):
            done, failed = 0, []
            for row, slot in zip(ready_rows[:batch_n], slots):
                try:
                    if batch_mode == "Facebook native":
                        fb = create_feed_post(row.get("post_text") or "", link=row.get("source_url") or None, scheduled_at=slot)
                        store.update_post(row["id"], status="scheduled_fb", scheduled_at=slot.astimezone(timezone.utc).isoformat(), schedule_mode="facebook_native", fb_post_id=str(fb.get("id") or ""), error="")
                        store.add_log(row["id"], "batch_schedule_fb", True, f"Hẹn Facebook lúc {slot.isoformat()}")
                    else:
                        schedule_local(store, row["id"], slot.astimezone(timezone.utc).isoformat())
                    done += 1
                except Exception as e:
                    failed.append(f"{row.get('title')}: {e}"); store.add_log(row.get("id"), "batch_schedule", False, str(e))
            st.success(f"Đã xếp lịch {done} bài.")
            if failed: st.error("Một số bài lỗi:\n" + "\n".join(failed[:8]))
            st.rerun()


with tab_plan:
    st.subheader("🧭 Kế hoạch nội dung & kiểm soát chất lượng")
    st.info("Tab này dùng để vận hành Page lâu dài: nhìn lịch đăng như calendar, kiểm tra bài yếu, phát hiện trùng tin và tạo backup đầy đủ.")

    all_rows = store.list_posts(status=None, limit=1000)
    scheduled_like: list[dict] = []
    for stt in ["scheduled_fb", "queued", "scheduled_local", "retry", "published"]:
        scheduled_like.extend(store.list_posts(status=stt, limit=500))
    summary = analytics_summary(all_rows)

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Tổng bài", summary.get("total_posts", 0))
    m2.metric("Chất lượng TB", summary.get("avg_quality", 0))
    m3.metric("Rủi ro TB", summary.get("avg_risk", 0))
    m4.metric("Cần sửa", summary.get("needs_fix", 0))
    m5.metric("Bài hẹn/chờ", len([r for r in all_rows if r.get("status") in ["scheduled_fb", "queued", "scheduled_local", "retry"]]))

    plan_tab1, plan_tab2, plan_tab3, plan_tab4 = st.tabs(["Calendar", "Kế hoạch 7 ngày", "Checklist chất lượng", "Chống trùng & backup"])

    with plan_tab1:
        st.markdown("#### Calendar đăng bài")
        days = st.slider("Xem lịch trong bao nhiêu ngày tới", 3, 30, 14)
        cal = calendar_rows(scheduled_like, days=days)
        if cal:
            st.dataframe(pd.DataFrame(cal), use_container_width=True, hide_index=True)
        else:
            st.caption("Chưa có bài nào trong calendar. Hãy đánh dấu bài Sẵn sàng rồi xếp lịch.")
        st.markdown("#### Cơ cấu nội dung hiện có")
        by_campaign = summary.get("by_campaign", {})
        if by_campaign:
            st.dataframe(pd.DataFrame([{"Nhóm nội dung": k, "Số bài": v} for k, v in by_campaign.items()]), use_container_width=True, hide_index=True)

    with plan_tab2:
        st.markdown("#### Lập kế hoạch đăng thông minh")
        st.caption("Gợi ý khung giờ sẽ tránh bài đã hẹn quá sát nhau. Sau khi xem ổn, bạn có thể xếp lịch hàng loạt ở tab Lịch hẹn đăng bài.")
        ready_rows = store.list_posts(status="ready", limit=100)
        draft_rows = store.list_posts(status="draft", limit=100)
        c1, c2, c3 = st.columns(3)
        plan_count = c1.slider("Số bài cần lên kế hoạch", 3, 30, min(12, max(3, len(ready_rows) or 3)))
        min_gap = c2.slider("Khoảng cách tối thiểu giữa 2 bài", 45, 240, 90, step=15)
        source_pool = c3.selectbox("Nguồn bài", ["Chỉ bài Sẵn sàng", "Sẵn sàng + Nháp"])
        pool = ready_rows if source_pool == "Chỉ bài Sẵn sàng" else ready_rows + draft_rows
        slots = next_smart_slots(scheduled_like, count=plan_count, min_gap_minutes=min_gap)
        plan_rows = []
        for idx, (row, slot) in enumerate(zip(pool[:plan_count], slots), start=1):
            bucket = row.get("campaign") or bucket_from_text(row.get("title") or "", row.get("summary") or "")
            q = post_quality(row)
            plan_rows.append({
                "STT": idx,
                "Ngày": slot.strftime("%d/%m/%Y"),
                "Giờ": slot.strftime("%H:%M"),
                "Nhóm": bucket,
                "Chất lượng": q.score,
                "Trạng thái": row.get("status"),
                "Tiêu đề": row.get("title"),
                "ID": row.get("id"),
            })
        if plan_rows:
            st.dataframe(pd.DataFrame(plan_rows), use_container_width=True, hide_index=True)
            mode = st.radio("Khi bấm áp dụng", ["Chỉ chuyển nháp thành Sẵn sàng", "Xếp lịch nội bộ theo kế hoạch", "Hẹn Facebook native theo kế hoạch"], horizontal=False)
            if st.button("Áp dụng kế hoạch v5", type="primary"):
                done, failed = 0, []
                for row, slot in zip(pool[:plan_count], slots):
                    try:
                        if mode == "Chỉ chuyển nháp thành Sẵn sàng":
                            mark_ready(store, row["id"])
                            store.update_post(row["id"], campaign=row.get("campaign") or bucket_from_text(row.get("title") or "", row.get("summary") or ""))
                            store.add_log(row["id"], "v5_plan_ready", True, "Đưa vào kế hoạch nội dung và đánh dấu sẵn sàng")
                        elif mode == "Xếp lịch nội bộ theo kế hoạch":
                            if row.get("status") == "draft":
                                mark_ready(store, row["id"])
                            schedule_local(store, row["id"], slot.astimezone(timezone.utc).isoformat())
                            store.update_post(row["id"], campaign=row.get("campaign") or bucket_from_text(row.get("title") or "", row.get("summary") or ""))
                        else:
                            if row.get("status") == "draft":
                                mark_ready(store, row["id"])
                            fb = create_feed_post(row.get("post_text") or "", link=row.get("source_url") or None, scheduled_at=slot)
                            store.update_post(row["id"], status="scheduled_fb", scheduled_at=slot.astimezone(timezone.utc).isoformat(), schedule_mode="facebook_native_v5_plan", fb_post_id=str(fb.get("id") or ""), campaign=row.get("campaign") or bucket_from_text(row.get("title") or "", row.get("summary") or ""), error="")
                            store.add_log(row["id"], "v5_plan_schedule_fb", True, f"Hẹn Facebook theo kế hoạch v5: {slot.isoformat()}")
                        done += 1
                    except Exception as e:
                        failed.append(f"{row.get('title')}: {e}")
                        store.add_log(row.get("id"), "v5_plan_apply", False, str(e))
                st.success(f"Đã áp dụng {done} bài.")
                if failed:
                    st.error("Một số bài lỗi:\n" + "\n".join(failed[:10]))
                st.rerun()
        else:
            st.caption("Chưa đủ bài để lập kế hoạch. Hãy tạo nháp hoặc đánh dấu Sẵn sàng trước.")

    with plan_tab3:
        st.markdown("#### Checklist chất lượng hàng loạt")
        quality_status = st.multiselect("Trạng thái cần kiểm tra", ["draft", "ready", "queued", "scheduled_fb", "retry", "error", "published"], default=["draft", "ready"])
        rows_quality: list[dict] = []
        for stt in quality_status:
            rows_quality.extend(store.list_posts(status=stt, limit=300))
        qrows = quality_table(rows_quality)
        if qrows:
            st.dataframe(pd.DataFrame(qrows), use_container_width=True, hide_index=True)
            bad_ids = [x["ID"] for x in qrows if x["Điểm chất lượng"] < 60]
            if bad_ids and st.button("Chuyển các bài chất lượng thấp về Nháp"):
                for pid in bad_ids:
                    store.update_post(pid, status="draft", review_note="v5: Cần sửa trước khi đăng/hẹn giờ")
                    store.add_log(pid, "v5_quality_to_draft", True, "Chất lượng thấp, chuyển về nháp để sửa")
                st.warning(f"Đã chuyển {len(bad_ids)} bài về Nháp.")
                st.rerun()
        else:
            st.caption("Không có bài trong nhóm trạng thái đã chọn.")

    with plan_tab4:
        st.markdown("#### Phát hiện trùng tin")
        dup_rows = duplicate_groups(all_rows)
        if dup_rows:
            st.dataframe(pd.DataFrame(dup_rows), use_container_width=True, hide_index=True)
        else:
            st.success("Chưa phát hiện nhóm bài trùng rõ ràng trong kho hiện tại.")
        st.markdown("#### Backup đầy đủ")
        backup_bytes = make_backup_payload(
            posts=store.export_posts(),
            sources=store.list_sources(include_disabled=True),
            articles=store.list_articles(min_score=0, limit=5000, include_drafted=True),
            logs=store.list_logs(limit=5000),
        )
        st.download_button("Tải backup JSON đầy đủ", data=backup_bytes, file_name="beat_nghe_an_v5_full_backup.json", mime="application/json")
        e1, e2 = st.columns(2)
        if e1.button("Đưa tất cả bài lỗi về hàng chờ retry"):
            errors = store.list_posts(status="error", limit=200)
            for r in errors:
                store.update_post(r["id"], status="retry", next_retry_at=utc_now_iso(), error="")
                store.add_log(r["id"], "v5_requeue_error", True, "Đưa bài lỗi về retry")
            st.success(f"Đã đưa {len(errors)} bài lỗi về retry.")
            st.rerun()
        if e2.button("Ghi log health-check v5"):
            store.add_log(None, "v5_health_check", True, "App v5 hoạt động: storage, planner, quality, backup OK", extra_json=summary)
            st.success("Đã ghi log health-check.")


with tab_posts:
    st.subheader("📚 Kho bài")
    status_options = ["Tất cả", "draft", "ready", "queued", "scheduled_fb", "retry", "published", "error"]
    status_filter = st.selectbox("Lọc trạng thái", status_options, format_func=lambda x: "Tất cả" if x == "Tất cả" else clean_status(x))
    rows = store.list_posts(status=None if status_filter == "Tất cả" else status_filter, limit=500)
    if rows:
        st.dataframe(post_row_table(rows), use_container_width=True, hide_index=True)
        labels = [f"{clean_status(r.get('status'))} | {r.get('title') or '(không tiêu đề)'} | {r.get('id')}" for r in rows]
        selected_label = st.selectbox("Chọn bài để sửa/quản lý", labels)
        selected = rows[labels.index(selected_label)]; pid = selected["id"]
        with st.form("edit_post_form"):
            title = st.text_input("Tiêu đề", value=selected.get("title") or "")
            source_url = st.text_input("Link nguồn", value=selected.get("source_url") or "")
            post_text = st.text_area("Nội dung", value=selected.get("post_text") or "", height=250)
            first_comment = st.text_area("Bình luận nguồn", value=selected.get("first_comment") or "", height=100)
            image_note = st.text_area("Gợi ý ảnh/link", value=selected.get("image_note") or "", height=80)
            campaign = st.text_input("Chiến dịch/nhóm nội dung", value=selected.get("campaign") or "")
            review_note = st.text_area("Ghi chú duyệt nội bộ", value=selected.get("review_note") or "", height=70)
            col1, col2, col3 = st.columns(3)
            save_edit = col1.form_submit_button("Lưu sửa")
            set_ready = col2.form_submit_button("Đánh dấu sẵn sàng")
            delete_local = col3.form_submit_button("Xóa khỏi kho")
        if save_edit:
            safety = check_post_safety(post_text, source_url, title, selected.get("summary") or "")
            store.update_post(pid, title=title, source_url=source_url, post_text=post_text, first_comment=first_comment, image_note=image_note, campaign=campaign, review_note=review_note, risk_score=safety.score, risk_level=safety.level, risk_notes=json.dumps(safety.to_dict(), ensure_ascii=False))
            store.add_log(pid, "edit_post", True, "Đã sửa bài"); st.success("Đã lưu."); st.rerun()
        if set_ready:
            mark_ready(store, pid); st.success("Đã đánh dấu sẵn sàng."); st.rerun()
        if delete_local:
            store.delete_post_local(pid); st.warning("Đã xóa khỏi kho local/Supabase."); st.rerun()

        st.markdown("#### Hành động nhanh với bài đã chọn")
        q1, q2, q3, q4 = st.columns(4)
        if q1.button("Đăng ngay bài này", type="primary"):
            try:
                fb = create_feed_post(selected.get("post_text") or "", link=selected.get("source_url") or None)
                fb_id = str(fb.get("id") or ""); comment_id = ""
                if selected.get("first_comment"):
                    try:
                        c = create_first_comment(fb_id, selected.get("first_comment") or ""); comment_id = str(c.get("id") or "")
                    except Exception as ce:
                        st.warning(f"Đã đăng nhưng lỗi bình luận: {ce}")
                store.update_post(pid, status="published", fb_post_id=fb_id, fb_comment_id=comment_id, error="", schedule_mode="manual_now")
                store.add_log(pid, "publish_selected", True, f"Đăng ngay Facebook post_id={fb_id}"); st.success(f"Đã đăng: {fb_id}"); st.rerun()
            except Exception as e:
                store.update_post(pid, status="error", error=str(e)); store.add_log(pid, "publish_selected", False, str(e)); st.error(str(e))
        quick_slot = default_slots(count=1)[0]
        if q2.button(f"Xếp lịch nội bộ {quick_slot.strftime('%d/%m %H:%M')}"):
            schedule_local(store, pid, quick_slot.astimezone(timezone.utc).isoformat()); st.success("Đã xếp lịch nội bộ."); st.rerun()
        if q3.button(f"Hẹn Facebook {quick_slot.strftime('%d/%m %H:%M')}"):
            try:
                fb = create_feed_post(selected.get("post_text") or "", link=selected.get("source_url") or None, scheduled_at=quick_slot)
                store.update_post(pid, status="scheduled_fb", scheduled_at=quick_slot.astimezone(timezone.utc).isoformat(), schedule_mode="facebook_native", fb_post_id=str(fb.get("id") or ""), error="")
                store.add_log(pid, "schedule_selected_fb", True, f"Hẹn Facebook lúc {quick_slot.isoformat()}"); st.success("Đã hẹn Facebook."); st.rerun()
            except Exception as e:
                st.error(str(e))
        if q4.button("Xóa post trên Facebook", disabled=not bool(selected.get("fb_post_id"))):
            try:
                delete_post(selected.get("fb_post_id")); store.add_log(pid, "delete_facebook", True, f"Đã xóa Facebook post {selected.get('fb_post_id')}"); st.success("Đã gửi lệnh xóa post Facebook.")
            except Exception as e:
                st.error(str(e))
        with st.expander("Log của bài"):
            logs = store.list_logs(post_id=pid, limit=50)
            st.dataframe(pd.DataFrame(logs), use_container_width=True, hide_index=True) if logs else st.caption("Chưa có log bài này.")
    else:
        st.caption("Kho chưa có bài.")

with tab_sources:
    st.subheader("🛰️ Nguồn RSS")
    c1, c2 = st.columns([1, 1.4])
    with c1:
        with st.form("add_source_form_v4"):
            name = st.text_input("Tên nguồn", placeholder="Ví dụ: Báo Nghệ An")
            url = st.text_input("RSS URL", placeholder="https://.../rss")
            category = st.text_input("Nhóm nguồn", value="RSS")
            priority = st.slider("Ưu tiên nguồn", 1, 10, 5)
            if st.form_submit_button("Lưu nguồn", type="primary"):
                if not url.strip(): st.error("Chưa nhập RSS URL.")
                else:
                    store.add_source(name=name or url, url=url, category=category, priority=priority); st.success("Đã lưu nguồn RSS."); st.rerun()
    with c2:
        sources = store.list_sources(include_disabled=True)
        if sources:
            st.dataframe(pd.DataFrame(sources), use_container_width=True, hide_index=True)
            label = st.selectbox("Chọn nguồn để bật/tắt", [f"{'✅' if s.get('enabled') in [True, 1] else '❌'} {s.get('name')} | {s.get('id')}" for s in sources])
            src = sources[[f"{'✅' if s.get('enabled') in [True, 1] else '❌'} {s.get('name')} | {s.get('id')}" for s in sources].index(label)]
            toggle = st.toggle("Bật nguồn này", value=bool(src.get("enabled")))
            if st.button("Cập nhật nguồn"):
                store.update_source(src["id"], enabled=toggle); st.success("Đã cập nhật nguồn."); st.rerun()
        else:
            st.caption("Chưa có nguồn lưu trong DB. Bạn có thể cấu hình RSS_SOURCES trong Secrets.")

with tab_settings:
    st.subheader("🔌 Cài đặt & kiểm tra")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("#### Kiểm tra Facebook Page")
        if st.button("Test kết nối Page", type="primary"):
            try: st.json(test_connection())
            except Exception as e: st.error(str(e))
        if st.button("Debug token sâu"):
            try: st.json(debug_token())
            except Exception as e: st.error(str(e))
        if st.button("Lấy danh sách bài đã hẹn trên Facebook"):
            try:
                rows_sched = get_scheduled_posts(limit=50)
                st.dataframe(pd.DataFrame(rows_sched), use_container_width=True, hide_index=True) if rows_sched else st.info("Facebook chưa trả về bài hẹn giờ nào.")
            except Exception as e: st.error(str(e))
    with col2:
        st.markdown("#### Secrets checklist")
        checklist = {
            "APP_PASSWORD": bool(secret("APP_PASSWORD", "")),
            "STORAGE_BACKEND": secret("STORAGE_BACKEND", "supabase"),
            "SUPABASE_URL": bool(secret("SUPABASE_URL", "")),
            "SUPABASE_SERVICE_ROLE_KEY": bool(secret("SUPABASE_SERVICE_ROLE_KEY", "")),
            "FB_PAGE_ID": bool(secret("FB_PAGE_ID", "")),
            "FB_PAGE_ACCESS_TOKEN": bool(secret("FB_PAGE_ACCESS_TOKEN", "")),
            "FB_GRAPH_VERSION": secret("FB_GRAPH_VERSION", "v25.0"),
            "OPENAI_API_KEY": bool(secret("OPENAI_API_KEY", "")),
            "AUTO_REFRESH_SECONDS": secret("AUTO_REFRESH_SECONDS", "90"),
        }
        st.json(checklist)
        st.markdown("#### Export dữ liệu")
        st.download_button("Tải kho bài CSV", data=export_csv_bytes(store.export_posts()), file_name="beat_nghe_an_posts_export.csv", mime="text/csv")
        st.download_button("Tải log CSV", data=export_csv_bytes(store.list_logs(limit=1000)), file_name="beat_nghe_an_automation_logs.csv", mime="text/csv")

    st.divider()
    st.markdown("#### Gợi ý chạy nhanh, mượt, ổn định")
    st.write("- Dùng Supabase thay SQLite để không mất dữ liệu khi Streamlit ngủ/redeploy.")
    st.write("- Dùng Facebook native schedule cho bài link/text; hàng đợi nội bộ cần app đang mở hoặc GitHub Actions worker.")
    st.write("- AUTO_REFRESH_SECONDS nên để 60–180 giây. Đừng để quá thấp vì nặng app và dễ bị giới hạn API.")
    st.write("- RSS nên quét theo lô, cache vào article_cache, sau đó auto-draft từ cache để app mượt hơn.")
