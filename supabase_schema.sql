-- 在 Supabase Dashboard → SQL Editor 執行此檔案

CREATE TABLE IF NOT EXISTS screening_results (
    id          BIGSERIAL PRIMARY KEY,
    date        DATE          NOT NULL,
    symbol      VARCHAR(10)   NOT NULL,
    name        VARCHAR(50)   DEFAULT '',
    close       DECIMAL(10,2),
    volume      BIGINT,
    avg_volume_30d DECIMAL(15,2),
    relative_volume DECIMAL(8,4),
    atr_14      DECIMAL(10,4),
    created_at  TIMESTAMPTZ   DEFAULT NOW(),
    CONSTRAINT screening_results_date_symbol_key UNIQUE (date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_screening_date ON screening_results (date DESC);

-- 開啟 Row Level Security
ALTER TABLE screening_results ENABLE ROW LEVEL SECURITY;

-- 允許所有人讀取（公開資料）
CREATE POLICY "Public read access"
    ON screening_results FOR SELECT
    USING (true);
