# Beat Nghệ An AutoPost Pro v9 Secure Ops

Web app Streamlit riêng cho Page **Beat Nghệ An**: nhập tin từ đoạn chat **Beat Nghệ An Hourly**, quét RSS, chống trùng, tạo nháp/sẵn sàng, kiểm tra rủi ro, lập lịch hẹn đăng Facebook Page và lưu dữ liệu vĩnh viễn bằng Supabase.

Bản **v9 Secure Ops** giữ toàn bộ chức năng v8 và nâng cấp thêm lớp vận hành an toàn: đăng nhập theo vai trò, dry-run test không đăng thật, cổng rà bài trước khi đăng, backup JSON đầy đủ, kiểm tra Page không lộ token và Facebook API retry ổn định hơn.

## Tính năng chính

### 1. Nhập tin từ ChatGPT / Beat Nghệ An Hourly
- Tab **Nhập từ ChatGPT**.
- Dán nội dung từ đoạn chat Beat Nghệ An Hourly hoặc upload `.txt`, `.json`, `.zip` export.
- Tự tách nhiều tin, nhận diện tiêu đề, link nguồn, nội dung bài đăng, bình luận nguồn, gợi ý ảnh/link preview.
- Tạo nháp hoặc bài **Sẵn sàng** hàng loạt.
- Chống trùng theo link và `content_hash`.

### 2. Kết nối Facebook Page an toàn hơn
- Tab **Kết nối Facebook Page** riêng.
- Có mật khẩu riêng `PAGE_CONNECT_PASSWORD` hoặc `PAGE_CONNECT_PASSWORD_SHA256`.
- Token nhập bằng ô password, bị che, không ghi vào GitHub.
- Có test Page, debug token, xem bài đã hẹn.
- Ưu tiên Streamlit Secrets để chạy ổn định lâu dài.

### 3. An toàn & vận hành v9
- Tab **An toàn & vận hành** mới.
- Đăng nhập theo vai trò: `ADMIN_PASSWORD`, `EDITOR_PASSWORD`, `VIEWER_PASSWORD` hoặc dạng SHA256.
- `DRY_RUN_MODE`: test đăng/hẹn giờ không đăng thật lên Facebook.
- `BLOCK_HIGH_RISK_POSTS`: chặn bài vượt ngưỡng rủi ro trước khi đăng.
- `MAX_RISK_SCORE_TO_PUBLISH`, `MAX_POST_CHARS` để giảm đăng nhầm bài nhạy cảm/quá dài.
- Rà hàng loạt bài Ready/Queued và chuyển bài rủi ro về Nháp.
- Backup JSON đầy đủ: bài viết, nguồn RSS, tin cache, logs.

### 4. Lịch hẹn đăng bài
- Hẹn Facebook native cho bài link/text.
- Hàng đợi nội bộ cho worker/app xử lý.
- Retry lỗi tự động, có log chi tiết.
- Xếp lịch hàng loạt theo khung giờ.
- GitHub Actions worker chạy định kỳ mỗi 15 phút.

### 5. Tốc độ & ổn định
- RSS có timeout/User-Agent.
- Facebook API có retry cho lỗi mạng/rate limit tạm thời.
- SQLite local bật WAL + busy timeout để test mượt hơn.
- Supabase dùng cho dữ liệu vĩnh viễn khi deploy thật.
- Health-check và smoke-test có sẵn.

## Chạy local

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Cấu hình Supabase

1. Tạo project Supabase.
2. Vào SQL Editor.
3. Chạy file:

```text
sql/supabase_schema.sql
```

Nếu đã chạy schema từ v4/v5/v6/v7/v8 thì bản v9 vẫn tương thích.

## Secrets Streamlit Cloud

Dán trong **Advanced settings > Secrets**:

```toml
ADMIN_PASSWORD = "mat-khau-admin"
EDITOR_PASSWORD = "mat-khau-bien-tap"
VIEWER_PASSWORD = "mat-khau-chi-xem"
PAGE_CONNECT_PASSWORD = "mat-khau-rieng-de-mo-ket-noi-page"
APP_TIMEZONE = "Asia/Bangkok"

STORAGE_BACKEND = "supabase"
SUPABASE_URL = "https://xxx.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "service-role-key-cua-ban"

FB_PAGE_ID = "id_page_beat_nghe_an"
FB_PAGE_ACCESS_TOKEN = "page_access_token_dai_han"
FB_GRAPH_VERSION = "v25.0"
FB_APP_ACCESS_TOKEN = "APP_ID|APP_SECRET"

OPENAI_API_KEY = ""
OPENAI_MODEL = "gpt-4.1-mini"

DRY_RUN_MODE = false
BLOCK_HIGH_RISK_POSTS = true
MAX_RISK_SCORE_TO_PUBLISH = 64
MAX_POST_CHARS = 1800
MIN_MINUTES_BETWEEN_POSTS = 20

AUTO_WORKER_DEFAULT = true
AUTO_REFRESH_SECONDS = 90
WORKER_BATCH_LIMIT = 5
DEFAULT_POST_HOURS = "06:30,11:30,17:30,20:30"
MIN_SCHEDULE_MINUTES = 12
MAX_SCHEDULE_DAYS = 30
LOCK_TTL_MINUTES = 20

RSS_SOURCES = """
https://example.com/rss
"""
```

### Dùng mật khẩu SHA256 thay vì lưu mật khẩu thường

```bash
python scripts/generate_password_hash.py
```

Sau đó dán vào Secrets dạng:

```toml
ADMIN_PASSWORD_SHA256 = "hash_o_day"
PAGE_CONNECT_PASSWORD_SHA256 = "hash_o_day"
```

## Cách dùng với Beat Nghệ An Hourly

1. Mở đoạn chat **Beat Nghệ An Hourly** trong ChatGPT.
2. Copy phần tin/bài muốn đăng.
3. Mở app → tab **Nhập từ ChatGPT**.
4. Dán nội dung vào ô nhập.
5. Bấm **Phân tích nội dung ChatGPT**.
6. Chọn tin đúng → bấm **Tạo nháp** hoặc **Tạo bài Sẵn sàng**.
7. Sang **An toàn & vận hành** để rà bài.
8. Sang **Lịch hẹn đăng bài** để hẹn Facebook native hoặc hàng đợi nội bộ.

## Kiểm tra trước khi deploy

```bash
python -m compileall .
python tests/smoke_test.py
python scripts/health_check.py
```

## Lưu ý an toàn

Không thể bảo đảm 100% không lỗi trong mọi tình huống vì còn phụ thuộc Facebook token, quyền Page, Supabase, mạng và API Meta. Bản v9 đã thêm role login, token masking, dry-run, retry, safety gate, backup, log và health-check để giảm rủi ro tối đa khi dùng thật.
