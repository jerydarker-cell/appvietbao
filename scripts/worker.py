from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from beatna.automation import auto_draft_hot_articles, publish_due_posts, scan_feeds_to_cache
from beatna.config import as_bool, rss_sources, secret
from beatna.storage import get_store


def main() -> None:
    store = get_store()
    if as_bool(os.getenv("WORKER_SCAN_FEEDS", secret("WORKER_SCAN_FEEDS", False)), False):
        urls = list(rss_sources())
        for src in store.list_sources():
            if src.get("url"):
                urls.append(str(src["url"]))
        urls = list(dict.fromkeys([u for u in urls if u]))
        if urls:
            result = scan_feeds_to_cache(store, urls, per_feed=int(os.getenv("WORKER_PER_FEED", "15")))
            print(f"Scanned {len(urls)} feeds, cached {len(result['items'])} items, errors={len(result['errors'])}")
    if as_bool(os.getenv("WORKER_AUTO_DRAFT", secret("WORKER_AUTO_DRAFT", False)), False):
        ids = auto_draft_hot_articles(
            store,
            min_score=int(os.getenv("WORKER_MIN_SCORE", "25")),
            limit=int(os.getenv("WORKER_AUTO_DRAFT_LIMIT", "5")),
            status=os.getenv("WORKER_DRAFT_STATUS", "draft"),
            use_ai=as_bool(os.getenv("WORKER_USE_AI", "false"), False),
        )
        print(f"Auto-drafted/seen {len(ids)} posts")
    results = publish_due_posts(store, limit=int(os.getenv("WORKER_BATCH_LIMIT", "10")))
    print(results)


if __name__ == "__main__":
    main()
