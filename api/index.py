import os
import logging
import traceback

# Trigger redeploy — 2026-05-20
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# load_dotenv is local-dev only; no-op in Vercel production
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Critical imports — log full traceback before re-raising so Vercel Logs shows the root cause
try:
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from supabase import create_client, Client
except Exception:
    log.error("FATAL: failed to import required packages\n" + traceback.format_exc())
    raise

app = FastAPI(title="台股選股系統")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _get_sb():
    """Return (Client, None) on success, (None, error_str) on failure."""
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")

    # Emit presence of each variable (never log actual values)
    log.info(f"[env] SUPABASE_URL present={bool(url)}  SUPABASE_KEY present={bool(key)}")
    log.info(f"[env] total env vars visible to process: {len(os.environ)}")

    if not url or not key:
        missing = [name for name, val in [("SUPABASE_URL", url), ("SUPABASE_KEY", key)] if not val]
        log.error(f"[env] missing variables: {missing} — check Vercel project settings")
        return None, "Backend variables missing. Please check Vercel settings."

    try:
        log.info(f"[env] connecting to Supabase ({url[:32]}…)")
        client = create_client(url, key)
        log.info("[env] Supabase client ready")
        return client, None
    except Exception as exc:
        log.error(f"[env] create_client failed: {exc}\n{traceback.format_exc()}")
        return None, str(exc)


def _err(msg: str, status: int = 500) -> JSONResponse:
    # Return both "status/message" (human-readable) and "error" (legacy frontend compat)
    return JSONResponse(
        status_code=status,
        content={"status": "error", "message": msg, "error": msg},
    )


@app.get("/api/stocks")
async def get_stocks(date: str = None):
    """
    依日期取得篩選結果，依 relative_volume 降序排列。
    - date 未傳：自動取 Supabase 最新一天的資料。
    - date 已傳（YYYY-MM-DD）：只回傳該日資料。
    """
    sb, err = _get_sb()
    if err:
        return _err(err)
    try:
        if not date:
            latest = (
                sb.table("screening_results")
                .select("date")
                .order("date", desc=True)
                .limit(1)
                .execute()
            )
            date = latest.data[0]["date"] if latest.data else None

        if not date:
            return {"date": None, "count": 0, "results": []}

        result = (
            sb.table("screening_results")
            .select("*")
            .eq("date", date)
            .order("relative_volume", desc=True)
            .execute()
        )
        return {"date": date, "count": len(result.data), "results": result.data}
    except Exception as exc:
        log.error(f"get_stocks error:\n{traceback.format_exc()}")
        return _err(str(exc))


@app.get("/api/dates")
async def get_dates():
    """回傳最近 30 天有資料的不重複日期清單（降序）。"""
    sb, err = _get_sb()
    if err:
        return _err(err)
    try:
        result = (
            sb.table("screening_results")
            .select("date")
            .order("date", desc=True)
            .execute()
        )
        dates = list(dict.fromkeys(r["date"] for r in result.data))
        return {"dates": dates[:30]}
    except Exception as exc:
        log.error(f"get_dates error:\n{traceback.format_exc()}")
        return _err(str(exc))
