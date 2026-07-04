from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import asdict, dataclass
from io import BytesIO
from typing import Any, Iterable
from urllib.parse import urlparse

from .article import ArticleInfo, clean_spaces, detect_sensitivity, domain_name, score_article, stable_hash
from .composer import make_rule_based_post

URL_RE = re.compile(r"https?://[^\s\)\]\}\"'<>]+", re.I)
COPY_POST_MARKERS = [
    "copy bài đăng", "bài đăng", "copy-ready post", "nội dung bài đăng", "post facebook", "caption"
]
COMMENT_MARKERS = ["copy bình luận", "bình luận ghim", "first comment", "nguồn:", "link nguồn"]
IMAGE_MARKERS = ["gợi ý ảnh", "gợi ý hiển thị", "ảnh/link", "image", "link preview"]


@dataclass
class ImportedChatItem:
    title: str = ""
    source_url: str = ""
    source_name: str = ""
    summary: str = ""
    post_text: str = ""
    first_comment: str = ""
    image_note: str = ""
    confidence: int = 0
    note: str = ""
    content_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean_url(url: str) -> str:
    return (url or "").strip().rstrip(".,;。)】]>\"")


def _source_name(url: str) -> str:
    return domain_name(url) if url else "ChatGPT / Beat Nghệ An Hourly"


def _strip_markup(line: str) -> str:
    line = re.sub(r"^[\s#>\-*•\d\.\)\(\[\]✅🔥📰📌👉⚠️🚨🌧️⛈️]+", "", line or "").strip()
    line = re.sub(r"\*\*|__|`", "", line).strip()
    line = re.sub(r"^(tiêu đề|title|chủ đề|headline)\s*[:：-]\s*", "", line, flags=re.I).strip()
    line = re.sub(r"^tin\s*\d+\s*[:：-]\s*", "", line, flags=re.I).strip()
    return clean_spaces(line)


def _looks_like_title(line: str) -> bool:
    l = _strip_markup(line)
    if not l or len(l) < 12 or len(l) > 180:
        return False
    low = l.lower()
    bad = ["nguồn:", "link:", "http", "hashtag", "gợi ý", "copy", "bình luận", "ảnh", "lưu ý", "@beat nghệ an"]
    if any(x in low for x in bad):
        return False
    return True


def _best_title(block: str, url: str) -> str:
    raw_lines = block.splitlines()
    lines = [_strip_markup(x) for x in raw_lines]
    before_url = []
    for line in lines:
        if url and url in line:
            break
        before_url.append(line)

    # Ưu tiên dòng tiêu đề thật ở đầu block: "Tin 1:", dòng markdown heading, hoặc dòng viết hoa rõ.
    early = [x for x in before_url[:12] if _looks_like_title(x)]
    if early:
        scored = []
        for i, x in enumerate(early):
            raw = raw_lines[i] if i < len(raw_lines) else x
            score = 0
            if re.search(r"tin\s*\d+\s*[:：-]", raw, re.I): score += 6
            if raw.lstrip().startswith("#"): score += 4
            if sum(1 for c in x if c.isupper()) >= max(4, len(x) // 3): score += 3
            if len(x) > 22: score += 1
            scored.append((score, -i, x))
        scored.sort(reverse=True)
        if scored[0][0] > 0:
            return scored[0][2]

    candidates = [x for x in reversed(before_url[-12:]) if _looks_like_title(x)]
    if candidates:
        return candidates[0]
    candidates = [x for x in lines[:18] if _looks_like_title(x)]
    return candidates[0] if candidates else "Tin từ Beat Nghệ An Hourly"


def _summary_from_block(block: str, title: str, url: str) -> str:
    lines: list[str] = []
    for raw in block.splitlines():
        line = _strip_markup(raw)
        if not line or line == title or url in line:
            continue
        low = line.lower()
        if any(m in low for m in COPY_POST_MARKERS + COMMENT_MARKERS + IMAGE_MARKERS):
            continue
        if line.startswith("#") or len(line) < 16:
            continue
        lines.append(line)
    out = " ".join(lines[:4])
    return clean_spaces(out[:650])


def _extract_section(block: str, markers: Iterable[str], stop_markers: Iterable[str]) -> str:
    lines = block.splitlines()
    start = None
    for i, raw in enumerate(lines):
        low = raw.lower()
        if any(m in low for m in markers):
            start = i + 1
            # keep line content after colon if it has useful payload
            after = re.split(r"[:：]", raw, maxsplit=1)
            seed = after[1].strip() if len(after) > 1 and len(after[1].strip()) > 8 else ""
            collected = [seed] if seed else []
            for nxt in lines[start:]:
                nlow = nxt.lower().strip()
                if any(m in nlow for m in stop_markers) and collected:
                    break
                if nlow.startswith("---"):
                    break
                collected.append(nxt)
            return clean_spaces("\n".join([x for x in collected if x.strip()]))
    return ""


def _post_from_block(block: str, title: str, url: str, summary: str, source_name: str) -> tuple[str, str, str]:
    low = block.lower()
    post = ""
    comment = ""
    image_note = ""
    if any(m in low for m in COPY_POST_MARKERS):
        post = _extract_section(block, COPY_POST_MARKERS, COMMENT_MARKERS + IMAGE_MARKERS)
    if any(m in low for m in COMMENT_MARKERS):
        comment = _extract_section(block, COMMENT_MARKERS, IMAGE_MARKERS + COPY_POST_MARKERS)
    if any(m in low for m in IMAGE_MARKERS):
        image_note = _extract_section(block, IMAGE_MARKERS, COMMENT_MARKERS + COPY_POST_MARKERS)
    # Clean accidental source/comment leakage inside post.
    if post:
        post_lines = []
        for x in post.splitlines():
            if URL_RE.fullmatch(x.strip()):
                continue
            post_lines.append(x)
        post = "\n".join(post_lines).strip()
    if comment and URL_RE.fullmatch(comment.strip()):
        comment = f"Nguồn: {comment.strip()}"
    if not comment and url:
        comment = f"Nguồn: {url}"
    if not post:
        generated = make_rule_based_post(title, url, summary, source_name, tone="Tin nhanh")
        post = generated.get("post_text", "")
        comment = comment or generated.get("first_comment", "")
        image_note = image_note or generated.get("image_note", "")
    return post.strip(), comment.strip(), image_note.strip()


def _block_around_url(text: str, match: re.Match[str], all_matches: list[re.Match[str]]) -> str:
    idx = all_matches.index(match)
    prev_end = all_matches[idx - 1].end() if idx > 0 else 0
    next_start = all_matches[idx + 1].start() if idx + 1 < len(all_matches) else len(text)
    start = max(prev_end, match.start() - 2400)
    end = min(next_start, match.end() + 2600)
    # Prefer human-visible boundaries if present.
    pre = text.rfind("\n\n", start, match.start())
    if pre != -1 and match.start() - pre < 1800:
        start = pre
    post = text.find("\n\n", match.end(), end)
    if post != -1 and post - match.end() < 2200:
        end = post
    return text[start:end].strip()


def parse_chatgpt_hourly_text(raw_text: str, max_items: int = 80) -> list[ImportedChatItem]:
    text = (raw_text or "").replace("\r\n", "\n")
    matches = list(URL_RE.finditer(text))
    items: list[ImportedChatItem] = []
    seen: set[str] = set()
    for m in matches:
        url = _clean_url(m.group(0))
        if not url or url in seen:
            continue
        seen.add(url)
        block = _block_around_url(text, m, matches)
        title = _best_title(block, url)
        source_name = _source_name(url)
        summary = _summary_from_block(block, title, url)
        score, reason = score_article(title, summary, source_name, "")
        post_text, first_comment, image_note = _post_from_block(block, title, url, summary, source_name)
        confidence = 60
        if title and title != "Tin từ Beat Nghệ An Hourly": confidence += 15
        if summary: confidence += 8
        if post_text: confidence += 7
        if first_comment and url in first_comment: confidence += 5
        confidence = min(100, confidence)
        chash = stable_hash(title, url)
        items.append(ImportedChatItem(
            title=title,
            source_url=url,
            source_name=source_name,
            summary=summary,
            post_text=post_text,
            first_comment=first_comment,
            image_note=image_note or "Ưu tiên dùng link preview hoặc ảnh tự thiết kế. Không dùng lại ảnh báo nếu chưa có quyền.",
            confidence=confidence,
            note=reason,
            content_hash=chash,
        ))
        if len(items) >= max_items:
            break
    return items


def _flatten_message_parts(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        out = []
        for part in value:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                # ChatGPT export parts can be dicts with text/content fields.
                out.append(str(part.get("text") or part.get("content") or part.get("value") or ""))
        return "\n".join([x for x in out if x])
    if isinstance(value, dict):
        return _flatten_message_parts(value.get("parts") or value.get("text") or value.get("content"))
    return str(value)


def _conversation_texts_from_json(data: Any, title_filter: str = "") -> list[tuple[str, str]]:
    conversations = data if isinstance(data, list) else data.get("conversations", []) if isinstance(data, dict) else []
    out: list[tuple[str, str]] = []
    for conv in conversations:
        if not isinstance(conv, dict):
            continue
        title = str(conv.get("title") or conv.get("name") or "Không tiêu đề")
        if title_filter and title_filter.lower() not in title.lower():
            continue
        chunks: list[str] = []
        mapping = conv.get("mapping")
        if isinstance(mapping, dict):
            for node in mapping.values():
                msg = node.get("message") if isinstance(node, dict) else None
                if not isinstance(msg, dict):
                    continue
                role = ((msg.get("author") or {}).get("role") or "").lower()
                if role not in {"assistant", "user"}:
                    continue
                content = msg.get("content") or {}
                text = _flatten_message_parts(content.get("parts") if isinstance(content, dict) else content)
                if text:
                    chunks.append(text)
        else:
            msgs = conv.get("messages") or []
            for msg in msgs:
                text = _flatten_message_parts(msg.get("content") if isinstance(msg, dict) else msg)
                if text:
                    chunks.append(text)
        joined = "\n\n".join(chunks)
        if joined:
            out.append((title, joined))
    return out


def parse_chatgpt_export_bytes(data: bytes, filename: str = "", title_filter: str = "Beat Nghệ An") -> list[ImportedChatItem]:
    name = (filename or "").lower()
    texts: list[tuple[str, str]] = []
    if name.endswith(".zip"):
        with zipfile.ZipFile(BytesIO(data)) as zf:
            candidates = [n for n in zf.namelist() if n.endswith("conversations.json") or n.endswith("chat.html") or n.endswith(".txt")]
            for n in candidates:
                raw = zf.read(n)
                if n.endswith(".json"):
                    try:
                        texts.extend(_conversation_texts_from_json(json.loads(raw.decode("utf-8")), title_filter=title_filter))
                    except Exception:
                        pass
                else:
                    texts.append((n, raw.decode("utf-8", errors="ignore")))
    elif name.endswith(".json"):
        texts.extend(_conversation_texts_from_json(json.loads(data.decode("utf-8", errors="ignore")), title_filter=title_filter))
    else:
        texts.append((filename or "uploaded_text", data.decode("utf-8", errors="ignore")))

    merged: list[ImportedChatItem] = []
    seen: set[str] = set()
    for title, body in texts:
        parsed = parse_chatgpt_hourly_text(body)
        for item in parsed:
            key = item.content_hash or item.source_url
            if key in seen:
                continue
            seen.add(key)
            item.note = f"Từ ChatGPT export: {title}. " + (item.note or "")
            merged.append(item)
    return merged


def imported_to_article(item: ImportedChatItem | dict[str, Any]) -> ArticleInfo:
    if isinstance(item, dict):
        item = ImportedChatItem(**{k: item.get(k, "") for k in ImportedChatItem.__dataclass_fields__.keys()})
    score, reason = score_article(item.title, item.summary, item.source_name, "")
    return ArticleInfo(
        title=item.title,
        description=item.summary,
        source_name=item.source_name or _source_name(item.source_url),
        url=item.source_url,
        image="",
        published_at="",
        score=score,
        reason=item.note or reason,
        content_hash=item.content_hash or stable_hash(item.title, item.source_url),
        sensitivity=detect_sensitivity(item.title, item.summary),
    )


def table_rows(items: Iterable[ImportedChatItem | dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, raw in enumerate(items, start=1):
        item = ImportedChatItem(**raw) if isinstance(raw, dict) else raw
        rows.append({
            "STT": idx,
            "Tin cậy": item.confidence,
            "Tiêu đề": item.title,
            "Nguồn": item.source_name,
            "Link": item.source_url,
            "Có bài sẵn": "Có" if item.post_text else "Không",
            "Ghi chú": item.note,
        })
    return rows
