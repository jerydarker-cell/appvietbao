# Beat Nghệ An AutoPost Pro v5 Ultra Stable

Web app Streamlit riêng cho Page **Beat Nghệ An**: quét RSS, chấm điểm tin hot, chống trùng, soạn bài an toàn, kiểm tra chất lượng, lập kế hoạch nội dung, hẹn giờ đăng Facebook Page và lưu dữ liệu vĩnh viễn bằng Supabase.

> Bản v5 tập trung vào vận hành lâu dài: nhanh, mượt, ổn định, có calendar lịch đăng, auto-draft, worker retry, backup đầy đủ và checklist chất lượng trước khi đăng.

## Tính năng chính

### 1. Nguồn tin & dashboard tin hot
- Thêm nhiều nguồn RSS.
- Quét RSS theo lô, cache vào Supabase/SQLite.
- Chấm điểm tin hot theo từ khóa Nghệ An, Vinh, xã/phường/thôn/bản, dân sinh, cảnh báo, thời tiết, giáo dục, hạ tầng, giá cả.
- Chống trùng tin bằng `content_hash`.
- Auto-draft tin hot nhưng vẫn giữ bước duyệt của chủ Page.

### 2. Soạn bài an toàn cho Beat Nghệ An
- Tạo bài viết lại bằng lời của Page.
- Bình luận nguồn ngắn, sạch, có link nguồn.
- Gợi ý nên dùng link preview hay ảnh tự thiết kế.
- Kiểm tra rủi ro: thiếu nguồn, giật tít, nhạy cảm, quá dài/quá ngắn.

### 3. Lịch hẹn đăng bài
- Hẹn Facebook native cho bài link/text.
- Hàng đợi nội bộ cho worker/app xử lý.
- Retry lỗi tự động, có log chi tiết.
- Xếp lịch hàng loạt theo khung giờ.
- GitHub Actions worker chạy định kỳ mỗi 15 phút.

### 4. V5 Ultra Stable mới
- Tab **Kế hoạch & chất lượng**.
- Calendar lịch đăng 3–30 ngày.
- Kế hoạch nội dung 7 ngày / nhiều ngày theo khung giờ thông minh.
- Gợi ý khung giờ tránh bài đã hẹn quá sát nhau.
- Checklist chất lượng hàng loạt cho nháp/sẵn sàng/lỗi.
- Chuyển bài chất lượng thấp về nháp để sửa.
- Phát hiện trùng bài theo link, hash và độ giống tiêu đề.
- Backup JSON đầy đủ: bài viết, nguồn RSS, tin cache, logs.
- Health-check log để biết app còn chạy ổn.

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

Bản v5 tương thích schema v4; nếu đã chạy schema v4 thì không cần tạo lại, chỉ cần deploy code mới.

## Secrets Streamlit Cloud

Dán trong **Advanced settings > Secrets**:

```toml
APP_PASSWORD = "mat-khau-rieng-cua-ban"
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

AUTO_WORKER_DEFAULT = true
AUTO_REFRESH_SECONDS = 90
WORKER_BATCH_LIMIT = 5
DEFAULT_POST_HOURS = "06:30,11:30,17:30,20:30"
MIN_SCHEDULE_MINUTES = 12
MAX_SCHEDULE_DAYS = 30

RSS_SOURCES = """
https://example.com/rss
"""
```

## GitHub Actions worker

File `.github/workflows/beatna-worker.yml` đã có sẵn. Worker dùng Supabase + Facebook token từ GitHub Secrets để xử lý hàng đợi nội bộ theo lịch.

Nên thêm GitHub Secrets tương ứng:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `FB_PAGE_ID`
- `FB_PAGE_ACCESS_TOKEN`
- `FB_GRAPH_VERSION`
- `APP_TIMEZONE`

## Cách dùng khuyến nghị

1. Thêm nguồn RSS.
2. Quét RSS và lưu cache.
3. Auto-draft tin hot.
4. Vào kho bài để sửa/duyệt.
5. Đánh dấu bài sẵn sàng.
6. Vào **Kế hoạch & chất lượng** để kiểm tra chất lượng và xem calendar.
7. Vào **Lịch hẹn đăng bài** để hẹn Facebook native hoặc hàng đợi nội bộ.

## Lưu ý an toàn cho Page

Không nên để app tự quét rồi đăng ồ ạt 100% không duyệt. Hướng an toàn nhất cho Beat Nghệ An là: app tự quét, tự soạn nháp, tự chống trùng và gợi ý lịch; bạn duyệt nhanh trước khi đăng/hẹn giờ. Không dùng ảnh/video báo nếu chưa có quyền; ưu tiên link preview hoặc ảnh tự thiết kế.
