-- Beat Nghệ An AutoPost Pro v6 ChatGPT Bridge
-- Chạy trong Supabase > SQL Editor > Run

create extension if not exists pgcrypto;

create table if not exists posts (
  id text primary key,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  title text,
  source_url text,
  source_name text,
  summary text,
  source_image text,
  post_text text not null default '',
  first_comment text,
  image_note text,
  status text default 'draft',
  scheduled_at timestamptz,
  schedule_mode text default 'manual',
  fb_post_id text,
  fb_comment_id text,
  error text,
  risk_score integer default 0,
  risk_level text,
  risk_notes text,
  tags text,
  content_hash text,
  extra_json text,
  attempt_count integer default 0,
  last_attempt_at timestamptz,
  next_retry_at timestamptz,
  locked_at timestamptz,
  priority integer default 0,
  campaign text,
  post_type text default 'link',
  review_note text,
  publish_channel text default 'facebook_page'
);

create index if not exists idx_posts_status_scheduled on posts(status, scheduled_at);
create index if not exists idx_posts_hash on posts(content_hash);
create index if not exists idx_posts_created on posts(created_at desc);

create table if not exists sources (
  id text primary key,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  name text,
  url text unique,
  enabled boolean default true,
  priority integer default 1,
  category text default 'RSS',
  last_scan_at timestamptz,
  last_error text
);

create table if not exists article_cache (
  id text primary key,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  title text,
  url text,
  source_name text,
  summary text,
  source_image text,
  published_at timestamptz,
  score integer default 0,
  reason text,
  content_hash text unique,
  sensitivity text default 'normal',
  status text default 'new',
  drafted_post_id text,
  extra_json text
);

create index if not exists idx_article_score on article_cache(score desc, published_at desc);
create index if not exists idx_article_hash on article_cache(content_hash);
create index if not exists idx_article_status on article_cache(status);

create table if not exists automation_logs (
  id text primary key,
  created_at timestamptz default now(),
  post_id text,
  action text,
  ok boolean default true,
  message text,
  extra_json text
);

create index if not exists idx_logs_created on automation_logs(created_at desc);
create index if not exists idx_logs_post on automation_logs(post_id);

-- RLS: vì app cá nhân dùng service role key server-side, có thể để RLS tắt cho các bảng private này.
-- Bản v5 dùng cùng schema, bổ sung planner/checklist bằng code app nên không cần thêm bảng mới.
alter table posts disable row level security;
alter table sources disable row level security;
alter table article_cache disable row level security;
alter table automation_logs disable row level security;

-- Index phụ trợ v5 cho lịch đăng và campaign
create index if not exists idx_posts_campaign on posts(campaign);
create index if not exists idx_posts_risk on posts(risk_score);
