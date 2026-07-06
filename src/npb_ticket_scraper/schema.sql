-- 発売スケジュールのローカル状態を保持する SQLite スキーマ。
-- 日付・日時はすべて ISO 8601 文字列（TEXT）で保存する。生の日程データ（HTML/PDF）は保持しない。
-- 設計背景は docs/decisions.md を参照。

-- 試合の事実。自然キー = (game_date, home_team, away_team)。
CREATE TABLE IF NOT EXISTS game (
    id          INTEGER PRIMARY KEY,
    game_date   TEXT NOT NULL,          -- ISO date (YYYY-MM-DD)
    home_team   TEXT NOT NULL,          -- TeamId.value
    away_team   TEXT NOT NULL,          -- TeamId.value
    start_time  TEXT,                   -- ISO time (HH:MM[:SS]) / NULL
    venue       TEXT NOT NULL,
    season_type TEXT NOT NULL,          -- SeasonType.value
    UNIQUE (game_date, home_team, away_team)
);

-- 発売スケジュール本体。同一性は (selling_team, source_key)。内容変化は content_hash で検知する。
CREATE TABLE IF NOT EXISTS sale_schedule (
    id              INTEGER PRIMARY KEY,
    selling_team    TEXT NOT NULL,      -- TeamId.value（販売主体）
    source_key      TEXT NOT NULL,      -- アダプタ定義の安定キー
    sale_type       TEXT NOT NULL,      -- SaleType.value
    sale_label      TEXT NOT NULL,      -- 原文ラベル
    membership_rank TEXT,               -- 会員ランク原文 / NULL
    sale_start      TEXT,               -- ISO datetime / NULL
    sale_end        TEXT,               -- ISO datetime / NULL
    official_url    TEXT NOT NULL,
    source_url      TEXT NOT NULL,
    notes           TEXT,
    content_hash    TEXT NOT NULL,      -- SaleSchedule.content_fingerprint()
    status          TEXT NOT NULL,      -- ScheduleStatus.value
    first_seen_at   TEXT NOT NULL,      -- 初回観測時刻（ISO datetime）
    last_seen_at    TEXT NOT NULL,      -- 最終観測時刻
    last_changed_at TEXT NOT NULL,      -- 最終内容変化時刻
    UNIQUE (selling_team, source_key)
);

CREATE INDEX IF NOT EXISTS idx_sale_schedule_status ON sale_schedule (status);
CREATE INDEX IF NOT EXISTS idx_sale_schedule_selling_team ON sale_schedule (selling_team);
CREATE INDEX IF NOT EXISTS idx_sale_schedule_sale_start ON sale_schedule (sale_start);

-- 発売スケジュールと試合の多対多（1発売が複数試合をバンドルし得る）。
CREATE TABLE IF NOT EXISTS sale_schedule_game (
    schedule_id INTEGER NOT NULL REFERENCES sale_schedule (id) ON DELETE CASCADE,
    game_id     INTEGER NOT NULL REFERENCES game (id) ON DELETE CASCADE,
    PRIMARY KEY (schedule_id, game_id)
);

-- 内容変化の履歴（要確認レビュー時の差分参照用）。
CREATE TABLE IF NOT EXISTS sale_schedule_revision (
    id              INTEGER PRIMARY KEY,
    schedule_id     INTEGER NOT NULL REFERENCES sale_schedule (id) ON DELETE CASCADE,
    changed_at      TEXT NOT NULL,
    old_content_hash TEXT,              -- 直前の content_hash / 新規時は NULL
    new_content_hash TEXT NOT NULL,
    diff_json       TEXT NOT NULL       -- 変化した項目の JSON
);
