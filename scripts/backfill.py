#!/usr/bin/env python3
"""
一次性補資料腳本：補入過去 N 個交易日的篩選結果到 Supabase。

本地執行：
    SUPABASE_URL=xxx SUPABASE_KEY=yyy python scripts/backfill.py

透過 GitHub Actions（workflow_dispatch）執行時，
可在 Inputs 設定 BACKFILL_DAYS，預設 7 個交易日。
"""

import os
import sys
import logging
import requests
import pandas as pd
import yfinance as yf
from datetime import date, timedelta
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

BACKFILL_DAYS    = int(os.environ.get("BACKFILL_DAYS", "7"))
BATCH_SIZE       = 200
MIN_AVG_VOLUME   = 1_000_000
MIN_REL_VOLUME   = 1.5
ATR_PERIOD       = 14
ATR_LOW          = 2.5
ATR_HIGH         = 5.0
HIGH_PRICE_CUT   = 200.0


# ─── 工具函式 ─────────────────────────────────────────────────────────────────

def get_twse_stock_list() -> dict:
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    try:
        resp = requests.get(url, timeout=30, headers={"accept": "application/json"})
        resp.raise_for_status()
        return {
            item["Code"]: item["Name"]
            for item in resp.json()
            if item.get("Code", "").isdigit() and 4 <= len(item.get("Code", "")) <= 5
        }
    except Exception as e:
        log.error(f"無法取得股票清單: {e}")
        return {}


def calc_atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 1:
        return 0.0
    h, l, pc = df["High"], df["Low"], df["Close"].shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if pd.notna(val) else 0.0


def past_trading_days(n: int) -> list:
    """回傳最近 n 個交易日（週一～五），不含今天，最新在前"""
    days, d = [], date.today() - timedelta(days=1)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return days


def screen_on_date(target: date, df_full: pd.DataFrame) -> dict | None:
    """
    在完整歷史 DataFrame 中，模擬在 target 日當天收盤後執行選股。
    回傳篩選結果 dict，或 None（不符合條件 / 當日無交易）。
    """
    # 切出截至 target 日的資料
    df = df_full[df_full.index.date <= target].dropna(subset=["Close", "Volume"]).copy()

    if len(df) < 32:
        return None

    # 確認最後一筆就是 target（當日有開盤交易）
    last_date = df.index[-1]
    if hasattr(last_date, "date"):
        last_date = last_date.date()
    if last_date != target:
        return None

    close   = float(df["Close"].iloc[-1])
    volume  = float(df["Volume"].iloc[-1])
    avg_vol = float(df["Volume"].iloc[-31:-1].mean())

    if avg_vol < MIN_AVG_VOLUME:
        return None

    rel_vol = volume / avg_vol if avg_vol > 0 else 0.0
    if rel_vol < MIN_REL_VOLUME:
        return None

    atr = calc_atr(df, ATR_PERIOD)
    if atr < (ATR_HIGH if close >= HIGH_PRICE_CUT else ATR_LOW):
        return None

    # 條件四：均線多頭排列 5MA > 10MA > 24MA
    ma5  = df["Close"].rolling(5).mean().iloc[-1]
    ma10 = df["Close"].rolling(10).mean().iloc[-1]
    ma24 = df["Close"].rolling(24).mean().iloc[-1]
    if not (pd.notna(ma5) and pd.notna(ma10) and pd.notna(ma24)
            and ma5 > ma10 > ma24):
        return None

    return {
        "close":           float(round(close, 2)),
        "volume":          int(round(volume)),
        "avg_volume_30d":  int(round(avg_vol)),
        "relative_volume": float(round(rel_vol, 4)),
        "atr_14":          float(round(atr, 4)),
    }


# ─── 核心邏輯 ─────────────────────────────────────────────────────────────────

def download_batch(tickers: list) -> pd.DataFrame | None:
    end   = date.today() + timedelta(days=1)
    start = date.today() - timedelta(days=120)  # 120 天確保有足夠 ATR + 均量資料
    try:
        data = yf.download(
            tickers,
            start=start,
            end=end,
            auto_adjust=True,
            progress=False,
            threads=True,
            group_by="ticker",
        )
        return data
    except Exception as e:
        log.error(f"yfinance 下載失敗: {e}")
        return None


def save_records(sb, records: list) -> None:
    if not records:
        return
    sb.table("screening_results").upsert(
        records, on_conflict="date,symbol"
    ).execute()


def main():
    stock_names = get_twse_stock_list()
    if not stock_names:
        log.error("無法取得股票清單，請確認網路連線或證交所 API 可用性")
        sys.exit(1)

    target_dates = past_trading_days(BACKFILL_DAYS)
    log.info(f"補資料範圍：{BACKFILL_DAYS} 個交易日")
    log.info(f"目標日期: {[str(d) for d in target_dates]}")

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

    tickers = [f"{code}.TW" for code in stock_names]
    total_batches = (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE
    grand_total = 0

    for b_start in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[b_start : b_start + BATCH_SIZE]
        batch_num = b_start // BATCH_SIZE + 1
        log.info(f"下載第 {batch_num}/{total_batches} 批（{len(batch)} 支）…")

        data = download_batch(batch)
        if data is None or data.empty:
            log.warning(f"  批次 {batch_num} 無資料，跳過")
            continue

        single = len(batch) == 1

        # 為每個目標日期收集結果
        date_buckets: dict[str, list] = {d.isoformat(): [] for d in target_dates}

        for ticker in batch:
            code = ticker.replace(".TW", "")
            try:
                df_ticker = data if single else data.get(ticker)
                if df_ticker is None or df_ticker.empty:
                    continue

                for target in target_dates:
                    rec = screen_on_date(target, df_ticker)
                    if rec is None:
                        continue
                    rec["date"]   = target.isoformat()
                    rec["symbol"] = str(code)
                    rec["name"]   = str(stock_names.get(code, ""))
                    date_buckets[target.isoformat()].append(rec)

            except Exception as e:
                log.debug(f"處理 {ticker} 失敗: {e}")

        # 按日期寫入 Supabase
        for d_str, recs in date_buckets.items():
            if recs:
                save_records(sb, recs)
                grand_total += len(recs)
                log.info(f"  {d_str}: 寫入 {len(recs)} 筆（本批）")

    log.info(f"補資料完成，共寫入 {grand_total} 筆到 Supabase")


if __name__ == "__main__":
    main()
