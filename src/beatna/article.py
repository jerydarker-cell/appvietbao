from __future__ import annotations

import hashlib
import html
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

from .config import hot_keywords, sensitive_keywords


@dataclass
class ArticleInfo:
    title: str = ""
    description: str = ""
    source_name: str = ""
    url: str = ""
    image: str = ""
    published_at: str = ""
    score: int = 0
    reason: str = ""
    content_hash: str = ""
    sensitivity: str = "normal"

    def to_dict(self) -> dict:
        return asdict(self)


def clean_html(text: str) -> str:
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    out = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", html.unescape(out)).strip()


def clean_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def domain_name(url: str) -> str:
    try:
        host = urlparse(url).netloc.replace("www.", "")
        return host or "nguồn báo"
    except Exception:
        return "nguồn báo"


def stable_hash(*parts: str) -> str:
    raw = "|".join((p or "").strip().lower() for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def parse_dt(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = parsedate_to_datetime(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            return ""


def detect_sensitivity(title: str, description: str) -> str:
    text = f"{title} {description}".lower()
    hits = [kw for kw in sensitive_keywords() if kw and kw in text]
    if any(x in text for x in ["tử vong", "chết", "hiếp", "tự tử", "trẻ em", "đuối nước"]):
        return "high"
    if hits:
        return "medium"
    return "normal"


def score_article(title: str, description: str, source_name: str = "", published_at: str = "") -> tuple[int, str]:
    text = f"{title} {description} {source_name}".lower()
    score = 0
    hits: list[str] = []
    for kw in hot_keywords():
        if kw and kw in text:
            score += 10 if kw in {"nghệ an", "vinh", "khẩn", "nóng", "cảnh báo"} else 5
            hits.append(kw)
    for kw in ["xã", "phường", "thôn", "bản", "khối", "làng", "chợ", "trường", "cầu", "đường", "bà con"]:
        if kw in text:
            score += 3
    if published_at:
        try:
            dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            age_hours = max(0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
            if age_hours <= 2:
                score += 20
            elif age_hours <= 8:
                score += 14
            elif age_hours <= 24:
                score += 8
            elif age_hours <= 72:
                score += 3
        except Exception:
            pass
    sensitivity = detect_sensitivity(title, description)
    if sensitivity == "high":
        score += 6
    elif sensitivity == "medium":
        score += 3
    score = min(score, 100)
    reason = "Trùng từ khóa: " + ", ".join(list(dict.fromkeys(hits))[:10]) if hits else "Tin mới từ RSS, chưa thấy từ khóa nóng rõ ràng"
    return score, reason


def fetch_article_meta(url: str, timeout: int = 14) -> ArticleInfo:
    headers = {
        "User-Agent": "Mozilla/5.0 BeatNgheAnPrivateTool/4.0 (+Streamlit private editorial tool)",
        "Accept-Language": "vi,en;q=0.9",
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    def meta(*keys: str) -> str:
        for key in keys:
            tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
            if tag and tag.get("content"):
                return clean_spaces(tag["content"])
        return ""

    title = meta("og:title", "twitter:title") or (soup.title.string.strip() if soup.title and soup.title.string else "")
    desc = meta("og:description", "description", "twitter:description")
    image = meta("og:image", "twitter:image")
    published = meta("article:published_time", "pubdate", "publishdate", "date")
    published_at = parse_dt(published)
    source_name = domain_name(url)
    score, reason = score_article(title, desc, source_name, published_at)
    return ArticleInfo(
        title=title,
        description=desc,
        source_name=source_name,
        url=url,
        image=image,
        published_at=published_at,
        score=score,
        reason=reason,
        content_hash=stable_hash(title, url),
        sensitivity=detect_sensitivity(title, desc),
    )


def _entry_image(entry) -> str:
    for attr in ("media_content", "media_thumbnail"):
        vals = getattr(entry, attr, None)
        if vals and isinstance(vals, list):
            first = vals[0]
            if isinstance(first, dict) and first.get("url"):
                return first["url"]
    links = getattr(entry, "links", []) or []
    for link in links:
        if isinstance(link, dict) and str(link.get("type", "")).startswith("image") and link.get("href"):
            return link["href"]
    return ""


def read_feed(url: str, limit: int = 20) -> list[ArticleInfo]:
    parsed = feedparser.parse(url)
    source_title = clean_html(getattr(parsed.feed, "title", "")) or domain_name(url)
    items: list[ArticleInfo] = []
    for entry in parsed.entries[:limit]:
        title = clean_html(getattr(entry, "title", ""))
        summary = clean_html(getattr(entry, "summary", "") or getattr(entry, "description", ""))
        link = getattr(entry, "link", "")
        published_raw = getattr(entry, "published", "") or getattr(entry, "updated", "")
        published_at = parse_dt(published_raw)
        score, reason = score_article(title, summary, source_title, published_at)
        items.append(ArticleInfo(
            title=title,
            description=summary,
            source_name=source_title,
            url=link,
            image=_entry_image(entry),
            published_at=published_at,
            score=score,
            reason=reason,
            content_hash=stable_hash(title, link),
            sensitivity=detect_sensitivity(title, summary),
        ))
    return items


def merge_dedupe_articles(groups: Iterable[Iterable[ArticleInfo]]) -> list[ArticleInfo]:
    seen: set[str] = set()
    out: list[ArticleInfo] = []
    for group in groups:
        for item in group:
            key = item.content_hash or stable_hash(item.title, item.url)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
    return sorted(out, key=lambda x: (x.score, x.published_at or ""), reverse=True)
