import os
import logging
import traceback

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
    if not url or not key:
        missing = [name for name, val in [("SUPABASE_URL", url), ("SUPABASE_KEY", key)] if not val]
        msg = f"Missing environment variables: {', '.join(missing)}"
        log.error(msg)
        return None, msg
    try:
        return create_client(url, key), None
    except Exception as exc:
        log.error(f"create_client failed: {exc}\n{traceback.format_exc()}")
        return None, str(exc)


def _err(msg: str, status: int = 500) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": msg})


@app.get("/api/stocks")
async def get_stocks():
    sb, err = _get_sb()
    if err:
        return _err(err)
    try:
        result = (
            sb.table("screening_results")
            .select("*")
            .order("date", desc=True)
            .order("relative_volume", desc=True)
            .limit(1000)
            .execute()
        )
        return {"count": len(result.data), "results": result.data}
    except Exception as exc:
        log.error(f"get_stocks error:\n{traceback.format_exc()}")
        return _err(str(exc))


@app.get("/api/results")
async def get_results(date: str = None):
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
        log.error(f"get_results error:\n{traceback.format_exc()}")
        return _err(str(exc))


@app.get("/api/dates")
async def get_dates():
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
        return {"dates": dates[:60]}
    except Exception as exc:
        log.error(f"get_dates error:\n{traceback.format_exc()}")
        return _err(str(exc))
