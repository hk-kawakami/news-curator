-- ニュース管理サイト D1 スキーマ
-- 適用: npx wrangler d1 execute news-curator --remote --file=schema.sql

-- 配信元（config/sources.yaml から毎回同期）
CREATE TABLE IF NOT EXISTS sources (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  url         TEXT NOT NULL UNIQUE,
  category    TEXT NOT NULL,          -- it / keiba / stock / society / entame / trivia / lifehack / discovery
  enabled     INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 取得した記事
CREATE TABLE IF NOT EXISTS articles (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id     INTEGER NOT NULL REFERENCES sources(id),
  url           TEXT NOT NULL UNIQUE,
  title         TEXT NOT NULL,
  publisher     TEXT,                 -- 実際の媒体名（Googleニュース経由の記事で使用）
  description   TEXT,
  summary       TEXT,                 -- AI要約（要約バッチで設定）
  image_url     TEXT,
  category      TEXT NOT NULL,
  tags          TEXT,                 -- JSON配列（要約バッチで設定）
  published_at  TEXT,
  fetched_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_articles_fetched  ON articles(fetched_at);
CREATE INDEX IF NOT EXISTS idx_articles_summary  ON articles(summary) WHERE summary IS NULL;

-- その日に表示する記事と順番
CREATE TABLE IF NOT EXISTS daily_picks (
  date        TEXT NOT NULL,          -- YYYY-MM-DD（日本時間）
  edition     TEXT NOT NULL,          -- morning / evening
  article_id  INTEGER NOT NULL REFERENCES articles(id),
  rank        INTEGER NOT NULL,
  score       REAL NOT NULL,
  slot        TEXT NOT NULL,          -- main / discovery
  PRIMARY KEY (date, edition, article_id)
);

-- 評価（最新のみ保持）
CREATE TABLE IF NOT EXISTS ratings (
  article_id  INTEGER PRIMARY KEY REFERENCES articles(id),
  rating      INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
  rated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 評価以外の状態
CREATE TABLE IF NOT EXISTS article_states (
  article_id  INTEGER PRIMARY KEY REFERENCES articles(id),
  is_read     INTEGER NOT NULL DEFAULT 0,
  read_later  INTEGER NOT NULL DEFAULT 0,
  favorite    INTEGER NOT NULL DEFAULT 0,
  opened_at   TEXT
);

-- 学習した好みの重み
CREATE TABLE IF NOT EXISTS weights (
  feature_type  TEXT NOT NULL,        -- category / tag / source
  feature       TEXT NOT NULL,
  weight        REAL NOT NULL DEFAULT 0,
  rated_count   INTEGER NOT NULL DEFAULT 0,
  updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (feature_type, feature)
);

-- 非表示（配信元・キーワード）
CREATE TABLE IF NOT EXISTS mutes (
  type        TEXT NOT NULL,          -- source / keyword
  value       TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (type, value)
);

-- 初期の重み：関心分野7つを +1 で開始
INSERT OR IGNORE INTO weights (feature_type, feature, weight) VALUES
  ('category', 'it', 1), ('category', 'keiba', 1), ('category', 'stock', 1),
  ('category', 'society', 1), ('category', 'entame', 1), ('category', 'trivia', 1),
  ('category', 'lifehack', 1);
