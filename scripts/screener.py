#!/usr/bin/env python3
"""台股選股腳本 — 每日收盤後由 GitHub Actions 執行"""

import os
import sys
import logging
import requests
import pandas as pd
import yfinance as yf
from datetime import date, timedelta
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

BATCH_SIZE = 200
MIN_AVG_VOLUME = 1_000_000
MIN_REL_VOLUME = 1.5
ATR_PERIOD = 14
ATR_LOW_THRESHOLD = 2.5
ATR_HIGH_THRESHOLD = 5.0
HIGH_PRICE_CUTOFF = 200.0


def get_last_completed_trading_day() -> date:
    """
    方案 B：找出最近一個「已完整收盤」的交易日（週一～五，嚴格排除今日）。
    不論在盤前、盤中、假日執行，一律鎖定前一個完整交易日，
    防止盤中資料未累積完全就寫入 Supabase 產生失真訊號。
    台灣國定假日由 yfinance 自動過濾（假日當天該股票沒有資料列）。
    """
    d = date.today() - timedelta(days=1)
    while d.weekday() >= 5:  # 跳過週六(5)、週日(6)
        d -= timedelta(days=1)
    return d


def get_twse_stock_list() -> dict:
    """從台灣證交所 OpenAPI 取得上市股票代碼與名稱"""
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    try:
        resp = requests.get(url, timeout=30, headers={"accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        stocks = {}
        for item in data:
            code = item.get("Code", "")
            name = item.get("Name", "")
            # 只取 4~5 碼純數字的一般股票
            if code.isdigit() and 4 <= len(code) <= 5:
                stocks[code] = name
        log.info(f"取得 {len(stocks)} 支上市股票")
        return stocks
    except Exception as e:
        log.error(f"無法取得股票清單: {e}")
        return {}


def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 1:
        return 0.0
    high = df["High"]
    low = df["Low"]
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = tr.rolling(window=period).mean()
    val = atr.iloc[-1]
    return float(val) if pd.notna(val) else 0.0


def screen_batch(tickers: list, stock_names: dict, target_date: date) -> list:
    # 方案 B：只下載到 target_date，確保絕不含盤中未完成數據
    end = target_date + timedelta(days=1)
    start = target_date - timedelta(days=90)  # 約 60 個交易日

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
    except Exception as e:
        log.error(f"yfinance 下載失敗: {e}")
        return []

    results = []
    single = len(tickers) == 1

    for ticker in tickers:
        code = ticker.replace(".TW", "")
        try:
            df = data if single else data.get(ticker)
            if df is None or df.empty:
                continue

            df = df.dropna(subset=["Close", "Volume"]).copy()
            if len(df) < 32:
                continue

            # 方案 B 防呆：確認最後一筆確實是 target_date（台灣國定假日無此列 → 自動跳過）
            last_idx = df.index[-1]
            last_date = last_idx.date() if hasattr(last_idx, "date") else last_idx
            if last_date != target_date:
                continue

            today_close = float(df["Close"].iloc[-1])
            today_volume = float(df["Volume"].iloc[-1])

            # 30 日均量（不含今日）
            avg_vol_30d = float(df["Volume"].iloc[-31:-1].mean())

            # 條件一：30 日均量 > 100 萬
            if avg_vol_30d < MIN_AVG_VOLUME:
                continue

            # 條件二：相對成交量 > 1.5
            rel_vol = today_volume / avg_vol_30d if avg_vol_30d > 0 else 0.0
            if rel_vol < MIN_REL_VOLUME:
                continue

            # 條件三：ATR(14)
            atr = calculate_atr(df, ATR_PERIOD)
            threshold = ATR_HIGH_THRESHOLD if today_close >= HIGH_PRICE_CUTOFF else ATR_LOW_THRESHOLD
            if atr < threshold:
                continue

            # 【核心修復點】全面強制轉換為 Python 標準原生型態，防止 Supabase JSON 序列化失敗
            results.append(
                {
                    "date": target_date.isoformat(),
                    "symbol": str(code),
                    "name": str(stock_names.get(code, "")),
                    "close": float(round(today_close, 2)),
                    "volume": int(round(today_volume)),
                    "avg_volume_30d": int(round(avg_vol_30d)),
                    "relative_volume": float(round(rel_vol, 4)),
                    "atr_14": float(round(atr, 4)),
                }
            )
        except Exception as e:
            log.debug(f"處理 {ticker} 時發生錯誤: {e}")
            continue

    return results


def save_to_supabase(results: list) -> None:
    if not results:
        log.info("無符合條件的股票，不寫入資料庫")
        return

    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]

    # 使用官方強型別初始化
    sb: Client = create_client(url, key)

    try:
        # 純 insert 執行
        sb.table("screening_results").insert(results).execute()
        log.info(f"已寫入 {len(results)} 筆結果至 Supabase")
    except Exception as e:
        log.error(f"寫入 Supabase 失敗: {e}")
        raise e


def run_screening() -> int:
    target_date = get_last_completed_trading_day()
    log.info(f"開始台股選股... 目標交易日：{target_date}（方案 B 靜態鎖定）")

    stock_names = get_twse_stock_list()
    if not stock_names:
        log.warning("無法取得股票清單，今日可能為非交易日")
        return 0

    tickers = [f"{code}.TW" for code in stock_names]
    log.info(f"共 {len(tickers)} 支股票待篩選")

    all_results = []
    total_batches = (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i : i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        log.info(f"處理第 {batch_num}/{total_batches} 批（{len(batch)} 支）...")
        batch_results = screen_batch(batch, stock_names, target_date)
        all_results.extend(batch_results)
        log.info(f"  → 本批符合: {len(batch_results)} 支")

    log.info(f"篩選完成，共 {len(all_results)} 支股票符合條件")
    for r in sorted(all_results, key=lambda x: x["relative_volume"], reverse=True):
        log.info(
            f"  {r['symbol']} {r['name']}: "
            f"收={r['close']}, 相對量={r['relative_volume']:.2f}x, ATR={r['atr_14']:.2f}"
        )

    save_to_supabase(all_results)
    return len(all_results)


if __name__ == "__main__":
    count = run_screening()
    sys.exit(0)
