from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from beatna.health import health_rows, health_summary, run_health_checks
from beatna.storage import get_store


def main() -> int:
    store = get_store()
    checks = run_health_checks(store)
    summary = health_summary(checks)
    print(json.dumps({"summary": summary, "checks": health_rows(checks)}, ensure_ascii=False, indent=2))
    return 1 if summary.get("error", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
