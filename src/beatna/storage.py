from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .article import ArticleInfo
from .config import lock_ttl_minutes, secret

DB_PATH = Path("data/beatna_v4.db")

POST_FIELDS = [
    "id", "created_at", "updated_at", "title", "source_url", "source_name", "summary", "source_image",
    "post_text", "first_comment", "image_note", "status", "scheduled_at", "schedule_mode", "fb_post_id",
    "fb_comment_id", "error", "risk_score", "risk_level", "risk_notes", "tags", "content_hash", "extra_json",
    "attempt_count", "last_attempt_at", "next_retry_at", "locked_at", "priority", "campaign", "post_type",
    "review_note", "publish_channel",
]
SOURCE_FIELDS = ["id", "created_at", "updated_at", "name", "url", "enabled", "priority", "category", "last_scan_at", "last_error"]
LOG_FIELDS = ["id", "created_at", "post_id", "action", "ok", "message", "extra_json"]
ARTICLE_FIELDS = [
    "id", "created_at", "updated_at", "title", "url", "source_name", "summary", "source_image", "published_at",
    "score", "reason", "content_hash", "sensitivity", "status", "drafted_post_id", "extra_json",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def _jsonable(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _article_data(article: ArticleInfo, status: str = "new", drafted_post_id: str = "") -> dict[str, Any]:
    now = utc_now_iso()
    return {
        "id": new_id(),
        "created_at": now,
        "updated_at": now,
        "title": article.title,
        "url": article.url,
        "source_name": article.source_name,
        "summary": article.description,
        "source_image": article.image,
        "published_at": article.published_at,
        "score": int(article.score or 0),
        "reason": article.reason,
        "content_hash": article.content_hash,
        "sensitivity": article.sensitivity,
        "status": status,
        "drafted_post_id": drafted_post_id,
        "extra_json": "",
    }


class BaseStore:
    backend_name = "base"
    def init(self) -> None: ...
    def add_post(self, **fields) -> str: ...
    def update_post(self, post_id: str, **fields) -> None: ...
    def get_post(self, post_id: str) -> dict[str, Any] | None: ...
    def list_posts(self, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]: ...
    def list_due_posts(self, now_iso: str, limit: int = 20) -> list[dict[str, Any]]: ...
    def try_claim_post(self, post_id: str, now_iso: str | None = None) -> bool: ...
    def find_post_by_hash(self, content_hash: str) -> dict[str, Any] | None: ...
    def add_source(self, name: str, url: str, category: str = "RSS", priority: int = 1) -> None: ...
    def update_source(self, source_id: str, **fields) -> None: ...
    def list_sources(self, include_disabled: bool = False) -> list[dict[str, Any]]: ...
    def upsert_article(self, article: ArticleInfo) -> None: ...
    def list_articles(self, min_score: int = 0, limit: int = 200, include_drafted: bool = False) -> list[dict[str, Any]]: ...
    def mark_article_drafted(self, content_hash: str, post_id: str) -> None: ...
    def add_log(self, post_id: str | None, action: str, ok: bool, message: str, extra_json: str = "") -> None: ...
    def list_logs(self, post_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]: ...
    def export_posts(self) -> list[dict[str, Any]]: ...
    def delete_post_local(self, post_id: str) -> None: ...
    def stats(self) -> dict[str, Any]: ...


class SQLiteStore(BaseStore):
    backend_name = "SQLite local / tạm thời"

    def connect(self) -> sqlite3.Connection:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL + busy_timeout makes the app more tolerant when Streamlit and a worker
        # touch the local database at nearly the same time. Supabase is still preferred
        # for production, but this makes local testing much smoother.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_columns(self, conn: sqlite3.Connection, table: str, fields: list[str]) -> None:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col in fields:
            if col not in existing:
                default = "INTEGER DEFAULT 0" if col in {"attempt_count", "risk_score", "priority", "score"} else "TEXT"
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {default}")

    def init(self) -> None:
        conn = self.connect()
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS posts (
            id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, title TEXT, source_url TEXT, source_name TEXT,
            summary TEXT, source_image TEXT, post_text TEXT NOT NULL, first_comment TEXT, image_note TEXT,
            status TEXT DEFAULT 'draft', scheduled_at TEXT, schedule_mode TEXT DEFAULT 'manual', fb_post_id TEXT,
            fb_comment_id TEXT, error TEXT, risk_score INTEGER DEFAULT 0, risk_level TEXT, risk_notes TEXT,
            tags TEXT, content_hash TEXT, extra_json TEXT, attempt_count INTEGER DEFAULT 0, last_attempt_at TEXT,
            next_retry_at TEXT, locked_at TEXT, priority INTEGER DEFAULT 0, campaign TEXT, post_type TEXT DEFAULT 'link',
            review_note TEXT, publish_channel TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_posts_status_scheduled ON posts(status, scheduled_at);
        CREATE INDEX IF NOT EXISTS idx_posts_hash ON posts(content_hash);
        CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at DESC);

        CREATE TABLE IF NOT EXISTS sources (
            id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, name TEXT, url TEXT UNIQUE, enabled INTEGER DEFAULT 1,
            priority INTEGER DEFAULT 1, category TEXT DEFAULT 'RSS', last_scan_at TEXT, last_error TEXT
        );

        CREATE TABLE IF NOT EXISTS article_cache (
            id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, title TEXT, url TEXT, source_name TEXT,
            summary TEXT, source_image TEXT, published_at TEXT, score INTEGER DEFAULT 0, reason TEXT,
            content_hash TEXT UNIQUE, sensitivity TEXT DEFAULT 'normal', status TEXT DEFAULT 'new', drafted_post_id TEXT,
            extra_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_article_score ON article_cache(score DESC, published_at DESC);
        CREATE INDEX IF NOT EXISTS idx_article_hash ON article_cache(content_hash);

        CREATE TABLE IF NOT EXISTS automation_logs (
            id TEXT PRIMARY KEY, created_at TEXT, post_id TEXT, action TEXT, ok INTEGER DEFAULT 1, message TEXT, extra_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_logs_created ON automation_logs(created_at DESC);
        """)
        self._ensure_columns(conn, "posts", POST_FIELDS)
        self._ensure_columns(conn, "sources", SOURCE_FIELDS)
        self._ensure_columns(conn, "article_cache", ARTICLE_FIELDS)
        conn.commit(); conn.close()

    def add_post(self, **fields) -> str:
        post_id = fields.get("id") or new_id(); now = utc_now_iso()
        data = {k: _jsonable(fields.get(k)) for k in POST_FIELDS}
        data.update({"id": post_id, "created_at": fields.get("created_at") or now, "updated_at": now})
        data["post_text"] = fields.get("post_text") or ""
        data["status"] = fields.get("status") or "draft"
        data["attempt_count"] = int(fields.get("attempt_count") or 0)
        data["priority"] = int(fields.get("priority") or 0)
        data["post_type"] = fields.get("post_type") or "link"
        cols = [k for k, v in data.items() if v is not None]
        vals = [data[k] for k in cols]
        conn = self.connect()
        conn.execute(f"INSERT INTO posts({', '.join(cols)}) VALUES ({', '.join(['?'] * len(cols))})", vals)
        conn.commit(); conn.close()
        return post_id

    def update_post(self, post_id: str, **fields) -> None:
        clean = {k: _jsonable(v) for k, v in fields.items() if k in POST_FIELDS and k != "id"}
        if not clean: return
        clean["updated_at"] = utc_now_iso()
        conn = self.connect(); cols = ", ".join(f"{k} = ?" for k in clean)
        conn.execute(f"UPDATE posts SET {cols} WHERE id = ?", (*clean.values(), post_id))
        conn.commit(); conn.close()

    def get_post(self, post_id: str) -> dict[str, Any] | None:
        conn = self.connect(); row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone(); conn.close()
        return dict(row) if row else None

    def list_posts(self, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        conn = self.connect()
        if status and status != "Tất cả":
            rows = conn.execute("SELECT * FROM posts WHERE status = ? ORDER BY scheduled_at IS NULL, scheduled_at ASC, created_at DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM posts ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        conn.close(); return [dict(r) for r in rows]

    def list_due_posts(self, now_iso: str, limit: int = 20) -> list[dict[str, Any]]:
        conn = self.connect()
        rows = conn.execute("""
        SELECT * FROM posts WHERE status IN ('queued','scheduled_local','retry') AND scheduled_at IS NOT NULL
        AND scheduled_at <= ? AND (next_retry_at IS NULL OR next_retry_at <= ?)
        ORDER BY priority DESC, scheduled_at ASC LIMIT ?
        """, (now_iso, now_iso, limit)).fetchall()
        conn.close(); return [dict(r) for r in rows]

    def try_claim_post(self, post_id: str, now_iso: str | None = None) -> bool:
        now_iso = now_iso or utc_now_iso()
        lock_expired = (datetime.now(timezone.utc) - timedelta(minutes=max(5, lock_ttl_minutes()))).isoformat()
        conn = self.connect()
        cur = conn.execute("""
        UPDATE posts SET status='publishing', locked_at=?, updated_at=?
        WHERE id=?
          AND status IN ('queued','scheduled_local','retry')
          AND scheduled_at IS NOT NULL
          AND scheduled_at <= ?
          AND (next_retry_at IS NULL OR next_retry_at = '' OR next_retry_at <= ?)
          AND (locked_at IS NULL OR locked_at = '' OR locked_at <= ?)
        """, (now_iso, now_iso, post_id, now_iso, now_iso, lock_expired))
        conn.commit()
        ok = cur.rowcount == 1
        conn.close()
        return ok

    def find_post_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        if not content_hash: return None
        conn = self.connect(); row = conn.execute("SELECT * FROM posts WHERE content_hash = ? ORDER BY created_at DESC LIMIT 1", (content_hash,)).fetchone(); conn.close()
        return dict(row) if row else None

    def add_source(self, name: str, url: str, category: str = "RSS", priority: int = 1) -> None:
        conn = self.connect(); now = utc_now_iso()
        conn.execute("""
        INSERT INTO sources(id, created_at, updated_at, name, url, enabled, priority, category)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(url) DO UPDATE SET name=excluded.name, enabled=1, priority=excluded.priority, category=excluded.category, updated_at=excluded.updated_at
        """, (new_id(), now, now, name or url, url, int(priority), category))
        conn.commit(); conn.close()

    def update_source(self, source_id: str, **fields) -> None:
        clean = {k: _jsonable(v) for k, v in fields.items() if k in SOURCE_FIELDS and k not in {"id", "created_at"}}
        if not clean: return
        clean["updated_at"] = utc_now_iso(); conn = self.connect(); cols = ", ".join(f"{k} = ?" for k in clean)
        conn.execute(f"UPDATE sources SET {cols} WHERE id = ?", (*clean.values(), source_id)); conn.commit(); conn.close()

    def list_sources(self, include_disabled: bool = False) -> list[dict[str, Any]]:
        conn = self.connect()
        if include_disabled:
            rows = conn.execute("SELECT * FROM sources ORDER BY priority DESC, name, url").fetchall()
        else:
            rows = conn.execute("SELECT * FROM sources WHERE enabled = 1 ORDER BY priority DESC, name, url").fetchall()
        conn.close(); return [dict(r) for r in rows]

    def upsert_article(self, article: ArticleInfo) -> None:
        data = _article_data(article)
        conn = self.connect()
        conn.execute("""
        INSERT INTO article_cache(id, created_at, updated_at, title, url, source_name, summary, source_image, published_at, score, reason, content_hash, sensitivity, status, drafted_post_id, extra_json)
        VALUES (:id,:created_at,:updated_at,:title,:url,:source_name,:summary,:source_image,:published_at,:score,:reason,:content_hash,:sensitivity,:status,:drafted_post_id,:extra_json)
        ON CONFLICT(content_hash) DO UPDATE SET title=excluded.title, url=excluded.url, source_name=excluded.source_name, summary=excluded.summary,
        source_image=excluded.source_image, published_at=excluded.published_at, score=excluded.score, reason=excluded.reason,
        sensitivity=excluded.sensitivity, updated_at=excluded.updated_at
        """, data)
        conn.commit(); conn.close()

    def list_articles(self, min_score: int = 0, limit: int = 200, include_drafted: bool = False) -> list[dict[str, Any]]:
        conn = self.connect()
        if include_drafted:
            rows = conn.execute("SELECT * FROM article_cache WHERE score >= ? ORDER BY score DESC, published_at DESC LIMIT ?", (min_score, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM article_cache WHERE score >= ? AND COALESCE(status,'new') != 'drafted' ORDER BY score DESC, published_at DESC LIMIT ?", (min_score, limit)).fetchall()
        conn.close(); return [dict(r) for r in rows]

    def mark_article_drafted(self, content_hash: str, post_id: str) -> None:
        if not content_hash: return
        conn = self.connect(); conn.execute("UPDATE article_cache SET status='drafted', drafted_post_id=?, updated_at=? WHERE content_hash=?", (post_id, utc_now_iso(), content_hash)); conn.commit(); conn.close()

    def add_log(self, post_id: str | None, action: str, ok: bool, message: str, extra_json: str = "") -> None:
        conn = self.connect(); conn.execute("INSERT INTO automation_logs(id, created_at, post_id, action, ok, message, extra_json) VALUES (?, ?, ?, ?, ?, ?, ?)", (new_id(), utc_now_iso(), post_id, action, 1 if ok else 0, message, _jsonable(extra_json))); conn.commit(); conn.close()

    def list_logs(self, post_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        conn = self.connect()
        if post_id:
            rows = conn.execute("SELECT * FROM automation_logs WHERE post_id=? ORDER BY created_at DESC LIMIT ?", (post_id, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM automation_logs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        conn.close(); return [dict(r) for r in rows]

    def export_posts(self) -> list[dict[str, Any]]:
        return self.list_posts(limit=5000)

    def delete_post_local(self, post_id: str) -> None:
        conn = self.connect(); conn.execute("DELETE FROM posts WHERE id=?", (post_id,)); conn.commit(); conn.close()

    def stats(self) -> dict[str, Any]:
        conn = self.connect()
        posts = conn.execute("SELECT status, COUNT(*) c FROM posts GROUP BY status").fetchall()
        active_sources = conn.execute("SELECT COUNT(*) c FROM sources WHERE enabled=1").fetchone()["c"]
        articles = conn.execute("SELECT COUNT(*) c FROM article_cache").fetchone()["c"]
        undrafted_hot = conn.execute("SELECT COUNT(*) c FROM article_cache WHERE score >= 25 AND COALESCE(status,'new') != 'drafted'").fetchone()["c"]
        conn.close()
        return {"posts_by_status": {r["status"]: r["c"] for r in posts}, "active_sources": active_sources, "articles_cached": articles, "undrafted_hot": undrafted_hot}


class SupabaseStore(BaseStore):
    backend_name = "Supabase/Postgres vĩnh viễn"

    def __init__(self) -> None:
        from supabase import create_client
        url = secret("SUPABASE_URL", "")
        key = secret("SUPABASE_SERVICE_ROLE_KEY", "") or secret("SUPABASE_KEY", "")
        if not url or not key:
            raise RuntimeError("Thiếu SUPABASE_URL hoặc SUPABASE_SERVICE_ROLE_KEY.")
        self.client = create_client(str(url), str(key))

    def init(self) -> None:
        # Tables must be created by sql/supabase_schema.sql. This lightweight query validates connection.
        self.client.table("posts").select("id").limit(1).execute()

    def add_post(self, **fields) -> str:
        post_id = fields.get("id") or new_id(); now = utc_now_iso()
        data = {k: _jsonable(fields.get(k)) for k in POST_FIELDS if fields.get(k) is not None}
        data.update({"id": post_id, "created_at": fields.get("created_at") or now, "updated_at": now, "post_text": fields.get("post_text") or "", "status": fields.get("status") or "draft", "attempt_count": int(fields.get("attempt_count") or 0), "priority": int(fields.get("priority") or 0), "post_type": fields.get("post_type") or "link"})
        self.client.table("posts").insert(data).execute(); return post_id

    def update_post(self, post_id: str, **fields) -> None:
        clean = {k: _jsonable(v) for k, v in fields.items() if k in POST_FIELDS and k not in {"id", "created_at"}}
        if not clean: return
        clean["updated_at"] = utc_now_iso(); self.client.table("posts").update(clean).eq("id", post_id).execute()

    def get_post(self, post_id: str) -> dict[str, Any] | None:
        rows = self.client.table("posts").select("*").eq("id", post_id).limit(1).execute().data or []
        return rows[0] if rows else None

    def list_posts(self, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        q = self.client.table("posts").select("*").order("created_at", desc=True).limit(limit)
        if status and status != "Tất cả": q = q.eq("status", status)
        return q.execute().data or []

    def list_due_posts(self, now_iso: str, limit: int = 20) -> list[dict[str, Any]]:
        return (self.client.table("posts").select("*").in_("status", ["queued", "scheduled_local", "retry"]).lte("scheduled_at", now_iso).or_(f"next_retry_at.is.null,next_retry_at.lte.{now_iso}").order("priority", desc=True).order("scheduled_at", desc=False).limit(limit).execute().data or [])

    def try_claim_post(self, post_id: str, now_iso: str | None = None) -> bool:
        now_iso = now_iso or utc_now_iso()
        lock_expired = (datetime.now(timezone.utc) - timedelta(minutes=max(5, lock_ttl_minutes()))).isoformat()
        try:
            res = (
                self.client.table("posts")
                .update({"status": "publishing", "locked_at": now_iso, "updated_at": now_iso})
                .eq("id", post_id)
                .in_("status", ["queued", "scheduled_local", "retry"])
                .lte("scheduled_at", now_iso)
                .or_(f"next_retry_at.is.null,next_retry_at.eq.,next_retry_at.lte.{now_iso}")
                .or_(f"locked_at.is.null,locked_at.eq.,locked_at.lte.{lock_expired}")
                .execute()
            )
            return bool(res.data)
        except Exception:
            # Fallback: avoid double-posting if the conditional claim query fails.
            # The worker will retry later and log the failure path from automation.py.
            return False

    def find_post_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        if not content_hash: return None
        rows = self.client.table("posts").select("*").eq("content_hash", content_hash).order("created_at", desc=True).limit(1).execute().data or []
        return rows[0] if rows else None

    def add_source(self, name: str, url: str, category: str = "RSS", priority: int = 1) -> None:
        data = {"id": new_id(), "name": name or url, "url": url, "enabled": True, "priority": int(priority), "category": category, "updated_at": utc_now_iso()}
        self.client.table("sources").upsert(data, on_conflict="url").execute()

    def update_source(self, source_id: str, **fields) -> None:
        clean = {k: _jsonable(v) for k, v in fields.items() if k in SOURCE_FIELDS and k not in {"id", "created_at"}}
        if not clean: return
        clean["updated_at"] = utc_now_iso(); self.client.table("sources").update(clean).eq("id", source_id).execute()

    def list_sources(self, include_disabled: bool = False) -> list[dict[str, Any]]:
        q = self.client.table("sources").select("*").order("priority", desc=True).order("name")
        if not include_disabled: q = q.eq("enabled", True)
        return q.execute().data or []

    def upsert_article(self, article: ArticleInfo) -> None:
        data = _article_data(article)
        self.client.table("article_cache").upsert(data, on_conflict="content_hash").execute()

    def list_articles(self, min_score: int = 0, limit: int = 200, include_drafted: bool = False) -> list[dict[str, Any]]:
        q = self.client.table("article_cache").select("*").gte("score", min_score).order("score", desc=True).order("published_at", desc=True).limit(limit)
        if not include_drafted:
            q = q.neq("status", "drafted")
        return q.execute().data or []

    def mark_article_drafted(self, content_hash: str, post_id: str) -> None:
        if not content_hash: return
        self.client.table("article_cache").update({"status": "drafted", "drafted_post_id": post_id, "updated_at": utc_now_iso()}).eq("content_hash", content_hash).execute()

    def add_log(self, post_id: str | None, action: str, ok: bool, message: str, extra_json: str = "") -> None:
        self.client.table("automation_logs").insert({"id": new_id(), "post_id": post_id, "action": action, "ok": ok, "message": message, "extra_json": _jsonable(extra_json), "created_at": utc_now_iso()}).execute()

    def list_logs(self, post_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        q = self.client.table("automation_logs").select("*").order("created_at", desc=True).limit(limit)
        if post_id: q = q.eq("post_id", post_id)
        return q.execute().data or []

    def export_posts(self) -> list[dict[str, Any]]:
        return self.list_posts(limit=5000)

    def delete_post_local(self, post_id: str) -> None:
        self.client.table("posts").delete().eq("id", post_id).execute()

    def stats(self) -> dict[str, Any]:
        rows = self.client.table("posts").select("status").limit(5000).execute().data or []
        sources = self.client.table("sources").select("id").eq("enabled", True).execute().data or []
        articles = self.client.table("article_cache").select("id").limit(5000).execute().data or []
        hot = self.client.table("article_cache").select("id").gte("score", 25).neq("status", "drafted").limit(5000).execute().data or []
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.get("status") or ""] = counts.get(row.get("status") or "", 0) + 1
        return {"posts_by_status": counts, "active_sources": len(sources), "articles_cached": len(articles), "undrafted_hot": len(hot)}


_STORE: BaseStore | None = None
_STORE_ERROR = ""


def get_store() -> BaseStore:
    global _STORE, _STORE_ERROR
    if _STORE is not None: return _STORE
    use_supabase = str(secret("STORAGE_BACKEND", "supabase") or "supabase").lower() == "supabase"
    if use_supabase:
        try:
            store = SupabaseStore(); store.init(); _STORE = store; _STORE_ERROR = ""; return _STORE
        except Exception as e:
            _STORE_ERROR = str(e)
    store = SQLiteStore(); store.init(); _STORE = store; return _STORE


def storage_warning() -> str:
    return _STORE_ERROR
