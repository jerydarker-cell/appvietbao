from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app.py"
WIDGETS = {
    "button", "download_button", "text_input", "text_area", "selectbox", "slider",
    "toggle", "radio", "file_uploader", "multiselect", "date_input", "time_input",
    "dataframe", "data_editor",
}
PATTERN = re.compile(r"(?:st|\w+)\.({})\(([^\n]*)".format("|".join(sorted(WIDGETS))))


def main() -> int:
    missing: list[str] = []
    duplicates: dict[str, list[int]] = {}
    for lineno, line in enumerate(APP.read_text(encoding="utf-8").splitlines(), start=1):
        m = PATTERN.search(line)
        if not m:
            continue
        widget, args = m.group(1), m.group(2)
        if "key=" not in args:
            missing.append(f"L{lineno}: {widget}: {line.strip()}")
        km = re.search(r"key\s*=\s*([furbFURB]*)(['\"])(.*?)\2", args)
        if km and not km.group(1).lower().startswith("f"):
            key = km.group(3)
            duplicates.setdefault(key, []).append(lineno)
    duplicate_keys = {k: v for k, v in duplicates.items() if len(v) > 1}
    if missing:
        print("Missing explicit Streamlit keys:")
        print("\n".join(missing))
    if duplicate_keys:
        print("Duplicate explicit keys:")
        for k, lines in duplicate_keys.items():
            print(k, lines)
    if missing or duplicate_keys:
        return 1
    print("OK: all audited Streamlit widgets have explicit keys and no repeated static keys.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
