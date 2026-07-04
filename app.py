from __future__ import annotations

import csv
import io
import json
import sys
import secrets as pysecrets
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
from beatna.config import app_timezone, app_version, as_bool, as_int, dry_run_mode, rss_sources, secret
from beatna.chatgpt_bridge import ImportedChatItem, imported_to_article, parse_chatgpt_export_bytes, parse_chatgpt_hourly_text, table_rows
from beatna.facebook import clear_session_credentials, create_feed_post, create_first_comment, create_photo_post, credential_source, debug_token, delete_post, get_pages_from_user_token, get_scheduled_posts, mask_token, oauth_configured, oauth_missing_items, build_oauth_login_url, exchange_code_for_user_token, exchange_long_lived_user_token, page_status_report, set_session_credentials, test_connection, test_connection_with_credentials
from beatna.health import health_rows, health_summary, run_health_checks
from beatna.safety import check_post_safety
from beatna.scheduler import check_schedule_time, default_slots, human_delta_from_now, iso_to_local_text
from beatna.planner import analytics_summary, bucket_from_text, calendar_rows, duplicate_groups, make_backup_payload, next_smart_slots, post_quality, quality_table
from beatna.storage import get_store, storage_warning, utc_now_iso
from beatna.security import backup_json_bytes, can_admin, can_write, login_role, publish_gate, security_check_rows, secret_matches

try:
    from streamlit_autorefresh import st_autorefresh
except Exception:  # pragma: no cover
    st_autorefresh = None

TZ = ZoneInfo(app_timezone())
st.set_page_config(page_title="Beat Nghệ An AutoPost Pro v11 OAuth Connect", page_icon="📰", layout="wide", initial_sidebar_state="expanded")
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
    # v10 supports role passwords. Legacy APP_PASSWORD still works as admin.
    has_login_secret = any(str(secret(k, "") or "") or str(secret(f"{k}_SHA256", "") or "") for k in ["ADMIN_PASSWORD", "EDITOR_PASSWORD", "VIEWER_PASSWORD", "APP_PASSWORD"])
    if not has_login_secret:
        if as_bool(secret("ALLOW_DEMO_MODE", False), False):
            st.warning("ALLOW_DEMO_MODE đang bật. App chạy demo; ai có link đều có thể vào.")
            st.session_state.setdefault("role", "demo")
            return True
        st.error("App chưa đặt mật khẩu nên đã tự khóa để an toàn.")
        st.info("Vào Streamlit Cloud → Manage app → Settings → Secrets, thêm ADMIN_PASSWORD hoặc APP_PASSWORD rồi reboot app.")
        st.code('ADMIN_PASSWORD = "mat-khau-admin-cua-ban"\nPAGE_CONNECT_PASSWORD = "mat-khau-rieng-ket-noi-page"', language="toml")
        return False
    if st.session_state.get("authed"):
        return True
    st.title("📰 Beat Nghệ An AutoPost Pro v11 OAuth Connect")
    st.caption("App riêng để nhập tin ChatGPT Hourly, quét RSS, soạn bài, kiểm tra rủi ro, lên lịch và đăng Facebook Page an toàn hơn.")
    pw = st.text_input("Mật khẩu app", type="password", key="auth_password_v10")
    if st.button("Đăng nhập", type="primary", key="auth_login_btn_v10"):
        role = login_role(pw)
        if role != "none":
            st.session_state["authed"] = True
            st.session_state["role"] = role
            st.rerun()
        else:
            st.error("Sai mật khẩu.")
    st.caption("Có thể dùng ADMIN_PASSWORD, EDITOR_PASSWORD, VIEWER_PASSWORD hoặc APP_PASSWORD trong Secrets.")
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
    st.markdown("### ⚙️ Beat Nghệ An v11 OAuth Connect")
    st.write("Kho dữ liệu:", f"**{store.backend_name}**")
    warn = storage_warning()
    if warn:
        st.warning("Supabase chưa dùng được, đang fallback SQLite. " + warn)
    fb_status = credential_source()
    st.write("Facebook Page:", "✅" if fb_status.get("source") != "missing" else "❌")
    st.caption(f"Kết nối: {fb_status.get('source')} · {fb_status.get('masked_token')}")
    st.write("Vai trò:", f"**{st.session_state.get('role', 'unknown')}**")
    if dry_run_mode():
        st.warning("DRY_RUN_MODE đang bật: test không đăng thật.")
    st.write("AI:", "✅" if secret("OPENAI_API_KEY", "") else "Tắt")
    st.write("Timezone:", app_timezone())
    st.divider()
    auto_worker = st.toggle("Tự xử lý lịch khi app đang mở", value=as_bool(secret("AUTO_WORKER_DEFAULT", True), True), key="sidebar_auto_worker_v10")
    refresh_seconds = max(as_int("AUTO_REFRESH_SECONDS", 90), 45)
    if auto_worker and st_autorefresh:
        st_autorefresh(interval=refresh_seconds * 1000, key="queue_autorefresh_v10")
        results = publish_due_posts(store, limit=as_int("WORKER_BATCH_LIMIT", 5))
        if results:
            ok = sum(1 for x in results if x.get("ok"))
            st.toast(f"Đã xử lý lịch: {ok}/{len(results)} bài thành công")
    elif auto_worker:
        st.caption("Chưa cài streamlit-autorefresh; dùng nút xử lý lịch hoặc GitHub Actions worker.")
    if st.button("Đăng xuất", key="sidebar_logout_v10"):
        st.session_state.pop("authed", None); st.rerun()

st.title("📰 Beat Nghệ An AutoPost Pro v11 OAuth Connect")
st.caption("Quét RSS → chống trùng tin → tự soạn nháp → kiểm tra chất lượng → lên calendar → kết nối Facebook OAuth → hẹn giờ Facebook/Page hoặc hàng đợi nội bộ.")

tab_home, tab_chatgpt, tab_hot, tab_compose, tab_schedule, tab_plan, tab_posts, tab_sources, tab_connect, tab_ops, tab_settings = st.tabs([
    "Tổng quan",
    "Nhập từ ChatGPT",
    "Tin hot & Auto-draft",
    "Soạn & đăng",
    "Lịch hẹn đăng bài",
    "Kế hoạch & chất lượng",
    "Kho bài",
    "Nguồn RSS",
    "Kết nối Facebook Page",
    "An toàn & vận hành",
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
            st.dataframe(post_row_table(due), use_container_width=True, hide_index=True, key="df_home_due_posts_v10")
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


with tab_chatgpt:
    st.subheader("🤝 Nhập tin từ ChatGPT / Beat Nghệ An Hourly")
    st.info("App không đọc trực tiếp lịch sử ChatGPT riêng tư của bạn. Cách ổn định nhất là copy nội dung từ đoạn chat Beat Nghệ An Hourly dán vào đây, hoặc tải file export ChatGPT rồi upload. App sẽ tự tách link nguồn, tiêu đề, bài đã soạn, bình luận nguồn và tạo nháp/sẵn sàng.")

    import_mode = st.radio("Cách nhập", ["Dán nội dung từ đoạn chat", "Upload export ChatGPT / file txt-json-zip"], horizontal=True, key="chatgpt_import_mode_v10")
    title_filter = "Beat Nghệ An"
    raw_text = ""
    uploaded = None
    if import_mode == "Dán nội dung từ đoạn chat":
        raw_text = st.text_area("Dán nguyên đoạn trả lời từ Beat Nghệ An Hourly", height=300, placeholder="Dán các bài/tin có link nguồn ở đây...\nVí dụ: tiêu đề, nội dung bài đăng, bình luận nguồn, link báo", key="chatgpt_raw_text_v10")
    else:
        c1, c2 = st.columns([1, 1])
        uploaded = c1.file_uploader("Upload file .txt, .json hoặc .zip export từ ChatGPT", type=["txt", "json", "zip"], key="chatgpt_export_file_v10")
        title_filter = c2.text_input("Lọc tên cuộc chat khi đọc export", value="Beat Nghệ An", key="chatgpt_title_filter_v10")
        st.caption("Với file export ChatGPT, app sẽ tìm conversations.json và ưu tiên các cuộc chat có tiêu đề chứa cụm lọc này.")

    p1, p2, p3, p4 = st.columns([1, 1, 1, 1])
    max_items = p1.slider("Tối đa số tin tách", 5, 100, 40, key="chatgpt_max_items_v10")
    min_conf = p2.slider("Độ tin cậy tối thiểu", 40, 100, 60, key="chatgpt_min_conf_v10")
    use_ai_import = p3.toggle("Khi cần thì dùng AI viết lại", value=bool(secret("OPENAI_API_KEY", "")), key="chatgpt_use_ai_import_v10")
    default_status = p4.selectbox("Trạng thái khi nhập", ["draft", "ready"], format_func=lambda x: "Nháp" if x == "draft" else "Sẵn sàng", key="chatgpt_default_status_v10")

    if st.button("Phân tích nội dung ChatGPT", type="primary", key="chatgpt_parse_btn_v10"):
        try:
            if import_mode == "Dán nội dung từ đoạn chat":
                items = parse_chatgpt_hourly_text(raw_text, max_items=max_items)
            else:
                if not uploaded:
                    st.error("Bạn chưa upload file.")
                    items = []
                else:
                    items = parse_chatgpt_export_bytes(uploaded.getvalue(), uploaded.name, title_filter=title_filter)[:max_items]
            items = [x for x in items if int(x.confidence or 0) >= min_conf]
            st.session_state["chatgpt_import_items"] = [x.to_dict() for x in items]
            st.success(f"Đã tách được {len(items)} tin đủ điều kiện.")
        except Exception as e:
            st.error(f"Lỗi phân tích: {e}")

    import_items = st.session_state.get("chatgpt_import_items", [])
    if import_items:
        st.markdown("#### Tin đã tách từ ChatGPT")
        st.dataframe(pd.DataFrame(table_rows(import_items)), use_container_width=True, hide_index=True, key="df_chatgpt_items_v10")
        labels = [f"{i+1}. {x.get('title') or '(không tiêu đề)'} | {x.get('source_url')}" for i, x in enumerate(import_items)]
        selected_labels = st.multiselect("Chọn tin muốn nhập", labels, default=labels[:min(5, len(labels))], key="chatgpt_selected_labels_v10")
        selected_idx = [labels.index(x) for x in selected_labels]
        selected_items = [import_items[i] for i in selected_idx]

        col_a, col_b, col_c, col_d = st.columns(4)
        if col_a.button("Đưa 1 tin sang tab Soạn", disabled=not bool(selected_items), key="chatgpt_send_compose_v10"):
            item = ImportedChatItem(**selected_items[0])
            article = imported_to_article(item)
            put_article_to_state(article)
            st.session_state["post_text"] = item.post_text
            st.session_state["first_comment"] = item.first_comment or f"Nguồn: {item.source_url}"
            st.session_state["image_note"] = item.image_note
            st.success("Đã đưa tin đầu tiên sang tab Soạn & đăng.")

        if col_b.button("Chỉ lưu cache tin", disabled=not bool(selected_items), key="chatgpt_save_cache_v10"):
            done = 0
            for raw in selected_items:
                item = ImportedChatItem(**raw)
                article = imported_to_article(item)
                store.upsert_article(article)
                done += 1
            store.add_log(None, "chatgpt_import_cache", True, f"Lưu cache {done} tin từ ChatGPT")
            st.success(f"Đã lưu cache {done} tin.")
            st.rerun()

        if col_c.button("Tạo nháp từ tin đã chọn", disabled=not bool(selected_items), key="chatgpt_create_drafts_v10"):
            done, reused = 0, 0
            for raw in selected_items:
                item = ImportedChatItem(**raw)
                article = imported_to_article(item)
                store.upsert_article(article)
                existing = store.find_post_by_hash(article.content_hash)
                if existing:
                    reused += 1
                    continue
                if item.post_text:
                    safety = check_post_safety(item.post_text, item.source_url, item.title, item.summary)
                    pid = store.add_post(
                        title=item.title,
                        source_url=item.source_url,
                        source_name=item.source_name,
                        summary=item.summary,
                        source_image="",
                        post_text=item.post_text,
                        first_comment=item.first_comment or f"Nguồn: {item.source_url}",
                        image_note=item.image_note,
                        status="draft",
                        risk_score=safety.score,
                        risk_level=safety.level,
                        risk_notes=json.dumps(safety.to_dict(), ensure_ascii=False),
                        tags="#BeatNgheAn #NgheAn #TinNgheAn",
                        content_hash=article.content_hash,
                        priority=int(article.score or 0),
                        post_type="link" if item.source_url else "text",
                        extra_json=json.dumps({"import_from": "chatgpt_hourly", "confidence": item.confidence, "note": item.note}, ensure_ascii=False),
                        publish_channel="facebook_page",
                    )
                    store.mark_article_drafted(article.content_hash, pid)
                else:
                    pid = create_post_from_article(store, article, status="draft", use_ai=use_ai_import)
                store.add_log(pid, "chatgpt_import_draft", True, "Tạo nháp từ ChatGPT/Beat Nghệ An Hourly")
                done += 1
            st.success(f"Đã tạo {done} nháp. Bỏ qua {reused} tin đã có trong kho.")
            st.rerun()

        if col_d.button("Tạo bài Sẵn sàng", disabled=not bool(selected_items), key="chatgpt_create_ready_v10"):
            done, reused = 0, 0
            for raw in selected_items:
                item = ImportedChatItem(**raw)
                article = imported_to_article(item)
                store.upsert_article(article)
                existing = store.find_post_by_hash(article.content_hash)
                if existing:
                    store.update_post(existing["id"], status="ready")
                    reused += 1
                    continue
                if item.post_text:
                    safety = check_post_safety(item.post_text, item.source_url, item.title, item.summary)
                    pid = store.add_post(
                        title=item.title,
                        source_url=item.source_url,
                        source_name=item.source_name,
                        summary=item.summary,
                        source_image="",
                        post_text=item.post_text,
                        first_comment=item.first_comment or f"Nguồn: {item.source_url}",
                        image_note=item.image_note,
                        status="ready",
                        risk_score=safety.score,
                        risk_level=safety.level,
                        risk_notes=json.dumps(safety.to_dict(), ensure_ascii=False),
                        tags="#BeatNgheAn #NgheAn #TinNgheAn",
                        content_hash=article.content_hash,
                        priority=int(article.score or 0),
                        post_type="link" if item.source_url else "text",
                        extra_json=json.dumps({"import_from": "chatgpt_hourly", "confidence": item.confidence, "note": item.note}, ensure_ascii=False),
                        publish_channel="facebook_page",
                    )
                    store.mark_article_drafted(article.content_hash, pid)
                else:
                    pid = create_post_from_article(store, article, status="ready", use_ai=use_ai_import)
                store.add_log(pid, "chatgpt_import_ready", True, "Tạo bài sẵn sàng từ ChatGPT/Beat Nghệ An Hourly")
                done += 1
            st.success(f"Đã tạo {done} bài sẵn sàng. Cập nhật {reused} bài đã có thành Sẵn sàng.")
            st.rerun()

        with st.expander("Xem trước bài đầu tiên đã chọn"):
            if selected_items:
                item = ImportedChatItem(**selected_items[0])
                st.markdown("**Tiêu đề:** " + item.title)
                st.code(item.source_url, language=None)
                st.text_area("Bài đăng", value=item.post_text, height=220, disabled=True, key="chatgpt_preview_post_text_v10")
                st.text_area("Bình luận nguồn", value=item.first_comment or f"Nguồn: {item.source_url}", height=80, disabled=True, key="chatgpt_preview_first_comment_v10")
                st.text_area("Gợi ý ảnh/link", value=item.image_note, height=80, disabled=True, key="chatgpt_preview_image_note_v10")
    else:
        st.caption("Chưa có dữ liệu. Dán nội dung Beat Nghệ An Hourly hoặc upload file export rồi bấm Phân tích.")

    st.divider()
    st.markdown("#### Cách dùng nhanh")
    st.write("1. Ở ChatGPT, mở đoạn chat Beat Nghệ An Hourly, copy phần tin bạn muốn dùng.")
    st.write("2. Dán vào ô trên và bấm Phân tích nội dung ChatGPT.")
    st.write("3. Chọn các tin đúng, tạo Nháp hoặc Sẵn sàng.")
    st.write("4. Sang Lịch hẹn đăng bài để xếp lịch Facebook native hoặc hàng đợi nội bộ.")

with tab_hot:
    st.subheader("🔥 Tin hot & Auto-draft")
    urls = active_feed_urls()
    h1, h2, h3, h4 = st.columns([1, 1, 1, 1])
    per_feed = h1.slider("Số tin mỗi nguồn", 5, 40, 15, key="hot_per_feed_v10")
    min_score = h2.slider("Điểm hot tối thiểu", 0, 100, 20, key="hot_min_score_v10")
    include_drafted = h3.toggle("Hiện cả tin đã soạn", value=False, key="hot_include_drafted_v10")
    use_ai_auto = h4.toggle("Auto-draft dùng AI", value=bool(secret("OPENAI_API_KEY", "")), key="hot_use_ai_auto_v10")

    b1, b2, b3 = st.columns([1, 1, 1])
    if b1.button("Quét RSS và lưu Supabase", type="primary", disabled=not urls, key="hot_scan_save_v10"):
        with st.spinner("Đang quét nguồn, chấm điểm và lưu cache..."):
            result = scan_feeds_to_cache(store, urls, per_feed=per_feed)
        st.success(f"Đã lưu {len(result['items'])} tin vào cache.")
        if result["errors"]:
            st.warning("Một số nguồn lỗi. Xem log/cảnh báo bên dưới.")
    if b2.button("Quét live không lưu", disabled=not urls, key="hot_scan_live_v10"):
        items, errors = load_hot_news_live(urls, per_feed=per_feed)
        st.session_state["hot_live_items"] = [x.to_dict() for x in items]
        st.session_state["hot_live_errors"] = errors
    if b3.button("Auto-draft 5 tin hot", type="secondary", key="hot_auto_draft_v10"):
        ids = auto_draft_hot_articles(store, min_score=min_score, limit=5, status="draft", use_ai=use_ai_auto)
        st.success(f"Đã tạo/nhận diện {len(ids)} nháp. Có chống trùng theo content_hash.")
        st.rerun()

    cached_rows = store.list_articles(min_score=min_score, limit=150, include_drafted=include_drafted)
    if cached_rows:
        st.markdown("#### Tin đã cache trong Supabase/SQLite")
        st.dataframe(article_table(cached_rows), use_container_width=True, hide_index=True, key="df_cached_articles_v10")
        labels = [f"{r.get('score')}/100 | {r.get('title') or '(không tiêu đề)'} | {r.get('content_hash')}" for r in cached_rows]
        selected_label = st.selectbox("Chọn tin để xử lý nhanh", labels, key="hot_selected_article_v10")
        row = cached_rows[labels.index(selected_label)]
        article = article_row_to_info(row)
        st.write(article.description or "Không có mô tả RSS.")
        st.code(article.url, language=None)
        a, b, c = st.columns(3)
        if a.button("Đưa sang tab Soạn", key="hot_to_compose_v10"):
            put_article_to_state(article); st.success("Đã đưa tin sang tab Soạn & đăng.")
        if b.button("Tạo nháp từ tin này", key="hot_create_draft_this_v10"):
            pid = create_post_from_article(store, article, status="draft", use_ai=use_ai_auto)
            st.success(f"Đã tạo/nhận diện nháp: {pid}")
        if c.button("Tạo bài Sẵn sàng", key="hot_create_ready_this_v10"):
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
        st.dataframe(article_table([{"score": x.score, "sensitivity": x.sensitivity, "status": "live", "title": x.title, "source_name": x.source_name, "published_at": x.published_at, "reason": x.reason, "url": x.url} for x in live_items]), use_container_width=True, hide_index=True, key="df_live_items_v10")

with tab_compose:
    st.subheader("✍️ Soạn & đăng bài")
    with st.form("fetch_link_form"):
        link_to_fetch = st.text_input("Dán link báo để lấy metadata", value=st.session_state.get("fetch_url", ""), key="compose_fetch_url_v10")
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
        tone = st.selectbox("Giọng bài", ["Tin nhanh", "Cảnh báo / dân sinh", "Nhẹ nhàng cộng đồng", "Hyperlocal xã/phường", "Thể thao / giải trí"], key="compose_tone_v10")
        local_angle = st.text_input("Góc địa phương muốn nhấn", placeholder="Ví dụ: bà con Vinh/Cửa Lò/Nam Đàn cần chú ý...", key="compose_local_angle_v10")
        use_ai = st.toggle("Dùng AI nếu có API key", value=bool(secret("OPENAI_API_KEY", "")), key="compose_use_ai_v10")
        if st.button("Tạo bài đăng", type="primary", key="compose_generate_btn_v10"):
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
    if a.button("Lưu nháp", key="compose_save_draft_v10"):
        pid = save_current_post("draft"); store.add_log(pid, "save_draft", True, "Lưu nháp từ form soạn"); st.success(f"Đã lưu nháp: {pid}")
    if b.button("Lưu sẵn sàng", key="compose_save_ready_v10"):
        pid = save_current_post("ready"); store.add_log(pid, "save_ready", True, "Lưu bài sẵn sàng từ form soạn"); st.success(f"Đã lưu bài sẵn sàng: {pid}")
    if c.button("Đăng link/text ngay", type="primary", key="compose_publish_now_v10"):
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
        image_file = st.file_uploader("Ảnh tự thiết kế để đăng ngay", type=["png", "jpg", "jpeg", "webp"], label_visibility="collapsed", key="compose_photo_upload_v10")
        if image_file and st.button("Đăng ảnh ngay", key="compose_publish_photo_v10"):
            try:
                fb = create_photo_post(st.session_state.get("post_text", ""), image_file.getvalue(), image_file.name, getattr(image_file, "type", None))
                fb_id = str(fb.get("post_id") or fb.get("id") or "")
                pid = save_current_post("published", fb_post_id=fb_id, schedule_mode="photo_now"); store.add_log(pid, "publish_photo", True, f"Đăng ảnh Facebook post_id={fb_id}")
                st.success(f"Đã đăng ảnh Facebook: {fb_id}")
            except Exception as e:
                st.error(f"Lỗi đăng ảnh: {e}")

    st.markdown("##### Hẹn giờ bài này")
    sc1, sc2, sc3, sc4 = st.columns([1, 1, 1.2, 1])
    schedule_date = sc1.date_input("Ngày đăng", value=now_local().date(), key="compose_schedule_date_v10")
    schedule_time = sc2.time_input("Giờ đăng", value=(now_local() + timedelta(hours=1)).time().replace(second=0, microsecond=0), key="compose_schedule_time_v10")
    schedule_mode = sc3.selectbox("Kiểu hẹn", ["Facebook native - ổn định nhất", "Hàng đợi nội bộ - cần app/worker chạy"], key="compose_schedule_mode_v10")
    schedule_dt = datetime.combine(schedule_date, schedule_time).replace(tzinfo=TZ)
    schedule_check = check_schedule_time(schedule_dt, mode="facebook" if schedule_mode.startswith("Facebook") else "local")
    sc4.success(schedule_check.message) if schedule_check.ok else sc4.error(schedule_check.message)
    if st.button("Hẹn giờ đăng bài", type="primary", disabled=not schedule_check.ok, key="compose_schedule_btn_v10"):
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
    if s1.button("Xử lý bài nội bộ đến giờ", type="primary", key="schedule_process_due_v10"):
        st.write(publish_due_posts(store, limit=20)); st.rerun()
    slots_count = s2.slider("Số khung giờ gợi ý", 3, 30, 10, key="schedule_slots_count_v10")
    slots = default_slots(count=slots_count)
    s3.metric("Bài sẵn sàng", len(store.list_posts(status="ready", limit=500)))
    s4.caption("Khung giờ: " + ", ".join([x.strftime("%d/%m %H:%M") for x in slots[:5]]))

    scheduled_rows: list[dict] = []
    for stt in ["scheduled_fb", "queued", "scheduled_local", "retry"]:
        scheduled_rows.extend(store.list_posts(status=stt, limit=200))
    scheduled_rows = sorted(scheduled_rows, key=lambda r: r.get("scheduled_at") or "")
    st.markdown("#### Bài đang hẹn/chờ đăng")
    st.dataframe(post_row_table(scheduled_rows), use_container_width=True, hide_index=True, key="df_scheduled_rows_v10") if scheduled_rows else st.caption("Chưa có bài đang hẹn giờ.")

    st.divider(); st.markdown("#### Xếp lịch hàng loạt cho bài đã sẵn sàng")
    ready_rows = store.list_posts(status="ready", limit=100)
    if not ready_rows:
        st.caption("Chưa có bài trạng thái Sẵn sàng.")
    else:
        st.dataframe(post_row_table(ready_rows[:50]), use_container_width=True, hide_index=True, key="df_schedule_ready_rows_v10")
        batch_n = st.slider("Số bài muốn xếp lịch", 1, min(len(ready_rows), 30), min(len(ready_rows), 5), key="schedule_batch_n_v10")
        batch_mode = st.selectbox("Kiểu xếp lịch hàng loạt", ["Facebook native", "Hàng đợi nội bộ"], key="schedule_batch_mode_v10")
        if st.button("Xếp lịch tự động theo khung giờ", type="primary", key="schedule_batch_apply_v10"):
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
        days = st.slider("Xem lịch trong bao nhiêu ngày tới", 3, 30, 14, key="plan_calendar_days_v10")
        cal = calendar_rows(scheduled_like, days=days)
        if cal:
            st.dataframe(pd.DataFrame(cal), use_container_width=True, hide_index=True, key="df_plan_calendar_v10")
        else:
            st.caption("Chưa có bài nào trong calendar. Hãy đánh dấu bài Sẵn sàng rồi xếp lịch.")
        st.markdown("#### Cơ cấu nội dung hiện có")
        by_campaign = summary.get("by_campaign", {})
        if by_campaign:
            st.dataframe(pd.DataFrame([{"Nhóm nội dung": k, "Số bài": v} for k, v in by_campaign.items()]), use_container_width=True, hide_index=True, key="df_plan_campaigns_v10")

    with plan_tab2:
        st.markdown("#### Lập kế hoạch đăng thông minh")
        st.caption("Gợi ý khung giờ sẽ tránh bài đã hẹn quá sát nhau. Sau khi xem ổn, bạn có thể xếp lịch hàng loạt ở tab Lịch hẹn đăng bài.")
        ready_rows = store.list_posts(status="ready", limit=100)
        draft_rows = store.list_posts(status="draft", limit=100)
        c1, c2, c3 = st.columns(3)
        plan_count = c1.slider("Số bài cần lên kế hoạch", 3, 30, min(12, max(3, len(ready_rows) or 3)), key="plan_count_v10")
        min_gap = c2.slider("Khoảng cách tối thiểu giữa 2 bài", 45, 240, 90, step=15, key="plan_min_gap_v10")
        source_pool = c3.selectbox("Nguồn bài", ["Chỉ bài Sẵn sàng", "Sẵn sàng + Nháp"], key="plan_source_pool_v10")
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
            st.dataframe(pd.DataFrame(plan_rows), use_container_width=True, hide_index=True, key="df_plan_rows_v10")
            mode = st.radio("Khi bấm áp dụng", ["Chỉ chuyển nháp thành Sẵn sàng", "Xếp lịch nội bộ theo kế hoạch", "Hẹn Facebook native theo kế hoạch"], horizontal=False, key="plan_apply_mode_v10")
            if st.button("Áp dụng kế hoạch", type="primary", key="plan_apply_btn_v10"):
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
        quality_status = st.multiselect("Trạng thái cần kiểm tra", ["draft", "ready", "queued", "scheduled_fb", "retry", "error", "published"], default=["draft", "ready"], key="plan_quality_status_v10")
        rows_quality: list[dict] = []
        for stt in quality_status:
            rows_quality.extend(store.list_posts(status=stt, limit=300))
        qrows = quality_table(rows_quality)
        if qrows:
            st.dataframe(pd.DataFrame(qrows), use_container_width=True, hide_index=True, key="df_quality_rows_v10")
            bad_ids = [x["ID"] for x in qrows if x["Điểm chất lượng"] < 60]
            if bad_ids and st.button("Chuyển các bài chất lượng thấp về Nháp", key="plan_quality_to_draft_v10"):
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
            st.dataframe(pd.DataFrame(dup_rows), use_container_width=True, hide_index=True, key="df_duplicate_rows_v10")
        else:
            st.success("Chưa phát hiện nhóm bài trùng rõ ràng trong kho hiện tại.")
        st.markdown("#### Backup đầy đủ")
        backup_bytes = make_backup_payload(
            posts=store.export_posts(),
            sources=store.list_sources(include_disabled=True),
            articles=store.list_articles(min_score=0, limit=5000, include_drafted=True),
            logs=store.list_logs(limit=5000),
        )
        st.download_button("Tải backup JSON đầy đủ", data=backup_bytes, file_name="beat_nghe_an_v10_full_backup.json", mime="application/json", key="plan_backup_download_v10")
        e1, e2 = st.columns(2)
        if e1.button("Đưa tất cả bài lỗi về hàng chờ retry", key="plan_requeue_errors_v10"):
            errors = store.list_posts(status="error", limit=200)
            for r in errors:
                store.update_post(r["id"], status="retry", next_retry_at=utc_now_iso(), error="")
                store.add_log(r["id"], "v5_requeue_error", True, "Đưa bài lỗi về retry")
            st.success(f"Đã đưa {len(errors)} bài lỗi về retry.")
            st.rerun()
        if e2.button("Ghi log health-check", key="plan_health_log_v10"):
            store.add_log(None, "v5_health_check", True, "App v5 hoạt động: storage, planner, quality, backup OK", extra_json=summary)
            st.success("Đã ghi log health-check.")


with tab_posts:
    st.subheader("📚 Kho bài")
    status_options = ["Tất cả", "draft", "ready", "queued", "scheduled_fb", "retry", "published", "error"]
    status_filter = st.selectbox("Lọc trạng thái", status_options, format_func=lambda x: "Tất cả" if x == "Tất cả" else clean_status(x), key="posts_status_filter_v10")
    rows = store.list_posts(status=None if status_filter == "Tất cả" else status_filter, limit=500)
    if rows:
        st.dataframe(post_row_table(rows), use_container_width=True, hide_index=True, key="df_posts_rows_v10")
        labels = [f"{clean_status(r.get('status'))} | {r.get('title') or '(không tiêu đề)'} | {r.get('id')}" for r in rows]
        selected_label = st.selectbox("Chọn bài để sửa/quản lý", labels, key="posts_selected_label_v10")
        selected = rows[labels.index(selected_label)]; pid = selected["id"]
        with st.form("edit_post_form"):
            title = st.text_input("Tiêu đề", value=selected.get("title") or "", key=f"edit_title_{pid}")
            source_url = st.text_input("Link nguồn", value=selected.get("source_url") or "", key=f"edit_source_url_{pid}")
            post_text = st.text_area("Nội dung", value=selected.get("post_text") or "", height=250, key=f"edit_post_text_{pid}")
            first_comment = st.text_area("Bình luận nguồn", value=selected.get("first_comment") or "", height=100, key=f"edit_first_comment_{pid}")
            image_note = st.text_area("Gợi ý ảnh/link", value=selected.get("image_note") or "", height=80, key=f"edit_image_note_{pid}")
            campaign = st.text_input("Chiến dịch/nhóm nội dung", value=selected.get("campaign") or "", key=f"edit_campaign_{pid}")
            review_note = st.text_area("Ghi chú duyệt nội bộ", value=selected.get("review_note") or "", height=70, key=f"edit_review_note_{pid}")
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
        if q1.button("Đăng ngay bài này", type="primary", key=f"post_publish_now_{pid}"):
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
        if q2.button(f"Xếp lịch nội bộ {quick_slot.strftime('%d/%m %H:%M')}", key=f"post_schedule_local_{pid}"):
            schedule_local(store, pid, quick_slot.astimezone(timezone.utc).isoformat()); st.success("Đã xếp lịch nội bộ."); st.rerun()
        if q3.button(f"Hẹn Facebook {quick_slot.strftime('%d/%m %H:%M')}", key=f"post_schedule_fb_{pid}"):
            try:
                fb = create_feed_post(selected.get("post_text") or "", link=selected.get("source_url") or None, scheduled_at=quick_slot)
                store.update_post(pid, status="scheduled_fb", scheduled_at=quick_slot.astimezone(timezone.utc).isoformat(), schedule_mode="facebook_native", fb_post_id=str(fb.get("id") or ""), error="")
                store.add_log(pid, "schedule_selected_fb", True, f"Hẹn Facebook lúc {quick_slot.isoformat()}"); st.success("Đã hẹn Facebook."); st.rerun()
            except Exception as e:
                st.error(str(e))
        if q4.button("Xóa post trên Facebook", disabled=not bool(selected.get("fb_post_id")), key=f"post_delete_fb_{pid}"):
            try:
                delete_post(selected.get("fb_post_id")); store.add_log(pid, "delete_facebook", True, f"Đã xóa Facebook post {selected.get('fb_post_id')}"); st.success("Đã gửi lệnh xóa post Facebook.")
            except Exception as e:
                st.error(str(e))
        with st.expander("Log của bài"):
            logs = store.list_logs(post_id=pid, limit=50)
            st.dataframe(pd.DataFrame(logs), use_container_width=True, hide_index=True, key=f"df_post_logs_{pid}") if logs else st.caption("Chưa có log bài này.")
    else:
        st.caption("Kho chưa có bài.")

with tab_sources:
    st.subheader("🛰️ Nguồn RSS")
    c1, c2 = st.columns([1, 1.4])
    with c1:
        with st.form("add_source_form_v4"):
            name = st.text_input("Tên nguồn", placeholder="Ví dụ: Báo Nghệ An", key="source_name_v10")
            url = st.text_input("RSS URL", placeholder="https://.../rss", key="source_url_v10")
            category = st.text_input("Nhóm nguồn", value="RSS", key="source_category_v10")
            priority = st.slider("Ưu tiên nguồn", 1, 10, 5, key="source_priority_v10")
            if st.form_submit_button("Lưu nguồn", type="primary"):
                if not url.strip(): st.error("Chưa nhập RSS URL.")
                else:
                    store.add_source(name=name or url, url=url, category=category, priority=priority); st.success("Đã lưu nguồn RSS."); st.rerun()
    with c2:
        sources = store.list_sources(include_disabled=True)
        if sources:
            st.dataframe(pd.DataFrame(sources), use_container_width=True, hide_index=True, key="df_sources_v10")
            label = st.selectbox("Chọn nguồn để bật/tắt", [f"{'✅' if s.get('enabled') in [True, 1] else '❌'} {s.get('name')} | {s.get('id')}" for s in sources], key="source_select_v10")
            src = sources[[f"{'✅' if s.get('enabled') in [True, 1] else '❌'} {s.get('name')} | {s.get('id')}" for s in sources].index(label)]
            toggle = st.toggle("Bật nguồn này", value=bool(src.get("enabled")), key="source_enabled_toggle_v10")
            if st.button("Cập nhật nguồn", key="source_update_btn_v10"):
                store.update_source(src["id"], enabled=toggle); st.success("Đã cập nhật nguồn."); st.rerun()
        else:
            st.caption("Chưa có nguồn lưu trong DB. Bạn có thể cấu hình RSS_SOURCES trong Secrets.")


with tab_connect:
    st.subheader("🔐 Kết nối Facebook Page Beat Nghệ An")
    st.info("Bản v11 có 2 cách: ① bấm nút đăng nhập Facebook để lấy Page token tự động qua OAuth; ② nhập Page ID/token thủ công nếu bạn đã có token. Token không hiện đầy đủ trên giao diện và không được ghi vào GitHub.")

    status = credential_source()
    s1, s2, s3 = st.columns(3)
    s1.metric("Trạng thái", "Đã kết nối" if status.get("source") != "missing" else "Chưa kết nối")
    s2.metric("Nguồn cấu hình", status.get("source", "missing"))
    s3.metric("Token", status.get("masked_token", "Chưa có"))

    st.markdown("#### 1) Đăng nhập riêng cho khu kết nối")
    connect_password = str(secret("PAGE_CONNECT_PASSWORD", "") or secret("PAGE_CONNECT_PASSWORD_SHA256", "") or "")
    page_connect_unlocked = (not connect_password) or bool(st.session_state.get("page_connect_authed"))
    if connect_password and not page_connect_unlocked:
        cpw = st.text_input("Mật khẩu riêng để mở phần kết nối Page", type="password", key="page_connect_password_v11")
        if st.button("Mở khu kết nối", type="primary", key="page_connect_unlock_v11"):
            if secret_matches(cpw, "PAGE_CONNECT_PASSWORD"):
                st.session_state["page_connect_authed"] = True
                st.rerun()
            else:
                st.error("Sai mật khẩu kết nối Page.")
        st.caption("Khu kết nối Page đang khóa. Các tab khác vẫn dùng bình thường.")
    elif not connect_password:
        st.warning("Bạn chưa đặt PAGE_CONNECT_PASSWORD. Nên thêm mật khẩu riêng này trong Secrets để bảo vệ khu kết nối Page.")
    else:
        st.success("Đã mở khu kết nối Page trong phiên này.")

    def _query_value(name: str) -> str:
        try:
            val = st.query_params.get(name, "")
            if isinstance(val, list):
                return str(val[0] if val else "")
            return str(val or "")
        except Exception:
            return ""

    if page_connect_unlocked:
        st.markdown("#### 2) Cách mới: bấm nút đăng nhập Facebook")
        st.caption("Bạn bấm nút bên dưới → Facebook mở trang đăng nhập/cấp quyền → quay lại app → app liệt kê Page bạn quản lý → bạn chọn Beat Nghệ An để kết nối.")

        missing_oauth = oauth_missing_items()
        redirect_uri = str(secret("FB_OAUTH_REDIRECT_URI", "") or "")
        if missing_oauth:
            st.warning("Chưa bật được đăng nhập Facebook vì thiếu: " + ", ".join(missing_oauth))
            st.code('FB_APP_ID = "app_id_cua_meta_app"\nFB_APP_SECRET = "app_secret_cua_meta_app"\nFB_OAUTH_REDIRECT_URI = "https://ten-app.streamlit.app"\nFB_OAUTH_SCOPES = "pages_show_list,pages_read_engagement,pages_manage_posts,pages_manage_engagement"', language="toml")
        else:
            if "fb_oauth_state" not in st.session_state:
                st.session_state["fb_oauth_state"] = pysecrets.token_urlsafe(24)
            login_url = build_oauth_login_url(st.session_state["fb_oauth_state"], redirect_uri=redirect_uri)
            st.success("OAuth đã cấu hình. Redirect URI đang dùng: " + redirect_uri)
            lb1, lb2 = st.columns([1, 1])
            with lb1:
                if hasattr(st, "link_button"):
                    st.link_button("Đăng nhập Facebook để kết nối Page", login_url, type="primary", use_container_width=True)
                else:
                    st.markdown(f"[Đăng nhập Facebook để kết nối Page]({login_url})")
            with lb2:
                if st.button("Tạo lại mã bảo vệ OAuth", key="oauth_regen_state_v11"):
                    st.session_state["fb_oauth_state"] = pysecrets.token_urlsafe(24)
                    st.rerun()

        oauth_error = _query_value("error_message") or _query_value("error")
        if oauth_error:
            st.error("Facebook trả lỗi khi đăng nhập: " + oauth_error)

        oauth_code = _query_value("code")
        oauth_state = _query_value("state")
        if oauth_code:
            expected_state = str(st.session_state.get("fb_oauth_state", ""))
            if expected_state and oauth_state and oauth_state != expected_state:
                st.error("Mã bảo vệ OAuth không khớp. Hãy bấm 'Tạo lại mã bảo vệ OAuth' rồi đăng nhập lại Facebook.")
            elif not oauth_configured():
                st.error("App nhận được code từ Facebook nhưng thiếu FB_APP_ID/FB_APP_SECRET/FB_OAUTH_REDIRECT_URI trong Secrets.")
            else:
                if st.button("Hoàn tất kết nối từ Facebook", type="primary", key="oauth_finish_v11"):
                    try:
                        token_data = exchange_code_for_user_token(oauth_code, redirect_uri=redirect_uri)
                        user_token = token_data.get("access_token")
                        try:
                            long_data = exchange_long_lived_user_token(user_token)
                            user_token = long_data.get("access_token") or user_token
                            st.caption("Đã đổi sang user token dài hạn nếu Meta cho phép.")
                        except Exception as long_err:
                            st.caption(f"Không đổi được token dài hạn, vẫn dùng token hiện tại: {long_err}")
                        pages = get_pages_from_user_token(user_token)
                        if not pages:
                            st.warning("Facebook đăng nhập được nhưng không trả Page nào. Kiểm tra bạn có quyền quản trị Page và app đã được cấp pages_show_list/pages_manage_posts.")
                        else:
                            st.session_state["oauth_user_token"] = user_token
                            st.session_state["oauth_pages"] = pages
                            store.add_log(None, "facebook_oauth_pages", True, f"OAuth trả {len(pages)} Page")
                            st.success(f"Đã lấy được {len(pages)} Page. Hãy chọn Page ở mục bên dưới.")
                            try:
                                st.query_params.clear()
                            except Exception:
                                pass
                            st.rerun()
                    except Exception as e:
                        st.error(f"Không hoàn tất OAuth được: {e}")

        pages = st.session_state.get("oauth_pages") or []
        if pages:
            st.markdown("#### 3) Chọn Page sau khi đăng nhập Facebook")
            labels = []
            for pitem in pages:
                tasks = pitem.get("tasks") or pitem.get("perms") or []
                labels.append(f"{pitem.get('name')} · {pitem.get('id')} · quyền: {', '.join(tasks[:4]) if isinstance(tasks, list) else tasks}")
            selected_label = st.selectbox("Chọn Page muốn app đăng bài", labels, key="oauth_page_select_v11")
            selected_page = pages[labels.index(selected_label)]
            csel1, csel2 = st.columns(2)
            if csel1.button("Kết nối Page này cho phiên hiện tại", type="primary", key="oauth_connect_selected_v11"):
                try:
                    page_token = selected_page.get("access_token")
                    if not page_token:
                        st.error("Page này không có access_token. Hãy kiểm tra quyền app và vai trò Page.")
                    else:
                        info = test_connection_with_credentials(str(selected_page.get("id")), str(page_token))
                        set_session_credentials(str(selected_page.get("id")), str(page_token))
                        store.add_log(None, "facebook_oauth_connect_page", True, f"Đã kết nối OAuth tới Page {info.get('name')} ({info.get('id')})")
                        st.success(f"Đã kết nối Page: {info.get('name')} · ID {info.get('id')}")
                        st.rerun()
                except Exception as e:
                    st.error(f"Chưa kết nối được Page này: {e}")
            if csel2.button("Xóa danh sách Page OAuth", key="oauth_clear_pages_v11"):
                st.session_state.pop("oauth_pages", None)
                st.session_state.pop("oauth_user_token", None)
                st.success("Đã xóa danh sách Page lấy từ OAuth trong phiên.")
                st.rerun()

        st.markdown("#### 4) Cách dự phòng: nhập Page ID và Page Access Token thủ công")
        st.caption("Dùng cách này nếu bạn đã tự tạo Page Access Token. App chỉ lưu tạm trong phiên nếu bạn bấm lưu; khi app restart sẽ mất. Muốn ổn định lâu dài, hãy dán vào Streamlit Secrets.")
        fc1, fc2 = st.columns([0.8, 1.4])
        with fc1:
            page_id_input = st.text_input("FB_PAGE_ID", value=status.get("page_id") or "", placeholder="ID Page Beat Nghệ An", key="page_connect_page_id_v11")
        with fc2:
            token_input = st.text_input("FB_PAGE_ACCESS_TOKEN", type="password", placeholder="Dán Page Access Token ở đây", key="page_connect_token_v11")

        b1, b2, b3 = st.columns(3)
        if b1.button("Test token vừa nhập", type="primary", disabled=not bool(page_id_input and token_input), key="page_connect_test_input_v11"):
            try:
                info = test_connection_with_credentials(page_id_input, token_input)
                st.success(f"Kết nối được Page: {info.get('name')} · ID {info.get('id')}")
                st.json({"id": info.get("id"), "name": info.get("name"), "link": info.get("link"), "fan_count": info.get("fan_count")})
            except Exception as e:
                st.error(f"Chưa kết nối được: {e}")
        if b2.button("Lưu tạm trong phiên", disabled=not bool(page_id_input and token_input), key="page_connect_save_session_v11"):
            try:
                info = test_connection_with_credentials(page_id_input, token_input)
                set_session_credentials(page_id_input, token_input)
                store.add_log(None, "facebook_session_connect", True, f"Kết nối phiên tới Page {info.get('name')} ({info.get('id')})")
                st.success("Đã lưu tạm trong phiên. Bây giờ các nút đăng/hẹn giờ trong app sẽ dùng token này trước Secrets.")
                st.rerun()
            except Exception as e:
                st.error(f"Không lưu vì token chưa test thành công: {e}")
        if b3.button("Xóa kết nối tạm", key="page_connect_clear_session_v11"):
            clear_session_credentials()
            st.session_state.pop("page_connect_authed", None)
            st.success("Đã xóa kết nối tạm trong phiên.")
            st.rerun()

        st.markdown("#### 5) Kiểm tra Page đang dùng")
        c1, c2, c3 = st.columns(3)
        if c1.button("Test Page đang dùng", key="page_connect_test_current_v11"):
            try:
                st.json(test_connection())
            except Exception as e:
                st.error(str(e))
        if c2.button("Debug token sâu", key="page_connect_debug_token_v11"):
            try:
                data = debug_token()
                st.json(data)
            except Exception as e:
                st.error(str(e))
        if c3.button("Xem bài đã hẹn", key="page_connect_scheduled_posts_v11"):
            try:
                rows_sched = get_scheduled_posts(limit=50)
                st.dataframe(pd.DataFrame(rows_sched), use_container_width=True, hide_index=True, key="df_page_connect_scheduled_v11") if rows_sched else st.info("Chưa thấy bài hẹn giờ nào từ Facebook.")
            except Exception as e:
                st.error(str(e))

        st.markdown("#### 6) Cấu hình Secrets ổn định để deploy")
        secrets_block = (
            'APP_PASSWORD = "mat-khau-dang-nhap-app"\n'
            'PAGE_CONNECT_PASSWORD = "mat-khau-rieng-ket-noi-page"\n'
            'FB_APP_ID = "app_id_cua_meta_app"\n'
            'FB_APP_SECRET = "app_secret_cua_meta_app"\n'
            f'FB_OAUTH_REDIRECT_URI = "{redirect_uri or "https://ten-app.streamlit.app"}"\n'
            'FB_OAUTH_SCOPES = "pages_show_list,pages_read_engagement,pages_manage_posts,pages_manage_engagement"\n'
            f'FB_PAGE_ID = "{page_id_input or "id_page_beat_nghe_an"}"\n'
            'FB_PAGE_ACCESS_TOKEN = "PASTE_TOKEN_HERE_NEU_MUON_LUU_CO_DINH"\n'
            'FB_GRAPH_VERSION = "v25.0"\n'
        )
        st.code(secrets_block, language="toml")
        st.caption("Không commit file .streamlit/secrets.toml lên GitHub. Khi deploy Streamlit Cloud, dán phần này vào Advanced settings → Secrets.")

        with st.expander("Checklist đăng nhập Facebook đúng"):
            st.write("- Trong Meta Developer, tạo app và bật Facebook Login hoặc Facebook Login for Business.")
            st.write("- Valid OAuth Redirect URI phải trùng chính xác FB_OAUTH_REDIRECT_URI, ví dụ https://appvietbao.streamlit.app")
            st.write("- Tài khoản Facebook đăng nhập phải có quyền quản lý Page Beat Nghệ An.")
            st.write("- Quyền thường cần: pages_show_list, pages_read_engagement, pages_manage_posts, pages_manage_engagement.")
            st.write("- Khi app ở Development Mode, chỉ tài khoản có vai trò trong Meta App mới login được. Muốn người khác dùng phải qua cấu hình/review của Meta.")
            st.write("- Tuyệt đối không gửi token cho người khác, không dán token vào GitHub public, Facebook comment hoặc ảnh chụp màn hình.")

with tab_ops:
    st.subheader("🛡️ An toàn & vận hành")
    st.info("Mục này giúp kiểm tra bảo mật, bật chế độ test, rà bài rủi ro và xuất backup đầy đủ. Không có hệ thống nào an toàn tuyệt đối, nhưng các lớp này giúp giảm tối đa nguy cơ đăng nhầm, lộ token hoặc mất dữ liệu.")

    o1, o2, o3, o4 = st.columns(4)
    stats = store.stats(); counts = stats.get("posts_by_status", {})
    o1.metric("DRY RUN", "BẬT" if dry_run_mode() else "TẮT")
    o2.metric("Bài lỗi/retry", counts.get("error", 0) + counts.get("retry", 0))
    o3.metric("Đang hẹn", counts.get("queued", 0) + counts.get("scheduled_local", 0) + counts.get("scheduled_fb", 0))
    o4.metric("Vai trò", st.session_state.get("role", "unknown"))

    st.markdown("#### Kiểm tra bảo mật cấu hình")
    st.dataframe(pd.DataFrame(security_check_rows()), use_container_width=True, hide_index=True, key="df_security_checks_v10")

    st.markdown("#### Kiểm tra Page không lộ token")
    if st.button("Tạo báo cáo trạng thái Page an toàn", key="ops_page_status_report_v10"):
        st.json(page_status_report())

    st.markdown("#### Rà bài trước khi đăng")
    pending = store.list_posts(status="ready", limit=200)
    queued = store.list_posts(status="queued", limit=200) + store.list_posts(status="scheduled_local", limit=200)
    rows_gate = []
    for r in (pending + queued)[:300]:
        gate = publish_gate(r.get("post_text") or "", r.get("source_url") or "", r.get("title") or "", r.get("summary") or "", r.get("first_comment") or "")
        rows_gate.append({"ID": r.get("id"), "Trạng thái": clean_status(r.get("status")), "Tiêu đề": r.get("title"), "Kết quả": "OK" if gate.ok else "CHẶN", "Mức": gate.level, "Ghi chú": " | ".join(gate.notes[:3])})
    if rows_gate:
        st.dataframe(pd.DataFrame(rows_gate), use_container_width=True, hide_index=True, key="df_publish_gate_v10")
        block_ids = [x["ID"] for x in rows_gate if x["Kết quả"] == "CHẶN"]
        if block_ids and st.button(f"Chuyển {len(block_ids)} bài bị chặn về Nháp", key="ops_blocked_to_draft_v10"):
            for pid in block_ids:
                store.update_post(pid, status="draft", review_note="v9 safety gate: chuyển về nháp để duyệt lại")
                store.add_log(pid, "v9_safety_gate", True, "Chuyển về nháp vì không qua cổng an toàn trước khi đăng")
            st.success("Đã chuyển bài rủi ro về Nháp.")
            st.rerun()
    else:
        st.caption("Chưa có bài Ready/Queued để rà.")

    st.markdown("#### Backup đầy đủ")
    st.download_button("Tải backup JSON đầy đủ", data=backup_json_bytes(store), file_name="beat_nghe_an_v10_ops_backup.json", mime="application/json", key="ops_backup_download_v10")

    with st.expander("Secrets gợi ý cho bản v10"):
        st.code('''ADMIN_PASSWORD = "mat-khau-admin"
EDITOR_PASSWORD = "mat-khau-bien-tap"
VIEWER_PASSWORD = "mat-khau-chi-xem"
PAGE_CONNECT_PASSWORD = "mat-khau-rieng-ket-noi-page"

STORAGE_BACKEND = "supabase"
SUPABASE_URL = "https://xxx.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "service-role-key"

FB_PAGE_ID = "id_page_beat_nghe_an"
FB_PAGE_ACCESS_TOKEN = "page_access_token"
FB_GRAPH_VERSION = "v25.0"

DRY_RUN_MODE = false
BLOCK_HIGH_RISK_POSTS = true
MAX_RISK_SCORE_TO_PUBLISH = 64
MAX_POST_CHARS = 1800
AUTO_WORKER_DEFAULT = true
AUTO_REFRESH_SECONDS = 90
WORKER_BATCH_LIMIT = 5''', language="toml")


with tab_settings:
    st.subheader("🔌 Cài đặt & kiểm tra")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("#### Kiểm tra Facebook Page")
        if st.button("Test kết nối Page", type="primary", key="settings_test_page_v10"):
            try: st.json(test_connection())
            except Exception as e: st.error(str(e))
        if st.button("Debug token sâu", key="settings_debug_token_v10"):
            try: st.json(debug_token())
            except Exception as e: st.error(str(e))
        if st.button("Lấy danh sách bài đã hẹn trên Facebook", key="settings_get_scheduled_v10"):
            try:
                rows_sched = get_scheduled_posts(limit=50)
                st.dataframe(pd.DataFrame(rows_sched), use_container_width=True, hide_index=True, key="df_settings_scheduled_v10") if rows_sched else st.info("Facebook chưa trả về bài hẹn giờ nào.")
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
        st.download_button("Tải kho bài CSV", data=export_csv_bytes(store.export_posts()), file_name="beat_nghe_an_posts_export.csv", mime="text/csv", key="settings_posts_csv_v10")
        st.download_button("Tải log CSV", data=export_csv_bytes(store.list_logs(limit=1000)), file_name="beat_nghe_an_automation_logs.csv", mime="text/csv", key="settings_logs_csv_v10")

    st.divider()
    st.markdown("#### Health check nhanh trước khi deploy")
    st.caption("Kiểm tra package, timezone, lịch đăng, secrets, Supabase/SQLite và log. Nút này không đăng bài, không gọi Facebook nếu bạn chưa bấm test Page riêng.")
    if st.button("Chạy kiểm tra hệ thống", type="primary", key="settings_health_check_v10"):
        checks = run_health_checks(store)
        summary = health_summary(checks)
        h1, h2, h3, h4 = st.columns(4)
        h1.metric("Tổng", summary["total"])
        h2.metric("OK", summary["ok"])
        h3.metric("Cảnh báo", summary["warning"])
        h4.metric("Lỗi", summary["error"])
        st.dataframe(pd.DataFrame(health_rows(checks)), use_container_width=True, hide_index=True, key="df_health_checks_v10")

    st.divider()
    st.markdown("#### Gợi ý chạy nhanh, mượt, ổn định")
    st.write("- Dùng Supabase thay SQLite để không mất dữ liệu khi Streamlit ngủ/redeploy.")
    st.write("- Dùng Facebook native schedule cho bài link/text; hàng đợi nội bộ cần app đang mở hoặc GitHub Actions worker.")
    st.write("- AUTO_REFRESH_SECONDS nên để 60–180 giây. Đừng để quá thấp vì nặng app và dễ bị giới hạn API.")
    st.write("- RSS nên quét theo lô, cache vào article_cache, sau đó auto-draft từ cache để app mượt hơn.")
