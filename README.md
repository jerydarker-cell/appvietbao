# Beat Nghệ An AutoPost Pro v11 OAuth Connect

Web app Streamlit riêng cho Page **Beat Nghệ An**: nhập tin từ đoạn chat **Beat Nghệ An Hourly**, quét RSS, chống trùng, tạo nháp/sẵn sàng, kiểm tra rủi ro, lập lịch hẹn đăng Facebook Page, lưu Supabase và kết nối Page bằng nút **Đăng nhập Facebook**.

Bản **v11 OAuth Connect** nâng cấp từ v10 Hotfix:

- Thêm luồng **Facebook OAuth**: bấm nút đăng nhập Facebook → Facebook xin quyền → quay lại app → chọn Page → app tự dùng Page access token trong phiên.
- Giữ cách nhập Page ID/token thủ công làm dự phòng.
- Không hiện token đầy đủ trên UI.
- Có mật khẩu riêng `PAGE_CONNECT_PASSWORD` cho khu kết nối Page.
- Giữ toàn bộ sửa lỗi `StreamlitDuplicateElementId` từ v10.
- Có health-check, smoke-test, audit widget key.

> Lưu ý: không có app nào cam kết 100% không lỗi trong mọi tình huống vì còn phụ thuộc Meta/Facebook API, quyền Page, token, mạng, Supabase và Streamlit Cloud. Bản này đã thêm kiểm tra, retry, khóa đăng trùng và chế độ `DRY_RUN_MODE` để giảm rủi ro tối đa.

## Chạy local

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Cấu hình đăng nhập Facebook bằng nút

Bạn cần tạo Meta App riêng trong Meta for Developers, bật Facebook Login/Facebook Login for Business và thêm **Valid OAuth Redirect URI** đúng URL app Streamlit, ví dụ:

```text
https://appvietbao.streamlit.app
```

Dán Secrets trong Streamlit Cloud:

```toml
ADMIN_PASSWORD = "mat-khau-admin"
PAGE_CONNECT_PASSWORD = "mat-khau-rieng-ket-noi-page"
APP_TIMEZONE = "Asia/Bangkok"

STORAGE_BACKEND = "supabase"
SUPABASE_URL = "https://xxx.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "service-role-key-cua-ban"

FB_GRAPH_VERSION = "v25.0"
FB_APP_ID = "app_id_meta_app"
FB_APP_SECRET = "app_secret_meta_app"
FB_OAUTH_REDIRECT_URI = "https://appvietbao.streamlit.app"
FB_OAUTH_SCOPES = "pages_show_list,pages_read_engagement,pages_manage_posts,pages_manage_engagement"
FB_APP_ACCESS_TOKEN = "APP_ID|APP_SECRET"

# Dự phòng nếu muốn lưu Page token cố định
FB_PAGE_ID = "id_page_beat_nghe_an"
FB_PAGE_ACCESS_TOKEN = "page_access_token_dai_han"

OPENAI_API_KEY = ""
OPENAI_MODEL = "gpt-4.1-mini"

DRY_RUN_MODE = true
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

Khi mới test nên để:

```toml
DRY_RUN_MODE = true
```

Khi đã test kết nối Page, lịch hẹn và đăng thử nội bộ ổn, đổi thành:

```toml
DRY_RUN_MODE = false
```

## Luồng kết nối Page trong app

1. Mở tab **Kết nối Facebook Page**.
2. Nhập mật khẩu khu Page.
3. Bấm **Đăng nhập Facebook để kết nối Page**.
4. Đăng nhập Facebook và cấp quyền.
5. Facebook quay lại app.
6. Bấm **Hoàn tất kết nối từ Facebook**.
7. Chọn Page **Beat Nghệ An**.
8. Bấm **Kết nối Page này cho phiên hiện tại**.
9. Bấm **Test Page đang dùng**.

## Kiểm tra trước khi deploy

```bash
python -m compileall .
python scripts/audit_streamlit_keys.py
python tests/smoke_test.py
python scripts/health_check.py
```

## Supabase

Chạy file SQL:

```text
sql/supabase_schema.sql
```

Nếu chưa cấu hình Supabase, app fallback SQLite để test, nhưng dùng thật lâu dài nên dùng Supabase.

## An toàn

- Không commit `.streamlit/secrets.toml` thật lên GitHub.
- Không chụp màn hình token.
- Không gửi `FB_APP_SECRET` hoặc Page token cho người khác.
- App chỉ dùng OAuth khi `FB_APP_ID`, `FB_APP_SECRET`, `FB_OAUTH_REDIRECT_URI` đã đủ.
- Nếu app ở Development Mode, chỉ tài khoản được thêm vai trò trong Meta App mới đăng nhập được.
